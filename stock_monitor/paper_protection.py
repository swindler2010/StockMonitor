"""Optional PAPER bracket over the existing executor; no feed or broker.

This closes at the current validated 5m/15m close, including normal spread and
fees. It is NOT an intrabar stop fill, a guaranteed loss ceiling, or V2 Smart
Exit. V1 entry rules and the V1/V2 comparison books are unchanged.
"""
from decimal import Decimal

VERSION='atr_closed_bar_bracket_v1'
POLICY=dict(version=VERSION,enabled=True,atr_multiple='2',target_r='2',
            trigger='VALIDATED_CLOSED_QUOTE',fill='EXISTING_PAPER_CLOSE')


def enabled(cfg):
    value=cfg.get('protection')
    if value is None:return False
    if value!=POLICY:raise ValueError('PAPER_PROTECTION_POLICY_UNCONFIRMED')
    return True


def entry_plan(cfg,result,size,now):
    """Bind a new entry only. Never retroactively arm existing positions."""
    if not enabled(cfg):return None
    from paper_hourly import SIZING
    from paper_account import POLICY as execution
    from timeframes import LABELS
    tf=result['timeframe'];q=result['execution_quote']
    if tf not in LABELS or size.get('mode')!=SIZING:
        raise ValueError('PAPER_PROTECTION_CONTEXT_UNCONFIRMED')
    f=result['execution_context']['trading_engine']['timeframe_context'][tf]['facts']
    try:
        reference,atr,support=(Decimal(str(v)) for v in (q['bars'][-1]['close'],f['atr'],f['support']))
        if not all(v.is_finite() and v>0 for v in (reference,atr,support)):raise ValueError()
        stop=min(reference-Decimal(POLICY['atr_multiple'])*atr,support*Decimal('.995'))
        if not 0<stop<reference:raise ValueError()
        half=Decimal(execution['spread_bps'])/20000
        entry=reference*(1+half);quantity=Decimal(size['quantity']);fee=Decimal(execution['commission'])
        risk=(entry-stop*(1-half))*quantity+2*fee
        # Preserve the existing conservative V1 risk estimate as an upper bound.
        if risk>Decimal(size['risk_budget']):raise ValueError()
        target=(entry+(Decimal(POLICY['target_r'])*risk+2*fee)/quantity)/(1-half)
    except (KeyError,ArithmeticError,ValueError):
        raise ValueError('PAPER_PROTECTION_RISK_UNCONFIRMED') from None
    return dict(version=VERSION,identity=result['identity'].copy(),signal_tf=tf,
        quote_tf=q['timeframe'],stop=str(stop),target=str(target),entry=str(entry),
        planned_quantity=str(quantity),estimated_stop_risk=str(risk),
        risk_is_estimate=True,armed_at=now,trigger=POLICY['trigger'],fill=POLICY['fill'])


def decision(db,info,result,now):
    """Immutable entry receipt + current exact-identity quote, no new grant."""
    from paper_account import ledger,quote
    from paper_hourly import authority,digest
    from eur_listings import stored_identity
    from datetime import datetime
    cfg=authority(db,info['isin'],result.get('identity'))
    if not enabled(cfg):return None
    state=ledger(db)
    position=next((p for p in state['positions'] if p['isin']==info['isin'] and Decimal(p['quantity'])>0),None)
    if position is None:return None
    if position['identity']!=result['identity']:raise ValueError('PAPER_PROTECTION_IDENTITY_CHANGED')
    entries=[f for f in state['fills'] if f['isin']==info['isin'] and f['side']=='BUY']
    if not entries:raise ValueError('PAPER_PROTECTION_ENTRY_UNCONFIRMED')
    entry=max(entries,key=lambda f:f['at']);plan=entry.get('smart_eligibility',{}).get('protection')
    if not plan:return None  # Existing unarmed V1 holding keeps its original exits.
    if (plan.get('version')!=VERSION or plan.get('identity')!=result['identity']
        or plan.get('signal_tf')!=entry.get('timeframe') or plan.get('trigger')!=POLICY['trigger']
        or plan.get('fill')!=POLICY['fill']):raise ValueError('PAPER_PROTECTION_BINDING_CHANGED')
    if plan['signal_tf']!=result['timeframe']:return None
    if result.get('quote_blocker'):raise ValueError(result['quote_blocker'])
    q=result.get('execution_quote',{})
    if q.get('identity')!=result['identity']:raise ValueError('PAPER_PROTECTION_QUOTE_IDENTITY')
    _,bar=quote(info,q,now,stored_listing=stored_identity(db,info,result['identity'],q.get('timeframe')))
    close_at=datetime.fromisoformat(bar['close_at']).timestamp()
    if close_at<=datetime.fromisoformat(entry['at']).timestamp():return None
    stop,target=Decimal(plan['stop']),Decimal(plan['target'])
    if not stop.is_finite() or not target.is_finite() or not 0<stop<target:
        raise ValueError('PAPER_PROTECTION_LEVEL_INVALID')
    price=Decimal(str(bar['close']))
    reason='STOP_LOSS' if price<=stop else 'TAKE_PROFIT' if price>=target else None
    if not reason:return None
    # One exit per entry, stable across signal bars, retries and restarts.
    return dict(side='SELL',reason=reason,entry_request_id=entry['request_id'],
        request_id='protect-'+digest([cfg['epoch'],info['isin'],entry['request_id']]),
        plan_digest=digest(plan),quote_at=bar['close_at'],reference=str(price),
        quantity=position['quantity'],trigger=POLICY['trigger'])
