"""V1 news and calendar context. Reads the shared caches without trading authority."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from news_engine import moment, normalize, plain
from official_calendar import ECONOMIC_RELEASES, SETTING as CALENDAR_SETTING, SOURCE_PAGES
from company_events import PREFIX as COMPANY_EVENTS, projection as company_events

ARTICLE_DAYS = 7
CALENDAR_DAYS = 14
FEED_MAX_AGE = timedelta(hours=2)
CALENDAR_MAX_AGE = timedelta(days=2)
BERLIN = ZoneInfo('Europe/Berlin')
MEETING_KINDS = ('FOMC','GENERAL','NON_MONETARY','MONETARY_DAY1','MONETARY_DAY2','OTHER')


def setting(db, key):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row[0])
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def source_feed(db, key, now, *, info=None, language=None, limit=6):
    packet = setting(db, 'news:'+key)
    if not packet:
        return dict(key=key, status='NOT_CHECKED', recent=0, articles=[])
    if info is not None:
        symbol = info.get('profile_symbol') or info.get('history_symbol') or ''
        identity = packet.get('identity')
        if (not isinstance(identity, dict) or identity.get('isin') != key
                or identity.get('symbol') != symbol or identity.get('name') != info.get('name','')):
            return dict(key=key, status='IDENTITY_CHANGED', recent=0, articles=[])

    fetched = moment(packet.get('fetched_at'))
    attempted = moment(packet.get('attempted_at'))
    if (fetched and fetched>now) or (attempted and attempted>now):
        return dict(key=key, status='AFTER_CUTOFF', recent=0, articles=[])
    status = packet.get('status')
    if status not in ('AVAILABLE','PARTIAL','STALE_CACHE','UNAVAILABLE','ERROR','EMPTY'):
        status = 'UNAVAILABLE'
    last_good = fetched if status != 'EMPTY' else attempted
    if status in ('AVAILABLE','PARTIAL','EMPTY') and (last_good is None or now-last_good>FEED_MAX_AGE):
        status = 'STALE_CACHE' if packet.get('articles') else 'UNAVAILABLE'

    rows = packet.get('articles')
    known = []
    oldest = now-timedelta(days=ARTICLE_DAYS)
    if isinstance(rows, list):
        for row in rows[:100]:
            if not isinstance(row, dict) or not isinstance(row.get('url'), str):
                continue
            if info is None:
                if row.get('scope') != 'market':
                    continue
            elif (row.get('scope') != 'company' or row.get('isin') != key or row.get('symbol') != symbol):
                continue
            published = moment(row.get('published'))
            observed = moment(row.get('fetched_at')) if row.get('fetched_at') is not None else fetched
            if (published is None or observed is None or observed>now
                    or not oldest<=published<=now):
                continue
            known.append({**row, 'published':published.isoformat(), 'fetched_at':observed.isoformat(),
                          'provider':plain(row.get('provider'),120)})
    items = normalize(known, now.timestamp())
    visible = items[:limit]
    if language is not None:
        from news_translation import project, setting as translation_key
        visible = project(visible, setting(db,translation_key(key,language)), language)
    return dict(key=key, status=status, fetched_at=fetched.isoformat() if fetched else None,
                attempted_at=attempted.isoformat() if attempted else None,
                recent=len(items), articles=visible)


def official_events(db, now):
    today = now.astimezone(BERLIN).date()
    latest = today+timedelta(days=CALENDAR_DAYS)
    packet = setting(db,CALENDAR_SETTING)
    attempted = moment(packet.get('attempted_at'))
    values = packet.get('sources')
    states, out = {}, []
    for source,url in SOURCE_PAGES.items():
        data = values.get(source) if isinstance(values,dict) else None
        if not isinstance(data,dict) or attempted is not None and attempted>now:
            states[source]='NOT_CHECKED'
            continue
        fetched=moment(data.get('fetched_at'))
        if not fetched or fetched>now:
            states[source]='UNAVAILABLE'
            continue
        current=data.get('status')=='AVAILABLE' and now-fetched<=CALENDAR_MAX_AGE
        states[source]='AVAILABLE' if current else 'STALE_CACHE'
        rows=data.get('rows')
        if not isinstance(rows,list):
            continue
        for row in rows[:500]:
            try:
                begin=date.fromisoformat(row['date'])
                end=date.fromisoformat(row['end_date'])
                kind=row['kind']
            except (ValueError,TypeError,KeyError):
                continue
            kinds=ECONOMIC_RELEASES[source].values() if source in ECONOMIC_RELEASES else MEETING_KINDS
            if begin<=latest and end>=today and 0<=(end-begin).days<=10 and kind in kinds:
                event=dict(source=source, date=begin.isoformat(), end_date=end.isoformat(),
                    kind=kind, original=plain(row.get('original'),800), url=url,
                    fetched_at=fetched.isoformat(), stale=not current)
                starts=moment(row.get('starts_at'))
                if starts and starts.astimezone(BERLIN).date()==begin:
                    event['starts_at']=starts.isoformat()
                out.append(event)
    return dict(sources=states, events=sorted(out,key=lambda row:(row['date'],row['source']))[:12])


def context(db, info, now, *, language=None, limit=6):
    if now.tzinfo is None:
        raise ValueError('V1_NEWS_TIMEZONE_REQUIRED')
    result = dict(market=source_feed(db,'market',now,language=language,limit=limit),
                  calendar=official_events(db,now))
    if info is not None:
        isin=info['isin']
        result['company']=source_feed(db,isin,now,info=info,language=language,limit=limit)
        result['company_events']=company_events(info,now,now.astimezone(BERLIN).date(),
            auto=setting(db,COMPANY_EVENTS+isin),data=setting(db,'fundamentals:'+isin),card=setting(db,'card:'+isin))
    return result


def evidence(db, info, now):
    """Bounded original references for a decision, independent of display/translation."""
    background = context(db,info,now,limit=2)
    for key in ('market','company'):
        feed=background[key]
        feed['articles']=[{field:row[field] for field in
            ('title','url','provider','published','fetched_at','events') if field in row}
            for row in feed['articles']]
    for row in background['calendar']['events']:
        row.pop('original',None)
    return background


def status(path, isin=None, *, language='ru', now=None):
    from paper_account import registered_instrument
    uri=Path(path).resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        info=registered_instrument(db,isin) if isin else None
        return context(db,info,now or datetime.now(timezone.utc),language=language)

