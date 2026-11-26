"""Bounded, loss-explicit projections; no market calculations or writes."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def page(value,args,revision):
    path=args.get('path','')
    if path and not path.startswith('/'):raise ValueError('INVALID_JSON_POINTER')
    if args.get('expected_revision') and args['expected_revision']!=revision:raise ValueError('STALE_PAGE')
    if args.get('offset',0) and args.get('expected_revision')!=revision:raise ValueError('REVISION_REQUIRED')
    node=value
    from mcp_shared_values import ReferenceTree
    for part in ([] if isinstance(value,ReferenceTree) else path.split('/')[1:]):
        key=part.replace('~1','/').replace('~0','~')
        try:node=node[int(key)] if isinstance(node,list) else node[key]
        except (KeyError,IndexError,ValueError,TypeError):raise ValueError('PATH_NOT_FOUND')
    if isinstance(value,ReferenceTree):
        try:node=value.resolve(path)
        except (KeyError,IndexError,ValueError,TypeError):raise ValueError('PATH_NOT_FOUND')
    def pointer(key):return path+'/'+str(key).replace('~','~0').replace('/','~1')
    offset=args.get('offset',0);limit=args.get('limit',20)
    if type(offset)!=int or offset<0 or type(limit)!=int or not 1<=limit<=50:raise ValueError('INVALID_PAGE')
    if isinstance(node,str) and len(node.encode())>1200:
        end=min(len(node),offset+limit*100)
        return dict(status='AVAILABLE',revision=revision,path=path,type='string',offset=offset,total_count=len(node),
                    text=node[offset:end],next_offset=end if end<len(node) else None,complete=end>=len(node),paging_unit='characters')
    items=list(enumerate(node)) if isinstance(node,list) else sorted(node.items()) if isinstance(node,dict) else [(None,node)]
    # By-stock rows contain all 11 TF: small pages may inline a complete row
    # instead of forcing hundreds of expansion calls within a fixed lease.
    inline_limit=max(1200,min(20000,50000//limit)) if path=='/by_stock' else 1200
    result=[];budget=0
    for key,item in items[offset:offset+limit]:
        target=pointer(key) if key is not None else path
        if isinstance(value,ReferenceTree):item=value.resolve(target)
        size=len(json.dumps(item,ensure_ascii=False,allow_nan=False).encode())
        if size>inline_limit:
            projected={'expanded':False,'path':target,'type':type(item).__name__,'bytes':size,'reason':'EXPAND_PATH'}
        else:projected={'expanded':True,'value':item}
        row={'key':key,'path':target,**projected}
        budget+=len(json.dumps(row,ensure_ascii=False).encode())
        if budget>60000:break
        result.append(row)
    nxt=offset+len(result)
    return dict(status='AVAILABLE',revision=revision,path=path,total_count=len(items),offset=offset,
                next_offset=nxt if nxt<len(items) else None,items=result,
                complete=nxt>=len(items),nested_values_require_expansion=any(not r['expanded'] for r in result),
                paging='Use returned path to expand; expected_revision is required for continuation. No silent omission.'+(' Nested _read_reference pointers name shared values in this same pinned snapshot; original paths resolve automatically.' if isinstance(value,ReferenceTree) else ''))

def instruments(path,args,return_rows=False):
    """One coherent read transaction, independent of UI and analysis admission."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        infos=[(isin,json.loads(payload)) for isin,payload in db.execute('SELECT isin,payload FROM instruments ORDER BY isin')]
        rows=[]
        for isin,info in infos:
            listings=[dict(zip(('source','venue','currency','symbol','timeframe','count','first','last'),r)) for r in db.execute('SELECT source,venue,currency,symbol,base_tf,count(*),min(ts),max(ts) FROM series WHERE isin=? GROUP BY source,venue,currency,symbol,base_tf ORDER BY source,venue,currency,symbol,base_tf',(isin,))]
            for listing in listings:listing['storage_table']='series'
            for tf,count,first,last in db.execute("SELECT base_tf,count(*),min(ts),max(ts) FROM candles WHERE isin=? AND source='gettex' GROUP BY base_tf ORDER BY base_tf",(isin,)):
                listings.append(dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timeframe=tf,count=count,first=first,last=last,storage_table='candles',identity_basis='Existing Store.load_official contract'))
            saved={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM settings WHERE key IN (?,?)',('history_identity:'+isin,'gettex_last_success:'+isin))}
            # LIKE is case-insensitive in SQLite and scans every settings value
            # for every stock (including multi-megabyte evidence). Use the PK
            # range for this literal namespace; instrument IDs are not patterns.
            prefix='data_quality:'+isin+':'
            qualities={k:json.loads(v) for k,v in db.execute(
                'SELECT key,value FROM settings WHERE key>=? AND key<?',
                (prefix,prefix+'\uffff'))}
            quality_summary={k:{f:v.get(f) for f in ('state','issues','warnings','checked_at','identity','base_tf','missing_count')} for k,v in qualities.items() if isinstance(v,dict)}
            rows.append(dict(instrument_id=isin,isin=isin,name=info.get('name'),kind=info.get('kind','stock'),
                symbol=info.get('symbol'),currency=info.get('currency'),venue=info.get('venue'),
                stored_instrument=info,stored_series=listings,
                listing_confirmation='Stored presence is not confirmation; inspect saved DQ/provenance before analysis',
                saved_identity=saved.get('history_identity:'+isin),saved_quality=quality_summary,
                quality='SAVED_REPORTS_ONLY; not independently reassessed',last_successful_gettex_load=saved.get('gettex_last_success:'+isin),
                unavailable_fields={'other_source_last_success':'Not established by raw stored series timestamps'}))
        if return_rows:return rows
        revision=digest(rows)
        result=page(rows,args,revision)
        result.update(instrument_total=len(rows),stock_total=sum(r['kind']!='benchmark' for r in rows),benchmark_total=sum(r['kind']=='benchmark' for r in rows),analysis_ready_required=False)
        return result

def compact_identity(identity):
    cp=identity.get('candlepilot_health',{})
    paper=identity.get('paper_account',{})
    paper={k:(dict(status='PAGED',total_count=len(v),section='runtime',path='/paper_account/'+k) if isinstance(v,list) else v) for k,v in paper.items()}
    real=identity.get('real_account')
    real_field={} if real is None else {'real_account':{k:(dict(status='PAGED',total_count=len(v),section='runtime',path='/real_account/'+k) if isinstance(v,list) else v) for k,v in real.items()}}
    summary={k:v for k,v in cp.items() if not isinstance(v,(dict,list)) or k in ('eligibility_summary','activity')}
    return {**identity,**real_field,'paper_account':paper,'candlepilot_health':summary,
            'detail_delivery':{'status':'PAGED','tool':'stock_monitor_snapshot','section':'candlepilot_health','reason':'Large diagnostic evidence is read explicitly by path'}}


def read_summary(path,state=None,previous=None):
    """Compact stored totals, never a raw-DQ-derived error/trade count.

    All persisted values are from one readonly transaction. The independently
    published analysis has its own timestamp; it is not relabelled as current.
    Full series identities/counts remain expandable without returning OHLCV.
    """
    from datetime import datetime,timezone
    import time
    started=time.monotonic()
    state=state or {}
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        db.set_progress_handler(lambda:int(time.monotonic()-started>55),1000)
        infos=[(isin,json.loads(payload)) for isin,payload in db.execute('SELECT isin,payload FROM instruments ORDER BY isin')]
        grouped={isin:[] for isin,_ in infos}
        fields=('isin','source','venue','currency','symbol','timeframe','count','first','last')
        for isin,_ in infos:
            for record in db.execute('SELECT isin,source,venue,currency,symbol,base_tf,count(*),min(ts),max(ts) FROM series WHERE isin=? GROUP BY isin,source,venue,currency,symbol,base_tf',(isin,)):
                row=dict(zip(fields,record));row['storage_table']='series';grouped[isin].append(row)
            for source,tf,count,first,last in db.execute('SELECT source,base_tf,count(*),min(ts),max(ts) FROM candles WHERE isin=? GROUP BY source,base_tf',(isin,)):
                # Identity is defined by the Store contract only for GETTEX.
                grouped[isin].append(dict(isin=isin,source=source,timeframe=tf,count=count,first=first,last=last,storage_table='candles',
                    venue='GETTEX' if source=='gettex' else None,currency='EUR' if source=='gettex' else None,
                    symbol=isin if source=='gettex' else None))
        rows=[];series=[];by_table={'series':0,'candles':0};assessments={}
        for isin,info in infos:
            archive=grouped[isin];series.extend(archive)
            counts={table:sum(s['count'] for s in archive if s['storage_table']==table) for table in by_table}
            for table,count in counts.items():by_table[table]+=count
            assessment=db.execute('SELECT value FROM settings WHERE key=?',('cp_eligibility:'+isin,)).fetchone()
            assessment=json.loads(assessment[0]) if assessment else {};assessments[isin]=assessment
            raw_reports=db.execute('SELECT count(*) FROM settings WHERE key>=? AND key<?',('data_quality:'+isin+':','data_quality:'+isin+':\uffff')).fetchone()[0]
            rows.append(dict(isin=isin,name=info.get('name'),kind=info.get('kind','stock'),
                stored_rows_by_table=counts,stored_series_count=len(archive),raw_dq_series=raw_reports,
                first=min((s['first'] for s in archive if s['first']),default=None),
                last=max((s['last'] for s in archive if s['last']),default=None),
                saved_assessment_at=assessment.get('checked_at'),saved_assessment_expires_at=assessment.get('expires_at'),
                unresolved_errors=None,owner_accepted_risk=None,evidence_status='EXACT_RECONCILIATION_REQUIRED'))
        revision_clock=db.execute('SELECT count(*),coalesce(sum(revision),0) FROM input_revisions').fetchone()
        from mcp_observability import enrich
        generated=datetime.now(timezone.utc).isoformat()
        enrich(db,infos,rows,state,generated,assessments=assessments)
    published=[]
    for stock in state.get('stocks',[]):
        engine=stock.get('trading_engine') or {}
        published.append(dict(isin=stock.get('isin'),timeframe=stock.get('timeframe'),
            engine_status=engine.get('status'),signal=engine.get('signal'),
            technical_blockers=engine.get('blockers',[]),entry_conditions=engine.get('entry_blockers',[]),
            basis='LAST_PUBLISHED_ANALYSIS; NOT_REVALIDATED; NO_EXECUTION_PERMISSION'))
    if state.get('selected_analysis'):
        engine=state['selected_analysis'];selection=state.get('selected',{})
        published=[p for p in published if (p['isin'],p['timeframe'])!=(selection.get('isin'),selection.get('timeframe'))]
        published.append(dict(isin=selection.get('isin'),timeframe=selection.get('timeframe'),engine_status=engine.get('status'),
            signal=engine.get('signal'),technical_blockers=engine.get('blockers',[]),entry_conditions=engine.get('entry_blockers',[]),
            completed_at=state.get('selected_analysis_completed_at'),validity=state.get('selected_analysis_status'),
            basis='INDEPENDENT_SELECTED_CHART; CURRENT_AT_CAPTURE; NO_EXECUTION_PERMISSION'))
    owner_values=[r['owner_accepted_risk'] for r in rows if r['kind']!='benchmark']
    result=dict(schema_version=2,generated_at=generated,
        totals=dict(instruments=len(rows),stocks=sum(r['kind']!='benchmark' for r in rows),
            benchmarks=sum(r['kind']=='benchmark' for r in rows),stored_rows_by_table=by_table,
            raw_dq_series=sum(r['raw_dq_series'] for r in rows),unresolved_errors=None,
            owner_accepted_risk=sum(owner_values) if owner_values and all(v is not None for v in owner_values) else None),
        by_stock=rows,stored_series=series,published_analysis=published,
        published_analysis_at=state.get('at'),input_revision_clock=list(revision_clock),
        execution_permission=False,writes=False,
        count_semantics='Exact physical PK rows by table and full source/venue/currency/symbol/TF identity; no deduplication across different feeds or TF, not economic unique candles. Raw DQ series are not errors. Unreconciled errors/risks are unknown, never zero.',
        diagnostic_pipeline=state.get('diagnostic_pipeline',{}),cp_activity=state.get('cp_activity',{}),
        observation_semantics='Persisted values share one SQLite transaction; independent selected analysis has its own publication time and was current only at capture. No execution grant.')
    result['revision']=digest(result)
    from mcp_observability import short_report
    result['short_report']=short_report(result,previous)
    result['report']=result['short_report']['text']
    return result

def archive_catalog(path):
    """Inventory all physical candle series, including removed watchlist items."""
    from mcp_routes import escape
    result=[]
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        registered={r[0] for r in db.execute('SELECT isin FROM instruments')}
        for table,keys in (('series',('isin','source','venue','currency','symbol','base_tf')),('candles',('isin','source','base_tf'))):
            cols=','.join(keys)
            for record in db.execute('SELECT '+cols+',count(*),min(ts),max(ts) FROM '+table+' GROUP BY '+cols+' ORDER BY '+cols):
                values=record[:len(keys)];identity={('timeframe' if k=='base_tf' else k):v for k,v in zip(keys,values)}
                result.append(dict(**identity,storage_table=table,count=record[-3],first=record[-2],last=record[-1],
                    registered_instrument=identity['isin'] in registered,
                    read_path='/@archive/'+table+'/'+'/'.join(escape(v) for v in values)))
    return result


def raw_archive(path,identity,args):
    """Exact physical rows, including auxiliary TF and invalid/unassessed bars.

    No calendar, closed-candle, DQ, analysis or execution predicate. The query
    uses the table's actual PK; missing legacy identity is never invented.
    """
    table=identity.get('storage_table')
    if table not in ('series','candles'):raise ValueError('INVALID_STORAGE_TABLE')
    keys=('isin','source','venue','currency','symbol','timeframe') if table=='series' else ('isin','source','timeframe')
    if set(identity)!={*keys,'storage_table'}:raise ValueError('EXACT_STORED_IDENTITY_REQUIRED')
    offset=args.get('offset',0);limit=args.get('limit',50)
    if type(offset)!=int or offset<0 or type(limit)!=int or not 1<=limit<=50:raise ValueError('INVALID_PAGE')
    where=' AND '.join(('base_tf' if k=='timeframe' else k)+'=?' for k in keys)
    params=[identity[k] for k in keys]
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        scopes=('market:'+identity['isin']+':'+identity['timeframe'],'instrument:'+identity['isin'])
        token=[(s,(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or [0])[0]) for s in scopes]
        total,first,last=db.execute('SELECT count(*),min(ts),max(ts) FROM '+table+' WHERE '+where,params).fetchone()
        revision=digest([identity,token,total,first,last])
        if args.get('expected_revision') and args['expected_revision']!=revision:raise ValueError('STALE_PAGE')
        if offset and not args.get('expected_revision'):raise ValueError('REVISION_REQUIRED')
        data=db.execute('SELECT ts,open,high,low,close,volume FROM '+table+' WHERE '+where+' ORDER BY ts LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
    from datetime import datetime,timezone
    return dict(status='AVAILABLE' if total else 'DATA_UNAVAILABLE',view='raw_stored_archive',
        identity=identity,revision=revision,total_count=total,first=first,last=last,
        offset=offset,next_offset=offset+len(data) if offset+len(data)<total else None,
        bars=[dict(zip(('at','open','high','low','close','volume'),r)) for r in data],
        as_of=datetime.now(timezone.utc).isoformat(),read_only=True,execution_permission=False,
        readiness_required=False,market_open_required=False,closed_status='UNASSESSED',
        timestamp_semantics='Exact stored local timestamp; timezone/identity evidence is available in the instrument catalog and persisted metadata; no UTC or candle-close claim.',
        metadata_route='/@persisted',catalog_route='/@catalog',
        missing_reason=None if total else 'NO_EXACT_STORED_SERIES',
        revision_semantics='Exact identity and stored-input revisions; changed relevant input rejects continuation. No readiness lease or execution grant.')


def archive(path,args):
    fields=('isin','source','venue','currency','symbol')
    if args.get('closed_only'):raise ValueError('ARCHIVE_CLOSED_STATUS_UNASSESSED')
    if any(not args.get(k) for k in fields):raise ValueError('EXACT_STORED_IDENTITY_REQUIRED')
    offset=args.get('offset',0);limit=args.get('limit',200)
    if type(offset)!=int or offset<0 or type(limit)!=int or not 1<=limit<=500:raise ValueError('INVALID_PAGE')
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        scopes=('market:'+args['isin']+':'+args['timeframe'],'instrument:'+args['isin'])
        token=[(scope,(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]) for scope in scopes]
        query={k:v for k,v in args.items() if k not in ('offset','limit','expected_revision','_deadline')}
        revision=digest([token,query])
        if args.get('expected_revision') and args['expected_revision']!=revision:raise ValueError('STALE_PAGE')
        if offset and args.get('expected_revision')!=revision:raise ValueError('REVISION_REQUIRED')
        table=args.get('storage_table','candles' if args['source']=='gettex' else 'series')
        if table not in ('candles','series'):raise ValueError('INVALID_STORAGE_TABLE')
        if table=='candles':
            if (args['source'],args['venue'],args['currency'],args['symbol'])!=('gettex','GETTEX','EUR',args['isin']):raise ValueError('IDENTITY_MISMATCH')
            where='isin=? AND source=? AND base_tf=?';params=[args['isin'],'gettex',args['timeframe']]
        else:
            where='isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=?'
            params=[args[k] for k in fields]+[args['timeframe']]
        # Stored timestamps are local exchange wall time: reject UTC range filters
        # here instead of pretending their lexical comparison is valid.
        if args.get('from') or args.get('to'):raise ValueError('ARCHIVE_RANGE_REQUIRES_CALCULATED_VIEW')
        total,first,last=db.execute('SELECT count(*),min(ts),max(ts) FROM '+table+' WHERE '+where,params).fetchone()
        rows=db.execute('SELECT ts,open,high,low,close,volume FROM '+table+' WHERE '+where+' ORDER BY ts LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
        return dict(status='AVAILABLE' if total else 'DATA_UNAVAILABLE',view='stored_archive',revision=revision,
            identity={k:args[k] for k in fields},storage_table=table,timeframe=args['timeframe'],total_count=total,first=first,last=last,
            bars=[dict(zip(('at','open','high','low','close','volume'),r)) for r in rows],offset=offset,
            next_offset=offset+len(rows) if offset+len(rows)<total else None,
            missing_reason=None if total else 'NO_EXACT_STORED_SERIES; derived TF may be available in calculated view',
            confirmation='UNASSESSED; raw persisted OHLCV is not confirmed closed data or trade permission',
            timezone='Stored local wall time; consult listing provenance',
            indicators='Use existing calculated view; raw archive does not recalculate indicators')

def receipt(value,args=None):
    args=args or {}
    if args or len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())>60000:
        projected=page(value,args,digest(value))
        projected.update(command_id=value.get('command_id'),command_status=value.get('status'),action=value.get('action'))
        return projected
    return value

def persisted(path):
    """Explicit allowlist: never expose arbitrary settings/tunnel credentials."""
    prefixes=('news:','fundamentals:','card:','history_identity:','history_coverage:','history_archive_progress:','data_quality:','gettex_last_success:','dividends:','benchmark_history:')
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        rows=db.execute('SELECT key,value FROM settings WHERE key=? OR '+' OR '.join('key LIKE ?' for _ in prefixes),('global_indicators',*[p+'%' for p in prefixes])).fetchall()
        settings={k:json.loads(v) for k,v in rows}
        positions={isin:json.loads(payload) for isin,payload in db.execute('SELECT isin,payload FROM instruments ORDER BY isin')}
        quotes={}
        for isin in positions:
            latest=db.execute('SELECT ts,price,volume FROM trades WHERE isin=? ORDER BY ts DESC LIMIT 1',(isin,)).fetchone()
            quotes[isin]=dict(status='STORED_ONLY' if latest else 'DATA_UNAVAILABLE',
                last_trade=dict(zip(('at','price','volume'),latest)) if latest else None,
                bid=None,ask=None,spread=None,source='NOT_STORED_IN_TRADE_ROW',
                quality='UNASSESSED; stored tick is not a live quote',missing_reason='Bid/ask/spread and per-tick venue/currency/provenance are not in this table; inspect card overview separately')
        from datetime import datetime,timezone
        return dict(as_of=datetime.now(timezone.utc).isoformat(),settings=settings,instruments_and_positions=positions,stored_quotes=quotes,
            availability={'news':'news:<ISIN> / news:market: saved original articles, source attempts, dates, freshness and errors; no article translation; legacy card news retained',
                          'fundamentals':'fundamentals:<ISIN> saved quarterly/yearly/TTM statements, valuation_metrics, calendar, provenance, identity and retained-data flags; card:<ISIN> legacy only; no fetch implied',
                          'quotes':'Saved card overview may contain bid/ask/price/time; no guarantee of a live or same-venue quote',
                          'missing_keys':'DATA_UNAVAILABLE; no saved value, not evidence that provider has none'},
            execution='read-only coherent SQLite transaction; no downloads, credentials or unrestricted settings')

def freshness(path,token):
    if not token:return dict(status='UNKNOWN',reason='NO_PUBLISHED_INPUT_REVISION_TOKEN')
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            changed=sum((db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]!=revision for scope,revision in token.items())
        return dict(status='STALE' if changed else 'CURRENT',changed_input_count=changed,dependency_count=len(token),reason='Current DB dependencies compared to published snapshot; publication is historical when stale')
    except sqlite3.Error:return dict(status='UNAVAILABLE',reason='REVISION_STORE_UNAVAILABLE')
