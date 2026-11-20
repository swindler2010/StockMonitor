"""Per-entry-strategy reporting from the existing PAPER receipts."""
import json
from decimal import Decimal

from paper_account import money
from paper_entry_modes import STRATEGY_CODES

OTHER = '—'
CODES = (*STRATEGY_CODES.values(), OTHER)


def _has_table(db, name):
    return db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                      ('table', name)).fetchone() is not None


def save_note(db, fill_id, note):
    """The caller owns the write transaction. A note survives an account reset."""
    note = note.strip()
    if len(note) > 1000:
        raise ValueError('До 1000 символов')
    for table in ('paper_fills', 'paper_fill_archive'):
        if _has_table(db, table) and db.execute(
            f'SELECT 1 FROM {table} WHERE id=?', (fill_id,)
        ).fetchone():
            break
    else:
        raise ValueError('Операция не найдена')
    db.execute('CREATE TABLE IF NOT EXISTS paper_fill_notes(fill_id TEXT PRIMARY KEY, note TEXT NOT NULL)')
    if note:
        db.execute('INSERT OR REPLACE INTO paper_fill_notes VALUES (?,?)', (fill_id, note))
    else:
        db.execute('DELETE FROM paper_fill_notes WHERE fill_id=?', (fill_id,))


def _entry_code(fill):
    initiator = fill.get('initiator')
    if initiator in ('MANUAL', 'CHATGPT'):
        return OTHER
    mode = fill.get('smart_eligibility', {}).get('strategy_mode')
    return STRATEGY_CODES.get(mode) or (initiator if initiator in CODES else OTHER)


def read(db, current):
    notes = dict(db.execute('SELECT fill_id,note FROM paper_fill_notes')) if _has_table(db, 'paper_fill_notes') else {}
    rows = [(None, fill) for fill in current]
    if _has_table(db, 'paper_fill_archive'):
        archived = db.execute('SELECT reset_epoch,payload FROM paper_fill_archive ORDER BY rowid')
        rows.extend((epoch, json.loads(payload)) for epoch, payload in archived)
    totals = {scope: {code: dict(buys=0, sells=0, winners=0, losers=0, flat=0,
                                  realized=Decimal(0), fees=Decimal(0))
                      for code in CODES} for scope in ('current', 'archive')}
    inventory = {}
    journal = []
    for epoch, fill in rows:
        # Same listing key and proportional cost basis as paper_account.ledger.
        key = (epoch, fill['isin'], json.dumps(fill['identity'], sort_keys=True))
        position = inventory.setdefault(key, dict(qty=Decimal(0), groups={}))
        holdings = position['groups']
        stats = totals['current' if epoch is None else 'archive']
        qty = Decimal(fill['quantity'])
        fee = Decimal(fill['fee'])
        gross = Decimal(fill['gross'])
        smart = fill.get('smart_eligibility', {})
        reason = (smart.get('protective_exit') or {}).get('reason') or smart.get('signal_reason', '')
        if smart.get('strategy_model') and smart.get('entry_evidence'):
            evidence = smart['entry_evidence']
            if evidence.get('setup')=='DAILY_REVERSAL':
                reason = f"Разворот после {evidence['daily_down_closes']} снижений из 3"
            elif evidence.get('setup')=='OPEN_RANGE':
                reason = 'Пробой максимума первого часа'
            reason += (f" · растут {evidence['breadth_positive']}/{evidence['breadth_available']}"
                       f" · объём {evidence['relative_volume']:.2f}× медианы")
            if evidence.get('setup')=='DAILY_REVERSAL' and evidence.get('entry_level') is not None:
                reason += f" · уровень {evidence['entry_level']:.2f} €"
        pnl = None
        if fill['side'] == 'BUY':
            code = _entry_code(fill)
            lot = holdings.setdefault(code, dict(qty=Decimal(0), cost=Decimal(0), pnl=Decimal(0)))
            position['qty'] += qty
            lot['qty'] += qty
            lot['cost'] += gross + fee
            stats[code]['buys'] += 1
            stats[code]['fees'] += fee
            codes = [code]
        else:
            sold = {code: lot for code, lot in holdings.items() if lot['qty'] > 0}
            held = position['qty']
            codes = list(sold)
            if held:
                fraction = qty / held
                net = gross - fee
                fee_used = net_used = qty_used = pnl = Decimal(0)
                for code, lot in sold.items():
                    weight = lot['qty'] / held
                    allocated_fee = fee * weight
                    allocated_net = net * weight
                    sold_qty = qty * weight
                    if code == codes[-1]:
                        allocated_fee, allocated_net = fee - fee_used, net - net_used
                        sold_qty = qty - qty_used
                    fee_used += allocated_fee
                    net_used += allocated_net
                    qty_used += sold_qty
                    cost = lot['cost'] * fraction
                    gain = allocated_net - cost
                    lot['qty'] -= sold_qty
                    lot['cost'] -= cost
                    lot['pnl'] += gain
                    pnl += gain
                    stats[code]['sells'] += 1
                    stats[code]['fees'] += allocated_fee
                    stats[code]['realized'] += gain
                    if qty == held:
                        result = money(lot['pnl'])
                        stats[code]['winners' if result > 0 else 'losers' if result < 0 else 'flat'] += 1
                if qty == held:
                    del inventory[key]
                else:
                    position['qty'] -= qty
        journal.append(dict(fill=fill, codes=codes or [OTHER], pnl=None if pnl is None else str(money(pnl)),
                            reason=reason, note=notes.get(fill['request_id'], ''), archived=epoch is not None))
    for groups in totals.values():
        for row in groups.values():
            for name in ('realized', 'fees'):
                row[name] = str(money(row[name]))
    journal.sort(key=lambda entry: entry['fill']['at'], reverse=True)
    indices = {scope: {code: [] for code in ('all', *CODES)} for scope in totals}
    for index, entry in enumerate(journal):
        groups = indices['archive' if entry['archived'] else 'current']
        for code in ('all', *entry['codes']):
            groups[code].append(index)
    return dict(totals=totals, entries=journal, indices=indices)
