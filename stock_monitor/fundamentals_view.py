"""Structured auxiliary statements. Does not modify price/DQ/analytical CORE."""
from datetime import datetime, timezone
import math
import re
from urllib.parse import quote

METRICS = {
 'TotalRevenue':('Выручка','money'), 'OperatingIncome':('Операционная прибыль','money'),
 'NetIncome':('Чистая прибыль','money'), 'BasicEPS':('Базовый EPS','per_share'),
 'DilutedEPS':('Разводнённый EPS','per_share'), 'EBITDA':('EBITDA','money'),
 'OperatingCashFlow':('Операционный денежный поток','money'),
 'FreeCashFlow':('Свободный денежный поток','money'), 'TotalDebt':('Долг','money'),
 'CashAndCashEquivalents':('Денежные средства','money'),
 'StockholdersEquity':('Капитал акционеров','money'), 'OrdinarySharesNumber':('Обыкновенные акции','shares'),
 'InvestingCashFlow':('Инвестиционный денежный поток','money'),
 'FinancingCashFlow':('Финансовый денежный поток','money'),
 'CapitalExpenditure':('Капитальные затраты','money'),
 'CashDividendsPaid':('Выплаченные дивиденды','money'),
}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def statement_rows(data, kind, frequency, currency, updated, source):
    if frequency not in ('quarterly','yearly','trailing'):
        raise ValueError('UNSUPPORTED_FREQUENCY')
    if kind not in ('income','balance','cashflow') or (kind=='balance' and frequency=='trailing'):
        raise ValueError('UNSUPPORTED_STATEMENT_PERIOD')
    rows=[]
    for period, values in sorted(data.items(),key=lambda x:str(x[0])):
        if not isinstance(values,dict):continue
        period_end=str(period).split(' ')[0]
        try:datetime.strptime(period_end,'%Y-%m-%d')
        except ValueError:continue
        for original,value in values.items():
            key=re.sub(r'\s+','',original)
            if key not in METRICS or number(value) is None:continue
            label,unit=METRICS[key]
            rows.append(dict(metric=key,label=label,value=value,period_end=str(period).split(' ')[0],
                frequency=frequency,statement=kind,currency=currency if unit!='shares' else None,
                period_basis='POINT_IN_TIME' if kind=='balance' else 'PROVIDER_PERIOD_END; start date not supplied',
                unit=unit,source=source,updated_at=updated,method='PROVIDER_REPORTED',provider_key=original,
                status='AVAILABLE' if currency or unit=='shares' else 'CURRENCY_UNKNOWN'))
    # Compute margins only from the same statement, period, frequency and currency.
    if kind=='income' and currency:
        for period in sorted({r['period_end'] for r in rows}):
            values={r['metric']:r for r in rows if r['period_end']==period}
            revenue=values.get('TotalRevenue',{}).get('value')
            if revenue is None or revenue<=0:continue
            for key,label in [('NetIncome','Чистая рентабельность'),('OperatingIncome','Операционная рентабельность')]:
                if key not in values:continue
                rows.append({**values[key],'metric':key+'Margin','label':label,
                    'value':values[key]['value']/revenue,'unit':'ratio','currency':None,
                    'method':key+' / TotalRevenue; same statement period and reporting currency'})
    return rows


def fetch_statements(symbol, cache_dir, ticker=None):
    import market
    if ticker is None:
        import yfinance as yf
        yf.set_tz_cache_location(str(cache_dir));ticker=yf.Ticker(symbol)
    updated=datetime.now(timezone.utc).isoformat()
    result=dict(symbol=symbol,source='Yahoo Finance via yfinance',updated_at=updated,metrics=[],errors={},datasets={})
    try:overview=ticker.get_info() or {}
    except Exception as exc:
        overview={};result['errors']['overview']=type(exc).__name__
    result['financial_currency']=overview.get('financialCurrency')
    result['source_url']='https://finance.yahoo.com/quote/'+quote(symbol,safe='')+'/financials/'
    result['identity_basis']='Configured provider symbol; not independent ISIN confirmation'
    if overview.get('symbol') and overview['symbol'].casefold()!=symbol.casefold():
        result.update(status='IDENTITY_MISMATCH',errors={'identity':'PROVIDER_SYMBOL_MISMATCH'})
        return result
    # Raw overview remains separated: multiple fields have unspecified/forward/TTM periods.
    result['overview']=market.clean(overview)
    result['valuation_metrics']=overview_rows(result['overview'],updated,result['source'])
    try:result['calendar']=market.clean(ticker.get_calendar() or {})
    except Exception as exc:result['calendar']={};result['errors']['calendar']=type(exc).__name__
    result['calendar_basis']='Provider calendar; dates may be estimates or a range, not confirmed announcements'
    for kind,method,frequencies in [('income',ticker.get_income_stmt,('quarterly','yearly','trailing')),
                                    ('balance',ticker.get_balance_sheet,('quarterly','yearly')),
                                    ('cashflow',ticker.get_cashflow,('quarterly','yearly','trailing'))]:
        for freq in frequencies:
            key=kind+':'+freq
            try:
                frame=method(freq=freq)
                data=market.clean(frame.iloc[:,:8].to_dict())
                rows=statement_rows(data,kind,freq,result['financial_currency'],updated,result['source'])
                result['metrics'].extend(rows)
                result['datasets'][key]={'status':'AVAILABLE' if rows else 'UNAVAILABLE','period_count':len(data)}
            except Exception as exc:
                result['errors'][key]=type(exc).__name__
                result['datasets'][key]={'status':'UNAVAILABLE'}
    result['status']='PARTIAL' if result['metrics'] and (result['errors'] or any(x['status']=='UNAVAILABLE' for x in result['datasets'].values())) else 'AVAILABLE' if result['metrics'] else 'UNAVAILABLE'
    if result['status']=='UNAVAILABLE' and (result['valuation_metrics'] or result['calendar']):result['status']='PARTIAL'
    for row in result['metrics']:
        row['source_url']='https://finance.yahoo.com/quote/'+quote(symbol,safe='')+'/'+{'income':'financials','balance':'balance-sheet','cashflow':'cash-flow'}[row['statement']]+'/'
    for row in result['valuation_metrics']:row['source_url']='https://finance.yahoo.com/quote/'+quote(symbol,safe='')+'/'
    return result


def legacy_card(card):
    """Existing reports are known quarterly income; no annual/TTM fabrication."""
    overview=card.get('overview') or {}
    return statement_rows(card.get('reports') or {},'income','quarterly',overview.get('financialCurrency'),card.get('fetched_at'),card.get('source','UNKNOWN'))


def overview_rows(overview,updated,source):
    """Provider-reported valuations remain separate from statement periods.

    Do not divide an EUR quote by USD EPS, infer fiscal dates, or rescale an
    ambiguously defined dividendYield. Only the explicit trailing yield is used.
    """
    fields={
        'trailingPE':('P/E (TTM)','multiple','trailing'),
        'forwardPE':('P/E (прогноз)','multiple','forward'),
        'priceToSalesTrailing12Months':('P/S (TTM)','multiple','trailing'),
        'priceToBook':('P/B','multiple','latest_reported'),
        'trailingAnnualDividendRate':('Дивиденд на акцию (12 месяцев)','per_share','trailing'),
        'trailingAnnualDividendYield':('Дивидендная доходность (12 месяцев)','ratio','trailing'),
    }
    rows=[]
    for key,(label,unit,frequency) in fields.items():
        value=number(overview.get(key))
        if value is None:continue
        # A rate above 100% is not silently corrected to a guessed scale.
        status='REVIEW_REQUIRED' if unit=='ratio' and not 0<=value<=1 else 'AVAILABLE'
        currency=overview.get('currency') if unit=='per_share' else None
        if unit=='per_share' and not currency:status='CURRENCY_UNKNOWN'
        rows.append(dict(metric=key,label=label,value=value,period_end=None,
            frequency=frequency,statement='valuation',currency=currency,unit=unit,
            source=source,updated_at=updated,method='PROVIDER_REPORTED; fiscal period end not supplied',
            provider_key=key,status=status,period_basis='AS_REPORTED; not a historical statement'))
    return rows


def cache_matches(value, info):
    return (isinstance(value,dict) and value.get('isin')==info.get('isin') and
            value.get('symbol')==(info.get('profile_symbol') or info.get('history_symbol')))


def freshness(value,now=None,max_age_hours=24):
    try:
        stamp=datetime.fromisoformat(value['updated_at'])
        if stamp.tzinfo is None:return 'UNKNOWN'
        age=((now or datetime.now(timezone.utc))-stamp).total_seconds()
        return 'FUTURE_TIMESTAMP' if age< -300 else 'STALE' if age>max_age_hours*3600 else 'CURRENT_CACHE'
    except (KeyError,TypeError,ValueError):return 'UNKNOWN'


def merge_saved(previous,incoming):
    """Keep unavailable datasets explicitly marked as retained, never re-date them."""
    if incoming.get('status')=='IDENTITY_MISMATCH':return previous
    if previous.get('symbol')!=incoming.get('symbol'):previous={}
    result=dict(incoming);result['metrics']=list(incoming.get('metrics',[]))
    result['last_refresh']={'attempted_at':incoming.get('updated_at'),'status':incoming.get('status'),'errors':dict(incoming.get('errors',{}))}
    result['retained_datasets']=[]
    for key,state in incoming.get('datasets',{}).items():
        if state.get('status')=='AVAILABLE':continue
        old=[dict(row,cache_status='RETAINED_AFTER_REFRESH_FAILURE') for row in previous.get('metrics',[])
             if row['statement']+':'+row['frequency']==key]
        if old:result['metrics'].extend(old);result['retained_datasets'].append(key)
    if not incoming.get('valuation_metrics') and previous.get('valuation_metrics'):
        result['valuation_metrics']=[dict(row,cache_status='RETAINED_AFTER_REFRESH_FAILURE') for row in previous['valuation_metrics']]
        result['retained_datasets'].append('valuation')
    if not incoming.get('calendar') and previous.get('calendar'):
        result['calendar']=previous['calendar'];result['calendar_status']='RETAINED_AFTER_REFRESH_FAILURE'
        result['calendar_updated_at']=previous.get('calendar_updated_at') or previous.get('updated_at')
        result['retained_datasets'].append('calendar')
    else:result['calendar_updated_at']=incoming.get('updated_at')
    if result['retained_datasets']:result['status']='PARTIAL_RETAINED'
    return result
