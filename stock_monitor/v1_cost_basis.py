"""Reconstruct the open EUR lots from settled Scalable Capital transactions."""
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from itertools import groupby

from v1_portfolio import timestamp

ZERO = Decimal(0)
QUANTITY_TOLERANCE = Decimal("0.00000001")
INWARD = {"BUY", "TRANSFER_IN", "SWAP_IN"}
OUTWARD = {"SELL", "TRANSFER_OUT", "SWAP_OUT"}


def decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def movements(rows, isins):
    by_isin = defaultdict(list)
    for row in rows:
        kind = row.get("kind")
        if kind == "cash":
            continue
        source = row.get({"security": "security", "non_trade_security": "nonTradeSecurity",
                          "eltif": "eltif"}.get(kind, ""), {}) or {}
        isin = source.get("isin")
        if isin in isins and row.get("status") == "SETTLED":
            by_isin[isin].append((row, source))
    return by_isin


def lot_history(entries):
    """Resolve exact reversals before allocating sales. Flag ambiguous reversals."""
    events = []
    for row, source in entries:
        action = source.get("side") or source.get("transactionType")
        quantity = decimal(source.get("quantity"))
        amount = decimal(source.get("amount"))
        date = timestamp(row.get("lastEventAt"))
        if action not in INWARD | OUTWARD or not quantity or not date:
            return None
        if row.get("currency") not in ("EUR", None):
            return None
        events.append((date, bool(row.get("isCancellation")), action, abs(quantity),
                       abs(amount) if amount is not None and row.get("currency") == "EUR" else None,
                       row.get("custodian") or ""))
    events.sort(key=lambda event: (event[0], event[1]))
    settled = []
    for date, reversal, action, quantity, amount, custodian in events:
        event = (date, action, quantity, amount, custodian)
        if not reversal:
            settled.append(event)
            continue
        matches = [index for index, candidate in enumerate(settled)
                   if candidate[1:] == event[1:]]
        if len(matches) != 1:
            return None
        settled.pop(matches[0])
    ordered = []
    balance = ZERO
    for _, same_time in groupby(settled, key=lambda event: event[0]):
        group = list(same_time)
        incoming = [event for event in group if event[1] in INWARD]
        outgoing = [event for event in group if event[1] in OUTWARD]
        out_quantity = sum((event[2] for event in outgoing), ZERO)
        prices = {event[3] / event[2] if event[3] else None for event in incoming}
        if len(prices) > 1 or (incoming and outgoing and out_quantity > balance + QUANTITY_TOLERANCE):
            return None
        # If existing lots cover all outgoing shares, they are consumed first
        # regardless of which same-time event the broker sent first.
        ordered.extend(outgoing)
        ordered.extend(incoming)
        balance += sum((event[2] for event in incoming), ZERO) - out_quantity
    return ordered


def open_lots(events):
    lots = deque()
    for _, action, quantity, amount, _ in events:
        if action in INWARD:
            price = amount / quantity if amount else None
            lots.append([quantity, price])
            continue
        while quantity and lots:
            lot = lots[0]
            taken = min(quantity, lot[0])
            lot[0] -= taken
            quantity -= taken
            if not lot[0]:
                lots.popleft()
        if quantity > QUANTITY_TOLERANCE:
            return None
    return lots


def cost_basis(rows, holdings):
    available = {row["isin"]: decimal((row.get("position") or {}).get("filled"))
                 for row in holdings}
    transactions = movements(rows, available)
    result = {}
    for isin, quantity in available.items():
        entries = transactions.get(isin, ())
        if quantity is None or quantity <= 0 or not entries:
            continue
        events = lot_history(entries)
        if events is None:
            result[isin] = dict(status="INCOMPLETE_OR_AMBIGUOUS")
            continue
        lots = open_lots(events)
        if lots is None or abs(sum((lot[0] for lot in lots), ZERO) - quantity) > QUANTITY_TOLERANCE:
            result[isin] = dict(status="QUANTITY_MISMATCH")
            continue
        if not lots or any(price is None for _, price in lots):
            result[isin] = dict(status="MISSING_LOT_COST")
            continue
        remaining = sum((units * price for units, price in lots), ZERO)
        transferred = any(action.startswith(("TRANSFER_", "SWAP_")) for _, action, *_ in events)
        result[isin] = dict(status="TRANSFER_ESTIMATE" if transferred else "FIFO_ESTIMATE",
                            average=float(remaining / quantity), cost=float(remaining))
    return result
