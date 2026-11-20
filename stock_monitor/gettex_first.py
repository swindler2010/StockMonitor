"""Separate calculation availability from CP opinions; never an execution grant."""
VERSION = 'gettex_first_v1'


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
    return dict(policy=VERSION, source_contract=verified, data_present=not frame.empty,
                indicator_computable=bool(verified and valid and closed_count >= 35),
                analysis_available=False, execution_permission=False,
                source=frame.attrs.get('source'), venue=frame.attrs.get('venue'),
                quality_review_status=report.get('state', 'UNKNOWN'),
                dq_warnings=report if report.get('state') != 'READY' else {})


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
