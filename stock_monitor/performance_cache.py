"""Bounded read cache; SQLite commit generation invalidates internal/external writes."""
import copy,sqlite3,threading
from collections import OrderedDict
from contextlib import contextmanager

def install_input_revisions(db):
    """Transactional dependency revisions; no journal or global setting clock."""
    db.execute('CREATE TABLE IF NOT EXISTS input_revisions(scope TEXT PRIMARY KEY, revision INTEGER NOT NULL)')
    specs={'candles':"'market:'||{r}.isin||':'||{r}.base_tf",
           'series':"'market:'||{r}.isin||':'||{r}.base_tf",
           'settings':"'setting:'||{r}.key",'instruments':"'instrument:'||{r}.isin"}
    for table,scope in specs.items():
        columns=[row[1] for row in db.execute('PRAGMA table_info('+table+')')]
        same=' AND '.join('NEW.'+c+' IS '+c for c in columns)
        equal=' AND '.join('NEW.'+c+' IS OLD.'+c for c in columns)
        if table=='settings':
            # Transport/check timestamps do not change identity, candles or DQ.
            # Keep every other field, including state, ranges, session and source.
            def meaningful(prefix):
                key=prefix+'key';value=prefix+'value'
                return (f"CASE WHEN json_valid({value}) THEN CASE "
                    f"WHEN {key} LIKE 'data_quality:%' THEN json_remove({value},'$.checked_at','$.raw_retry_after','$.parent_child_revisions') "
                    f"WHEN {key} LIKE 'history_identity:%' OR {key} LIKE 'history_coverage:%' THEN json_remove({value},'$.fetched_at') "
                    f"ELSE {value} END ELSE {value} END")
            same='NEW.key IS key AND '+meaningful('NEW.')+' IS '+meaningful('')
            equal='NEW.key IS OLD.key AND '+meaningful('NEW.')+' IS '+meaningful('OLD.')
            # Upgrade existing dev databases transactionally; no data rewriting.
            for action in ('insert','update'):
                db.execute('DROP TRIGGER IF EXISTS input_settings_'+action)
        def bump(row):
            return 'INSERT INTO input_revisions VALUES('+scope.format(r=row)+',1) ON CONFLICT(scope) DO UPDATE SET revision=revision+1;'
        db.execute(f'CREATE TRIGGER IF NOT EXISTS input_{table}_insert BEFORE INSERT ON {table} WHEN NOT EXISTS(SELECT 1 FROM {table} WHERE {same}) BEGIN {bump("NEW")} END')
        db.execute(f'CREATE TRIGGER IF NOT EXISTS input_{table}_delete AFTER DELETE ON {table} BEGIN {bump("OLD")} END')
        db.execute(f'CREATE TRIGGER IF NOT EXISTS input_{table}_update AFTER UPDATE ON {table} WHEN NOT ({equal}) BEGIN {bump("OLD")} {bump("NEW")} END')

class InputRevisionChanged(RuntimeError):
    """A captured dependency changed; only this error permits bounded retry."""


def frame_dependency(isin, frame, tf, now):
    """Describe the actual closed raw range used by an existing calculation."""
    import pandas as pd
    from analytics import bar_ends
    from timeframes import base_for
    if frame.empty:return None
    meta=frame.attrs
    if not all(meta.get(k) for k in ('source','symbol','venue','currency','timezone')):return None
    view=frame.copy(deep=False);view.attrs={**meta,'timeframe':tf}
    ends=bar_ends(view);closed=ends[ends<=now]
    if not len(closed):return None
    spec=dict(isin=isin,identity={k:meta[k] for k in ('source','symbol','venue','currency','timezone')},
                base_tf=meta.get('aggregation_base') or base_for(tf,meta['source']),
                through=pd.Timestamp(closed.max()).isoformat(),timeframe=tf)
    if meta.get('analysis_scope'):spec['from']=meta['analysis_scope']['start']
    spec['recovered_inputs']=[s for item in meta.get('parent_recovery_inputs',[]) if
        (not spec.get('from') or pd.Timestamp(item['parent_at'])>=pd.Timestamp(spec['from']))
        and pd.Timestamp(item['parent_at']).tz_localize(meta['timezone'])<closed.max()
        for s in item['inputs']]
    return spec


def _digest(value):
    import hashlib,json
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str,separators=(',',':')).encode()).hexdigest()


def _setting_value(db, key, start=None):
    import json
    row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    if row is None:return None
    value=json.loads(row[0])
    if isinstance(value,dict):
        if key.startswith('data_quality:'):
            value={k:v for k,v in value.items() if k not in ('checked_at','raw_retry_after','parent_child_revisions')}
            if start:
                from calculation_window import scope_report
                value=scope_report(value,start)
        elif key.startswith(('history_identity:','history_coverage:')):
            value={k:v for k,v in value.items() if k!='fetched_at'}
        elif key.startswith('gettex_chart:'):
            # A later HTTP receipt can contain exactly the same candles.
            # Closed OHLCV have their own digest; retain identity, listing
            # status/evidence, quality, archive faults and native-month proof.
            value={k:v for k,v in value.items() if k not in ('received_at','planned_at','requested_to','receipt','sha256','live_receipt','provisional_only')}
            if isinstance(value.get('listing_proof'),dict):
                value['listing_proof']={k:v for k,v in value['listing_proof'].items() if k!='verified_at'}
        elif key.startswith('benchmark_history:'):
            value={k:v for k,v in value.items() if k!='attempted_at'}
    return value


def _closed_rows(db, spec):
    import pandas as pd
    ident=spec['identity'];args=[spec['isin'],ident['source'],spec['base_tf']]
    if ident['source']=='gettex':
        sql='SELECT ts,open,high,low,close,volume FROM candles WHERE isin=? AND source=? AND base_tf=?'
    else:
        sql='SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND base_tf=? AND symbol=? AND venue=? AND currency=?'
        args += [ident['symbol'],ident['venue'],ident['currency']]
    rows=db.execute(sql+' ORDER BY ts',args).fetchall()
    if not rows:return []
    # Compare instants, not lexicographic mixed-offset date strings. A late
    # insertion anywhere inside the used range is a material revision too.
    stamps=pd.DatetimeIndex(pd.to_datetime([r[0] for r in rows]))
    if stamps.tz is None:stamps=stamps.tz_localize(ident['timezone'],ambiguous='raise',nonexistent='raise')
    inside=stamps.tz_convert('UTC')<pd.Timestamp(spec['through'])
    if spec.get('from'):inside &= stamps.tz_convert(ident['timezone'])>=pd.Timestamp(spec['from']).tz_localize(ident['timezone'])
    return [tuple(r) for r,keep in zip(rows,inside) if keep]


def capture_closed_inputs(db, specs, token):
    """Existing revisions are the fast path; exact used inputs are the proof.

    Caller owns a consistent read snapshot. Later appends are outside each
    range; corrections/deletions/insertions within it are never ignored.
    """
    specs=[s for s in specs if s]
    specs=list({_digest(s):s for s in [*specs,*(v for s in specs for v in s.get('recovered_inputs',[]))]}.values())
    wanted={s['isin'] for s in specs}
    quality_keys={f"{s['isin']}:{s['identity']['symbol']}:{s['base_tf']}" for s in specs}
    tracked={};settings={};strict={};settings_start={}
    starts={f"{s['isin']}:{s['identity']['symbol']}:{s['base_tf']}":s.get('from') for s in specs}
    for scope,revision in token.items():
        if scope.startswith('market:'):continue
        if scope.startswith('instrument:') and scope[len('instrument:'):] not in wanted:continue
        if scope.startswith('setting:'):
            key=scope[len('setting:'):]
            if key.startswith(('data_quality:','history_coverage:')) and key.split(':',1)[1] not in quality_keys:continue
            if key.startswith(('history_identity:','interval_evidence:')) and key.split(':',1)[1] not in wanted:continue
            start=starts.get(key.split(':',1)[1]) if key.startswith('data_quality:') else None
            if start:settings_start[scope]=start
            settings[scope]=_digest(_setting_value(db,key,start))
        else:strict[scope]=revision
        tracked[scope]=revision
    series=[]
    for spec in specs:
        scope='market:'+spec['isin']+':'+spec['base_tf']
        revision=(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]
        tracked[scope]=revision
        rows=_closed_rows(db,spec)
        series.append(dict(spec=spec,scope=scope,count=len(rows),digest=_digest(rows)))
    return dict(version=1,revisions=tracked,strict=strict,settings=settings,settings_start=settings_start,series=series)


def validate_closed_inputs(db, guard, memo=None):
    """Return a current revision token only if the frozen inputs still match."""
    if not guard or guard.get('version')!=1:raise InputRevisionChanged('Closed input proof missing')
    memo={} if memo is None else memo
    token=guard['revisions'];current={k:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0] for k in token}
    changed={k for k,v in token.items() if current[k]!=v}
    for k in changed & guard['strict'].keys():raise InputRevisionChanged('Closed input changed: '+k)
    for k in changed & guard['settings'].keys():
        start=guard.get('settings_start',{}).get(k)
        cache=('setting',k,current[k],start)
        if cache not in memo:memo[cache]=_digest(_setting_value(db,k[len('setting:'):],start))
        if memo[cache]!=guard['settings'][k]:raise InputRevisionChanged('Closed input changed: '+k)
    for item in guard['series']:
        if item['scope'] not in changed:continue
        cache=('series',_digest(item['spec']),current[item['scope']])
        if cache not in memo:memo[cache]=_digest(_closed_rows(db,item['spec']))
        if memo[cache]!=item['digest']:raise InputRevisionChanged('Closed candle changed: '+str(item['spec']))
    return current


@contextmanager
def frozen_store(store):
    """Short SQLite online backup, then compute without holding the live DB.

    The temporary read-only copy uses the SAME Store and ReadCache, not another
    history or calculation pipeline. Only the owned temporary directory closes.
    """
    import tempfile
    from pathlib import Path
    from contextlib import closing
    with tempfile.TemporaryDirectory(prefix='sm-closed-input-') as folder:
        path=Path(folder)/'snapshot.sqlite3'
        with closing(sqlite3.connect(store.path.resolve().as_uri()+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(path)) as dest:
            source.backup(dest,pages=2048,sleep=.01)
        captured=copy.copy(store);captured.path=path;captured.read_cache=ReadCache(path)
        try:yield captured
        finally:captured.read_cache.close()

class ReadCache:
    def __init__(self,path,limit=64):
        self.db=sqlite3.connect(path,check_same_thread=False,timeout=5)
        self.lock=threading.RLock();self.items=OrderedDict();self.limit=limit
        self.local=threading.local()
        self.analytics_items=OrderedDict();self.analytics_limit=128

    @contextmanager
    def track_inputs(self,isolated=False):
        previous=getattr(self.local,'stack',[])
        if isolated:self.local.stack=[]
        if not hasattr(self.local,'stack'):self.local.stack=[]
        token={};self.local.stack.append(token)
        try:yield token
        finally:
            self.local.stack.pop()
            if isolated:self.local.stack=previous

    def depend(self,scope):
        with self.lock:
            row=self.db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone()
        revision=row[0] if row else 0
        self._record({scope:revision})

    def token_for(self,scopes):
        scopes=tuple(scopes)
        if not scopes:return {}
        with self.lock:
            rows=dict(self.db.execute('SELECT scope,revision FROM input_revisions WHERE scope IN ('+','.join('?' for _ in scopes)+')',scopes))
        return {k:rows.get(k,0) for k in scopes}

    def _record(self,token):
        for active in getattr(self.local,'stack',[]):
            for scope,revision in token.items():
                if scope in active and active[scope]!=revision:
                    raise InputRevisionChanged(f'Stale Trading Engine input dependency: {scope} {active[scope]}->{revision}')
                active[scope]=revision

    def dependencies(self,local=False):
        result={}
        for token in (getattr(self.local,'stack',[])[-1:] if local else getattr(self.local,'stack',[])):result.update(token)
        return result

    def current(self,token):
        # A single SQLite statement checks one coherent revision snapshot.
        if not token:return True
        with self.lock:
            rows=dict(self.db.execute('SELECT scope,revision FROM input_revisions WHERE scope IN ('+','.join('?' for _ in token)+')',tuple(token)))
        return all(rows.get(k,0)==v for k,v in token.items())

    def assert_current(self,token):
        revisions=self.token_for(token)
        changed=[f'{key} {value}->{revisions[key]}' for key,value in token.items() if revisions[key]!=value]
        if changed:raise InputRevisionChanged('Stale Trading Engine input dependency: '+'; '.join(changed[:12]))

    def version(self):
        with self.lock:return self.db.execute('PRAGMA data_version').fetchone()[0]

    def get(self,key,loader,*,scoped=False,inputs=None,category=None,content_addressed=False):
        if category not in (None,'analytics'):raise ValueError('Unknown cache category')
        if content_addressed and (scoped or inputs):raise ValueError('Pure calculation cannot use database dependencies')
        items=self.analytics_items if category=='analytics' else self.items
        limit=self.analytics_limit if category=='analytics' else self.limit
        with self.lock:
            version=self.version()
            cached=items.get(key)
            if cached is not None and ((content_addressed and not cached[2]) or (self.current(cached[2]) if scoped and cached[2] else cached[0]==version)):
                self._record(cached[2]);items.move_to_end(key)
            else:cached=None
        # Cached objects are private immutable copies. Copying large frames and
        # DQ metadata must not hold the lock needed by Tk's revision reads.
        if cached is not None:return copy.deepcopy(cached[1])
        with self.track_inputs() as dependencies:
            if inputs:self._record(inputs)
            result=loader()
        # Only exact, fully serialized pure-function inputs may outlive an
        # unrelated database commit. Any actual DB dependency rejects this path.
        if content_addressed and dependencies:raise ValueError('Pure calculation read database dependencies')
        saved=copy.deepcopy(result)
        with self.lock:
            # Never cache a read that overlaps a commit.
            if content_addressed or (self.current(dependencies) if scoped and dependencies else version==self.version()):
                items[key]=(version,saved,dict(dependencies));items.move_to_end(key)
                while len(items)>limit:items.popitem(last=False)
        return result

    def close(self):
        with self.lock:self.items.clear();self.analytics_items.clear();self.db.close()

class CachedStoreMixin:
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.read_cache=ReadCache(self.path)

    def setting(self,key,default=None):
        # Fetch heartbeat is displayed as telemetry, never a signal input.
        if hasattr(self,'read_cache') and not key.startswith('gettex_last_success:'):
            self.read_cache.depend('setting:'+key)
        return super().setting(key,default)

    def load_official(self,*args,**kwargs):
        self.read_cache.depend('market:'+args[0]+':'+(args[1] if len(args)>1 else kwargs.get('base','15m')))
        call=super().load_official
        return self.read_cache.get(('official',args,tuple(sorted(kwargs.items()))),lambda:call(*args,**kwargs),scoped=True,inputs=self.read_cache.token_for(['market:'+args[0]+':'+(args[1] if len(args)>1 else kwargs.get('base','15m'))]))

    def load_series(self,*args,**kwargs):
        self.read_cache.depend('market:'+args[0]+':'+args[1])
        call=super().load_series
        return self.read_cache.get(('series',args,tuple(sorted(kwargs.items()))),lambda:call(*args,**kwargs),scoped=True,inputs=self.read_cache.token_for(['market:'+args[0]+':'+(args[1] if len(args)>1 else kwargs.get('base','15m'))]))
