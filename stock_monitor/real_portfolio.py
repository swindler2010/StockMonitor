"""V1 real-account records and read-only model advice. No broker transport."""
import copy
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

KEY = 'real_portfolio_v1'
VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def number(value, *, nonnegative=False):
    try:
        if value is None or isinstance(value, bool): raise ValueError('REAL_NUMBER_REQUIRED')
        result = Decimal(str(value))
        if not result.is_finite() or (nonnegative and result < 0): raise ValueError('REAL_NUMBER_INVALID')
        return result
    except (InvalidOperation, TypeError):
        raise ValueError('REAL_NUMBER_INVALID') from None


def money(value):
    return str(number(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def moment(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None: raise ValueError('REAL_TIMEZONE_REQUIRED')
    return result


def read(db):
    row = db.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    if row is None:return None
    state=json.loads(row[0])
    state.setdefault('estimated_realized_since_import','0.00')
    state.setdefault('realized_method','BROKER_RESULT_REQUIRED_AFTER_SALE')
    return state


def prepare(packet):
    """Check an observed broker snapshot; history is never replayed as new fills."""
    result = copy.deepcopy(packet)
    holdings, cash, overview = (result[k] for k in ('holdings', 'cash', 'overview'))
    account = holdings.get('portfolioId')
    if not account or cash.get('portfolioId') != account or overview.get('portfolioId') != account:
        raise ValueError('REAL_BROKER_ACCOUNT_MISMATCH')
    observed = moment(result['observed_at'])
    positions = []
    seen = set()
    for row in holdings.get('holdings', []):
        isin = row.get('isin')
        if not isinstance(isin, str) or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', isin) or isin in seen:
            raise ValueError('REAL_HOLDING_IDENTITY')
        seen.add(isin)
        quantity = number(row['position']['filled'], nonnegative=True)
        quote = row.get('currentQuote') or {}
        if quote and quote.get('currency') != 'EUR': raise ValueError('REAL_CURRENCY_UNSUPPORTED')
        if row.get('valuationCurrency') not in (None, 'EUR'): raise ValueError('REAL_CURRENCY_UNSUPPORTED')
        if quote.get('timestamp') and moment(quote['timestamp']) > observed:
            raise ValueError('REAL_QUOTE_AFTER_OBSERVATION')
        price = row.get('fifoPrice')
        cost = money(quantity * number(price, nonnegative=True)) if price is not None else None
        value = row.get('valuation')
        if value is not None: number(value, nonnegative=True)
        positions.append(dict(isin=isin, name=row.get('name', isin), quantity=str(quantity),
            cost=cost, cost_method='BROKER_FIFO' if cost is not None else 'UNKNOWN',
            fifo_price=None if price is None else str(number(price, nonnegative=True)),
            mark=None if quote.get('midPrice') is None else str(number(quote['midPrice'], nonnegative=True)),
            mark_at=quote.get('timestamp'), market_value=None if value is None else money(value),
            quote_venue='NOT_PROVIDED_BY_BROKER', blocked=str(number(row['position'].get('blocked', 0), nonnegative=True)),
            pending=str(number(row['position'].get('pending', 0))), levels={}))
    balances = cash['cash']
    for key in ('cashBalance', 'buyingPowerWithoutCredit', 'buyingPower'):
        number(balances.get(key))
    valuation = overview['valuation']
    for key in ('total', 'securities', 'crypto'): number(valuation.get(key))
    # Two independently delivered projections must reconcile to the cent.
    if all(p['market_value'] is not None for p in positions):
        assets = sum((number(p['market_value']) for p in positions), Decimal(0))
        if abs(assets-number(valuation['securities'])) > Decimal('.02'):
            raise ValueError('REAL_HOLDINGS_OVERVIEW_MISMATCH')
    if abs(number(valuation['total'])-number(valuation['securities'])-
           number(valuation['crypto'])-number(balances['cashBalance'])) > Decimal('.02'):
        raise ValueError('REAL_CASH_OVERVIEW_MISMATCH')
    transactions = result.get('transactions', [])
    ids = [row['id'] for row in transactions]
    if len(ids) != len(set(ids)): raise ValueError('REAL_DUPLICATE_TRANSACTION')
    if result.get('transactions_complete') is not True: raise ValueError('REAL_HISTORY_INCOMPLETE')
    return dict(version=VERSION, account='V1', mode='REAL_ACCOUNTING', currency='EUR',
        broker='Scalable Capital', broker_portfolio=account, observed_at=result['observed_at'],
        broker_timestamps=overview.get('timestamps', {}), broker_performance=overview.get('performance', []),
        broker_valuation=valuation, cash=money(balances['cashBalance']),
        broker_cash=balances, credit=cash.get('credit', {}), positions=positions,
        broker_transactions=transactions, transactions_complete=True, local_events=[],
        realized_since_import='0.00', estimated_realized_since_import='0.00',
        realized_method='BROKER_RESULT_REQUIRED_AFTER_SALE', fees_since_import='0.00', revision=0,
        execution_permission=False, broker_orders=False, input_digest=digest(packet))


def save_import(db, packet, expected_revision=None):
    proposed = prepare(packet)
    current = read(db)
    if current and current['input_digest'] == proposed['input_digest']: return current
    if current:
        if current['revision'] != expected_revision: raise ValueError('REAL_ACCOUNT_CHANGED')
        if current['broker_portfolio'] != proposed['broker_portfolio']: raise ValueError('REAL_BROKER_ACCOUNT_CHANGED')
        if current['local_events']: raise ValueError('REAL_RECONCILIATION_REQUIRED')
        if moment(proposed['observed_at']) <= moment(current['observed_at']): raise ValueError('REAL_OLDER_SNAPSHOT')
    elif expected_revision is not None:
        raise ValueError('REAL_ACCOUNT_CHANGED')
    proposed['revision'] = (current or {}).get('revision', 0)+1
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (KEY, json.dumps(proposed, ensure_ascii=False, allow_nan=False)))
    return proposed


def operation(db, event, expected_revision):
    """Owner-recorded broker execution or cash flow, not an order instruction."""
    state = read(db)
    if state is None: raise ValueError('REAL_ACCOUNT_NOT_INITIALIZED')
    event = copy.deepcopy(event)
    request_id = event.get('id')
    if not isinstance(request_id, str) or not 8 <= len(request_id) <= 160:
        raise ValueError('REAL_OPERATION_ID_REQUIRED')
    for previous in state['local_events']:
        if previous['id'] == request_id:
            if previous['input_digest'] != digest(event): raise ValueError('REAL_OPERATION_ID_CONFLICT')
            return state
    if state['revision'] != expected_revision: raise ValueError('REAL_ACCOUNT_CHANGED')
    at = moment(event['at'])
    if at < moment(state['observed_at']) or at > datetime.now(timezone.utc):
        raise ValueError('REAL_OPERATION_DATE_OUTSIDE_SNAPSHOT')
    if event.get('confirmed_execution') is not True: raise ValueError('REAL_EXECUTION_CONFIRMATION_REQUIRED')
    kind = event.get('kind')
    fee = number(event.get('fee', 0), nonnegative=True)
    cash = number(state['cash'])
    if kind in ('BUY', 'SELL'):
        isin = event.get('isin')
        if not isinstance(isin, str) or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', isin):
            raise ValueError('REAL_HOLDING_IDENTITY')
        qty = number(event['quantity'], nonnegative=True);price = number(event['price'], nonnegative=True)
        if qty <= 0 or price <= 0: raise ValueError('REAL_QUANTITY_PRICE_REQUIRED')
        position = next((p for p in state['positions'] if p['isin'] == isin), None)
        if position is None:
            if kind == 'SELL': raise ValueError('REAL_INSUFFICIENT_POSITION')
            position = dict(isin=isin, name=event.get('name', isin), quantity='0', cost='0.00',
                cost_method='OWNER_WEIGHTED_COST', mark=None, mark_at=None, market_value=None,
                quote_venue='NOT_PROVIDED', blocked='0', pending='0', levels={})
            state['positions'].append(position)
        old = number(position['quantity'])
        cost = None if position['cost'] is None else number(position['cost'])
        if kind == 'BUY':
            position['quantity'] = str(old+qty);cash -= qty*price+fee
            if cost is not None: position['cost'] = money(cost+qty*price+fee)
        else:
            if qty > old-number(position.get('blocked', 0)): raise ValueError('REAL_INSUFFICIENT_POSITION')
            basis = None if cost is None else cost*qty/old
            position['quantity'] = str(old-qty);cash += qty*price-fee
            if basis is not None:
                position['cost'] = money(cost-basis)
                if state['estimated_realized_since_import'] is not None:
                    state['estimated_realized_since_import'] = money(number(state['estimated_realized_since_import'])+qty*price-fee-basis)
            else: state['estimated_realized_since_import'] = None
            # The broker's remaining FIFO lots cannot be inferred from one
            # aggregate fifoPrice. Keep the actual realized result unknown.
            state['realized_since_import'] = None
        position['cost_method'] = 'OWNER_WEIGHTED_COST_ESTIMATE'
        position['fifo_price'] = None
        # A new execution price is not a current market quote.
        position['market_value'] = None
    elif kind in ('DEPOSIT', 'WITHDRAWAL', 'DIVIDEND', 'FEE'):
        amount = number(event['amount'], nonnegative=True)
        if amount <= 0: raise ValueError('REAL_AMOUNT_REQUIRED')
        cash += amount if kind in ('DEPOSIT', 'DIVIDEND') else -amount
        cash -= fee
        if kind == 'FEE': fee += amount
    else: raise ValueError('REAL_OPERATION_UNKNOWN')
    state['cash'] = money(cash)
    state['fees_since_import'] = money(number(state['fees_since_import'])+fee)
    state['local_events'].append({**event, 'input_digest':digest(event), 'source':'OWNER_RECORDED_EXECUTION'})
    state['revision'] += 1
    db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(state, ensure_ascii=False, allow_nan=False), KEY))
    return state


def write_operation(path, event, expected_revision):
    with closing(sqlite3.connect(path, timeout=.5)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            state = operation(db, event, expected_revision)
            db.commit()
            return state['revision']
        except Exception:
            db.rollback()
            raise


def write_levels(path, isin, stop, target, expected_revision):
    with closing(sqlite3.connect(path, timeout=.5)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            state = read(db)
            if state is None or state['revision'] != expected_revision: raise ValueError('REAL_ACCOUNT_CHANGED')
            position = next((p for p in state['positions'] if p['isin']==isin), None)
            if position is None: raise ValueError('REAL_HOLDING_IDENTITY')
            levels = {key:str(number(value, nonnegative=True)) for key,value in (('stop',stop),('target',target)) if value is not None}
            if any(number(v)<=0 for v in levels.values()): raise ValueError('REAL_LEVEL_INVALID')
            if len(levels)==2 and number(levels['stop'])>=number(levels['target']): raise ValueError('REAL_LEVEL_INVALID')
            position['levels'] = levels;state['revision'] += 1
            db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(state,ensure_ascii=False,allow_nan=False),KEY))
            db.commit()
            return state['revision']
        except Exception:
            db.rollback();raise


def status(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as db:
        db.execute('BEGIN')
        state = read(db)
        if state is None: return dict(status='NOT_INITIALIZED', account='V1')
        instruments = {json.loads(row[0])['isin']:json.loads(row[0]) for row in db.execute('SELECT payload FROM instruments')}
        state['instruments'] = [{'isin':isin,'name':info['name']} for isin,info in instruments.items() if info.get('kind','stock')=='stock']
        for p in state['positions']:
            p['registered'] = p['isin'] in instruments
            p['unrealized'] = (money(number(p['market_value'])-number(p['cost']))
                if p['market_value'] is not None and p['cost'] is not None else None)
        state['status'] = 'AVAILABLE'
        state['broker_snapshot_current'] = False  # explicit dated observation; no live broker stream
        return state


def footer_status(path):
    """The main screen's V1 balance, never the currently selected PAPER book."""
    from zoneinfo import ZoneInfo
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        state=read(db)
    result=dict(account='V1',mode='REAL_ACCOUNTING',equity=None,daily_pnl=None)
    if state is None or state['local_events']:return result
    result['equity']=state['broker_valuation'].get('total')
    stamp=state['broker_timestamps'].get('valuationTimestampUtc')
    zone=ZoneInfo('Europe/Berlin')
    if stamp and moment(stamp).astimezone(zone).date()==datetime.now(zone).date():
        result['daily_pnl']=next((p['simpleAbsoluteReturn'] for p in state['broker_performance'] if p['timeframe']=='INTRADAY'),None)
    return result


def calculate(store, values):
    """Existing closed CORE + V1 rules, using REAL quantity and read-only inputs."""
    from analytical_read import read_time
    from trading_context import plan, calculate as context
    from paper_hourly import strategy_signal
    from paper_v1_long_term import PROFILE_KEY, MODEL
    from gettex_chart import state_key, confirmed_state
    from calculation_window import context_deadline
    from fundamentals_view import cache_matches, freshness
    store.read_cache.depend('setting:'+KEY)
    info = values['watchlist'][0]
    fundamentals_key = 'fundamentals:'+info['isin']
    store.read_cache.depend('setting:'+fundamentals_key)
    with store.connect() as db:
        account = read(db)
        if account is None: raise ValueError('REAL_ACCOUNT_NOT_INITIALIZED')
        if account['revision'] != values['real_revision']: raise ValueError('REAL_ACCOUNT_CHANGED')
        listing_key = state_key(info['isin'], '1D')
        row = db.execute('SELECT value FROM settings WHERE key=?', (listing_key,)).fetchone()
        listing = json.loads(row[0]) if row else {}
        row = db.execute('SELECT value FROM settings WHERE key=?', (fundamentals_key,)).fetchone()
        try:
            fundamentals = json.loads(row[0]) if row else {}
        except (ValueError, TypeError):
            fundamentals = {}
    store.read_cache.depend('setting:'+listing_key)
    if not confirmed_state(info, listing): raise ValueError('REAL_LISTING_UNCONFIRMED')
    identity = listing['identity']
    held = next((p for p in account['positions'] if p['isin']==info['isin'] and number(p['quantity'])>0), {})
    position = {'quantity':held['quantity']} if held else {}
    now = read_time()
    company = {}
    if cache_matches(fundamentals, info) and fundamentals.get('symbol'):
        metrics = fundamentals.get('metrics') or []
        company = dict(updated_at=fundamentals.get('updated_at'), freshness=freshness(fundamentals, now), metrics=[])
        for metric in ('TotalRevenue', 'DilutedEPS', 'FreeCashFlow'):
            candidates = [r for r in metrics if isinstance(r, dict) and r.get('metric')==metric
                          and r.get('frequency')=='quarterly' and isinstance(r.get('period_end'),str)
                          and r['period_end'] <= now.date().isoformat()]
            if candidates:
                latest = max(candidates, key=lambda r:r['period_end'])
                company['metrics'].append({key:latest.get(key) for key in
                    ('metric','value','currency','period_end','cache_status','updated_at')})
    calculated = context(store, dict(values, _paper_v1_long_term=True), identity, plan('1D'), position, now)
    engine = calculated['engine']
    result = dict(isin=info['isin'], timeframe='1D', execution_context={'trading_engine':engine})
    _, decision = strategy_signal({'strategy_mode':'V1',PROFILE_KEY:MODEL}, result, position)
    return dict(account='V1', account_revision=account['revision'], isin=info['isin'],
        identity=identity, generated_at=now.isoformat(), core=engine.get('status'),
        valid_until=context_deadline(identity,'1D',now).isoformat(),
        signal=decision['action'], reason=decision.get('reason'), signal_at=engine.get('timestamp'),
        model=MODEL, real_quantity=held.get('quantity','0'), company=company,
        long_term=engine.get('v1_long_term',{}), execution_permission=False,
        quote_is_broker_execution_price=False, proof=calculated['proof'])
