"""Separate calculation availability from CP opinions; never an execution grant."""
VERSION = 'gettex_first_v1'


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
