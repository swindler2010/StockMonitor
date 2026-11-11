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
    for part in path.split('/')[1:]:
        key=part.replace('~1','/').replace('~0','~')
        try:node=node[int(key)] if isinstance(node,list) else node[key]
        except (KeyError,IndexError,ValueError,TypeError):raise ValueError('PATH_NOT_FOUND')
    def pointer(key):return path+'/'+str(key).replace('~','~0').replace('/','~1')
    offset=args.get('offset',0);limit=args.get('limit',20)
    if type(offset)!=int or offset<0 or type(limit)!=int or not 1<=limit<=50:raise ValueError('INVALID_PAGE')
    if isinstance(node,str) and len(node.encode())>1200:
        end=min(len(node),offset+limit*100)
        return dict(status='AVAILABLE',revision=revision,path=path,type='string',offset=offset,total_count=len(node),
                    text=node[offset:end],next_offset=end if end<len(node) else None,complete=end>=len(node),paging_unit='characters')
    items=list(enumerate(node)) if isinstance(node,list) else sorted(node.items()) if isinstance(node,dict) else [(None,node)]
    result=[];budget=0
    for key,item in items[offset:offset+limit]:
        size=len(json.dumps(item,ensure_ascii=False,allow_nan=False).encode())
        target=pointer(key) if key is not None else path
        if size>1200:
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
                paging='Use returned path to expand; expected_revision is required for continuation. No silent omission.')

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
            qualities={k:json.loads(v) for k,v in db.execute("SELECT key,value FROM settings WHERE key LIKE ?",('data_quality:'+isin+':%',))}
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
    summary={k:v for k,v in cp.items() if not isinstance(v,(dict,list)) or k in ('eligibility_summary','activity')}
    return {**identity,'paper_account':paper,'candlepilot_health':summary,
            'detail_delivery':{'status':'PAGED','tool':'stock_monitor_snapshot','section':'candlepilot_health','reason':'Large diagnostic evidence is read explicitly by path'}}

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
