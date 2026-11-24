"""Protective exits for the PAPER account on validated 15m closing prices."""
from decimal import Decimal

VERSION='atr_closed_bar_bracket_v1'
STRATEGY_VERSION='selected_strategy_exit_v1'
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


def strategy_plan(result, signal, mode, size, now):
    if mode=='V2' and signal.get('model'):
        from paper_v2_30m import protection_plan
        return protection_plan(result,signal,size,now)
    from paper_account import POLICY as execution
    from paper_v5 import HELD_GAP
    q=result['execution_quote']
    price=Decimal(str(q['bars'][-1]['close']))
    half=Decimal(execution['spread_bps'])/20000
    stop=Decimal(str(size.get('stop',signal['stop'])))
    entry=price*(1+half)
    target=entry+2*(entry-stop) if mode=='V2' else Decimal(str(size.get('target',signal['target'])))
    plan=dict(version=STRATEGY_VERSION,strategy_mode=mode,identity=result['identity'].copy(),
        signal_tf=result['timeframe'],quote_tf=q['timeframe'],stop=str(stop),target=str(target),
        entry=str(entry),atr=str(signal['atr']),planned_quantity=str(size['quantity']),armed_at=now,
        estimated_stop_risk=size['estimated_risk'],risk_is_estimate=True,
        trigger=POLICY['trigger'],fill=POLICY['fill'])
    if signal.get('model'):
        plan['model']=signal['model']
        if signal.get('setup')==HELD_GAP:
            plan.update(setup=HELD_GAP,time_exit_at=signal['gap_evidence']['exit_at'])
        else:
            plan['hold_minutes']=signal['hold_minutes']
    return plan


def strategy_trigger(db, position, entry, plan, bar, price, *, result=None):
    if plan.get('strategy_mode')=='V2' and plan.get('model'):
        from paper_v2_30m import MODEL,protection_trigger
        if plan['model']!=MODEL:raise ValueError('PAPER_PROTECTION_BINDING_CHANGED')
        return protection_trigger(db,position,entry,plan,bar,price)
    from datetime import datetime
    from paper_account import POLICY as execution, money
    from paper_hourly import read
    mode=plan['strategy_mode']
    raw_stop=Decimal(plan['stop'])
    qty=Decimal(position['quantity'])
    close_at=datetime.fromisoformat(bar['close_at'])
    last=read(db,management_key(entry['request_id']),{})
    if last and datetime.fromisoformat(last['quote_at'])>close_at:
        raise ValueError('PAPER_PROTECTION_OLDER_QUOTE')
    stop=Decimal(last.get('stop',raw_stop))
    # Retries on the same candle cannot trigger a freshly tightened stop.
    if last.get('quote_at')==bar['close_at']:
        stop=Decimal(last['prior_stop'])
    from paper_v1_long_term import MODEL as V1_MODEL, trailing_level
    if mode=='V1':
        if (plan.get('model')!=V1_MODEL or entry['smart_eligibility'].get('strategy_model')!=V1_MODEL
                or plan.get('target') is not None or result is None):
            raise ValueError('PAPER_PROTECTION_BINDING_CHANGED')
        trail = trailing_level(result,entry,close_at)
        new_stop = max(stop, trail) if trail is not None else stop
        if price<=new_stop:
            return 'STOP_LOSS',qty,False,None
        state = (dict(quote_at=bar['close_at'],stop=str(new_stop),prior_stop=str(stop))
                 if new_stop>stop and last.get('quote_at')!=bar['close_at'] else None)
        return None,qty,False,state
    target=Decimal(plan['target'])
    half=Decimal(execution['spread_bps'])/20000
    fee=money(execution['commission'])
    breakeven=(Decimal(position['cost'])/qty+fee/qty)/(1-half)
    entry_price=Decimal(plan['entry'])
    risk=entry_price-raw_stop
    remaining=qty
    reason=None
    part=False
    from paper_v4 import VERSION as V4_MODEL
    from paper_v5 import HELD_GAP
    model=plan.get('model')
    v4=mode=='MOMENTUM' and model==V4_MODEL
    if mode=='MOMENTUM' and model and not v4:
        raise ValueError('PAPER_PROTECTION_BINDING_CHANGED')
    if price<=stop:
        reason='STOP_LOSS'
    elif mode=='V2' and not position.get('partial_exit') and price>=target:
        if qty<2:
            reason='TAKE_PROFIT'
        else:
            reason='PARTIAL_2R'
            remaining=max(Decimal(1),qty//2)
            part=True
    elif (v4 or mode in ('PULLBACK','AGGRESSIVE')) and price>=target:
        reason='TAKE_PROFIT'
    elif mode=='AGGRESSIVE' and plan.get('setup')==HELD_GAP:
        gap=entry['smart_eligibility']['entry_evidence']['gap_evidence']
        exit_at=datetime.fromisoformat(gap['exit_at'])
        if (not model or entry['smart_eligibility']['strategy_model']!=model
                or plan.get('time_exit_at')!=gap['exit_at'] or exit_at.tzinfo is None
                or exit_at<=datetime.fromisoformat(entry['at'])):
            raise ValueError('PAPER_PROTECTION_BINDING_CHANGED')
        if close_at>=exit_at:
            reason='STRATEGY_TIME_EXIT'
    elif v4 or mode=='AGGRESSIVE' and plan.get('model'):
        from paper_v5 import DAILY_PULLBACK_MODEL, FOUR_HOUR_MODEL, HOLD_BY_VERSION, HOLD_MINUTES
        minutes=HOLD_MINUTES if v4 else HOLD_BY_VERSION.get(plan['model'])
        if model==FOUR_HOUR_MODEL:
            from paper_v5_four_hour import position_minutes
            minutes=position_minutes(entry)
        if minutes is None or plan.get('hold_minutes')!=minutes:
            raise ValueError('PAPER_PROTECTION_BINDING_CHANGED')
        basis=entry['bar_close_at'] if model in (FOUR_HOUR_MODEL,DAILY_PULLBACK_MODEL) else entry['at']
        if close_at.timestamp()-datetime.fromisoformat(basis).timestamp()>=minutes*60:
            reason='STRATEGY_TIME_EXIT'
    elif mode=='AGGRESSIVE':
        import pandas as pd
        from timeframes import schedule
        opened=pd.Timestamp(datetime.fromisoformat(entry['at']))
        closed=pd.Timestamp(close_at)
        zone=position['identity']['timezone']
        sessions=schedule(position['identity'],opened.tz_convert(zone).date(),closed.tz_convert(zone).date())
        minutes=sum(max(0,(min(closed,row.close)-max(opened,row.open)).total_seconds())
                    for row in sessions.itertuples())/60
        if minutes>=240:reason='STRATEGY_TIME_EXIT'
    new_stop=stop
    if mode=='V2' and (position.get('partial_exit') or price>=entry_price+risk):
        new_stop=max(stop,breakeven)
    if mode=='MOMENTUM' and not v4:
        new_stop=max(stop,price-Decimal('1.5')*Decimal(plan['atr']))
    state=None
    if new_stop>stop and not reason and last.get('quote_at')!=bar['close_at']:
        state=dict(quote_at=bar['close_at'],stop=str(new_stop),prior_stop=str(stop))
    return reason,remaining,part,state


def management_key(request_id):
    return 'paper_position_stop:'+request_id


def write_management(db, state, entry_id):
    import json
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
        (management_key(entry_id),json.dumps(state)))


def decision(db,info,result,now):
    """Immutable entry receipt and verified execution price."""
    from paper_account import ledger,quote,result_portfolio
    from paper_hourly import authority,digest
    from eur_listings import stored_identity
    from datetime import datetime
    portfolio=result_portfolio(db,result)
    cfg=authority(db,info['isin'],result.get('identity'),portfolio=portfolio)
    state=ledger(db,portfolio)
    position=next((p for p in state['positions'] if p['isin']==info['isin'] and Decimal(p['quantity'])>0),None)
    if position is None:return None
    if position['identity']!=result['identity']:raise ValueError('PAPER_PROTECTION_IDENTITY_CHANGED')
    entry=next((f for f in reversed(state['fills']) if f['isin']==info['isin'] and f['side']=='BUY'),None)
    if entry is None:raise ValueError('PAPER_PROTECTION_ENTRY_UNCONFIRMED')
    plan=entry.get('smart_eligibility',{}).get('protection')
    if not plan:return None
    strategy=plan.get('version')==STRATEGY_VERSION
    if not strategy and not enabled(cfg):return None
    if (plan.get('version') not in (VERSION,STRATEGY_VERSION) or plan.get('identity')!=result['identity']
        or plan.get('signal_tf')!=entry.get('timeframe') or plan.get('trigger')!=POLICY['trigger']
        or plan.get('fill')!=POLICY['fill']):raise ValueError('PAPER_PROTECTION_BINDING_CHANGED')
    if plan['signal_tf']!=result['timeframe']:return None
    if strategy and plan.get('strategy_mode')!=(position.get('auto_entry') or {}).get('strategy_mode'):
        raise ValueError('PAPER_PROTECTION_BINDING_CHANGED')
    if result.get('quote_blocker'):raise ValueError(result['quote_blocker'])
    q=result.get('execution_quote',{})
    from paper_v2_quotes import bound_identity
    origin=bound_identity(db,info,result)
    _,bar=quote(info,q,now,stored_listing=stored_identity(db,info,origin,q.get('timeframe')))
    close_at=datetime.fromisoformat(bar['close_at']).timestamp()
    if close_at<=datetime.fromisoformat(entry['at']).timestamp():return None
    from paper_v1_long_term import MODEL as V1_MODEL
    stop=Decimal(plan['stop'])
    v1 = strategy and plan.get('model') == V1_MODEL
    target=None if v1 else Decimal(plan['target'])
    if (not stop.is_finite() or stop<=0
            or target is not None and (not target.is_finite() or target<=stop)):
        raise ValueError('PAPER_PROTECTION_LEVEL_INVALID')
    price=Decimal(str(bar['close']))
    if strategy:
        reason,qty,partial,management=strategy_trigger(db,position,entry,plan,bar,price,result=result)
        if management:
            return dict(management=management,entry_request_id=entry['request_id'])
    else:
        reason='STOP_LOSS' if price<=stop else 'TAKE_PROFIT' if price>=target else None
        qty=Decimal(position['quantity'])
        partial=False
    if not reason:return None
    if plan.get('model') and Decimal(str(bar['volume']))<qty:
        raise ValueError('PAPER_V5_QUOTE_VOLUME_BELOW_SIZE')
    key=[cfg['epoch'],info['isin'],entry['request_id']]
    if partial:key.append('partial')
    return dict(side='SELL',reason=reason,entry_request_id=entry['request_id'],
        request_id='protect-'+digest(key),plan_digest=digest(plan),quote_at=bar['close_at'],
        reference=str(price),quantity=str(qty),trigger=POLICY['trigger'])
