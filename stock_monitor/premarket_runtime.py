"""Indicative premarket only. One turn on the existing news Dispatcher lane.

No GETTEX writes, symbol substitution, FX conversion or execution quotes.
TLS uses Python's default verified context including the Windows trust store.
"""
import json
import math
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
from urllib.error import HTTPError

PREFIX = 'premarket:'
COOLDOWN = 'premarket_provider_cooldown'
LIMIT = 2_000_000


def identity(info):
    return info.get('isin'), info.get('profile_symbol') or info.get('history_symbol')


def matches(packet, info):
    return isinstance(packet, dict) and (packet.get('isin'), packet.get('symbol')) == identity(info)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def parse(info, data, now):
    isin, symbol = identity(info)
    chart = data.get('chart') or {}
    results = chart.get('result') or []
    if chart.get('error') or len(results) != 1:
        raise ValueError('SOURCE_EMPTY')
    result = results[0];meta = result.get('meta') or {}
    if meta.get('symbol') != symbol or meta.get('instrumentType') != 'EQUITY':
        raise ValueError('LISTING_MISMATCH')
    currency, venue = meta.get('currency'), meta.get('exchangeName')
    if not isinstance(currency, str) or len(currency) != 3 or not venue:
        raise ValueError('LISTING_METADATA_MISSING')
    packet = dict(isin=isin, symbol=symbol, currency=currency, venue=venue,
                  source='Yahoo Finance chart', attempted_at=now,
                  mapping='CONFIGURED_PROFILE_SYMBOL_NOT_INDEPENDENT_ISIN', quote=None)
    if meta.get('hasPrePostMarketData') is False:
        return dict(packet, status='NOT_PROVIDED', next_retry_at=now+86400)
    pre = (meta.get('currentTradingPeriod') or {}).get('pre') or {}
    start, end = pre.get('start'), pre.get('end')
    if not finite(start) or not finite(end) or start >= end:
        raise ValueError('PREMARKET_SESSION_UNCONFIRMED')
    timestamps = result.get('timestamp') or []
    quotes = (result.get('indicators') or {}).get('quote') or []
    closes = quotes[0].get('close') if len(quotes) == 1 else None
    if not isinstance(closes, list) or len(closes) != len(timestamps):
        raise ValueError('INCOMPLETE_PUBLICATION')
    candidates = [(at, price) for at, price in zip(timestamps, closes)
                  if finite(at) and finite(price) and price > 0 and start <= at < end and at <= now]
    if not candidates:
        return dict(packet, status='WAITING_SESSION' if now < start else 'NO_PREMARKET_BARS',
                    next_retry_at=min(start, now+3600) if now < start else now+600)
    at, price = max(candidates)
    baseline, regular_at = meta.get('regularMarketPrice'), meta.get('regularMarketTime')
    change = (price/baseline-1)*100 if finite(baseline) and baseline > 0 and finite(regular_at) and regular_at < start else None
    packet['quote'] = dict(price=price, at=at, change=change, session_start=start, session_end=end)
    return dict(packet, status='AVAILABLE' if now-at <= 900 else 'ARCHIVED',
                next_retry_at=now+(120 if start <= now < end else 1800))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('SOURCE_REDIRECT')


def download(symbol, *, opener=None, clock=time.monotonic):
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol, safe='')+'?interval=1m&range=1d&includePrePost=true'
    opener = opener or build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    request = Request(url, headers={'User-Agent':'StockMonitor/0.16 personal quote display', 'Accept':'application/json'})
    with opener.open(request, timeout=12) as response:
        if urlsplit(response.geturl()).hostname != 'query1.finance.yahoo.com':
            raise ValueError('SOURCE_REDIRECT')
        if response.status != 200:
            raise ValueError('HTTP_'+str(response.status))
        parts=[];size=0;started=clock()
        while True:
            part=response.read(16384)
            if not part:break
            size+=len(part)
            if size>LIMIT or clock()-started>20:raise ValueError('SOURCE_BUDGET')
            parts.append(part)
    return json.loads(b''.join(parts))


def fetch(info, previous=None, cooldown=None, *, now=None, transport=download):
    now=time.time() if now is None else now
    previous=previous if matches(previous,info) else {}
    if previous.get('next_retry_at',0)>now:return previous, cooldown
    if isinstance(cooldown,dict) and cooldown.get('retry_at',0)>now:
        return dict(previous,isin=identity(info)[0],symbol=identity(info)[1],status='PROVIDER_COOLDOWN',next_retry_at=cooldown['retry_at']),cooldown
    symbol=identity(info)[1]
    if not symbol:return dict(isin=identity(info)[0],symbol=None,status='NO_PROFILE_SYMBOL',next_retry_at=now+86400),cooldown
    try:return parse(info,transport(symbol),now),cooldown
    except Exception as error:
        retry=now+300;code=str(error) if isinstance(error,ValueError) else type(error).__name__
        if isinstance(error,HTTPError):
            code='HTTP_'+str(error.code)
            if error.code in (403,429):
                raw=error.headers.get('Retry-After','3600')
                try:retry=now+max(300,int(raw))
                except (TypeError,ValueError):
                    try:retry=max(now+300,parsedate_to_datetime(raw).timestamp())
                    except (TypeError,ValueError,OverflowError):retry=now+3600
                cooldown=dict(retry_at=retry,status=code)
        # Keep the last good quote with its original timestamp, never renew it.
        return dict(previous,isin=identity(info)[0],symbol=symbol,status='SOURCE_ERROR',error=code,
                    attempted_at=now,next_retry_at=retry),cooldown


def present(info, packet, now=None):
    from i18n import tr,language
    from financial_format import format_date
    now=time.time() if now is None else now
    if not matches(packet,info):packet={}
    quote_value=packet.get('quote') or {}
    at,price=quote_value.get('at'),quote_value.get('price')
    if finite(at) and at<=now and finite(price) and price>0 and packet.get('currency') and packet.get('venue'):
        archived=now-at>900 or packet.get('status')!='AVAILABLE'
        title=tr('Премаркет')+f": {price:.2f} {packet['currency']}"
        change=quote_value.get('change')
        if finite(change):title+=f' {change:+.2f}%'
        if archived:title+=' · '+tr('Архив')
        stamp=datetime.fromtimestamp(at,timezone.utc).isoformat()
        detail=f"Yahoo Finance · {packet['symbol']} · {packet['venue']} · {packet['currency']}\n"+format_date(stamp,language(),clock=True)
        detail+='\n'+tr('Последняя минутная цена до открытия. Привязка по настроенному тикеру; ISIN независимо не проверен.')
        detail+='\n'+tr('Справочно. Не используется для исполнения GETTEX.')
        if packet.get('error'):detail+='\n'+tr('Ошибка источника')+': '+str(packet['error'])
        return title,detail,change if finite(change) and not archived else None
    status=packet.get('status')
    title=tr('Премаркет: нет у источника') if status=='NOT_PROVIDED' else tr('Премаркет: ожидание открытия') if status=='WAITING_SESSION' else tr('Премаркет: нет данных')
    reasons={'NOT_PROVIDED':'Источник не публикует премаркет этого тикера.','WAITING_SESSION':'До начала указанной источником сессии.','NO_PREMARKET_BARS':'Источник пока не прислал минутную цену до открытия.','PROVIDER_COOLDOWN':'Ожидание по требованию источника.','SOURCE_ERROR':'Источник временно недоступен.','NO_PROFILE_SYMBOL':'Не указан тикер карточки.'}
    detail=tr(reasons.get(status,'Премаркет ещё не загружен.'))+'\n'+tr('Справочно. Не используется для исполнения GETTEX.')
    if packet.get('error'):detail+='\n'+tr('Ошибка источника')+': '+str(packet['error'])
    return title,detail,None
