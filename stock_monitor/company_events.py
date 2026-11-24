"""Read-only company calendar and quarterly reports on the existing news worker."""
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from fundamentals_view import METRICS, cache_matches, legacy_card, number, statement_rows
from news_engine import moment, plain, safe_url


PREFIX = 'company_events:'
CALENDAR_TTL = 86400
REPORT_TTL = 7*86400
REPORT_METRICS = ('TotalRevenue', 'NetIncome', 'DilutedEPS', 'EBITDA')
DATE_FIELDS = {'Earnings Date':'EARNINGS_DATE', 'Ex-Dividend Date':'EX_DIVIDEND', 'Dividend Date':'DIVIDEND_PAYMENT'}
SOURCE = 'Yahoo Finance'
FORMS = ('10-Q','10-K','20-F','40-F','6-K','8-K')


def fetch(info, previous, cache_dir, ticker=None):
    """One calendar lookup per turn, and a quarterly income snapshot when due."""
    from market import clean

    symbol = info.get('profile_symbol') or info.get('history_symbol')
    if not cache_matches(previous, info):
        previous = {}
    if ticker is None:
        import yfinance as yf
        yf.set_tz_cache_location(str(cache_dir))
        ticker = yf.Ticker(symbol)
    packet = dict(previous, isin=info['isin'], symbol=symbol, source=SOURCE, rate_limited=False)

    def update(key, load, previous_values):
        try:
            records = load()
            if not records:
                raise ValueError('SOURCE_EMPTY')
        except Exception as error:
            packet[key+'_status'] = 'STALE_CACHE' if previous_values else 'UNAVAILABLE'
            packet['rate_limited'] = type(error).__name__=='YFRateLimitError'
        else:
            packet[key] = records
            packet[key+'_fetched_at'] = datetime.now(timezone.utc).isoformat()
            packet[key+'_status'] = 'AVAILABLE'

    def load_calendar():
        raw = ticker.get_calendar()
        return {key:clean(raw[key]) for key in DATE_FIELDS if raw.get(key)}

    def load_reports():
        frame = ticker.get_income_stmt(freq='quarterly')
        if frame.empty:
            return []
        try:
            details=ticker.get_info() or {}
            # Only the reporting currency supplied for this provider symbol is relevant.
            if details.get('symbol') not in (None,symbol):
                return []
            currency=details.get('financialCurrency')
        except Exception as error:
            if type(error).__name__=='YFRateLimitError':
                raise
            currency=None
        stamp = datetime.now(timezone.utc).isoformat()
        raw = clean(frame.iloc[:,:5].to_dict())
        return [r for r in statement_rows(raw,'income','quarterly',currency,stamp,SOURCE)
                if r['metric'] in REPORT_METRICS]

    update('calendar',load_calendar,previous.get('calendar'))
    attempted = moment(previous.get('reports_attempted_at'))
    now = datetime.now(timezone.utc)
    if not packet['rate_limited'] and (attempted is None or (now-attempted).total_seconds()>=REPORT_TTL):
        update('reports',load_reports,previous.get('reports'))
        packet['reports_attempted_at'] = datetime.now(timezone.utc).isoformat()
    packet['attempted_at'] = datetime.now(timezone.utc).isoformat()
    return packet


def day(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def dates_from(raw, today, url, stamp, state):
    if not isinstance(raw, dict):
        return []
    out = []
    for key, kind in DATE_FIELDS.items():
        values = raw.get(key)
        values = values[:4] if isinstance(values, list) else [values]
        parsed = sorted(d for v in values if (d:=day(v)) is not None)
        if (parsed and (parsed[-1]-parsed[0]).days<=7 and parsed[-1]>=today
                and parsed[0]<=today+timedelta(days=30)):
            out.append(dict(kind=kind,date=parsed[0].isoformat(),end_date=parsed[-1].isoformat(),
                            url=url,source=SOURCE,fetched_at=stamp.isoformat(),stale=state=='STALE_CACHE'))
    return out


def quarterly(rows, now, today):
    result = {}
    for row in rows:
        if not isinstance(row,dict) or row.get('frequency')!='quarterly':
            continue
        metric = row.get('metric')
        stamp = moment(row.get('updated_at'))
        period = day(row.get('period_end'))
        value = number(row.get('value'))
        if metric not in REPORT_METRICS or stamp is None or stamp>now or period is None or period>today or value is None:
            continue
        currency = row.get('currency')
        currency = currency if isinstance(currency,str) and len(currency)==3 else None
        stale=bool(row.get('cache_status') or now-stamp>timedelta(days=8))
        rank=(period,not stale,currency is not None,stamp)
        if metric not in result or rank>result[metric][0]:
            result[metric]=(rank,dict(metric=metric,value=value,unit=METRICS[metric][1],
                period_end=period.isoformat(),currency=currency,source=SOURCE,observed_at=stamp.isoformat(),stale=stale))
    return [result[metric][1] for metric in REPORT_METRICS if metric in result]


def projection(info, now, today, *, auto, data, card):
    symbol = info.get('profile_symbol') or info.get('history_symbol')
    base = dict(calendar_status='NOT_CHECKED', report_status='NOT_CHECKED', dates=[], reports=[], filings=[])
    if not symbol:
        return base
    url='https://finance.yahoo.com/quote/'+quote(symbol,safe='')+'/'
    base['report_url']=url+'financials/'
    auto=auto if cache_matches(auto,info) else {}
    data=data if cache_matches(data,info) and data.get('status')!='IDENTITY_MISMATCH' else {}
    overview=card.get('overview')
    if card.get('symbol')!=symbol or isinstance(overview,dict) and overview.get('symbol') not in (None,symbol):
        card={}
    card_at=moment(card.get('fetched_at'))
    if card_at is None or card_at>now:
        card={}
    options=[]
    for raw, at, state in ((auto.get('calendar'),auto.get('calendar_fetched_at'),auto.get('calendar_status')),
                           (data.get('calendar'),data.get('calendar_updated_at') or data.get('updated_at'),data.get('calendar_status')),
                           (card.get('events'),card.get('fetched_at'),None)):
        stamp=moment(at)
        if isinstance(raw,dict) and raw and stamp is not None and stamp<=now:
            options.append((stamp,raw,state))
    if options:
        stamp,raw,state=max(options,key=lambda item:item[0])
        base['calendar_status']='STALE_CACHE' if state in ('STALE_CACHE','RETAINED_AFTER_REFRESH_FAILURE') or now-stamp>timedelta(days=2) else 'AVAILABLE'
        base['calendar_fetched_at']=stamp.isoformat()
        base['dates']=dates_from(raw,today,url,stamp,base['calendar_status'])
    elif auto.get('calendar_status')=='UNAVAILABLE':
        base['calendar_status']='UNAVAILABLE'
    report_rows=[]
    # Prefer reports with a known reporting currency when same-quarter data exists.
    if card and isinstance(card.get('reports'),dict) and isinstance(card.get('overview'),dict):
        report_rows.extend(legacy_card(card))
    for source,key in ((data,'metrics'),(auto,'reports')):
        if isinstance(source.get(key),list):
            report_rows.extend(source[key][:600])
    reports=quarterly(report_rows,now,today)
    base['reports']=reports
    if reports:
        base['report_status']='STALE_CACHE' if all(row['stale'] for row in reports) else 'AVAILABLE'
    elif auto.get('reports_status')=='UNAVAILABLE':
        base['report_status']='UNAVAILABLE'
    filings=card.get('filings')
    if isinstance(filings,list):
        for row in filings[:60]:
            if not isinstance(row,dict) or row.get('type') not in FORMS:
                continue
            filed=day(row.get('date'))
            link=row.get('edgarUrl') or row.get('url')
            if filed and today-timedelta(days=30)<=filed<today and isinstance(link,str) and safe_url(link):
                base['filings'].append(dict(kind='FILING',date=filed.isoformat(),end_date=filed.isoformat(),url=safe_url(link),
                    source=SOURCE,form=row['type'],title=plain(row.get('title'),240),fetched_at=card_at.isoformat(),stale=False))
        base['filings']=sorted(base['filings'],key=lambda row:row['date'],reverse=True)[:3]
    return base
