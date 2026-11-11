"""Read-only display order from existing receipts. Never grants permission."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def rank(info, record, current, candidates, demo=None):
    if info.get('kind') == 'benchmark':
        return dict(tier=7, label='Индекс / ориентир', analysis=0, chart=0)
    analysis = {c['timeframe'] for c in candidates if c.get('analysis_ready')}
    charts = {tf for tf, r in record.get('timeframes', {}).items() if r.get('chart_ready') and current == 'CURRENT'} | analysis
    if demo is not None:
        label = 'Деморежим 1H: ' + ('пауза' if not demo['enabled'] else demo.get('signal', 'ожидание'))
        if demo.get('core') != 'READY': label += ' · ожидается проверка CORE'
        if demo.get('quote_blocker'): label += ' · цена не готова'
        if demo.get('expired'): label += ' · оценка устарела'
        # Configured working route is a separate group, NOT a current trade grant.
        return dict(tier=0, label=label, analysis=len(analysis), chart=len(charts))
    if any(c.get('trade_ready') for c in candidates): tier, label = 1, 'Есть допуск CP к демоторговле'
    elif analysis: tier, label = 2, 'Анализ доступен: ' + ', '.join(sorted(analysis))
    elif charts: tier, label = 3, 'График доступен; анализ пока заблокирован'
    elif current in ('STALE', 'EXPIRED'): tier, label = 4, 'Ожидает актуализации оценки'
    elif record:
        tier, label = 5, 'Анализ заблокирован'
        codes=[code for r in record.get('timeframes',{}).values() for code in r.get('blockers',[])]
        if codes:
            from candlepilot_health import eligibility_reason
            label += ': ' + eligibility_reason(codes[0])
    else: tier, label = 6, 'Ещё не проверено'
    return dict(tier=tier, label=label, analysis=len(analysis), chart=len(charts))


def ordered(infos, rows):
    def key(info):
        r=rows.get(info['isin'],{});tier=r.get('tier',7 if info.get('kind')=='benchmark' else 6)
        return (tier, -r.get('analysis',0) if tier else 0, -r.get('chart',0) if tier else 0,
            info['name'].casefold(),info['isin'])
    return sorted(infos,key=key)


def collect(path, infos, now=None):
    """Project compact metadata, not the ~70MB full diagnostic payload/archives."""
    from calculation_window import eligibility_state, candidate_state, execution_eligibility
    from paper_hourly import CONFIG, STATUS, EPOCH, read, VERSION
    from paper_account import ACCOUNT
    now = now or datetime.now(timezone.utc); started = time.perf_counter()
    headers = ('isin', 'checked_at', 'expires_at', 'error', 'execution_version', 'analysis_scope',
        'calendar_revision', 'reference_binding_version', '_input_revisions')
    fields = ('risk_context', 'timeframe', 'blockers', 'context', 'input_binding_version', 'expires_at',
        'analysis_ready', 'signal_ready', 'trade_ready', 'identity', 'execution_identity')
    # Only positive candidates need their existing closed-input validation proof.
    paths = ','.join("'$."+k+"'" for k in headers)
    cpaths = ','.join("'$."+k+"'" for k in fields)
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=.2) as db:
        db.execute('BEGIN'); db.execute('PRAGMA query_only=ON')
        records = {k.split(':',1)[1]: dict(zip(headers, json.loads(v))) for k,v in db.execute(
            "SELECT key,json_extract(value,"+paths+") FROM settings WHERE key LIKE 'cp_eligibility:%'")}
        for k, tf, raw in db.execute("SELECT s.key,t.key,json_extract(t.value,'$.chart_ready','$.identity','$.accepted_risk','$.full_reference.analysis_ready','$.blockers') "
                "FROM settings s,json_each(s.value,'$.timeframes') t WHERE s.key LIKE 'cp_eligibility:%'"):
            chart,identity,risk,full,blockers = json.loads(raw)
            records[k.split(':',1)[1]].setdefault('timeframes',{})[tf] = dict(chart_ready=chart,identity=identity or {},
                accepted_risk=risk,full_reference=dict(analysis_ready=full),blockers=blockers or [])
        for k, raw, guard in db.execute("SELECT s.key,json_extract(c.value,"+cpaths+"),CASE WHEN json_extract(c.value,'$.analysis_ready')=1 "
                "THEN json_extract(c.value,'$.closed_inputs') ELSE NULL END "
                "FROM settings s,json_each(s.value,'$.candidates') c WHERE s.key LIKE 'cp_eligibility:%'"):
            c=dict(zip(fields,json.loads(raw))); c['closed_inputs']=json.loads(guard) if guard else None
            c['blockers']=c.get('blockers') or []
            records[k.split(':',1)[1]].setdefault('candidates',[]).append(c)
        cfg=read(db,CONFIG,{})
        configured=(cfg.get('approved') is True and cfg.get('version') == VERSION and cfg.get('account') == ACCOUNT
            and cfg.get('epoch') == read(db,EPOCH))
        rows={};memo={};scopes={'setting:'+CONFIG,'setting:'+EPOCH}
        for info in infos:
            isin=info['isin'];record=records.get(isin,{})
            scopes.update(('instrument:'+isin,'setting:cp_eligibility:'+isin,'setting:'+STATUS+isin))
            scopes.update(record.get('_input_revisions') or {})
            current=eligibility_state(db,record,now)
            if record and record.get('isin')!=isin:current='STALE'
            candidates=[]
            for c in record.get('candidates',[]):
                if not c.get('analysis_ready') or record.get('isin')!=isin:continue
                scopes.update((c.get('closed_inputs') or {}).get('revisions',{}))
                if candidate_state(db,record,c,now,memo) != 'CURRENT':continue
                c=dict(c)
                if c.get('trade_ready'):
                    try: execution_eligibility(db,info,c['timeframe'],c.get('execution_identity'),now)
                    except (ValueError,KeyError,TypeError): c['trade_ready']=False
                candidates.append(c)
            exact=cfg.get('identities',{}).get(isin,{})
            demo=None
            if configured and exact.get('source') == 'gettex_chart' and exact.get('venue') == 'GETTEX' and exact.get('currency') == 'EUR':
                demo=read(db,STATUS+isin,{})
                demo={**demo,'enabled':cfg.get('enabled',False),'expired':not 0<=now.timestamp()-(demo.get('at') or 0)<=120}
            rows[isin]=rank(info,record,current,candidates,demo)
            rows[isin]['checked_at']=now.isoformat()
        token={s:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or (0,))[0] for s in scopes}
    return dict(rows=rows,token=token,checked_at=now.timestamp(),read_seconds=time.perf_counter()-started)


class Runtime:
    def __init__(self,app):
        self.app=app;self.rows={};self.busy=False;self.last=0;self.next_try=0;self.callbacks=[]

    def start(self):self.app.root.after(2000,self.tick)

    def ordered(self):return ordered(self.app.watchlist,self.rows)

    def apply(self):
        box=getattr(self.app,'symbol_box',None)
        if box is None:return
        # Do not move dropdown items under an ongoing mouse/keyboard selection.
        popup=box.tk.call('ttk::combobox::PopdownWindow',str(box))
        if box.tk.call('winfo','ismapped',popup):return
        names=[i['name'] for i in self.ordered()]
        if tuple(box['values']) != tuple(names):box['values']=names

    def opened(self):
        self.apply();self.refresh()

    def tick(self):
        if self.app.closing:return
        box=getattr(self.app,'symbol_box',None)
        if box is not None:box.configure(postcommand=self.opened)
        self.apply();self.refresh();self.app.root.after(30000,self.tick)

    def refresh(self,callback=None):
        if callback:self.callbacks.append(callback)
        if self.busy or self.app.closing:return
        if time.time()<self.next_try:return
        if time.time()-self.last<60:
            for fn in self.callbacks:fn()
            self.callbacks=[];return
        app=self.app
        infos=[{k:i.get(k) for k in ('isin','name','kind')} for i in app.watchlist]
        binding=[(i['isin'],i['name'],i.get('kind')) for i in app.watchlist]
        self.busy=True
        def done(value):
            self.busy=False
            if app.closing:return
            if (binding!=[(i['isin'],i['name'],i.get('kind')) for i in app.watchlist]
                    or app.store.read_cache.token_for(value['token'])!=value['token']):
                self.next_try=time.time()+5;return
            self.rows=value['rows'];self.last=time.time();self.apply()
            callbacks,self.callbacks=self.callbacks,[]
            for fn in callbacks:fn()
        def failed(_):
            self.busy=False;self.next_try=time.time()+60
        if app.job(lambda:collect(app.store.path,infos),done,failed,lane='misc',key='watchlist-order') is False:
            self.busy=False
