"""Official schedules for presentation; never a trading risk input."""
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
import re

SETTING = 'ui_official_calendar'
SOURCES = {
    'ECB': 'https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html',
    'FED': 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm',
    'BLS': 'https://www.bls.gov/schedule/news_release/bls.ics',
    'BEA': 'https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics',
}
SOURCE_PAGES = {**SOURCES,'BLS':'https://www.bls.gov/schedule/news_release/','BEA':'https://www.bea.gov/news/schedule'}
ECONOMIC_RELEASES = {
    'BLS': {'consumer price index':'US_CPI','producer price index':'US_PPI',
            'employment situation':'US_JOBS','job openings and labor turnover':'US_JOLTS'},
    'BEA': {'gross domestic product,':'US_GDP','gdp (':'US_GDP','personal income and outlays':'US_PCE'},
}
MONTHS = {v: n for n, v in enumerate(('January', 'February', 'March', 'April', 'May', 'June',
                                    'July', 'August', 'September', 'October', 'November', 'December'), 1)}


MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})


class Blocks(HTMLParser):
    """Read only documented date/description blocks, with bounded input/output."""
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.blocks, self.active, self.depth, self.parts = [], None, 0, []
        if len(text) > 2_000_000: raise ValueError('CALENDAR_TOO_LARGE')
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if self.active:
            if tag == self.active[0]: self.depth += 1
            if tag == 'br': self.parts.append(' ')
            return
        cls = dict(attrs).get('class', '').split()
        kind = tag if tag in ('dt', 'dd', 'h4') else next((k for k in ('fomc-meeting__month', 'fomc-meeting__date') if k in cls), None)
        if kind: self.active, self.depth, self.parts = (tag, kind), 1, []

    def handle_endtag(self, tag):
        if self.active and tag == self.active[0]:
            self.depth -= 1
            if not self.depth:
                self.blocks.append((self.active[1], re.sub(r'\s+', ' ', ''.join(self.parts)).strip()))
                self.active = None
                if len(self.blocks) > 2000: raise ValueError('CALENDAR_BLOCK_LIMIT')

    def handle_data(self, text):
        if self.active: self.parts.append(text)


def row(source, start, end, kind, original):
    if end < start or (end-start).days > 10: raise ValueError('CALENDAR_DATE_RANGE')
    return dict(source=source, date=start.isoformat(), end_date=end.isoformat(), kind=kind,
                precision='DATE_RANGE' if end != start else 'DATE', original=original[:800], url=SOURCE_PAGES[source])


def parse_economic_calendar(source, data):
    if len(data)>2_000_000:
        raise ValueError('CALENDAR_TOO_LARGE')
    content=data.decode('utf-8-sig').replace('\r\r\n','\n').replace('\r\n','\n')
    content=re.sub(r'\n[ \t]', '', content)
    if not content.startswith('BEGIN:VCALENDAR'):
        raise ValueError('ECONOMIC_CALENDAR_FORMAT_CHANGED')
    releases=ECONOMIC_RELEASES[source]
    events=[]
    eastern=ZoneInfo('America/New_York')
    berlin=ZoneInfo('Europe/Berlin')
    for block in re.findall(r'^BEGIN:VEVENT\s*\n(.*?)^END:VEVENT',content,re.M|re.S)[:2000]:
        fields = {k:(name,value) for line in block.splitlines() if ':' in line
                  for name, value in [line.split(':',1)] for k in [name.split(';')[0]]}
        if fields.get('STATUS',('', ''))[1]=='CANCELLED':
            continue
        summary=fields.get('SUMMARY',('', ''))[1].replace(r'\,',',')
        name=summary.casefold().removeprefix('the ')
        kind=next((kind for label,kind in releases.items() if name.startswith(label)), None)
        prop,value=fields.get('DTSTART',('', ''))
        if not kind or not value:
            continue
        if re.fullmatch(r'\d{8}',value):
            day=datetime.strptime(value,'%Y%m%d').date()
            events.append(row(source,day,day,kind,summary))
        elif re.fullmatch(r'\d{8}T\d{6}Z?',value):
            timezone_id=prop.split('TZID=',1)[1].split(';')[0].strip('"') if 'TZID=' in prop else ''
            if not value.endswith('Z') and timezone_id not in ('','America/New_York','US/Eastern'):
                continue
            instant=datetime.strptime(value.rstrip('Z'),'%Y%m%dT%H%M%S').replace(
                tzinfo=timezone.utc if value.endswith('Z') else eastern)
            day=instant.astimezone(berlin).date()
            events.append(dict(row(source,day,day,kind,summary),precision='TIME',
                               starts_at=instant.astimezone(timezone.utc).isoformat()))
    if not events:
        raise ValueError('ECONOMIC_CALENDAR_FORMAT_CHANGED_OR_EMPTY')
    return sorted({(r['date'],r['kind']):r for r in events}.values(),key=lambda r:r['date'])[-500:]


def parse(source, data, year_floor=None):
    if source in ECONOMIC_RELEASES:
        return parse_economic_calendar(source,data)
    year_floor = year_floor or datetime.now(ZoneInfo('Europe/Berlin')).year
    blocks = Blocks(data.decode('utf-8-sig')).blocks
    out = []
    if source == 'ECB':
        pending = None
        for key, value in blocks:
            if key == 'dt':
                pending = datetime.strptime(value, '%d/%m/%Y').date() if re.fullmatch(r'\d{2}/\d{2}/\d{4}', value) else None
            elif key == 'dd' and pending:
                kind = ('GENERAL' if 'General Council' in value else 'NON_MONETARY' if 'non-monetary' in value
                        else 'MONETARY_DAY1' if '(Day 1)' in value else 'MONETARY_DAY2' if '(Day 2)' in value else 'OTHER')
                out.append(row(source, pending, pending, kind, value)); pending = None
    elif source == 'FED':
        year = month = None
        for key, value in blocks:
            if key == 'h4':
                match = re.fullmatch(r'(20\d{2}) FOMC Meetings', value)
                year = int(match[1]) if match and int(match[1]) >= year_floor else None; month = None
            elif key == 'fomc-meeting__month': month = value
            elif key == 'fomc-meeting__date' and year and month:
                months = month.split('/')
                days = re.fullmatch(r'(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*\*?', value)
                if not days or any(m not in MONTHS for m in months) or len(months) > 2: raise ValueError('FED_DATE_FORMAT_CHANGED')
                start = date(year, MONTHS[months[0]], int(days[1]))
                end_month = MONTHS[months[-1]]
                end = date(year + int(end_month < start.month), end_month, int(days[2] or days[1]))
                out.append(row(source, start, end, 'FOMC', month+' '+value+' '+str(year))); month = None
    else: raise ValueError('CALENDAR_UNKNOWN_SOURCE')
    if not out or (source == 'FED' and len(out) < 4): raise ValueError('CALENDAR_FORMAT_CHANGED_OR_EMPTY')
    return sorted({(r['date'], r['kind']): r for r in out}.values(), key=lambda r: r['date'])[:500]


def refresh(previous, fetch, now=None):
    now = now or datetime.now(timezone.utc)
    result = dict(attempted_at=now.isoformat(), sources={})
    for source, url in SOURCES.items():
        old = (previous.get('sources') or {}).get(source, {})
        try:
            rows = parse(source, fetch(url))
            receipt = getattr(fetch, 'last_receipt', {})
            fetched = datetime.fromtimestamp(receipt['at'], timezone.utc).isoformat() if receipt.get('url') == url and receipt.get('at') else now.isoformat()
            result['sources'][source] = dict(status='AVAILABLE', rows=rows, fetched_at=fetched, error=None)
        except Exception as error:
            result['sources'][source] = dict(status='STALE_CACHE' if old.get('rows') else 'UNAVAILABLE',
                                            rows=old.get('rows', [])[:500], fetched_at=old.get('fetched_at'),
                                            error=str(getattr(error, 'code', str(error) if isinstance(error, ValueError) else type(error).__name__))[:100])
    return result


def upcoming(snapshot, today=None, limit=100):
    today = today or datetime.now(ZoneInfo('Europe/Berlin')).date()
    out = []
    for source, value in (snapshot.get('sources') or {}).items():
        if source not in SOURCES: continue
        try: stale = value.get('status') != 'AVAILABLE' or (datetime.now(timezone.utc)-datetime.fromisoformat(value['fetched_at'])).total_seconds() > 172800
        except (ValueError, TypeError, KeyError): stale = True
        for item in value.get('rows', [])[:500]:
            try:
                if date.fromisoformat(item['end_date']) < today: continue
                date.fromisoformat(item['date'])
            except (ValueError, TypeError, KeyError): continue
            out.append({**item, 'source': source, 'url': SOURCE_PAGES[source], 'stale': stale})
    return sorted(out, key=lambda r: (r['date'], r['source']))[:limit]
