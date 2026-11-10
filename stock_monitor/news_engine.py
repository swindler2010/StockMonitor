"""Auxiliary news only. Call from the existing Dispatcher, never the Tk thread.

No portfolio/market writes, paid APIs or article translation.
Caller persists returned snapshots in news:<ISIN> / news:market.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import re
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import xml.etree.ElementTree as ET

FEEDS = (
    ('ECB', 'https://www.ecb.europa.eu/rss/press.html'),
    ('Federal Reserve', 'https://www.federalreserve.gov/feeds/press_all.xml'),
)
ISSUER_FEEDS = {'US0378331005': ('Apple Newsroom', 'https://www.apple.com/newsroom/rss-feed.rss')}
EVENTS = {'earnings': r'\b(earnings|results|отчётност|ergebnis)',
          'guidance': r'\b(guidance|outlook|прогноз|prognose)',
          'dividend': r'\b(dividend|дивиденд)',
          'deal': r'\b(merger|acquisition|takeover|слияни|поглощени|übernahme)',
          'management': r'\b(CEO|CFO|appoint|resign|назначен|отставк|vorstand)'}


def plain(value, limit=1600):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))).strip()[:limit]


def safe_url(value):
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            return ''
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in ('guccounter', 'guce_referrer', '.tsrc')]
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ''))
    except (ValueError, TypeError):
        return ''


def timestamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            dt = datetime.fromtimestamp(value, timezone.utc)
        else:
            try:
                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            except ValueError:
                dt = parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            return None  # Do not guess an exchange timezone.
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def normalize(rows, now=None):
    now = time.time() if now is None else now
    result, seen, headlines = [], set(), set()
    for raw in rows:
        title = plain(raw.get('title'), 500)
        url = safe_url(raw.get('url'))
        if not title or not url:
            continue
        key = url
        headline = re.sub(r'\W+', ' ', title.casefold()).strip()
        if key in seen or headline in headlines:
            continue
        seen.add(key); headlines.add(headline)
        published = timestamp(raw.get('published'))
        age = now - datetime.fromisoformat(published).timestamp() if published else None
        result.append({**raw, 'title': title, 'url': url, 'summary': plain(raw.get('summary')),
                       'published': published, 'date_status': 'UNKNOWN' if age is None else 'FUTURE' if age < -300 else 'STALE' if age > 7*86400 else 'CURRENT',
                       'events': [k for k, pattern in EVENTS.items() if re.search(pattern, title, re.I)],
                       'event_basis': 'HEADLINE_KEYWORDS_NOT_CONFIRMED_EVENT',
                       'text_quality': 'UPSTREAM_REPLACEMENT_CHARACTERS' if '\ufffd' in title+plain(raw.get('summary')) else 'AS_PROVIDED',
                       'translation': {'status': 'DISABLED_BY_OWNER', 'text_is_original': True}})
    return sorted(result, key=lambda x: x['published'] or '', reverse=True)[:100]


def parse_feed(data, provider):
    if len(data) > 2_000_000 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('UNSAFE_OR_OVERSIZE_FEED')
    root = ET.fromstring(data)
    rows = []
    for item in root.findall('.//item')[:100]:
        link=safe_url(item.findtext('link'))
        publisher=item.findtext('source')
        rows.append(dict(title=item.findtext('title'), url=item.findtext('link'),
                         summary=item.findtext('description'), published=item.findtext('pubDate'),
                         provider=publisher or (urlsplit(link).hostname if link else None) or provider, feed_provider=provider, scope='market', language=None,
                         publisher_status='NAMED_BY_FEED' if publisher else 'LINK_DOMAIN_ONLY_ORIGINAL_PUBLISHER_UNCONFIRMED',
                         publisher_url=safe_url(item.find('source').get('url','')) if item.find('source') is not None else '',
                         relevance='Market/economy; no company association'))
    ns={'a':'http://www.w3.org/2005/Atom'}
    for item in root.findall('a:entry',ns)[:100]:
        links=item.findall('a:link',ns)
        url=next((x.get('href') for x in links if x.get('rel','alternate')=='alternate'),None)
        rows.append(dict(title=item.findtext('a:title',namespaces=ns),url=url,
                         summary=item.findtext('a:summary',namespaces=ns),
                         published=item.findtext('a:published',namespaces=ns) or item.findtext('a:updated',namespaces=ns),
                         provider=provider,feed_provider=provider,scope='market',language=None,relevance='Market/economy; no company association'))
    return rows


def download_feed(url):
    import requests
    with requests.get(url, timeout=(4, 10), stream=True,
                      headers={'User-Agent': 'StockMonitor/0.17 news reader'}, allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('FEED_NOT_200')
        chunks, size = [], 0
        for chunk in response.iter_content(16384):
            size += len(chunk)
            if size > 2_000_000:
                raise ValueError('OVERSIZE_FEED')
            chunks.append(chunk)
        return b''.join(chunks)


def snapshot(rows, errors, previous=None, now=None):
    now = time.time() if now is None else now
    items = normalize(rows, now)
    retained = not items and bool((previous or {}).get('articles'))
    if retained:
        items = normalize(previous['articles'], now)
    return dict(articles=items, errors=errors,
                status='STALE_CACHE' if retained else 'PARTIAL' if items and errors else 'AVAILABLE' if items else 'UNAVAILABLE' if errors else 'EMPTY',
                attempted_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                fetched_at=previous.get('fetched_at') if retained else datetime.fromtimestamp(now, timezone.utc).isoformat() if items else None,
                retained_previous=retained)


def market_news(previous=None, fetch=download_feed):
    rows, errors, attempts = [], {}, []
    for provider, url in FEEDS:
        try:
            incoming=parse_feed(fetch(url), provider)
            receipt=getattr(fetch,'last_receipt',{})
            for row in incoming:row['fetched_at']=timestamp(receipt.get('at',time.time()))
            rows.extend(incoming)
            attempts.append({'provider':provider,'url':url,'status':'AVAILABLE' if incoming else 'EMPTY','transport':receipt})
        except Exception as exc:
            errors[provider] = getattr(exc,'code',type(exc).__name__)
            attempts.append({'provider':provider,'url':url,'status':'UNAVAILABLE','error':errors[provider]})
    result=snapshot(rows, errors, previous)
    times=[a.get('fetched_at') for a in rows if a.get('fetched_at')]
    if times:result['fetched_at']=max(times)
    return {**result,'source_attempts':attempts,'translation_policy':'ORIGINAL_ONLY_BY_OWNER_REQUEST'}


def company_news(instrument, client, previous=None):
    """Sequential public RSS, with explicit name relevance and source attempts."""
    from news_identity import match, query, aliases
    from news_http import FeedError
    symbol=instrument.get('profile_symbol') or instrument.get('history_symbol') or ''
    isin=instrument['isin']
    identity={'isin':isin,'name':instrument.get('name',''),'symbol':symbol,'aliases':aliases(instrument)}
    if previous and previous.get('identity')!=identity:previous=None
    sources=[]
    if symbol:
        sources.append(('Yahoo Finance RSS','https://feeds.finance.yahoo.com/rss/2.0/headline?'+urlencode({'s':symbol,'region':'US','lang':'en-US'}),False))
    sources.append(('Google News RSS','https://news.google.com/rss/search?'+urlencode({'q':query(instrument),'hl':'en-US','gl':'US','ceid':'US:en'}),False))
    if isin in ISSUER_FEEDS:
        name,url=ISSUER_FEEDS[isin];sources.append((name,url,True))
    rows=[];errors={};attempts=[]
    for provider,url,official in sources:
        attempt={'provider':provider,'url':url,'attempted_at':timestamp(time.time())}
        try:
            raw=parse_feed(client(url),provider)
            selected=[]
            for row in raw:
                if not plain(row.get('title')) or not safe_url(row.get('url')):continue
                # Google's description repeats linked headlines; it is not an article abstract.
                if provider=='Google News RSS':row['summary']=''
                basis=match(row,instrument,official)
                if not basis:continue
                row.update(scope='company',isin=isin,symbol=symbol,relevance=basis,
                           fetched_at=timestamp(getattr(client,'last_receipt',{}).get('at',time.time())),
                           link_kind='AGGREGATOR_LINK_TO_ORIGINAL' if provider=='Google News RSS' else 'PUBLISHER_LINK',
                           summary_status='PROVIDED_BY_FEED' if row.get('summary') else 'NOT_PROVIDED')
                selected.append(row)
            rows.extend(selected)
            attempt.update(status='AVAILABLE' if selected else 'NO_MATCHING_ITEMS',received=len(raw),matched=len(selected),
                           transport=getattr(client,'last_receipt',{}))
        except Exception as exc:
            code=exc.code if isinstance(exc,FeedError) else type(exc).__name__
            errors[provider]=code;attempt.update(status='UNAVAILABLE',error=code,retry_at=getattr(exc,'retry_at',None))
        attempts.append(attempt)
        if any(a['date_status']=='CURRENT' for a in normalize(rows)):break
    result=snapshot(rows,errors,previous)
    if rows and attempts[-1].get('transport',{}).get('at'):
        result['fetched_at']=timestamp(attempts[-1]['transport']['at'])
    result['articles']=result['articles'][:30]
    return {**result,'schema_version':2,'identity':identity,'isin':isin,'symbol':symbol,'source_attempts':attempts,
            'fallback_status':'USED' if any(a.get('matched',0)>0 for a in attempts[1:]) else 'EXHAUSTED' if len(attempts)>1 or not rows else 'NOT_NEEDED',
            'translation_policy':'ORIGINAL_ONLY_BY_OWNER_REQUEST',
            'identity_notice':'News name matching does not verify the watchlist security, ISIN or exchange listing.'}
