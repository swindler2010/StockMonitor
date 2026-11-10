from i18n import tr
"""Presentation of auxiliary company data with explicit units and provenance."""
from datetime import datetime,timezone
import math

PERCENT={'ebitdaMargins','profitMargins','grossMargins','returnOnEquity','revenueGrowth','earningsGrowth'}
DATES={'exDividendDate','earningsTimestamp','Ex-Dividend Date','Dividend Date','Earnings Date'}
MONEY={'totalRevenue','netIncomeToCommon','trailingEps','forwardEps','ebitda','freeCashflow','totalDebt','totalCash'}
QUOTE={'marketCap','fiftyTwoWeekLow','fiftyTwoWeekHigh'}
LABELS = {'Total Revenue': 'Выручка', 'Operating Revenue': 'Операционная выручка', 'Cost Of Revenue': 'Себестоимость', 'Gross Profit': 'Валовая прибыль', 'Operating Income': 'Операционная прибыль', 'Net Income': 'Чистая прибыль', 'Net Income Common Stockholders': 'Чистая прибыль акционеров', 'Basic EPS': 'Базовый EPS', 'Diluted EPS': 'Разводнённый EPS', 'EBITDA': 'EBITDA', 'Normalized EBITDA': 'Нормализованная EBITDA', 'Tax Provision': 'Налог на прибыль', 'Pretax Income': 'Прибыль до налогов', 'Interest Expense': 'Процентные расходы', 'Earnings Date': 'Дата отчётности', 'Ex-Dividend Date': 'Дата без дивиденда', 'Dividend Date': 'Дата выплаты дивиденда', 'Earnings High': 'Прогноз EPS — верхняя граница', 'Earnings Low': 'Прогноз EPS — нижняя граница', 'Earnings Average': 'Средний прогноз EPS', 'Revenue High': 'Прогноз выручки — верхняя граница', 'Revenue Low': 'Прогноз выручки — нижняя граница', 'Revenue Average': 'Средний прогноз выручки'}


def value(raw, key='', currency=None):
    if raw is None:return tr('нет данных')
    if isinstance(raw,(list,tuple)):return '; '.join(value(v,key,currency) for v in raw) or tr('нет данных')
    if isinstance(raw,(int,float)) and not isinstance(raw,bool):
        if not math.isfinite(raw):return tr('нет данных')
        if key in DATES:
            try:return datetime.fromtimestamp(raw,tz=timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
            except (OverflowError,OSError,ValueError):return tr('некорректная дата источника')
        if key in PERCENT:return f'{raw*100:.2f} %'
        formatted=f'{raw:,.4f}'.rstrip('0').rstrip('.').replace(',',' ')
        if key=='dividendYield':return formatted+tr(' (значение поставщика; единица не подтверждена)')
        return formatted+(' '+currency if currency else '')
    return str(raw)


def fundamentals(overview,labels):
    lines=[]
    for key,label in labels.items():
        currency=(overview.get('financialCurrency') or tr('валюта отчётности неизвестна')) if key in MONEY else (overview.get('currency') or tr('валюта котировки неизвестна')) if key in QUOTE else None
        lines.append(f'{tr(label)}: {value(overview.get(key),key,currency)}')
    return '\n'.join(lines)


def reports(card):
    overview=card.get('overview') or {}
    currency=overview.get('financialCurrency') or tr('не указана источником')
    lines=[f"{tr('Квартальная отчётность. Валюта: ')}{currency}.", tr('Показатели на акцию и количество акций имеют собственные единицы; пересчёта в EUR нет.')]
    data=card.get('reports') or {}
    if isinstance(data,dict):
        for period,values in sorted(data.items(),key=lambda x:str(x[0]),reverse=True):
            if not isinstance(values,dict):continue
            lines.append(tr('\nПериод: ')+str(period).split(' ')[0])
            for key,raw in values.items():
                if raw is not None:lines.append(f'  {tr(LABELS.get(key,key))}: {value(raw)}')
    if len(lines)==2:lines.append(tr('\nИсточник ещё не предоставил отчёты.'))
    lines.append(tr('\nПубликации эмитента:'))
    filings=card.get('filings') or []
    if not isinstance(filings,list):filings=[]
    for filing in filings:
        if not isinstance(filing,dict):continue
        lines.append(' • '.join(str(filing[k]) for k in ('date','type','title') if filing.get(k)) or tr('Публикация без описания'))
        url=filing.get('edgarUrl') or filing.get('url')
        if isinstance(url,str) and url.startswith(('https://','http://')):lines.append(url)
    if not filings:lines.append(tr('Список публикаций не получен.'))
    return '\n'.join(lines)


def events(card):
    data=card.get('events') or {}
    if not isinstance(data,dict) or not data:return tr('События не получены от источника.')
    return '\n'.join(f'{tr(LABELS.get(key,key))}: {value(raw,key)}' for key,raw in data.items())+tr('\nДаты и прогнозы поставщика могут измениться.')
