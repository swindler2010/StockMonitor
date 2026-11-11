"""Session-aligned OHLCV buckets and their actual closing instants."""
import pandas as pd
import numpy as np
import data_quality as quality
from functools import lru_cache

# 1m is an auxiliary recovery input only; LABELS remains the eleven UI TF.
MINUTES={'1m':1,'5m':5,'10m':10,'15m':15,'30m':30,'1H':60,'2H':120,'3H':180,'4H':240}
LABELS={'5m':'5м','10m':'10м','15m':'15м','30m':'30м','1H':'1ч','2H':'2ч','3H':'3ч','4H':'4ч','1D':'1д','1W':'1н','1M':'1мес'}

def base_for(tf,source='proxy'):
    if source=='gettex':return '5m' if tf in ('5m','10m') else '15m'
    if source=='gettex_chart':return '1D' if tf in ('1D','1W','1M') else '5m' if tf in ('5m','10m') else '15m'
    return '1D' if tf in ('1D','1W','1M') else '5m' if tf in ('5m','10m') else '1H' if tf in ('1H','2H','3H','4H') else '15m'

def schedule(meta,first,last):
    name=quality.CALENDARS.get(meta.get('venue'))
    if not name or quality.TIMEZONES[name]!=meta.get('timezone'):raise ValueError('Не подтверждён календарь/timezone площадки')
    first=pd.Timestamp(first);last=pd.Timestamp(last)
    daily=meta.get('timeframe') in ('1D','1W','1M')
    if name=='GETTEX' and not ((int(quality.GETTEX_DAILY_START[:4]) if daily else 2026)<=first.year<=last.year<=2026):raise ValueError('Календарь GETTEX для этого TF не подтверждён')
    return quality.calendar(name,first.year-1,last.year+1,daily=daily and name=='GETTEX').schedule.loc[str(first.date()):str(last.date())]

def endpoints(frame):
    tf=frame.attrs['timeframe'];zone=frame.attrs['timezone']
    if 'bar_ends' in frame.attrs:
        ends=frame.attrs['bar_ends']
        if isinstance(ends,dict):ends=[ends.get(str(stamp)) for stamp in frame.index]
        return pd.DatetimeIndex(pd.to_datetime(ends,utc=True))
    idx=frame.index.tz_convert(zone).tz_localize(None) if frame.index.tz is not None else frame.index
    first=idx.min().normalize()-pd.Timedelta(days=7)
    last=idx.max().normalize()+pd.Timedelta(days=40 if tf=='1M' else 7)
    if frame.attrs.get('venue')=='GETTEX' and (int(quality.GETTEX_DAILY_START[:4]) if tf in ('1D','1W','1M') else 2026)<=idx.min().year<=idx.max().year<=2026:
        # Calendar lookup padding is not an actual out-of-year candle. Keep
        # all real bars subject to the verified-year guard in schedule().
        first=max(first,pd.Timestamp(quality.GETTEX_DAILY_START if tf in ('1D','1W','1M') else '2026-01-01'));last=min(last,pd.Timestamp('2026-12-31'))
    sched=schedule(frame.attrs,first,last)
    if tf=='1D':
        # One calendar lookup for the entire daily archive. Slicing the schedule
        # once per bar made 20-year benchmark snapshots contend with Tk drawing.
        return pd.DatetimeIndex(pd.to_datetime(sched.close.reindex(idx.normalize()).to_numpy(),utc=True))
    if tf in ('1W','1M'):
        begin=idx.normalize() if tf=='1M' else idx.normalize()-pd.Timedelta(days=6)
        end=idx+pd.offsets.MonthBegin(1)-pd.Timedelta(days=1) if tf=='1M' else idx.normalize()
        positions=sched.index.searchsorted(end.normalize(),side='right')-1
        valid=positions>=0
        safe=np.maximum(positions,0)
        if not len(sched):return pd.DatetimeIndex([pd.NaT]*len(idx),tz='UTC')
        valid &= sched.index.take(safe)>=begin
        closes=pd.DatetimeIndex(sched.close).as_unit('ns').asi8[safe].copy()
        closes[~valid]=pd.NaT.value
        return pd.DatetimeIndex(pd.to_datetime(closes,utc=True))
    if tf in MINUTES:
        # Align the calendar once, rather than building a pandas row per candle.
        rows=sched.reindex(idx.normalize())
        valid=rows.close.notna().to_numpy()
        result=np.full(len(idx),pd.NaT.value,dtype='int64')
        if valid.any():
            starts=idx[valid].tz_localize(zone,ambiguous='raise',nonexistent='raise').tz_convert('UTC')
            ends=(starts+pd.Timedelta(minutes=MINUTES[tf])).as_unit('ns').asi8.copy()
            breaks=pd.DatetimeIndex(rows.break_start[valid]).as_unit('ns').asi8
            closes=pd.DatetimeIndex(rows.close[valid]).as_unit('ns').asi8
            before_break=(breaks!=pd.NaT.value)&(starts.as_unit('ns').asi8<breaks)
            ends[before_break]=np.minimum(ends[before_break],breaks[before_break])
            result[valid]=np.minimum(ends,closes)
        return pd.DatetimeIndex(pd.to_datetime(result,utc=True))
    result=[]
    for stamp in idx:
        if tf in MINUTES:
            row=sched.loc[stamp.normalize()] if stamp.normalize() in sched.index else None
            if row is None:result.append(pd.NaT);continue
            start=stamp.tz_localize(zone,ambiguous='raise',nonexistent='raise')
            end=start+pd.Timedelta(minutes=MINUTES[tf])
            if pd.notna(row.break_start) and start<row.break_start:end=min(end,row.break_start)
            result.append(min(end,row.close))
        else:
            end=stamp+pd.offsets.MonthBegin(1)-pd.Timedelta(days=1) if tf=='1M' else stamp.normalize()
            begin=stamp.normalize() if tf in ('1D','1M') else stamp.normalize()-pd.Timedelta(days=6)
            part=sched.loc[str(begin.date()):str(end.date())]
            result.append(part.close.iloc[-1] if len(part) else pd.NaT)
    return pd.DatetimeIndex(pd.to_datetime(result,utc=True))

@lru_cache(maxsize=4096)
def _session_buckets(identity,base,target,opening,closing,stamps,values,empty,exclude_gaps,archive_start,display_tail=False):
    """Immutable numeric kernel of the existing aggregator, cached per session.

    Keys contain actual child OHLCV and validated empty slots, not a TTL. DQ,
    calendar, freshness and revision acceptance remain outside this cache.
    """
    index=pd.DatetimeIndex(stamps);numeric=np.asarray(values,dtype=float)
    base_step=pd.Timedelta(minutes=MINUTES[base]);step=pd.Timedelta(minutes=MINUTES[target])
    records=[];excluded=[];gap_excluded=[];empty=pd.DatetimeIndex(empty)
    for start in pd.date_range(opening,closing,freq=step,inclusive='left'):
        end=min(start+step,closing);a=index.searchsorted(start);b=index.searchsorted(end)
        if a==b:continue
        actual=index[a:b];group=numeric[a:b]
        missing=pd.date_range(start,end,freq=base_step,inclusive='left').difference(actual)
        complete=not len(missing.difference(empty))
        live=display_tail and b==len(index)
        if not live and not complete and start<archive_start and actual[0]==archive_start:
            excluded.append(str(start));continue
        if exclude_gaps and not complete and not live:gap_excluded.append(str(start));continue
        records.append((start,end,float(group[0,0]),float(np.nanmax(group[:,1])),
                        float(np.nanmin(group[:,2])),float(group[-1,3]),float(np.nansum(group[:,4])),complete))
    return tuple(records),tuple(excluded),tuple(gap_excluded)


def covered_slots(meta,missing,base,now):
    """Exchange proof plus explicitly bound TV policy; never DQ NO_TRADES."""
    from cp_controls import tv_absent_slots,TV_PRICE_MODEL
    official=quality.covered_empty(meta,missing,base,now)
    risk=meta.get('accepted_gap_input',{});proof=meta.get('tv_accepted_absence',{})
    allowed=(meta.get('tv_display_projection') or (risk.get('approved')
        and risk.get('price_model')==TV_PRICE_MODEL and risk.get('base')==base
        and risk.get('grant_digest')==proof.get('grant_digest')))
    tv=tv_absent_slots(meta,missing,base,now) if allowed else pd.DatetimeIndex([])
    return official.union(tv),tv


def accepted_display_slots(meta,missing,target,now):
    """No warning band for a fully accepted empty period; no invented bar."""
    from cp_controls import tv_absent_slots
    missing=pd.DatetimeIndex(missing);proof=meta.get('tv_accepted_absence',{})
    base=proof.get('base');found=[]
    if not base or missing.empty:return pd.DatetimeIndex([])
    if base==target:return tv_absent_slots(meta,missing,base,now)
    if base not in quality.STEPS:return pd.DatetimeIndex([])
    # An upper-TF observation never certifies its lower-TF children.
    if target in MINUTES and (base not in MINUTES or MINUTES[base]>MINUTES[target]):return pd.DatetimeIndex([])
    labels=pd.DataFrame(index=missing);labels.attrs={**meta,'timeframe':target}
    labels.attrs.pop('bar_ends',None)
    try:
        for stamp,end in zip(missing,endpoints(labels)):
            if pd.isna(end) or end>now:continue
            start=stamp-pd.Timedelta(days=6) if target=='1W' else stamp
            last=end.tz_convert(meta['timezone']).tz_localize(None)-pd.Timedelta(nanoseconds=1)
            slots=quality.expected_index({**meta,'timeframe':base},base,start,last,now)
            covered,tv=covered_slots({**meta,'tv_display_projection':True},slots,base,now)
            if len(slots) and len(tv) and not len(slots.difference(covered)):found.append(stamp)
    except (ValueError,KeyError,TypeError):return pd.DatetimeIndex([])
    return pd.DatetimeIndex(found)


def aggregate(frame,base,target,now=None):
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    meta=frame.attrs.copy();zone=meta.get('timezone')
    raw_report=quality.audit(frame,base,now=now,
        check_stale=not bool(meta.get('closed_parent_input') and base in MINUTES))
    inherited=meta.get('data_quality',{})
    if quality.absence_report_resolved(inherited,frame,base,now):inherited={}
    # Availability/recovery is a separate gate, not a replacement for candle validation.
    if inherited.get('state')=='INVALID' and raw_report['state']!='INVALID':raw_report=inherited
    source_gap=inherited.get('state')=='SOURCE_GAP' and raw_report['state'] in ('READY','STALE','GAP')
    if raw_report['state']=='INVALID':
        out=frame.iloc[:0].copy();out.attrs['data_quality']=raw_report;return out
    # Pandas copies attrs on every slice/column access. Large DQ reports made
    # session aggregation repeatedly deep-copy the same metadata thousands of
    # times, also starving Tk when called by the feed threads. Preserve metadata
    # once and aggregate numeric views; never mutate the input frame.
    frame=pd.DataFrame(frame,copy=False)
    if frame.index.tz is not None:frame=frame.copy();frame.index=frame.index.tz_convert(zone).tz_localize(None)
    numeric=frame[['open','high','low','close','volume']].to_numpy()
    base_step=pd.Timedelta(minutes=MINUTES[base])
    records=[];indices=[];ends=[];excluded=[];gap_excluded=[];tv_used=[]
    try:sched=schedule(meta,frame.index.min(),frame.index.max())
    except Exception as error:
        out=frame.iloc[:0].copy();out.attrs['data_quality']={'state':'INVALID','issues':[str(error)]};return out
    for day,row in sched.iterrows():
        segments=[(row.open,row.close)] if pd.isna(row.break_start) else [(row.open,row.break_start),(row.break_end,row.close)]
        for opening,closing in segments:
            opening=opening.tz_convert(zone).tz_localize(None);closing=closing.tz_convert(zone).tz_localize(None)
            left=frame.index.searchsorted(opening);right=frame.index.searchsorted(closing)
            if left==right:continue
            stamps=frame.index[left:right]
            absent=pd.date_range(opening,closing,freq=base_step,inclusive='left').difference(stamps)
            empty,tv=covered_slots(meta,absent,base,now)
            tv_used.extend(map(str,tv))
            day_records,day_excluded,day_gaps=_session_buckets(tuple(meta.get(k) for k in quality.IDENTITY),
                base,target,opening,closing,tuple(stamps),tuple(map(tuple,numeric[left:right])),tuple(empty),
                source_gap or raw_report['state'] in ('GAP','UNCONFIRMED'),
                frame.index[0] if stamps[0]==frame.index[0] else opening,
                bool(meta.get('display_live_tail')) and stamps[-1]==frame.index[-1])
            excluded.extend(day_excluded);gap_excluded.extend(day_gaps)
            for start,end,o,h,l,c,v,complete in day_records:
                record=dict(open=o,high=h,low=l,close=c,volume=v,bar_complete=complete)
                if 'source' in frame:record['source']=meta['source']
                records.append(record);indices.append(start);ends.append(end.tz_localize(zone).tz_convert('UTC').isoformat())
    out=pd.DataFrame(records,index=pd.DatetimeIndex(indices))
    out.attrs={**meta,'timeframe':target,'aggregation_base':base,'alignment':'verified session open; breaks and close clipped','bar_ends':dict(zip(map(str,indices),ends)),'archive_boundary_excluded':excluded}
    if tv_used:out.attrs['tv_aggregation_used']=tv_used
    report=quality.audit(out,target,now=now)
    if raw_report['state']!='READY' and report['state']=='READY':
        report.update(state=raw_report['state'],issues=raw_report.get('issues',[]))
    if source_gap and report['state'] in ('READY','STALE','GAP'):
        report.update(state='SOURCE_GAP',source_ranges=inherited.get('ranges',[]),excluded_incomplete=gap_excluded)
        report['issues']=list(dict.fromkeys(report.get('issues',[])+inherited.get('issues',[])))
    if inherited.get('state') in ('RECOVERING','SOURCE_UNAVAILABLE') and report['state']!='INVALID':
        report['observed_state']=report['state']
        report['state']=inherited['state']
        report['upstream_quality']=dict(inherited)
        report['issues']=list(dict.fromkeys(report.get('issues',[])+inherited.get('issues',[])))
    if gap_excluded:
        report['excluded_incomplete']=gap_excluded
        report['source_ranges']=report.get('source_ranges') or raw_report.get('ranges',[])
    if excluded:report['warnings'].append('ARCHIVE_START_PARTIAL: исключено '+str(len(excluded))+' неполных начальных блоков')
    out.attrs['data_quality']=report
    return out


@lru_cache(maxsize=2048)
def _day_values(identity,day,stamps,values):
    # Same pandas arithmetic; only a changed child's own day is recalculated.
    f=pd.DataFrame(values,index=pd.DatetimeIndex(stamps),columns=quality.FIELDS)
    r=f.resample('1D').agg(dict(open='first',high='max',low='min',close='last',volume='sum')).iloc[0]
    return tuple(r[k] for k in quality.FIELDS)

def daily_ohlcv(frame):
    meta=frame.attrs;view=pd.DataFrame(frame,copy=False);records=[];index=[]
    for stamp,part in view.groupby(view.index.normalize()):
        records.append(_day_values(tuple(meta.get(k) for k in quality.IDENTITY),stamp,tuple(part.index),
                                  tuple(map(tuple,part[list(quality.FIELDS)].to_numpy()))));index.append(stamp)
    out=pd.DataFrame(records,index=pd.DatetimeIndex(index,name=frame.index.name),columns=quality.FIELDS)
    if 'source' in view:out['source']=meta.get('source')
    return out

@lru_cache(maxsize=4096)
def _period_grid(identity,base,start,end):
    meta=dict(zip(quality.IDENTITY,identity))
    return tuple(quality.expected_index(meta,base,start,end+pd.Timedelta(days=1)-pd.Timedelta(nanoseconds=1),
                    (end+pd.Timedelta(days=2)).tz_localize(meta['timezone']).tz_convert('UTC')))

def daily_provenance(daily, out, target, now=None, base='1D'):
    """Verify provenance of existing pandas OHLCV aggregation; do not fill bars."""
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    meta=daily.attrs.copy()
    inherited=meta.get('data_quality',{})
    try:
        raw=quality.audit(daily,base,expected={k:meta.get(k) for k in quality.IDENTITY},now=now,
            check_stale=not bool(meta.get('closed_parent_input') and base in MINUTES))
        first=out.index[0]-pd.Timedelta(days=6) if target=='1W' else out.index[0]
        last=out.index[-1] if target in ('1D','1W') else out.index[-1]+pd.offsets.MonthBegin(1)-pd.Timedelta(days=1)
        if meta.get('venue')=='GETTEX' and base=='1D' and int(quality.GETTEX_DAILY_START[:4])<=daily.index.min().year<=daily.index.max().year<=2026:
            # Calendar lookup padding is not an out-of-range observed candle.
            first=max(first,pd.Timestamp(quality.GETTEX_DAILY_START));last=min(last,pd.Timestamp('2026-12-31'))
        sched=schedule(meta,first,last)
        complete=[];ends={};tv_used=[]
        for stamp in out.index:
            start,end=((stamp,stamp) if target=='1D' else (stamp-pd.Timedelta(days=6),stamp) if target=='1W' else
                       (stamp,stamp+pd.offsets.MonthBegin(1)-pd.Timedelta(days=1)))
            sessions=sched.loc[str(start.date()):str(end.date())]
            # Only timestamps are needed. Slicing a DataFrame here deep-copies
            # the entire archive/evidence attrs once per week or month.
            actual=daily.index[(daily.index>=start)&(daily.index<end+pd.Timedelta(days=1))]
            if base=='1D':
                expected=pd.DatetimeIndex(sessions.index)
                covered,tv=covered_slots(meta,expected.difference(actual),base,now)
                tv_used.extend(map(str,tv))
                proven=not len(actual.difference(expected)) and not len(expected.difference(actual).difference(covered))
            else:
                # Check the WHOLE calendar bucket, not only first/last trades.
                # Future expected slots stay incomplete; they do not become bars.
                expected=pd.DatetimeIndex(_period_grid(tuple(meta.get(k) for k in quality.IDENTITY),base,start,end))
                missing=expected.difference(actual)
                covered,tv=covered_slots(meta,missing,base,now);tv_used.extend(map(str,tv))
                proven=(len(expected)>0 and not len(actual.difference(expected)) and
                    not len(missing.difference(covered)))
            complete.append(not sessions.empty and proven)
            ends[str(stamp)]=sessions.close.iloc[-1].isoformat() if not sessions.empty else None
        out['bar_complete']=complete
        out.attrs.update(aggregation_base=base,bar_ends=ends,alignment='verified calendar period coverage')
        if tv_used:out.attrs['tv_aggregation_used']=tv_used
        report=quality.audit(out,target,now=now)
        upstream=inherited if inherited.get('state') in ('INVALID','GAP','UNCONFIRMED','SOURCE_GAP','SOURCE_UNAVAILABLE','RECOVERING') else raw
        if raw.get('state')=='INVALID':upstream=raw
        if upstream.get('state')=='INVALID' or (upstream.get('state')!='READY' and report['state'] in ('READY','STALE')):
            report.update(state=upstream.get('state','UNCONFIRMED'),issues=upstream.get('issues',[]))
        out.attrs['data_quality']=report
        out.attrs['daily_provenance']={'base':base,'raw_state':raw['state'],
            'inherited_state':inherited.get('state',raw['state'])}
        # A leading retention edge is not a missing internal session. Keep it
        # visible in the archive, with proof for consumers of CLOSED periods.
        if (base=='1D' and target in ('1W','1M') and not complete[0]
                and raw['state'] in ('READY','STALE') and inherited.get('state','READY') in ('READY','STALE')):
            # Missing recent daily publications do not invalidate a proven
            # retention edge five years earlier. Keep STALE on the raw archive;
            # closed-period consumers must still audit their own actual inputs.
            beginning=out.index[0]-pd.Timedelta(days=6) if target=='1W' else out.index[0]
            ending=out.index[0] if target=='1W' else out.index[0]+pd.offsets.MonthBegin(1)-pd.Timedelta(days=1)
            expected=pd.DatetimeIndex(sched.loc[str(beginning.date()):str(ending.date())].index)
            actual=daily.index[daily.index<=ending]
            if len(actual) and actual.equals(expected[expected>=daily.index[0]]) and daily.index[0]>expected[0]:
                out.attrs['daily_provenance']['leading_partial']=str(out.index[0])
    except (ValueError,KeyError,TypeError,IndexError) as error:
        out.attrs['data_quality']={'state':'INVALID','issues':['DAILY_PROVENANCE: '+str(error)]}
