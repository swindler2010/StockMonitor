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

def collect(directory,now=None,session=None):
    now=now or datetime.now(timezone.utc).isoformat();clock(now)
    directory=Path(directory)
    session=session or session_reader(now)
    with closing(sqlite3.connect((directory/'stock_monitor.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        infos=[json.loads(r[0]) for r in db.execute('SELECT payload FROM instruments ORDER BY isin')]
        infos=[i for i in infos if i.get('kind')!='benchmark']
        identities=set()
        for info in infos:
            isin=info['isin'];identities.add((isin,'gettex','GETTEX','EUR',isin))
            identities.update(tuple(r) for r in db.execute("SELECT DISTINCT isin,source,venue,currency,symbol FROM series WHERE isin=? AND currency='EUR' AND base_tf='5m'",(isin,)))
    allowed={i['isin'] for i in infos};records=[];store_status='NOT_STARTED';total=0
    path=directory/'delivery_observations.sqlite3'
    if path.exists():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                total=db.execute('SELECT count(*) FROM receipts').fetchone()[0]
                for ident,payload in db.execute('SELECT id,payload FROM receipts ORDER BY id DESC LIMIT 5000'):
                    row=json.loads(payload)
                    if row.get('isin') in allowed and row.get('currency') in ('EUR',None):records.append(dict(row,receipt_id=ident))
                store_status='AVAILABLE'
        except (sqlite3.Error,ValueError,TypeError):store_status='UNAVAILABLE';records=[]
    latest={};valid_latest={};stats={}
    for r in records:
        key=tuple(r.get(k) for k in FIELDS)
        if not all(key):continue
        identities.add(key)
        # 5m measurement must never borrow timestamps from 1D/1H fetches.
        if r.get('base_tf') in ('5m','trades'):latest.setdefault(key,r)
        if r.get('base_tf') in ('5m','trades') and r.get('tail_sha256'):valid_latest.setdefault(key,r)
        group=key+(r.get('base_tf'),)
        stat=stats.setdefault(group,{'samples':0,'unknown_confirmed_market_time':0,'reported_time_samples':0,
             'reported_within_minutes':{str(m):0 for m in (15,20,25)},'observation_days':set(),'outcomes':{},
             'sessions':{s:{'samples':0,'reported_time_samples':0,'reported_within_minutes':{str(m):0 for m in (15,20,25)}} for s in ('OPEN','MARKET_CLOSED','UNKNOWN')}})
        stat['samples']+=1;stat['unknown_confirmed_market_time']+=1
        outcome=r.get('outcome') or ('RECEIVED' if r.get('tail_sha256') else 'LEGACY_UNCLASSIFIED')
        stat['outcomes'][outcome]=stat['outcomes'].get(outcome,0)+1
        observed=time_fields(r.get('checked_at'),now)
        if observed['berlin']:stat['observation_days'].add(observed['berlin'][:10])
        reported=time_fields(r.get('provider_reported_market_at'),r.get('checked_at',now))
        session_status=session_at(r.get('venue'),r.get('checked_at'))
        bucket=stat['sessions'][session_status];bucket['samples']+=1
        if reported['within_minutes']:
            stat['reported_time_samples']+=1
            bucket['reported_time_samples']+=1
            for m,v in reported['within_minutes'].items():
                stat['reported_within_minutes'][m]+=int(v);bucket['reported_within_minutes'][m]+=int(v)
    source_rows=[]
    for key in sorted(identities):
        r=latest.get(key,{})
        valid=valid_latest.get(key,{})
        state=session(key[2]);status='MARKET_CLOSED' if state=='MARKET_CLOSED' else 'UNKNOWN'
        source_rows.append({**dict(zip(FIELDS,key)),'session_status':state,'status':status,
            'reason':'MARKET_CLOSED' if status=='MARKET_CLOSED' else 'NO_CONFIRMED_MARKET_UPDATE',
            'confirmed_market_update':time_fields(None,now),
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
        stocks.append({'isin':info['isin'],'name':info.get('name'),'status':'MARKET_CLOSED' if closed else 'UNKNOWN',
                       'source_count':len(rows),'observed_sources':sum(r['receipt_id'] is not None for r in rows)})
    statistics=[]
    for key,stat in sorted(stats.items()):
        stat['observation_days']=sorted(stat['observation_days'])
        statistics.append({**dict(zip(FIELDS+('base_tf',),key)),**stat})
    return {'schema':1,'checked_at':now,'checked_at_berlin':clock(now).astimezone(ZoneInfo('Europe/Berlin')).isoformat(),
        'measurement_only':True,'trade_permission':False,'threshold_minutes':15,'threshold_implemented':False,
        'stock_count':len(stocks),'source_count':len(source_rows),'status_counts':dict(Counter(s['status'] for s in stocks)),
        'confirmed_within_minutes':{str(m):0 for m in (15,20,25)},'stocks':stocks,'sources':source_rows,'statistics':statistics,
        'journal':{'status':store_status,'total_receipts':total,'read_limit':5000,'window_truncated':total>5000,
                   'scoped_receipts':len(records),'latest':records[:50]},
        'semantics':'Reported quote time, HTTP receipt and bar time are different. Statistics are observations, not provider latency or permission. Closed/unknown sessions never grant admission. Sources are not merged.',
        'snapshot_semantics':'Market catalog and diagnostic journal are separate read transactions; pinned report time is not current trade permission.'}
