"""Separate calculation availability from CP opinions; never an execution grant."""
VERSION = 'gettex_first_v1'

def audit_only_scopes(specs):
    """Only DQ opinions for explicitly bound factual calculation inputs."""
    return {'setting:data_quality:'+s['isin']+':'+s['identity']['symbol']+':'+s['base_tf']
        for s in specs if s and s.get('calculation_policy')==VERSION
        and s.get('identity',{}).get('source') in ('gettex','gettex_chart')
        and s.get('identity',{}).get('venue')=='GETTEX'
        and s.get('identity',{}).get('currency')=='EUR'}


def bind(frame, info):
    """Attach an in-memory contract; no stored quality/provenance is changed."""
    if source_contract(info, frame.attrs):
        frame = frame.copy(deep=False)
        frame.attrs = {**frame.attrs, 'gettex_first_contract': dict(
            version=VERSION, isin=info['isin'], identity={k:frame.attrs.get(k)
            for k in ('source','symbol','venue','currency','timezone')})}
    return frame


def bound(frame):
    import numpy as np
    receipt = frame.attrs.get('gettex_first_contract', {})
    try:
        prices=frame[['open','high','low','close']]
        numeric=frame[['open','high','low','close','volume']]
        valid=(not frame.empty and frame.index.is_unique and frame.index.is_monotonic_increasing
            and not frame.index.hasnans and np.isfinite(numeric.to_numpy(dtype=float)).all()
            and prices.gt(0).all().all() and frame.volume.ge(0).all()
            and frame.high.ge(prices.max(axis=1)).all() and frame.low.le(prices.min(axis=1)).all())
        valid=valid and 'source' in frame and set(frame['source'].astype(str))=={frame.attrs.get('source')}
    except (KeyError,ValueError,TypeError,AttributeError):return False
    return bool(valid and receipt.get('version') == VERSION and
                source_contract({'isin':receipt.get('isin')},frame.attrs) and
                receipt.get('identity') == {k:frame.attrs.get(k) for k in
                    ('source','symbol','venue','currency','timezone')})


def source_contract(info, metadata):
    from eur_listings import confirmed_listing
    return bool(info.get('isin') and metadata.get('source') in ('gettex', 'gettex_chart')
                and metadata.get('venue') == 'GETTEX' and metadata.get('currency') == 'EUR'
                and metadata.get('timezone') == 'Europe/Berlin'
                and confirmed_listing(info, metadata))


def availability(frame, info, valid, closed_count):
    verified = source_contract(info, frame.attrs)
    if 'source' not in frame or set(frame['source'].astype(str)) != {frame.attrs.get('source')}:
        verified = False
    report = frame.attrs.get('data_quality', {})
    warning=report.get('state')!='READY'
    warning_count=max(1,len(report.get('issues',[]))+len(report.get('warnings',[]))) if warning else 0
    return dict(policy=VERSION, source_contract=verified, data_present=not frame.empty,
                indicator_computable=bool(verified and valid and closed_count >= 35),
                analysis_available=False, execution_permission=False,
                source=frame.attrs.get('source'), venue=frame.attrs.get('venue'),
                quality_review_status=report.get('state', 'UNKNOWN'),
                dq_warning_count=warning_count,
                dq_warnings=dict(state=report.get('state','UNKNOWN'),missing_count=report.get('missing_count'),
                    range_count=len(report.get('ranges',[])),report_ref='data_quality') if warning else {})


def calculation_allowed(context):
    proof = context.get('calculation_availability', {})
    return bool(proof.get('policy') == VERSION and proof.get('source_contract') is True
                and proof.get('analysis_available') is True
                and context.get('ohlcv_valid') and context.get('sufficient_history')
                and context.get('facts'))


def engine_available(engine, required):
    contexts=engine.get('timeframe_context',{})
    return bool(engine.get('status')=='READY' and required and all(
        contexts.get(tf,{}).get('calculation_availability',{}).get('policy')==VERSION
        and contexts.get(tf,{}).get('calculation_availability',{}).get('source_contract') is True
        and contexts.get(tf,{}).get('calculation_availability',{}).get('analysis_available') is True
        and contexts.get(tf,{}).get('facts') for tf in required))


def decision_evidence(result):
    """Bounded decision-time evidence; original fills never rewritten."""
    import hashlib,json
    engine=result.get('execution_context',{}).get('trading_engine',{})
    proof=result.get('hourly',{}).get('proof',{})
    contexts={}
    for tf,row in engine.get('timeframe_context',{}).items():
        report=row.get('data_quality',{})
        contexts[tf]=dict(facts=row.get('facts'),availability=row.get('calculation_availability',{}),
            quality_state=report.get('state'),missing_count=report.get('missing_count'),
            quality_digest=hashlib.sha256(json.dumps({k:v for k,v in report.items() if k not in ('checked_at','updated_at')},sort_keys=True,default=str).encode()).hexdigest())
        # The quality tree can be large; full state remains in CP's archive.
        contexts[tf]['availability']={k:v for k,v in contexts[tf]['availability'].items() if k!='dq_warnings'}
    quote=result.get('execution_quote',{})
    return dict(policy=VERSION,isin=result.get('isin'),identity=result.get('identity'),
        timeframe=result.get('timeframe'),signal_at=engine.get('timestamp'),signal=engine.get('signal'),
        inputs=[{k:s.get(k) for k in ('spec','count','digest')} for s in proof.get('series',[])],
        contexts=contexts,execution_quote={k:quote.get(k) for k in ('isin','identity','timeframe','bars')},quote_blocker=result.get('quote_blocker'))

