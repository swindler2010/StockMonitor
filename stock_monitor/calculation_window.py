"""Read-only, conservative execution context; never replaces archive quality."""
import math
import pandas as pd
from analytics import bar_ends, indicators
from timeframes import schedule
import data_quality

IDENTITY = ('source', 'symbol', 'venue', 'currency', 'timezone')
SCOPE_POLICY = 'rolling_five_calendar_years_v1'


def scope_report(report,start):
    """Only proven, localized older faults are outside the analytical scope."""
    if not report or not report.get('repairable') or not report.get('ranges'):return report
    try:
        def local(value):
            stamp=pd.Timestamp(value)
            return stamp.tz_convert((report.get('identity') or {}).get('timezone') or 'Europe/Berlin').tz_localize(None) if stamp.tzinfo else stamp
        if all(local(end)<pd.Timestamp(start) for _,end in report['ranges']):
            return dict(state='OUTSIDE_ANALYSIS_SCOPE',identity=report.get('identity'),base_tf=report.get('base_tf'),
                        analysis_scope_start=start,reason='All localized fault ranges predate the analytical scope')
    except (ValueError,TypeError):pass
    return report


def scope_raw(frame,base,now):
    """Memory-only raw view BEFORE aggregation. Max/archive stay untouched."""
    zone=frame.attrs.get('timezone') or 'Europe/Berlin'
    start=(pd.Timestamp(now).tz_convert(zone).normalize()-pd.DateOffset(years=5)).tz_localize(None)
    meta=frame.attrs.copy();index=frame.index
    if getattr(index,'tz',None) is not None:index=index.tz_convert(zone).tz_localize(None)
    out=frame.loc[index>=start].copy() if not frame.empty else frame.copy()
    out.attrs=meta
    out.attrs['analysis_scope']=dict(policy=SCOPE_POLICY,start=start.date().isoformat(),
        timezone=zone,years=5,label='Область анализа: последние 5 лет',
        older_faults='Outside current analysis; original archive and DQ evidence retained')
    if meta.get('data_quality'):
        out.attrs['archive_quality_before_scope']=meta.get('archive_quality_before_scope',meta['data_quality'])
        out.attrs['data_quality']=scope_report(meta['data_quality'],str(start))
    scope_archive_faults(out,start)
    scope_session_rejects(out,base,now)
    span=meta.get('intraday_span')
    if span and len(span)==2:
        out.attrs['intraday_span']=[str(max(start,pd.Timestamp(span[0]).tz_localize(None))),span[1]]
    return out


def scope_session_rejects(frame,base,now):
    """An excluded post-session record is not a missing in-session input.

    Keep original INVALID evidence. Re-audit every retained row/gap; this is
    neither NO_TRADES evidence nor permission to use a rejected observation.
    Misaligned timestamps INSIDE a session must remain blocking.
    """
    from timeframes import MINUTES
    report=frame.attrs.get('archive_invalid') or {}
    if (frame.empty or base not in MINUTES or frame.attrs.get('source')!='gettex_chart'
            or frame.attrs.get('venue')!='GETTEX' or frame.attrs.get('currency')!='EUR'
            or not report.get('repairable') or report.get('state')!='INVALID'
            or any(report.get('identity',{}).get(k)!=frame.attrs.get(k) for k in IDENTITY)):
        return
    issues=report.get('issues',[])
    if not issues or any(not (x=='Свечи вне сессии/неправильное выравнивание'
            or x.startswith(('Неподтверждённые пустые интервалы:', 'Пропущенные свечи:'))) for x in issues):return
    try:
        rejected=pd.DatetimeIndex(pd.to_datetime(report.get('rejected_timestamps',[])))
        if rejected.empty or rejected.tz is not None or rejected.hasnans or len(rejected.intersection(frame.index)):return
        sessions=schedule(frame.attrs,rejected.min().normalize(),rejected.max().normalize())
        for stamp in rejected:
            utc=stamp.tz_localize(frame.attrs['timezone']).tz_convert('UTC')
            if any(row.open<=utc<row.close for _,row in sessions.iterrows()):return
        current=data_quality.audit(frame,base,expected=frame.attrs,now=now)
        if current['state'] in ('INVALID','SOURCE_UNAVAILABLE'):return
    except (ValueError,TypeError,KeyError):return
    frame.attrs.setdefault('archive_quality_before_scope',report)
    frame.attrs['excluded_archive_faults']=dict(state='OUTSIDE_SESSION_INPUT',
        identity=report['identity'],base_tf=base,rejected_timestamps=list(map(str,rejected)),
        reason='Rejected records outside confirmed sessions; original receipt/INVALID retained; real gaps audited separately')
    frame.attrs.pop('archive_invalid',None)
    frame.attrs.pop('partial_chart',None)
    frame.attrs['data_quality']=current


def scope_archive_faults(frame,start):
    """Keep full-archive faults, but do not inherit proven out-of-input rejects.

    Memory-only metadata for an already sliced dependency view. Its actual rows
    still require the ordinary OHLCV/DQ/provenance audit before any permission.
    """
    report=frame.attrs.get('archive_invalid')
    if not report:return
    if any(report.get('identity',{}).get(k)!=frame.attrs.get(k) for k in IDENTITY):return
    scoped=scope_report(report,str(start))
    if scoped.get('state')!='OUTSIDE_ANALYSIS_SCOPE':return
    try:
        if any(pd.Timestamp(t)>=pd.Timestamp(start) for t in report.get('rejected_timestamps',[])):return
    except (ValueError,TypeError):return
    frame.attrs.setdefault('archive_quality_before_scope',report)
    frame.attrs['excluded_archive_faults']=scoped
    frame.attrs.pop('archive_invalid',None)
    frame.attrs.pop('partial_chart',None)


def scope_current(record,now):
    scope=record.get('analysis_scope') or {}
    if scope.get('policy')!=SCOPE_POLICY:return False
    try:return scope['start']==(pd.Timestamp(now).tz_convert(scope.get('timezone','Europe/Berlin')).normalize()-pd.DateOffset(years=5)).date().isoformat()
    except (ValueError,KeyError,TypeError):return False


def context_deadline(identity, tf, now):
    """First possible new closed bar, not an arbitrary longer permission TTL.

    Input revisions revoke immediately. Calendar/identity uncertainty keeps the
    old conservative one-minute bound. No provider delay is assumed here.
    """
    from timeframes import MINUTES
    fallback=now+pd.Timedelta(seconds=60)
    try:
        day=now.tz_convert(identity['timezone']).tz_localize(None).normalize()
        sessions=schedule(identity,day-pd.Timedelta(days=7),day+pd.Timedelta(days=40))
        if tf in ('1D','1W','1M'):
            ends=sessions.close if tf=='1D' else sessions.close.groupby(pd.Grouper(freq='W-FRI' if tf=='1W' else 'MS')).max()
        else:
            ends=[];step=pd.Timedelta(minutes=MINUTES[tf])
            for _,row in sessions.loc[str(day.date()):].iterrows():
                segments=[(row.open,row.close)] if pd.isna(row.break_start) else [(row.open,row.break_start),(row.break_end,row.close)]
                for opening,closing in segments:
                    ends.extend(min(start+step,closing) for start in pd.date_range(opening,closing,freq=step,inclusive='left'))
                if any(end>now for end in ends):break
        grace=pd.Timedelta(minutes=15 if tf in MINUTES else 30)
        future=[end for end in ends if pd.notna(end) and end>now]
        # DQ may still be waiting for a just-closed bar. Revalidate when its
        # existing publication grace ends, even if the next session is tomorrow.
        future += [end+grace for end in ends if pd.notna(end) and end<=now<end+grace]
        return min(future) if future else fallback
    except (KeyError,ValueError,TypeError):return fallback


def exclude_verified_leading_partial(frame,tf,now):
    """Existing retention-boundary proof, shared by both analytical callers."""
    proof=frame.attrs.get('daily_provenance',{});leading=proof.get('leading_partial')
    if (not frame.empty and leading and proof.get('raw_state') in ('READY','STALE')
            and proof.get('inherited_state') in ('READY','STALE') and leading==str(frame.index[0])
            and 'bar_complete' in frame and not bool(frame.bar_complete.iloc[0])):
        frame=frame.iloc[1:].copy()
        frame.attrs['excluded_leading_partial']=leading
        frame.attrs['data_quality']=data_quality.audit(frame,tf,expected={k:frame.attrs.get(k) for k in IDENTITY},now=now)
    return frame


def bounded_features(info, frame, tf, now):
    """Separate, fail-closed CORE context; never rewrites archive quality.

    Exact equality is intentionally required: no undocumented tolerance or
    assumption that 35/200 bars erases recursive indicator dependencies.
    """
    from analytics import _safe_features
    from cp_controls import analysis_quality_allowed, risk_summary
    result = dict(context='bounded_200_exact_reference_v1', timeframe=tf,
                  chart_ready=False, analysis_ready=False, blockers=[], count=0)
    try:
        meta = frame.attrs.copy()
        identity = {k: meta.get(k) for k in IDENTITY}
        result['identity'] = identity
        result['analysis_scope']=meta.get('analysis_scope')
        archive = meta.get('data_quality', {})
        risk=meta.get('accepted_risk')
        if risk:result['accepted_risk']=risk_summary(risk)
        archive_display=meta.get('archive_quality_before_scope',archive)
        result['archive_state'] = archive_display.get('state', 'UNKNOWN')
        result['archive_quality']={k:archive_display[k] for k in ('state','issues','ranges','missing_count','stale_range','stale_count','excluded_incomplete') if k in archive_display}
        result['provenance']=meta.get('daily_provenance',{})
        from performance_cache import frame_dependency
        result['_closed_input']=frame_dependency(info.get('isin'),frame,tf,now)
        # An empty analytical view is not evidence of damaged timestamps or
        # wrong identity. Saved rows may have been excluded by upstream DQ.
        # Keep that evidence above; no empty view can grant any permission.
        if frame.empty:
            raise ValueError('NO_USABLE_CANDLES')
        expected = info.get('expected_identity', {})
        if not all(identity.values()) or identity['currency'] != 'EUR' or meta.get('timeframe') != tf:
            raise ValueError('IDENTITY_UNCONFIRMED')
        if any(identity.get(k) != v for k, v in expected.items()):
            raise ValueError('IDENTITY_MISMATCH')
        from eur_listings import confirmed_listing
        confirmed = confirmed_listing(info,meta)
        if not confirmed:
            raise ValueError('LISTING_UNCONFIRMED')
        if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
            raise ValueError('TIMESTAMPS_INVALID')
        # Partial bars must not disappear in a way that conceals a damaged bucket.
        closed = frame.loc[bar_ends(frame) <= now].copy()
        if closed.empty:
            raise ValueError('NO_CLOSED_BARS')
        closed=exclude_verified_leading_partial(closed,tf,now)
        if closed.empty:raise ValueError('NO_CLOSED_BARS')
        leading=closed.attrs.get('excluded_leading_partial')
        scoped_archive=closed.attrs.get('data_quality') if leading else None
        if leading:result['excluded_leading_partial']=leading
        if 'bar_complete' in closed and not closed.bar_complete.fillna(False).all():
            raise ValueError('CLOSED_AGGREGATE_INCOMPLETE')
        window = closed.tail(200).copy()
        window.attrs = {**meta, 'intraday_span': [str(window.index[0]), str(window.index[-1])]}
        result.update(count=len(window), first=str(window.index[0]), last=str(window.index[-1]))
        # Derived rows require the existing upstream provenance check. A clean
        # aggregate alone cannot prove that omitted base candles never traded.
        effective_archive=scoped_archive or archive
        impact=None
        if effective_archive.get('state') not in ('READY','OUTSIDE_ANALYSIS_SCOPE'):
            from candle_impact import assess
            impact=assess(closed,tf,now)
            result['quality_impact']=impact
        if tf in ('10m','30m','2H','3H','4H','1W','1M') and (
                not meta.get('aggregation_base') or not analysis_quality_allowed(effective_archive,risk)):
            raise ValueError('DERIVED_PROVENANCE_NOT_PROVEN')
        if effective_archive.get('state') in ('INVALID','SOURCE_UNAVAILABLE','RECOVERING') and not (impact and impact['eligible']):
            raise ValueError('ARCHIVE_ERROR_SCOPE_NOT_PROVEN')
        report = data_quality.audit(window, tf, expected=identity, now=now)
        result['window_quality'] = report
        result['chart_ready'] = analysis_quality_allowed(report,risk)
        if not analysis_quality_allowed(report,risk):
            raise ValueError('WINDOW_' + report['state'])
        result['valid_until']=context_deadline(identity,tf,now).isoformat()
        window.attrs['data_quality'] = report
        calc = dict(info)
        if any(calc.get('levels_'+k) != identity[k] for k in ('source','symbol','venue','currency')):
            calc['levels'] = {}
        enriched, features = _safe_features(window, calc, now)
        if not features.get('sufficient_history') or not features.get('ohlcv_valid'):
            raise ValueError('INDICATOR_WARMUP_OR_OHLCV')
        # Audit every actual reference dependency, not the entire saved archive.
        # Missing evidence within this reference cannot be waived by convergence.
        reference_report = data_quality.audit(closed, tf, expected=identity, now=now)
        result['reference_quality']=reference_report
        if not analysis_quality_allowed(reference_report,risk):
            if impact and impact['eligible']:
                from candle_impact import verify
                if not verify(closed,enriched,features):
                    impact.update(status='BLOCKED',eligible=False,reason='EXACT_CORE_COMPARISON_FAILED')
                    raise ValueError('IMPACT_RECURSIVE_INITIALIZATION_SENSITIVE')
                impact.update(status='PROVEN_NO_CURRENT_EFFECT',reason='EXACT_TERMINAL_INDICATORS_AND_CORE_FACTS')
                result.update(analysis_ready=True,features=features,analytical_only=True,
                    reliability='Localized old volume fault excluded by finite dependencies and exact current CORE equality',
                    last_close_at=bar_ends(window)[-1].isoformat())
                return result
            raise ValueError('RECURSIVE_REFERENCE_' + reference_report['state'])
        closed.attrs = {**meta, 'data_quality': reference_report}
        reference, reference_features = _safe_features(closed, calc, now,prove_scope=True)
        if not reference_features.get('sufficient_history') or not reference_features.get('ohlcv_valid'):
            result['scope_initialization']=reference_features.get('scope_initialization')
            raise ValueError(reference_features.get('reason') or 'REFERENCE_WARMUP_OR_OHLCV')
        result['full_reference']={'context':'full_closed_reference_v1','analysis_ready':True,
            'count':len(closed),'first':str(closed.index[0]),'last':str(closed.index[-1]),
            'last_close_at':bar_ends(closed)[-1].isoformat(),
            'analysis_scope':meta.get('analysis_scope'),'scope_initialization':reference_features.get('scope_initialization'),
            'quality':reference_report,'features':reference_features}
        columns = ['ema10','ema30','rsi','macd','macd_signal','atr','volume_mean20']
        if (not enriched[columns].tail(2).equals(reference[columns].tail(2))
                or features.get('facts') != reference_features.get('facts')):
            raise ValueError('RECURSIVE_INITIALIZATION_SENSITIVE')
        result.update(analysis_ready=True, features=features,
                      reliability='Exact indicator/fact equality against audited actual reference inputs',
                      last_close_at=bar_ends(window)[-1].isoformat())
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        result['blockers'].append(str(error))
    return result


def benchmark_ready(market):
    return market.get('quality') == 'READY' and market.get('state') in ('OUTPERFORMING','NEUTRAL','UNDERPERFORMING')


def bounded_benchmark(context, info, state, daily, now, full_reference=False):
    """Same existing benchmark, bounded daily context; no substitute index.

    Benchmark has no recursive indicators. Its existing CORE vote must match
    the full-reference vote; stock recursive reliability is checked separately.
    """
    import benchmarks
    key=state.get('benchmark_id',benchmarks.default_benchmark(info))
    cfg=benchmarks.CATALOG.get(key)
    if not cfg: return {'state':'UNAVAILABLE','reason':'BENCHMARK_NOT_SELECTED'}
    context.store.read_cache.depend('instrument:'+key)
    raw=benchmarks.load(context.store,cfg)
    if getattr(context,'five_year_scope',False):raw=scope_raw(raw,'1D',now)
    from performance_cache import frame_dependency
    input_spec=frame_dependency(cfg['isin'],raw,'1D',now)
    deadline=context_deadline(raw.attrs,'1D',now).isoformat()
    reference=daily.loc[bar_ends(daily)<=now].copy()
    window=reference.tail(200).copy()
    if window.empty: return {'state':'UNAVAILABLE','reason':'BENCHMARK_STOCK_WINDOW_EMPTY'}
    if full_reference:
        full=benchmarks.relative_performance(reference,raw,'Max',now=now)
        return {**full,'benchmark_id':key,'benchmark_name':cfg['name'],'context':'full_closed_reference_v1','valid_until':deadline,'_closed_input':input_spec}
    bounded=benchmarks.relative_performance(window,raw,'Max',now=now)
    full=benchmarks.relative_performance(reference,raw,'Max',now=now)
    bounded.update(benchmark_id=key,benchmark_name=cfg['name'],context='bounded_200_exact_reference_v1',
                   reference_state=full.get('state'),valid_until=deadline,_closed_input=input_spec)
    if not benchmark_ready(bounded) or not benchmark_ready(full):
        return {**bounded,'state':'UNAVAILABLE','reason':'BENCHMARK_REFERENCE_UNCONFIRMED'}
    if bounded['state'] != full['state']:
        return {**bounded,'state':'UNAVAILABLE','reason':'BENCHMARK_WINDOW_VOTE_CHANGED'}
    bounded['reliability']='Same existing CORE benchmark vote as audited full daily reference'
    return bounded


def _input_tracking(context):
    from contextlib import nullcontext
    cache=getattr(getattr(context,'store',None),'read_cache',None)
    return cache.track_inputs() if cache else nullcontext({})


def _candidate_proof(store, specs, token):
    from performance_cache import capture_closed_inputs,InputRevisionChanged
    with store.connect() as db:
        db.execute('BEGIN')
        if any((db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0]!=v for k,v in token.items()):
            raise InputRevisionChanged('Candidate inputs changed before proof')
        return capture_closed_inputs(db,specs,token)


def _owner_analysis_candidate(context, info, tf, now):
    """Reuse the separately owner-approved CORE path and exact dependencies."""
    import copy
    from cp_controls import demo_initialization_policy,initialization_policy_for
    from snapshot_worker import Value
    store=getattr(context,'store',None)
    if store is None or tf in ('5m','10m'):return None
    policy=initialization_policy_for(store,info)
    if not demo_initialization_policy(policy,info['isin']) or tf not in policy.get('analysis_timeframes',[]):return None
    clone=copy.copy(context);clone.timeframe=Value(tf);clone.closed_specs={}
    with _input_tracking(clone) as token:
        engine=clone.trading_analysis(info,analysis_only=True)
    required=list(dict.fromkeys((tf,*__import__('trading_engine').profile(tf))))
    market=engine.get('benchmark_context',{});ready=engine.get('status')=='READY'
    deadlines=[context_deadline(policy['identity'],t,now) for t in required]
    market_identity=market.get('benchmark_identity')
    if market_identity:deadlines.append(context_deadline(market_identity,'1D',now))
    return dict(context=policy['version'],owner_reviewed_analysis=True,owner_initialization_semantics=3,
        analysis_ready=ready,signal_ready=bool(ready and benchmark_ready(market) and engine.get('signal') in ('BUY','SELL')),
        result=engine,blockers=engine.get('blockers',[]),signal_benchmark=market,
        identity=policy['identity'],closed_inputs=_candidate_proof(store,list(clone.closed_specs.values()),dict(token)),
        input_binding_version=2,expires_at=min(deadlines).isoformat(),completed_at=pd.Timestamp.now(tz='UTC').isoformat(),
        trade_ready=False,trade_blockers=['OWNER_REVIEWED_DEMO_CONTEXT; execution rechecked separately on 15m'])


def assess_all(context, info, now, benchmark=None):
    """Read-only automatic assessment in the existing snapshot worker scope."""
    from calculation_reuse import using
    with using(getattr(getattr(context,'store',None),'read_cache',None)):
        return _assess_all(context,info,now,benchmark)


def _assess_all(context, info, now, benchmark=None):
    from analytics import TIMEFRAMES
    from trading_engine import evaluate, profile, VERSION
    from paper_account import instrument_allowed
    if hasattr(context,'set_analysis_scope'):context.set_analysis_scope(now)
    state = dict(context.user_state(info)); state.update(isin=info['isin'], eur_listings=info.get('eur_listings', []))
    if context.source_mode(info) == 'gettex' and not info.get('eur_history_auto'):
        state['expected_identity'] = dict(source='gettex', symbol=info['isin'], venue='GETTEX', currency='EUR')
    else:
        state['expected_identity'] = {k: v for k, v in dict(currency='EUR', symbol=state.get('history_symbol'), venue=state.get('history_venue')).items() if v}
        if info.get('eur_history_auto'): state['expected_identity'] = {'currency':'EUR'}
    rows = {}
    ordered_tf=[t for t in TIMEFRAMES if t not in ('5m','10m')]+['5m','10m']
    for tf in ordered_tf:
        try:
            with _input_tracking(context) as token:
                frame=context.frame_for(info,tf)
                row_state=state
                if frame.attrs.get('source_selection',{}).get('policy')=='proxy_extended_eur_v1':
                    row_state={**state,'expected_identity':{k:frame.attrs.get(k) for k in IDENTITY}}
                rows[tf] = bounded_features(row_state, frame, tf, now)
            rows[tf]['_input_revisions']=dict(token)
        except (ValueError, KeyError, TypeError) as error:
            rows[tf] = dict(timeframe=tf, chart_ready=False, analysis_ready=False, blockers=[str(error)])
    benchmark_token={};full_benchmark_token={}
    if benchmark is None:
        benchmark={'state':'UNAVAILABLE','reason':'DAILY_CONTEXT_BLOCKED'}
        if rows.get('1D',{}).get('analysis_ready'):
            with _input_tracking(context) as benchmark_token:
                benchmark=bounded_benchmark(context,info,state,context.frame_for(info,'1D'),now)
    full_benchmark={'state':'UNAVAILABLE','reason':'FULL_DAILY_CONTEXT_BLOCKED'}
    if rows.get('1D',{}).get('full_reference',{}).get('analysis_ready'):
        with _input_tracking(context) as full_benchmark_token:
            full_benchmark=bounded_benchmark(context,info,state,context.frame_for(info,'1D'),now,full_reference=True)
    candidates = []; aligned_cache={}; aligned_markets={}
    def same_listing_reference(required, identity):
        # Existing daily cache pin, calculation-local: never changes source
        # preferences, fetches data, or joins venues. Every pinned TF is audited.
        from desktop014 import Desktop014
        selected={};daily=None
        for t in required:
            if rows[t].get('identity')==identity:
                selected[t]=rows[t]
                if t=='1D':daily=context.frame_for(info,t)
            else:
                key=(tuple(identity.get(k) for k in IDENTITY),t)
                if key not in aligned_cache:
                    with _input_tracking(context) as aligned_token:
                        frame=context.pinned_frame(info,t,identity)
                        row=bounded_features({**state,'expected_identity':identity},frame,t,now)
                    row['_input_revisions']=dict(aligned_token)
                    aligned_cache[key]=(row,frame)
                selected[t],frame=aligned_cache[key]
                if t=='1D':daily=frame
        key=tuple(identity.get(k) for k in IDENTITY)
        if key not in aligned_markets:
            with _input_tracking(context) as market_token:
                aligned_markets[key]=(bounded_benchmark(context,info,state,daily,now,full_reference=True)
                    if daily is not None and selected.get('1D',{}).get('full_reference',{}).get('analysis_ready')
                    else {'state':'UNAVAILABLE','reason':'PINNED_DAILY_CONTEXT_BLOCKED'})
            aligned_markets[key]['_input_revisions']=dict(market_token)
        return selected,aligned_markets[key]
    for tf in ordered_tf:
        required = tuple(dict.fromkeys((tf, *profile(tf))))
        blockers = [f'{t}: {reason}' for t in required for reason in rows[t].get('blockers', [])]
        identities = [rows[t].get('identity') for t in required]
        if any(x != identities[0] for x in identities) or not identities[0]: blockers.append('MTF_IDENTITY_MISMATCH')
        result = None
        candidate_context='bounded_200_exact_reference_v1'
        reference_rows=rows;reference_market=full_benchmark;reference_dependencies=None
        if ('MTF_IDENTITY_MISMATCH' in blockers and rows[tf].get('full_reference',{}).get('analysis_ready')
                and info.get('eur_history_auto',bool(info.get('eur_listings')))
                and all(t in TIMEFRAMES for t in required)):
            try:
                reference_rows,reference_market=same_listing_reference(required,identities[0])
                reference_dependencies={t:{k:v for k,v in reference_rows[t].items() if k not in ('features','full_reference')}
                    | {'full_reference':{k:v for k,v in reference_rows[t].get('full_reference',{}).items() if k!='features'}} for t in required}
                blockers.extend(t+': PINNED_REFERENCE: '+reason for t in required
                    if not reference_rows[t].get('full_reference',{}).get('analysis_ready')
                    for reason in reference_rows[t].get('blockers',['UNCONFIRMED']))
            except (ValueError,KeyError,TypeError) as error:
                blockers.append('SAME_LISTING_REFERENCE_UNAVAILABLE: '+str(error))
        if not blockers and all(rows[t].get('analysis_ready') for t in required):
            # Missing benchmark is explicit; do not borrow the full-context result.
            result = evaluate({t: rows[t]['features'] for t in required}, tf, state.get('position'),
                              benchmark or {'state':'UNAVAILABLE','reason':'Benchmark not evaluated'})
        elif (identities[0] and all(reference_rows[t].get('identity')==identities[0] for t in required)
                and all(reference_rows[t].get('full_reference',{}).get('analysis_ready') for t in required)):
            # Existing CORE on ONE fully audited closed reference per TF.
            # Do not borrow truncated features or relax recursive equivalence.
            result=evaluate({t:reference_rows[t]['full_reference']['features'] for t in required},tf,
                state.get('position'),reference_market)
            candidate_context='full_closed_reference_v1'
            blockers=[]
        analysis_ready = bool(result and result.get('status') == 'READY')
        signal_market=reference_market if candidate_context=='full_closed_reference_v1' else benchmark
        signal_ready = bool(analysis_ready and signal_market and benchmark_ready(signal_market)
                            and result.get('signal') in ('BUY','SELL'))
        candidates.append(dict(strategy='CORE:'+VERSION, context=candidate_context, timeframe=tf, required_tf=list(required),
            analysis_ready=analysis_ready, signal_ready=signal_ready, trade_ready=False,
            blockers=blockers, signal_blockers=[] if signal_ready else ['Нет подтверждённого BUY/SELL с пригодным benchmark'],
            signal_benchmark=signal_market,
            trade_blockers=['PAPER_INSTRUMENT_BLOCKED'] if not instrument_allowed(info) else ['EXECUTION_GATES_NOT_CHECKED'],
            result=result,reference_dependencies=reference_dependencies,
            reference_benchmark=reference_market if reference_dependencies is not None else None))
        candidate=candidates[-1]
        impact_rows={t:rows[t]['quality_impact'] for t in required if rows[t].get('quality_impact')}
        if impact_rows:
            candidate['quality_impact']=impact_rows
            if result:
                result.setdefault('warnings',[]).extend(w for r in impact_rows.values() for w in r['warnings'])
        candidate['analytical_only']=any(rows[t].get('analytical_only') for t in required)
        if result and result.get('accepted_risks'):
            candidate['risk_context']=result['calculation_context']
        if analysis_ready and hasattr(context,'store'):
            used=[reference_rows[t] for t in required]
            selected_market=reference_market if candidate_context=='full_closed_reference_v1' else benchmark
            specs=[row.get('_closed_input') for row in used]+[selected_market.get('_closed_input')]
            # Bind only this candidate's calculation inputs. Execution also
            # checks its own strict revisions and every existing paper gate.
            if all(specs):
                used_token={k:v for row in used for k,v in row.get('_input_revisions',{}).items()}
                used_token.update(selected_market.get('_input_revisions',full_benchmark_token if candidate_context=='full_closed_reference_v1' else benchmark_token))
                used_token.update(context.store.read_cache.token_for(['instrument:'+info['isin'],'setting:cp_manual_risk:'+info['isin']]))
                candidate['closed_inputs']=_candidate_proof(context.store,specs,used_token)
                candidate['input_binding_version']=2
                candidate['expires_at']=min([row['valid_until'] for row in used]+[selected_market['valid_until']])
                candidate['as_of']={t:reference_rows[t]['full_reference'].get('last_close_at') for t in required}
                candidate['completed_at']=pd.Timestamp.now(tz='UTC').isoformat()
        if not analysis_ready:
            approved=_owner_analysis_candidate(context,info,tf,now)
            if approved:candidate.update(approved)
    for row in rows.values():
        row.pop('features', None)
        row.get('full_reference',{}).pop('features',None)
    deadlines=[pd.Timestamp(row.get('valid_until',now+pd.Timedelta(seconds=60))) for row in rows.values() if row.get('chart_ready') or row.get('analysis_ready')]
    if any(c['analysis_ready'] for c in candidates):
        for market in (benchmark,full_benchmark):
            if market.get('benchmark_id'):deadlines.append(pd.Timestamp(market.get('valid_until',now+pd.Timedelta(seconds=60))))
        for candidate in candidates:
            if candidate['analysis_ready'] and candidate.get('reference_dependencies'):
                deadlines.extend(pd.Timestamp(row.get('valid_until',now+pd.Timedelta(seconds=60))) for row in candidate['reference_dependencies'].values())
                market=candidate.get('reference_benchmark') or {}
                if market.get('benchmark_id'):deadlines.append(pd.Timestamp(market.get('valid_until',now+pd.Timedelta(seconds=60))))
    expires=min(deadlines) if deadlines else now+pd.Timedelta(minutes=15)
    analysis_scope=next((r.get('analysis_scope') for r in rows.values() if r.get('analysis_scope')),None)
    if analysis_scope is None and all(not r.get('analysis_ready') and not r.get('full_reference',{}).get('analysis_ready') for r in rows.values()) and not any(c.get('analysis_ready') for c in candidates):
        # The attempted scope exists even when every input view is empty. Its
        # absence used to look like a revision race, losing all eleven exact
        # NO_USABLE_CANDLES diagnostics. This is only a blocked receipt; no
        # identity, candle quality, indicator or execution proof is invented.
        analysis_scope=dict(policy=SCOPE_POLICY,timezone='Europe/Berlin',
            start=(pd.Timestamp(now).tz_convert('Europe/Berlin').normalize()-pd.DateOffset(years=5)).date().isoformat(),
            no_usable_input=True)
    return dict(context='bounded_200_exact_reference_v1', isin=info['isin'], checked_at=now.isoformat(),
                expires_at=expires.isoformat(), expiry_basis='Earliest next closed-bar boundary of usable TF/benchmark; revisions checked on every read', timeframes=rows, candidates=candidates,
                benchmark=benchmark, full_reference_benchmark=full_benchmark, execution_version=1,
                calendar_revision=data_quality.XFRA_SESSION_REVISION,
                input_binding_version=2,closed_input_semantics=2,primary_entry_timeframe='15m',optional_timeframes=['5m','10m'],
                analysis_scope=analysis_scope,
                reference_binding_version=1,
                scope='Separate bounded/full closed reference candidates; no mixed features; execution requires existing paper gates and transactional revalidation')


def eligibility_state(db, record, now):
    """Validate persisted evidence against one read transaction, on EVERY read."""
    if not record: return 'UNKNOWN'
    # Failed attempts have no usable input proof. Preserve their bounded retry
    # state even when a newly approved policy is absent from the error receipt.
    if record.get('error'): return 'ERROR'
    from cp_controls import DEMO_ALL_POLICY,read
    if read(db,DEMO_ALL_POLICY,{}).get('approved') is True and 'setting:'+DEMO_ALL_POLICY not in record.get('_input_revisions',{}):return 'STALE'
    from cp_controls import RISK_CONTEXT
    if (any(c.get('risk_context') and c['risk_context']!=RISK_CONTEXT for c in record.get('candidates',[]))
        or any(r.get('accepted_risk') and r['accepted_risk'].get('context')!=RISK_CONTEXT for r in record.get('timeframes',{}).values())):return 'STALE'
    if record.get('execution_version') != 1: return 'STALE'
    if not scope_current(record,now):return 'STALE'
    if record.get('reference_binding_version')!=1 and any(c.get('timeframe') in ('1W','1M')
            and 'MTF_IDENTITY_MISMATCH' in c.get('blockers',[])
            and record.get('timeframes',{}).get(c['timeframe'],{}).get('full_reference',{}).get('analysis_ready')
            for c in record.get('candidates',[])):
        return 'STALE'
    if (any(row.get('identity',{}).get('venue')=='FRA' for row in record.get('timeframes',{}).values())
            and record.get('calendar_revision')!=data_quality.XFRA_SESSION_REVISION): return 'STALE'
    token=record.get('_input_revisions')
    if not token or 'instrument:'+record.get('isin','') not in token: return 'STALE'
    # Old receipts did not depend on owner permission. Only a granted stock
    # needs reevaluation; the ordinary queue and other stocks stay unchanged.
    grant_key='setting:cp_manual_risk:'+record.get('isin','')
    if grant_key not in token:
        from cp_controls import read
        if read(db,grant_key.removeprefix('setting:'),{}).get('accepted'):return 'STALE'
    if any((db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0]!=v for k,v in token.items()): return 'STALE'
    try:
        if pd.Timestamp(now)>=pd.Timestamp(record['expires_at']): return 'EXPIRED'
    except (KeyError,ValueError,TypeError): return 'STALE'
    return 'CURRENT'


def candidate_state(db, record, candidate, now, memo=None):
    """Independent analysis evidence; alone never grants execution permission."""
    from cp_controls import RISK_CONTEXT
    if candidate.get('risk_context') and candidate['risk_context']!=RISK_CONTEXT:return 'STALE'
    guard=candidate.get('closed_inputs')
    if (candidate.get('input_binding_version')!=2 and candidate.get('context')!='full_closed_reference_v1') or not guard:
        return eligibility_state(db,record,now)
    if record.get('error') or record.get('execution_version')!=1:return 'STALE'
    if not scope_current(record,now):return 'STALE'
    if record.get('calendar_revision')!=data_quality.XFRA_SESSION_REVISION:return 'STALE'
    if 'instrument:'+record.get('isin','') not in guard.get('revisions',{}):return 'STALE'
    from performance_cache import validate_closed_inputs,InputRevisionChanged
    try:validate_closed_inputs(db,guard,memo)
    except (InputRevisionChanged,ValueError,KeyError,TypeError):return 'STALE'
    try:
        if pd.Timestamp(now)>=pd.Timestamp(candidate['expires_at']):return 'EXPIRED'
    except (KeyError,ValueError,TypeError):return 'STALE'
    return 'CURRENT'


def _owner_initialization_current(record):
    # Re-evaluate earlier receipts through the ordinary CP queue once for the
    # approved EUR noise comparison and current owner-policy precedence.
    return all(c.get('owner_initialization_semantics')==3 for c in (record or {}).get('candidates',[]) if c.get('owner_reviewed_analysis'))


def calculate_eligibility(store, info):
    """Existing all-TF assessment, read-only; acquisition need not wait for it."""
    import json
    from snapshot_worker import SnapshotContext, Value
    from paper_account import UNIVERSE_VERSION
    isin=info['isin'];key='cp_eligibility:'+isin
    reused=None
    with store.connect() as db:
        db.execute('BEGIN')
        prior=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        prior=json.loads(prior[0]) if prior else None
        if prior and prior.get('paper_universe_version')==UNIVERSE_VERSION and prior.get('input_binding_version')==2 and prior.get('closed_input_semantics')==2 and _owner_initialization_current(prior) and eligibility_state(db,prior,pd.Timestamp.now(tz='UTC'))=='CURRENT': reused=prior
    if reused is not None:return reused
    now=pd.Timestamp.now(tz='UTC')
    try:
        with store.read_cache.track_inputs(isolated=True) as token:
            store.read_cache.depend('instrument:'+isin)
            with store.connect() as db:
                row=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
            if row is None: return None
            current=json.loads(row[0])
            if current.get('kind')=='benchmark': return None
            ctx=SnapshotContext();ctx.store=store;ctx.watchlist=[current];ctx.drafts={}
            ctx.symbol=Value(current['name']);ctx.timeframe=Value('15m');ctx.period=Value('Max')
            result=assess_all(ctx,current,now)
            result['execution_version']=1
            attach_execution(ctx,current,result,now)
            result['paper_universe_version']=UNIVERSE_VERSION
            result['_input_revisions']=dict(token)
            store.read_cache.assert_current(token)
        return result
    except Exception as error:
        return dict(isin=isin,error=type(error).__name__+": "+str(error),checked_at=now.isoformat(),
                    expires_at=(now+pd.Timedelta(minutes=5)).isoformat())


def publish_eligibility(store,info,result):
    """Short validated publication; never publish stale dependencies as READY."""
    import json
    if result is None:return None
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not result.get('error'):
            state=eligibility_state(db,result,pd.Timestamp.now(tz='UTC'))
            if state not in ('CURRENT','EXPIRED'):
                raise ValueError('ELIGIBILITY_INPUT_CHANGED: '+state)
            result=dict(result,publication_state=state)
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
            ('cp_eligibility:'+info['isin'],json.dumps(result,ensure_ascii=False,allow_nan=False)))
    return result


def eligibility_turn(store, info):
    """Preserve the entry point; the live feed only requests the owned read worker."""
    if getattr(store,'defer_eligibility',False):
        from cp_assessment import request
        return request(store,info['isin'])
    requested=store.setting('cp_reconciliation_progress:'+info['isin'],{}).get('analysis_requested_at')
    result=publish_eligibility(store,info,calculate_eligibility(store,info))
    if requested:
        from cp_reconciliation import complete_analysis_request
        complete_analysis_request(store,info['isin'],requested,result)
    return result


def eligibility_due(store, infos, now):
    import json
    from paper_account import ALLOWED,UNIVERSE_VERSION
    pending=[]
    with store.connect() as db:
        db.execute('BEGIN')
        for info in infos:
            if info.get('kind')=='benchmark': continue
            row=db.execute('SELECT value FROM settings WHERE key=?',('cp_eligibility:'+info['isin'],)).fetchone()
            prior=json.loads(row[0]) if row else None
            status=eligibility_state(db,prior,now)
            upgrade=bool(prior and (prior.get('paper_universe_version')!=UNIVERSE_VERSION or prior.get('closed_input_semantics')!=2 or not _owner_initialization_current(prior)))
            if status=='CURRENT' and prior.get('input_binding_version')==2 and not upgrade: continue
            if status=='ERROR' and pd.Timestamp(now)<pd.Timestamp(prior['expires_at']): continue
            # One-time rebind of existing usable proofs after the metadata
            # semantics upgrade, then return to normal oldest-first fairness.
            upgrade_ready=upgrade and any(c.get('analysis_ready') for c in prior.get('candidates',[]))
            pending.append((bool(prior),not upgrade_ready,(prior or {}).get('checked_at',''),info['isin'] not in ALLOWED,info))
    # Unvisited instruments first, then oldest attempt. No per-ticker special code.
    return [i for _,_,_,_,i in sorted(pending,key=lambda r:r[:4])[:1]]

def inspect(info, frame, tf, now):
    identity = {k: frame.attrs.get(k) for k in IDENTITY}
    archive = frame.attrs.get('data_quality', {})
    result = dict(identity=identity, archive_quality=archive, timeframe=tf, blockers=[])
    blockers = result['blockers']
    from eur_listings import confirmed_listing
    confirmed = confirmed_listing(info,frame.attrs)
    if not confirmed or identity['currency'] != 'EUR': blockers.append('IDENTITY_UNCONFIRMED')
    if archive.get('state') != 'READY': blockers.append('ARCHIVE_' + str(archive.get('state', 'UNKNOWN')))
    try:
        ends = bar_ends(frame)
        closed = frame.loc[(ends <= now) & frame.get('bar_complete', pd.Series(True, index=frame.index)).fillna(False)].copy()
        window = closed.iloc[-200:].copy()
        if len(window) < 35: blockers.append('INSUFFICIENT_CLOSED_BARS')
        if window.empty: raise ValueError('NO_CLOSED_BARS')
        # Fixed trailing policy, not a search for an arbitrary clean segment.
        window.attrs = {**frame.attrs, 'intraday_span': [str(window.index[0]), str(window.index[-1])]}
        report = data_quality.audit(window, tf, expected=identity, now=now)
        result.update(window_quality=report, count=len(window), first=str(window.index[0]), last=str(window.index[-1]),
                      last_close_at=bar_ends(window)[-1].isoformat(), reference_close=float(window.iloc[-1]['close']))
        if report['state'] != 'READY': blockers.append('WINDOW_' + report['state'])
        enriched = indicators(window)
        names = ('ema10','ema30','rsi','macd','macd_signal','atr','volume_mean20')
        values = {k: float(enriched.iloc[-1][k]) for k in names}
        if not all(math.isfinite(v) for v in values.values()): blockers.append('INDICATOR_WARMUP')
        result['indicators'] = {k: v if math.isfinite(v) else None for k,v in values.items()}
        result['indicator_context']='validation_window_200; not the CORE signal context'
        local = now.tz_convert(identity['timezone']).tz_localize(None)
        sessions = schedule(identity, local.normalize(), local.normalize())
        active = any(row.open <= now < row.close and not (pd.notna(row.break_start) and row.break_start <= now < row.break_end)
                     for _, row in sessions.iterrows())
        result['session_open'] = active
        if not active: blockers.append('SESSION_CLOSED')
        if tf in ('5m','15m') and not 0 <= (now - bar_ends(window)[-1]).total_seconds() <= 900:
            blockers.append('STALE_EXECUTION_BAR')
    except Exception as error:
        blockers.append('WINDOW_UNAVAILABLE:' + type(error).__name__)
    result['status'] = 'BLOCKED' if blockers else 'READY'
    return result

def build(context, info, entry, state, now, require_smart=True):
    engine=None
    if require_smart:
        import copy
        from cp_controls import manual_context
        entry_frame=context.frame_for(info,entry)
        proof=None
        if context.store.setting('cp_manual_risk:'+info['isin'],{}).get('accepted'):
            scoped=copy.copy(context);scoped.closed_specs={}
            with _input_tracking(scoped) as token:engine=scoped.trading_analysis(info) or {}
            proof=_candidate_proof(context.store,list(scoped.closed_specs.values()),dict(token))
        manual=manual_context(context.store,info['isin'],{k:entry_frame.attrs.get(k) for k in IDENTITY},entry,proof)
        if manual:
            # Owner's separate decision now also runs the actual CORE. An
            # accepted gap is not a fabricated candle or a fabricated signal.
            if engine is None:engine=context.trading_analysis(info) or {}
            return dict(status='MANUAL_RISK_ACCEPTED',isin=info['isin'],checked_at=now.isoformat(),manual_risk=manual,
                manual_inputs=proof,
                trading_engine=engine,analysis_ready=engine.get('status')=='READY',
                automatic_permission=False,permission='Owner-reviewed historical risks only; fresh valid quote and transactional checks still mandatory')
    entry_frame=context.frame_for(info,entry)
    windows = {tf: inspect(info, entry_frame if tf==entry else context.frame_for_entry(info,tf,entry_frame), tf, now) for tf in (entry, '1H', '4H')}
    blockers = [tf + ':' + reason for tf, row in windows.items() for reason in row['blockers']]
    identities = [row['identity'] for row in windows.values()]
    if any(x != identities[0] for x in identities[1:]): blockers.append('MTF_IDENTITY_MISMATCH')
    # Retain the existing CORE profile and its independent blockers. The requested
    # 4H/1H/entry context adds information; it does not replace CORE's 1D gate.
    if engine is None:engine = context.trading_analysis(info) or {}
    if engine.get('status') != 'READY': blockers.append('TRADING_ENGINE_DATA_BLOCKED')
    # Existing user levels only; no new strategy or invented stop/target.
    valid_levels = state.get('levels_currency',state.get('currency')) == 'EUR' and state.get('levels_source',context.source_mode(info)) == identities[0]['source']
    levels = state.get('levels', {}) if valid_levels else {}
    stop, target = levels.get('stop'), levels.get('target')
    price = windows[entry].get('reference_close')
    rr = None
    if all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in (stop,price,target)) and 0 < stop < price < target:
        rr = (target-price)/(price-stop)
    smart=None
    if require_smart:
        try:
            with context.store.connect() as db:
                db.execute('BEGIN')
                smart=execution_eligibility(db,info,entry,windows[entry]['identity'],now)
        except ValueError as error: blockers.append(str(error))
    return dict(status='BLOCKED' if blockers else 'READY', isin=info['isin'], windows=windows, blockers=blockers, smart_eligibility=smart,
                roles={entry:'entry','1H':'confirmation','4H':'trend'}, checked_at=now.isoformat(),
                trading_engine=engine, max_execution_age_seconds=900, stop=stop, target=target,
                risk_reward=rr, risk_reward_reason='Existing same-source EUR user levels; no automatic stop/target' if rr is not None else 'Requires a validated same-source EUR entry/stop/target',
                permission='Additional gate only; archive DQ and transactional input revisions remain mandatory')


def attach_execution(context, info, assessment, now):
    """Existing execution gates are an additional intersection, never a bypass."""
    from paper_account import instrument_allowed
    from snapshot_worker import Value
    from cp_controls import DEMO_ALL_INITIALIZATION
    if not instrument_allowed(info): return
    for candidate in assessment['candidates']:
        tf=candidate['timeframe']
        owner_demo=candidate.get('owner_reviewed_analysis') and candidate.get('context')==DEMO_ALL_INITIALIZATION and tf=='15m'
        if candidate.get('owner_reviewed_analysis') and not owner_demo:
            candidate['trade_ready']=False
            candidate['trade_blockers']=['OWNER_REVIEWED_DEMO_CONTEXT; execution rechecked separately on 15m'];continue
        if candidate.get('analytical_only'):
            candidate['trade_ready']=False
            candidate['trade_blockers']=['IMPACT_ANALYTICAL_ONLY_NO_EXECUTION_CHANGE'];continue
        if tf not in ('5m','15m'):
            candidate['trade_blockers']=['PAPER_EUR_INTRADAY_REQUIRED'];continue
        if not candidate.get('signal_ready'):
            candidate['trade_blockers']=['CORE_SIGNAL_NOT_READY'];continue
        previous=context.timeframe
        try:
            context.timeframe=Value(tf)
            with _input_tracking(context) as execution_token:
                contract=build(context,info,tf,context.user_state(info),now,require_smart=False)
        finally: context.timeframe=previous
        blockers=list(contract['blockers'])
        full=contract.get('trading_engine',{})
        if owner_demo:
            candidate['owner_demo_execution_version']=1
            if contract['windows'][tf]['identity']!=candidate.get('identity'):
                blockers.append('OWNER_DEMO_EXECUTION_IDENTITY_MISMATCH')
            if full.get('calculation_context') not in (None,DEMO_ALL_INITIALIZATION):
                blockers.append('OWNER_DEMO_EXECUTION_CONTEXT_MISMATCH')
        if full.get('signal') != candidate.get('result',{}).get('signal'):
            blockers.append('FULL_BOUNDED_SIGNAL_MISMATCH')
        if candidate.get('context')=='full_closed_reference_v1':
            # This flag is created only after the original executor gates run;
            # an older analysis-only receipt cannot acquire permission by reuse.
            candidate['full_execution_version']=1
            try:
                with context.store.connect() as db:
                    db.execute('BEGIN')
                    _full_execution_proof(db,assessment,candidate,contract['windows'][tf]['identity'],now)
            except (ValueError,KeyError,TypeError,AttributeError) as error:
                blockers.append('FULL_EXECUTION_PROOF: '+str(error))
        candidate['trade_blockers']=blockers
        candidate['trade_ready']=contract['status']=='READY' and not blockers
        candidate['execution_identity']=contract['windows'][tf]['identity']
        if candidate.get('input_binding_version')==2:
            # Execution has stricter freshness than historical analysis. Track
            # the executor's own inputs (including 1H confirmation), not all TF.
            execution_token.update(context.store.read_cache.token_for([
                'instrument:'+info['isin'],'setting:cp_manual_risk:'+info['isin']]))
            candidate['execution_revisions']=dict(execution_token)
            last=contract['windows'][tf].get('last_close_at')
            candidate['execution_expires_at']=min(pd.Timestamp(candidate['expires_at']),
                pd.Timestamp(last)+pd.Timedelta(seconds=900)).isoformat() if last else now.isoformat()


def _full_execution_proof(db, record, candidate, identity, now):
    """Additional proof for the existing full CORE, not a relaxation of bounded reliability."""
    if candidate.get('full_execution_version')!=1 or not candidate.get('closed_inputs'):
        raise ValueError('SMART_FULL_PROOF_MISSING')
    if candidate.get('result',{}).get('status')!='READY':raise ValueError('SMART_FULL_CORE_BLOCKED')
    if candidate_state(db,record,candidate,now)!='CURRENT':
        raise ValueError('SMART_FULL_PROOF_NOT_CURRENT')
    rows=candidate.get('reference_dependencies') or record.get('timeframes',{})
    required=candidate['required_tf']
    for tf in required:
        row=rows.get(tf,{})
        ref=row.get('full_reference',{})
        if (row.get('identity')!=identity or not ref.get('analysis_ready') or
                ref.get('context')!='full_closed_reference_v1' or ref.get('quality',{}).get('state')!='READY' or
                not ref.get('scope_initialization',{}).get('stable') or ref.get('count',0)<35):
            raise ValueError('SMART_FULL_REFERENCE_BLOCKED:'+tf)
        if not (pd.Timestamp(ref['last_close_at'])<=now<pd.Timestamp(row['valid_until'])):
            raise ValueError('SMART_FULL_REFERENCE_EXPIRED:'+tf)
    market=candidate.get('reference_benchmark') or record.get('full_reference_benchmark',{})
    if (market.get('context')!='full_closed_reference_v1' or not benchmark_ready(market) or
            candidate.get('signal_benchmark')!=market or not market.get('benchmark_id')):
        raise ValueError('SMART_FULL_BENCHMARK_BLOCKED')
    if now>=pd.Timestamp(market['valid_until']):raise ValueError('SMART_FULL_BENCHMARK_EXPIRED')
    series=candidate['closed_inputs'].get('series',[])
    for tf in required:
        if not any(s.get('count',0)>0 and s['spec'].get('isin')==record['isin'] and
                s['spec'].get('timeframe')==tf and s['spec'].get('identity')==identity for s in series):
            raise ValueError('SMART_FULL_DEPENDENCY_MISSING:'+tf)
    if not any(s.get('count',0)>0 and s['spec']==market.get('_closed_input') for s in series):
        raise ValueError('SMART_FULL_BENCHMARK_DEPENDENCY_MISSING')
    return rows,market


def _owner_demo_execution_proof(db,info,candidate,identity,required):
    """Existing executor intersection, not an analysis-only permission."""
    from cp_controls import read,DEMO_ALL_POLICY,DEMO_ALL_INITIALIZATION,analysis_quality_allowed
    from paper_account import ACCOUNT
    from eur_listings import stored_identity
    authority=read(db,DEMO_ALL_POLICY,{})
    if not (authority.get('version')==DEMO_ALL_INITIALIZATION and authority.get('approved') is True
            and authority.get('account')==ACCOUNT and authority.get('scope')=='registered_stocks'
            and authority.get('older_verified_warmup') is True
            and candidate.get('owner_demo_execution_version')==1 and candidate.get('timeframe')=='15m'
            and candidate.get('identity')==identity and stored_identity(db,info,identity,'15m')):
        raise ValueError('OWNER_DEMO_EXECUTION_POLICY_BLOCKED')
    engine=candidate.get('result') or {};proof=engine.get('demo_initialization') or {}
    if (engine.get('status')!='READY' or engine.get('analysis_identity')!=identity
            or (proof and (proof.get('policy')!=DEMO_ALL_INITIALIZATION or not proof.get('consistent') or not proof.get('scenarios_checked')))):
        raise ValueError('OWNER_DEMO_CORE_PROOF_BLOCKED')
    for tf in required:
        row=engine.get('timeframe_context',{}).get(tf,{})
        if (not row.get('scope_initialization',{}).get('stable') or row.get('closed_bar_count',0)<35
                or not row.get('facts') or not analysis_quality_allowed(row.get('data_quality',{}),row.get('accepted_risk'))):
            raise ValueError('OWNER_DEMO_DEPENDENCY_BLOCKED: '+tf)
    return candidate.get('signal_benchmark') or {}


def execution_eligibility(db, info, tf, identity, now, side=None):
    """Called inside the execution transaction; persisted flags alone confer no rights."""
    import json
    from trading_engine import VERSION, profile
    from paper_account import registered_instrument
    registered_instrument(db,info['isin'])
    if tf not in ('5m','15m'):
        raise ValueError('SMART_PAPER_POLICY_BLOCKED')
    row=db.execute('SELECT value FROM settings WHERE key=?',('cp_eligibility:'+info['isin'],)).fetchone()
    record=json.loads(row[0]) if row else {}
    if record.get('context')!='bounded_200_exact_reference_v1' or record.get('isin')!=info['isin']:
        raise ValueError('SMART_CONTEXT_MISMATCH')
    candidate=next((c for c in record.get('candidates',[]) if c.get('timeframe')==tf),{})
    if candidate.get('input_binding_version')==2:
        if candidate_state(db,record,candidate,now)!='CURRENT':raise ValueError('SMART_ELIGIBILITY_NOT_CURRENT')
        token=candidate.get('execution_revisions',{})
        if ('instrument:'+info['isin'] not in token or
                any((db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0]!=v for k,v in token.items())):
            raise ValueError('SMART_EXECUTION_INPUT_CHANGED')
        if now>=pd.Timestamp(candidate.get('execution_expires_at',now)):
            raise ValueError('SMART_EXECUTION_EXPIRED')
    elif eligibility_state(db,record,now)!='CURRENT':raise ValueError('SMART_ELIGIBILITY_NOT_CURRENT')
    if candidate.get('analytical_only'):raise ValueError('IMPACT_ANALYTICAL_ONLY_NO_EXECUTION_CHANGE')
    required=list(dict.fromkeys((tf,*profile(tf))))
    if candidate.get('strategy')!='CORE:'+VERSION or candidate.get('required_tf')!=required:
        raise ValueError('SMART_STRATEGY_MISMATCH')
    mode=candidate.get('context',record.get('context'))
    from cp_controls import DEMO_ALL_INITIALIZATION
    owner_demo=mode==DEMO_ALL_INITIALIZATION
    if mode not in ('bounded_200_exact_reference_v1','full_closed_reference_v1',DEMO_ALL_INITIALIZATION):
        raise ValueError('SMART_CONTEXT_MISMATCH')
    rows=record.get('timeframes',{});market=record.get('benchmark',{})
    if mode=='full_closed_reference_v1':rows,market=_full_execution_proof(db,record,candidate,identity,now)
    if owner_demo:market=_owner_demo_execution_proof(db,info,candidate,identity,required)
    if not all(candidate.get(k) for k in ('analysis_ready','signal_ready','trade_ready')) or not benchmark_ready(market):
        raise ValueError('SMART_EXECUTION_BLOCKED')
    if candidate.get('execution_identity')!=identity or (not owner_demo and any(
        not (rows.get(t,{}).get('full_reference',{}) if mode=='full_closed_reference_v1' else rows.get(t,{})).get('analysis_ready') or
        rows[t].get('identity')!=identity for t in required)):
        raise ValueError('SMART_IDENTITY_BLOCKED')
    signal=candidate.get('result',{}).get('signal')
    if signal not in ('BUY','SELL') or (side is not None and side!=signal):
        raise ValueError('SMART_SIGNAL_MISMATCH')
    return dict(context=mode,strategy=candidate['strategy'],timeframe=tf,
                checked_at=record['checked_at'],expires_at=candidate.get('execution_expires_at',record['expires_at']),signal=signal)
