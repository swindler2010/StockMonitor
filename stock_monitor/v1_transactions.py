"""Display broker transaction facts without inferring fills from order limits."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

STATUS = {
    "SETTLED": "Рассчитано", "FILLED": "Исполнено", "PARTIALLY_FILLED": "Частично исполнено",
    "PARTIAL_FILLED": "Частично исполнено", "PENDING": "Ожидает исполнения", "OPEN": "Открыта",
    "CREATED": "Создана", "PLACED": "Размещена", "CANCELLED": "Отменена",
    "REJECTED": "Отклонена", "EXPIRED": "Срок истёк", "EXECUTED": "Исполнено",
}
SIDE = {"BUY": "Покупка", "SELL": "Продажа"}
ORDER = {"MARKET": "Рыночная", "LIMIT": "Лимитная", "STOP": "Стоп", "STOP_LIMIT": "Стоп-лимит"}


def value(raw, currency=None):
    if raw is None or raw == "":
        return "—"
    try:
        number = Decimal(str(raw))
        if not number.is_finite():
            return "—"
        result = format(number.normalize(), "f")
    except (InvalidOperation, ValueError):
        return "—"
    return result + (" " + currency if currency else "")


def date_label(raw):
    try:
        date = datetime.fromisoformat(raw)
        return date.astimezone().strftime("%d.%m.%Y %H:%M:%S") if date.tzinfo else raw
    except (ValueError, TypeError):
        return "—"


def transaction_rows(rows, names):
    result = []
    for row in rows:
        security = row.get("security") or row.get("eltif") or {}
        if row.get("kind") not in ("security", "eltif") or security.get("side") not in SIDE:
            continue
        isin = security.get("isin")
        result.append(dict(id=row["id"], isin=isin, name=names.get(isin) or isin or "—",
                           side=SIDE[security["side"]], status=STATUS.get(row.get("status"), row.get("status") or "—"),
                           at=row.get("lastEventAt"), quantity=security.get("quantity"),
                           amount=security.get("amount"), currency=row.get("currency"),
                           reversal=row.get("isCancellation")))
    return sorted(result, key=lambda row: row["at"] or "", reverse=True)


def detail_lines(transaction):
    trade = transaction.get("securityTrade") or transaction.get("eltif") or {}
    security = transaction.get("security") or {}
    currency = transaction.get("currency")
    shares = trade.get("numberOfShares") or {}
    amounts = trade.get("tradeTransactionAmounts") or {}
    tax = trade.get("aggregatedTransactionTaxes") or {}
    lines = [
        (security.get("name") or security.get("isin") or "Операция SC", ""),
        ("ISIN", security.get("isin")),
        ("Операция", SIDE.get(trade.get("side"), trade.get("side"))),
        ("Статус", STATUS.get(trade.get("status"), trade.get("status"))),
        ("Последнее изменение", date_label(transaction.get("lastEventAt"))),
        ("Тип заявки", ORDER.get(trade.get("orderKind"), trade.get("orderKind"))),
        ("Заказано", value(shares.get("total") or trade.get("quantity"))),
        ("Исполнено", value(shares.get("filled"))),
        ("Средняя цена исполнения", value(trade.get("averagePrice") or trade.get("executionPrice"), currency)),
        ("Сумма по данным SC", value(trade.get("totalAmount") or trade.get("amount"), currency)),
        ("Комиссия", value(trade.get("fee"), currency)),
        ("Транзакционная комиссия", value(trade.get("transactionalFee"), currency)),
        ("Комиссия операции", value(amounts.get("transactionFee"), currency)),
        ("Комиссия площадки", value(amounts.get("venueFee"), currency)),
        ("Налоги", value(tax.get("totalTax") or trade.get("taxes"), currency)),
        ("Лимитная цена", value(trade.get("limitPrice"), currency)),
        ("Стоп-цена", value(trade.get("stopPrice"), currency)),
        ("Площадка", trade.get("tradingVenue")),
        ("Действует до", date_label(trade.get("validUntil"))),
        ("Причина завершения", trade.get("finalisationReason")),
        ("Номер операции", transaction.get("transactionReference") or transaction.get("id")),
    ]
    if transaction.get("isCancellation"):
        lines.append(("Корректировка", "Сторнирование операции"))
    lines.append(("Ход исполнения", ""))
    for event in transaction.get("history") or []:
        amount = event.get("numberOfShares") or {}
        state = STATUS.get(event.get("state"), event.get("state") or "—")
        event_text = state + " · исполнено " + value(amount.get("filled"))
        event_text += " · цена " + value(event.get("executionPrice"), currency)
        lines.append((date_label(event.get("timestamp")), event_text))
    return [(label, "—" if item is None else str(item)) for label, item in lines]
