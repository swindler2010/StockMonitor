"""Read-only presentation of existing Yahoo card fields, separate from GETTEX."""
import math
from datetime import datetime, timezone


def project(info, card, now=None):
    now = now or datetime.now(timezone.utc)
    expected = info.get('profile_symbol') or info.get('history_symbol')
    if not isinstance(card, dict) or not expected or card.get('symbol') != expected:
        return None
    overview = card.get('overview') or {}
    if not isinstance(overview, dict) or overview.get('symbol') != expected:
        return None
    source = card.get('source') or ''
    if not source.startswith('Yahoo Finance'):
        return None
    price, at = overview.get('preMarketPrice'), overview.get('preMarketTime')
    currency, venue = overview.get('currency'), overview.get('exchange')
    if (type(price) not in (int, float) or not math.isfinite(price) or price <= 0 or
            type(at) not in (int, float) or not math.isfinite(at) or not currency or not venue):
        return None
    try:
        stamp = datetime.fromtimestamp(at, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    if stamp > now:
        return None
    change = overview.get('preMarketChangePercent')
    if type(change) not in (int, float) or not math.isfinite(change):
        change = None
    return dict(price=price, at=stamp.isoformat(), currency=currency, venue=venue,
                symbol=expected, change=change, source='Yahoo Finance', archived=(now-stamp).total_seconds() > 900)


def present(info, card, now=None):
    from i18n import tr, language
    from financial_format import format_date
    value = project(info, card, now)
    if not value:
        return tr('Премаркет: нет данных'), tr('Источник карточки не предоставил подтверждённую цену премаркета. Это отдельная площадка, не котировка GETTEX.'), None
    title = tr('Премаркет') + f": {value['price']:.2f} {value['currency']}"
    if value['change'] is not None:
        title += f" {value['change']:+.2f}%"
    if value['archived']:
        title += ' · ' + tr('Архив')
    detail = f"{value['source']} · {value['symbol']} · {value['venue']} · {value['currency']}\n" + format_date(value['at'], language(), clock=True)
    detail += '\n' + tr('Справочно. Не используется для исполнения GETTEX.')
    return title, detail, value['change']
