"""Public GETTEX chart archive, on CP's existing feed lane and series store.

The chart vendor is NOT the post-trade file source. Never join their candles.
Native 5m/15m buckets follow the vendor chart SDK; hourly data are deliberately
not imported (its clock-hour buckets differ from our session-open buckets).
"""
import base64
import hashlib
import json
import re
from pathlib import Path

import pandas as pd
import requests

import candle_schedule as scheduler
import data_quality as quality
from timeframes import endpoints, aggregate

SOURCE = 'gettex_chart'
BASES = ('15m', '5m', '1D', '1m')
# 1m is an internal live/recovery input, never a twelfth UI/strategy TF.
INTRADAY_MINUTES = {'1m': 1, '5m': 5, '15m': 15}
FIELDS = ('open', 'high', 'low', 'close', 'volume')
HOST = 'https://lseg-widgets.financial.com'


def state_key(isin, base):
    return 'gettex_chart:' + isin + ':' + base


def identity(isin, ric):
    if not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', isin):
        raise ValueError('Invalid ISIN')
    if not isinstance(ric, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.GTX', ric):
        raise ValueError('GETTEX RIC not confirmed')
    return dict(source=SOURCE, symbol=ric, venue='GETTEX', currency='EUR', timezone='Europe/Berlin')


class Client:
    """Same public signed-session flow as gettex.de; no account secrets stored."""
    def __init__(self):
        self.session = requests.Session()

    def request(self, method, path, **kwargs):
        response = self.session.request(method, path, timeout=20, **kwargs)
        response.raise_for_status()
        return response

    def fetch(self, isin, base, start, end):
        if base not in ('1D','1M') and base not in INTRADAY_MINUTES:
            raise ValueError('Unsupported GETTEX chart base')
        page = self.request('GET', 'https://www.gettex.de/aktie/' + isin + '/').text
        match = re.search(r'const samlRequest=`([^`]+)`', page)
        if not match:
            raise ValueError('Public GETTEX chart session unavailable')
        auth = self.request('POST', HOST + '/auth/api/v1/sessions/samllogin?fetchToken=true',
            data={'SAMLResponse': base64.b64encode(match[1].encode()).decode()}).json()
        if not isinstance(auth.get('token'), str):
            raise ValueError('Public chart authentication rejected')
        self.session.headers['jwt'] = auth['token']
        found = self.request('GET', HOST + '/rest/api/find/securities', params={
            'fids': 'x.RIC', 'search': isin, 'searchFor': 'ISIN', 'exchanges': 'GTX', 'isNF': 'false'}).json()
        if found.get('status') != 'OK' or len(found.get('data', [])) != 1:
            raise ValueError('Ambiguous GETTEX listing')
        ric = found['data'][0]['x.RIC']
        meta = identity(isin, ric)
        answer = self.request('GET', HOST + '/rest/api/quote/info', params={
            'rics': ric, 'fids': 'x._ISIN,x._CURRENCY,q._CURRENCY', 'feeliable': 'false'}).json()
        rows = answer.get('data', [])
        if answer.get('status') != 'OK' or len(rows) != 1 or rows[0].get('x._ISIN') != isin:
            raise ValueError('Provider ISIN not confirmed')
        currencies = [rows[0][k] for k in ('x._CURRENCY', 'q._CURRENCY') if rows[0].get(k)]
        if not currencies or any(c != 'EUR' for c in currencies):
            raise ValueError('Provider currency not confirmed EUR')
        params = dict(ric=ric, fromDate=start.strftime('%Y-%m-%dT%H:%M:%S'), toDate=end.strftime('%Y-%m-%dT%H:%M:%S'))
        if base in ('1D','1M'):
            # Daily endpoint consumes listing-local date labels, not UTC instants.
            # UTC 23:00 for Berlin midnight otherwise requests the prior day.
            params.update(fromDate=start.tz_convert('Europe/Berlin').date().isoformat(),
                          toDate=end.tz_convert('Europe/Berlin').date().isoformat())
            path = '/timeseries/historical'
            params.update(fids='_DATE_END,OPEN_PRC,HIGH_1,LOW_1,CLOSE_PRC,ACVOL_1', samples='M' if base=='1M' else 'D')
        else:
            path = '/timeseries/intraday'
            params.update(fids='TIME,OPEN,HIGH,LOW,CLOSE,VOLUME', interval=INTRADAY_MINUTES[base])
        payload = self.request('GET', HOST + '/rest/api' + path, params=params).json()
        return payload, meta


def decode(payload, meta, isin, base, start, end, now, *, include_forming=False):
    """Validate the response envelope and actual rows, without filling holes."""
    if (base != '1D' and base not in INTRADAY_MINUTES) or meta != identity(isin, meta.get('symbol')):
        raise ValueError('Unexpected chart identity/base')
    request = payload.get('request', {})
    if payload.get('status') != 'OK' or request.get('ric') != meta['symbol']:
        raise ValueError('Response listing/status mismatch')
    if base != '1D' and (request.get('interval') != INTRADAY_MINUTES[base] or request.get('intervalUnit') != 'm'):
        raise ValueError('Response interval mismatch')
    if base == '1D' and request.get('samples') != 'D':
        raise ValueError('Response daily interval mismatch')
    rows = payload.get('data')
    if not isinstance(rows, list) or len(rows) > 12000:
        raise ValueError('Unexpected response size')
    proof = dict(meta, isin=isin, status='CONFIRMED', verified_at=now.isoformat(),
                 evidence='gettex.de public chart: ISIN search restricted to GTX + EUR quote identity')
    attrs = dict(meta, timeframe=base, listing_proof=proof,
        provider='LSEG / financial.com — GETTEX website chart',
        timestamp_basis='native provider chart buckets; not post-trade execution timestamps',
        publication_delay_seconds=None, publication_delay_status='UNKNOWN', classify_absence=True)
    if base=='1D':attrs['historical_session_end_basis']='Before 2026: conservative end-of-local-day availability bound; verified session dates, intraday hours not inferred'
    if not rows:
        frame = pd.DataFrame(columns=FIELDS, index=pd.DatetimeIndex([])); frame.attrs = attrs
        return frame
    frame = pd.DataFrame(rows)
    if base == '1D':
        index = pd.DatetimeIndex(pd.to_datetime(frame['_DATE_END'], errors='raise')).normalize()
        if (index < start.tz_convert('Europe/Berlin').tz_localize(None).normalize()).any() or (index > end.tz_convert('Europe/Berlin').tz_localize(None).normalize()).any():
            raise ValueError('Daily dates outside requested interval')
        names = dict(zip(('OPEN_PRC', 'HIGH_1', 'LOW_1', 'CLOSE_PRC', 'ACVOL_1'), FIELDS))
    else:
        native = pd.to_datetime(pd.to_numeric(frame['TIME'], errors='raise'), unit='s', utc=True)
        if (native < start).any() or (native > end).any():
            raise ValueError('Provider timestamps outside requested interval')
        index = pd.DatetimeIndex(native).tz_convert(meta['timezone']).tz_localize(None).floor(str(INTRADAY_MINUTES[base])+'min')
        names = dict(zip(('OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME'), FIELDS))
    # Provider '-' / unavailable numeric cells are invalid observations, not
    # zeros. Route them through the existing DQ/partial-archive path so one
    # bad row cannot discard all genuine neighbouring OHLCV.
    frame = frame[list(names)].rename(columns=names).apply(pd.to_numeric, errors='coerce').astype(float); frame.index = index
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError('Duplicate/unordered provider candles')
    frame.attrs = attrs
    initial = quality.audit(frame, base, expected=meta, now=now, check_stale=False)
    if initial['state'] == 'INVALID':
        from partial_chart import split_archive
        safe=split_archive(frame,meta,now,base)
        if safe is None:raise ValueError('GETTEX chart INVALID: ' + '; '.join(initial['issues']))
        safe.attrs['archive_invalid']={**initial,'rejected_timestamps':[str(t) for t in frame.index.difference(safe.index)]}
        frame=safe
    # Receipt metadata only. Provisional OHLCV never enter the series table.
    frame.attrs['provisional_timestamps']=list(map(str,frame.index[endpoints(frame)>now]))
    # Do not persist forming bars, including the current daily bar.
    if not include_forming:frame = frame.loc[endpoints(frame) <= now].copy()
    report = quality.audit(frame, base, expected=meta, now=now, check_stale=False)
    if report['state'] == 'INVALID':
        raise ValueError('GETTEX chart INVALID: ' + '; '.join(report['issues']))
    frame.attrs['data_quality'] = frame.attrs.get('archive_invalid',report)
    return frame


def display_native(store,info,base,now):
    """Read the last verified response's provisional rows; never persist them.

    Closure is tested at RECEIPT time, not the current clock: an old partial
    response cannot become a confirmed candle merely by waiting.
    """
    frame=_load_native(store,info,base)
    saved=store.setting(state_key(info['isin'],base),{})
    if not confirmed_state(info,saved):return frame
    extras=[];observed={};seen=set()
    for receipt in (saved.get('live_receipt',{}),saved):
        try:
            if not receipt.get('receipt') or receipt.get('identity')!=saved['identity']:continue
            path=Path(receipt['receipt']).resolve()
            if path.parent!=(Path(store.directory)/'chart_cache'/info['isin']).resolve():continue
            if (str(path),receipt.get('sha256')) in seen:continue
            seen.add((str(path),receipt.get('sha256')))
            stat=path.stat()
            if stat.st_size>10_000_000:continue
            def read():
                payload=path.read_bytes()
                if hashlib.sha256(payload).hexdigest()!=receipt['sha256']:raise ValueError('Live receipt changed')
                received=pd.Timestamp(receipt['received_at'])
                raw=decode(json.loads(payload),receipt['identity'],info['isin'],base,
                    pd.Timestamp(receipt['requested_from']),pd.Timestamp(receipt['requested_to']),received,include_forming=True)
                return raw.loc[endpoints(raw)>received].copy()
            extra=store.read_cache.get(('live_receipt_v1',info['isin'],base,receipt['sha256'],stat.st_mtime_ns),read,scoped=True)
            extra=extra.loc[~extra.index.isin(frame.index)]
            if extra.empty:continue
            extras.append(extra);observed.update({str(t):receipt['received_at'] for t in extra.index})
        except (OSError,ValueError,KeyError,TypeError):continue
    if not extras:return frame
    extra=pd.concat(extras);extra=extra.loc[~extra.index.duplicated(keep='last')]
    attrs=frame.attrs.copy() if not frame.empty else extras[-1].attrs.copy()
    for k in ('source','venue','currency','symbol'):
        if k in frame:extra[k]=attrs.get(k)
    out=pd.concat([frame,extra]).sort_index();out.attrs=attrs
    out.attrs['provisional_children']=observed
    out.attrs['received_at']=max(observed.values())
    return out


def pending_receipt(saved,result,frame):
    """Keep one last provisional receipt across delayed/empty later replies."""
    keys=('receipt','sha256','identity','received_at','requested_from','requested_to')
    stamps=frame.attrs.get('provisional_timestamps',[])
    if stamps:return {**{k:result[k] for k in keys},'provisional_timestamps':stamps}
    prior=saved.get('live_receipt') or {k:saved.get(k) for k in keys}
    if not prior.get('receipt') or prior.get('identity')!=result.get('identity'):return None
    known=prior.get('provisional_timestamps',[])
    if known and all(pd.Timestamp(t) in frame.index for t in known):return None
    return prior


def confirmed_state(info,saved):
    meta = saved.get('identity', {})
    proof = saved.get('listing_proof', {})
    if saved.get('state') not in ('SAVED', 'UNCHANGED', 'PARTIAL') or proof.get('isin') != info['isin'] or proof.get('status') != 'CONFIRMED':
        return False
    try:
        return meta == identity(info['isin'], meta.get('symbol')) and all(proof.get(k) == meta[k] for k in quality.IDENTITY)
    except (ValueError,KeyError,TypeError):return False


def _load_native(store, info, base):
    saved = store.setting(state_key(info['isin'], base), {})
    if not confirmed_state(info,saved):return pd.DataFrame()
    meta=saved['identity'];proof=saved['listing_proof']
    frame = store.load_series(info['isin'], base, SOURCE, meta['symbol'], 'GETTEX', 'EUR')
    frame.attrs.update(meta, timeframe=base, listing_proof=proof, classify_absence=True,
        provider='LSEG / financial.com — GETTEX website chart',
        timestamp_basis='native provider chart buckets; not post-trade execution timestamps',
        publication_delay_seconds=None, publication_delay_status='UNKNOWN',
        received_at=saved.get('received_at'), archive_receipt=saved.get('receipt'))
    if base=='1D':frame.attrs['historical_session_end_basis']='Before 2026: conservative end-of-local-day availability bound; verified session dates, intraday hours not inferred'
    if saved.get('archive_invalid'):
        frame.attrs.update(archive_invalid=saved['archive_invalid'],data_quality=saved['archive_invalid'],
            partial_chart={'display_only':True,'excluded_rows':len(saved['archive_invalid']['rejected_timestamps']),
                           'raw_count':len(frame)+len(set(saved['archive_invalid']['rejected_timestamps']).difference(map(str,frame.index))),
                           'ranges':saved['archive_invalid'].get('ranges',[]),
                           'reason':'INVALID / PARTIAL: rejected source rows retained in raw receipt'})
    return frame


def derived_daily(store, info, now=None):
    """Existing session/provenance aggregation; raw 15m/5m remain in storage.

    Incomplete days keep bar_complete=False and DQ. Consumers already exclude
    them from closed analysis and project safe chart periods separately.
    """
    from analytics import resample
    now = scheduler.clock(now)
    def calculate():
        base = '15m'
        raw = _load_native(store, info, base)
        if raw.empty:
            base = '5m'; raw = _load_native(store, info, base)
        if raw.empty:return raw
        raw.attrs['data_quality'] = raw.attrs.get('archive_invalid') or quality.audit(raw, base, now=now)
        prior = store.setting(quality.key(info, raw.attrs['symbol'], base), {})
        if (prior.get('state') in ('INVALID', 'SOURCE_UNAVAILABLE', 'RECOVERING', 'SOURCE_GAP')
            and all(prior.get('identity', {}).get(k) == raw.attrs[k] for k in quality.IDENTITY)):
            raw.attrs['data_quality'] = prior
        daily = resample(raw, base, '1D', now=now)
        daily.attrs['daily_origin'] = 'CP: существующая агрегация сохранённых '+base+'; полнота всей сессии'
        return daily
    return store.read_cache.get(('gettex_chart_daily_v2', info['isin'], now.floor('min').isoformat()),
        calculate, scoped=True, inputs=store.read_cache.token_for(['instrument:'+info['isin']]))


def load(store, info, base, now=None):
    native = _load_native(store, info, base)
    # Never splice native daily observations and aggregates or overwrite either.
    if base != '1D' or not native.empty:return native
    return derived_daily(store, info, now)


def monthly_digest(frame):
    rows=[(str(t),*[float(r[k]) for k in FIELDS]) for t,r in frame.iterrows()]
    return hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()


def decode_monthly_warmup(payload,meta,isin,now,receipt_sha256):
    """Validate a separate native monthly receipt; never replace daily rows.

    Only the valid contiguous suffix is eligible for WARMUP. A bad earlier
    month is retained verbatim in the receipt and explicitly excluded. Numeric
    DQ, exact identity, monthly grid and all later holes remain mandatory.
    """
    req=payload.get('request',{})
    if not isinstance(receipt_sha256,str) or not re.fullmatch('[0-9a-f]{64}',receipt_sha256):raise ValueError('NATIVE_MONTHLY_RECEIPT')
    if meta!=identity(isin,meta.get('symbol')) or payload.get('status')!='OK' or req.get('ric')!=meta['symbol'] or req.get('samples')!='M':
        raise ValueError('NATIVE_MONTHLY_IDENTITY')
    rows=payload.get('data',[])
    if not rows or len(rows)>500:raise ValueError('NATIVE_MONTHLY_SIZE')
    raw=pd.DataFrame(rows);dates=pd.DatetimeIndex(pd.to_datetime(raw.pop('_DATE_END')))
    if not dates.is_monotonic_increasing or dates.has_duplicates:raise ValueError('NATIVE_MONTHLY_TIMESTAMPS')
    index=dates.to_period('M').to_timestamp()
    if index.has_duplicates:raise ValueError('NATIVE_MONTHLY_DUPLICATE_PERIOD')
    if (dates<pd.Timestamp(req['fromDate'])).any() or (dates>pd.Timestamp(req['toDate'])).any():raise ValueError('NATIVE_MONTHLY_RANGE')
    names=dict(zip(('OPEN_PRC','HIGH_1','LOW_1','CLOSE_PRC','ACVOL_1'),FIELDS))
    frame=raw[list(names)].rename(columns=names).apply(pd.to_numeric,errors='coerce').astype(float);frame.index=index
    import numpy as np
    bad=(~np.isfinite(frame).all(axis=1))|(frame[['open','high','low','close']]<=0).any(axis=1)|(frame.volume<0)
    bad|=(frame.high<frame[['open','close','low']].max(axis=1))|(frame.low>frame[['open','close','high']].min(axis=1))
    excluded=[str(t) for t in index[bad]]
    if bad.any():frame=frame.loc[frame.index>index[bad][-1]].copy()
    if len(frame)<35:raise ValueError('NATIVE_MONTHLY_WARMUP_TOO_SHORT')
    ends=(frame.index+pd.offsets.MonthBegin(1)).tz_localize(meta['timezone']).tz_convert('UTC')
    if (ends>pd.Timestamp(now)).any():raise ValueError('NATIVE_MONTHLY_NOT_CLOSED')
    proof=dict(version=1,identity=meta,samples='M',receipt_sha256=receipt_sha256,
        first=str(frame.index[0]),last=str(frame.index[-1]),excluded_invalid_months=excluded,
        availability='Conservative next calendar month start; historical intraday sessions are not inferred')
    frame['source']=SOURCE
    frame.attrs.update(meta,timeframe='1M',native_monthly=proof,bar_ends={str(t):v.isoformat() for t,v in zip(frame.index,ends)},
        listing_proof=dict(meta,isin=isin,status='CONFIRMED',verified_at=pd.Timestamp(now).isoformat()))
    report=quality.audit(frame,'1M',expected=meta,now=now,check_stale=False)
    if report['state']!='READY':raise ValueError('NATIVE_MONTHLY_DQ: '+str(report['issues']))
    frame.attrs['data_quality']=report
    return frame


def attach_monthly_receipt(store,isin,frame):
    state=store.setting(state_key(isin,'1M'),{})
    if not state.get('native_monthly'):return frame
    if (not confirmed_state({'isin':isin},state) or monthly_digest(frame)!=state.get('series_digest')
            or any(frame.attrs.get(k)!=state['identity'][k] for k in ('source','symbol','venue','currency'))):
        raise ValueError('NATIVE_MONTHLY_REVISION_UNCONFIRMED')
    frame.attrs.update(state['identity'],timeframe='1M',listing_proof=state['listing_proof'],native_monthly=state['native_monthly'],
        bar_ends={str(t):((t+pd.offsets.MonthBegin(1)).tz_localize('Europe/Berlin').tz_convert('UTC')).isoformat() for t in frame.index})
    return frame


def verified_demo_warmup(store,info,current,now):
    """Owner-approved older, genuine daily prefix in the separate demo context.

    Reuse the same loader, calendar, DQ and resampler. Never fill, repair or
    splice venues. A bad earlier segment is excluded only by choosing a later
    independently audited contiguous dependency window, never by dropping rows.
    """
    from cp_controls import initialization_policy_for,DEMO_ALL_INITIALIZATION,DEMO_ALL_POLICY
    from analytics import resample,bar_ends
    from calculation_window import exclude_verified_leading_partial,scope_archive_faults
    tf=current.attrs.get('timeframe');meta={k:current.attrs.get(k) for k in quality.IDENTITY}
    policy=initialization_policy_for(store,info,meta)
    if not (policy and policy['version']==DEMO_ALL_INITIALIZATION and tf in ('1W','1M')
            and not current.empty and current.attrs.get('analysis_scope')
            and current.attrs.get('data_quality',{}).get('state')=='READY'):return current
    def calculate():
        raw=load(store,info,'1D',now)
        if raw.empty or {k:raw.attrs.get(k) for k in quality.IDENTITY}!=meta:return current
        # Try complete calendar-year boundaries, oldest first. The full current
        # overlap and every older daily row must pass the existing quality gate.
        starts=[raw.index[0]]+[pd.Timestamp(year=y,month=1,day=1) for y in range(raw.index[0].year+1,current.index[0].year+1)]
        for start in starts:
            if start>=current.index[0]:break
            daily=raw.loc[raw.index>=start].copy();scope_archive_faults(daily,start)
            if daily.attrs.get('archive_invalid'):continue
            report=quality.audit(daily,'1D',expected=meta,now=now,check_stale=False)
            if report['state']!='READY':continue
            daily.attrs['data_quality']=report
            derived=resample(daily,'1D',tf,now)
            derived=derived.loc[bar_ends(derived)<=now].copy()
            derived=exclude_verified_leading_partial(derived,tf,now)
            if derived.empty or ('bar_complete' in derived and not derived.bar_complete.fillna(False).all()):continue
            checked=quality.audit(derived,tf,expected=meta,now=now)
            if checked['state']!='READY':continue
            overlap=derived.index.intersection(current.index)
            if len(overlap)<35 or not overlap.equals(current.index):continue
            if not derived.loc[overlap,list(FIELDS)].equals(current.loc[overlap,list(FIELDS)]):continue
            prefix=derived.loc[derived.index<current.index[0],list(FIELDS)]
            if prefix.empty:continue
            out=pd.concat([prefix,current[list(FIELDS)]]);out['source']=SOURCE;out.attrs=current.attrs.copy()
            out.attrs['initialization_policy']=policy
            out.attrs['bar_ends']={str(t):e.isoformat() for t,e in zip(derived.index,bar_ends(derived))}
            out.attrs['data_quality']=quality.audit(out,tf,expected=meta,now=now)
            if out.attrs['data_quality']['state']!='READY':continue
            through=bar_ends(derived)[-1].isoformat()
            out.attrs['warmup_dependencies']=[dict(isin=info['isin'],identity=meta,base_tf='1D',timeframe=tf,
                **{'from':str(daily.index[0]),'through':through})]
            out.attrs['warmup_context']=dict(source=SOURCE,venue='GETTEX',currency='EUR',first=str(prefix.index[0]),
                last=str(prefix.index[-1]),older_bars=len(prefix),analysis_bars=len(current),overlap_verified=len(overlap),
                label='Отдельный деморежим: проверенные старые GETTEX-свечи используются только для прогрева; окно анализа остаётся пятилетним')
            return out
        return current
    scopes=['instrument:'+info['isin'],'market:'+info['isin']+':1D','setting:'+state_key(info['isin'],'1D'),
        'setting:'+DEMO_ALL_POLICY,'setting:cp_manual_risk:'+info['isin']]
    return store.read_cache.get(('verified_demo_warmup_v1',info['isin'],tf,str(current.index[0]),str(current.index[-1]),str(now.date())),
        calculate,scoped=True,inputs=store.read_cache.token_for(scopes))


def monthly_warmup(store,info,current,now):
    """Approved memory-only prefix; live analysis remains the scoped daily view.

    Native months are used only before the complete verified daily archive.
    Newer months use the existing daily aggregation, including its provenance.
    Every current overlap and both input revisions remain mandatory.
    """
    from cp_controls import demo_initialization_policy,initialization_policy_for,DEMO_ALL_INITIALIZATION
    key='cp_manual_risk:'+info['isin'];store.read_cache.depend('setting:'+key)
    policy=store.setting(key,{}).get('initialization_policy')
    if current.attrs.get('approved_demo_warmup'):
        policy=initialization_policy_for(store,info,{k:current.attrs.get(k) for k in quality.IDENTITY})
    universal=bool(policy and policy.get('version')==DEMO_ALL_INITIALIZATION and policy.get('older_verified_warmup'))
    if not (demo_initialization_policy(policy,info['isin']) and (policy.get('older_monthly_warmup') is True or universal)):return current
    if current.attrs.get('timeframe')!='1M' or not current.attrs.get('analysis_scope') or current.empty:return current
    if {k:current.attrs.get(k) for k in quality.IDENTITY}!=policy['identity']:return current
    state=store.setting(state_key(info['isin'],'1M'),{})
    store.read_cache.depend('setting:'+state_key(info['isin'],'1M'));store.read_cache.depend('market:'+info['isin']+':1M')
    if universal and not state.get('native_monthly'):return current
    if not confirmed_state(info,state) or not state.get('native_monthly'):raise ValueError('NATIVE_MONTHLY_PROOF_MISSING')
    native=store.load_series(info['isin'],'1M',SOURCE,policy['identity']['symbol'],'GETTEX','EUR')
    if monthly_digest(native)!=state.get('series_digest'):raise ValueError('NATIVE_MONTHLY_REVISION_UNCONFIRMED')
    if current.attrs.get('data_quality',{}).get('state')!='READY':return current
    from analytics import resample,bar_ends
    from calculation_window import exclude_verified_leading_partial,scope_archive_faults
    daily=load(store,info,'1D',now)
    if daily.empty:raise ValueError('MONTHLY_DAILY_REFERENCE_UNCONFIRMED')
    # Native monthly bars already cover this prefix. Newly downloaded older
    # daily rows are not inputs to this existing warmup and must not replace it
    # or make its disjoint newer daily dependency fail.
    daily_start=native.index[-1]+pd.offsets.MonthBegin(1)
    daily=daily.loc[daily.index>=daily_start].copy()
    scope_archive_faults(daily,daily_start)
    if (daily.empty or {k:daily.attrs.get(k) for k in quality.IDENTITY}!=policy['identity']
            or daily.attrs.get('archive_invalid')
            or quality.audit(daily,'1D',expected=policy['identity'],now=now,check_stale=False)['state']!='READY'):
        raise ValueError('MONTHLY_DAILY_REFERENCE_UNCONFIRMED')
    daily.attrs['data_quality']=quality.audit(daily,'1D',expected=policy['identity'],now=now,check_stale=False)
    derived=resample(daily,'1D','1M',now)
    derived=derived.loc[bar_ends(derived)<=now].copy()
    derived=exclude_verified_leading_partial(derived,'1M',now)
    if derived.empty or native.index[-1]>=derived.index[0]:raise ValueError('MONTHLY_ORIGINS_OVERLAP')
    overlap=derived.index.intersection(current.index)
    if len(overlap)<35:raise ValueError('NATIVE_MONTHLY_OVERLAP_UNCONFIRMED')
    if not derived.loc[overlap,list(FIELDS)].equals(current.loc[overlap,list(FIELDS)]):raise ValueError('NATIVE_MONTHLY_OVERLAP_CHANGED')
    older_daily=derived.loc[derived.index<current.index[0],list(FIELDS)]
    prefix=pd.concat([native[list(FIELDS)],older_daily])
    if prefix.empty:return current
    out=pd.concat([prefix,current[list(FIELDS)]]);out['source']=SOURCE;out.attrs=current.attrs.copy()
    out.attrs['native_monthly']=state['native_monthly']
    ends={str(t):((t+pd.offsets.MonthBegin(1)).tz_localize('Europe/Berlin').tz_convert('UTC')).isoformat() for t in prefix.index}
    ends.update(current.attrs.get('bar_ends',{}));out.attrs['bar_ends']=ends
    out.attrs['data_quality']=quality.audit(out,'1M',expected=policy['identity'],now=now)
    if out.attrs['data_quality']['state']!='READY':raise ValueError('NATIVE_MONTHLY_COMBINED_DQ')
    out.attrs['warmup_context']=dict(source=SOURCE,venue='GETTEX',currency='EUR',native_months=len(native),daily_derived_months=len(older_daily),
        first=str(prefix.index[0]),last=str(prefix.index[-1]),analysis_bars=len(current),overlap_verified=len(overlap),
        receipt_sha256=state['native_monthly']['receipt_sha256'],excluded_invalid_months=state['native_monthly']['excluded_invalid_months'],
        label='Проверенные GETTEX месяцы до пятилетнего окна используются только для прогрева; текущие свечи не заменены')
    out.attrs['warmup_dependencies']=[dict(isin=info['isin'],identity=policy['identity'],base_tf='1M',timeframe='1M',
        **{'from':str(native.index[0]),'through':((native.index[-1]+pd.offsets.MonthBegin(1)).tz_localize('Europe/Berlin').tz_convert('UTC')).isoformat()}),
        dict(isin=info['isin'],identity=policy['identity'],base_tf='1D',timeframe='1M',
        **{'from':str(derived.index[0]),'through':((derived.index[-1]+pd.offsets.MonthBegin(1)).tz_localize('Europe/Berlin').tz_convert('UTC')).isoformat()})]
    return out


def prepare_daily(store, info, now=None):
    """Prepare on CP's existing worker; persist progress, not duplicate OHLCV."""
    now = scheduler.clock(now); isin = info['isin']; key = 'gettex_chart_daily:'+isin
    previous = store.setting(key, {})
    with store.read_cache.track_inputs(isolated=True) as token:
        state = store.setting(state_key(isin, '15m'), {})
        if not state.get('identity'):state = store.setting(state_key(isin, '5m'), {})
        if not state.get('identity'):return {'state':'NOT_LOADED'}
        boundary = scheduler.boundary(state['identity'], '1D', now, delay_seconds=0).get('close_at')
        if (previous.get('version') == 2 and previous.get('closed_session') == boundary
            and previous.get('input_revisions') and store.read_cache.current(previous['input_revisions'])):
            return dict(previous, skipped_unchanged=True)
        frame = derived_daily(store, info, now)
    if frame.empty:
        result = dict(state='SOURCE_UNAVAILABLE', count=0, complete_closed=0)
    else:
        complete = frame.bar_complete.fillna(False) & (endpoints(frame) <= now)
        result = dict(state=frame.attrs.get('data_quality', {}).get('state', 'UNKNOWN'), count=len(frame),
            complete_closed=int(complete.sum()), last_complete=str(frame.index[complete][-1]) if complete.any() else None,
            incomplete=[str(t) for t in frame.index[~complete]], identity={k:frame.attrs[k] for k in quality.IDENTITY},
            issues=frame.attrs.get('data_quality', {}).get('issues', []))
    result.update(version=2, base=frame.attrs.get('aggregation_base','15m'), target='1D', closed_session=boundary,
        checked_at=now.isoformat(), input_revisions=dict(token), storage='derived view; original '+frame.attrs.get('aggregation_base','15m')+' in SQLite')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current={s:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or [0])[0] for s in token}
        if current != token:raise ValueError('INPUT_CHANGED: daily preparation not published')
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key,json.dumps(result,ensure_ascii=False)))
    return result


def retain_deeper(info, tf, previous, previous_base, proposed, proposed_base, now=None):
    """Owner policy per requested TF, shared by CP and chart readers.

    Keep a longer confirmed EUR view; a fresher short series stays separate.
    This is a display/context choice, never an override of freshness or DQ.
    """
    from analytics import resample
    from eur_listings import confirmed_listing
    now=scheduler.clock(now)
    # GETTEX is the owner's primary venue. Depth preservation protects stored
    # archives and choices within that venue; it must not promote the reserve.
    if (not proposed.empty and proposed.attrs.get('venue')=='GETTEX' and
        confirmed_listing(info,proposed.attrs) and previous.attrs.get('venue')!='GETTEX'):
        return proposed,proposed_base
    if previous.empty or not confirmed_listing(info,previous.attrs):return proposed,proposed_base
    if previous.attrs.get('data_quality',{}).get('state') in ('INVALID','SOURCE_UNAVAILABLE','RECOVERING'):
        return proposed,proposed_base
    if quality.audit(previous,previous_base,now=now)['state']=='INVALID':return proposed,proposed_base
    def usable(raw,base):
        if raw.empty:return raw
        out=resample(raw,base,tf,now)
        if out.empty:return out
        valid=endpoints(out)<=now
        if 'bar_complete' in out:valid &= out.bar_complete.fillna(False).to_numpy()
        return out.loc[valid]
    try:
        old=usable(previous,previous_base);new=usable(proposed,proposed_base)
        keep=not old.empty and (new.empty or old.index.min()<new.index.min())
    except (ValueError,KeyError,TypeError):return proposed,proposed_base
    if not keep:return proposed,proposed_base
    # Live 5m/15m views cannot prefer a stale tail solely for an earlier first
    # date. Keep the complete longer archive in SQLite, and select the WHOLE
    # fresh confirmed same-venue EUR series; never append its tail to old prices.
    if (tf in ('5m','15m') and not new.empty and
        previous.attrs.get('venue')==proposed.attrs.get('venue')=='GETTEX' and
        confirmed_listing(info,proposed.attrs) and
        quality.audit(proposed,proposed_base,now=now)['state'] not in ('INVALID','SOURCE_UNAVAILABLE','RECOVERING')):
        from paper_account import POLICY
        max_age=pd.Timedelta(seconds=POLICY['max_bar_age_seconds'])
        old_age=now-endpoints(old).max();new_age=now-endpoints(new).max()
        if old_age>max_age and pd.Timedelta(0)<=new_age<=max_age:
            out=proposed.copy()
            out.attrs['source_selection']={**proposed.attrs.get('source_selection',{}),
                'policy':'proxy_extended_eur_v1','requested_mode':'proxy',
                'selected':'GETTEX_EUR' if out.attrs.get('source')=='gettex' else 'GETTEX_CHART_EUR',
                'history_policy':'fresh_view_preserve_complete_archives_v1','timeframe':tf,
                'reason':'Выбран свежий реальный ряд; более длинный архив сохранён отдельно',
                'separate_longer_source':{k:previous.attrs.get(k) for k in quality.IDENTITY},
                'separate_longer_from':str(old.index.min()),'fresh_close_at':endpoints(new).max().isoformat()}
            return out,proposed_base
    out=previous.copy();source=out.attrs.get('source')
    out.attrs['source_selection']={
        'policy':'proxy_extended_eur_v1','requested_mode':'proxy',
        'selected':'GETTEX_EUR' if source=='gettex' else 'GETTEX_CHART_EUR' if source==SOURCE else 'PROXY_EUR_ARCHIVE',
        'history_policy':'preserve_depth_per_tf_v1','timeframe':tf,
        'reason':'Сохранена более длинная история этого TF; свежесть и DQ проверяются отдельно',
        'preserved_from':str(old.index.min()),
        'separate_shorter_source':{k:proposed.attrs.get(k) for k in quality.IDENTITY},
        'separate_shorter_from':str(new.index.min()) if not new.empty else None}
    return out,previous_base


def candidate(store, info, tf, current, current_base, now=None,analysis_at=None):
    """Select a WHOLE deeper/fresher confirmed series, never splice providers."""
    now = scheduler.clock(now)
    base = '5m' if tf in ('5m', '10m') else '15m'
    frame = load(store, info, base)
    if frame.empty and base=='15m':
        base='5m';frame=load(store,info,base)
    if frame.empty:
        return current, current_base
    if analysis_at is not None:
        from calculation_window import scope_raw
        frame=scope_raw(frame,base,analysis_at)
    report = frame.attrs.get('archive_invalid') or quality.audit(frame, base, now=now)
    if report['state'] in ('INVALID', 'SOURCE_UNAVAILABLE'):
        return current, current_base
    if not current.empty and current.attrs.get('venue')=='GETTEX' and quality.audit(current,current_base,now=now)['state']!='INVALID':
        old = current.copy(deep=False); old.attrs = dict(current.attrs, timeframe=current_base)
        try:
            old_ends=endpoints(old);old_end=old_ends[old_ends<=now].max()
            new_end = endpoints(frame).max()
            if new_end < old_end or (len(frame) <= len(current) and new_end <= old_end):
                return current, current_base
        except (ValueError, KeyError):
            return current, current_base
        if tf not in ('5m','15m'):
            # More base rows alone do not prove a better requested TF. Missing
            # child intervals can make its last valid aggregate older.
            proposed=aggregate(frame,base,tf,now=now)
            previous=aggregate(current,current_base,tf,now=now)
            if proposed.empty:return current,current_base
            if not previous.empty:
                a=endpoints(proposed);b=endpoints(previous)
                a=a[a<=now];b=b[b<=now]
                if len(b) and (not len(a) or a.max()<b.max()):return current,current_base
    frame.attrs.update(data_quality=report, source_selection={
        'policy':'proxy_extended_eur_v1', 'requested_mode':'proxy', 'selected':'GETTEX_CHART_EUR',
        'session':'07:30–23:00 Europe/Berlin',
        'reason':'Отдельный архив GETTEX / LSEG; OHLCV другого поставщика не добавляются'})
    return retain_deeper(info,tf,current,current_base,frame,base,now)


def daily_candidate(store,info,tf,current,current_base,official,official_base,now=None,analysis_at=None):
    """Choose a whole confirmed GETTEX daily view; keep the reserve in SQLite.

    Reuse aggregation/DQ. No joins, writes, downloads or trading overrides.
    Native/deeper GETTEX daily archives need not be replaced by a shorter
    intraday-derived view. All views retain the original provider identity.
    """
    from analytics import resample
    from eur_listings import confirmed_listing
    from onvista_archive import available_daily
    now=scheduler.clock(now)
    options=[(current,current_base),(official,official_base),(load(store,info,'1D',now),'1D')]
    options.extend((f,'1D') for f in available_daily(store,info,now))
    choices=[]
    for raw,base in options:
        if raw.empty or raw.attrs.get('venue')!='GETTEX' or not confirmed_listing(info,raw.attrs):continue
        if analysis_at is not None:
            from calculation_window import scope_raw,scope_report
            raw=scope_raw(raw,base,analysis_at)
            if raw.empty:continue
        report=raw.attrs.get('archive_invalid') or quality.audit(raw,base,now=now)
        saved=store.setting(quality.key(info,raw.attrs['symbol'],base),{})
        if analysis_at is not None:saved=scope_report(saved,raw.attrs['analysis_scope']['start'])
        if saved.get('state') in ('INVALID','RECOVERING','SOURCE_UNAVAILABLE','SOURCE_GAP'):
            report=saved
        if raw.attrs.get('daily_origin') and raw.attrs.get('data_quality',{}).get('state') in ('INVALID','SOURCE_GAP','UNCONFIRMED'):
            report=raw.attrs['data_quality']
        if report['state'] in ('INVALID','RECOVERING','SOURCE_UNAVAILABLE'):continue
        try:
            raw=raw.copy();raw.attrs['data_quality']=report
            view=resample(raw,base,tf,now=now)
            closed=view.loc[endpoints(view)<=now]
            if 'bar_complete' in closed:closed=closed.loc[closed.bar_complete.fillna(False)]
        except (ValueError,KeyError,TypeError):continue
        if view.empty:continue
        # A blocked derived bucket stays blocked; no READY is manufactured.
        score=(bool(len(closed)),report['state'] in ('READY','STALE'),len(closed),len(view))
        choices.append((score,raw,base))
    if not choices:return current,current_base
    _,raw,base=max(choices,key=lambda x:x[0])
    return raw,base


def reserve_request(db,now,isin=None,base=None,recovery=False):
    """Shared by normal feed and bounded DQ recovery; caller holds write lock."""
    reservation=db.execute("SELECT value FROM settings WHERE key='gettex_chart_next_request'").fetchone()
    if reservation and now<pd.Timestamp(json.loads(reservation[0])):return False
    # Queue rank alone cannot reserve a shared HTTP budget. Two operational
    # requests, then one ordinary/archive request, reuse the same limiter.
    # A reservation is counted even on HTTP failure; retries stay bounded.
    due=scheduler.operational_chart_due(db,now)
    key='gettex_chart_request_rotation'
    row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    prior=json.loads(row[0]) if row else {}
    streak=prior.get('priority_streak',0)
    priority=not recovery and any((isin,base)==(i,b) for _,i,b in due)
    if due and streak<2 and not priority:return False
    preparing=scheduler.preparation_chart_due(db,now) if not due or streak>=2 else []
    preparation=not recovery and any((isin,base)==(i,b) for _,i,b in preparing)
    # Alternate the ordinary HTTP slot between the one pending candidate and
    # other archive work. Official-file jobs cannot consume or reset it.
    if preparing and prior.get('preparation_turn',True) and not preparation:return False
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('gettex_chart_next_request',json.dumps((now+pd.Timedelta(minutes=1)).isoformat())))
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(dict(
        priority_streak=min(2,streak+1) if priority else 0,at=now.isoformat(),isin=isin,base=base,priority=priority,
        preparation_turn=False if preparation else True if not priority else prior.get('preparation_turn',True)))))
    return True


def recovery_key(isin,meta,base):
    # Same scheduler/rate limiter, separate receipt for an old requested range.
    # An empty historical response must not delay acquisition of a new bar.
    return scheduler.unit_key(isin,meta,base).replace('cp_schedule:','cp_recovery_schedule:',1)


def recovery_wait(store,info,meta,base,now=None):
    now=scheduler.clock(now);future=[]
    for key in ('cp_source_retry:'+SOURCE,state_key(info['isin'],base),recovery_key(info['isin'],meta,base),'gettex_chart_next_request'):
        value=store.setting(key)
        retry=value.get('retry_after') if isinstance(value,dict) else value
        if retry and pd.Timestamp(retry)>now:future.append(pd.Timestamp(retry))
    if not future:
        with store.connect() as db:
            row=db.execute("SELECT value FROM settings WHERE key='gettex_chart_request_rotation'").fetchone()
            streak=json.loads(row[0]).get('priority_streak',0) if row else 0
            if streak<2 and scheduler.operational_chart_due(db,now):future.append(now+pd.Timedelta(seconds=30))
    return max(future).isoformat() if future else None


def recovery_download(store,info,meta,base,start,end,now=None,client=None):
    """Provider adapter for existing DQ.repair, which alone commits OHLCV.

    One requested session, same proof, reservation and source cooldown. A raw
    receipt is evidence, not a claim that an empty interval had no trades.
    """
    from eur_listings import stored_identity
    now=scheduler.clock(now);start=pd.Timestamp(start);end=pd.Timestamp(end)
    if (base not in BASES or start.tzinfo is None or end.tzinfo is None or
        not pd.Timedelta(0)<end-start<=pd.Timedelta(days=1) or end>now):
        raise ValueError('GETTEX_RECOVERY_RANGE_BLOCKED')
    unit=recovery_key(info['isin'],meta,base)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT payload FROM instruments WHERE isin=?',(info['isin'],)).fetchone()
        actual=json.loads(row[0]) if row else {}
        if not actual or not stored_identity(db,actual,meta,base):raise ValueError('GETTEX_RECOVERY_IDENTITY_BLOCKED')
        for key in ('cp_source_retry:'+SOURCE,state_key(info['isin'],base),unit):
            row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            value=json.loads(row[0]) if row else None
            retry=value.get('retry_after') if isinstance(value,dict) else value
            if retry and now<pd.Timestamp(retry):raise ValueError('GETTEX_RECOVERY_COOLDOWN')
        if not reserve_request(db,now,info['isin'],base,recovery=True):raise ValueError('GETTEX_RECOVERY_RATE_WAIT')
    plan={'key':unit,'planned_at':now.isoformat(),'reason':'BOUNDED_DQ_RECOVERY'}
    try:
        payload,returned=(client or Client()).fetch(info['isin'],base,start.tz_convert('UTC'),end.tz_convert('UTC'))
        if returned!={k:meta.get(k) for k in quality.IDENTITY}:raise ValueError('GETTEX_RECOVERY_IDENTITY_CHANGED')
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True).encode();sha=hashlib.sha256(raw).hexdigest()
        folder=Path(store.directory)/'chart_cache'/info['isin'];folder.mkdir(parents=True,exist_ok=True)
        receipt=folder/(base+'-'+sha[:16]+'.json')
        if receipt.exists() and hashlib.sha256(receipt.read_bytes()).hexdigest()!=sha:raise ValueError('Archive receipt hash collision')
        if not receipt.exists():receipt.write_bytes(raw)
        frame=decode(payload,returned,info['isin'],base,start,end,now)
        from delivery_observations import observe
        observe(store.directory,info['isin'],base,frame,received_at=now.isoformat(),receipt_kind='PROVIDER_CLIENT_RETURN')
        if frame.attrs.get('archive_invalid'):raise ValueError('GETTEX_RECOVERY_INVALID_RESPONSE')
        frame.attrs.update(archive_receipt=str(receipt),receipt_sha256=sha)
        scheduler.finish(store,info['isin'],returned,base,plan,now=now)
        return frame
    except Exception as error:
        from delivery_observations import outcome
        outcome(store.directory,info['isin'],base,meta,'ERROR',error)
        scheduler.finish(store,info['isin'],meta,base,plan,error,now)
        raise


def confirm_native_parents(store, info, base, now=None):
    """Retire derived lineage only for exact, later verified native rows.

    The receipt and original derived proof remain in the ordinary journal.
    This is an origin correction, not an OHLCV write or a DQ permission.
    """
    from history_service import parent_provenance_key
    from eur_listings import stored_identity
    now=scheduler.clock(now);isin=info['isin'];key=state_key(isin,base)
    saved=store.setting(key,{})
    if base not in BASES or not confirmed_state(info,saved):return {'confirmed':0}
    meta=saved['identity'];lineage=parent_provenance_key(isin,meta,base)
    with store.connect() as db:
        original=db.execute('SELECT value FROM settings WHERE key=?',(lineage,)).fetchone()
        if not original or not json.loads(original[0]):return {'confirmed':0}
        state=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        instrument=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
    if not state or json.loads(state[0])!=saved or not instrument:return {'confirmed':0}
    proofs=json.loads(original[0])
    try:
        path=Path(saved['receipt']).resolve()
        root=(Path(store.directory)/'chart_cache'/isin).resolve()
        if path.parent!=root or path.stat().st_size>8*1024*1024:raise ValueError('Receipt path/size')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=saved['sha256']:raise ValueError('Receipt hash')
        received=pd.Timestamp(saved['received_at'])
        if received>now:raise ValueError('Future receipt')
        frame=decode(json.loads(raw),meta,isin,base,pd.Timestamp(saved['requested_from']),
                     pd.Timestamp(saved['requested_to']),received)
        candidates={}
        for stamp,proof in proofs.items():
            t=pd.Timestamp(stamp)
            if t not in frame.index or pd.Timestamp(proof['detected_at'])>received:continue
            row=frame.loc[[t],list(FIELDS)].copy();row.attrs=dict(meta,timeframe=base)
            if quality.audit(row,base,meta,received,check_stale=False)['state']!='READY':continue
            candidates[stamp]=[float(row.iloc[0][k]) for k in FIELDS]
            if len(candidates)>=32:break
    except (OSError,KeyError,ValueError,TypeError) as error:
        return {'confirmed':0,'state':'UNCONFIRMED','reason':str(error)}
    confirmed=[]
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if (db.execute('SELECT value FROM settings WHERE key=?',(lineage,)).fetchone()!=original or
            db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()!=state or
            db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()!=instrument):
            raise ValueError('INPUT_CHANGED: native parent confirmation')
        actual=json.loads(instrument[0])
        if not stored_identity(db,actual,meta,base):raise ValueError('Native parent identity changed')
        for stamp,values in candidates.items():
            row=db.execute('SELECT open,high,low,close,volume FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? AND ts=?',
                (isin,meta['source'],meta['venue'],meta['currency'],meta['symbol'],base,stamp)).fetchone()
            if row is None or list(row)!=values:continue
            receipt=dict(isin=isin,identity=meta,base=base,ts=stamp,ohlcv=values,
                receipt=saved['receipt'],sha256=saved['sha256'],received_at=saved['received_at'],
                confirmed_at=now.isoformat(),previous_derived_proof=proofs.pop(stamp))
            db.execute('INSERT INTO journal(kind,payload) VALUES (?,?)',('cp_native_parent_confirmation',json.dumps(receipt)))
            confirmed.append(stamp)
        if confirmed:db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(lineage,json.dumps(proofs)))
    if confirmed:
        # Existing DQ writer rechecks the full archive; restrict recovery to the
        # now-confirmed row, never consume HTTP or repair other historical gaps.
        quality.repair(info,store,meta['symbol'],base,meta,None,now,max_ranges=1,
                       target_range=[confirmed[0],confirmed[0]],local_only=True)
        from cp_assessment import request
        request(store,isin)
    return {'confirmed':len(confirmed),'timestamps':confirmed,'ohlcv_changed':False}


def turn(info, store, now=None, client=None, preferred_tf=None):
    result = _fetch_turn(info, store, now, client, preferred_tf)
    if info.get('kind') != 'benchmark':
        try:
            result['native_parent_confirmation']={b:confirm_native_parents(store,info,b,now) for b in BASES}
            result['daily_aggregation'] = prepare_daily(store, info, now)
            result['daily_tail_recovery'] = recover_closed_daily_tail(info,store,result['daily_aggregation'],now)
        except (ValueError,KeyError,TypeError) as error:
            result['daily_aggregation'] = {'state':'BLOCKED', 'reason':str(error)}
    return result


def recover_closed_daily_tail(info,store,prepared,now=None):
    """A delayed native daily tail can use the existing verified-child writer.

    No network or accepted-gap policy here: every child must pass strict DQ.
    The provider receipt stays intact; recovered provenance is stored separately.
    """
    saved=store.setting(state_key(info['isin'],'1D'),{})
    if not confirmed_state(info,saved) or not prepared.get('last_complete'):return {'state':'NOT_AVAILABLE'}
    meta=saved['identity'];target=pd.Timestamp(prepared['last_complete'])
    with store.connect() as db:
        last=db.execute('SELECT MAX(ts) FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=?',
            (info['isin'],meta['source'],meta['venue'],meta['currency'],meta['symbol'],'1D')).fetchone()[0]
    if not last or pd.Timestamp(last)>=target:return {'state':'UNCHANGED'}
    # At most the last complete session. Never hide older missing daily bars.
    report=quality.repair(info,store,meta['symbol'],'1D',meta,None,now,max_ranges=1,
        target_range=(str(target),str(target)),include_stale_tail=True,local_only=True)
    return {'state':report['state'],'completed':report.get('recovery_completed_ranges',[]),
            'failed':report.get('recovery_failed_ranges',[]),'next_retry':report.get('next_retry')}


def _fetch_turn(info, store, now=None, client=None, preferred_tf=None):
    """One bounded source task per existing CP feed turn; restart-safe receipts."""
    if info.get('kind') == 'benchmark':
        return {'state':'NOT_APPLICABLE'}
    live_clock = now is None
    now = scheduler.clock(now); isin = info['isin']
    # The validator already supports older daily sessions. Use that same
    # calendar boundary, rather than a second, stale downloader-only year.
    daily_floor = str(quality.calendar('GETTEX', now.year, now.year, daily=True).schedule.index.min().date())
    retry = store.setting('cp_source_retry:' + SOURCE, None)
    if retry and now < pd.Timestamp(retry):
        return {'state':'SOURCE_COOLDOWN'}
    choices = []
    from cp_controls import DEMO_ALL_POLICY,DEMO_ALL_INITIALIZATION
    from paper_account import ACCOUNT
    authority=store.setting(DEMO_ALL_POLICY,{})
    native_monthly=bool(authority.get('approved') is True and authority.get('version')==DEMO_ALL_INITIALIZATION
        and authority.get('account')==ACCOUNT and authority.get('scope')=='registered_stocks' and authority.get('older_verified_warmup') is True
        and authority.get('native_monthly_warmup') is True and confirmed_state(info,store.setting(state_key(isin,'1D'),{})))
    bases=(*BASES,'1M') if native_monthly else BASES
    monthly_end=(now.tz_convert('Europe/Berlin').normalize()-pd.DateOffset(years=5)).replace(day=1)-pd.Timedelta(nanoseconds=1)
    for base in bases:
        saved = store.setting(state_key(isin, base), {})
        # A verified historical warmup prefix is immutable input, not a live
        # quote lane. Existing Apple proof must not be downloaded again.
        if base=='1M' and saved.get('native_monthly') and confirmed_state(info,saved) and saved.get('series_digest'):continue
        if saved.get('retry_after') and now < pd.Timestamp(saved['retry_after']):
            continue
        meta = saved.get('identity') or dict(source=SOURCE, symbol=isin, venue='GETTEX', currency='EUR', timezone='Europe/Berlin')
        plan = scheduler.decision(store, isin, meta, base, now)
        prefix_retry=saved.get('archive_prefix',{}).get('retry_after')
        if (base=='1D' and (saved.get('verified_daily_floor')!=daily_floor or
            (prefix_retry and now>=pd.Timestamp(prefix_retry))) and
            confirmed_state(info,saved) and (plan['due'] or plan.get('reason')=='UNCHANGED')):
            plan={**plan,'due':True,'reason':'VERIFIED_DAILY_CALENDAR_BACKFILL','archive_prefix':True}
        if plan['due']:
            choices.append((bool(saved.get('received_at')), saved.get('received_at',''), bases.index(base), base, saved, meta, plan))
    if not choices:
        return {'state':'NOT_DUE'}
    chosen=min(choices,key=lambda x:x[:3])
    if not preferred_tf:
        # A normal maintenance turn may fill one missing prefix. Foreground
        # fresh turns retain their existing bounded priority and source limits.
        chosen=next((x for x in choices if x[-1].get('archive_prefix')),chosen)
    focus_key='gettex_chart_focus:'+isin
    focus=store.setting(focus_key,{}) if preferred_tf else {}
    preferred='1m' if preferred_tf=='1m' else '5m' if preferred_tf in ('5m','10m') else '1D' if preferred_tf in ('1D','1W','1M') else '15m'
    # The fresh scheduler already reserved a specific due base and bounded
    # foreground/background fairness. Do not silently spend that reservation
    # on another TF. Recheck due/cooldown above; a stale reservation is a no-op.
    if info.get('_gettex_poll_only') and preferred_tf:
        chosen=next((x for x in choices if x[3]==preferred),None)
        if chosen is None:return {'state':'NOT_DUE','base':preferred}
    # Ordinary history turns retain the two-choice preference bound.
    elif preferred_tf and focus.get('streak',0)<2:
        chosen=next((x for x in choices if x[3]==preferred),chosen)
    _, _, _, base, saved, planning_meta, plan=chosen
    scopes = ['instrument:' + isin, 'market:' + isin + ':' + base]
    if base=='1M':scopes.append('setting:'+DEMO_ALL_POLICY)
    def revisions(db):
        return {s:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or [0])[0] for s in scopes}
    prefix_end=None
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        item = db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
        if not item:
            return {'state':'INSTRUMENT_REMOVED'}
        original = item[0]; token = revisions(db)
        if plan.get('archive_prefix'):
            first=db.execute('SELECT MIN(ts) FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=?',
                [isin,*[planning_meta[k] for k in ('source','venue','currency','symbol')],base]).fetchone()[0]
            if first:
                prefix_end=pd.Timestamp(first).normalize().tz_localize('Europe/Berlin')-pd.Timedelta(nanoseconds=1)
                if prefix_end.date()<pd.Timestamp(daily_floor).date():
                    # This archive already reaches the verified boundary (e.g.
                    # an earlier authorized import). No request or OHLCV write.
                    saved={**saved,'verified_daily_floor':daily_floor,
                        'archive_prefix':{'state':'ALREADY_PRESENT','checked_at':now.isoformat(),'floor':daily_floor}}
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(state_key(isin,base),json.dumps(saved,ensure_ascii=False)))
                    return {'state':'UNCHANGED','base':base,'added':0,'changed':0,'archive_prefix':saved['archive_prefix']}
        if not reserve_request(db,now,isin,base):
            return {'state':'RATE_WAIT'}
        # At most one five-request public chart job/minute across CP workers.
        if preferred_tf:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(focus_key,json.dumps({'streak':focus.get('streak',0)+1 if base==preferred and focus.get('streak',0)<2 else 0,'base':base})))
    start = now.normalize() - pd.Timedelta(days=1 if base=='1m' else 28 if base=='5m' else 90)
    if base in ('1D','1M'):
        # Extend only to independently verified daily calendar dates. Native
        # intraday before 2026 remains blocked (phased trading-hour extension).
        start = pd.Timestamp(daily_floor, tz='Europe/Berlin').tz_convert('UTC')
    if saved.get('last') and base!='1M' and not plan.get('archive_prefix') and (base!='1D' or saved.get('verified_daily_floor')==daily_floor):
        overlap = pd.Timedelta(days=1) if base=='1D' else pd.Timedelta(minutes=30)
        start = max(start, pd.Timestamp(saved['last']).tz_localize('Europe/Berlin').tz_convert('UTC')-overlap)
    end = prefix_end.tz_convert('UTC') if prefix_end is not None else now
    if base=='1M':
        end=monthly_end.tz_convert('UTC')
        if saved.get('last'):start=max(start,pd.Timestamp(saved['last']).tz_localize('Europe/Berlin').tz_convert('UTC'))
    result = dict(identity=planning_meta, base=base, requested_from=start.isoformat(), requested_to=end.isoformat())
    if saved.get('archive_prefix'):result['archive_prefix']=saved['archive_prefix']
    try:
        payload, meta = (client or Client()).fetch(isin, base, start, end)
        received = scheduler.clock() if live_clock else now
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        digest = hashlib.sha256(raw).hexdigest()
        folder = Path(store.directory)/'chart_cache'/isin; folder.mkdir(parents=True, exist_ok=True)
        receipt = folder/(base+'-'+digest[:16]+'.json')
        if receipt.exists() and hashlib.sha256(receipt.read_bytes()).hexdigest()!=digest:
            raise ValueError('Archive receipt hash collision')
        if not receipt.exists():
            receipt.write_bytes(raw) # provider payload only, never a session token
        if base=='1M':
            requested=payload.get('request',{})
            if (pd.Timestamp(requested.get('fromDate')).date()!=start.tz_convert('Europe/Berlin').date()
                    or pd.Timestamp(requested.get('toDate')).date()!=end.tz_convert('Europe/Berlin').date()):
                raise ValueError('NATIVE_MONTHLY_REQUEST_RANGE_MISMATCH')
        frame = decode_monthly_warmup(payload,meta,isin,received,digest) if base=='1M' else decode(payload, meta, isin, base, start, end, received)
        from delivery_observations import observe
        observe(store.directory,isin,base,frame,received_at=received.isoformat(),receipt_kind='PROVIDER_CLIENT_RETURN')
        result.update(identity=meta, listing_proof=frame.attrs['listing_proof'], planned_at=now.isoformat(),received_at=received.isoformat(),
                      receipt=str(receipt), sha256=digest, count=len(frame), added=0, changed=0)
        if base!='1M':
            pending=pending_receipt(saved,result,frame)
            if pending:result['live_receipt']=pending
        if base=='1M':result.update(native_monthly=frame.attrs['native_monthly'],series_digest=monthly_digest(frame),warmup_only=True)
        if frame.empty:
            if frame.attrs.get('provisional_timestamps'):
                # First real response can consist only of a forming bar.
                # Persist its evidence, never claim a closed market candle.
                result.update(state='UNCHANGED',quality=saved.get('quality','PENDING'),provisional_only=True)
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    item=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
                    if not item or item[0]!=original or revisions(db)!=token:raise ValueError('INPUT_CHANGED: live receipt not committed')
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(state_key(isin,base),json.dumps({**saved,**result},ensure_ascii=False)))
                scheduler.finish(store,isin,meta,base,{**plan,'key':scheduler.unit_key(isin,meta,base)},now=now)
                return result
            if prefix_end is not None:
                # A checked empty older range is not NO_TRADES or READY. Keep
                # the current tail, its DQ and scheduler watermark untouched.
                prefix=dict(state='UNCONFIRMED',reason='NO_EARLIER_ROWS_RETURNED; completeness unconfirmed',
                    checked_at=received.isoformat(),requested_from=start.isoformat(),requested_to=end.isoformat(),
                    receipt=str(receipt),sha256=digest,count=0,added=0,retry_after=(now+pd.Timedelta(days=30)).isoformat())
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    item=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
                    if not item or item[0]!=original or revisions(db)!=token:
                        raise ValueError('INPUT_CHANGED: chart prefix not committed')
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(state_key(isin,base),json.dumps(
                        {**saved,'verified_daily_floor':daily_floor,'archive_prefix':prefix},ensure_ascii=False)))
                return {'state':'SOURCE_UNAVAILABLE','base':base,'added':0,'changed':0,'archive_prefix':prefix}
            # Empty response is not NO_TRADES and cannot hide a saved archive.
            result.update(state='SOURCE_UNAVAILABLE',retry_after=(now+pd.Timedelta(hours=1)).isoformat())
            store.set_setting(state_key(isin,base),{**saved,'last_error':'SOURCE_UNAVAILABLE: empty response',
                'failed_at':now.isoformat(),'retry_after':result['retry_after']})
            scheduler.finish(store,isin,meta,base,{**plan,'key':scheduler.unit_key(isin,meta,base)},now=now)
            return result
        report = frame.attrs['data_quality']
        if base=='1D':result['verified_daily_floor']=daily_floor
        # Preserve unresolved rejected observations across incremental responses.
        # Only a newly validated candle at that exact timestamp resolves one.
        archive=frame.attrs.get('archive_invalid') or saved.get('archive_invalid')
        rejected=set(saved.get('archive_invalid',{}).get('rejected_timestamps',[]))
        rejected.difference_update(map(str,frame.index))
        rejected.update(frame.attrs.get('archive_invalid',{}).get('rejected_timestamps',[]))
        if rejected:
            result['archive_invalid']={**archive,'state':'INVALID','rejected_timestamps':sorted(rejected)}
            result['invalid_receipts']=list(dict.fromkeys(saved.get('invalid_receipts',[])+[str(receipt)]))
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
            if not item or item[0] != original or revisions(db) != token:
                raise ValueError('INPUT_CHANGED: chart import not committed')
            where = 'isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=?'
            args = [isin,meta['source'],meta['venue'],meta['currency'],meta['symbol'],base]
            for stamp, row in frame.iterrows():
                values = [float(row[k]) for k in FIELDS]; ts = str(stamp)
                old = db.execute('SELECT open,high,low,close,volume FROM series WHERE '+where+' AND ts=?',args+[ts]).fetchone()
                if old is None:
                    db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',args+[ts]+values);result['added']+=1
                elif list(old) != values:
                    before = revisions(db)[scopes[1]]
                    db.execute('UPDATE series SET open=?,high=?,low=?,close=?,volume=? WHERE '+where+' AND ts=?',values+args+[ts])
                    after = revisions(db)[scopes[1]]
                    db.execute('INSERT INTO candle_revision_history(detected_at,isin,identity,base_tf,ts,old_ohlcv,new_ohlcv,revision_before,revision_after) VALUES (?,?,?,?,?,?,?,?,?)',
                        (now.isoformat(),isin,json.dumps(meta,sort_keys=True),base,ts,json.dumps(dict(zip(FIELDS,old))),json.dumps(dict(zip(FIELDS,values))),before,after))
                    result['changed']+=1
            latest=max(pd.Timestamp(saved['last']),frame.index[-1]) if prefix_end is not None and saved.get('last') else frame.index[-1]
            result.update(state='SAVED' if result['added'] or result['changed'] else 'UNCHANGED', last=str(latest),
                          quality=report['state'], input_revisions_before=token, input_revisions_after=revisions(db))
            if prefix_end is not None:
                result['archive_prefix']=dict(state='SAVED',checked_at=received.isoformat(),
                    requested_from=start.isoformat(),requested_to=end.isoformat(),first=str(frame.index[0]),
                    last=str(frame.index[-1]),added=result['added'],count=len(frame),receipt=str(receipt),sha256=digest,
                    scope='validated returned rows only; not proof of archive completeness')
            if rejected:result.update(state='PARTIAL',quality='INVALID')
            if base=='1M':
                # Digest the whole retained prefix, not just an incremental reply.
                combined=db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE '+where+' ORDER BY ts',args).fetchall()
                joined=pd.DataFrame([v[1:] for v in combined],index=pd.DatetimeIndex([v[0] for v in combined]),columns=list(FIELDS))
                result['series_digest']=monthly_digest(joined)
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(state_key(isin,base),json.dumps(result,ensure_ascii=False)))
        scheduler.finish(store,isin,meta,base,{**plan,'key':scheduler.unit_key(isin,meta,base)},now=now)
        return result
    except Exception as error:
        # Keep previous accepted rows/identity and DQ. Error diagnostics do not
        from delivery_observations import outcome
        outcome(store.directory,isin,base,planning_meta,'ERROR',error)
        # turn a failed request into an empty or READY replacement archive.
        result = {**saved,'rejected_receipt':str(receipt) if 'receipt' in locals() else None,'last_error':type(error).__name__+': '+str(error).split('?')[0],
                  'failed_at':now.isoformat(),'retry_after':(now+(pd.Timedelta(days=1) if base=='1M' else pd.Timedelta(minutes=15))).isoformat()}
        store.set_setting(state_key(isin,base),result)
        scheduler.finish(store,isin,planning_meta,base,plan,error,now)
        return dict(state='ERROR',reason=result['last_error'],base=base)
