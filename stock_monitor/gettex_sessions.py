"""Exchange-confirmed session years. Extend only against an official calendar."""
from datetime import date

CALENDAR_URL = 'https://www.gettex.de/handel/handelskalender'
DAILY_START = '2015-01-19'
INTRADAY_START = '2026-01-01'
YEARS = {
    2026: {
        'reference': 'https://www.gettex.de/fileadmin/bulletin/bekanntmachungen.gtx.20261116.pdf',
        'holidays': ('01-01', '04-03', '04-06', '05-01', '12-24', '12-25', '12-31'),
        'short_sessions': {},
    },
}
VERIFIED_THROUGH = f'{max(YEARS)}-12-31'


def bounds(daily=False):
    return (DAILY_START if daily else INTRADAY_START, VERIFIED_THROUGH)


def years_covered(first, last, daily=False):
    start, end = bounds(daily)
    return int(start[:4]) <= first <= last <= int(end[:4]) and all(
        y in YEARS for y in range(max(first, int(INTRADAY_START[:4])), last+1))


def warning(today):
    remaining = (date.fromisoformat(VERIFIED_THROUGH)-today).days
    if remaining<0:
        return f'Подтверждённый календарь GETTEX закончился {VERIFIED_THROUGH}'
    if remaining<=60:
        return f'Календарь GETTEX подтверждён до {VERIFIED_THROUGH}. Следующий год ожидает подтверждения'
    return f'Календарь GETTEX подтверждён до {VERIFIED_THROUGH}'
