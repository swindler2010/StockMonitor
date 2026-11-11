"""ISIN-scoped EUR discovery. OpenFIGI identity + Yahoo venue metadata.

Suffixes follow https://help.yahoo.com/kb/SLN2352.html, not company-name
heuristics. Only venues with existing verified calendars are enabled.
"""
import re
import time
import threading
from collections import deque
from urllib.parse import quote
import requests

VENUES=(('XETR','GY','GER','.DE','Europe/Berlin'),
        ('XFRA','GF','FRA','.F','Europe/Berlin'))
TARGETS={'5m':100,'10m':100,'15m':100,'30m':100,'1H':1000,'2H':1000,'3H':1000,'4H':1000,'1D':3000,'1W':1000,'1M':240}

OWNER_HISTORY_POLICY='cp_gettex_source_policy'

def owner_history_info(info,policy):
    """Apply the owner's exchange scope, preserving separate deep archives."""
    if (info.get('kind','stock')=='stock' and policy.get('version')==1
            and policy.get('approved') is True and policy.get('scope')=='registered_stocks'
            and policy.get('exclusive_venue')=='GETTEX'):
        return {**info,'history_venue_lock':'GETTEX'}
    return info


def venue_allowed(info,meta):
    """Owner's optional venue lock; archived rows and identity receipts remain."""
    venue=info.get('history_venue_lock')
    return not venue or (meta.get('venue')==venue and meta.get('currency')=='EUR')


def confirmed_listing(info,meta):
    """One exact listing receipt, including legacy selected-cache resolution."""
    from data_quality import IDENTITY
    if not all(meta.get(k) for k in IDENTITY) or meta.get('currency')!='EUR' or not venue_allowed(info,meta):return False
    if meta.get('source')=='gettex':
        return meta.get('symbol')==info['isin'] and meta.get('venue')=='GETTEX' and meta.get('timezone')=='Europe/Berlin'
    receipts=list(info.get('eur_listings') or [])
    proof=meta.get('listing_proof')
    if proof:receipts.append(proof)
    return any(x.get('status')=='CONFIRMED' and x.get('isin')==info['isin'] and
               all(x.get(k)==meta.get(k) for k in IDENTITY) for x in receipts)


def stored_identity(db,info,meta,base):
    """Resolve proof from this database, never from an untrusted command body."""
    import json
    from data_quality import IDENTITY
    exact={k:meta.get(k) for k in IDENTITY}
    if exact.get('source')=='gettex_chart':
        from gettex_chart import state_key,confirmed_state
        row=db.execute('SELECT value FROM settings WHERE key=?',(state_key(info['isin'],base),)).fetchone()
        saved=json.loads(row[0]) if row else {}
        if not confirmed_state(info,saved) or saved['identity']!=exact:return None
        exact['listing_proof']=saved['listing_proof']
    elif exact.get('source')=='proxy':
        row=db.execute('SELECT value FROM settings WHERE key=?',('history_identity:'+info['isin'],)).fetchone()
        saved=json.loads(row[0]) if row else {}
        if all(saved.get(k)==exact[k] for k in IDENTITY):exact['listing_proof']=saved.get('listing_proof')
    return exact if confirmed_listing(info,exact) else None


def ensure_selected_proof(info,store,now=None):
    """Existing feed lane + resolver + durable scheduler; no source selection.

    Enrich only the currently selected history identity. Never update watchlist,
    user source settings, candles or DQ. A changed input cancels this receipt.
    """
    import json,pandas as pd
    import candle_schedule as scheduler
    from data_quality import IDENTITY
    clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'));key='history_identity:'+info['isin']
    with store.connect() as db:
        db.execute('BEGIN')
        saved=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        stock=db.execute('SELECT payload FROM instruments WHERE isin=?',(info['isin'],)).fetchone()
    if not saved or not stock:return {'state':'NOT_SELECTED'}
    meta=json.loads(saved[0]);current=json.loads(stock[0])
    if not venue_allowed(current,meta):return {'state':'VENUE_EXCLUDED','reason':'Сохранённый резерв не используется: выбрана другая биржа'}
    if confirmed_listing(current,meta):return {'state':'CONFIRMED'}
    if meta.get('source')!='proxy' or meta.get('currency')!='EUR' or not all(meta.get(k) for k in IDENTITY):
        return {'state':'UNCONFIRMED','reason':'Current EUR listing identity incomplete'}
    unit=scheduler.unit_key(info['isin'],meta,'listing-proof')
    previous=store.setting(unit,{})
    cooldown=store.setting('cp_source_retry:listing',None)
    if cooldown and clock<pd.Timestamp(cooldown):return {'state':'COOLDOWN'}
    if previous.get('retry_after') and clock<pd.Timestamp(previous['retry_after']):return {'state':'COOLDOWN'}
    if previous.get('finished_at') and previous.get('outcome')=='CHECKED' and clock<pd.Timestamp(previous['finished_at'])+pd.Timedelta(days=1):
        return {'state':'UNCONFIRMED','reason':'No matching receipt; bounded daily recheck'}
    plan={'key':unit,'planned_at':clock.isoformat(),'started_at':clock.isoformat()}
    try:
        receipts=discover(info['isin'])
        matches=[x for x in receipts if x.get('status')=='CONFIRMED' and x.get('isin')==info['isin'] and all(x.get(k)==meta[k] for k in IDENTITY)]
        if len(matches)==1:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if (db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()!=saved or
                    db.execute('SELECT payload FROM instruments WHERE isin=?',(info['isin'],)).fetchone()!=stock):
                    raise ValueError('LISTING_PROOF_INPUT_CHANGED')
                meta['listing_proof']=matches[0]
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(meta,ensure_ascii=False)))
        scheduler.finish(store,info['isin'],{'source':'listing'},'listing-proof',plan,now=clock)
        return {'state':'CONFIRMED' if len(matches)==1 else 'UNCONFIRMED','reason':'Exact existing ISIN/source/venue/currency/symbol/timezone receipt only'}
    except Exception as error:
        scheduler.finish(store,info['isin'],{'source':'listing'},'listing-proof',plan,error,now=clock)
        return {'state':'UNCONFIRMED','reason':str(error)}

def rank_reports(reports):
    ready=sum(min(1,r['count']/r['target']) for r in reports.values() if r['quality']=='READY')
    usable=[r for r in reports.values() if r['quality'] in ('READY','STALE','GAP','SOURCE_GAP') and r['count']]
    # Invalid rows never buy a higher rank. Gapped data remains explicitly gapped.
    return ready,len(usable),sum(r['count'] for r in usable)

def cached_daily_history(store,info,tf,current,analysis_at=None):
    """Select already stored confirmed EUR archives in the existing chart worker.

    No discovery, downloads, setting writes or cross-listing candle joins.
    Retain the selected listing on equal quality/depth, using the existing rank.
    """
    from analytics import resample
    import data_quality as quality
    choices=[]
    listings=sorted(info.get('eur_listings',[]),key=lambda l:l.get('symbol')!=current.get('symbol'))
    for listing in listings:
        if (listing.get('status')!='CONFIRMED' or listing.get('isin')!=info['isin'] or
            listing.get('currency')!='EUR' or listing.get('source')!='proxy' or
            not all(listing.get(k) for k in quality.IDENTITY)):continue
        meta={k:listing[k] for k in quality.IDENTITY}
        raw=store.load_series(info['isin'],'1D','proxy',meta['symbol'],meta['venue'],'EUR')
        if raw.empty:continue
        # Validate the returned row identity before attaching listing metadata.
        if any(raw.attrs.get(k)!=meta[k] for k in quality.IDENTITY if k!='timezone'):continue
        raw.attrs.update(meta)
        if analysis_at is not None:
            from calculation_window import scope_raw,scope_report
            raw=scope_raw(raw,'1D',analysis_at)
            if raw.empty:continue
        report=quality.audit(raw,'1D',meta,now=analysis_at)
        persisted=store.setting(quality.key(info,meta['symbol'],'1D'),{})
        if persisted.get('identity') and any(persisted['identity'].get(k)!=meta[k] for k in quality.IDENTITY):continue
        if analysis_at is not None:
            raw.attrs['archive_quality_before_scope']=persisted or {'state':'UNKNOWN','issues':['No saved complete archive report']}
            persisted=scope_report(persisted,raw.attrs['analysis_scope']['start'])
        if report['state']!='INVALID' and persisted.get('state') in ('INVALID','RECOVERING','SOURCE_UNAVAILABLE','SOURCE_GAP'):
            report={**report,**persisted}
        raw.attrs['data_quality']=report
        view=resample(raw,'1D',tf,now=analysis_at)
        summary=depth(view,tf)
        # Rank the stored source by its own audit. Incomplete derived calendar
        # buckets must not make an otherwise usable daily archive disappear.
        # The derived audit remains attached and still blocks dependent signals.
        summary['quality']=report['state']
        score=rank_reports({tf:summary})
        if score[1]:choices.append((score,raw))
    if choices:return max(choices,key=lambda c:c[0])[1]
    # Imported providers are a separate fallback, never a shorter replacement
    # of any usable existing EUR archive. Native and derived DQ stay attached.
    from onvista_archive import available_daily
    for raw in available_daily(store,info,now=analysis_at):
        if analysis_at is not None:
            from calculation_window import scope_raw
            raw=scope_raw(raw,'1D',analysis_at)
        if raw.empty or raw.attrs.get('data_quality',{}).get('state') not in ('READY','STALE','GAP'):continue
        choices.append((len(raw),raw))
    return max(choices,key=lambda c:c[0])[1] if choices else None

def depth(frame,tf):
    import pandas as pd
    from analytics import bar_ends
    closed=frame
    if not frame.empty:
        closed=frame.loc[bar_ends(frame)<=pd.Timestamp.now(tz='UTC')]
        if 'bar_complete' in closed:closed=closed.loc[closed.bar_complete.fillna(False)]
    count=len(closed);enough=count>=TARGETS[tf]
    if tf=='1M' and count:
        enough=enough and closed.index[0]<=closed.index[-1]-pd.DateOffset(years=20)+pd.DateOffset(months=1)
    # Reaching a count target is not evidence of a complete usable archive.
    # Keep the depth result separate so gaps/invalid data cannot display COMPLETE.
    depth_status='COMPLETE' if enough else 'SOURCE_LIMITED'
    quality=frame.attrs.get('data_quality',{}).get('state')
    if quality=='READY':status=depth_status
    elif quality in ('STALE','GAP','SOURCE_GAP','INVALID','RECOVERING','SOURCE_UNAVAILABLE'):status=quality
    else:status='INCOMPLETE' if enough else 'SOURCE_LIMITED'
    return dict(count=count,target=TARGETS[tf],status=status,depth_status=depth_status,target_met=bool(enough),
                first=str(closed.index[0]) if count else None,last=str(closed.index[-1]) if count else None)
_lock=threading.Lock()
_calls=deque()
_cooldown=0.
_cache={}

def discover(isin):
    import market
    global _cooldown
    if not market.valid_isin(isin):raise ValueError('Некорректный ISIN')
    with _lock:
        now=time.monotonic()
        if now<_cooldown:raise ValueError('EUR listing discovery: cooldown источника')
        saved=_cache.get(isin)
        if saved and now-saved[0]<3600:return [dict(x) for x in saved[1]]
        while _calls and now-_calls[0]>=60:_calls.popleft()
        if len(_calls)>=20:raise ValueError('EUR listing discovery: локальный rate limit')
        _calls.append(now)
        jobs=[dict(idType='ID_ISIN',idValue=isin,micCode=v[0],currency='EUR',marketSecDes='Equity') for v in VENUES]
        response=requests.post('https://api.openfigi.com/v3/mapping',json=jobs,timeout=20)
        if response.status_code==429:
            _cooldown=now+60
        response.raise_for_status()
        payload=response.json()
        if not isinstance(payload,list) or len(payload)!=len(jobs):raise ValueError('OpenFIGI: неверное число ответов')
        found=[]
        for venue,job,result in zip(VENUES,jobs,payload):
            mic,exchange,yahoo,suffix,zone=venue
            if result.get('error'):raise ValueError('OpenFIGI: '+str(result['error']))
            rows=result.get('data',[])
            # Ambiguous share classes/listings must never be selected by name.
            if len(rows)!=1:continue
            row=rows[0];ticker=row.get('ticker','')
            if row.get('exchCode')!=exchange or row.get('marketSector')!='Equity':continue
            if not re.fullmatch(r'[A-Za-z0-9-]+',ticker) or not row.get('figi'):continue
            symbol=ticker+suffix
            chart=requests.get('https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol,safe=''),
                params={'range':'1d','interval':'1d'},headers={'User-Agent':'Mozilla/5.0 StockMonitor/0.16'},timeout=20)
            if chart.status_code==429:
                _cooldown=time.monotonic()+60
                chart.raise_for_status()
            if chart.status_code==404:continue
            chart.raise_for_status()
            data=chart.json().get('chart',{})
            if data.get('error') or len(data.get('result') or [])!=1:continue
            meta=data['result'][0].get('meta',{})
            if any(meta.get(k)!=v for k,v in dict(symbol=symbol,currency='EUR',exchangeName=yahoo,exchangeTimezoneName=zone).items()):continue
            if meta.get('instrumentType') not in ('EQUITY','ETF'):continue
            found.append(dict(status='CONFIRMED',isin=isin,symbol=symbol,venue=yahoo,currency='EUR',
                source='proxy',timezone=zone,mic=mic,figi=row['figi'],name=row.get('name',symbol),
                evidence={'isin':'OpenFIGI ID_ISIN + MIC + EUR filter','request':job,'figi':row['figi'],
                          'listing':'Yahoo exact symbol / venue / EUR / timezone','checksum':True}))
        _cache[isin]=(time.monotonic(),found)
        return [dict(x) for x in found]

def resolve_for_add(symbol,cache_dir,search_result,requested_isin=None):
    """Runs inside the existing Search/Add background job, never Tk."""
    import market
    try:
        if requested_isin and market.valid_isin(requested_isin):
            isin=requested_isin
        else:
            original=market.resolve_symbol(symbol,cache_dir,search_result)
            if original.get('status')!='CONFIRMED':return original
            isin=original['isin']
        listings=discover(isin)
        if not listings:raise ValueError('Не найден подтверждённый EUR-листинг с поддержанным календарём')
        return {**listings[0],'eur_listings':listings}
    except Exception as error:
        return {'status':'BLOCKED','error':'EUR Identity BLOCKED: '+str(error)}
