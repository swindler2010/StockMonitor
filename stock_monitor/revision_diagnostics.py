"""Bounded evidence only. Never writes market data or grants trading readiness."""
import json, sqlite3, time, uuid, math
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_EVENTS=100
INDICATORS=('ema10','ema30','rsi','atr','macd','macd_signal','volume_mean20')

def probes(path):
    """Recent probe coverage is bounded, never expose raw provider exceptions."""
    if not Path(path).exists():return []
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
        db.execute('PRAGMA query_only=ON')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone():return []
        rows=db.execute("SELECT key,value FROM settings WHERE key>=? AND key<? ORDER BY key LIMIT 200",('cp_revision:','cp_revision;')).fetchall()
    result=[]
    import re
    for key,value in rows:
        data=json.loads(value);reason=data.get('reason','')
        match=re.search(r'REVISION_[A-Z_]+',reason)
        result.append({'probe_id':key,'state':data.get('state'),'base':data.get('base'),
            'identity':data.get('identity'),'checked_at':data.get('checked_at'),'retry_after':data.get('retry_after'),
            'reason_code':match.group() if match else 'CALENDAR_UNCONFIRMED' if reason.startswith('CALENDAR:') else 'PROBE_FAILED' if reason else None})
    return sorted(result,key=lambda x:x.get('checked_at') or '',reverse=True)[:10]

def history(path, *, before_id=None, limit=20, journal_id=None):
    if not Path(path).exists():return []
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
        db.execute('PRAGMA query_only=ON');db.row_factory=sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='candle_revision_history'").fetchone():return []
        where=' WHERE id=?' if journal_id is not None else ' WHERE id<?' if before_id is not None else ''
        args=[journal_id] if journal_id is not None else [before_id] if before_id is not None else []
        rows=db.execute('SELECT * FROM candle_revision_history'+where+' ORDER BY id DESC LIMIT ?',args+[min(MAX_EVENTS,limit)]).fetchall()
    events=[]
    for row in rows:
        event=dict(row)
        for key in ('identity','old_ohlcv','new_ohlcv'):event[key]=json.loads(event[key])
        # Content identity survives process restarts and distinguishes reused row IDs.
        event['event_id']=str(uuid.uuid5(uuid.NAMESPACE_URL,'stock-monitor/revision/v1/'+json.dumps(event,sort_keys=True,separators=(',',':'))))
        events.append(event)
    return events

def scope(event):return 'market:'+event['isin']+':'+event['base_tf']

def affected(event):
    from timeframes import LABELS,base_for
    return [tf for tf in LABELS if base_for(tf,event['identity']['source'])==event['base_tf']]

def affected_signals(event):
    from timeframes import LABELS,base_for
    from trading_engine import profile
    return [tf for tf in LABELS if any(base_for(dep,event['identity']['source'])==event['base_tf'] for dep in profile(tf))]

def finite(value):
    return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None

def observe_calculation(path,state,started_at):
    """Called in the existing snapshot lane, after guarded worker calculation."""
    completed=time.time();token=state.get('_input_revisions',{});traces={}
    for event in history(path,limit=MAX_EVENTS):
        revision=token.get(scope(event))
        if revision is None or revision<event['revision_after']:continue
        results=[]
        for stock in state.get('stocks',[]):
            if stock.get('isin')!=event['isin']:continue
            if any(stock.get(k)!=v for k,v in event['identity'].items()):continue
            tf=stock.get('timeframe')
            if tf not in set(affected(event)+affected_signals(event)):continue
            last=(stock.get('candles') or [{}])[-1]
            dq=stock.get('data_quality') or {}
            engine=stock.get('trading_engine') or {}
            results.append({'timeframe':tf,'bar_at':last.get('at'),
                'direct_indicator_dependency':tf in affected(event),
                'indicators':{k:finite(last.get(k)) for k in INDICATORS},
                'missing_indicators':[k for k in INDICATORS if finite(last.get(k)) is None],
                'dq':{'state':dq.get('state','UNKNOWN'),'issues':[str(x)[:160] for x in dq.get('issues',[])[:6]]},
                'signal':{k:engine.get(k) for k in ('status','signal','score','confidence')},
                'signal_blockers':[str(x)[:160] for x in engine.get('blockers',[])[:6]],
                'signal_evidence': 'OBSERVED' if engine else 'NOT_OBSERVED'})
        if results:
            traces[event['event_id']]={'journal_id':event['id'],'scope':scope(event),'input_revision':revision,
                'started_at':started_at,'completed_at':completed,'results':results,
                'basis':'existing worker result; may reuse valid cache; not proof of execution of each formula'}
    state['_revision_trace']=traces
    return traces

def safe_observe(path,state,started_at):
    try:observe_calculation(path,state,started_at)
    except Exception as error:
        state['_revision_trace_error']='DIAGNOSTIC_STORE_BUSY' if isinstance(error,sqlite3.OperationalError) else 'DIAGNOSTIC_UNAVAILABLE'

def _saved_records(path,isins):
    if not isins:return {}
    from cp_assessment import PREFIX
    keys=[prefix+i for i in isins for prefix in ('cp_eligibility:',PREFIX)]
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
        db.execute('PRAGMA query_only=ON')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone():return {}
        return {k:json.loads(v) for k,v in db.execute(
            'SELECT key,value FROM settings WHERE key IN ('+','.join('?' for _ in keys)+')',keys)}


def _saved_trace(event,record):
    """Match the committed analysis's exact series and used closed range."""
    if record.get('publication_state') not in ('CURRENT','EXPIRED'):return None
    key=scope(event)
    zone=ZoneInfo(event['identity']['timezone'])
    observed=datetime.fromisoformat(event['ts']).replace(tzinfo=zone)
    results=[]
    proofs={}
    for candidate in record.get('candidates',[]):
        guard=candidate.get('closed_inputs') or {}
        proofs.setdefault(candidate.get('timeframe'),[]).append((guard,candidate.get('result')))
    for tf,row in record.get('timeframes',{}).items():
        if row.get('_closed_input'):
            spec=row['_closed_input']
            proofs.setdefault(tf,[]).append(({'revisions':row.get('_input_revisions',{}),'series':[
                {'scope':'market:'+spec['isin']+':'+spec['base_tf'],'count':row.get('count'),'spec':spec}]},None))
    for tf,items in proofs.items():
        for guard,engine in items:
            revision=guard.get('revisions',{}).get(key,0)
            if revision<event['revision_after']:continue
            entry=next((item for item in guard.get('series',[]) if item.get('scope')==key and item.get('count')
                        and item['spec'].get('identity')==event['identity']
                        and observed<datetime.fromisoformat(item['spec']['through'])
                        and (not item['spec'].get('from') or
                             observed>=datetime.fromisoformat(item['spec']['from']).replace(tzinfo=zone))),None)
            if entry is None:continue
            row=record.get('timeframes',{}).get(tf,{})
            dq=(row.get('reference_quality') or row.get('window_quality') or {}) if row.get('identity')==event['identity'] else {}
            results.append({'timeframe':tf,'bar_at':row.get('last'),'input_revision':revision,
                'direct_indicator_dependency':tf in affected(event),
                'dq':{'state':dq.get('state','UNKNOWN'),'issues':[str(s)[:160] for s in dq.get('issues',[])[:6]]},
                'signal':{k:engine.get(k) for k in ('status','signal','score','confidence')} if engine else None,
                'signal_evidence':'OBSERVED' if engine else 'NOT_OBSERVED'})
            break
    if not results:return None
    return {'journal_id':event['id'],'scope':key,
        'started_at':datetime.fromisoformat(record['checked_at']).timestamp(),
        'completed_at':record.get('published_at'),
        'published_at':record.get('published_at'),'results':results,
        'basis':'Durable eligibility; exact series/range and captured revision at or after event',
        'publication_state':record['publication_state']}


def read(path,args,observations,rejections,pipeline,instance_id):
    events=history(path,before_id=args.get('before_id'),journal_id=args.get('journal_id'),limit=args.get('limit',10)+1)
    limit=args.get('limit',10);more=len(events)>limit;events=events[:limit]
    detail=args.get('detail',False)
    from cp_assessment import PREFIX
    saved=_saved_records(path,{event['isin'] for event in events})
    for event in events:
        trace=observations.get(event['event_id']);key=scope(event)
        record=saved.get('cp_eligibility:'+event['isin'],{})
        stored=None if trace else _saved_trace(event,record)
        pending=saved.get(PREFIX+event['isin'],{})
        queued=pending.get('market_revisions',{}).get(key,0)>=event['revision_after']
        if stored:trace=stored
        rejected=next((r for r in reversed(rejections) if r['reason']=='INPUT_REVISION_CHANGED' and
                       key in r['revisions'] and r['revisions'][key]<event['revision_after'] and
                       r['at']>=datetime.fromisoformat(event['detected_at']).timestamp()),None)
        event['affected_timeframes']=[{'timeframe':tf,'status':'RESULT_OBSERVED' if trace and any(r['timeframe']==tf for r in trace['results']) else 'NOT_OBSERVED'} for tf in affected(event)]
        event['affected_signals']=[{'timeframe':tf,'status':'RESULT_OBSERVED' if trace and any(r['timeframe']==tf and r['signal_evidence']=='OBSERVED' for r in trace['results']) else 'NOT_OBSERVED'} for tf in affected_signals(event)]
        event['signal_dependencies']='Same-instrument declared CORE profiles; cross-instrument benchmark dependencies not proven by this event alone'
        event['affected_indicators']=list(INDICATORS)
        event['indicator_scope']='Fixed CORE; configurable display indicators are not certified by snapshot evidence'
        event['stages']={'detected':{'status':'CONFIRMED','at':event['detected_at']},
            'invalidation':{'status':'REVISION_ADVANCED' if event['revision_after']>event['revision_before'] else 'UNCONFIRMED'},
            'stale_publication':{'status':'REJECTED' if rejected else 'NOT_OBSERVED','at':rejected['at'] if rejected else None},
            'calculation':{'status':'RESULT_OBSERVED' if trace else 'NOT_OBSERVED','started_at':trace.get('started_at') if trace else None,'completed_at':trace.get('completed_at') if trace else None},
            'dq':{'status':'CHECKED' if trace and all(r['dq']['state']!='UNKNOWN' for r in trace['results']) else 'NOT_OBSERVED'},
            'publication':{'status':'PUBLISHED_ELIGIBILITY' if stored else 'PUBLISHED' if trace else 'NOT_OBSERVED',
                           'at':trace.get('published_at') if trace else None}}
        event['status']='PUBLISHED_PARTIAL_COVERAGE' if trace else 'RETRY_PENDING' if queued and pending.get('retry_at') else 'QUEUED' if queued else 'AWAITING_OBSERVATION'
        event['reason']=None if trace else 'Assessment pending' if queued else 'No matching publication retained; absence is not proof of failure'
        if queued:
            event['assessment_queue']={k:pending.get(k) for k in ('requested_at','last_attempt_at','retry_at','retry_reason')}
            if pending.get('error'):event['assessment_queue']['error_code']=str(pending['error']).split(':',1)[0][:80]
        if detail:event['observation']=trace
        else:
            event.pop('old_ohlcv');event.pop('new_ohlcv')
    return {'schema_version':1,'instance_id':instance_id,'read_only':True,'events':events,
        'watcher_probes':probes(path) if args.get('include_probes') else None,
        'probe_limit':'10 latest from at most 200 saved probes; raw exception text excluded',
        'next_before_id':events[-1]['id'] if more else None,'observation_retention':'Last 100 snapshot observations and 32 rejections in this process; latest eligibility and history persist in SQLite',
        'pipeline':{k:pipeline.get(k) for k in ('status','cause','updated_at','revision_diagnostics_error')},
        'execution_permission':False,'note':'Historical evidence never grants current DQ, signal or trade readiness'}
