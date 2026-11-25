"""Read-only all-watchlist receipt table. Never issues market permissions."""
import json
import sqlite3
from pathlib import Path
from contextlib import closing
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from collections import Counter
from functools import lru_cache

FIELDS=('isin','source','venue','currency','symbol')

def clock(value):
    t=datetime.fromisoformat(value.replace('Z','+00:00'))
    if t.tzinfo is None:raise ValueError('TIMEZONE_REQUIRED')
    return t.astimezone(timezone.utc)

def time_fields(value,now):
    try:
        t=clock(value);seconds=(clock(now)-t).total_seconds()
        if seconds<0:raise ValueError('FUTURE_TIMESTAMP')
        return {'at':t.isoformat(),'berlin':t.astimezone(ZoneInfo('Europe/Berlin')).isoformat(),
                'age_seconds':seconds,'within_minutes':{str(m):seconds<=60*m for m in (15,20,25)}}
    except (ValueError,TypeError,AttributeError,OverflowError):
        return {'at':None,'berlin':None,'age_seconds':None,'within_minutes':None}

@lru_cache(maxsize=128)
def session_intervals(venue,date):
    from data_quality import CALENDARS,TIMEZONES
    from timeframes import schedule
    import pandas as pd
    zone=TIMEZONES[CALENDARS[venue]];day=pd.Timestamp(date)
    s=schedule({'venue':venue,'timezone':zone},day,day)
    return tuple((r.open.timestamp(),r.close.timestamp(),
                  None if pd.isna(r.break_start) else r.break_start.timestamp(),
                  None if pd.isna(r.break_end) else r.break_end.timestamp()) for _,r in s.iterrows())

def session_at(venue,at):
    from data_quality import CALENDARS,TIMEZONES
    try:
        t=clock(at);zone=TIMEZONES[CALENDARS[venue]]
        intervals=session_intervals(venue,t.astimezone(ZoneInfo(zone)).date().isoformat())
        n=t.timestamp()
        return 'OPEN' if any(a<=n<b and not(c is not None and c<=n<d) for a,b,c,d in intervals) else 'MARKET_CLOSED'
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError):return 'UNKNOWN'

def session_reader(now):
    cache={}
    def read(venue):
        if venue not in cache:cache[venue]=session_at(venue,now)
        return cache[venue]
    return read


def freshness_state(market_at,now,session,minutes=15):
    """Inclusive age guard for a proven event; never an execution permission."""
    if session=='MARKET_CLOSED':return 'MARKET_CLOSED'
    if session!='OPEN' or type(minutes) is not int or minutes not in (15,20,25):return 'UNKNOWN'
    age=time_fields(market_at,now)['age_seconds']
    if age is None:return 'UNKNOWN'
    return 'WITHIN_LIMIT' if age<=minutes*60 else 'STALE'

def collect(directory,now=None,session=None):
    now=now or datetime.now(timezone.utc).isoformat();clock(now)
    directory=Path(directory)
    session=session or session_reader(now)
    with closing(sqlite3.connect((directory/'stock_monitor.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        infos=[json.loads(r[0]) for r in db.execute('SELECT payload FROM instruments ORDER BY isin')]
        infos=[i for i in infos if i.get('kind')!='benchmark']
        identities=set()
        row=db.execute("SELECT value FROM settings WHERE key='market_freshness_policy'").fetchone()
        policy=json.loads(row[0]) if row else {'minutes':15}
        minutes=policy.get('minutes',15) if isinstance(policy,dict) else None
        if type(minutes) is not int or minutes not in (15,20,25):minutes=None
        if minutes in (20,25) and policy.get('owner_approved') is not True:minutes=15
        from candle_schedule import unit_key,chart_tail,boundary
        heartbeat={}
        for info in infos:
            isin=info['isin'];identities.add((isin,'gettex','GETTEX','EUR',isin))
            identities.update(tuple(r) for r in db.execute("SELECT DISTINCT isin,source,venue,currency,symbol FROM series WHERE isin=? AND currency='EUR' AND base_tf='5m'",(isin,)))
            snapshot=db.execute('SELECT value FROM settings WHERE key=?',('gettex_chart:'+isin+':15m',)).fetchone()
            saved=json.loads(snapshot[0]) if snapshot else {}
            meta=saved.get('identity',{})
            if meta.get('venue')=='GETTEX' and meta.get('currency')=='EUR' and meta.get('source')=='gettex_chart' and meta.get('symbol'):
                key=(isin,meta['source'],meta['venue'],meta['currency'],meta['symbol'])
                identities.add(key)
                scheduled=db.execute('SELECT value FROM settings WHERE key=?',(unit_key(isin,meta,'15m'),)).fetchone()
                last=json.loads(scheduled[0]) if scheduled else {}
                try:tail=chart_tail(saved,meta,'15m',boundary(meta,'15m',now)['close_at'])
                except (ValueError,KeyError,TypeError):tail={}
                heartbeat[key]={**(tail or {'status':'CLOSED_TAIL_UNCONFIRMED'}),'last_attempt_at':last.get('finished_at'),
                    'last_outcome':last.get('outcome'),'retry_after':last.get('retry_after')}
    allowed={i['isin'] for i in infos};records=[];store_status='NOT_STARTED';total=0;confirmed={}
    from delivery_observations import confirmed_event
    path=directory/'delivery_observations.sqlite3'
    if path.exists():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                total=db.execute('SELECT count(*) FROM receipts').fetchone()[0]
                for ident,payload in db.execute('SELECT id,payload FROM receipts ORDER BY id DESC LIMIT 5000'):
                    row=json.loads(payload)
                    if row.get('isin') in allowed and row.get('currency') in ('EUR',None):records.append(dict(row,receipt_id=ident))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='confirmed_latest'").fetchone():
                    for payload, in db.execute('SELECT payload FROM confirmed_latest'):
                        r=json.loads(payload)
                        if r.get('isin') in allowed and confirmed_event(r):confirmed[tuple(r.get(k) for k in FIELDS)]=r
                store_status='AVAILABLE'
        except (sqlite3.Error,ValueError,TypeError):store_status='UNAVAILABLE';records=[]
    latest={};valid_latest={};stats={};observed_events=set()
    # Read oldest to newest within the bounded journal view. Replayed pairs
    # describe the same trade, even when a candle revision changed the tail.
    for r in reversed(records):
        key=tuple(r.get(k) for k in FIELDS)
        if not all(key):continue
        observed=time_fields(r.get('checked_at'),now)
        if not observed['berlin']:continue
        identities.add(key)
        # 5m measurement must never borrow timestamps from 1D/1H fetches.
        if r.get('base_tf') in ('5m','trades'):latest[key]=r
        if r.get('base_tf') in ('5m','trades') and r.get('tail_sha256'):valid_latest[key]=r
        group=key+(r.get('base_tf'),)
        stat=stats.setdefault(group,{'samples':0,'unknown_confirmed_market_time':0,'confirmed_time_samples':0,
             'historical_confirmed_samples':0,'duplicate_confirmed_samples':0,
             'confirmed_within_minutes':{str(m):0 for m in (15,20,25)},'reported_time_samples':0,
             'reported_within_minutes':{str(m):0 for m in (15,20,25)},'observation_days':set(),'outcomes':{},
             'sessions':{s:{'samples':0,'reported_time_samples':0,'confirmed_time_samples':0,
                 'historical_confirmed_samples':0,'duplicate_confirmed_samples':0,'unknown_confirmed_market_time':0,
                 'confirmed_within_minutes':{str(m):0 for m in (15,20,25)},
                 'reported_within_minutes':{str(m):0 for m in (15,20,25)}} for s in ('OPEN','MARKET_CLOSED','UNKNOWN')}})
        bucket=stat['sessions'][session_at(r.get('venue'),r['checked_at'])]
        stat['samples']+=1;bucket['samples']+=1
        actual=time_fields(r['confirmed_market_update_at'],r['checked_at']) if confirmed_event(r) else None
        if not actual or not actual['berlin']:
            category='unknown_confirmed_market_time'
        elif actual['berlin'][:10]!=observed['berlin'][:10]:
            category='historical_confirmed_samples'
        else:
            event_key=(key,actual['at'])
            first=time_fields(r.get('tail_first_observed_at'),r['checked_at'])
            repeated=event_key in observed_events or (first['age_seconds'] or 0)>0
            observed_events.add(event_key)
            category='duplicate_confirmed_samples' if repeated else 'confirmed_time_samples'
        stat[category]+=1;bucket[category]+=1
        if category=='confirmed_time_samples':
            for m,v in actual['within_minutes'].items():
                stat['confirmed_within_minutes'][m]+=int(v);bucket['confirmed_within_minutes'][m]+=int(v)
        outcome=r.get('outcome') or ('RECEIVED' if r.get('tail_sha256') else 'LEGACY_UNCLASSIFIED')
        stat['outcomes'][outcome]=stat['outcomes'].get(outcome,0)+1
        stat['observation_days'].add(observed['berlin'][:10])
        reported=time_fields(r.get('provider_reported_market_at'),r.get('checked_at',now))
        if reported['within_minutes']:
            stat['reported_time_samples']+=1
            bucket['reported_time_samples']+=1
            for m,v in reported['within_minutes'].items():
                stat['reported_within_minutes'][m]+=int(v);bucket['reported_within_minutes'][m]+=int(v)
    source_rows=[]
    for key in sorted(identities):
        r=latest.get(key,{})
        valid=valid_latest.get(key,{})
        market=confirmed.get(key,{})
        state=session(key[2]);status=freshness_state(market.get('confirmed_market_update_at'),now,state,minutes)
        source_rows.append({**dict(zip(FIELDS,key)),'session_status':state,'status':status,
            'chart_15m_heartbeat':heartbeat.get(key),
            'reason':{'MARKET_CLOSED':'MARKET_CLOSED','UNKNOWN':'NO_CONFIRMED_MARKET_UPDATE_OR_POLICY',
                'STALE':'EXCHANGE_EVENT_EXCEEDS_LIMIT','WITHIN_LIMIT':'EXACT_EXCHANGE_EVENT_WITHIN_LIMIT'}[status],
            'confirmed_market_update':time_fields(market.get('confirmed_market_update_at'),now),
            'market_time_evidence':market.get('market_time_evidence'),
            'reported_market_time':time_fields(valid.get('provider_reported_market_at'),now),
            'last_data_receipt_id':valid.get('receipt_id'),
            'latest_attempt':{'outcome':r.get('outcome','LEGACY_UNCLASSIFIED' if r else 'NOT_OBSERVED'),
                              'reason':r.get('reason'),'checked_at':r.get('checked_at'),'http_status':r.get('http_status')},
            'response_received':time_fields(r.get('application_received_at'),now),
            'receipt_checked_at':r.get('checked_at'),'receipt_kind':r.get('receipt_kind','NOT_OBSERVED'),
            'base_tf':r.get('base_tf'),'tail_observation':r.get('tail_observation'),'receipt_id':r.get('receipt_id')})
    stocks=[]
    for info in infos:
        rows=[r for r in source_rows if r['isin']==info['isin']]
        closed=all(r['session_status']=='MARKET_CLOSED' for r in rows)
        primary=[r for r in rows if r['source']=='gettex' and r['venue']=='GETTEX' and r['symbol']==info['isin']]
        primary_status=primary[0]['status'] if primary else 'MARKET_CLOSED' if closed else 'UNKNOWN'
        stocks.append({'isin':info['isin'],'name':info.get('name'),'status':primary_status,
                       'status_source':'official GETTEX only; never substituted for another provider',
                       'source_count':len(rows),'observed_sources':sum(r['receipt_id'] is not None for r in rows)})
    statistics=[]
    for key,stat in sorted(stats.items()):
        stat['observation_days']=sorted(stat['observation_days'])
        statistics.append({**dict(zip(FIELDS+('base_tf',),key)),**stat})
    return {'schema':1,'checked_at':now,'checked_at_berlin':clock(now).astimezone(ZoneInfo('Europe/Berlin')).isoformat(),
        'measurement_only':True,'trade_permission':False,'threshold_minutes':minutes,'threshold_implemented':True,
        'execution_gate':'Existing independent PAPER quote/session/identity/risk checks; this diagnostic table does not grant execution',
        'stock_count':len(stocks),'source_count':len(source_rows),'status_counts':dict(Counter(s['status'] for s in stocks)),
        'confirmed_within_minutes':{str(m):sum(bool(r['confirmed_market_update']['within_minutes'] and r['confirmed_market_update']['within_minutes'][str(m)]) for r in source_rows if r['source']=='gettex' and r['venue']=='GETTEX' and r['session_status']=='OPEN') for m in (15,20,25)},'stocks':stocks,'sources':source_rows,'statistics':statistics,
        'journal':{'status':store_status,'total_receipts':total,'read_limit':5000,'window_truncated':total>5000,
                   'scoped_receipts':len(records),'latest':records[:50]},
        'confirmed_statistics_basis':'First observed confirmed events on the same Berlin date in the bounded journal view; historical and repeated receipts are counted separately.',
        'semantics':'Reported quote time, HTTP receipt and bar time are different. Statistics are observations, not provider latency or permission. Closed/unknown sessions never grant admission. Sources are not merged.',
        'snapshot_semantics':'Market catalog and diagnostic journal are separate read transactions; pinned report time is not current trade permission.'}
