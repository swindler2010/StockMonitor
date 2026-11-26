"""Owner-enabled hourly demo runner. Reuses CORE, Dispatcher and paper executor.

No feed, broker, inferred gap permission or market orders.
One position per authorized instrument; deterministic operations per signal bar.
"""
import copy
import hashlib
import json
import sqlite3
import time
from decimal import Decimal

from paper_account import (ACCOUNT, AUTO_PORTFOLIOS_SETTING, PORTFOLIO_SETTING, VIEW_PORTFOLIO_SETTING,
                           entry_status_key, independent_portfolios, maintained_positions, operation_rows,
                           portfolio_config, portfolio_epoch_key, portfolios_enabled, result_portfolio,
                           selected_portfolio)
import paper_v1_long_term as long_term
from paper_entry_modes import V2_TIMEFRAME
import paper_v5_daily_pullback as daily_pullback
import paper_v5_four_hour

CONFIG = 'paper_auto_1h'
EPOCH = 'paper_epoch'
STATUS = 'paper_auto_status:'
VERSION = 'core_hourly_demo_v1'
SIZING = 'cash_volatility_v1'
TIMEFRAME_POLICY = 'strategy_auto_v2'
SIGNAL_TIMEFRAMES = ('1H', '4H')
SELECTABLE_TIMEFRAMES = ('15m', '30m', '1H', '4H', '1D')
V5_CONTEXT_TIMEFRAMES = ('5m', '10m', '2H', '3H', '1W', '1M')
LEGACY_TIMEFRAME_KEY = 'signal_timeframe'
STRATEGY_KEY = 'strategy_mode'
LEGACY_V5_PROFILE_KEY = 'v5_entry_profile'
STRATEGY_TIMEFRAMES = {'V1': SIGNAL_TIMEFRAMES, 'V2': (V2_TIMEFRAME,),
                       'PULLBACK': SIGNAL_TIMEFRAMES, 'MOMENTUM': ('1H',),
                       'AGGRESSIVE': ('1H','4H','30m','15m','1D')}


def configured_strategy(cfg):
    from paper_entry_modes import STRATEGY_CODES
    mode=cfg.get(STRATEGY_KEY,'V1')
    if mode not in STRATEGY_CODES:
        raise ValueError('PAPER_AUTO_STRATEGY_BLOCKED')
    return mode


def configured_timeframes(cfg):
    """Priority order for new entries. Ignore any saved manual interval."""
    if paper_v5_four_hour.enabled(cfg):
        return ('4H','1D') if daily_pullback.enabled(cfg) else ('4H',)
    if long_term.enabled(cfg):
        return ('1D',)
    return STRATEGY_TIMEFRAMES[configured_strategy(cfg)]


def v5_rule_version(cfg, timeframe='4H'):
    from paper_v5 import UNIFIED_VERSION
    if paper_v5_four_hour.enabled(cfg):
        if timeframe=='1D' and daily_pullback.enabled(cfg):
            return daily_pullback.MODEL
        return paper_v5_four_hour.MODEL
    return UNIFIED_VERSION


def set_trading_mode(db, strategy):
    configured_strategy({STRATEGY_KEY:strategy})
    db.execute('BEGIN IMMEDIATE')
    cfg = read(db, CONFIG, {})
    if independent_portfolios(db) is not None:
        from paper_entry_modes import STRATEGY_CODES
        view=STRATEGY_CODES[strategy]
        if selected_portfolio(db)!=view:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(VIEW_PORTFOLIO_SETTING,json.dumps(view)))
        return strategy
    identities=cfg.get('identities',{})
    if not identities:
        raise ValueError('PAPER_AUTO_NOT_AUTHORIZED')
    authority(db,next(iter(identities)),allow_paused=True)
    use_long_term = strategy == 'V1' and not long_term.enabled(cfg)
    if (cfg.get(STRATEGY_KEY)!=strategy or LEGACY_TIMEFRAME_KEY in cfg
            or LEGACY_V5_PROFILE_KEY in cfg or use_long_term):
        cfg[STRATEGY_KEY]=strategy
        if strategy == 'V1':
            cfg[long_term.PROFILE_KEY] = long_term.MODEL
        cfg.pop(LEGACY_TIMEFRAME_KEY,None)
        cfg.pop(LEGACY_V5_PROFILE_KEY,None)
        db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(cfg), CONFIG))
    return strategy


def activate_v5_four_hour(db):
    db.execute('BEGIN IMMEDIATE')
    cfg=read(db,CONFIG,{})
    identities=cfg.get('identities',{})
    if not identities:
        raise ValueError('PAPER_AUTO_NOT_AUTHORIZED')
    authority(db,next(iter(identities)),allow_paused=True)
    if configured_strategy(cfg)!='AGGRESSIVE':
        raise ValueError('PAPER_AUTO_STRATEGY_BLOCKED')
    if not paper_v5_four_hour.enabled(cfg):
        cfg[paper_v5_four_hour.PROFILE_KEY]=paper_v5_four_hour.MODEL
        db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(cfg),CONFIG))
    return paper_v5_four_hour.MODEL


def activate_v5_daily_pullback(db):
    db.execute('BEGIN IMMEDIATE')
    cfg=read(db,CONFIG,{})
    identities=cfg.get('identities',{})
    if not identities:
        raise ValueError('PAPER_AUTO_NOT_AUTHORIZED')
    authority(db,next(iter(identities)),allow_paused=True)
    if not paper_v5_four_hour.enabled(cfg) or not variable_size(cfg):
        raise ValueError('PAPER_AUTO_STRATEGY_BLOCKED')
    if not daily_pullback.enabled(cfg):
        cfg[daily_pullback.PROFILE_KEY]=daily_pullback.MODEL
        db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(cfg),CONFIG))
    return daily_pullback.MODEL


def set_v5_individual_rules(db, revision, rules):
    db.execute('BEGIN IMMEDIATE')
    cfg=read(db,CONFIG,{})
    identities=cfg.get('identities',{})
    if not identities:
        raise ValueError('PAPER_AUTO_NOT_AUTHORIZED')
    authority(db,next(iter(identities)),allow_paused=True)
    if not paper_v5_four_hour.enabled(cfg):
        raise ValueError('PAPER_AUTO_STRATEGY_BLOCKED')
    if not isinstance(revision,str) or not revision.strip():
        raise ValueError('PAPER_V5_INDIVIDUAL_RULES_INVALID')
    paper_v5_four_hour.validate_individual_rules(rules,identities)
    if rules:
        cfg[paper_v5_four_hour.INDIVIDUAL_KEY]=dict(revision=revision,rules=rules)
    else:
        cfg.pop(paper_v5_four_hour.INDIVIDUAL_KEY,None)
    db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(cfg),CONFIG))


def signal_allowed(cfg, timeframe, side):
    # Existing automatic positions can exit on their own entry interval.
    return (timeframe in configured_timeframes(cfg)
            and (side=='BUY' or side=='SELL' and timeframe=='1H'))


def turn_timeframes(cfg, held):
    if held and held.get('auto_entry'):
        return (held['auto_entry']['timeframe'],)
    protection_tf = (held.get('protection') or {}).get('signal_tf') if held else None
    if protection_tf in SELECTABLE_TIMEFRAMES:
        return (protection_tf,)
    return configured_timeframes(cfg)


def strategy_signal(cfg, result, held=None):
    engine=result['execution_context']['trading_engine']
    tf=result['timeframe']
    original=(held or {}).get('auto_entry')
    mode=original['strategy_mode'] if original else 'V1' if held and held.get('protection') else configured_strategy(cfg)
    if engine.get('status')!='READY':
        return mode,dict(action='BLOCKED',reason=engine.get('reason','CORE_DATA_BLOCKED'))
    if original and original['timeframe']!=tf:
        return mode,dict(action='WAIT',reason='PAPER_POSITION_ENTRY_TIMEFRAME')
    if mode=='V1':
        if original and original.get('strategy_model') == long_term.MODEL:
            decision = dict(action='HOLD', reason='V1_DAILY_TRAILING_STOP')
        elif not held and long_term.enabled(cfg):
            decision = long_term.decision(engine) if tf=='1D' else dict(action='WAIT',reason='V1_DAILY_ONLY')
        else:
            decision=dict(action=engine.get('signal','WAIT'),reason=engine.get('reason',''))
    elif mode=='AGGRESSIVE' and not held:
        from paper_v5 import entry_decision
        if engine.get('demo_initialization') and not engine['demo_initialization'].get('consistent'):
            return mode,dict(action='BLOCKED',reason='CORE_SEED_UNSTABLE')
        if paper_v5_four_hour.enabled(cfg):
            if tf=='4H':
                decision=paper_v5_four_hour.decision(engine,result.get('execution_quote'),
                    reclaim=paper_v5_four_hour.reclaim_enabled(cfg,result['isin']))
                decision=paper_v5_four_hour.individual_decision(cfg,result,decision)
            elif tf=='1D' and daily_pullback.enabled(cfg):
                decision=daily_pullback.decision(engine,result.get('execution_quote'))
            else:
                decision=dict(action='WAIT',reason='V5_4H_TIMEFRAME_ONLY')
        else:
            decision=entry_decision(engine,tf,result.get('execution_quote'))
    elif mode=='MOMENTUM' and not original:
        from paper_v4 import decide
        if engine.get('demo_initialization') and not engine['demo_initialization'].get('consistent'):
            return mode,dict(action='BLOCKED',reason='CORE_SEED_UNSTABLE')
        decision=decide(engine,tf)
    elif mode=='V2' and held:
        return mode,dict(action='HOLD',reason='PAPER_POSITION_PROTECTION')
    else:
        from paper_entry_modes import decision as evaluate
        decision=evaluate(engine,mode,tf,legacy_position=bool(original and not original.get('strategy_model')))
    if original and original.get('strategy_model') and mode in ('AGGRESSIVE','MOMENTUM'):
        return mode,dict(action='HOLD',reason='V5_POSITION_MANAGED_BY_BRACKET')
    if original and decision['action']=='BUY' and engine.get('timestamp')!=original['signal_at']:
        return mode,dict(decision,action='HOLD',reason='PAPER_AUTO_POSITION_LIMIT')
    return mode,decision


def variable_size(cfg):
    policy = cfg.get('sizing', {})
    return (policy == dict(version=SIZING, risk_fraction='0.01', allocation_fraction='0.45')
            and cfg.get('target_equity') == '20000.00'
            and 'quantity' not in cfg and 'max_position' not in cfg)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def read(db, key, default=None):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def authority(db, isin, identity=None, *, allow_paused=False, portfolio=None):
    from paper_account import ACCOUNT
    cfg = read(db, CONFIG, {})
    if (cfg.get('version') != VERSION or (cfg.get('enabled') is not True and not allow_paused)
            or cfg.get('approved') is not True or cfg.get('account') != ACCOUNT
            or cfg.get('timeframe') != '1H'
            or not (variable_size(cfg) or ('sizing' not in cfg and cfg.get('quantity') == 1 and cfg.get('max_position') == 1))
            or cfg.get('epoch') != read(db, EPOCH)
            or isin not in cfg.get('identities', {})):
        raise ValueError('PAPER_AUTO_NOT_AUTHORIZED')
    configured_strategy(cfg)
    exact = cfg['identities'][isin]
    if (exact.get('source') != 'gettex_chart' or exact.get('venue') != 'GETTEX'
            or exact.get('currency') != 'EUR' or exact.get('timezone') != 'Europe/Berlin'
            or (identity is not None and identity != exact)):
        raise ValueError('PAPER_AUTO_IDENTITY_BLOCKED')
    return portfolio_config(cfg,portfolio) if portfolio and independent_portfolios(db) is not None else cfg


def authorize_hourly(db, isin, identity):
    # Historical-risk analysis may examine the one owner-approved preparation
    # candidate. Execution still requires authority(), which excludes it.
    try:authority(db, isin, identity, allow_paused=True)
    except ValueError:
        from paper_preparation import candidate_authority
        candidate_authority(db, isin, identity)


def ledger_token(db,portfolio=None):
    return digest(operation_rows(db,portfolio or selected_portfolio(db),revision_only=True))


def order_size(db, info, result, side, now, *, allow_paused=False, strategy=None, protective=None):
    """Integer entry size from free cash and existing CORE volatility/structure.

    This is a sizing estimate, not a stop order or a guaranteed loss ceiling.
    CORE decides entries/exits; an explicit optional PAPER bracket can add
    protective exits. No leverage, averaging or new indicators.
    """
    from paper_account import ledger, quote, POLICY, money
    from eur_listings import stored_identity
    portfolio=result_portfolio(db,result)
    cfg = authority(db,info['isin'],result.get('identity'),allow_paused=allow_paused,portfolio=portfolio)
    state = ledger(db,portfolio)
    held = [p for p in state['positions'] if p['isin'] == info['isin'] and Decimal(p['quantity'])]
    if side == 'SELL':
        if len(held) != 1 or held[0]['identity'] != result['identity']:
            raise ValueError('PAPER_INSUFFICIENT_POSITION')
        qty = Decimal(protective['quantity']) if protective else Decimal(held[0]['quantity'])
        from paper_v4 import VERSION as V4_MODEL
        if (not variable_size(cfg) and qty!=1
                and (held[0].get('auto_entry') or {}).get('strategy_model')!=V4_MODEL):
            raise ValueError('PAPER_INSUFFICIENT_POSITION')
        return dict(quantity=int(qty) if qty == int(qty) else float(qty),
            mode='PARTIAL_POSITION' if qty < Decimal(held[0]['quantity']) else 'FULL_POSITION')
    if side != 'BUY':
        raise ValueError('PAPER_AUTO_SIGNAL_MISMATCH')
    if held:
        raise ValueError('PAPER_AUTO_POSITION_LIMIT')
    if not variable_size(cfg) and strategy is None:
        return dict(quantity=1, mode='FIXED_ONE')
    q = result.get('execution_quote', {})
    from paper_v2_quotes import bound_identity
    quote_identity=bound_identity(db,info,result)
    _, bar = quote(info, q, now,
        stored_listing=stored_identity(db, info, quote_identity, q.get('timeframe')))
    v1 = strategy is not None and strategy.get('model') == long_term.MODEL
    if v1 and (not long_term.enabled(cfg) or result['timeframe'] != '1D'):
        raise ValueError('PAPER_AUTO_STRATEGY_BLOCKED')
    if strategy and strategy.get('model') and configured_strategy(cfg)=='V2':
        from paper_v2_30m import size as frozen_size
        return frozen_size(db,info,result,strategy,now)
    if strategy and strategy.get('model') and not v1:
        from datetime import datetime, timezone
        from paper_v5 import (PORTFOLIO_MAX_DISTANCE, MAX_SHARES, HELD_GAP,
                              GAP_TARGET_ATR, TARGET_ATR,
                              held_gap_quote_blocker, macro_release_day, levels, reversal_level_lost)
        from paper_v4 import MODE as V4_MODE, VERSION as V4_MODEL
        mode,timeframe=configured_strategy(cfg),result['timeframe']
        model=strategy['model']
        v5 = mode=='AGGRESSIVE' and model==v5_rule_version(cfg,timeframe)
        if timeframe=='1D' and paper_v5_four_hour.enabled(cfg):
            v5 = v5 and model==daily_pullback.MODEL
        four_hour = v5 and model==paper_v5_four_hour.MODEL and timeframe=='4H'
        daily = v5 and model==daily_pullback.MODEL and timeframe=='1D'
        v4 = mode==V4_MODE and model==V4_MODEL
        if timeframe not in configured_timeframes(cfg) or not (v5 or v4):
            raise ValueError('PAPER_AUTO_STRATEGY_BLOCKED')
        if v5 and daily_pullback.enabled(cfg):
            latest_sell=next((f for f in reversed(state['fills'])
                              if f['isin']==info['isin'] and f['side']=='SELL'),None)
            if latest_sell and datetime.fromisoformat(latest_sell['bar_close_at'])==datetime.fromisoformat(bar['close_at']):
                raise ValueError('PAPER_V5_SAME_QUOTE_REENTRY')
        if v5 or v4:
            closed = strategy.get('signal_closed')
            if not closed or datetime.fromisoformat(bar['close_at'])<datetime.fromisoformat(closed):
                raise ValueError('PAPER_V4_QUOTE_BEFORE_SIGNAL' if model==V4_MODEL else 'PAPER_QUOTE_BEFORE_STRATEGY_SIGNAL')
        cash=Decimal(state['cash'])
        reference=Decimal(str(bar['close']))
        macro=macro_release_day(datetime.fromtimestamp(now,timezone.utc))
        if macro['state']=='RELEASE_DAY':
            raise ValueError('PAPER_V5_MACRO_RELEASE_DAY:'+macro['kind'])
        if four_hour or daily:
            policy=daily_pullback if daily else paper_v5_four_hour
            blocker=policy.quote_blocker(strategy,bar,datetime.fromtimestamp(now,timezone.utc))
            if blocker:
                raise ValueError(blocker)
        if v5 and strategy['setup']==HELD_GAP:
            blocker=held_gap_quote_blocker(strategy['gap_evidence'],bar,datetime.fromtimestamp(now,timezone.utc))
            if blocker:
                raise ValueError(blocker)
        if model==V4_MODEL and strategy['setup']=='POST_EARNINGS' and reference<=Decimal(str(strategy['entry_level'])):
            raise ValueError('PAPER_V4_EARNINGS_LEVEL_LOST')
        if reversal_level_lost(strategy,reference):
            raise ValueError('PAPER_V5_REVERSAL_LEVEL_LOST')
        if v5 and not daily and timeframe!='1H' and reference<=Decimal(str(strategy['entry_level'])):
            raise ValueError('PAPER_V5_ENTRY_LEVEL_LOST')
        if v5 and timeframe=='4H' and reference-Decimal(str(strategy['entry_level']))>PORTFOLIO_MAX_DISTANCE*Decimal(str(strategy['atr'])):
            raise ValueError('PAPER_V5_4H_ENTRY_TOO_FAR')
        half=Decimal(POLICY['spread_bps'])/Decimal(20000)
        price=reference*(1+half)
        fee=money(POLICY['commission'])
        qty=min(MAX_SHARES,int(max(Decimal(0),cash-fee)//price))
        while qty>0 and money(price*qty)+fee>cash:
            qty-=1
        if qty<1:
            raise ValueError('PAPER_AUTO_SIZE_BELOW_ONE')
        if Decimal(str(bar['volume']))<qty:
            raise ValueError('PAPER_V5_QUOTE_VOLUME_BELOW_SIZE')
        target_atr=paper_v5_four_hour.TARGET_ATR if four_hour else TARGET_ATR
        if strategy['setup']==HELD_GAP:
            target_atr=GAP_TARGET_ATR
        atr=Decimal(str(strategy['atr']))
        stop,target=(daily_pullback.levels(reference,atr) if daily else levels(reference,atr,target_atr=target_atr))
        if not 0<stop<reference<target:
            raise ValueError('PAPER_STRATEGY_LEVELS_INVALID_AT_FILL')
        risk=(price-stop*(1-half))*qty+2*fee
        return dict(quantity=qty,mode=model,cash_basis=str(cash),allocation_limit=str(cash),
                    stop=str(stop),target=str(target),estimated_risk=str(risk),macro_calendar=macro,
                    gross_and_fee=str(money(price*qty)+fee),risk_is_estimate=True)
    facts = (strategy if v1 else result.get('execution_context', {}).get('trading_engine', {})
             .get('timeframe_context', {}).get(result['timeframe'], {}).get('facts', {}))
    try:
        atr, support = (Decimal(str(facts[k])) for k in ('atr', 'support'))
        if not all(v.is_finite() and v > 0 for v in (atr, support)):
            raise ValueError()
    except (KeyError, ValueError, ArithmeticError):
        raise ValueError('PAPER_AUTO_SIZING_UNCONFIRMED') from None
    cash = Decimal(state['cash']);fee = money(POLICY['commission'])
    reference = Decimal(str(bar['close']))
    half = Decimal(POLICY['spread_bps'])/Decimal(20000)
    price = reference*(1+half)
    sizing=cfg.get('sizing',{})
    allocation = money(cash*Decimal(sizing.get('allocation_fraction','0.45')))
    risk_fraction=Decimal(sizing.get('risk_fraction','0.01'))
    if v1:
        stop = long_term.initial_stop(strategy, bar)
        if not 0 < stop < reference:
            raise ValueError('PAPER_STRATEGY_LEVELS_INVALID_AT_FILL')
        distance = price - stop*(1-half)
    elif strategy is None:
        # Two ATR or distance below existing support, whichever is larger, plus spread.
        distance=max(2*atr,reference-support*Decimal('.995'))+2*reference*half
    else:
        stop,target,minimum=(Decimal(str(strategy[k])) for k in ('stop','target','entry_rr'))
        if not (0<stop<reference<target):
            raise ValueError('PAPER_STRATEGY_LEVELS_INVALID_AT_FILL')
        distance=price-stop*(1-half)
        risk_fraction=min(risk_fraction,Decimal(str(strategy['risk_fraction'])))
    risk_budget = money(cash*risk_fraction)
    qty = min(int((allocation-fee)//price), int((risk_budget-2*fee)//distance))
    if not variable_size(cfg):
        qty=min(qty,1)
    if v1:
        qty=min(qty,int(bar['volume']))
    while qty > 0 and (money(price*qty)+fee > min(cash, allocation)
            or distance*qty+2*fee > risk_budget):
        qty -= 1
    if qty < 1:
        raise ValueError('PAPER_AUTO_SIZE_BELOW_ONE')
    if strategy is not None and not v1:
        reward=(target*(1-half)-price)*qty-2*fee
        if reward < minimum*(distance*qty+2*fee):
            raise ValueError('PAPER_STRATEGY_REWARD_TOO_LOW_AFTER_COSTS')
    size = dict(quantity=qty, mode=SIZING if variable_size(cfg) else 'FIXED_ONE',
        cash_basis=str(cash), allocation_limit=str(allocation), risk_budget=str(risk_budget),
        reference_risk_per_share=str(distance), estimated_risk=str(distance*qty+2*fee),
        gross_and_fee=str(money(price*qty)+fee), risk_is_estimate=True, target_equity=cfg.get('target_equity'))
    if v1:
        size.update(stop=str(stop), entry=str(price))
    return size


def fresh_quote(store, info, identity, now):
    """Latest closed native 15m quote, independent of the signal interval."""
    import pandas as pd
    import data_quality as dq
    from analytics import bar_ends
    from eur_listings import stored_identity
    from paper_account import EXECUTION_TF, quote, POLICY
    tf=EXECUTION_TF
    candidates=[]
    with store.connect() as db:
        store.read_cache.depend('market:' + info['isin'] + ':' + tf)
        store.read_cache.depend('setting:gettex_chart:' + info['isin'] + ':' + tf)
        rows = db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE '
            'isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? '
            'ORDER BY ts DESC LIMIT 4', (info['isin'], identity['source'], identity['venue'],
                identity['currency'], identity['symbol'], tf)).fetchall()
        for ts, *vals in rows:
            frame = pd.DataFrame([vals], columns=dq.FIELDS, index=pd.DatetimeIndex([pd.Timestamp(ts)]))
            frame.attrs = dict(identity, timeframe=tf)
            end = bar_ends(frame)[0]
            if pd.notna(end) and end <= now:
                candidates.append((end, frame))
        if not candidates:
            raise ValueError('PAPER_NO_CLOSED_BAR')
        end, frame = max(candidates, key=lambda c: c[0])
        if not 0 <= (now-end).total_seconds() <= POLICY['max_bar_age_seconds']:
            raise ValueError('PAPER_STALE_QUOTE')
        quality = dq.audit(frame, tf, identity, now)
        bar = {**{k:float(frame.iloc[0][k]) for k in dq.FIELDS},
            'closed': True, 'open_at': frame.index[0].tz_localize(identity['timezone']).isoformat(),
            'close_at': end.isoformat()}
        result = dict(isin=info['isin'], identity=identity, timeframe=tf, data_quality=quality, bars=[bar])
        quote(info, result, time.time(), stored_listing=stored_identity(db, info, identity, tf))
        return result


def screen_v5_entry(store, info, identity, now, *, reclaim=False, watch=False):
    """Reject impossible entries before running the complete CORE calculation."""
    model = paper_v5_four_hour
    calendar, window = model.entry_window(identity, now, reclaim=reclaim)
    evidence = (model.closed_setup(store,info,identity,calendar,window,{}, {})
                if window else {'reason':'V5_4H_OUTSIDE_OPENING_WINDOW'})
    quote = {}
    if not evidence.get('reason'):
        try:
            quote = dict(execution_quote=fresh_quote(store,info,identity,now))
        except ValueError as error:
            quote['quote_blocker'] = str(error)
        bars=quote.get('execution_quote',{}).get('bars',[])
        if not bars or model.quote_blocker(evidence,bars[-1]):
            quote['quote_request']=dict(identity=identity, close_at=evidence['quote_end'])
    if watch:
        trend=evidence.get('weekly_trend')
        if trend is None:
            # Observe changes in direction outside the order window too.
            # This receipt has no role in authorization.
            with store.connect() as db:
                trend=model.weekly_context(store,db,info,identity,window[1] if window else now,
                                           {},include_recovery=True)
        evidence['trend_watch']=model.trend_watch(trend)
    decision=model.decision({'v5_four_hour':evidence},quote.get('execution_quote'),reclaim=reclaim)
    if decision['action']=='BUY':
        return None
    return dict(isin=info['isin'],identity=identity,timeframe='4H',_v5_entry_screen=True,
        execution_context=dict(trading_engine=dict(status='NOT_EVALUATED',signal='WAIT',
            timestamp=window[0].isoformat() if window is not None else None,
            reason=decision['reason'],v5_four_hour=evidence)),**quote)


def screen_daily_pullback(store, info, identity, cfg, now, inputs):
    import pandas as pd

    window=daily_pullback.opening_window(identity,now)
    if window is None:
        return {'reason':'V5_D1_OUTSIDE_OPENING_WINDOW'},{}
    evidence=daily_pullback.context(store,info,identity,cfg,window,inputs)
    if evidence.get('reason'):
        return evidence,{}
    request=dict(identity=identity,close_at=evidence['quote_end'])
    try:
        quote=fresh_quote(store,info,identity,now)
    except ValueError as error:
        return dict(evidence,reason=daily_pullback.WAIT_QUOTE),dict(quote_blocker=str(error),quote_request=request)
    bar=quote['bars'][-1]
    if pd.Timestamp(bar['close_at'])!=window[-1]:
        return dict(evidence,reason=daily_pullback.WAIT_QUOTE),dict(execution_quote=quote,quote_request=request)
    reason=daily_pullback.quote_blocker(evidence,bar,now)
    if reason:
        evidence['reason']=reason
    return evidence,{'execution_quote':quote}


def calculate(store, values, *, now=None, context_only=False, quote_cache=None):
    """Owned readonly worker: exact closed CORE context and existing proof."""
    import pandas as pd
    from calculation_window import context_deadline
    from trading_context import plan,calculate as calculate_context
    from paper_account import ledger
    info = values['watchlist'][0]
    for key in (CONFIG, EPOCH):
        store.read_cache.depend('setting:' + key)
    store.read_cache.depend('instrument:' + info['isin'])
    with store.connect() as db:
        preparing = values.get('_paper_prepare') is True
        split=portfolios_enabled(db)
        ready=independent_portfolios(db)
        selected=selected_portfolio(db)
        portfolio=ACCOUNT if preparing and ready is not None else values.get('_paper_portfolio') or selected
        manage_only=values.get('_paper_manage_only') is True
        if portfolio!=selected and not manage_only and ready is None:
            raise ValueError('PAPER_PORTFOLIO_CHANGED')
        if preparing:
            from paper_preparation import candidate_authority, STATE
            store.read_cache.depend('setting:'+STATE)
            cfg, identity = candidate_authority(db, info['isin'])
        else:
            cfg = authority(db,info['isin'],allow_paused=bool(values.get('_paper_order')),portfolio=portfolio)
            identity = cfg['identities'][info['isin']]
        if ready is not None:
            store.read_cache.depend('setting:'+AUTO_PORTFOLIOS_SETTING)
        if split:
            store.read_cache.depend('setting:'+PORTFOLIO_SETTING)
            store.read_cache.depend('setting:'+portfolio_epoch_key(portfolio))
        position=held_position(db,info['isin'],portfolio) or {}
        if manage_only and not position:
            raise ValueError('PAPER_AUTO_POSITION_CHANGED')
        position_token=ledger_token(db,portfolio)
    now = pd.Timestamp.now(tz='UTC') if now is None else now

    signal_tf = values.get('timeframe', '1H')
    allowed=V5_CONTEXT_TIMEFRAMES if context_only else SELECTABLE_TIMEFRAMES
    if (signal_tf not in allowed
            or (preparing and signal_tf != '1H')
            or (values.get('_paper_order') and signal_tf not in SIGNAL_TIMEFRAMES)):
        raise ValueError('PAPER_AUTO_TIMEFRAME_BLOCKED')
    reclaim = signal_tf=='4H' and paper_v5_four_hour.reclaim_enabled(cfg,info['isin'])
    if (values.get('_paper_v5_screen') is True and not position and signal_tf=='4H'
            and not context_only and not preparing and not values.get('_paper_order')
            and paper_v5_four_hour.enabled(cfg)):
        screened=screen_v5_entry(store,info,identity,now,reclaim=reclaim,
                                 watch=values.get('_paper_v5_watch') is True)
        if screened is not None:
            if split:screened['paper_portfolio']=portfolio
            return screened
    peer_inputs={}
    daily_inputs={}
    daily=None
    daily_quote={}
    entry_check=not context_only and not position and not values.get('_paper_order') and not preparing
    if entry_check and signal_tf=='30m' and configured_strategy(cfg)=='V2':
        from paper_v2_30m import screen as frozen_screen
        screened=frozen_screen(store,info,identity,now)
        if screened is not None:
            if split:screened['paper_portfolio']=portfolio
            return screened
    if (entry_check and signal_tf=='1D' and paper_v5_four_hour.enabled(cfg)
            and daily_pullback.enabled(cfg)):
        daily,daily_quote=screen_daily_pullback(store,info,identity,cfg,now,daily_inputs)
        if values.get('_paper_v5_screen') is True and daily.get('reason'):
            return dict(isin=info['isin'],identity=identity,timeframe='1D',_v5_entry_screen=True,
                 **({'paper_portfolio':portfolio} if split else {}),
                execution_context=dict(trading_engine=dict(status='NOT_EVALUATED',signal='WAIT',
                    timestamp=daily.get('signal_day'),reason=daily['reason'],v5_daily_pullback=daily)),
                **daily_quote)
    contract=plan(signal_tf)
    v1 = (signal_tf=='1D' and not context_only and not preparing and not values.get('_paper_order')
          and ((position.get('auto_entry') or {}).get('strategy_model')==long_term.MODEL
               or not position and long_term.enabled(cfg)))
    news_background=None
    if v1:
        from paper_v1_news import evidence as news_evidence
        try:
            with store.connect() as db:
                news_background=news_evidence(db,info,now)
        except sqlite3.Error:
            news_background={'status':'CACHE_UNAVAILABLE'}
    if v1 and not position:
        screened = long_term.screen(store,info,identity,now)
        if screened is not None:
            return dict(isin=info['isin'],identity=identity,timeframe=signal_tf,_v1_entry_screen=True,
                news_background=news_background,**({'paper_portfolio':portfolio} if split else {}),
                execution_context=dict(trading_engine=dict(status='NOT_EVALUATED',signal='WAIT',
                    timestamp=screened.get('signal_day'),reason=screened['reason'],v1_long_term=screened)))
    context_values = dict(values, _paper_v1_long_term=True) if v1 else values
    calculated=calculate_context(store,context_values,identity,contract,position,now)
    engine,proof,manual=(calculated[k] for k in ('engine','proof','manual'))
    strength = values.get('_paper_v1_strength') or {}
    v1_context = engine.get('v1_long_term')
    if v1_context is not None and v1_context.get('signal_day'):
        stamp = engine.get('timestamp')
        if stamp is None or str(stamp)[:10] != v1_context['signal_day']:
            engine['v1_long_term'] = {'reason':'V1_DAILY_INPUT_CHANGED'}
        elif strength.get('signal_day')==v1_context['signal_day']:
            v1_context['relative_strength'] = strength
    if entry_check and engine.get('status')=='READY':
        mode=configured_strategy(cfg)
        four_hour=paper_v5_four_hour.enabled(cfg)
        if four_hour:
            if signal_tf=='4H':
                engine['v5_four_hour']=paper_v5_four_hour.context(
                    store,info,identity,engine.get('timestamp'),now,peer_inputs,daily_inputs,reclaim=reclaim)
            elif daily is not None:
                engine['v5_daily_pullback']=daily
        elif mode=='AGGRESSIVE' and signal_tf=='1H':
            from paper_v5 import context as opening_range
            engine['v5_opening_range']=opening_range(store,info,identity,engine.get('timestamp'),now,
                cfg['identities'],peer_inputs=peer_inputs)
        elif mode=='MOMENTUM' or mode=='AGGRESSIVE' and engine.get('signal')=='BUY':
            from paper_v4 import context as selected_context
            engine['v4_selected_context']=selected_context(store,info,identity,engine.get('timestamp'),now,
                cfg['identities'],signal_tf,post_earnings=mode=='MOMENTUM',
                peer_inputs=peer_inputs if mode=='AGGRESSIVE' else None)
        if mode=='AGGRESSIVE' and not four_hour:
            from paper_v5 import decide, held_gap_window, portfolio_context
            window=held_gap_window(identity,now) if signal_tf=='1D' else None
            confirmed=engine.get('signal')=='BUY' and decide(engine,signal_tf)['action']=='BUY'
            if confirmed or window is not None:
                engine['v5_portfolio_context']=portfolio_context(
                    store,info,identity,now,signal_tf,daily_inputs,gap_window=window)
    result = dict(isin=info['isin'], timeframe=signal_tf, identity=identity,
        execution_context=dict(status='MANUAL_RISK_ACCEPTED' if manual else 'HOURLY_DEMO_READY',
            isin=info['isin'], checked_at=now.isoformat(), analysis_ready=engine.get('status') == 'READY',
            trading_engine=engine, manual_risk=manual, manual_inputs=proof),
        hourly=dict(version=VERSION, config_digest=digest(cfg), ledger_token=position_token,
            epoch=cfg['epoch'], checked_at=now.isoformat(),
            expires_at=min(now+pd.Timedelta(seconds=60), context_deadline(identity, signal_tf, now)).isoformat(),
            proof=proof, required_tf=contract['required_tf'],timeframe_contract=contract))
    if split:
        result.update(paper_portfolio=portfolio,paper_manage_only=manage_only)
    if news_background is not None:
        result['news_background']=news_background
    if preparing:result['hourly']['preparation_only']=True
    if peer_inputs:result['_peer_inputs']=peer_inputs
    if daily_inputs:result['_v5_daily_inputs']=daily_inputs
    if context_only:
        return result
    # Analysis is useful even while prices are stale. Its status cannot place a fill.
    try:
        from paper_orders import check_intent
        order = values.get('_paper_order')
        if order:
            check_intent(order, info['isin'])
            result['discretionary'] = copy.deepcopy(order)
        if daily_quote:
            result.update(daily_quote)
        elif (not order and configured_strategy(cfg)=='V2' and signal_tf=='30m'):
            from paper_v2_quotes import fresh_quote as v2_quote
            result['execution_quote']=v2_quote(store,info,identity,now)
        elif quote_cache is None:
            result['execution_quote'] = fresh_quote(store, info, identity, now)
        else:
            # All execution profiles have the same independent 15m quote.
            # Depend on the same scopes for each profile, including reuses.
            if not quote_cache:
                with store.read_cache.track_inputs() as quote_inputs:
                    try:
                        quote_cache['execution_quote']=fresh_quote(store,info,identity,now)
                    except ValueError as error:
                        quote_cache['quote_blocker']=str(error)
                quote_cache['inputs']=dict(quote_inputs)
            store.read_cache._record(quote_cache['inputs'])
            result.update({k:v for k,v in quote_cache.items() if k!='inputs'})
    except ValueError as error:
        result['quote_blocker'] = str(error)
    if entry_check and configured_strategy(cfg)=='V2' and signal_tf=='30m':
        from paper_v2_30m import context as frozen_context
        try:
            engine['v2_30m']=frozen_context(store,info,identity,now,result.get('execution_quote'))
            result['_v2_inputs']=engine['v2_30m'].pop('_v2_inputs',{})
        except ValueError as error:
            engine['v2_30m']=dict(action='BLOCKED',reason=str(error))
    return result


def _refresh_completed_window(result, finished):
    # A slow computation can publish only while its closed input horizon
    # remains exactly the same. Quote freshness is checked separately, at fill.
    import pandas as pd
    from candle_schedule import boundary
    from calculation_window import context_deadline
    from trading_context import closed_period_end

    analysis=result['hourly']
    start=pd.Timestamp(analysis['checked_at'])
    if finished<pd.Timestamp(analysis['expires_at']):
        return
    ttl=start+pd.Timedelta(seconds=60)
    identity=result['identity']
    def closed(tf, instant):
        if tf in ('1W','1M'):
            return closed_period_end(identity,tf,instant)
        return boundary(identity,tf,instant,delay_seconds=0).get('close_at')
    dependencies=set(analysis['required_tf'])|{'1D'}
    for tf in dependencies:
        previous=closed(tf,start)
        deadline=context_deadline(identity,tf,start)
        if (previous is None or previous!=closed(tf,finished)
                or deadline!=ttl and finished>=deadline):
            return
    deadlines=[context_deadline(identity,tf,finished) for tf in dependencies]
    analysis['expires_at']=min(finished+pd.Timedelta(seconds=60),*deadlines).isoformat()
    result['execution_context']['checked_at']=finished.isoformat()


def calculate_snapshot(store, values):
    """One WAL snapshot for a stock, exact revision validation per candidate."""
    import pandas as pd
    from contextlib import contextmanager
    from performance_cache import ReadCache,validate_closed_inputs,InputRevisionChanged,_setting_value
    from cp_controls import manual_gate
    from gettex_first import engine_available
    from paper_account import EXECUTION_TF

    intervals=values.get('_paper_timeframes')
    multi=intervals is not None
    if multi and (tuple(intervals)!=STRATEGY_TIMEFRAMES['AGGRESSIVE']
                  or values.get('_paper_order') or values.get('_paper_prepare')):
        raise ValueError('PAPER_AUTO_TIMEFRAME_BLOCKED')
    intervals=intervals if multi else (values.get('timeframe','1H'),)
    analysis_intervals=(*intervals,*V5_CONTEXT_TIMEFRAMES) if multi else intervals
    results={}
    captures={}
    def failure(tf,error):
        if not multi:
            raise error
        results[tf]={'_calculation_error':type(error).__name__+': '+str(error)}

    def quote_tail(db, result):
        meta=result.get('execution_quote',{}).get('identity') or result['identity']
        if meta.get('source')=='gettex':
            return db.execute('SELECT ts,open,high,low,close,volume FROM candles WHERE '
                'isin=? AND source=? AND base_tf=? ORDER BY ts DESC LIMIT 4',
                (result['isin'],'gettex',EXECUTION_TF)).fetchall()
        return db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE '
            'isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? '
            'ORDER BY ts DESC LIMIT 4',(result['isin'],meta['source'],meta['venue'],meta['currency'],meta['symbol'],EXECUTION_TF)).fetchall()

    captured=copy.copy(store);captured.read_cache=ReadCache(store.path)
    @contextmanager
    def connect():yield captured.read_cache.db
    captured.connect=connect
    try:
        db=captured.read_cache.db
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        now=pd.Timestamp.now(tz='UTC')
        values={**values,'paper_feature_cache':{}} if multi else values
        quote_cache={} if multi else None
        for tf in analysis_intervals:
            try:
                with captured.read_cache.track_inputs() as token:
                    expected=values.get('instrument_revisions',{})
                    captured.read_cache.assert_current(expected);captured.read_cache._record(expected)
                    context_only=tf not in intervals
                    result=calculate(captured,dict(values,timeframe=tf),now=now,
                        context_only=context_only,quote_cache=quote_cache)
                    admission_only=values.get('_paper_prepare') and engine_available(
                        result['execution_context']['trading_engine'],result['hourly']['required_tf'])
                    screened=result.get('_v5_entry_screen') or result.get('_v1_entry_screen') or result.get('_v2_entry_screen')
                    quote_rows=None if context_only or admission_only or screened else quote_tail(db,result)
                    listing='setting:gettex_chart:'+result['isin']+':'+EXECUTION_TF
                    saved=_setting_value(db,listing[len('setting:'):])
                captures[tf]=result,dict(token),quote_rows,listing,saved
            except (ValueError,InputRevisionChanged) as error:
                failure(tf,error)
    finally:
        captured.read_cache.close()

    def validate_one(db,data,memo):
        from paper_v5 import peer_input_current
        result,token,quote_rows,listing,saved=data
        if result.get('_v5_entry_screen') or result.get('_v1_entry_screen') or result.get('_v2_entry_screen'):
            version = 'V1' if result.get('_v1_entry_screen') else 'V2' if result.get('_v2_entry_screen') else 'V5'
            for scope,revision in token.items():
                row=db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone()
                if (row[0] if row else 0)!=revision:
                    raise InputRevisionChanged(version+' entry screen input changed: '+scope)
            if held_position(db,result['isin'],result_portfolio(db,result)):
                raise InputRevisionChanged(version+' entry screen position changed')
            result['_input_revisions']=token
            return result
        peer_inputs=result.pop('_peer_inputs',{})
        daily_inputs=result.pop('_v5_daily_inputs',{})
        v2_inputs=result.pop('_v2_inputs',{})
        from paper_v2_quotes import input_current as v2_input_current
        if not all(v2_input_current(db,value) for value in v2_inputs.values()):
            raise InputRevisionChanged('V2 30m supplemental input changed')
        if v2_inputs:result['hourly']['v2_inputs']=v2_inputs
        proof=result['hourly']['proof']
        current=validate_closed_inputs(db,proof,memo)
        if quote_rows is not None and quote_tail(db,result)!=quote_rows:
            raise InputRevisionChanged('Hourly execution quote changed')
        for scope,revision in token.items():
            if scope in current:
                if current[scope]!=revision and any(
                        scope in inputs and not peer_input_current(db,inputs[scope])
                        for inputs in (peer_inputs,daily_inputs)):
                    raise InputRevisionChanged('PAPER V5 supplemental input changed: '+scope)
                continue
            if scope.startswith('setting:data_quality:'):
                continue
            row=db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone()
            revision_now=row[0] if row else 0
            if revision_now!=revision:
                listing_unchanged=scope==listing and saved==_setting_value(db,scope[len('setting:'):])
                proofs=[basis[scope] for basis in (peer_inputs,daily_inputs) if scope in basis]
                peer_unchanged=bool(proofs) and all(peer_input_current(db,value) for value in proofs)
                v2_unchanged=scope in v2_inputs and v2_input_current(db,v2_inputs[scope])
                if not listing_unchanged and not peer_unchanged and not v2_unchanged:
                    raise InputRevisionChanged('PAPER input changed: '+scope)
            current[scope]=revision_now
        manual=result['execution_context'].get('manual_risk')
        if manual is not None and manual_gate(db,result['isin'],result['identity'],result['timeframe'],proof)!=manual:
            raise InputRevisionChanged('Hourly historical-risk permission changed')
        result['_input_revisions']=current
        if (result['execution_context']['analysis_ready'] and result['timeframe'] in intervals
                and not values.get('_paper_order')):
            _refresh_completed_window(result,pd.Timestamp.now(tz='UTC'))
        return result

    with store.connect() as db:
        db.execute('BEGIN')
        memo={}
        for tf,data in captures.items():
            try:
                results[tf]=validate_one(db,data,memo)
            except (ValueError,InputRevisionChanged) as error:
                failure(tf,error)
    if multi:
        context={}
        for tf in analysis_intervals:
            result=results[tf]
            error=result.get('_calculation_error')
            if error:
                context[tf]=dict(core='BLOCKED',reason=error)
                continue
            engine=result['execution_context']['trading_engine']
            quality=engine.get('timeframe_context',{}).get(tf,{}).get('data_quality',{})
            context[tf]=dict(core=engine.get('status'),signal=engine.get('signal'),
                signal_at=engine.get('timestamp'),reason=engine.get('reason'),quality=quality.get('state'))
        return dict(results={tf:results[tf] for tf in intervals},
                    analysis_timeframes=context,analysis_at=now.isoformat())
    return results[intervals[0]]


def validate_analysis(db, info, result, now, cfg):
    """Shared closed CORE proof; this function cannot authorize an order."""
    import pandas as pd
    from performance_cache import validate_closed_inputs,primary_closed_series
    from calculation_window import benchmark_ready
    from cp_controls import manual_gate
    from trading_engine import VERSION as CORE_VERSION, profile
    h = result.get('hourly', {});contract = result.get('execution_context', {})
    engine = contract.get('trading_engine', {})
    if h.get('version') != VERSION or h.get('config_digest') != digest(cfg) or h.get('epoch') != cfg['epoch']:
        raise ValueError('PAPER_AUTO_CONFIG_CHANGED')
    if h.get('ledger_token') != ledger_token(db,result_portfolio(db,result)):
        raise ValueError('PAPER_AUTO_POSITION_CHANGED')
    clock = pd.Timestamp(now, unit='s', tz='UTC')
    if not pd.Timestamp(h['checked_at']) <= clock < pd.Timestamp(h['expires_at']):
        raise ValueError('PAPER_STALE_WINDOW')
    if engine.get('status') != 'READY' or not contract.get('analysis_ready'):
        raise ValueError('PAPER_ENGINE_DATA_BLOCKED')
    if engine.get('rules_version') != CORE_VERSION:
        raise ValueError('PAPER_AUTO_RULES_CHANGED')
    if engine.get('demo_initialization') and not engine['demo_initialization'].get('consistent'):
        raise ValueError('PAPER_AUTO_SEED_BLOCKED')
    signal_tf = result.get('timeframe')
    if signal_tf not in SELECTABLE_TIMEFRAMES:
        raise ValueError('PAPER_AUTO_TIMEFRAME_BLOCKED')
    if signal_tf != '1H':
        from trading_context import plan
        if h.get('timeframe_contract') != plan(signal_tf):
            raise ValueError('PAPER_AUTO_DEPENDENCY_BLOCKED')
    required = set(profile(signal_tf))
    own = primary_closed_series(h.get('proof', {}),info['isin'],result['identity'])
    if (set(h.get('required_tf', [])) != required
            or not required <= {s['spec'].get('timeframe') for s in own if s.get('count', 0) >= 35}
            or any(s['spec'].get('identity') != result['identity'] for s in own)):
        raise ValueError('PAPER_AUTO_DEPENDENCY_BLOCKED')
    validate_closed_inputs(db, h['proof'])
    manual = None
    if contract.get('status') == 'MANUAL_RISK_ACCEPTED':
        manual = manual_gate(db, info['isin'], result['identity'], signal_tf, h['proof'])
        if manual != contract.get('manual_risk'):
            raise ValueError('PAPER_MANUAL_CONTEXT_CHANGED')
    elif contract.get('status') != 'HOURLY_DEMO_READY' or any(
            engine.get('timeframe_context', {}).get(tf, {}).get('data_quality', {}).get('state') != 'READY'
            and not (engine.get('timeframe_context', {}).get(tf, {}).get('calculation_availability',{}).get('policy')=='gettex_first_v1'
                and engine.get('timeframe_context', {}).get(tf, {}).get('calculation_availability',{}).get('source_contract') is True
                and engine.get('timeframe_context', {}).get(tf, {}).get('calculation_availability',{}).get('analysis_available') is True)
            for tf in required):
        raise ValueError('PAPER_AUTO_DQ_BLOCKED')
    if not benchmark_ready(engine.get('benchmark_context', {})):
        raise ValueError('PAPER_AUTO_BENCHMARK_BLOCKED')
    if configured_strategy(cfg)=='V2':
        from paper_v2_30m import validate_context
        from paper_v2_quotes import validate_inputs
        validate_context(db,engine,now)
        validate_inputs(db,result)
    daily = engine.get('v5_portfolio_context') or {}
    if 'cohort' in daily:
        from paper_v5 import daily_group
        if list(daily_group(db,daily.get('benchmark'))) != daily['cohort']:
            raise ValueError('PAPER_V5_DAILY_GROUP_CHANGED')
    return manual


def held_position(db, isin, portfolio=None):
    from paper_account import ledger
    return next((p for p in ledger(db,portfolio).get('positions',[])
                 if p['isin']==isin and Decimal(p['quantity'])>0),None)


def validate(db, info, result, args, now):
    """Inside paper_account's BEGIN IMMEDIATE, immediately before execution."""
    from trading_engine import VERSION as CORE_VERSION
    from paper_entry_modes import STRATEGY_CODES
    from paper_protection import decision, entry_plan, strategy_plan
    isin=info['isin']
    portfolio=result_portfolio(db,result)
    cfg=authority(db,isin,result.get('identity'),portfolio=portfolio)
    if result.get('hourly',{}).get('preparation_only'):
        raise ValueError('PAPER_PREPARATION_CANNOT_EXECUTE')
    manual=validate_analysis(db,info,result,now,cfg)
    engine=result['execution_context']['trading_engine']
    tf=result['timeframe']
    held=held_position(db,isin,portfolio)
    mode,signal=strategy_signal(cfg,result,held)
    protective=None
    if args.get('_protection'):
        protective=decision(db,info,result,now)
        if not protective or protective!=args['_protection'] or args['side']!='SELL':
            raise ValueError('PAPER_PROTECTION_TRIGGER_CHANGED')
    side=args['side']
    if not protective:
        action=signal['action']
        # Preserve historical idempotence and the explicit position-limit guard.
        if not (action==side or held and side=='BUY' and action=='HOLD'):
            raise ValueError('PAPER_AUTO_SIGNAL_MISMATCH')
        original_exit=held and held.get('auto_entry') and side=='SELL'
        if not original_exit and not signal_allowed(cfg,tf,side):
            raise ValueError('PAPER_AUTO_TIMEFRAME_NOT_ELIGIBLE')
    if result.get('quote_blocker'):
        raise ValueError(result['quote_blocker'])
    buy_setup=signal if side=='BUY' and (mode!='V1' or signal.get('model')==long_term.MODEL) else None
    size=order_size(db,info,result,side,now,strategy=buy_setup,protective=protective)
    if Decimal(str(args.get('quantity')))!=Decimal(str(size['quantity'])):
        raise ValueError('PAPER_AUTO_SIZE_BLOCKED')
    signal_at=signal_timestamp(result)
    command=protective['request_id'] if protective else portfolio_request_id(db,cfg,isin,signal_at,tf,portfolio)
    if not signal_at or args.get('request_id')!=command:
        raise ValueError('PAPER_AUTO_REQUEST_MISMATCH')
    q=result.get('execution_quote',{})
    from paper_v2_quotes import bound_identity
    bound_identity(db,info,result)
    if q.get('isin')!=isin:
        raise ValueError('PAPER_AUTO_QUOTE_IDENTITY_BLOCKED')
    receipt=dict(context=VERSION,strategy='CORE:'+CORE_VERSION if mode=='V1' else STRATEGY_CODES[mode],
        strategy_mode=mode,strategy_label=STRATEGY_CODES[mode],timeframe=tf,signal=signal['action'],
        signal_at=signal_at,quote_timeframe=q['timeframe'],manual_risk=manual,epoch=cfg['epoch'],sizing=size)
    receipt['signal_reason']=signal.get('reason','')
    if mode=='V1' and result.get('news_background') is not None:
        receipt['news_background']=result['news_background']
    from gettex_first import decision_evidence
    receipt['data_context']=decision_evidence(result)
    if protective:
        receipt['protective_exit']=protective
    elif side=='BUY':
        if buy_setup and buy_setup.get('model'):
            receipt['strategy_model']=buy_setup['model']
            if buy_setup['model']==long_term.MODEL:
                receipt['entry_evidence']={k:buy_setup[k] for k in (
                    'setup','atr','support','entry_level','signal_day','signal_closed','weekly','relative_strength')
                    if k in buy_setup}
            elif mode=='V2':
                receipt['entry_evidence']={k:buy_setup[k] for k in (
                    'setup','atr','anchor','signal_closed','quote_end','exit_rule','score','personal_days','artifact_sha256')}
            elif 'gap_evidence' in buy_setup:
                receipt['entry_evidence']={k:buy_setup[k] for k in ('setup','atr','entry_level','gap_evidence')}
            elif buy_setup['model']==paper_v5_four_hour.MODEL:
                receipt['entry_evidence']={k:buy_setup[k] for k in (
                    'setup','atr','entry_level','relative_volume','signal_closed','opening','quote_end',
                    'weekly_trend','entry_rule','weighted_entry')}
                for key in ('individual_4h','reclaim'):
                    if key in buy_setup:
                        receipt['entry_evidence'][key]=buy_setup[key]
            elif buy_setup['model']==daily_pullback.MODEL:
                receipt['entry_evidence']={k:buy_setup[k] for k in (
                    'setup','atr','signal_close','signal_closed','signal_day','entry_rule',
                    'opening','quote_end','relative_volume','defensive_score','market')}
            else:
                receipt['entry_evidence']={k:buy_setup[k] for k in ('opening_high','relative_volume','atr',
                    'breadth_positive','breadth_available','breadth_required','breadth_inputs','setup','daily_down_closes','entry_level')}
            if buy_setup.get('report_date'):
                receipt['entry_evidence']['report_date']=buy_setup['report_date']
            if 'daily_context' in buy_setup:
                receipt['entry_evidence']['daily_context']={k:v for k,v in buy_setup['daily_context'].items()
                                                            if k not in ('cohort','held_gap')}
        if buy_setup and buy_setup.get('model')==long_term.MODEL:
            protection=long_term.protection_plan(result,buy_setup,size,now)
        elif buy_setup:
            protection=strategy_plan(result,buy_setup,mode,size,now)
        else:
            protection=entry_plan(cfg,result,size,now)
        if protection:receipt['protection']=protection
    if mode=='V2' and (buy_setup and buy_setup.get('model') or held and
            (held.get('auto_entry') or {}).get('strategy_model')):
        from paper_v2_30m import MODEL as V2_MODEL,COSTS as V2_COSTS
        bound=buy_setup.get('model') if buy_setup else held['auto_entry']['strategy_model']
        if bound!=V2_MODEL:raise ValueError('V2_30M_MODEL_CHANGED')
        receipt['execution_policy']=V2_COSTS.copy()
    return receipt


def request_id(cfg, isin, signal_at, timeframe='1H'):
    # Side/revision deliberately excluded: no second opposite trade on the same bar.
    return 'auto'+timeframe.lower()+'-' + digest([cfg['epoch'], isin, signal_at])


def portfolio_request_id(db,cfg,isin,signal_at,tf,portfolio):
    if portfolio==ACCOUNT:
        return request_id(cfg,isin,signal_at,tf)
    epoch=read(db,portfolio_epoch_key(portfolio),cfg['epoch'])
    return 'auto'+tf.lower()+'-'+digest([portfolio,epoch,isin,signal_at])


def order_command(db, info, result, now, *, manage=False):
    """Prepare one order on the existing PAPER account."""
    from paper_protection import decision, write_management
    from paper_account import saved_fill
    isin=info['isin']
    portfolio=result_portfolio(db,result)
    cfg=authority(db,isin,result['identity'],portfolio=portfolio)
    engine=result['execution_context']['trading_engine']
    held=held_position(db,isin,portfolio)
    mode,signal=strategy_signal(cfg,result,held)
    protective=decision(db,info,result,now)
    management=protective if protective and 'management' in protective else None
    if management:
        protective=None
    side=protective['side'] if protective else signal['action']
    signal_tf=result['timeframe']
    original_exit=bool(held and held.get('auto_entry') and signal_tf==held['auto_entry']['timeframe'] and side=='SELL')
    if management and manage:
        from paper_account import validate_execution_inputs, validate_session
        token=validate_execution_inputs(db,result,isin)
        validate_analysis(db,info,result,now,cfg)
        quote_tf=result['execution_quote']['timeframe']
        if 'setting:gettex_chart:'+isin+':'+quote_tf not in token:
            raise ValueError('PAPER_NO_LISTING_REVISION')
        validate_session(result['identity'],now)
        write_management(db,management['management'],management['entry_request_id'])
    if result.get('paper_manage_only') and side!='SELL':
        return None
    if not (protective or original_exit or signal_allowed(cfg,signal_tf,side)):
        return None
    if result.get('quote_blocker'):raise ValueError(result['quote_blocker'])
    command=protective['request_id'] if protective else portfolio_request_id(
        db,cfg,isin,signal_timestamp(result),signal_tf,portfolio)
    prior=saved_fill(db,command,include_archive=False)
    if prior:
        if prior[0]!=portfolio:raise ValueError('PAPER_REQUEST_REUSED')
        receipt=json.loads(prior[2])
        if receipt['side']!=side:raise ValueError('PAPER_REQUEST_REUSED')
        qty=float(receipt['quantity']);qty=int(qty) if qty.is_integer() else qty
    else:
        setup=signal if side=='BUY' and (mode!='V1' or signal.get('model')==long_term.MODEL) else None
        qty=order_size(db,info,result,side,now,strategy=setup,protective=protective)['quantity']
    args=dict(isin=isin,timeframe=signal_tf,side=side,quantity=qty,request_id=command,_deadline=now+30)
    if protective:args['_protection']=protective
    return args


def reset(db, epoch):
    """Explicit owner reset only. Original receipts retained, never replayed."""
    from paper_account import initialize, ledger
    initialize(db)
    portfolio=selected_portfolio(db)
    if portfolio!=ACCOUNT:
        key=portfolio_epoch_key(portfolio)
        if read(db,key)!=epoch:
            db.execute('INSERT INTO paper_portfolio_archive SELECT id,portfolio,digest,payload,? '
                       'FROM paper_portfolio_fills WHERE portfolio=?',(epoch,portfolio))
            db.execute('DELETE FROM paper_portfolio_fills WHERE portfolio=?',(portfolio,))
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(epoch)))
        return ledger(db,portfolio)
    if read(db, EPOCH) == epoch:
        return ledger(db)
    db.execute('CREATE TABLE IF NOT EXISTS paper_fill_archive(id TEXT PRIMARY KEY,digest TEXT NOT NULL,payload TEXT NOT NULL,reset_epoch TEXT NOT NULL)')
    db.execute('INSERT INTO paper_fill_archive SELECT id,digest,payload,? FROM paper_fills', (epoch,))
    db.execute('DELETE FROM paper_fills')
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (EPOCH, json.dumps(epoch)))
    cfg = read(db, CONFIG, {})
    if cfg:
        cfg['enabled'] = False
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (CONFIG, json.dumps(cfg)))
    return ledger(db)


def signal_timestamp(result):
    """Model signal clock; keep CORE's independent archive timestamp intact."""
    engine=result.get('execution_context',{}).get('trading_engine',{})
    packet=engine.get('v2_30m') or {}
    if packet.get('action')=='BUY':
        from paper_v2_30m import MODEL
        if packet.get('model')==MODEL:return packet.get('signal_closed')
    return engine.get('timestamp')


def compact(result):
    engine = result.get('execution_context', {}).get('trading_engine', {})
    report=dict(at=time.time(), calculation_seconds=result.get('_calculation_timing',{}).get('compute_seconds'),
        core=engine.get('status', 'UNKNOWN'), signal=engine.get('signal', 'WAIT'),
        signal_at=signal_timestamp(result), reason=engine.get('reason'),
        quote_blocker=result.get('quote_blocker'),
        quote_at=(result.get('execution_quote', {}).get('bars') or [{}])[-1].get('close_at'))
    entry=engine.get('v2_30m') or {}
    if entry.get('v2_queue'):
        report['v2_queue'] = entry['v2_queue']
        report['v2_queue_key'] = entry['v2_queue']['key']
        if entry.get('action') == 'BUY':
            report['v2_attempted_isin'] = result['isin']
    if entry.get('ranked_isin'):
        report.update(v2_ranked_isin=entry['ranked_isin'],v2_market_source=entry.get('market_source'))
    return report


def primary_report(reports):
    for predicate in (lambda row:row['state']=='COMPLETED',
                      lambda row:row.get('signal')=='BUY',
                      lambda row:row['state']=='WAIT'):
        primary=next((r for r in reports.values() if predicate(r)),None)
        if primary:
            return primary
    return next(iter(reports.values()))


def run_turn(app, info, values, cfg, *, v5_queue=None, manage_portfolio=None, entry_portfolio=None):
    """Evaluate the configured signal intervals and any existing protection."""
    from paper_account import execute
    from paper_entry_modes import STRATEGY_CODES
    from paper_preparation import retry_inputs
    isin=info['isin']
    with app.store.connect() as db:
        portfolio=entry_portfolio or manage_portfolio or selected_portfolio(db)
        independent=independent_portfolios(db) is not None
        cfg=authority(db,isin,portfolio=portfolio)
        if paper_v5_four_hour.enabled(cfg):
            scopes=v5_retry_scopes(isin,cfg['identities'][isin])
            wake=v5_revisions(db,scopes)
        else:
            wake=retry_inputs(db,isin,cfg['identities'][isin])
        split=portfolios_enabled(db)
        held=held_position(db,isin,portfolio)
        if manage_portfolio and not held:
            return {}
    status_key=(entry_status_key(portfolio,isin) if independent else
                portfolio_status_key(portfolio,isin) if manage_portfolio else STATUS+isin)
    if split:
        values=dict(values,_paper_portfolio=portfolio,_paper_manage_only=bool(manage_portfolio))
    reports={}
    decisions=[]
    intervals=turn_timeframes(cfg,held)
    analysis=(app.compute_trading.calculate('paper_hourly',dict(values,_paper_timeframes=intervals))
           if configured_strategy(cfg)=='AGGRESSIVE' and len(intervals)>1
           and not paper_v5_four_hour.enabled(cfg) else None)
    batch=analysis['results'] if analysis else None
    from paper_v5 import AUTO_ENTRY_MODELS, priority_score
    if batch and not held and configured_strategy(cfg)=='AGGRESSIVE':
        quotes = {}
        for tf, candidate in batch.items():
            if (candidate.get('_calculation_error') or candidate.get('quote_blocker')
                    or not candidate.get('execution_quote',{}).get('bars')):
                continue
            _,signal=strategy_signal(cfg,candidate)
            if signal.get('model') in AUTO_ENTRY_MODELS:
                quotes[tf]=priority_score(candidate,signal,intervals)
        original_order=intervals
        intervals=tuple(sorted(intervals,key=lambda tf:(tf not in quotes,quotes.get(tf,()),original_order.index(tf))))
    for signal_tf in intervals:
        report=dict(timeframe=signal_tf,at=time.time())
        result=None
        try:
            request=dict(values,timeframe=signal_tf)
            if paper_v5_four_hour.enabled(cfg) and not held:
                request['_paper_v5_screen']=True
                request['_paper_v5_watch']=signal_tf=='4H' and values.get('_paper_v5_watch') is True
            result=(batch[signal_tf] if batch is not None else
                    app.compute_trading.calculate('paper_hourly',request))
            if '_calculation_error' in result:
                report.update(state='BLOCKED',execution_blocker=result['_calculation_error'])
                reports[signal_tf]=report
                continue
            report.update(compact(result))
            screened=result.get('_v5_entry_screen') or result.get('_v1_entry_screen') or result.get('_v2_entry_screen')
            if screened:
                code='V1' if result.get('_v1_entry_screen') else 'V2' if result.get('_v2_entry_screen') else 'V5'
                report.update(strategy=code,state='WAIT',entry_screen=True)
                needed=result.get('quote_request')
                if needed and (signal_tf!='4H' or paper_v5_four_hour.individual_decision(
                        cfg,result,dict(action='BUY',quote_end=needed['close_at']))['action']=='BUY'):
                    report['quote_request']=needed
            else:
                mode,signal=strategy_signal(cfg,result,held)
                report.update(strategy=STRATEGY_CODES[mode],signal=signal['action'],reason=signal['reason'])
            from gettex_first import decision_evidence
            evidence=decision_evidence(result)
            evidence.update(strategy_signal=report['signal'],strategy_reason=report['reason'])
            if not screened and 'individual_4h' in signal:
                evidence['individual_4h']=signal['individual_4h']
                report['individual_4h']=signal['individual_4h']
            entry=result['execution_context']['trading_engine'].get('v5_four_hour',{})
            evidence.update({key:entry[key] for key in ('weekly_trend','entry_rule','weighted_entry','reclaim') if key in entry})
            if signal_tf=='4H' and request.get('_paper_v5_watch'):
                trend=entry.get('trend_watch')
                if trend is None and 'weekly_trend' in entry:
                    trend=paper_v5_four_hour.trend_watch(entry['weekly_trend'])
                if trend is not None:
                    report['trend_watch']=evidence['trend_watch']=trend
            v1=result['execution_context']['trading_engine'].get('v1_long_term')
            if v1 is not None:
                report['v1_long_term']=evidence['v1_long_term']=v1
            if result.get('news_background') is not None:
                report['news_background']=evidence['news_background']=result['news_background']
            daily=result['execution_context']['trading_engine'].get('v5_daily_pullback')
            if daily is not None:
                evidence['daily_pullback']=daily
            report['data_context_digest']=digest(evidence)
            decisions.append((signal_tf,evidence))
            if screened:
                blocker=report['quote_blocker']
                if blocker and blocker not in ('PAPER_NO_CLOSED_BAR','PAPER_STALE_QUOTE'):
                    report['state']='BLOCKED'
                reports[signal_tf]=report
                continue
            ready=report['core']=='READY'
            report['state']='WAIT' if ready and signal['action']!='BLOCKED' else 'BLOCKED'
            managed=bool(held and held.get('protection'))
            actionable=signal_allowed(cfg,signal_tf,report['signal']) or bool(
                held and held.get('auto_entry') and signal['action'] in ('BUY','SELL'))
            queued = bool(v5_queue is not None and ready and not held and actionable
                          and signal.get('model') in AUTO_ENTRY_MODELS and not result.get('quote_blocker'))
            if queued:
                v5_queue.add(info,result,signal,configured_timeframes(cfg))
                report['execution_blocker']='PAPER_V5_RANKING_PENDING'
            if manage_portfolio and signal['action']!='SELL':
                actionable=False
            if ready and not queued and (actionable or managed or cfg.get('protection')):
                if app.closing or app.user_dirty:
                    raise ValueError('PAPER_APP_NOT_READY')
                if not actionable:
                    from paper_protection import decision
                    with app.store.connect() as db:
                        actionable=bool(decision(db,info,result,time.time()))
                answer=None
                if actionable:
                    with sqlite3.connect(app.store.path,timeout=.2) as db:
                        db.execute('BEGIN IMMEDIATE')
                        args=order_command(db,info,result,time.time(),manage=True)
                        answer=execute(db,info,result,args) if args else None
                if answer:
                    report.update(state='COMPLETED',request_id=args['request_id'],quantity=args['quantity'],
                        idempotent=answer.get('idempotent',False),
                        sizing=answer['fill'].get('smart_eligibility',{}).get('sizing'),
                        protective_exit=answer['fill'].get('smart_eligibility',{}).get('protective_exit'))
        except Exception as error:
            message=str(error)
            if not message.startswith('InputRevisionChanged:'):
                message=type(error).__name__+': '+message
            report.update(state='BLOCKED',execution_blocker=message)
        if (result is not None and signal_tf=='1H' and configured_strategy(cfg)=='AGGRESSIVE'
                and report.get('core')=='READY' and not report.get('quote_blocker')):
            from paper_v5_context_study import capture
            try:
                report['context_study']=capture(app.store.path,info,result,time.time(),
                    completed=report.get('state')=='COMPLETED')
            except (ValueError,sqlite3.Error) as error:
                report['context_study']=str(error)
            except Exception as error:
                report['context_study']=type(error).__name__
        reports[signal_tf]=report
        if report['state']=='COMPLETED':
            if batch is not None:
                for other_tf in intervals[intervals.index(signal_tf)+1:]:
                    other=batch[other_tf]
                    if '_calculation_error' in other:
                        reports[other_tf]=dict(timeframe=other_tf,state='BLOCKED',at=report['at'],
                            execution_blocker=other['_calculation_error'])
                        continue
                    other_mode,other_signal=strategy_signal(cfg,other,held)
                    reports[other_tf]=dict(compact(other),timeframe=other_tf,strategy=STRATEGY_CODES[other_mode],
                        signal=other_signal['action'],reason=other_signal['reason'],state='NOT_SELECTED')
            break
    if portfolio == 'V2':
        from paper_v2_quotes import candidate_rejected
        for row in reports.values():
            if candidate_rejected(row):
                row['v2_candidate_rejected'] = True
    retry=any(row.get('execution_blocker','').startswith(('InputRevisionChanged:','ValueError: PAPER_STALE_INPUTS'))
        for row in reports.values())
    primary=primary_report(reports)
    status=dict(primary,timeframes=reports,retry_inputs=wake,signal_timeframe=primary['timeframe'],
        timeframe_policy=TIMEFRAME_POLICY,candidate_timeframes=list(configured_timeframes(cfg)),
        strategy_mode=configured_strategy(cfg),
        next_check=time.time()+(15 if retry else 60))
    if status['strategy_mode']=='AGGRESSIVE':
        status['v5_rule_version']=v5_rule_version(cfg)
        individual=cfg.get(paper_v5_four_hour.INDIVIDUAL_KEY,{})
        status['v5_individual_revision']=individual.get('revision')
        status['v5_4h_reclaim_rule']=(paper_v5_four_hour.RECLAIM_RULE
                                     if paper_v5_four_hour.reclaim_enabled(cfg,isin) else None)
    previous=app.store.setting(status_key,{}) if hasattr(app.store,'setting') else {}
    if paper_v5_four_hour.enabled(cfg) and '4H' in reports:
        direction=reports['4H'].get('trend_watch')
        if direction is not None:
            status['trend_watch']=direction
        elif previous.get('trend_watch') and previous.get('retry_inputs')==wake:
            status['trend_watch']=previous['trend_watch']
    for key in ('analysis_timeframes','analysis_at'):
        if analysis and key in analysis:
            status[key]=analysis[key]
        elif key in previous:
            status[key]=previous[key]
    if callable(getattr(app.store,'event',None)):
        for interval,evidence in decisions:
            row=reports[interval];prior=previous.get('timeframes',{}).get(interval,{})
            if prior.get('data_context_digest')!=row.get('data_context_digest'):
                app.store.event('paper_data_decision',dict(at=row['at'],decision=evidence,
                    outcome={k:row.get(k) for k in ('state','signal','reason','quote_blocker','execution_blocker','request_id')}))
    app.store.set_setting(status_key,status)
    if v5_queue is not None and v5_queue.due():
        v5_queue.flush(app)
        status=app.store.setting(status_key,status)
    return status


class V5EntryQueue:
    """Briefly compare ready BUYs for the same quoted close in the trading lane."""

    def __init__(self):
        self.offers = {}
        self.deadline = None

    def add(self, info, result, signal, intervals):
        from datetime import datetime
        from paper_v5 import priority_score
        from paper_account import POLICY

        score = priority_score(result,signal,intervals)
        score = (score[0], bool(signal.get('reclaim')), score)
        quote = result['execution_quote']['bars'][-1]
        at = datetime.fromisoformat(quote['close_at'])
        expiry = datetime.fromisoformat(result['hourly']['expires_at']).timestamp()
        quote_expiry = at.timestamp() + POLICY['max_bar_age_seconds']
        due = min(time.time() + 17, expiry - 5, quote_expiry - 5)
        self.deadline = due if self.deadline is None else min(self.deadline, due)
        self.offers[(at,info['isin'],result['timeframe'])] = info,result,score

    def due(self):
        return bool(self.offers and (len(self.offers)>=10 or time.time()>=self.deadline))

    def flush(self, app):
        from paper_account import execute

        if not self.offers or app.user_dirty or app.closing:
            return
        ordered = sorted(self.offers.items(),key=lambda item:(item[0][0],item[1][2]))
        with sqlite3.connect(app.store.path,timeout=.2) as db:
            db.execute('BEGIN IMMEDIATE')
            ready=independent_portfolios(db)
            portfolio='V5' if ready is not None else selected_portfolio(db)
            start_token=ledger_token(db,portfolio)
            cfg=read(db,CONFIG,{})
            active=cfg.get('enabled') and ('V5' in ready if ready is not None else configured_strategy(cfg)=='AGGRESSIVE')
            def status_key(isin):
                return entry_status_key(portfolio,isin) if ready is not None else STATUS+isin
            updated = {}
            completed = set()
            for (_, isin, tf),(info,result,_) in ordered:
                status = updated.get(isin) or read(db,status_key(isin),{})
                row = status.get('timeframes',{}).get(tf)
                if not row or row.get('execution_blocker')!='PAPER_V5_RANKING_PENDING':
                    continue
                engine = result['execution_context']['trading_engine']
                if (row.get('signal_at')!=engine.get('timestamp')
                        or row.get('quote_at')!=result['execution_quote']['bars'][-1]['close_at']):
                    continue
                if isin in completed:
                    row.pop('execution_blocker',None)
                    row['state']='NOT_SELECTED'
                    continue
                db.execute('SAVEPOINT v5_offer')
                try:
                    if not active or app.closing or app.user_dirty:
                        raise ValueError('PAPER_APP_NOT_READY')
                    if result['hourly']['ledger_token'] != start_token or held_position(db,isin,portfolio):
                        raise ValueError('PAPER_AUTO_POSITION_CHANGED')
                    # Only prior fills in this same locked transaction may change the
                    # ledger. This order is still sized and validated on current cash.
                    result = dict(result,hourly=dict(result['hourly'],ledger_token=ledger_token(db,portfolio)))
                    args = order_command(db,info,result,time.time(),manage=True)
                    if not args or args['side']!='BUY':
                        raise ValueError('PAPER_AUTO_SIGNAL_MISMATCH')
                    answer = execute(db,info,result,args)
                    row.pop('execution_blocker',None)
                    row.update(state='COMPLETED',request_id=args['request_id'],quantity=args['quantity'],
                               idempotent=answer.get('idempotent',False),
                               sizing=answer['fill']['smart_eligibility']['sizing'])
                    completed.add(isin)
                except Exception as error:
                    db.execute('ROLLBACK TO SAVEPOINT v5_offer')
                    row.update(state='BLOCKED',execution_blocker=type(error).__name__+': '+str(error))
                finally:
                    db.execute('RELEASE SAVEPOINT v5_offer')
                updated[isin] = status
            for isin,status in updated.items():
                primary = primary_report(status['timeframes'])
                for key in ('execution_blocker','request_id','quantity','idempotent','sizing','protective_exit'):
                    status.pop(key,None)
                status.update(primary,signal_timeframe=primary['timeframe'])
                if any(str(row.get('execution_blocker','')).endswith(
                        ('PAPER_STALE_WINDOW','PAPER_STALE_INPUTS','PAPER_AUTO_POSITION_CHANGED'))
                        for row in status['timeframes'].values()):
                    status['next_check'] = min(status['next_check'],time.time()+15)
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(status_key(isin),json.dumps(status)))
        self.offers.clear()
        self.deadline = None



def v5_retry_scopes(isin, identity):
    from history_service import parent_provenance_key
    from paper_preparation import retry_scopes
    symbol=identity['symbol']
    return (*retry_scopes(isin,identity), 'market:'+isin+':1m',
            *('setting:data_quality:'+isin+':'+symbol+':'+tf for tf in ('1H','4H','1W')),
            *('setting:'+parent_provenance_key(isin,identity,base) for base in ('15m','1D')))


def v5_revisions(db, scopes):
    return {**dict.fromkeys(scopes,0), **dict(db.execute(
        'SELECT scope,revision FROM input_revisions WHERE scope IN ('+
        ','.join('?' for _ in scopes)+')',scopes))}


def due_stock(db,cfg,ids,now,portfolio=None):
    """Read only the configured candidate status and its input revisions."""
    if not ids:
        return None
    from paper_preparation import retry_scopes
    status_keys={entry_status_key(portfolio,i) if portfolio else STATUS+i:i for i in ids}
    previous={status_keys[key]:json.loads(value) for key,value in db.execute(
        'SELECT key,value FROM settings WHERE key IN ('+
        ','.join('?' for _ in status_keys)+')',tuple(status_keys))}
    v5=paper_v5_four_hour.enabled(cfg)
    scope_builder=v5_retry_scopes if v5 else retry_scopes
    scopes={i:scope_builder(i,cfg['identities'][i]) for i in ids}
    keys=tuple(dict.fromkeys(key for values in scopes.values() for key in values))
    revisions={}
    for offset in range(0,len(keys),900):
        revisions.update(v5_revisions(db,keys[offset:offset+900]))
    due=[]
    v2_slots=set()
    v2_ranked=set()
    if portfolio=='V2' and configured_strategy(cfg)=='V2':
        from paper_v2_quotes import new_slots,ranked_slots
        v2_slots=new_slots(db,ids,previous,now)
        v2_ranked=ranked_slots(ids,previous,now)
    for rank,isin in enumerate(ids):
        status=previous.get(isin,{})
        wake=status.get('retry_inputs',{})
        dirty=(any(wake.get(key)!=revisions[key] for key in scopes[isin])
               or status.get('strategy_mode')!=configured_strategy(cfg)
               or status.get('timeframe_policy')!=TIMEFRAME_POLICY
               or status.get('candidate_timeframes')!=list(configured_timeframes(cfg)))
        if v5:
            dirty=dirty or status.get('v5_rule_version')!=v5_rule_version(cfg)
        if dirty or now>=status.get('next_check',0) or isin in v2_ranked or isin in v2_slots:
            due.append((isin not in v2_ranked,isin not in v2_slots,not dirty,status.get('at',0),rank if portfolio=='V1' else isin,isin))
    return min(due)[-1] if due else None


def v5_due_stock(db,cfg,ids,now):
    return due_stock(db,cfg,ids,now)


def portfolio_status_key(portfolio,isin):
    return 'paper_protection_status:'+portfolio+':'+isin


class Runtime:
    """One bounded trading job; owned read client, existing Dispatcher and ledger."""
    def __init__(self, app):
        self.app = app;self.busy = False;self.cursor = 0;self.preparation_after = 0
        self.operational_turns = 0
        self.v5_queue = V5EntryQueue()
        self.opening_scan_day = None
        self.opening_next = {}
        self.v5_retry_after = {}
        self.v1_scan_key = None
        self.v1_scan_after = 0
        self.v1_order = ()
        self.v1_strength = {}
        self.split_portfolios = False
        self.protection_after = 0
        self.independent=False
        self.portfolio_cursor=0
        self.independent_after=0
        self.v5_priority_used=False
        self.failed_turns={}

    def start(self):
        self.app.root.after(15000,self.initialize_accounts)

    def initialize_accounts(self):
        from paper_account import initialize_independent_automation,initialize_portfolios
        app=self.app
        if app.closing:
            return

        def work():
            with app.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                initialize_portfolios(db)
                initialize_independent_automation(db)

        def done(_):
            self.split_portfolios=True
            self.independent=True
            self.tick()

        def failed(_):
            if not app.closing:
                app.root.after(5000,self.initialize_accounts)

        if app.job(work,done,failed,lane='trading',key='paper_portfolio_setup') is False:
            failed(None)

    def service_other_positions(self,app,cfg,ids,now):
        if not self.split_portfolios or self.busy or app.user_dirty or now<self.protection_after:
            return False
        watching={i['isin']:i for i in tuple(app.watchlist) if i['isin'] in ids}
        ai=copy.deepcopy(app.ai_config)
        self.busy=True

        def work():
            from paper_preparation import retry_inputs
            with app.store.connect() as db:
                db.execute('BEGIN')
                active=selected_portfolio(db)
                ready=[]
                for p in maintained_positions(db):
                    isin,portfolio=p['isin'],p['portfolio']
                    if (portfolio==active or isin not in watching
                            or not (p.get('auto_entry') or p.get('protection'))):
                        continue
                    status=read(db,portfolio_status_key(portfolio,isin),{})
                    inputs=retry_inputs(db,isin,p['identity'])
                    if inputs!=status.get('retry_inputs') or now>=status.get('next_check',0):
                        ready.append((status.get('at',0),portfolio,isin))
            if ready and not app.closing and not app.user_dirty:
                _,portfolio,isin=min(ready)
                info=copy.deepcopy(watching[isin])
                values=dict(watchlist=[info],drafts={},feed_errors={},symbol=info['name'],
                    timeframe='1H',period='Max',ai_config=ai,
                    instrument_revisions=app.store.read_cache.token_for(['instrument:'+isin]))
                run_turn(app,info,values,cfg,manage_portfolio=portfolio)
                return True
            return False

        def done(processed):
            self.busy=False
            self.protection_after=time.time()+(6 if processed else 15)

        if app.job(work,done,lambda _:done(False),lane='trading',key='paper_other_positions') is False:
            done(False)
            return False
        return True

    def opening_priority(self, cfg, ids, now):
        """Use the one trading lane for the brief, verified opening gap."""
        import pandas as pd
        from paper_v5 import opening_gap_candidates

        if not ids or configured_strategy(cfg)!='AGGRESSIVE' or paper_v5_four_hour.enabled(cfg):
            return None,False
        checked = pd.Timestamp(now,unit='s',tz='UTC')
        local = checked.tz_convert('Europe/Berlin')
        minute = local.hour*60+local.minute
        if local.dayofweek>=5 or not 465<=minute<495:
            return None,False
        app = self.app
        day = local.date()
        if self.opening_scan_day!=day:
            self.opening_scan_day = day
            self.opening_next = {}
            self.busy = True

            def completed(isins):
                self.opening_next = dict.fromkeys(isins,0)
                self.busy = False

            def failed(_):
                self.busy = False
                self.opening_scan_day = None

            if app.job(lambda:opening_gap_candidates(app.store.path,cfg,checked),completed,failed,
                       lane='trading',key='paper_v5_gap_scan') is False:
                failed(None)
            return None,self.busy
        if minute<480:
            return None,bool(self.opening_next and minute>=478)
        for isin in list(self.opening_next):
            if isin not in cfg['identities']:
                self.opening_next.pop(isin)
                continue
            if self.opening_next[isin]>now:
                continue
            status = app.store.setting(STATUS+isin,{})
            reports = status.get('timeframes',{})
            row = reports.get('1D',{})
            if row.get('execution_blocker')=='PAPER_V5_RANKING_PENDING':
                continue
            if any(r.get('state')=='COMPLETED' and r.get('signal')=='BUY' and r.get('at',0)>now-900
                   for r in reports.values()):
                self.opening_next.pop(isin)
                continue
            return isin,False
        return None,False

    def tick_v5(self, app, cfg):
        """Choose and evaluate off Tk, using the same single trading lane."""
        if self.busy or app.user_dirty:
            return
        pending={key[1] for key in self.v5_queue.offers}
        watching={i['isin']:i for i in tuple(app.watchlist) if i['isin'] in cfg['identities']}
        ai=copy.deepcopy(app.ai_config)
        self.busy=True

        def work():
            import pandas as pd
            from paper_preparation import turn
            now=time.time()
            with app.store.connect() as db:
                db.execute('BEGIN')
                current=read(db,CONFIG,{})
                if not current.get('enabled') or not paper_v5_four_hour.enabled(current):
                    return None
                ids=tuple(i for i in watching if i in current['identities'] and i not in pending
                          and now>=self.v5_retry_after.get(i,0))
                isin=v5_due_stock(db,current,ids,now)
            if (current.get('automatic_admission',{}).get('enabled') and now>=self.preparation_after
                    and (isin is None or self.operational_turns>=2)):
                at=pd.Timestamp(now,unit='s',tz='UTC')
                identity=next(iter(current['identities'].values()),None)
                if identity is not None:
                    try:
                        _,window=paper_v5_four_hour.entry_window(identity,at,reclaim=True)
                        _,upcoming=paper_v5_four_hour.entry_window(identity,at+paper_v5_four_hour.QUARTER,reclaim=True)
                    except ValueError:
                        window=upcoming=None
                    if window is None and upcoming is None:
                        turn(app)
                        return 'preparation'
            if isin and not app.closing and not app.user_dirty:
                info=copy.deepcopy(watching[isin])
                values=dict(watchlist=[info],drafts={},feed_errors={},symbol=info['name'],
                    timeframe='1H',period='Max',ai_config=ai,_paper_v5_watch=True,
                    instrument_revisions=app.store.read_cache.token_for(['instrument:'+isin]))
                try:
                    run_turn(app,info,values,current,v5_queue=self.v5_queue)
                except Exception as error:
                    # A vanished authority or failed turn must not starve the
                    # rest of the list. Re-read its revisions on the next visit.
                    import logging
                    logging.getLogger(__name__).warning("PAPER V5 reevaluation: %s",type(error).__name__)
                    return ('failed',isin)
                return 'trade'
            return None

        def done(outcome):
            self.busy=False
            if outcome=='trade':
                self.operational_turns+=1
            elif outcome=='preparation':
                self.operational_turns=0
                self.preparation_after=time.time()+60
            elif isinstance(outcome,tuple) and outcome[0]=='failed':
                self.v5_retry_after[outcome[1]]=time.time()+15

        if app.job(work,done,lambda _:done(None),lane='trading',key='paper_auto_v5') is False:
            self.busy=False

    def scan_v1(self, app, cfg, ids, now):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        day = datetime.fromtimestamp(now, ZoneInfo('Europe/Berlin')).date()
        identities = cfg['identities']
        key = (day, cfg['epoch'], tuple(ids), digest(identities))
        if key == self.v1_scan_key and now < self.v1_scan_after:
            return False
        if self.busy or app.user_dirty:
            return True
        self.busy = True
        new_session = key != self.v1_scan_key

        def work():
            import pandas as pd
            return long_term.ranked_isins(app.store.path, identities, ids, pd.Timestamp(now,unit='s',tz='UTC'))

        def done(result):
            if result is not None:
                self.v1_order, self.v1_strength = result
            elif new_session:
                self.v1_order, self.v1_strength = tuple(ids), {}
            self.v1_scan_key = key
            self.v1_scan_after = time.time() + (300 if result is not None else 30)
            if new_session:
                self.cursor = 0
            self.busy = False

        if app.job(work,done,lambda _:done(None),lane='trading',key='paper_v1_rank') is False:
            done(None)
        return True

    def tick_independent(self,app,ids):
        """One job at a time, with protection and timed V5 quotes ahead of entries."""
        if self.busy or app.user_dirty or not ids or time.time()<self.independent_after:
            return
        watching={i['isin']:i for i in tuple(app.watchlist) if i['isin'] in ids}
        ai=copy.deepcopy(app.ai_config)
        self.busy=True

        def work():
            import pandas as pd
            from paper_account import validate_session
            from paper_preparation import retry_inputs,turn as prepare
            now=time.time()
            at=pd.Timestamp(now,unit='s',tz='UTC')
            with app.store.connect() as db:
                db.execute('BEGIN')
                current=read(db,CONFIG,{})
                ready=independent_portfolios(db)
                if not current.get('enabled') or ready is None:
                    return 'idle'
                identities=current.get('identities',{})
                ids=tuple(isin for isin in watching if isin in identities)
                if not ids:
                    return 'idle'
                identity=identities[ids[0]]
                try:
                    validate_session(identity,now)
                except ValueError:
                    return 'idle'
                positions=maintained_positions(db)
                held={(p['portfolio'],p['isin']) for p in positions}
                due=[]
                for p in positions:
                    book,isin=p['portfolio'],p['isin']
                    if isin not in ids or not (p.get('auto_entry') or p.get('protection')):
                        continue
                    if now<self.failed_turns.get((book,isin),0):
                        continue
                    status=read(db,entry_status_key(book,isin),{})
                    profile=portfolio_config(current,book)
                    scopes=(v5_revisions(db,v5_retry_scopes(isin,p['identity']))
                            if paper_v5_four_hour.enabled(profile) else retry_inputs(db,isin,p['identity']))
                    if status.get('retry_inputs')!=scopes or now>=status.get('next_check',0):
                        due.append((status.get('at',0),book,isin))
            if due:
                _,book,isin=min(due)
                return execute_turn(current,book,isin,manage=True)
            if self.v5_queue.due():
                self.v5_queue.flush(app)
                return 'flush'
            v5_cfg=portfolio_config(current,'V5')
            timed=False
            if 'V5' in ready and paper_v5_four_hour.enabled(v5_cfg):
                try:
                    _,window=paper_v5_four_hour.entry_window(identity,at,reclaim=True)
                    _,upcoming=paper_v5_four_hour.entry_window(identity,at+paper_v5_four_hour.QUARTER,reclaim=True)
                    timed=bool(window or upcoming or daily_pullback.enabled(v5_cfg) and daily_pullback.opening_window(identity,at))
                except ValueError:
                    pass
            if (current.get('automatic_admission',{}).get('enabled') and now>=self.preparation_after
                    and self.operational_turns>=2 and not timed):
                prepare(app)
                return 'preparation'
            if not ready:
                return 'idle'
            books=tuple(book for book in ('V5','V1','V2','V3','V4') if book in ready)
            begin=self.portfolio_cursor % len(books)
            order=books[begin:]+books[:begin]
            boost=timed and not self.v5_priority_used and order[0]!='V5'
            if boost:
                order=('V5',*(book for book in order if book!='V5'))
            for book in order:
                profile=portfolio_config(current,book)
                if book=='V1':
                    day=at.tz_convert('Europe/Berlin').date()
                    key=(day,current['epoch'],ids,digest(identities))
                    if self.v1_scan_key!=key or now>=self.v1_scan_after:
                        self.v1_order,self.v1_strength=long_term.ranked_isins(app.store.path,identities,ids,at)
                        self.v1_scan_key=key
                        self.v1_scan_after=now+300
                ordered=self.v1_order if book=='V1' else ids
                pending={key[1] for key in self.v5_queue.offers} if book=='V5' else set()
                candidates=tuple(i for i in ordered if (book,i) not in held and i not in pending
                                 and now>=self.failed_turns.get((book,i),0))
                with app.store.connect() as db:
                    db.execute('BEGIN')
                    isin=due_stock(db,profile,candidates,now,book)
                if isin is not None:
                    self.v5_priority_used=boost and book=='V5'
                    if not self.v5_priority_used:
                        self.portfolio_cursor=books.index(book)+1
                    return execute_turn(current,book,isin,manage=False)
            if (current.get('automatic_admission',{}).get('enabled') and now>=self.preparation_after and not timed):
                prepare(app)
                return 'preparation'
            return 'idle'

        def execute_turn(current,book,isin,*,manage):
            if app.closing or app.user_dirty:
                return 'idle'
            info=copy.deepcopy(watching[isin])
            values=dict(watchlist=[info],drafts={},feed_errors={},symbol=info['name'],
                timeframe='1H',period='Max',ai_config=ai,_paper_v5_watch=book=='V5',
                instrument_revisions=app.store.read_cache.token_for(['instrument:'+isin]))
            if book=='V1':
                values['_paper_v1_strength']=self.v1_strength.get(isin,{})
            try:
                if manage:
                    run_turn(app,info,values,current,manage_portfolio=book)
                else:
                    run_turn(app,info,values,current,entry_portfolio=book,
                             v5_queue=self.v5_queue if book=='V5' else None)
            except Exception as error:
                import logging
                logging.getLogger(__name__).warning('PAPER %s turn: %s',book,type(error).__name__)
                self.failed_turns[(book,isin)]=time.time()+15
                return 'failed'
            return 'trade'

        def done(outcome):
            self.busy=False
            if outcome=='trade':
                self.operational_turns+=1
            elif outcome=='preparation':
                self.operational_turns=0
                self.preparation_after=time.time()+60
            elif outcome=='idle':
                self.independent_after=time.time()+5

        if app.job(work,done,lambda _:done('idle'),lane='trading',key='paper_auto_portfolios') is False:
            self.busy=False

    def tick(self):
        app = self.app
        if app.closing:
            return
        cfg = app.store.setting(CONFIG, {})
        ids = list(cfg.get('identities', {})) if cfg.get('enabled') else []
        if self.independent:
            self.tick_independent(app,ids)
            app.root.after(2000,self.tick)
            return
        submitted = False
        preparation_due=bool(ids and cfg.get('automatic_admission',{}).get('enabled')
            and time.time() >= self.preparation_after)
        # With five or more admitted stocks the round-robin can always find a
        # due trading calculation. Reserve one existing lane slot after two
        # operational turns so sequential preparation cannot starve forever.
        yield_preparation=preparation_due and self.operational_turns>=2
        if self.v5_queue.due() and not self.busy and not app.user_dirty:
            self.busy=True
            def flushed(_):
                self.busy=False
            if app.job(lambda:self.v5_queue.flush(app),flushed,flushed,lane='trading',
                       key='paper_v5_ranked_entries') is False:
                self.busy=False
        if ids and self.service_other_positions(app,cfg,ids,time.time()):
            app.root.after(2000,self.tick)
            return
        if ids and paper_v5_four_hour.enabled(cfg):
            self.tick_v5(app,cfg)
            app.root.after(2000,self.tick)
            return
        v1 = bool(ids and long_term.enabled(cfg))
        if v1 and self.scan_v1(app,cfg,ids,time.time()):
            app.root.after(2000,self.tick)
            return
        if v1:
            ids = self.v1_order
        priority,reserve = (self.opening_priority(cfg,ids,time.time())
                            if not self.busy and not app.user_dirty else (None,False))
        if ids and not self.busy and not app.user_dirty and not reserve and (priority or not yield_preparation):
            isin = priority or ids[self.cursor % len(ids)]
            if priority is None:
                self.cursor += 1
            info = next((i for i in app.watchlist if i['isin'] == isin), None)
            previous = app.store.setting(STATUS+isin, {})
            wake=previous.get('retry_inputs',{})
            inputs_changed=bool(wake) and app.store.read_cache.token_for(wake)!=wake
            old_policy='timeframes' in previous and (
                previous.get('timeframe_policy') != TIMEFRAME_POLICY
                or v1 and previous.get('candidate_timeframes') != list(configured_timeframes(cfg)))
            if info and (priority or inputs_changed or old_policy
                         or previous.get('strategy_mode','V1') != cfg.get(STRATEGY_KEY,'V1')
                         or (cfg.get(STRATEGY_KEY)=='AGGRESSIVE' and
                             (previous.get('v5_rule_version')!=v5_rule_version(cfg)
                              or previous.get('candidate_timeframes')!=list(configured_timeframes(cfg))
                              or previous.get('v5_individual_revision')!=cfg.get(
                                  paper_v5_four_hour.INDIVIDUAL_KEY,{}).get('revision')))
                         or time.time() >= previous.get('next_check', 0)):
                values = dict(watchlist=[copy.deepcopy(info)], drafts={}, feed_errors={}, symbol=info['name'],
                    timeframe='1H', period='Max', ai_config=copy.deepcopy(app.ai_config),
                    instrument_revisions=app.store.read_cache.token_for(['instrument:'+isin]))
                if v1:
                    values['_paper_v1_strength'] = self.v1_strength.get(isin, {})
                self.busy = True
                submitted = True

                def work():
                    return run_turn(app,info,values,cfg,v5_queue=self.v5_queue)

                def done(_):
                    self.busy = False

                def failed(message):
                    self.busy = False
                    # Store persistence is queued, not performed by the UI callback.
                    report = dict(state='BLOCKED', at=time.time(), execution_blocker=message, next_check=time.time()+60)
                    app.job(lambda:app.store.set_setting(STATUS+isin, report), lambda _:None, lane='misc', key='paper_auto_error')

                if app.job(work, done, failed, lane='trading', key='paper_auto_hourly', isin=isin) is False:
                    self.busy = False
                else:
                    self.operational_turns += 1
                    if priority is not None:
                        self.opening_next[priority] = time.time()+60
        # Idle trading slots only. The existing owned client and dispatcher do
        # one sequential candidate; no new threads, downloads or order path.
        if (ids and not submitted and not self.busy and not app.user_dirty and not reserve
                and cfg.get('automatic_admission',{}).get('enabled')
                and time.time() >= self.preparation_after):
            self.busy = True;self.preparation_after = time.time()+60
            def prepare():
                from paper_preparation import turn
                return turn(app)
            def prepared(_):self.busy = False
            if app.job(prepare,prepared,prepared,lane='trading',key='paper_preparation') is False:
                self.busy = False
            else:self.operational_turns = 0
        app.root.after(2000 if self.split_portfolios or v1 or paper_v5_four_hour.enabled(cfg) else 15000,self.tick)
