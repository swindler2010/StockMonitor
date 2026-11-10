"""Regular-session source rows only. Never invent prices, volume or timestamps."""
import numpy as np
import pandas as pd
import data_quality as quality

def normalize(frame,base,now=None,start=None,end=None,*,cached_quotes_only=False):
    if base not in ('1m','5m','15m','1H') or frame.empty:return frame.copy()
    if cached_quotes_only and frame.attrs.get('source')!='proxy':return frame.copy()
    meta=frame.attrs.copy();clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    def blocked(message):
        raise quality.QualityError({'state':'INVALID','issues':[message],'ranges':[]})
    if clock.tzinfo is None:blocked('INTRADAY: now без timezone')
    name=quality.CALENDARS.get(meta.get('venue'));zone=meta.get('timezone')
    if not name or quality.TIMEZONES[name]!=zone:blocked('INTRADAY: календарь/timezone площадки не подтверждены')
    if not all(meta.get(k) for k in quality.IDENTITY):blocked('INTRADAY: identity не подтверждена')
    for field in quality.IDENTITY:
        if field in frame and not frame[field].eq(meta[field]).all():blocked('INTRADAY: смешение identity '+field)
    try:
        idx=pd.DatetimeIndex(frame.index)
        if idx.hasnans:raise ValueError('NaT')
        if cached_quotes_only and idx.tz is not None and str(idx.tz)!=zone:raise ValueError('timezone индекса')
        absolute=idx.tz_localize(zone,ambiguous='raise',nonexistent='raise') if idx.tz is None else idx.tz_convert(zone)
        local=absolute.tz_localize(None)
    except Exception as error:blocked('INTRADAY timestamps/DST: '+str(error))
    out=frame.copy();out.index=local
    previous=meta.get('intraday_normalization',{})
    diagnostics={'input_rows':previous.get('input_rows',len(out)),
                 'excluded':dict(previous.get('excluded',{})),
                 'samples':list(previous.get('samples',[])),
                 'sorted':previous.get('sorted',False) or (not cached_quotes_only and not local.is_monotonic_increasing)}
    def exclude(mask,reason):
        nonlocal out
        mask=np.asarray(mask,dtype=bool)
        if not mask.any():return
        diagnostics['excluded'][reason]=diagnostics['excluded'].get(reason,0)+int(mask.sum())
        for stamp in out.index[mask][:max(0,40-len(diagnostics['samples']))]:
            diagnostics['samples'].append({'at':str(stamp),'reason':reason})
        out=out.iloc[np.flatnonzero(~mask)].copy()
    # Some historical responses append the latest quote outside the requested
    # interval. It must not extend coverage or create a false multi-year gap.
    for limit,is_start in ((start,True),(end,False)):
        if limit is None:continue
        limit=pd.Timestamp(limit)
        if limit.tzinfo is None:blocked('INTRADAY request boundary без timezone')
        boundary=limit.tz_convert(zone).tz_localize(None)
        exclude(out.index<boundary if is_start else out.index>=boundary,'outside_request')
    if out.empty:
        out.attrs={**meta,'intraday_normalization':diagnostics};return out
    if not cached_quotes_only:out=out.sort_index(kind='stable')
    first,last=out.index.min(),out.index.max()
    try:
        all_grid=quality.expected_index(meta,base,first,last,clock+pd.Timedelta(days=3))
        closed_grid=None if cached_quotes_only else quality.expected_index(meta,base,first,last,clock)
        cal=quality.calendar(name,first.year-1,last.year+1).schedule
    except Exception as error:blocked('INTRADAY session/calendar: '+str(error))
    # Preserve the observed span so removing a corrupt edge row cannot certify
    # a shorter archive as READY. DQ still sees every missing closed grid slot.
    span=[str(first),str(last)]
    if meta.get('intraday_span'):
        span=[str(min(first,pd.Timestamp(meta['intraday_span'][0]))),str(max(last,pd.Timestamp(meta['intraday_span'][1])))]
        if start is not None:span[0]=str(max(pd.Timestamp(span[0]),pd.Timestamp(start).tz_convert(zone).tz_localize(None)))
        if end is not None:span[1]=str(min(pd.Timestamp(span[1]),pd.Timestamp(end).tz_convert(zone).tz_localize(None)-pd.Timedelta(nanoseconds=1)))
    blockers=list(meta.get('intraday_blockers',[]));off=~out.index.isin(all_grid)
    if off.any():
        reasons=[]
        for stamp in out.index:
            if stamp in all_grid:reasons.append(None);continue
            row=cal.loc[stamp.normalize()] if stamp.normalize() in cal.index else None
            if row is None:reasons.append('outside_regular_session');continue
            at=stamp.tz_localize(zone,ambiguous='raise',nonexistent='raise')
            inside=False
            if row is not None:
                inside=row.open<=at<row.close
                if pd.notna(row.break_start) and row.break_start<=at<row.break_end:inside=False
            if not inside and not cached_quotes_only:reasons.append('outside_regular_session');continue
            values=out.loc[[stamp],list(quality.FIELDS)].apply(pd.to_numeric,errors='coerce')
            quote=(len(values)==1 and values.volume.iloc[0]==0 and
                   np.isfinite(values.iloc[0]).all() and values.iloc[0][['open','high','low','close']].nunique()==1)
            if cached_quotes_only:
                # Cached data may contain genuine bad bars: remove only established
                # quote markers, never repair/drop prices, duplicates or forming bars.
                quote=quote and values.open.iloc[0]>0
                reasons.append('non_candle_quote' if quote and inside else
                               'provider_close_quote' if quote and at==row.close else None)
                continue
            reasons.append('non_candle_quote' if quote else 'off_grid_source_row')
            if not quote:blockers.append('Неподтверждённое выравнивание source row: '+str(stamp))
        classified=list(zip(out.index,reasons))
        kinds=('non_candle_quote','provider_close_quote') if cached_quotes_only else ('outside_regular_session','non_candle_quote','off_grid_source_row')
        for reason in kinds:
            exclude(out.index.isin([stamp for stamp,kind in classified if kind==reason]),reason)
    if cached_quotes_only:
        diagnostics['accepted_rows']=len(out)
        out.attrs={**meta,'intraday_normalization':diagnostics,'intraday_span':span,
                   'intraday_blockers':blockers}
        return out
    # An identical duplicate is redundant; conflicting duplicates are a missing
    # source slot, not a reason to arbitrarily choose the first or last price.
    duplicate=out.index.duplicated(keep=False)
    conflicts=[]
    for stamp,group in out.loc[duplicate].groupby(level=0):
        if len(group[list(quality.FIELDS)].apply(pd.to_numeric,errors='coerce').drop_duplicates())>1:conflicts.append(stamp)
    exclude(out.index.isin(conflicts),'conflicting_duplicate')
    exclude(out.index.duplicated(keep='first'),'identical_duplicate')
    try:
        out[list(quality.FIELDS)]=out[list(quality.FIELDS)].apply(pd.to_numeric,errors='coerce')
        values=out[list(quality.FIELDS)]
        bad=(~np.isfinite(values).all(axis=1))|(values[['open','high','low','close']]<=0).any(axis=1)|(values.volume<0)
        bad|=(values.high<values[['open','low','close']].max(axis=1))|(values.low>values[['open','high','close']].min(axis=1))
    except Exception as error:blocked('INTRADAY OHLCV: '+str(error))
    exclude(bad,'invalid_ohlcv')
    exclude(~out.index.isin(closed_grid),'not_closed')
    diagnostics['accepted_rows']=len(out)
    out.attrs={**meta,'intraday_normalization':diagnostics,'intraday_span':span,
               'intraday_blockers':blockers}
    return out
