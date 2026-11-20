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

CONFIG = 'paper_auto_1h'
EPOCH = 'paper_epoch'
STATUS = 'paper_auto_status:'
VERSION = 'core_hourly_demo_v1'
SIZING = 'cash_volatility_v1'
SIGNAL_TIMEFRAMES = ('1H', '4H')
SELECTABLE_TIMEFRAMES = ('15m', '30m', '1H', '4H', '1D')
ENTRY_TIMEFRAME_KEY = 'signal_timeframe'
STRATEGY_KEY = 'strategy_mode'


def configured_strategy(cfg):
    from paper_entry_modes import STRATEGY_CODES
    mode=cfg.get(STRATEGY_KEY,'V1')
    if mode not in STRATEGY_CODES:
        raise ValueError('PAPER_AUTO_STRATEGY_BLOCKED')
    return mode


def configured_timeframes(cfg):
    selected = cfg.get(ENTRY_TIMEFRAME_KEY)
    if selected is None:
        return SIGNAL_TIMEFRAMES
    if selected not in SELECTABLE_TIMEFRAMES:
        raise ValueError('PAPER_AUTO_TIMEFRAME_BLOCKED')
    return (selected,)


def set_trading_mode(db, strategy, timeframe):
    configured_strategy({STRATEGY_KEY:strategy})
    if timeframe not in SELECTABLE_TIMEFRAMES:
        raise ValueError('PAPER_AUTO_TIMEFRAME_BLOCKED')
    if strategy=='AGGRESSIVE' and timeframe!='1H':
        raise ValueError('V5_SELECT_1H')
    db.execute('BEGIN IMMEDIATE')
    cfg = read(db, CONFIG, {})
    identities=cfg.get('identities',{})
    if not identities:
        raise ValueError('PAPER_AUTO_NOT_AUTHORIZED')
    authority(db,next(iter(identities)),allow_paused=True)
    if (cfg.get(ENTRY_TIMEFRAME_KEY), configured_strategy(cfg)) != (timeframe,strategy):
        cfg.update({ENTRY_TIMEFRAME_KEY:timeframe,STRATEGY_KEY:strategy})
        db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(cfg), CONFIG))
    return strategy,timeframe


def set_signal_timeframe(db, timeframe):
    return set_trading_mode(db,configured_strategy(read(db,CONFIG,{})),timeframe)[1]


def signal_allowed(cfg, timeframe, side):
    return (timeframe in configured_timeframes(cfg)
            and (side == 'BUY' or side == 'SELL' and (
                timeframe != '4H' or cfg.get(ENTRY_TIMEFRAME_KEY) == '4H'
            )))


def turn_timeframes(cfg, held):
    selected = configured_timeframes(cfg)
    if held and held.get('auto_entry'):
        return (held['auto_entry']['timeframe'],)
    protection_tf = (held.get('protection') or {}).get('signal_tf') if held else None
    if protection_tf in SELECTABLE_TIMEFRAMES and protection_tf not in selected:
        return (*selected, protection_tf)
    return selected


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
        decision=dict(action=engine.get('signal','WAIT'),reason=engine.get('reason',''))
    elif mode=='MOMENTUM' and not original:
        from paper_v4 import decide
        if engine.get('demo_initialization') and not engine['demo_initialization'].get('consistent'):
            return mode,dict(action='BLOCKED',reason='CORE_SEED_UNSTABLE')
        decision=decide(engine,tf)
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


def authority(db, isin, identity=None, *, allow_paused=False):
    from paper_account import ACCOUNT
    cfg = read(db, CONFIG, {})
    if (cfg.get('version') != VERSION or (cfg.get('enabled') is not True and not allow_paused)
            or cfg.get('approved') is not True or cfg.get('account') != ACCOUNT
            or cfg.get('timeframe') != '1H'
            or not (variable_size(cfg) or ('sizing' not in cfg and cfg.get('quantity') == 1 and cfg.get('max_position') == 1))
            or cfg.get('epoch') != read(db, EPOCH)
            or isin not in cfg.get('identities', {})):
        raise ValueError('PAPER_AUTO_NOT_AUTHORIZED')
    configured_timeframes(cfg)
    configured_strategy(cfg)
    exact = cfg['identities'][isin]
    if (exact.get('source') != 'gettex_chart' or exact.get('venue') != 'GETTEX'
            or exact.get('currency') != 'EUR' or exact.get('timezone') != 'Europe/Berlin'
            or (identity is not None and identity != exact)):
        raise ValueError('PAPER_AUTO_IDENTITY_BLOCKED')
    return cfg


def authorize_hourly(db, isin, identity):
    # Historical-risk analysis may examine the one owner-approved preparation
    # candidate. Execution still requires authority(), which excludes it.
    try:authority(db, isin, identity, allow_paused=True)
    except ValueError:
        from paper_preparation import candidate_authority
        candidate_authority(db, isin, identity)


def ledger_token(db):
    return digest(db.execute('SELECT id,digest FROM paper_fills ORDER BY rowid').fetchall())


def order_size(db, info, result, side, now, *, allow_paused=False, strategy=None, protective=None):
    """Integer entry size from free cash and existing CORE volatility/structure.

    This is a sizing estimate, not a stop order or a guaranteed loss ceiling.
    CORE decides entries/exits; an explicit optional PAPER bracket can add
    protective exits. No leverage, averaging or new indicators.
    """
    from paper_account import ledger, quote, POLICY, money
    from eur_listings import stored_identity
    cfg = authority(db, info['isin'], result.get('identity'), allow_paused=allow_paused)
    state = ledger(db)
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
    if q.get('identity') != result['identity']:
        raise ValueError('PAPER_AUTO_QUOTE_IDENTITY_BLOCKED')
    _, bar = quote(info, q, now,
        stored_listing=stored_identity(db, info, result['identity'], q.get('timeframe')))
    if strategy and strategy.get('model'):
        from datetime import datetime, timezone
        from paper_v5 import VERSION as V5_MODEL, MAX_SHARES, macro_release_day, levels, reversal_level_lost
        from paper_v4 import MODE as V4_MODE, VERSION as V4_MODEL
        mode,timeframe=configured_strategy(cfg),result['timeframe']
        model=strategy['model']
        if not ((mode=='AGGRESSIVE' and model==V5_MODEL and timeframe=='1H')
                or (mode==V4_MODE and model==V4_MODEL and timeframe in configured_timeframes(cfg))):
            raise ValueError('PAPER_AUTO_STRATEGY_BLOCKED')
        if model==V4_MODEL and datetime.fromisoformat(bar['close_at'])<datetime.fromisoformat(strategy['signal_closed']):
            raise ValueError('PAPER_V4_QUOTE_BEFORE_SIGNAL')
        cash=Decimal(state['cash'])
        reference=Decimal(str(bar['close']))
        macro=macro_release_day(datetime.fromtimestamp(now,timezone.utc))
        if macro['state']=='RELEASE_DAY':
            raise ValueError('PAPER_V5_MACRO_RELEASE_DAY:'+macro['kind'])
        if model==V4_MODEL and strategy['setup']=='POST_EARNINGS' and reference<=Decimal(str(strategy['entry_level'])):
            raise ValueError('PAPER_V4_EARNINGS_LEVEL_LOST')
        if reversal_level_lost(strategy,reference):
            raise ValueError('PAPER_V5_REVERSAL_LEVEL_LOST')
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
        stop,target=levels(reference,Decimal(str(strategy['atr'])))
        if not 0<stop<reference<target:
            raise ValueError('PAPER_STRATEGY_LEVELS_INVALID_AT_FILL')
        risk=(price-stop*(1-half))*qty+2*fee
        return dict(quantity=qty,mode=model,cash_basis=str(cash),allocation_limit=str(cash),
                    stop=str(stop),target=str(target),estimated_risk=str(risk),macro_calendar=macro,
                    gross_and_fee=str(money(price*qty)+fee),risk_is_estimate=True)
    facts = result.get('execution_context', {}).get('trading_engine', {}).get('timeframe_context', {}).get(result['timeframe'], {}).get('facts', {})
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
    if strategy is None:
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
    while qty > 0 and (money(price*qty)+fee > min(cash, allocation)
            or distance*qty+2*fee > risk_budget):
        qty -= 1
    if qty < 1:
        raise ValueError('PAPER_AUTO_SIZE_BELOW_ONE')
    if strategy is not None:
        reward=(target*(1-half)-price)*qty-2*fee
        if reward < minimum*(distance*qty+2*fee):
            raise ValueError('PAPER_STRATEGY_REWARD_TOO_LOW_AFTER_COSTS')
    return dict(quantity=qty, mode=SIZING if variable_size(cfg) else 'FIXED_ONE', cash_basis=str(cash), allocation_limit=str(allocation),
        risk_budget=str(risk_budget), reference_risk_per_share=str(distance),
        estimated_risk=str(distance*qty+2*fee), gross_and_fee=str(money(price*qty)+fee),
        risk_is_estimate=True, target_equity=cfg.get('target_equity'))


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


def calculate(store, values):
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
        if preparing:
            from paper_preparation import candidate_authority, STATE
            store.read_cache.depend('setting:'+STATE)
            cfg, identity = candidate_authority(db, info['isin'])
        else:
            cfg = authority(db, info['isin'], allow_paused=bool(values.get('_paper_order')))
            identity = cfg['identities'][info['isin']]
        position = next((p for p in ledger(db)['positions'] if p['isin'] == info['isin'] and Decimal(p['quantity'])), {})
        position_token = ledger_token(db)
    now = pd.Timestamp.now(tz='UTC')

    signal_tf = values.get('timeframe', '1H')
    if (signal_tf not in SELECTABLE_TIMEFRAMES
            or (preparing and signal_tf != '1H')
            or (values.get('_paper_order') and signal_tf not in SIGNAL_TIMEFRAMES)):
        raise ValueError('PAPER_AUTO_TIMEFRAME_BLOCKED')
    contract=plan(signal_tf)
    calculated=calculate_context(store,values,identity,contract,position,now)
    engine,proof,manual=(calculated[k] for k in ('engine','proof','manual'))
    if (signal_tf=='1H' and engine.get('status')=='READY' and configured_strategy(cfg)=='AGGRESSIVE'
            and not values.get('_paper_order') and not preparing):
        from paper_v5 import context as opening_range
        engine['v5_opening_range']=opening_range(store,info,identity,engine.get('timestamp'),now,cfg['identities'])
    if (engine.get('status')=='READY' and configured_strategy(cfg)=='MOMENTUM' and not position
            and not values.get('_paper_order') and not preparing):
        from paper_v4 import context as selected_context
        engine['v4_selected_context']=selected_context(store,info,identity,engine.get('timestamp'),now,
                                                       cfg['identities'],signal_tf)
    result = dict(isin=info['isin'], timeframe=signal_tf, identity=identity,
        execution_context=dict(status='MANUAL_RISK_ACCEPTED' if manual else 'HOURLY_DEMO_READY',
            isin=info['isin'], checked_at=now.isoformat(), analysis_ready=engine.get('status') == 'READY',
            trading_engine=engine, manual_risk=manual, manual_inputs=proof),
        hourly=dict(version=VERSION, config_digest=digest(cfg), ledger_token=position_token,
            epoch=cfg['epoch'], checked_at=now.isoformat(),
            expires_at=min(now+pd.Timedelta(seconds=60), context_deadline(identity, signal_tf, now)).isoformat(),
            proof=proof, required_tf=contract['required_tf'],timeframe_contract=contract))
    if preparing:result['hourly']['preparation_only']=True
    # Analysis is useful even while prices are stale. Its status cannot place a fill.
    try:
        from paper_orders import check_intent
        order = values.get('_paper_order')
        if order:
            check_intent(order, info['isin'])
            result['discretionary'] = copy.deepcopy(order)
        result['execution_quote'] = fresh_quote(store, info, identity, now)
    except ValueError as error:
        result['quote_blocker'] = str(error)
    return result


def calculate_snapshot(store, values):
    """One WAL read transaction, then existing exact closed-input validation.

    No database copy or writer lock. Independent DQ receipts can change while
    reading; they are not inputs of this exact closed context. Any new relevant
    or unidentified archive problem is checked again by the manual gate.
    """
    from contextlib import contextmanager
    from performance_cache import ReadCache,validate_closed_inputs,InputRevisionChanged,_setting_value
    from cp_controls import manual_gate
    from paper_account import EXECUTION_TF
    def quote_tail(db, result):
        meta=result['identity']
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
        with captured.read_cache.track_inputs() as token:
            expected=values.get('instrument_revisions',{})
            captured.read_cache.assert_current(expected);captured.read_cache._record(expected)
            result=calculate(captured,values)
            quote_rows=quote_tail(db,result)
            listing_prefix='setting:gettex_chart:'+result['isin']+':'
            quote_listings={scope:_setting_value(db,scope[len('setting:'):]) for scope in token
                if scope == listing_prefix+EXECUTION_TF}
        token=dict(token)
    finally:captured.read_cache.close()
    proof=result['hourly']['proof']
    with store.connect() as db:
        db.execute('BEGIN')
        current=validate_closed_inputs(db,proof)
        # A quote lane can share a market revision with old closed CORE input.
        # Never rebase its token over a changed/newer quote or invalid tail.
        if quote_tail(db,result)!=quote_rows:
            raise InputRevisionChanged('Hourly execution quote changed')
        # capture_closed_inputs already excludes unrelated DQ reports by exact
        # ISIN/symbol/base. Preserve every other dependency, notably the quote
        # lanes, configuration, owner receipts and benchmark provenance.
        for scope,revision in token.items():
            if scope in current:continue
            if scope.startswith('setting:data_quality:'):continue
            row=db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone()
            revision_now=row[0] if row else 0
            if revision_now!=revision and (scope not in quote_listings
                    or quote_listings[scope]!=_setting_value(db,scope[len('setting:'):])):
                raise InputRevisionChanged('PAPER input changed: '+scope)
            current[scope]=revision_now
        manual=result['execution_context'].get('manual_risk')
        if manual is not None and manual_gate(db,result['isin'],result['identity'],result['timeframe'],proof)!=manual:
            raise InputRevisionChanged('Hourly historical-risk permission changed')
    result['_input_revisions']=current
    return result


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
    if h.get('ledger_token') != ledger_token(db):
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
    return manual


def held_position(db, isin):
    from paper_account import ledger
    return next((p for p in ledger(db).get('positions',[])
                 if p['isin']==isin and Decimal(p['quantity'])>0),None)


def validate(db, info, result, args, now):
    """Inside paper_account's BEGIN IMMEDIATE, immediately before execution."""
    from trading_engine import VERSION as CORE_VERSION
    from paper_entry_modes import STRATEGY_CODES
    from paper_protection import decision, entry_plan, strategy_plan
    isin=info['isin']
    cfg=authority(db,isin,result.get('identity'))
    if result.get('hourly',{}).get('preparation_only'):
        raise ValueError('PAPER_PREPARATION_CANNOT_EXECUTE')
    manual=validate_analysis(db,info,result,now,cfg)
    engine=result['execution_context']['trading_engine']
    tf=result['timeframe']
    held=held_position(db,isin)
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
            if tf=='4H' and cfg.get(ENTRY_TIMEFRAME_KEY) is None:
                raise ValueError('PAPER_4H_ENTRY_ONLY')
            raise ValueError('PAPER_AUTO_TIMEFRAME_NOT_SELECTED')
    if result.get('quote_blocker'):
        raise ValueError(result['quote_blocker'])
    buy_setup=signal if side=='BUY' and mode!='V1' else None
    size=order_size(db,info,result,side,now,strategy=buy_setup,protective=protective)
    if Decimal(str(args.get('quantity')))!=Decimal(str(size['quantity'])):
        raise ValueError('PAPER_AUTO_SIZE_BLOCKED')
    signal_at=engine.get('timestamp')
    command=protective['request_id'] if protective else request_id(cfg,isin,signal_at,tf)
    if not signal_at or args.get('request_id')!=command:
        raise ValueError('PAPER_AUTO_REQUEST_MISMATCH')
    q=result.get('execution_quote',{})
    if q.get('identity')!=result['identity'] or q.get('isin')!=isin:
        raise ValueError('PAPER_AUTO_QUOTE_IDENTITY_BLOCKED')
    receipt=dict(context=VERSION,strategy='CORE:'+CORE_VERSION if mode=='V1' else STRATEGY_CODES[mode],
        strategy_mode=mode,strategy_label=STRATEGY_CODES[mode],timeframe=tf,signal=signal['action'],
        signal_at=signal_at,quote_timeframe=q['timeframe'],manual_risk=manual,epoch=cfg['epoch'],sizing=size)
    receipt['signal_reason']=signal.get('reason','')
    from gettex_first import decision_evidence
    receipt['data_context']=decision_evidence(result)
    if protective:
        receipt['protective_exit']=protective
    elif side=='BUY':
        if buy_setup and buy_setup.get('model'):
            receipt['strategy_model']=buy_setup['model']
            receipt['entry_evidence']={k:buy_setup[k] for k in ('opening_high','relative_volume','atr',
                'breadth_positive','breadth_available','breadth_required','breadth_inputs','setup','daily_down_closes','entry_level')}
            if buy_setup.get('report_date'):
                receipt['entry_evidence']['report_date']=buy_setup['report_date']
        plan=strategy_plan(result,buy_setup,mode,size,now) if buy_setup else entry_plan(cfg,result,size,now)
        if plan:receipt['protection']=plan
    return receipt


def request_id(cfg, isin, signal_at, timeframe='1H'):
    # Side/revision deliberately excluded: no second opposite trade on the same bar.
    return 'auto'+timeframe.lower()+'-' + digest([cfg['epoch'], isin, signal_at])


def order_command(db, info, result, now, *, manage=False):
    """Prepare one order on the existing PAPER account."""
    from paper_protection import decision, write_management
    isin=info['isin']
    cfg=authority(db,isin,result['identity'])
    engine=result['execution_context']['trading_engine']
    held=held_position(db,isin)
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
    if not (protective or original_exit or signal_allowed(cfg,signal_tf,side)):
        return None
    if result.get('quote_blocker'):raise ValueError(result['quote_blocker'])
    command=protective['request_id'] if protective else request_id(cfg,isin,engine['timestamp'],signal_tf)
    prior=db.execute('SELECT payload FROM paper_fills WHERE id=?',(command,)).fetchone()
    if prior:
        receipt=json.loads(prior[0])
        if receipt['side']!=side:raise ValueError('PAPER_REQUEST_REUSED')
        qty=float(receipt['quantity']);qty=int(qty) if qty.is_integer() else qty
    else:
        setup=signal if side=='BUY' and mode!='V1' else None
        qty=order_size(db,info,result,side,now,strategy=setup,protective=protective)['quantity']
    args=dict(isin=isin,timeframe=signal_tf,side=side,quantity=qty,request_id=command,_deadline=now+30)
    if protective:args['_protection']=protective
    return args


def reset(db, epoch):
    """Explicit owner reset only. Original receipts retained, never replayed."""
    from paper_account import initialize, ledger
    initialize(db)
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


def compact(result):
    engine = result.get('execution_context', {}).get('trading_engine', {})
    return dict(at=time.time(), calculation_seconds=result.get('_calculation_timing',{}).get('compute_seconds'),
        core=engine.get('status', 'UNKNOWN'), signal=engine.get('signal', 'WAIT'),
        signal_at=engine.get('timestamp'), reason=engine.get('reason'),
        quote_blocker=result.get('quote_blocker'),
        quote_at=(result.get('execution_quote', {}).get('bars') or [{}])[-1].get('close_at'))


def run_turn(app, info, values, cfg):
    """Evaluate the configured signal intervals and any existing protection."""
    from paper_account import execute
    from paper_entry_modes import STRATEGY_CODES
    from paper_preparation import retry_inputs
    isin=info['isin']
    with app.store.connect() as db:
        cfg=authority(db,isin)
        wake=retry_inputs(db,isin,cfg['identities'][isin])
        held=held_position(db,isin)
    reports={}
    decisions=[]
    filled=False
    for signal_tf in turn_timeframes(cfg,held):
        report=dict(timeframe=signal_tf,at=time.time())
        result=None
        try:
            result=app.compute_trading.calculate('paper_hourly',dict(values,timeframe=signal_tf))
            report.update(compact(result))
            mode,signal=strategy_signal(cfg,result,held)
            report.update(strategy=STRATEGY_CODES[mode],signal=signal['action'],reason=signal['reason'])
            from gettex_first import decision_evidence
            evidence=decision_evidence(result)
            evidence.update(strategy_signal=signal['action'],strategy_reason=signal['reason'])
            report['data_context_digest']=digest(evidence)
            decisions.append((signal_tf,evidence))
            ready=report['core']=='READY'
            report['state']='WAIT' if ready and signal['action']!='BLOCKED' else 'BLOCKED'
            managed=bool(held and held.get('protection'))
            if filled and report['signal']=='BUY':
                report.update(state='BLOCKED',execution_blocker='ValueError: PAPER_AUTO_POSITION_LIMIT')
                reports[signal_tf]=report
                continue
            if ready and not (held and held.get('auto_entry')) and not signal_allowed(cfg,signal_tf,'BUY'):
                report.update(state='PROTECTION_ONLY',reason='Контроль защиты открытой позиции')
            actionable=signal_allowed(cfg,signal_tf,report['signal']) or bool(
                held and held.get('auto_entry') and signal['action'] in ('BUY','SELL'))
            if ready and (actionable or managed or cfg.get('protection')):
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
                        if args and filled and args['side']=='BUY':
                            raise ValueError('PAPER_AUTO_FILL_ALREADY_IN_TURN')
                        answer=execute(db,info,result,args) if args else None
                if answer:
                    filled=filled or not answer.get('idempotent',False)
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
    retry=any(row.get('execution_blocker','').startswith(('InputRevisionChanged:','ValueError: PAPER_STALE_INPUTS'))
        for row in reports.values())
    selected=cfg.get(ENTRY_TIMEFRAME_KEY)
    primary=reports.get(selected or '1H') or next(iter(reports.values()))
    status=dict(primary,timeframes=reports,retry_inputs=wake,signal_timeframe=selected,
        strategy_mode=configured_strategy(cfg),
        next_check=time.time()+(15 if retry else 60))
    previous=app.store.setting(STATUS+isin,{}) if hasattr(app.store,'setting') else {}
    if callable(getattr(app.store,'event',None)):
        for interval,evidence in decisions:
            row=reports[interval];prior=previous.get('timeframes',{}).get(interval,{})
            if prior.get('data_context_digest')!=row.get('data_context_digest'):
                app.store.event('paper_data_decision',dict(at=row['at'],decision=evidence,
                    outcome={k:row.get(k) for k in ('state','signal','reason','quote_blocker','execution_blocker','request_id')}))
    app.store.set_setting(STATUS+isin,status)
    return status


class Runtime:
    """One bounded trading job; owned read client, existing Dispatcher and ledger."""
    def __init__(self, app):
        self.app = app;self.busy = False;self.cursor = 0;self.preparation_after = 0
        self.operational_turns = 0

    def start(self):
        self.app.root.after(15000, self.tick)

    def tick(self):
        app = self.app
        if app.closing:
            return
        cfg = app.store.setting(CONFIG, {})
        ids = list(cfg.get('identities', {})) if cfg.get('enabled') else []
        submitted = False
        preparation_due=bool(ids and cfg.get('automatic_admission',{}).get('enabled')
            and time.time() >= self.preparation_after)
        # With five or more admitted stocks the round-robin can always find a
        # due trading calculation. Reserve one existing lane slot after two
        # operational turns so sequential preparation cannot starve forever.
        yield_preparation=preparation_due and self.operational_turns>=2
        if ids and not self.busy and not app.user_dirty and not yield_preparation:
            isin = ids[self.cursor % len(ids)];self.cursor += 1
            info = next((i for i in app.watchlist if i['isin'] == isin), None)
            previous = app.store.setting(STATUS+isin, {})
            wake=previous.get('retry_inputs',{})
            inputs_changed=bool(wake) and app.store.read_cache.token_for(wake)!=wake
            if info and (inputs_changed or previous.get('signal_timeframe') != cfg.get(ENTRY_TIMEFRAME_KEY)
                         or previous.get('strategy_mode','V1') != cfg.get(STRATEGY_KEY,'V1')
                         or time.time() >= previous.get('next_check', 0)):
                values = dict(watchlist=[copy.deepcopy(info)], drafts={}, feed_errors={}, symbol=info['name'],
                    timeframe='1H', period='Max', ai_config=copy.deepcopy(app.ai_config),
                    instrument_revisions=app.store.read_cache.token_for(['instrument:'+isin]))
                self.busy = True
                submitted = True

                def work():
                    return run_turn(app,info,values,cfg)

                def done(_):
                    self.busy = False

                def failed(message):
                    self.busy = False
                    # Store persistence is queued, not performed by the UI callback.
                    report = dict(state='BLOCKED', at=time.time(), execution_blocker=message, next_check=time.time()+60)
                    app.job(lambda:app.store.set_setting(STATUS+isin, report), lambda _:None, lane='misc', key='paper_auto_error')

                if app.job(work, done, failed, lane='trading', key='paper_auto_hourly', isin=isin) is False:
                    self.busy = False
                else:self.operational_turns += 1
        # Idle trading slots only. The existing owned client and dispatcher do
        # one sequential candidate; no new threads, downloads or order path.
        if (ids and not submitted and not self.busy and not app.user_dirty
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
        app.root.after(15000, self.tick)

