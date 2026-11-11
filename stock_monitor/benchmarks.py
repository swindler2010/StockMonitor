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

def load(store,info):
    meta=identity(info)
    frame=store.load_series(info['isin'],'1D',meta['source'],meta['symbol'],meta['venue'],meta['currency'])
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
    return 'INDEX:SP500' if info.get('history_currency')=='USD' else None

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
