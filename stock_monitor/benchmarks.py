"""Explicit direct-index catalog and comparable, closed-session performance.

No ETF substitution. Archive defects and supported calculation coverage are separate.
"""
import io
import requests
import pandas as pd
import data_quality as quality
import history_service

VIX_URL='https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'
CATALOG={}
for code,name,symbol,venue,currency,zone,variant in (
    ('SP500','S&P 500','^GSPC','SNP','USD','America/New_York','price'),
    ('NDX','Nasdaq-100','^NDX','NIM','USD','America/New_York','price'),
    ('SOX','SOX / Semiconductor Index','^SOX','NIM','USD','America/New_York','price'),
    ('DAX','DAX','^GDAXI','GER','EUR','Europe/Berlin','performance (includes dividends)'),
    ('STOXX600','STOXX Europe 600','^STOXX','ZRH','EUR','Europe/Zurich','price'),
    ('VIX','VIX','VIX','CXI','USD','America/Chicago','volatility')):
    CATALOG['INDEX:'+code]={'isin':'INDEX:'+code,'name':name,'kind':'benchmark',
        'history_symbol':symbol,'history_source':'cboe' if code=='VIX' else 'proxy',
        'source_mode':'proxy','history_venue':venue,'currency':currency,'history_currency':currency,
        'timezone':zone,'return_basis':variant,'unit':'volatility points' if code=='VIX' else 'index points',
        'volume_policy':'not applicable: index is not a traded security','position':{},'levels':{}}

def identity(info):
    return dict(source=info['history_source'],symbol=info['history_symbol'],
                venue=info['history_venue'],currency=info['currency'],timezone=info['timezone'])

INTRADAY_BASE='5m'
INTRADAY_BASES=('5m','1m')

def base_for(tf):
    from timeframes import MINUTES
    return '1m' if tf in ('1m','3m') else INTRADAY_BASE if tf in MINUTES else '1D'


def data_identity(info,base='1D'):
    if base=='1D':return identity(info)
    registered=CATALOG.get(info.get('isin'))
    if registered is None or identity(info)!=identity(registered):raise ValueError('INDEX_CATALOGUE_IDENTITY_MISMATCH')
    # Same direct VIX index, separate Yahoo series. Never relabel these rows
    # as the official Cboe daily CSV or append one provider to the other.
    if info['isin']=='INDEX:VIX':
        return dict(source='proxy',symbol='^VIX',venue='CXI',currency='USD',timezone='America/Chicago')
    return identity(info)


def proxy_identity(symbol):
    matches=[data_identity(i,INTRADAY_BASE) for i in CATALOG.values()
             if data_identity(i,INTRADAY_BASE)['symbol']==symbol]
    return matches[0] if len(matches)==1 else None


def history_key(info,base='1D'):
    return 'benchmark_history:'+info['isin']+(':'+base if base!='1D' else '')


def feed_plan(store,info,now=None):
    """One native base per existing serial turn; no new loader or thread."""
    from candle_schedule import decision
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'));due=[]
    for base in INTRADAY_BASES:
        meta=data_identity(info,base);plan=decision(store,info['isin'],meta,base,now)
        saved=store.setting(history_key(info,base),{})
        if (not plan['due'] and plan.get('reason')=='UNCHANGED' and not saved.get('count')
                and saved.get('attempted_at') and now>=pd.Timestamp(saved['attempted_at'])+pd.Timedelta(days=1)):
            plan={**plan,'due':True,'reason':'INDEX_EMPTY_DAILY_RECHECK'}
        if plan['due']:due.append(dict(due=True,base=base,plan=plan,attempted_at=saved.get('attempted_at',''),missing=not saved.get('count')))
    missing=next((p for p in due if p['missing']),None)
    if missing:return missing
    daily=store.setting(history_key(info),{});seconds=86400 if info['history_source']=='cboe' else 3600
    daily_due=not daily.get('attempted_at') or now>=pd.Timestamp(daily['attempted_at'])+pd.Timedelta(seconds=seconds)
    cooldown=store.setting('cp_source_retry:'+identity(info)['source'],None)
    if cooldown and now<pd.Timestamp(cooldown):daily_due=False
    if daily_due:return dict(due=True,base='1D',attempted_at=daily.get('attempted_at',''))
    if due:return min(due,key=lambda p:p['attempted_at'])
    return dict(due=False,base=INTRADAY_BASE,reason=plan.get('reason'),attempted_at=saved.get('attempted_at',''))


def sync_intraday(store,info,now=None,downloader=None,plan=None,*,base=INTRADAY_BASE):
    from candle_schedule import decision,finish
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'));meta=data_identity(info,base)
    if base not in INTRADAY_BASES:raise ValueError('UNSUPPORTED_INDEX_BASE')
    key=history_key(info,base);state=store.setting(key,{})
    plan=decision(store,info['isin'],meta,base,now) if plan is None else plan
    if not plan.get('due'):return state
    cached=load(store,info,base);start=None
    if len(cached):
        start=pd.Timestamp(cached.index[-1]).tz_localize(meta['timezone'])
        if start>=now:raise ValueError('Future index intraday watermark')
    error=None
    try:
        incoming=(history_service.download(meta['symbol'],base,start=(max(start,now-pd.Timedelta(days=7)+pd.Timedelta(minutes=1)) if base=='1m' and start is not None else start),store=store,**({'minute_chart':True} if base=='1m' else {}))
                  if downloader is None else downloader(info,start))
        incoming.attrs.update(unit=info['unit'],return_basis=info['return_basis'],volume_policy=info['volume_policy'])
        report=quality.audit(incoming,base,meta,now=now,check_stale=False)
        # GAP contains real rows and explicit missing slots. Retain them for a
        # segmented chart; no filling, waiver or analytical READY promotion.
        if len(incoming) and report['state'] not in ('READY','GAP','STALE'):
            raise quality.QualityError(report)
        if len(incoming):store.save_series(info,incoming,base)
        cached=load(store,info,base);checked=quality.audit(cached,base,meta,now=now)
        state.update(identity=meta,quality=checked['state'],issues=checked['issues'],ranges=checked['ranges'],
            status='SAVED' if len(incoming) else 'UNCHANGED' if len(cached) else 'SOURCE_UNAVAILABLE',
            first=str(cached.index.min()) if len(cached) else None,last=str(cached.index.max()) if len(cached) else None,
            count=len(cached),normalization=incoming.attrs.get('intraday_normalization',{}),
            source_constraint=('Native Yahoo index 1m; initial named range 7d; saved rows accumulate; regular-session projection only' if base=='1m' else 'Native Yahoo index 5m; initial provider window <=60 days; saved rows accumulate; regular-session projection only'))
        # Provider-delay proof comes from actual saved closed bars, not HTTP
        # success. Reuse the scheduler's durable 15-minute retry up to four
        # attempts per closed boundary, then retain the explicit block.
        pending=not len(cached) or (not plan.get('session_open',False) and checked.get('stale_count',0)>0)
        if pending:
            count=state.get('publication_checks',0) if state.get('publication_close_at')==plan.get('close_at') else 0
            if plan.get('reason')=='INDEX_EMPTY_DAILY_RECHECK':count=0
            state.update(publication_close_at=plan.get('close_at'),publication_checks=count+1,
                         publication_wait='EMPTY_SOURCE' if not len(cached) else 'LATE_CLOSED_TAIL')
            if count+1<4:error=ValueError('INDEX_INTRADAY_'+state['publication_wait'])
            else:state['publication_wait']='UNCONFIRMED_AFTER_BOUNDED_RECHECKS'
        else:
            for field in ('publication_close_at','publication_checks','publication_wait'):state.pop(field,None)
    except Exception as caught:
        error=caught
        state.update(quality='SOURCE_UNAVAILABLE' if isinstance(caught,requests.RequestException) else 'INVALID',
                     issues=[str(caught)],status='ERROR')
    state.update(attempted_at=now.isoformat(),identity=meta,base=base,
                 source_url='https://query1.finance.yahoo.com/v8/finance/chart/'+meta['symbol'])
    store.set_setting(key,state);finish(store,info['isin'],meta,base,plan,error,now)
    return state


def sync_history(store,info,now=None):
    plan=feed_plan(store,info,now)
    if not plan['due']:return {**store.setting(history_key(info,plan['base']),{}),'feed_due':False}
    state=sync_intraday(store,info,now,plan=plan['plan'],base=plan['base']) if plan['base'] in INTRADAY_BASES else sync(store,info,now)
    return {**state,'base':plan['base']}


def ensure_catalog(store):
    present={x['isin'] for x in store.instruments()}
    for code,info in CATALOG.items():
        if code not in present:store.save_instrument(info)

def download(info,start=None,store=None):
    if info['history_source']!='cboe':
        frame=history_service.download(info['history_symbol'],'1d',start=start,store=store)
        if not frame.empty:
            # Cash indices have no traded-share volume. Normalize only this
            # explicitly N/A field to zero; missing prices remain invalid.
            frame['volume']=pd.to_numeric(frame['volume'],errors='coerce').fillna(0.0)
    else:
        response=requests.get(VIX_URL,timeout=30);response.raise_for_status()
        frame=pd.read_csv(io.StringIO(response.text))
        frame.index=pd.to_datetime(frame.pop('DATE'),format='%m/%d/%Y')
        frame.columns=frame.columns.str.lower();frame['volume']=0.0
        frame.attrs=identity(info)
        if start is not None:frame=frame.loc[pd.Timestamp(start).tz_localize(None) if pd.Timestamp(start).tzinfo else pd.Timestamp(start):]
        else:frame=frame.loc[pd.Timestamp.now().normalize()-pd.DateOffset(years=20):]
    frame.attrs.update(unit=info['unit'],return_basis=info['return_basis'],volume_policy=info['volume_policy'])
    return frame

def load(store,info,base='1D'):
    meta=data_identity(info,base)
    frame=store.load_series(info['isin'],base,meta['source'],meta['symbol'],meta['venue'],meta['currency'])
    frame.attrs.update(meta,unit=info['unit'],return_basis=info['return_basis'],volume_policy=info['volume_policy'])
    return frame

def sync(store,info,now=None,downloader=None):
    """Watermark update; an invalid archive never causes a full download loop.

    Retain only the explicitly validated contiguous suffix when the first archive
    is defective. Its limited coverage and rejected ranges remain visible.
    """
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    state_key='benchmark_history:'+info['isin'];state=store.setting(state_key,{})
    cached=load(store,info)
    retry_seconds=24*3600 if info['history_source']=='cboe' else 3600
    if state.get('attempted_at') and (now-pd.Timestamp(state['attempted_at'])).total_seconds()<retry_seconds:
        return state
    # Recheck the final session: it may have been saved while still forming.
    # A tomorrow-only watermark both skips its final OHLCV and sends period1
    # after period2 on the same day (Yahoo HTTP 400). Never refetch the archive.
    start=cached.index.max().tz_localize(info['timezone']) if not cached.empty else None
    try:
        if start is not None and start>=now:raise ValueError('Benchmark cache contains a future session; no future request sent')
        incoming=download(info,start,store=store) if downloader is None else downloader(info,start)
        if incoming.empty:
            report=quality.audit(cached,'1D',identity(info),now=now)
            state.update(quality=report['state'],issues=report['issues'])
        else:
            report=quality.audit(incoming,'1D',identity(info),now=now,check_stale=False)
            if report['state'] in ('INVALID','GAP'):
                # Identity/order/duplicates cannot be repaired by truncating dates.
                for field,value in identity(info).items():
                    if incoming.attrs.get(field)!=value:raise ValueError('Identity mismatch: '+field)
                if incoming.index.has_duplicates or not incoming.index.is_monotonic_increasing:
                    raise ValueError('Invalid timestamp order or duplicate index')
                if not report['ranges']:raise ValueError('; '.join(report['issues']))
                boundary=max(pd.Timestamp(b) for a,b in report['ranges'])
                accepted=incoming.loc[incoming.index>boundary].copy();accepted.attrs=dict(incoming.attrs)
                suffix=quality.audit(accepted,'1D',identity(info),now=now)
                state.update(status='INCOMPLETE',archive_quality=report['state'],rejected_ranges=report['ranges'],archive_issues=report['issues'])
                if suffix['state']!='READY':raise ValueError('No verified contiguous suffix: '+str(suffix))
                if not cached.empty:raise ValueError('Incremental range invalid; correct stored history preserved')
                incoming=accepted
            else:state.setdefault('status','COMPLETE')
            store.save_series(info,incoming,'1D');cached=load(store,info)
            checked=quality.audit(cached,'1D',identity(info),now=now)
            state.update(quality=checked['state'],issues=checked['issues'],first=str(cached.index.min()),last=str(cached.index.max()),count=len(cached))
        state.update(identity=identity(info),attempted_at=now.isoformat(),source_url=VIX_URL if info['history_source']=='cboe' else 'https://query1.finance.yahoo.com/v8/finance/chart/'+info['history_symbol'],
                     source_constraint='Cboe CSV is a full-file endpoint; SQLite uses watermark' if info['history_source']=='cboe' else 'Direct Yahoo index, daily archive')
    except Exception as error:
        state.update(quality='SOURCE_UNAVAILABLE' if isinstance(error,requests.RequestException) else 'INVALID',issues=[str(error)],attempted_at=now.isoformat())
    store.set_setting(state_key,state)
    return state

def default_benchmark(info):
    symbol=info.get('history_symbol') or info.get('profile_symbol') or ''
    # Store listings such as APC.F are European trading lines for US companies;
    # choose context from the underlying issuer identity, not the local suffix.
    isin=info.get('isin')
    if isin in {'US0378331005','US5949181045','US0231351067','US02079K1079','US02079K3059','US30303M1027','US88160R1014'}:
        return 'INDEX:NDX'
    if isin in {'US67066G1040','US11135F1012','US0079031078','US4581401001','TW0002330008'}:
        return 'INDEX:SOX'
    if isin and isin.startswith('DE'):
        return 'INDEX:DAX'
    if symbol in ('NVDA','AVGO','AMD','INTC','TSM'):return 'INDEX:SOX'
    if symbol in ('NVDA.DE','AVGO.DE','AMD.DE','INTC.DE','TSM.DE'):return 'INDEX:SOX'
    if symbol in ('AAPL','MSFT','AMZN','GOOG','GOOGL','META','TSLA','APC.F','APC.DE','TL0.F','TL0.DE'):return 'INDEX:NDX'
    if symbol.endswith('.DE'):return 'INDEX:DAX'
    if symbol.endswith(('.PA','.AS','.MI','.SW','.L')):return 'INDEX:STOXX600'
    # A future GETTEX listing can have only an ISIN before the history profile
    # is discovered. Keep the existing domestic broad indices as defaults.
    if (isin and isin.startswith('US')) or info.get('history_currency')=='USD':
        return 'INDEX:SP500'
    if isin and isin[:2] in {'AT','BE','CH','DK','ES','FI','FR','GB','IE','IT','NL','NO','PT','SE'}:
        return 'INDEX:STOXX600'
    return None

def relative_performance(stock,benchmark,period='месяц',now=None,threshold_pp=0.1):
    """Local-currency price returns, same closed daily session labels and endpoints."""
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    result={'state':'UNAVAILABLE','basis':'closed 1D / local currency returns; no FX conversion',
            'period':period,'neutral_threshold_pp':threshold_pp,
            'stock_identity':{k:stock.attrs.get(k) for k in quality.IDENTITY},
            'benchmark_identity':{k:benchmark.attrs.get(k) for k in quality.IDENTITY}}
    if benchmark.attrs.get('return_basis')=='volatility':return {**result,'reason':'VIX is volatility context, not an equity performance benchmark'}
    if stock.empty or benchmark.empty:return {**result,'reason':'History unavailable'}
    if any(not stock.attrs.get(k) for k in quality.IDENTITY) or any(not benchmark.attrs.get(k) for k in quality.IDENTITY):
        return {**result,'reason':'Data Quality INVALID: не подтверждены source/listing/currency/timezone'}
    months={'месяц':1,'6 месяцев':6,'год':12,'2 года':24,'5 лет':60}.get(period)
    lower=now.tz_localize(None).normalize()-pd.DateOffset(months=months) if months else max(stock.index.min(),benchmark.index.min())
    from chart_periods import DAYS
    if period in DAYS:lower=now.tz_convert('Europe/Berlin').tz_localize(None).normalize()-pd.Timedelta(days=DAYS[period])
    frames=[]
    for raw in (stock,benchmark):
        f=raw.loc[raw.index>=lower].copy();f.attrs=dict(raw.attrs)
        report=quality.audit(f,'1D',now=now)
        if report['state']!='READY':return {**result,'reason':'Data Quality '+report['state'],'quality':report}
        closed=quality.expected_index(f.attrs,'1D',f.index.min(),f.index.max(),now)
        frames.append(f.loc[f.index.intersection(closed)])
    a,b=frames;dates=a.index.intersection(b.index)
    if len(dates)<2:return {**result,'reason':'Fewer than two comparable closed sessions'}
    a=a.loc[dates];b=b.loc[dates]
    ar=float((a.close.iloc[-1]/a.close.iloc[0]-1)*100);br=float((b.close.iloc[-1]/b.close.iloc[0]-1)*100);delta=ar-br
    return {**result,'state':'OUTPERFORMING' if delta>threshold_pp else 'UNDERPERFORMING' if delta<-threshold_pp else 'NEUTRAL',
            'stock_return_pct':ar,'benchmark_return_pct':br,'difference_pp':delta,
            'first':str(dates[0]),'last':str(dates[-1]),'comparable_sessions':len(dates),'quality':'READY',
            'stock_currency':stock.attrs.get('currency'),'benchmark_currency':benchmark.attrs.get('currency'),
            'benchmark_return_basis':benchmark.attrs.get('return_basis','price')}
