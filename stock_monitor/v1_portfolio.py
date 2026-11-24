"""Read-only Scalable Capital data and auditable V1 portfolio calculations."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math
import re
from urllib.parse import urlsplit

MAX_QUOTE_AGE = timedelta(days=4)
MAX_REPORT_AGE = timedelta(days=190)
ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
SEMI = ("semiconductor", "halbleiter")
SCENARIOS = (
    ("Рынок −20%", "market", -.20),
    ("Полупроводники −30%", "industry", -.30),
    ("USD к EUR −10%", "reporting_currency", -.10),
)


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def fresh(value, now, age):
    date = timestamp(value)
    return date is not None and timedelta() <= now - date <= age


def require_portfolio(holdings, cash, overview, expected=None):
    """Prevent a partial fetch or a different account from becoming current."""
    packets = (holdings, cash, overview)
    if any(not isinstance(p, dict) or p.get("error") for p in packets):
        raise ValueError("SC_PORTFOLIO_READ_FAILED")
    portfolio = holdings.get("portfolioId")
    if not portfolio or any(p.get("portfolioId") != portfolio for p in packets):
        raise ValueError("SC_PORTFOLIO_MISMATCH")
    if expected and portfolio != expected:
        raise ValueError("SC_PORTFOLIO_MISMATCH")
    if not isinstance(holdings.get("holdings"), list):
        raise ValueError("SC_HOLDINGS_MISSING")
    ids = set()
    for row in holdings["holdings"]:
        if not isinstance(row, dict):
            raise ValueError("SC_HOLDING_IDENTITY")
        isin = row.get("isin")
        if not isinstance(isin, str) or not ISIN.fullmatch(isin) or isin in ids:
            raise ValueError("SC_HOLDING_IDENTITY")
        ids.add(isin)
    return portfolio


def news_items(packet, isin, now):
    items = []
    seen = set()
    for row in packet.get("articles", []):
        title, url = row.get("title"), row.get("url")
        try:
            link = urlsplit(url)
        except (TypeError, ValueError):
            continue
        if not title or link.scheme not in ("https", "http") or not link.hostname or row.get("isin") != isin:
            continue
        key = " ".join(title.casefold().split())
        date = row.get("published_at") or row.get("published")
        if key in seen or not fresh(date, now, timedelta(days=7)):
            continue
        seen.add(key)
        items.append(dict(title=title, url=url, date=date, summary=row.get("summary"),
                          source=row.get("source") or row.get("provider"),
                          relevance=row.get("relevance"), impact=news_impact(title, row.get("summary"))))
    return sorted(items, key=lambda item: timestamp(item["date"]), reverse=True)[:8]


def news_impact(title, summary):
    text = (title + " " + (summary or "")).casefold()
    checks = (
        (("guidance", "outlook", "prognose"), "Проверить прогноз компании и допущения по выручке и марже."),
        (("earnings", "results", "quarter", "quartal"), "Сопоставить результаты с прежним прогнозом и денежным потоком."),
        (("debt", "refinanc", "liquidity", "schuld"), "Проверить ликвидность и стоимость финансирования."),
        (("regulat", "export control", "antitrust", "kartell"), "Проверить влияние на доступ к рынкам, затраты и сроки."),
    )
    return next((message for words, message in checks if any(word in text for word in words)),
                "Требуется проверка материала, чтобы оценить влияние на тезис.")


def valuation(facts, currency_rates):
    """Transparent FCF yield sensitivity, using verified same-currency source rows."""
    fcf = facts["series"].get("FreeCashFlow", [])[:3]
    shares = facts["series"].get("OrdinarySharesNumber", [])
    currency = facts["reporting_currency"]
    fx = 1 if currency == "EUR" else numeric(currency_rates.get(currency))
    if len(fcf) != 3 or not shares or not fx or fx <= 0 or not facts["fresh"]:
        return None
    cash_per_share = sorted(value for _, value in fcf)[1] / shares[0][1] / fx if shares[0][1] > 0 else None
    if cash_per_share is None or cash_per_share <= 0:
        return None
    return dict(low=cash_per_share / .10, base=cash_per_share / .075, high=cash_per_share / .05,
                buy_below=cash_per_share / .10 * .85, currency=currency)


def evidence(fundamentals, now):
    raw = fundamentals.get("overview") or {}
    rows = fundamentals.get("metrics") or []
    currency = raw.get("financialCurrency") or fundamentals.get("financial_currency")
    annual = defaultdict(dict)
    for row in rows:
        if (row.get("frequency") == "yearly" and isinstance(row.get("period_end"), str)
                and (row.get("currency") == currency or row.get("unit") in ("shares", "ratio"))
                and numeric(row.get("value")) is not None):
            annual[row.get("metric")][row["period_end"]] = row["value"]
    series = {metric: sorted(values.items(), reverse=True) for metric, values in annual.items()}
    return dict(sector=raw.get("sector"), industry=raw.get("industry"),
                reporting_currency=raw.get("financialCurrency") or fundamentals.get("financial_currency"),
                series=series, pe=numeric(raw.get("trailingPE")),
                source_url=fundamentals.get("source_url"), updated=fundamentals.get("updated_at"),
                fresh=fresh(fundamentals.get("updated_at"), now, MAX_REPORT_AGE))


def decision(holding, price, weight, facts, zone):
    """Do not make a recommendation from a quote or news headline alone."""
    cashflows = facts["series"].get("FreeCashFlow", [])
    revenue = facts["series"].get("TotalRevenue", [])
    if not facts["fresh"] or len(cashflows) < 3 or len(revenue) < 3 or price is None:
        return "Данных недостаточно", "Нужны актуальная цена и 3 годовых периода выручки и свободного денежного потока."
    if holding and cashflows[0][1] < 0 and cashflows[1][1] < 0 and revenue[0][1] < revenue[1][1] < revenue[2][1]:
        return "Пересмотреть позицию", "Два года отрицательного денежного потока и последовательное снижение выручки. Проверить причины по свежей отчётности."
    if holding and weight is not None and weight > .15:
        return "Рассмотреть сокращение", "Вес выше исследовательского порога 15%. Сравнить риск до и после сокращения."
    if zone and price > zone["high"] and holding:
        return "Рассмотреть сокращение", "Цена выше верхней границы оценки по денежному потоку."
    if zone and price <= zone["buy_below"] and all(value > 0 for _, value in cashflows[:3]) and revenue[0][1] >= revenue[2][1]:
        return ("Рассмотреть докупку" if holding else "Рассмотреть покупку"), "Цена в зоне входа, а денежный поток положителен три года. Проверить отчётность и вес позиции."
    if holding and zone:
        return "Держать", "Цена вне зоны докупки. Пересмотреть после отчёта или изменения цены."
    return "Ждать", "Проверить оценку и условия входа после обновления данных."


def stresses(positions, total):
    result = []
    for title, factor, shock in SCENARIOS:
        pnl = known = 0.0
        impacts = []
        for row in positions:
            value = row["value"]
            if value is None:
                continue
            if factor == "market":
                affected = row["kind"] == "stock"
                classified = row["kind"] is not None
            elif factor == "industry":
                industry = row["facts"]["industry"]
                classified = bool(industry)
                affected = classified and any(key in industry.casefold() for key in SEMI)
            else:
                currency = row["facts"]["reporting_currency"]
                classified = bool(currency) and row["kind"] == "stock"
                affected = classified and currency == "USD"
            if not classified:
                continue
            known += value
            if affected:
                loss = value * shock
                pnl += loss
                impacts.append((row["name"], loss))
        result.append(dict(name=title, loss=pnl if known else None, coverage=known / total if total else None,
                           pct=pnl / total if known and total else None, contributors=sorted(impacts, key=lambda pair: pair[1]),
                           basis="Валюта отчётности как приближение" if factor == "reporting_currency" else "Прямой сценарный шок без оценки корреляций"))
    return result


def analyze(snapshot, instruments, load_financials, load_news, now=None, currency_rates=None, load_quote=None):
    now = now or datetime.now(timezone.utc)
    currency_rates = currency_rates or {}
    holdings, cash, overview = (snapshot[key] for key in ("holdings", "cash", "overview"))
    require_portfolio(holdings, cash, overview)
    current = fresh(snapshot.get("synced_at"), now, timedelta(minutes=30))
    total = numeric((overview.get("valuation") or {}).get("total"))
    cash_available = numeric((cash.get("cash") or {}).get("buyingPowerWithoutCredit"))
    catalog = {item["isin"]: item for item in instruments if item.get("isin")}
    positions, candidates = [], []
    present = set()
    for row in holdings["holdings"]:
        isin = row["isin"]
        quantity = numeric((row.get("position") or {}).get("filled"))
        if quantity == 0:
            continue
        present.add(isin)
        info = catalog.get(isin, {})
        quote = row.get("currentQuote") or {}
        price = numeric(quote.get("midPrice")) if quote.get("currency") == "EUR" else None
        price = price if price and price > 0 and fresh(quote.get("timestamp"), now, MAX_QUOTE_AGE) else None
        value = quantity * price if quantity is not None and price is not None else None
        weight = value / total if value is not None and total and total > 0 else None
        facts = evidence(load_financials(info), now)
        zone = valuation(facts, currency_rates)
        label, reason = decision(True, price, weight, facts, zone)
        if not current:
            label, reason = "Обновить данные", "Снимок брокера устарел."
        positions.append(dict(isin=isin, name=row.get("name") or isin, quantity=quantity, price=price, value=value,
                              weight=weight, kind=info.get("kind", "stock") if info else None, facts=facts, zone=zone,
                              decision=label, reason=reason, articles=news_items(load_news(isin), isin, now)))
    for info in catalog.values():
        if info["isin"] in present or info.get("kind", "stock") != "stock":
            continue
        facts = evidence(load_financials(info), now)
        zone = valuation(facts, currency_rates)
        price = load_quote(info, now) if zone and load_quote else None
        label, reason = decision(False, price, None, facts, zone)
        if not current:
            label, reason = "Обновить данные", "Снимок брокера устарел."
        candidates.append(dict(isin=info["isin"], name=info.get("name") or info["isin"], facts=facts,
                               decision=label, reason=reason, price=price, zone=zone,
                               articles=news_items(load_news(info["isin"]), info["isin"], now)))
    crypto = numeric((overview.get("valuation") or {}).get("crypto"))
    return dict(positions=sorted(positions, key=lambda row: (row["value"] is None, -(row["value"] or 0))),
                candidates=sorted(candidates, key=lambda row: (row["decision"] != "Рассмотреть покупку",
                    row["price"] / row["zone"]["buy_below"] if row["price"] and row["zone"] else float("inf"), row["name"])),
                scenarios=stresses(positions, total), cash=cash_available, total=total, current=current,
                synced_at=snapshot.get("synced_at"), crypto=crypto,
                covered=sum(row["value"] is not None for row in positions),
                valued=sum(row["value"] or 0 for row in positions), performance=overview.get("performance") or [])


def simulate(report, isin, amount):
    """A EUR what-if, no broker order or assumption of an executable quote."""
    if not report["current"]:
        raise ValueError("V1_REFRESH_REQUIRED")
    if numeric(amount) is None or amount == 0:
        raise ValueError("V1_INVALID_AMOUNT")
    positions = [dict(row) for row in report["positions"]]
    row = next((item for item in positions if item["isin"] == isin), None)
    if row is None:
        candidate = next((item for item in report["candidates"] if item["isin"] == isin), None)
        if not candidate or amount < 0:
            raise ValueError("V1_UNKNOWN_POSITION")
        row = {**candidate, "value": 0, "kind": "stock"}
        positions.append(row)
    value = row["value"]
    cash = report["cash"]
    if value is None or value + amount < 0 or (amount > 0 and (cash is None or amount > cash)):
        raise ValueError("V1_AMOUNT_EXCEEDS_DATA_OR_FUNDS")
    row["value"] += amount
    total = report["total"]
    return dict(before=value / total if total else None, after=row["value"] / total if total else None,
                scenarios=stresses(positions, total))
