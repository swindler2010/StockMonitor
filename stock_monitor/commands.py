"""Transactional application edits. This module cannot submit broker orders."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone

MUTATIONS = {'add_instrument', 'remove_instrument', 'update_position', 'record_decision', 'review_decision'}
ACTIONS = MUTATIONS | {'decision_journal'}
SIGNALS = ['BUY', 'HOLD', 'SELL', 'ADD', 'REDUCE', 'STOP']
REVIEWS = ['correct', 'false', 'late', 'early_exit', 'bad_stop', 'missed_opportunity', 'unassessed']

def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)

def number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError('Expected a finite positive number' if positive else 'Expected a finite nonnegative number')
    return value

def isin_valid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d', value):
        raise ValueError('Invalid ISIN')
    digits = ''.join(str(ord(c)-55) if c.isalpha() else c for c in value)
    total = 0
    for n, ch in enumerate(reversed(digits)):
        v = int(ch) * (2 if n % 2 else 1)
        total += v // 10 + v % 10
    if total % 10:
        raise ValueError('Invalid ISIN checksum')
    return value

def text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Missing or overlong text')
    return value.strip()

def execute(store, action, args, snapshot=None):
    if action not in ACTIONS or not isinstance(args, dict):
        raise ValueError('Unsupported application command')
    permitted = {
        'add_instrument': {'request_id', 'instrument'},
        'remove_instrument': {'request_id', 'isin'},
        'update_position': {'request_id', 'isin', 'quantity', 'average_price', 'currency', 'stop', 'target', 'support', 'resistance'},
        'record_decision': {'request_id', 'isin', 'signal', 'reason', 'comment', 'scenario', 'model_quantity'},
        'review_decision': {'request_id', 'decision_id', 'classification', 'reason'},
        'decision_journal': {'limit', 'isin'},
    }
    if set(args) - permitted[action]:
        raise ValueError('Unknown arguments')
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS command_receipts (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, isin TEXT NOT NULL, payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS decision_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL)')
        if action == 'decision_journal':
            limit = args.get('limit', 100)
            if type(limit) is not int or not 1 <= limit <= 500:
                raise ValueError('Limit must be 1..500')
            where, values = (' WHERE isin=?', [isin_valid(args['isin'])]) if args.get('isin') else ('', [])
            rows = db.execute('SELECT id,at,isin,payload FROM decisions'+where+' ORDER BY id DESC LIMIT ?', values+[limit]).fetchall()
            return {'decisions': [{'id': r[0], 'at': r[1], 'isin': r[2], **json.loads(r[3]), 'reviews': [json.loads(x[0]) for x in db.execute('SELECT payload FROM decision_reviews WHERE decision_id=? ORDER BY id', (r[0],))]} for r in rows], 'execution': 'recommendations only; no broker trades'}
        request_id = text(args.get('request_id'), 128)
        digest = hashlib.sha256(encoded([action, args]).encode()).hexdigest()
        db.execute('BEGIN IMMEDIATE')
        receipt = db.execute('SELECT digest,result FROM command_receipts WHERE id=?', (request_id,)).fetchone()
        if receipt:
            if receipt[0] != digest:
                raise ValueError('request_id already used for different arguments')
            return json.loads(receipt[1])
        at = datetime.now(timezone.utc).isoformat()
        item = None
        if action in {'remove_instrument', 'update_position', 'record_decision'}:
            isin = isin_valid(args.get('isin'))
            row = db.execute('SELECT payload FROM instruments WHERE isin=?', (isin,)).fetchone()
            if row is None:
                raise ValueError('Instrument does not exist')
            item = json.loads(row[0])
        if action == 'add_instrument':
            supplied = args.get('instrument')
            allowed = {'isin', 'name', 'symbol', 'currency', 'venue', 'profile_symbol', 'history_symbol'}
            if not isinstance(supplied, dict) or set(supplied)-allowed:
                raise ValueError('Invalid instrument fields')
            item = {k: text(v, 160) for k, v in supplied.items()}
            isin = isin_valid(item.get('isin'))
            text(item.get('name')); text(item.get('symbol'))
            if not re.fullmatch('[A-Z]{3}', item.get('currency', '')):
                raise ValueError('Explicit currency required')
            if db.execute('SELECT 1 FROM instruments WHERE isin=?', (isin,)).fetchone():
                raise ValueError('Instrument already exists')
            if any(json.loads(r[0]).get('name') == item['name'] for r in db.execute('SELECT payload FROM instruments')):
                raise ValueError('Instrument name already exists')
            item.update(position={}, levels={}, drawings=[], source_mode='gettex')
            db.execute('INSERT INTO instruments VALUES (?,?)', (isin, encoded(item)))
            result = {'saved': True, 'instrument': item}
        elif action == 'remove_instrument':
            if db.execute('SELECT count(*) FROM instruments').fetchone()[0] <= 1:
                raise ValueError('Keep at least one instrument')
            db.execute('DELETE FROM instruments WHERE isin=?', (isin,))
            result = {'removed': isin, 'history_retained': True}
        elif action == 'update_position':
            if len(args) <= 2:
                raise ValueError('No fields to update')
            for key in ('quantity', 'average_price', 'stop', 'target', 'support', 'resistance'):
                if key in args:
                    value = args[key]
                    if value is not None:
                        number(value, positive=key != 'quantity')
                    item.setdefault('position' if key in ('quantity', 'average_price') else 'levels', {})[key] = value
            currency = args.get('currency', item.get('currency'))
            if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
                raise ValueError('Invalid currency')
            item.setdefault('position', {})['currency'] = currency
            item['levels_currency'] = currency
            item['levels_source'] = item.get('source_mode', 'gettex')
            db.execute('UPDATE instruments SET payload=? WHERE isin=?', (encoded(item), isin))
            result = {'saved': True, 'instrument': item}
        elif action == 'record_decision':
            if args.get('signal') not in SIGNALS:
                raise ValueError('Invalid signal')
            reason = text(args.get('reason'))
            comment = args.get('comment', '')
            if not isinstance(comment, str) or len(comment) > 4000:
                raise ValueError('Invalid comment')
            stock = next((s for s in (snapshot or {}).get('stocks', []) if s['isin'] == isin), {})
            candle = (stock.get('candles') or [{}])[-1]
            price = candle.get('close')
            if price is not None:
                number(price, positive=True)
            payload = {'signal': args['signal'], 'reason': reason, 'comment': comment, 'author': 'MCP', 'price': price, 'price_at': candle.get('at'), 'source': stock.get('source'), 'currency': stock.get('currency'), 'timeframe': stock.get('timeframe'), 'sufficient_history': stock.get('sufficient_history', False), 'execution': 'recommendation; not a trade', 'classification': 'unassessed'}
            payload['price_identity']={k:stock.get(k) for k in ('source','venue','currency','symbol','timeframe','timezone')}
            payload['history_session']={k:stock[k] for k in ('session_open','session_close','session_policy','alignment') if stock.get(k)}
            if 'model_quantity' in args:
                quantity=number(args['model_quantity'],positive=True)
                if args['signal']=='HOLD':raise ValueError('HOLD cannot change model quantity')
                if any(not v for v in payload['price_identity'].values()):
                    raise ValueError('Model accounting requires complete source identity')
                payload['model_quantity']=quantity
                payload['model_policy']='long only; next observed complete bar open; no fees; no broker execution'
            if 'scenario' in args:
                scenario=args['scenario']
                if not isinstance(scenario,dict) or set(scenario)!={'horizon_hours','target','stop'}:
                    raise ValueError('Scenario requires horizon_hours, target and stop')
                hours=scenario['horizon_hours']
                if type(hours) is not int or not 1<=hours<=8760:
                    raise ValueError('Scenario horizon must be 1..8760 hours')
                target=number(scenario['target'],positive=True)
                stop=number(scenario['stop'],positive=True)
                if price is None or any(not v for v in payload['price_identity'].values()):
                    raise ValueError('A scenario requires an observed price and complete source identity')
                upward=args['signal'] in ('BUY','ADD','HOLD')
                if not (stop<price<target if upward else target<price<stop):
                    raise ValueError('Scenario target and invalidation must bracket the observed price in the signal direction')
                payload['scenario']={**scenario,'direction':'up' if upward else 'down',
                                     'currency':stock['currency'],'policy':'closed OHLC first touch; ambiguous intrabar order is not inferred'}
            cursor = db.execute('INSERT INTO decisions(at,isin,payload) VALUES (?,?,?)', (at, isin, encoded(payload)))
            result = {'saved': True, 'decision_id': cursor.lastrowid, **payload}
        else:
            decision_id = args.get('decision_id')
            if type(decision_id) is not int or not db.execute('SELECT 1 FROM decisions WHERE id=?', (decision_id,)).fetchone():
                raise ValueError('Unknown decision')
            if args.get('classification') not in REVIEWS:
                raise ValueError('Invalid classification')
            payload = {'at': at, 'classification': args['classification'], 'reason': text(args.get('reason')), 'author': 'MCP', 'assessment': 'explicit review; not automatically verified'}
            db.execute('INSERT INTO decision_reviews(decision_id,at,payload) VALUES (?,?,?)', (decision_id, at, encoded(payload)))
            result = {'saved': True, 'decision_id': decision_id, 'review': payload}
        db.execute('INSERT INTO journal(kind,payload) VALUES (?,?)', ('mcp_command', encoded({'at': at, 'action': action, 'request_id': request_id, 'result': result})))
        db.execute('INSERT INTO command_receipts VALUES (?,?,?)', (request_id, digest, encoded(result)))
        return result
