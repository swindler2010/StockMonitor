"""Read-only diagnostic archive adapter. Never a history/analytics input.

Only explicitly retained, hash-pinned EUR evidence is eligible. DQ owns all
exclusions; segment-local validity never replaces the archive's INVALID.
"""
import copy
import hashlib
import json
from pathlib import Path
import pandas as pd
import data_quality as quality

REGISTRY = Path(__file__).resolve().parents[1]/'history64-evidence/partial-chart-archives.json'

def token(path):
    st=Path(path).stat()
    return (str(Path(path).resolve()),st.st_size,st.st_mtime_ns)

def current(tokens):
    try:return all(token(p)==tuple(saved) for saved in tokens for p in [saved[0]])
    except OSError:return False

def split_archive(raw,meta,now=None,base='1D'):
    """Use existing DQ ranges, including absent intervals, as path breaks."""
    report=quality.audit(raw,base,meta,now)
    if report['state']!='INVALID' or not report.get('repairable'):return None
    cursor=raw.index.min();parts=[]
    for first,last in report.get('ranges',[]):
        a,b=pd.Timestamp(first),pd.Timestamp(last)
        if a<cursor or b<a:return None
        part=raw.loc[(raw.index>=cursor)&(raw.index<a)].copy()
        if not part.empty:parts.append(part)
        cursor=b+pd.Timedelta(nanoseconds=1)
    tail=raw.loc[raw.index>=cursor].copy()
    if not tail.empty:parts.append(tail)
    if not parts:return None
    for part in parts:
        if quality.audit(part,base,meta,now)['state'] not in ('READY','STALE'):return None
    frame=pd.concat(parts);frame.attrs=copy.deepcopy(raw.attrs)
    frame.attrs.update(timeframe=base,data_quality=copy.deepcopy(report),
        partial_chart={'break_before':[str(p.index[0]) for p in parts[1:]],
                       'segments':len(parts),'excluded_rows':len(raw)-len(frame),
                       'raw_count':len(raw),'ranges':copy.deepcopy(report['ranges']),
                       'display_only':True})
    return frame

def _display_reviews(store,info,frame,base):
    """Current exact receipts for warning bands, never an analytical waiver."""
    import cp_controls as cp
    from eur_listings import confirmed_listing
    if store is None or not info or not confirmed_listing(info,frame.attrs):return []
    isin=info['isin'];key='cp_manual_risk:'+isin
    cp.attach_tv_absence(store,frame,isin,base)
    frame.attrs['tv_display_projection']=True
    store.read_cache.depend('setting:'+cp.REVIEWED_PREFIX+isin)
    grant=store.setting(key,{})
    # Even a rejected receipt is a dependency: a later renewal must invalidate
    # the cached projection, just as revocation must restore its warning.
    for entry in grant.get('accepted',{}).values():
        if entry.get('actor') in (cp.TV_ACTOR,cp.REVIEWED_ACTOR):
            for scope in entry.get('input_revisions',{}):store.read_cache.depend(scope)
    with store.connect() as db:accepted=cp.effective_accepted(db,isin,grant)
    identity={k:frame.attrs.get(k) for k in cp.IDENTITY};ranges=[]
    for entry in accepted.values():
        p=entry.get('problem',{});e=entry.get('evidence',{})
        expected={cp.TV_ACTOR:'OWNER_ACCEPTED_TV_ABSENCE',cp.REVIEWED_ACTOR:'OWNER_ACCEPTED_SCOPED_DISCREPANCY'}.get(entry.get('actor'))
        if (expected and e.get('status')==expected and e.get('price_model')==cp.TV_PRICE_MODEL
                and p.get('isin')==isin and p.get('kind')=='archive' and p.get('tf')==base
                and p.get('identity')==identity and p.get('range')
                and identity.get('venue')=='GETTEX' and identity.get('currency')=='EUR'
                and p.get('state') in ('GAP','UNCONFIRMED','SOURCE_GAP')):
            ranges.append(p['range'])
    return ranges


def _accepted_breaks(out,safe,grid,breaks,base,target,approved,excluded,now):
    """Hide only warnings whose omitted periods contain no unaccepted damage.

    Keep path breaks and original DQ: accepted absence is not a real candle.
    This also handles calendar periods and overnight/session boundaries.
    """
    from cp_controls import _covered
    from timeframes import endpoints
    if not approved or not breaks:return []
    missing=grid.difference(out.index)
    if missing.empty:return []
    labels=pd.DataFrame(index=missing);labels.attrs={**out.attrs,'timeframe':target}
    labels.attrs.pop('bar_ends',None)
    ends=dict(zip(missing,endpoints(labels)));accepted=[]
    excluded=pd.DatetimeIndex(pd.to_datetime(excluded))
    for boundary in breaks:
        i=out.index.get_loc(pd.Timestamp(boundary))
        periods=missing[(missing>out.index[i-1])&(missing<out.index[i])]
        verified=bool(len(periods))
        for stamp in periods:
            end=ends[stamp]
            if pd.isna(end) or end>now:verified=False;break
            start=stamp-pd.Timedelta(days=6) if target=='1W' else stamp
            last=end.tz_convert(safe.attrs['timezone']).tz_localize(None)-pd.Timedelta(nanoseconds=1)
            slots=quality.expected_index(safe.attrs,base,start,last,now)
            absent=slots.difference(safe.index)
            unknown=absent.difference(quality.covered_empty(safe.attrs,absent,base,now))
            if (not len(absent) or len(slots.intersection(excluded))
                    or not _covered([(s,s) for s in unknown],approved)):
                verified=False;break
        if verified:accepted.append(boundary)
    return accepted


def project_saved(raw,base,target,now=None,store=None,info=None):
    """Project retained valid rows; parent INVALID never becomes analytical input."""
    from analytics import resample
    from timeframes import endpoints
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    if raw.empty or not raw.attrs.get('partial_chart'):return None
    original=copy.deepcopy(raw.attrs)
    excluded=original.get('archive_invalid',{}).get('rejected_timestamps',[])
    safe=raw.loc[~raw.index.isin(pd.to_datetime(excluded))].copy()
    safe.attrs={k:v for k,v in original.items() if k not in ('data_quality','partial_chart','archive_invalid')}
    report=quality.audit(safe,base,expected=original,now=now)
    if report['state'] in ('INVALID','SOURCE_UNAVAILABLE'):return None
    safe.attrs['data_quality']=report
    approved=_display_reviews(store,info,safe,base)
    # A rejected real row is damage, never evidence of absent trading, even
    # when a retained receipt happens to name the same timestamp.
    tv=safe.attrs.get('tv_accepted_absence')
    if tv and excluded:
        rejected=pd.DatetimeIndex(pd.to_datetime(excluded))
        tv['ranges']=[r for r in tv['ranges'] if not ((rejected>=pd.Timestamp(r[0]))&(rejected<=pd.Timestamp(r[1]))).any()]
    out=resample(safe,base,target,now)
    if out.empty:return None
    mask=endpoints(out)<=now
    if 'bar_complete' in out:mask &= out.bar_complete.fillna(False).to_numpy()
    out=out.loc[mask].copy()
    if out.empty:return None
    # The requested TF is already aggregated and carries its own provenance.
    # Daily metadata cannot prove a pre-2021 weekly/monthly projection.
    grid=quality.expected_index(out.attrs,target,out.index.min(),out.index.max(),now+pd.Timedelta(days=40))
    positions=grid.get_indexer(out.index)
    breaks=[str(out.index[i]) for i in range(1,len(out)) if positions[i]<0 or positions[i]!=positions[i-1]+1]
    accepted=_accepted_breaks(out,safe,grid,breaks,base,target,approved,excluded,now)
    # Both exact owner policies use actual bars only. Keep the genuine price
    # path gap, but do not restart a visual calculation for an accepted gap.
    # This is not a CORE permission or a reclassification to NO_TRADES.
    out.attrs.update(data_quality=original['data_quality'],archive_invalid=original.get('archive_invalid',{}),
        partial_chart={**original['partial_chart'],'display_only':True,'break_before':breaks,
                       'accepted_break_before':accepted,
                       'indicator_break_before':[x for x in breaks if x not in accepted],
                       'segments':len(breaks)+1,'complete_periods':len(out),'aggregation_base':base})
    return out

def complete_periods(daily,target,now=None):
    """Admit whole verified calendar buckets BEFORE existing OHLCV aggregation."""
    from analytics import TIMEFRAMES,resample
    from timeframes import schedule
    if target not in ('1W','1M'):raise ValueError('Diagnostic daily derivatives only')
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    if now.tzinfo is None:raise ValueError('UTC-aware verification time required')
    groups=list(daily.groupby(pd.Grouper(freq=TIMEFRAMES[target])))
    if not groups:return None
    def bounds(stamp):
        return ((stamp-pd.Timedelta(days=6),stamp) if target=='1W' else
                (stamp,stamp+pd.offsets.MonthBegin(1)-pd.Timedelta(days=1)))
    first,_=bounds(groups[0][0]);_,last=bounds(groups[-1][0])
    sched=schedule(daily.attrs,first,last)
    admitted=[];excluded=[]
    for stamp,part in groups:
        start,end=bounds(stamp);sessions=sched.loc[str(start.date()):str(end.date())]
        expected=pd.DatetimeIndex(sessions.index)
        # Exact equality excludes retention edges, gaps, malformed and missing
        # sessions. A forming month/week is never promoted to a closed candle.
        reason=None
        if sessions.empty:reason='NO_CONFIRMED_SESSIONS'
        elif not part.index.equals(expected):reason='INCOMPLETE_OR_DAMAGED_PERIOD'
        elif sessions.close.iloc[-1]>now:reason='FORMING_PERIOD'
        if reason:excluded.append({'period':str(stamp),'reason':reason});continue
        admitted.append(part)
    if not admitted:return None
    safe=pd.concat(admitted);safe.attrs=copy.deepcopy(daily.attrs)
    out=resample(safe,'1D',target,now)
    out['bar_complete']=True
    partial=out.attrs['partial_chart']
    labels=pd.date_range(groups[0][0],groups[-1][0],freq=TIMEFRAMES[target])
    positions=labels.get_indexer(out.index)
    breaks=[str(out.index[i]) for i in range(1,len(out)) if positions[i]!=positions[i-1]+1]
    partial.update(break_before=breaks,segments=len(breaks)+1,
                   aggregation_base='1D',excluded_periods=excluded,
                   complete_periods=len(out),verification_time=now.isoformat())
    # Root INVALID, raw exclusions, provenance and file guards survive unchanged.
    return out

def cached_periods(normal,tf,now=None):
    """Project verified whole daily-derived buckets for display only.

    Keep the original audit on the projection; never feed it back to CORE.
    Incomplete periods become visible path breaks, not fabricated candles.
    """
    if (tf not in ('1D','1W','1M') or normal.empty or
        not normal.attrs.get('aggregation_base') or 'bar_complete' not in normal):return None
    from analytics import bar_ends,TIMEFRAMES
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    proof=normal.attrs.get('daily_provenance',{})
    allowed=('READY','STALE','GAP','SOURCE_GAP','UNCONFIRMED')
    valid_proof=proof.get('raw_state') in allowed and proof.get('inherited_state') in allowed
    closed=bar_ends(normal)<=now
    mask=normal.bar_complete.fillna(False)&closed
    if not valid_proof:mask[:]=False
    # A forming calendar period is normal, not a damaged archive. Keep the
    # ordinary chart/analysis route, whose closed-bar gate excludes it. Actual
    # incomplete CLOSED periods and invalid upstream provenance still project.
    from cp_controls import analysis_quality_allowed
    accepted=analysis_quality_allowed(normal.attrs.get('data_quality',{}),normal.attrs.get('accepted_risk'))
    if (valid_proof and (normal.attrs.get('data_quality',{}).get('state') in ('READY','STALE') or accepted)
            and normal.bar_complete.fillna(False).loc[closed].all()):return None
    out=normal.loc[mask].copy();out.attrs=copy.deepcopy(normal.attrs)
    grid=pd.date_range(normal.index[0],normal.index[-1],freq=TIMEFRAMES[tf])
    from timeframes import accepted_display_slots
    grid=grid.difference(accepted_display_slots(out.attrs,grid.difference(out.index),tf,now))
    positions=grid.get_indexer(out.index)
    breaks=[str(out.index[i]) for i in range(1,len(out)) if positions[i]!=positions[i-1]+1]
    out.attrs['partial_chart']={'display_only':True,'break_before':breaks,
        'segments':len(breaks)+1 if len(out) else 0,'raw_count':len(normal),
        'excluded_rows':len(normal)-len(out),'complete_periods':len(out),
        'aggregation_base':normal.attrs['aggregation_base'],'verification_time':now.isoformat(),
        'reason':'Only complete, closed calendar periods; original DQ retained'}
    return out

def load(info,tf,normal,store,registry=REGISTRY):
    # Dynamic retained archives use the same projection/binding as pinned files.
    # A healthy normal source still wins; never substitute a pinned calculation.
    if normal.empty or normal.attrs.get('data_quality',{}).get('state')=='INVALID':
        import gettex_chart
        for base in (('5m',) if tf in ('5m','10m') else ('1D','15m') if tf in ('1D','1W','1M') else ('15m',)):
            raw=gettex_chart._load_native(store,info,base)
            if not raw.attrs.get('partial_chart'):continue
            if not normal.empty and any(normal.attrs.get(k)!=raw.attrs.get(k) for k in quality.IDENTITY):continue
            projected=project_saved(raw,base,tf,store=store,info=info)
            if projected is not None:return projected
    projected=cached_periods(normal,tf)
    if projected is not None:return projected
    # A valid normal chart always wins. No implicit change of a pinned source.
    if tf not in ('1D','1W','1M') or (not normal.empty and normal.attrs.get('data_quality',{}).get('state')!='INVALID'):
        return None
    if not info.get('eur_history_auto',bool(info.get('eur_listings'))):return None
    try:
        registry=Path(registry);rt=token(registry)
        entries=json.loads(registry.read_text(encoding='utf-8'))['archives']
        for entry in entries:
            if not entry.get('enabled') or entry['isin']!=info['isin']:continue
            meta=entry['identity']
            if meta.get('currency')!='EUR' or meta.get('source')!='proxy':continue
            if not any(x.get('status')=='CONFIRMED' and x.get('isin')==info['isin'] and
                       all(x.get(k)==meta.get(k) for k in quality.IDENTITY)
                       for x in info.get('eur_listings',[])):continue
            persisted=store.setting(quality.key(info,meta['symbol'],'1D'),{})
            if persisted.get('state') not in ('INVALID','SOURCE_UNAVAILABLE'):continue
            if any(persisted.get('identity',{}).get(k) not in (None,meta.get(k)) for k in quality.IDENTITY):continue
            path=(registry.parent/entry['file']).resolve()
            if path.parent!=registry.parent.resolve():continue
            ft=token(path)
            if ft[1]>10_000_000:continue
            payload=path.read_bytes()
            if hashlib.sha256(payload).hexdigest()!=entry['sha256']:continue
            data=json.loads(payload)
            if data['isin']!=info['isin'] or any(data['attrs'].get(k)!=meta.get(k) for k in quality.IDENTITY):continue
            raw=pd.DataFrame(data['ohlcv'],columns=list(quality.FIELDS),index=pd.to_datetime(data['timestamps']),dtype=float)
            raw.attrs=data['attrs']
            frame=split_archive(raw,meta)
            if frame is not None and tf!='1D':frame=complete_periods(frame,tf)
            if frame is None or not current([rt,ft]):continue
            frame.attrs['partial_chart']['files']=[rt,ft]
            frame.attrs['partial_chart']['persisted_quality']=copy.deepcopy(persisted)
            frame.attrs['isin']=info['isin']
            return frame
    except (OSError,ValueError,KeyError,TypeError):return None
    return None


def live_tail(info,target,chosen,store,now):
    """One display-only tail from real same-provider children, no SQLite writes.

    Keep the last observed values through a delay. Time passing never upgrades
    this view to a confirmed bar; only the ordinary history path can do that.
    """
    from analytics import resample,bar_ends,RANK
    from timeframes import MINUTES
    from eur_listings import confirmed_listing,venue_allowed
    import gettex_chart
    meta=chosen.attrs.copy();identity={k:meta.get(k) for k in quality.IDENTITY}
    if not confirmed_listing(info,meta) or not venue_allowed(info,meta):return chosen
    source=identity['source'];loaded={}
    def raw(base):
        if base in loaded:return loaded[base]
        if source=='gettex_chart':f=gettex_chart.display_native(store,info,base,now)
        elif source=='gettex':f=store.load_official(info['isin'],base)
        else:
            f=store.load_series(info['isin'],base,source,identity['symbol'],identity['venue'],identity['currency'])
            f.attrs.update(identity,listing_proof=meta.get('listing_proof'),timeframe=base)
        if f.empty or any(f.attrs.get(k)!=v for k,v in identity.items()):f=pd.DataFrame()
        loaded[base]=f;return f
    def project(f,base,tf):
        if f.empty:return None
        last=f.index[-1];day=last.normalize()
        start=day.to_period('W-FRI').start_time if tf=='1W' else day.replace(day=1) if tf=='1M' else day
        part=f.loc[f.index>=start].copy();part.attrs['timeframe']=base
        report=quality.audit(part,base,expected=identity,now=now,check_stale=False)
        if report['state']=='INVALID':return None
        part.attrs.update(data_quality=report,display_live_tail=True)
        view=resample(part,base,tf,now)
        if view.empty:return None
        stamp=view.index[-1];end=bar_ends(view)[-1]
        if pd.isna(end):return None
        begin=stamp-pd.Timedelta(days=6) if tf=='1W' else stamp
        used=part.loc[(part.index>=begin)&(part.index<end.tz_convert(identity['timezone']).tz_localize(None))]
        if used.empty:return None
        last=used.index[-1];child_ends=bar_ends(used)
        provisional=part.attrs.get('provisional_children',{})
        observed=child_ends[-1]
        if str(last) in provisional:observed=min(observed,pd.Timestamp(provisional[str(last)]))
        observed=min(observed,now)
        return dict(frame=view.tail(1).copy(),stamp=stamp,end=end,observed=observed,
            received_at=f.attrs.get('received_at'),base=base,child_count=len(used),
            child_first=str(used.index[0]),child_last=str(last),
            child_quality=report['state'],provisional_children=provisional)
    try:
        candidates=[]
        for base in ('1m','5m','15m','1H'):
            if RANK.get(base,0)>RANK[target]:continue
            candidate=project(raw(base),base,'1D' if target in ('1W','1M') else target)
            if candidate:candidates.append(candidate)
        if target in ('1D','1W','1M'):
            daily=raw('1D').copy()
            native=project(daily,'1D','1D')
            if native:candidates.append(native)
            if target in ('1W','1M') and candidates:
                day=max(candidates,key=lambda c:(c['stamp'],c['observed']));row=day['frame']
                # Closed daily provider candles win. A real intraday preview
                # adds only the missing/current day of the SAME identity.
                if (daily.empty or day['stamp'] not in daily.index or
                        day['stamp'] in pd.to_datetime(list(daily.attrs.get('provisional_children',{})))):
                    attrs=daily.attrs.copy() if not daily.empty else row.attrs.copy()
                    addition=row[list(quality.FIELDS)].copy()
                    for k in ('source','venue','currency','symbol'):
                        if k in daily:addition[k]=identity[k]
                    daily=pd.concat([daily.loc[daily.index!=day['stamp']],addition]).sort_index()
                    daily.attrs=attrs;daily.attrs.update(identity,timeframe='1D',received_at=day['received_at'])
                    daily.attrs.pop('bar_ends',None)
                    daily.attrs['provisional_children']={str(day['stamp']):day['observed'].isoformat()}
                aggregate=project(daily,'1D',target)
                candidates=[aggregate] if aggregate else []
        if not candidates:return chosen
        tail=max(candidates,key=lambda c:(c['stamp'],c['observed'],-RANK[c['base']]))
        stamp=tail['stamp']
        if not chosen.empty:
            if stamp<chosen.index[-1]:return chosen
            if stamp in chosen.index:
                complete=bool(pd.DataFrame(chosen,copy=False).loc[stamp].get('bar_complete',True))
                if complete and bar_ends(chosen.loc[[stamp]])[0]<=now:return chosen
        out=chosen.copy();ends=dict(zip(map(str,out.index),map(lambda x:x.isoformat(),bar_ends(out)))) if not out.empty else {}
        if 'bar_complete' not in out:out['bar_complete']=True
        last_values=pd.DataFrame(tail['frame'],copy=False).iloc[-1]
        for k in quality.FIELDS:out.loc[stamp,k]=last_values[k]
        for k in ('source','venue','currency','symbol'):
            if k in out:out.loc[stamp,k]=identity[k]
        out.loc[stamp,'bar_complete']=False;out=out.sort_index();out.attrs=meta
        ends[str(stamp)]=tail['end'].isoformat();out.attrs['bar_ends']=ends
        status='FORMING' if tail['end']>now else 'WAITING_DATA'
        out.attrs['live_candle']=dict(version=1,name='Живая свеча',display_only=True,
            timestamp=str(stamp),close_at=tail['end'].isoformat(),status=status,
            observed_through=tail['observed'].isoformat(),received_at=tail['received_at'],
            source_base=tail['base'],identity=identity,child_count=tail['child_count'],
            child_first=tail['child_first'],child_last=tail['child_last'],child_quality=tail['child_quality'],
            provider_latency='UNKNOWN',execution_allowed=False,
            analysis_mode='PRELIMINARY_ONLY',confirmed_signal=False,
            message='Формируется из полученных данных' if status=='FORMING' else 'Ожидаются данные; сохранено последнее полученное состояние')
        if not chosen.empty and stamp>chosen.index[-1]:
            grid=quality.expected_index(meta,target,chosen.index[-1],stamp,now+pd.Timedelta(days=40))
            from timeframes import accepted_display_slots
            absent=grid.difference(out.index)
            absent=absent.difference(accepted_display_slots(meta,absent,target,now))
            if len(absent):
                partial=copy.deepcopy(meta.get('partial_chart',{}))
                breaks=list(dict.fromkeys(partial.get('break_before',[])+[str(stamp)]))
                if 'indicator_break_before' in partial:
                    partial['indicator_break_before']=list(dict.fromkeys(partial['indicator_break_before']+[str(stamp)]))
                out.attrs['partial_chart']={**partial,'display_only':True,'break_before':breaks,'segments':len(breaks)+1}
        return out
    except (ValueError,KeyError,TypeError,IndexError):return chosen


def select_display(info,tf,normal,store,now=None):
    """Longest verified display projection, independent of execution inputs.

    Existing loaders, DQ and projection only; no joins, downloads or writes.
    The complete original quality report remains attached even to safe parts.
    """
    from analytics import resample,bar_ends,RANK
    from eur_listings import confirmed_listing,venue_allowed
    import gettex_chart
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    def project(raw,base):
        if raw.empty or not confirmed_listing(info,raw.attrs) or not venue_allowed(info,raw.attrs):return None
        try:
            raw=raw.copy();raw.attrs['timeframe']=base
            report=raw.attrs.get('archive_invalid') or quality.audit(raw,base,expected=raw.attrs,now=now)
            saved=store.setting(quality.key(info,raw.attrs['symbol'],base),{})
            if (saved.get('state') in ('INVALID','RECOVERING','SOURCE_UNAVAILABLE','SOURCE_GAP')
                    and all(saved.get('identity',{}).get(k)==raw.attrs.get(k) for k in quality.IDENTITY)):
                report=saved
            raw.attrs['data_quality']=report
            if report.get('state')=='INVALID' and not report.get('repairable'):return None
            if report.get('state') in ('RECOVERING','SOURCE_UNAVAILABLE') and not raw.attrs.get('partial_chart'):
                raw.attrs['partial_chart']={'display_only':True,'raw_count':len(raw),
                    'excluded_rows':0,
                    'reason':'Saved candles independently verified for display; source availability remains blocked'}
            if raw.attrs.get('partial_chart'):return project_saved(raw,base,tf,now,store,info)
            if report.get('state')=='INVALID':
                part=split_archive(raw,raw.attrs,now,base)
                # A persisted/global problem cannot be silently cleared by a
                # clean local audit. Only DQ-localized damage is projectable.
                if part is None:return None
                part.attrs['data_quality']=copy.deepcopy(report)
                return project_saved(part,base,tf,now,store,info)
            from cp_controls import accepted_gap_input,accepted_gap_output
            raw=accepted_gap_input(store,info,raw,base,now)
            approved=_display_reviews(store,info,raw,base)
            # Partial approval may restore DISPLAY of real parent OHLCV, but
            # analysis still needs its own complete, current risk receipt.
            raw.attrs['tv_display_projection']=True
            view=accepted_gap_output(resample(raw,base,tf,now),tf,now)
            if view.attrs.get('tv_aggregation_used') and not view.attrs.get('accepted_risk'):
                view.attrs['partial_chart']={'display_only':True,'raw_count':len(view),
                    'excluded_rows':0,'reason':'TV-matched absence accepted for display; analytical dependencies checked separately'}
            if view.empty:return None
            derived=cached_periods(view,tf,now)
            if derived is not None:return derived
            if 'bar_complete' in view and not view.bar_complete.fillna(False).all():
                mask=view.bar_complete.fillna(False)&(bar_ends(view)<=now)
                view=view.loc[mask].copy()
                view.attrs['partial_chart']={'display_only':True,'raw_count':len(mask),
                    'excluded_rows':int((~mask).sum()),'reason':'Only verified complete aggregates; original DQ retained'}
            if view.empty:return None
            grid=quality.expected_index(view.attrs,tf,view.index.min(),view.index.max(),now+pd.Timedelta(days=40))
            from timeframes import accepted_display_slots
            grid=grid.difference(accepted_display_slots(view.attrs,grid.difference(view.index),tf,now))
            positions=grid.get_indexer(view.index)
            breaks=[str(view.index[i]) for i in range(1,len(view)) if positions[i]<0 or positions[i]!=positions[i-1]+1]
            from cp_controls import analysis_quality_allowed
            accepted=analysis_quality_allowed(view.attrs.get('data_quality',{}),view.attrs.get('accepted_risk'))
            # An exact current owner-risk calculation is not a display-only
            # diagnostic. Preserve factual DQ/ranges and its explicit label;
            # otherwise this projection would suppress a separately valid CORE.
            if (breaks and not accepted) or view.attrs.get('partial_chart'):
                reviewed=_accepted_breaks(view,raw,grid,breaks,base,tf,approved,[],now)
                view.attrs['partial_chart']={**view.attrs.get('partial_chart',{}),'display_only':True,
                    'raw_count':view.attrs.get('partial_chart',{}).get('raw_count',len(view)),
                    'excluded_rows':view.attrs.get('partial_chart',{}).get('excluded_rows',0),
                    'break_before':breaks,'accepted_break_before':reviewed,
                    'indicator_break_before':[x for x in breaks if x not in reviewed],
                    'segments':len(breaks)+1,'reason':'Confirmed candles with visible missing intervals; original DQ retained'}
            return view
        except (ValueError,KeyError,TypeError):return None
    def calculate():
        existing=load(info,tf,normal,store)
        current=existing if existing is not None else normal
        choices=[];excluded=[]
        def add(view):
            if view is None or view.empty or not confirmed_listing(info,view.attrs) or not venue_allowed(info,view.attrs):return
            base=view.attrs.get('aggregation_base') or view.attrs.get('timeframe') or tf
            saved=store.setting(quality.key(info,view.attrs['symbol'],base),{})
            if all(saved.get('identity',{}).get(k)==view.attrs.get(k) for k in quality.IDENTITY):
                if saved.get('state') in ('RECOVERING','SOURCE_UNAVAILABLE') and not view.attrs.get('partial_chart'):return
                if saved.get('state')=='INVALID' and not saved.get('repairable'):return
            if view.attrs.get('data_quality',{}).get('state')=='INVALID' and not view.attrs.get('partial_chart'):return
            try:
                closed=bar_ends(view)<=now
                if 'bar_complete' in view:closed &= view.bar_complete.fillna(False).to_numpy()
                count=int(closed.sum())
                # Max is historical depth, not density. A dense recent series
                # must not displace verified older periods of the same venue.
                from cp_controls import analysis_quality_allowed
                reviewed=bool(view.attrs.get('accepted_risk') and analysis_quality_allowed(view.attrs.get('data_quality',{}),view.attrs['accepted_risk']))
                if count:choices.append(((view.attrs.get('venue')=='GETTEX',-view.index[closed][0].value,count,view.index[closed][-1].value,reviewed,bool(view.attrs.get('partial_chart'))),view))
            except (ValueError,KeyError,TypeError):pass
        add(current)
        def consider(loader,base,label):
            try:
                raw=loader()
                if raw.empty:return
                # A lower upper bound cannot beat an already verified display.
                # Count only distinct possible periods, never infer OHLCV/DQ.
                upper=len(raw)
                if tf in ('1D','1W','1M') and base!='1M':
                    index=raw.index.tz_localize(None) if raw.index.tz is not None else raw.index
                    upper=len(index.to_period({'1D':'D','1W':'W-FRI','1M':'M'}[tf]).unique())
                peers=[score for score,view in choices if view.attrs.get('venue')==raw.attrs.get('venue')]
                if peers:
                    best=max(peers)
                    # Conservative earliest label bound; incomplete boundary
                    # buckets and session-aligned labels are still audited.
                    earliest=raw.index.min().normalize()
                    if tf=='1W':earliest-=pd.Timedelta(days=7)
                    elif tf=='1M':earliest=earliest.replace(day=1)
                    first=-earliest.value
                    if first<best[1] or (first==best[1] and upper<best[2]):return
                add(project(raw,base))
            except (ValueError,KeyError,TypeError) as error:
                excluded.append({'source':label,'base':base,'reason':str(error)})
        bases=('5m',) if tf in ('5m','10m') else ('1D','15m','5m') if tf in ('1D','1W','1M') else ('15m','5m')
        for base in bases:
            consider(lambda:gettex_chart._load_native(store,info,base),base,'gettex_chart')
            consider(lambda:store.load_official(info['isin'],base),base,'gettex')
        if tf=='1M':
            monthly=store.setting(gettex_chart.state_key(info['isin'],'1M'),{})
            # This separate native archive ends BEFORE the analytical window.
            # It may seed the approved warmup, but cannot replace the live chart
            # merely because its first month is older than the daily projection.
            if monthly.get('warmup_only') or monthly.get('native_monthly'):
                excluded.append({'source':'gettex_chart','base':'1M','reason':'WARMUP_PREFIX_ONLY'})
            else:
                consider(lambda:gettex_chart._load_native(store,info,'1M'),'1M','gettex_chart')
        if tf in ('1D','1W','1M'):
            from onvista_archive import available_daily
            for raw in available_daily(store,info,now):consider(lambda:raw,'1D','onvista')
        # A reserve is considered only when no verified primary display exists.
        if not any(score[0] for score,_ in choices):
            for proof in info.get('eur_listings',[]):
                if proof.get('source')!='proxy' or not confirmed_listing(info,dict(proof,listing_proof=proof)):continue
                for base in (('1D',) if tf in ('1D','1W','1M') else ('5m','15m','1H')):
                    if RANK[base]>RANK[tf]:continue
                    raw=store.load_series(info['isin'],base,'proxy',proof['symbol'],proof['venue'],'EUR')
                    raw.attrs.update({k:proof.get(k) for k in quality.IDENTITY},listing_proof=proof)
                    add(project(raw,base))
        if not choices:
            current=current.iloc[:0].copy();current.attrs['partial_chart']={'display_only':True,'reason':'No proven display intervals; original DQ retained'}
            # First observed child need not wait for an entire parent period.
            # No historical rows are substituted; identity is still verified.
            templates=[current]
            for base in ('1m','5m','15m','1D'):
                if RANK[base]<=RANK[tf]:
                    raw=gettex_chart._load_native(store,info,base)
                    if confirmed_listing(info,raw.attrs):
                        template=raw.iloc[:0].copy();template.attrs.update(timeframe=tf,partial_chart=current.attrs['partial_chart'])
                        templates.append(template)
            for template in templates:
                live=live_tail(info,tf,template,store,now)
                if not live.empty:return live
            return current
        _,chosen=max(choices,key=lambda x:x[0])
        chosen=chosen.copy()
        # add() has matched the exact confirmed listing against this ISIN.
        # Legacy official/proxy archives may carry it only in their receipt.
        chosen.attrs['isin']=info['isin']
        chosen.attrs['source_selection']={**chosen.attrs.get('source_selection',{}),
            'policy':'proxy_extended_eur_v1','history_policy':'deepest_verified_display_per_tf_v2',
            'selected':'GETTEX_EUR' if chosen.attrs.get('venue')=='GETTEX' else 'PROXY_EUR_ARCHIVE',
            'timeframe':tf,'display_selection_only':True,
            'excluded_candidates':excluded,
            'reason':'Максимальная подтверждённая история этого TF; DQ и торговый контекст сохраняются отдельно',
            'previous_display':{'identity':{k:normal.attrs.get(k) for k in quality.IDENTITY},'count':len(normal)}}
        return live_tail(info,tf,chosen,store,now)
    if hasattr(store,'read_cache'):
        key=('verified_display_v3_live',info['isin'],tf,now.floor('min').isoformat(),
             info.get('history_venue_lock'),
             tuple(normal.attrs.get(k) for k in quality.IDENTITY),len(normal))
        return store.read_cache.get(key,calculate,scoped=True,inputs=store.read_cache.token_for(['instrument:'+info['isin']]))
    return calculate()

def display_levels(enriched,now=None):
    """Existing price extrema/pivot zones on a verified CLOSED visual segment.

    Historical levels remain dated. They never set analytical readiness, stops
    or execution permission, and do not borrow an unverified daily archive.
    """
    from analytics import bar_ends
    from significant_levels import zones
    if enriched.empty:
        return dict(display_levels_available=False,
                    levels_note='Уровни: нет проверенных свечей в выбранном контексте'),[]
    status=enriched.attrs.get('partial_indicators',{})
    if status.get('blocked_reason') or 'chart_segment' not in enriched:return {},[]
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    ends=bar_ends(enriched)
    values=pd.DataFrame(enriched,copy=False)
    mask=(ends<=now)&(values.chart_segment>0)
    if 'bar_complete' in values:mask &= values.bar_complete.fillna(False).to_numpy()
    closed=values.loc[mask]
    for segment in sorted(closed.chart_segment.unique(),reverse=True):
        part=closed.loc[closed.chart_segment==segment]
        if len(part)<21:continue
        recent=part.iloc[:-1].tail(20)
        end=ends[enriched.index.get_loc(part.index[-1])].tz_convert(enriched.attrs['timezone'])
        historical=segment!=values.chart_segment.iloc[-1]
        note=('Исторические уровни' if historical else 'Уровни закрытого участка')+' до '+end.strftime('%d.%m.%Y %H:%M')
        return dict(support=float(recent.low.min()),resistance=float(recent.high.max()),
            display_levels_available=True,display_levels_at=end.isoformat(),
            display_levels_historical=bool(historical),levels_note=note,
            display_levels_identity={k:enriched.attrs.get(k) for k in quality.IDENTITY}),zones(part)
    return dict(display_levels_available=False,levels_note='Уровни: нет участка из 21 достоверной закрытой свечи'),[]


def bind(prepared,frame,settings=None):
    """Segment-local visual indicators; no engine evaluation or permission."""
    from chart_indicators import partial_indicators
    enriched=partial_indicators(frame,settings)
    frame=frame.copy();frame.attrs=copy.deepcopy(enriched.attrs)
    reason=str(frame.attrs.get('data_quality',{}).get('state','UNKNOWN'))+' / PARTIAL: диагностические участки; торговые сигналы заблокированы'
    result=dict(ohlcv_valid=False,display_indicators_available=True,decision='WAIT',reason=reason,
                support=None,resistance=None,stop=None,add=None)
    levels,zones=display_levels(enriched);result.update(levels)
    engine=copy.deepcopy(prepared.get('trading_engine') or {})
    engine.update(signal='WAIT',status='DATA BLOCKED',confidence=None,score=None,
                  bullish_reasons=[],bearish_reasons=[],blockers=[reason])
    context={'state':'UNAVAILABLE','reason':reason,'benchmark_id':None}
    engine['benchmark_context']=context
    return {**prepared,'frame':frame,'analysis':(enriched,result),'zones':zones,
            'context':context,'trading_engine':engine}
