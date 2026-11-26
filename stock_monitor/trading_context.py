"""Independent closed-data context using the existing CORE/history/cache.

No downloads, UI state changes, write permissions or strategy substitution.
The PAPER executor pins the CORE profile; other profiles are calculation
contracts unless explicitly authorized for execution.
"""
import copy

from paper_account import EXECUTION_TF


def closed_period_end(meta, target, now):
    import pandas as pd
    from timeframes import schedule
    from gettex_sessions import VERIFIED_THROUGH
    local=now.tz_convert(meta['timezone']).tz_localize(None)
    week=target=='1W'
    period=local.to_period('W-FRI' if week else 'M')
    last=period.end_time.normalize()
    verified=pd.Timestamp(VERIFIED_THROUGH)
    if meta.get('venue')=='GETTEX' and local<=verified+pd.Timedelta(days=1)-pd.Timedelta(nanoseconds=1)<last:
        # A W-FRI label can extend beyond the confirmed trading year.
        last=verified
    sessions=schedule({**meta,'timeframe':'1D'},(period-2).start_time,last)
    ends=sessions.close.groupby(pd.Grouper(freq='W-FRI' if week else 'MS')).max()
    eligible=ends[ends<=now]
    return eligible.iloc[-1] if not eligible.empty else None


def closed_parent_input(frame, base, target, now):
    """Only children of closed parents belong to this closed CORE context.

    Validate the complete child window up to the required parent's close; the TF
    is still audited at real now, and execution has its own current quote.
    An unfinished newer parent cannot revoke an older accepted dependency.
    """
    import pandas as pd
    from candle_schedule import boundary
    from timeframes import MINUTES,endpoints
    if frame.empty:return frame,now
    if base=='1D' and target in ('1W','1M'):
        cutoff=closed_period_end(frame.attrs,target,now)
        if cutoff is None:return frame,now
    elif target in (*MINUTES,'1D'):
        due=boundary(frame.attrs,target,now,delay_seconds=0)
        if not due.get('close_at'):return frame,now
        cutoff=pd.Timestamp(due['close_at'])
    else:return frame,now
    raw=frame.copy();raw.attrs['timeframe']=base
    ends=endpoints(raw)
    # Keep malformed timestamps visible to DQ; never erase bad rows in order
    # to turn a source into READY. Only known unclosed children are omitted.
    out=raw.loc[ends.isna() | (ends<=cutoff)].copy()
    # Include the whole required child window, including an absent final
    # child. It is an ordinary classified gap, not freshness of a newer parent.
    # Evidence uses REAL now, so a review obtained after the parent closed is
    # usable. No rows are generated and the resulting target keeps real-time DQ.
    if base in MINUTES and not out.empty:
        span=out.attrs.get('intraday_span')
        first=min(pd.Timestamp(span[0]),out.index.min()) if span else out.index.min()
        last=cutoff.tz_convert(raw.attrs['timezone']).tz_localize(None)-pd.Timedelta(nanoseconds=1)
        out.attrs['intraday_span']=[str(first),str(last)]
    out.attrs['closed_parent_input']=dict(target=target,input_base=base,through=cutoff.isoformat(),
        evaluated_at=pd.Timestamp(now).isoformat(),excluded_forming_children=len(raw)-len(out))
    input_clock=cutoff if base=='1D' and target in ('1W','1M') else now
    return out,input_clock


def plan(signal_tf, trend_tf=None, execution_tf=(EXECUTION_TF,)):
    from timeframes import ANALYSIS_LABELS as LABELS
    from trading_engine import profile
    required=tuple(profile(signal_tf) if trend_tf is None else trend_tf)
    if (signal_tf not in LABELS or len(required)!=3 or len(set(required))!=3
        or any(t not in LABELS for t in required)
        or tuple(execution_tf)!=(EXECUTION_TF,)):
        raise ValueError('TRADING_TIMEFRAME_CONTRACT_INVALID')
    return dict(version='closed_core_context_v1',strategy='CORE:1',signal_tf=signal_tf,
        required_tf=list(required),execution_tf=list(execution_tf),
        analysis_tf=list(dict.fromkeys((signal_tf,*required,'1D'))),execution_authorized=False)


def calculate(store,values,identity,contract,position,now):
    from snapshot_worker import SnapshotContext,Value
    from desktop014 import Desktop014
    from calculation_window import _candidate_proof
    from cp_controls import manual_context
    from eur_listings import stored_identity
    from data_quality import IDENTITY
    from timeframes import base_for
    exact=plan(contract['signal_tf'],contract['required_tf'],contract['execution_tf'])
    if contract!=exact:raise ValueError('TRADING_TIMEFRAME_CONTRACT_CHANGED')
    if set(identity)!=set(IDENTITY) or identity.get('currency')!='EUR':
        raise ValueError('TRADING_CONTEXT_IDENTITY_UNCONFIRMED')
    info=values['watchlist'][0]
    with store.connect() as db:
        if not stored_identity(db,info,identity,base_for(contract['signal_tf'],identity['source'])):
            raise ValueError('TRADING_CONTEXT_LISTING_UNCONFIRMED')

    class ClosedContext(SnapshotContext):
        def user_state(self,instrument):
            state=copy.deepcopy(super().user_state(instrument))
            state['position']={'quantity':position.get('quantity','0')}
            return state

        def frame_for(self,instrument,tf):
            return self.closed_frame(instrument,
                self.pinned_frame(instrument,tf,identity),tf)

    context=ClosedContext();context.store=store
    context.closed_parent_inputs=True
    for key,value in values.items():
        setattr(context,key,Value(value) if key in ('symbol','timeframe','period') else value)
    # Selection is owned by this request, never the currently displayed chart.
    context.timeframe=Value(contract['signal_tf']);context.period=Value('Max')
    context.trading_required_tf=tuple(contract['required_tf'])
    context.closed_specs={};context.set_analysis_scope(now)
    engine=context.trading_analysis(info,analysis_only=True)
    if values.get('_paper_v1_long_term') is True and engine.get('status')=='READY':
        from paper_v1_long_term import context as long_term_context
        daily=context.frame_for(info,'1D')
        weekly=None if position else context.frame_for(info,'1W')
        engine['v1_long_term']=long_term_context(daily,weekly,identity,now,held=bool(position))
    proof=_candidate_proof(store,list(context.closed_specs.values()),store.read_cache.dependencies())
    from gettex_first import VERSION
    factual=all(engine.get('timeframe_context',{}).get(tf,{}).get('calculation_availability',{}).get('policy')==VERSION
        and engine.get('timeframe_context',{}).get(tf,{}).get('calculation_availability',{}).get('analysis_available')
        for tf in contract['required_tf'])
    manual=None if factual else manual_context(store,info['isin'],identity,contract['signal_tf'],proof)
    return dict(engine=engine,proof=proof,manual=manual,contract=contract)
