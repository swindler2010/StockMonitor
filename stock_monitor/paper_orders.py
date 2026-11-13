"""Explicit owner orders over the existing PAPER executor, never a V1 signal.

The GUI and Bridge share this path. Analysis/quote/risk proofs are unchanged;
only the decision to BUY/SELL and quantity come from the user's instruction.
"""
import copy
import math
import sqlite3
import time
from concurrent.futures import Future
from decimal import Decimal

PAIR = {'US0378331005': 'Apple', 'US67066G1040': 'NVIDIA'}
VERSION = 'owner_paper_order_v1'


def check_intent(intent, isin):
    qty = intent.get('quantity')
    if (intent.get('version') != VERSION or isin not in PAIR
            or intent.get('isin') != isin or intent.get('initiator') not in ('MANUAL', 'CHATGPT')
            or intent.get('side') not in ('BUY', 'SELL') or intent.get('quote_tf') not in ('5m', '15m', 'AUTO')
            or not isinstance(intent.get('request_id'), str) or not 8 <= len(intent['request_id']) <= 160):
        raise ValueError('PAPER_ORDER_INTENT_INVALID')
    if (isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty)
            or qty <= 0 or Decimal(str(qty)).as_tuple().exponent < -6):
        raise ValueError('PAPER_BAD_QUANTITY')


def quote_timeframes(intent):
    return ('15m', '5m') if not intent or intent['quote_tf']=='AUTO' else (intent['quote_tf'],)


def check_binding(result, args):
    intent = result.get('discretionary', {})
    check_intent(intent, args.get('isin'))
    if (any(intent.get(k) != args.get(k) for k in ('isin', 'side', 'quantity', 'request_id'))
            or args.get('_initiator') != intent['initiator']
            or args.get('_quote_tf') != intent['quote_tf']
            or result.get('timeframe') != '1H' or args.get('timeframe') != '1H'
            or result.get('hourly', {}).get('preparation_only') or args.get('_protection')):
        raise ValueError('PAPER_ORDER_BINDING_CHANGED')


def validate(db, info, result, args, now):
    from paper_hourly import authority, validate_analysis, order_size, SIZING
    from paper_account import POLICY, money
    from paper_protection import entry_plan
    check_binding(result, args)
    cfg = authority(db, info['isin'], result.get('identity'), allow_paused=True)
    manual = validate_analysis(db, info, result, now, cfg)
    if result.get('quote_blocker'):
        raise ValueError(result['quote_blocker'])
    q = result.get('execution_quote', {})
    if (q.get('identity') != result['identity'] or q.get('isin') != info['isin']
            or q.get('timeframe') not in quote_timeframes(result['discretionary'])):
        raise ValueError('PAPER_AUTO_QUOTE_IDENTITY_BLOCKED')
    size = order_size(db, info, result, args['side'], now, allow_paused=True)
    quantity = Decimal(str(args['quantity']))
    if quantity > Decimal(str(size['quantity'])):
        raise ValueError('PAPER_ORDER_RISK_SIZE_LIMIT' if args['side'] == 'BUY' else 'PAPER_INSUFFICIENT_POSITION')
    size['maximum_quantity'] = size['quantity']
    size['quantity'] = args['quantity']
    if size.get('mode') == SIZING:
        fee = money(POLICY['commission'])
        size['estimated_risk'] = str(Decimal(size['reference_risk_per_share'])*quantity+2*fee)
        price = Decimal(str(q['bars'][-1]['close']))*(1+Decimal(POLICY['spread_bps'])/20000)
        size['gross_and_fee'] = str(money(price*quantity)+fee)
    engine = result['execution_context']['trading_engine']
    receipt = dict(context=VERSION, initiator=result['discretionary']['initiator'],
        timeframe='1H', signal=engine.get('signal'), signal_at=engine.get('timestamp'),
        decision=args['side'], quote_timeframe=q['timeframe'], epoch=cfg['epoch'],
        manual_risk=manual, sizing=size, strategy='OWNER_INSTRUCTION')
    if args['side'] == 'BUY':
        plan = entry_plan(cfg, result, size, now)
        if plan: receipt['protection'] = plan
    return receipt


def denial(error, result=None):
    message = str(error)
    code = message if message.startswith('PAPER_') else (
        'PAPER_STALE_INPUTS' if 'InputRevisionChanged' in message else
        'PAPER_BUSY' if 'database is locked' in message else 'PAPER_EXECUTION_UNAVAILABLE')
    engine = (result or {}).get('execution_context', {}).get('trading_engine', {})
    descriptions = {
        'PAPER_STALE_QUOTE':'Цена исполнения устарела; ожидается свежая закрытая свеча.',
        'PAPER_STALE_INPUTS':'Данные изменились во время проверки; повторите поручение с новым расчётом.',
        'PAPER_STALE_WINDOW':'Срок расчёта истёк; требуется новая проверка.',
        'PAPER_BUSY':'Исполнитель занят; повторите после завершения текущей проверки.',
        'PAPER_ORDER_RISK_SIZE_LIMIT':'Количество превышает разрешённый размер по риску или доступным средствам.',
        'PAPER_AUTO_POSITION_LIMIT':'Позиция уже открыта; повторная покупка запрещена.',
        'PAPER_INSUFFICIENT_POSITION':'Для продажи недостаточно акций на демосчёте.',
        'PAPER_INSUFFICIENT_CASH':'Недостаточно свободных средств с учётом комиссии.',
        'PAPER_SAVE_DRAFTS_REQUIRED':'Сначала сохраните изменения в программе.',
        'PAPER_BRIDGE_DISABLED':'Разрешение на команды через Bridge выключено.',
        'PAPER_SESSION_CLOSED':'Торговая сессия закрыта.',
        'PAPER_BAD_QUANTITY':'Количество должно быть положительным, не более шести знаков после запятой.',
    }
    detail=descriptions.get(code,code)
    if code=='PAPER_ENGINE_DATA_BLOCKED':detail=engine.get('reason') or 'CORE не подтвердил пригодность данных.'
    if code=='PAPER_AUTO_DQ_BLOCKED':
        detail='Качество данных не подтверждено: '+', '.join(
            tf+' '+str(row.get('data_quality',{}).get('state','UNKNOWN'))
            for tf,row in engine.get('timeframe_context',{}).items()
            if row.get('data_quality',{}).get('state')!='READY')
    return dict(status='BLOCKED', code=code, detail=detail)


def submit(app, args, initiator, preview=False):
    """UI captures only; owned trading worker calculates AND commits off Tk."""
    future = Future()
    try:
        args = copy.deepcopy(args)
        intent = dict(version=VERSION, initiator=initiator, isin=args.get('isin'),
            side=args.get('side'), quantity=args.get('quantity'), request_id=args.get('request_id'),
            quote_tf=args.get('timeframe', '15m'))
        check_intent(intent, args.get('isin'))
        if app.user_dirty: raise ValueError('PAPER_SAVE_DRAFTS_REQUIRED')
        if app.closing or getattr(app, '_paper_busy', False): raise ValueError('PAPER_BUSY')
        info = copy.deepcopy(next(i for i in app.watchlist if i['isin'] == args['isin']))
        args.update(timeframe='1H', _initiator=initiator, _quote_tf=intent['quote_tf'])
        args.setdefault('_deadline', time.time()+110)
        values = dict(watchlist=[info], drafts={}, feed_errors={}, symbol=info['name'],
            timeframe='1H', period='Max', ai_config=copy.deepcopy(app.ai_config), _paper_order=intent,
            instrument_revisions=app.store.read_cache.token_for(['instrument:'+info['isin']]))
    except Exception as error:
        future.set_result(denial(error));return future
    app._paper_busy = True

    def work():
        result = None
        try:
            if time.time() >= args['_deadline']: raise ValueError('PAPER_EXPIRED')
            result = app.compute_trading.calculate('paper_hourly', values)
            if app.closing or app.user_dirty: raise ValueError('PAPER_SAVE_DRAFTS_REQUIRED')
            from paper_account import execute, ledger
            with sqlite3.connect(app.store.path, timeout=.2) as db:
                db.execute('BEGIN IMMEDIATE')
                if initiator == 'CHATGPT':
                    from paper_hourly import read
                    if read(db, 'mcp_allow_edits', False) is not True:
                        raise ValueError('PAPER_BRIDGE_DISABLED')
                if preview:
                    # Roll back the real executor's transaction. The estimate thus
                    # passes the same final guards as an order, without a journal row.
                    answer = execute(db, info, result, args)
                    db.rollback()
                    return dict(status='PREVIEW', fill=answer['fill'], position=ledger(db)['positions'],
                        detail='Оценка цены; при нажатии BUY/SELL данные проверяются заново.')
                return execute(db, info, result, args)
        except Exception as error:
            return denial(error, result)

    def done(answer):
        app._paper_busy = False
        if not future.done(): future.set_result(answer)

    def failed(message): done(denial(message))
    try:
        if app.job(work, done, failed, lane='trading', key='paper_owner_order', isin=info['isin']) is False:
            failed('PAPER_BUSY')
    except Exception as error: failed(error)
    return future
