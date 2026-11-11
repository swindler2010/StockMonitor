"""Owner-enabled hourly demo runner. Reuses CORE, Dispatcher and paper executor.

No feed, broker, alternative indicators, inferred gap permission or market orders.
One position per authorized instrument; one deterministic operation per 1H bar.
"""
import copy
import hashlib
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

CONFIG = 'paper_auto_1h'
EPOCH = 'paper_epoch'
STATUS = 'paper_auto_status:'
VERSION = 'core_hourly_demo_v1'
SIZING = 'cash_volatility_v1'


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


def authority(db, isin, identity=None):
    from paper_account import ACCOUNT
    cfg = read(db, CONFIG, {})
    if (cfg.get('version') != VERSION or cfg.get('enabled') is not True
            or cfg.get('approved') is not True or cfg.get('account') != ACCOUNT
            or cfg.get('timeframe') != '1H'
            or not (variable_size(cfg) or ('sizing' not in cfg and cfg.get('quantity') == 1 and cfg.get('max_position') == 1))
            or cfg.get('epoch') != read(db, EPOCH)
            or isin not in cfg.get('identities', {})):
        raise ValueError('PAPER_AUTO_NOT_AUTHORIZED')
    exact = cfg['identities'][isin]
    if (exact.get('source') != 'gettex_chart' or exact.get('venue') != 'GETTEX'
            or exact.get('currency') != 'EUR' or exact.get('timezone') != 'Europe/Berlin'
            or (identity is not None and identity != exact)):
        raise ValueError('PAPER_AUTO_IDENTITY_BLOCKED')
    return cfg


def authorize_hourly(db, isin, identity):
    # Historical-risk analysis may examine the one owner-approved preparation
    # candidate. Execution still requires authority(), which excludes it.
    try:authority(db, isin, identity)
    except ValueError:
        from paper_preparation import candidate_authority
        candidate_authority(db, isin, identity)


def ledger_token(db):
    return digest(db.execute('SELECT id,digest FROM paper_fills ORDER BY rowid').fetchall())


def order_size(db, info, result, side, now):
    """Integer entry size from free cash and existing CORE volatility/structure.

    This is a sizing estimate, not a stop order or a guaranteed loss ceiling.
    CORE decides entries/exits; an explicit optional PAPER bracket can add
    protective exits. No leverage, averaging or new indicators.
    """
    from paper_account import ledger, quote, POLICY, money
    from eur_listings import stored_identity
    cfg = authority(db, info['isin'], result.get('identity'))
    state = ledger(db)
    held = [p for p in state['positions'] if p['isin'] == info['isin'] and Decimal(p['quantity'])]
    if side == 'SELL':
        if len(held) != 1 or held[0]['identity'] != result['identity']:
            raise ValueError('PAPER_INSUFFICIENT_POSITION')
        qty = Decimal(held[0]['quantity'])
        if not variable_size(cfg) and qty != 1:
            raise ValueError('PAPER_INSUFFICIENT_POSITION')
        return dict(quantity=int(qty) if qty == int(qty) else float(qty), mode='FULL_POSITION')
    if side != 'BUY':
        raise ValueError('PAPER_AUTO_SIGNAL_MISMATCH')
    if held:
        raise ValueError('PAPER_AUTO_POSITION_LIMIT')
    if not variable_size(cfg):
        return dict(quantity=1, mode='FIXED_ONE')
    q = result.get('execution_quote', {})
    if q.get('identity') != result['identity']:
        raise ValueError('PAPER_AUTO_QUOTE_IDENTITY_BLOCKED')
    _, bar = quote(info, q, now,
        stored_listing=stored_identity(db, info, result['identity'], q.get('timeframe')))
    facts = result.get('execution_context', {}).get('trading_engine', {}).get('timeframe_context', {}).get('1H', {}).get('facts', {})
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
    allocation = money(cash*Decimal(cfg['sizing']['allocation_fraction']))
    risk_budget = money(cash*Decimal(cfg['sizing']['risk_fraction']))
    # Two ATR or distance below existing support, whichever is larger, plus spread.
    distance = max(2*atr, reference-support*Decimal('.995')) + 2*reference*half
    qty = min(int((allocation-fee)//price), int((risk_budget-2*fee)//distance))
    while qty > 0 and (money(price*qty)+fee > min(cash, allocation)
            or distance*qty+2*fee > risk_budget):
        qty -= 1
    if qty < 1:
        raise ValueError('PAPER_AUTO_SIZE_BELOW_ONE')
    return dict(quantity=qty, mode=SIZING, cash_basis=str(cash), allocation_limit=str(allocation),
        risk_budget=str(risk_budget), reference_risk_per_share=str(distance),
        estimated_risk=str(distance*qty+2*fee), gross_and_fee=str(money(price*qty)+fee),
        risk_is_estimate=True, target_equity=cfg['target_equity'])


def fresh_quote(store, info, identity, now):
    """Current real 15m/5m quote, independent of the hourly signal's price."""
    import pandas as pd
    import data_quality as dq
    from analytics import bar_ends
    from eur_listings import stored_identity
    from paper_account import quote, POLICY
    candidates = []
    with store.connect() as db:
        for tf in ('15m', '5m'):
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
                    candidates.append((end, tf, frame))
        if not candidates:
            raise ValueError('PAPER_NO_CLOSED_BAR')
        end, tf, frame = max(candidates, key=lambda c: (c[0], c[1] == '15m'))
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
            cfg = authority(db, info['isin'])
            identity = cfg['identities'][info['isin']]
        position = next((p for p in ledger(db)['positions'] if p['isin'] == info['isin'] and Decimal(p['quantity'])), {})
        position_token = ledger_token(db)
    now = pd.Timestamp.now(tz='UTC')

    contract=plan('1H')
    calculated=calculate_context(store,values,identity,contract,position,now)
    engine,proof,manual=(calculated[k] for k in ('engine','proof','manual'))
    result = dict(isin=info['isin'], timeframe='1H', identity=identity,
        execution_context=dict(status='MANUAL_RISK_ACCEPTED' if manual else 'HOURLY_DEMO_READY',
            isin=info['isin'], checked_at=now.isoformat(), analysis_ready=engine.get('status') == 'READY',
            trading_engine=engine, manual_risk=manual, manual_inputs=proof),
        hourly=dict(version=VERSION, config_digest=digest(cfg), ledger_token=position_token,
            epoch=cfg['epoch'], checked_at=now.isoformat(),
            expires_at=min(now+pd.Timedelta(seconds=60), context_deadline(identity, '1H', now)).isoformat(),
            proof=proof, required_tf=contract['required_tf'],timeframe_contract=contract))
    if preparing:result['hourly']['preparation_only']=True
    # Analysis is useful even while prices are stale. Its status cannot place a fill.
    try:
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
    from performance_cache import ReadCache,validate_closed_inputs,InputRevisionChanged
    from cp_controls import manual_gate
    def quote_tail(db, result):
        meta=result['identity'];rows=[]
        for tf in ('5m','15m'):
            rows.append((tf,db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE '
                'isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? '
                'ORDER BY ts DESC LIMIT 4',(result['isin'],meta['source'],meta['venue'],meta['currency'],meta['symbol'],tf)).fetchall()))
        return rows
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
            if (row[0] if row else 0)!=revision:
                raise InputRevisionChanged('Hourly input changed: '+scope)
            current[scope]=revision
        manual=result['execution_context'].get('manual_risk')
        if manual is not None and manual_gate(db,result['isin'],result['identity'],'1H',proof)!=manual:
            raise InputRevisionChanged('Hourly historical-risk permission changed')
    result['_input_revisions']=current
    return result


def validate_analysis(db, info, result, now, cfg):
    """Shared closed CORE proof; this function cannot authorize an order."""
    import pandas as pd
    from performance_cache import validate_closed_inputs
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
    required = set(profile('1H'))
    own = [s for s in h.get('proof', {}).get('series', []) if s['spec'].get('isin') == info['isin']]
    if (set(h.get('required_tf', [])) != required
            or not required <= {s['spec'].get('timeframe') for s in own if s.get('count', 0) >= 35}
            or any(s['spec'].get('identity') != result['identity'] for s in own)):
        raise ValueError('PAPER_AUTO_DEPENDENCY_BLOCKED')
    validate_closed_inputs(db, h['proof'])
    manual = None
    if contract.get('status') == 'MANUAL_RISK_ACCEPTED':
        manual = manual_gate(db, info['isin'], result['identity'], '1H', h['proof'])
        if manual != contract.get('manual_risk'):
            raise ValueError('PAPER_MANUAL_CONTEXT_CHANGED')
    elif contract.get('status') != 'HOURLY_DEMO_READY' or any(
            engine.get('timeframe_context', {}).get(tf, {}).get('data_quality', {}).get('state') != 'READY'
            for tf in required):
        raise ValueError('PAPER_AUTO_DQ_BLOCKED')
    if not benchmark_ready(engine.get('benchmark_context', {})):
        raise ValueError('PAPER_AUTO_BENCHMARK_BLOCKED')
    return manual


def validate(db, info, result, args, now):
    """Inside paper_account's BEGIN IMMEDIATE, immediately before execution."""
    from trading_engine import VERSION as CORE_VERSION
    cfg = authority(db, info['isin'], result.get('identity'))
    if result.get('hourly',{}).get('preparation_only'):
        raise ValueError('PAPER_PREPARATION_CANNOT_EXECUTE')
    manual = validate_analysis(db,info,result,now,cfg)
    engine = result.get('execution_context',{}).get('trading_engine',{})
    protective = None
    if args.get('_protection'):
        from paper_protection import decision
        protective = decision(db, info, result, now)
        if not protective or protective != args['_protection'] or args.get('side') != 'SELL':
            raise ValueError('PAPER_PROTECTION_TRIGGER_CHANGED')
    if not protective and (engine.get('signal') not in ('BUY','SELL') or engine['signal'] != args.get('side')):
        raise ValueError('PAPER_AUTO_SIGNAL_MISMATCH')
    size = order_size(db, info, result, args['side'], now)
    if Decimal(str(args.get('quantity'))) != Decimal(str(size['quantity'])):
        raise ValueError('PAPER_AUTO_SIZE_BLOCKED')
    signal_at = engine.get('timestamp')
    command = protective['request_id'] if protective else request_id(cfg, info['isin'], signal_at)
    if not signal_at or args.get('request_id') != command:
        raise ValueError('PAPER_AUTO_REQUEST_MISMATCH')
    if result.get('quote_blocker'):
        raise ValueError(result['quote_blocker'])
    q = result.get('execution_quote', {})
    if q.get('identity') != result['identity'] or q.get('isin') != info['isin']:
        raise ValueError('PAPER_AUTO_QUOTE_IDENTITY_BLOCKED')
    receipt = dict(context=VERSION, strategy='CORE:'+CORE_VERSION, timeframe='1H', signal=engine['signal'],
        signal_at=signal_at, quote_timeframe=q['timeframe'], manual_risk=manual, epoch=cfg['epoch'], sizing=size)
    if protective:receipt['protective_exit']=protective
    elif args['side']=='BUY':
        from paper_protection import entry_plan
        plan=entry_plan(cfg,result,size,now)
        if plan:receipt['protection']=plan
    return receipt


def request_id(cfg, isin, signal_at):
    # Side/revision deliberately excluded: no second opposite trade on the same bar.
    return 'auto1h-' + digest([cfg['epoch'], isin, signal_at])


def order_command(db, info, result, now):
    """Existing CORE order, or an explicitly configured protective exit."""
    from paper_protection import decision
    isin=info['isin'];cfg=authority(db,isin,result['identity'])
    engine=result['execution_context']['trading_engine']
    protective=decision(db,info,result,now)
    side=protective['side'] if protective else engine.get('signal')
    if side not in ('BUY','SELL'):return None
    command=protective['request_id'] if protective else request_id(cfg,isin,engine['timestamp'])
    prior=db.execute('SELECT payload FROM paper_fills WHERE id=?',(command,)).fetchone()
    if prior:
        receipt=json.loads(prior[0])
        if receipt['side']!=side:raise ValueError('PAPER_REQUEST_REUSED')
        qty=float(receipt['quantity']);qty=int(qty) if qty.is_integer() else qty
    else:qty=order_size(db,info,result,side,now)['quantity']
    args=dict(isin=isin,timeframe='1H',side=side,quantity=qty,request_id=command,_deadline=now+30)
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


class Runtime:
    """One bounded trading job; owned read client, existing Dispatcher and ledger."""
    def __init__(self, app):
        self.app = app;self.busy = False;self.cursor = 0;self.preparation_after = 0

    def start(self):
        self.app.root.after(15000, self.tick)

    def tick(self):
        app = self.app
        if app.closing:
            return
        cfg = app.store.setting(CONFIG, {})
        ids = list(cfg.get('identities', {})) if cfg.get('enabled') else []
        submitted = False
        if ids and not self.busy and not app.user_dirty:
            isin = ids[self.cursor % len(ids)];self.cursor += 1
            info = next((i for i in app.watchlist if i['isin'] == isin), None)
            previous = app.store.setting(STATUS+isin, {})
            if info and time.time() >= previous.get('next_check', 0):
                values = dict(watchlist=[copy.deepcopy(info)], drafts={}, feed_errors={}, symbol=info['name'],
                    timeframe='1H', period='Max', ai_config=copy.deepcopy(app.ai_config),
                    instrument_revisions=app.store.read_cache.token_for(['instrument:'+isin]))
                self.busy = True
                submitted = True

                def work():
                    # All computation, proof verification and the write transaction stay off Tk.
                    result = app.compute_trading.calculate('paper_hourly', values)
                    report = compact(result)
                    engine = result['execution_context']['trading_engine']
                    report['state'] = 'WAIT' if engine.get('status') == 'READY' else 'BLOCKED'
                    # Protection shares the exact CORE/quote/transaction guards;
                    # WAIT alone neither causes a fill nor disables a valid stop.
                    if engine.get('status') == 'READY' and (engine.get('signal') in ('BUY','SELL') or cfg.get('protection')):
                        from paper_account import execute
                        try:
                            if app.closing or app.user_dirty:
                                raise ValueError('PAPER_APP_NOT_READY')
                            with sqlite3.connect(app.store.path, timeout=.2) as db:
                                db.execute('BEGIN IMMEDIATE')
                                args = order_command(db,info,result,time.time())
                                answer = execute(db, info, result, args) if args else None
                            if answer:
                                report.update(state='COMPLETED', request_id=args['request_id'], quantity=args['quantity'],
                                    idempotent=answer.get('idempotent', False),
                                    sizing=answer.get('fill', {}).get('smart_eligibility', {}).get('sizing'),
                                    protective_exit=answer.get('fill', {}).get('smart_eligibility', {}).get('protective_exit'))
                        except Exception as error:
                            report.update(state='BLOCKED', execution_blocker=type(error).__name__+': '+str(error))
                    report['next_check'] = time.time()+60
                    app.store.set_setting(STATUS+isin, report)
                    return report

                def done(_):
                    self.busy = False

                def failed(message):
                    self.busy = False
                    # Store persistence is queued, not performed by the UI callback.
                    report = dict(state='BLOCKED', at=time.time(), execution_blocker=message, next_check=time.time()+60)
                    app.job(lambda:app.store.set_setting(STATUS+isin, report), lambda _:None, lane='misc', key='paper_auto_error')

                if app.job(work, done, failed, lane='trading', key='paper_auto_hourly', isin=isin) is False:
                    self.busy = False
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
        app.root.after(15000, self.tick)
