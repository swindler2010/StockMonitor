"""Auxiliary Yahoo data stays explicitly separate from official GETTEX prices."""
from datetime import datetime, timezone
import math
import json
import re
from urllib.parse import quote
import requests
import pandas as pd

def search(query):
    result=requests.get('https://query1.finance.yahoo.com/v1/finance/search',
                        params={'q':query,'quotesCount':12,'newsCount':0},
                        headers={'User-Agent':'Mozilla/5.0'},timeout=20)
    result.raise_for_status()
    return [q for q in result.json().get('quotes',[]) if q.get('quoteType') in ('EQUITY','ETF')]

def valid_isin(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',value):return False
    digits=''.join(str(ord(c)-55) if c.isalpha() else c for c in value)
    return sum((int(c)*2//10+int(c)*2%10) if i%2 else int(c)
               for i,c in enumerate(reversed(digits)))%10==0

def _exact_isins(text,symbol,kind):
    # The provider returns JS arrays, not executable application code. Parse
    # only six-string record arrays as JSON; never evaluate the response.
    token=r'"(?:\\.|[^"\\])*"'
    records=re.finditer(r'new Array\(\s*('+token+r'(?:\s*,\s*'+token+r'){5})\s*\)',text)
    category='Stocks' if kind=='EQUITY' else 'Funds'
    matches=set()
    for record in records:
        row=json.loads('['+record.group(1)+']')
        parts=row[2].split('|')
        if row[1]!=category or len(parts)<2:continue
        if symbol not in (parts[0],*parts[2:]):continue
        if valid_isin(parts[1]):matches.add(parts[1])
    if len(matches)!=1:
        raise ValueError('ISIN не подтверждён: нет однозначного точного совпадения symbol')
    return matches.pop()

def resolve_symbol(symbol,cache_dir,search_result=None):
    """Confirm one exact Search listing; never guess an ISIN, market or currency."""
    try:
        if not isinstance(symbol,str) or not symbol.strip():raise ValueError('Нет symbol')
        symbol=symbol.strip()
        if search_result is None:
            matches=[q for q in search(symbol) if q.get('symbol')==symbol]
            if len(matches)!=1:raise ValueError('Нет однозначного результата Search для symbol')
            search_result=matches[0]
        if search_result.get('symbol')!=symbol:raise ValueError('Search вернул другой symbol')
        kind=search_result.get('quoteType')
        if kind not in ('EQUITY','ETF'):raise ValueError('Неподдерживаемый тип инструмента')
        headers={'User-Agent':'Mozilla/5.0 StockMonitor/0.16'}
        response=requests.get('https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol,safe=''),
            params={'range':'1d','interval':'1d'},headers=headers,timeout=20)
        response.raise_for_status();chart=response.json().get('chart',{})
        if chart.get('error') or len(chart.get('result') or [])!=1:raise ValueError('Источник не подтвердил listing')
        meta=chart['result'][0].get('meta',{})
        if meta.get('symbol')!=symbol or meta.get('instrumentType')!=kind:
            raise ValueError('Источник вернул другой symbol/тип инструмента')
        venue=meta.get('exchangeName');currency=meta.get('currency')
        if not isinstance(venue,str) or not venue or venue!=search_result.get('exchange'):raise ValueError('Venue не подтверждён или не совпадает с Search')
        if not isinstance(currency,str) or not re.fullmatch('[A-Za-z]{3}',currency):raise ValueError('Currency не подтверждена')
        if search_result.get('currency') and search_result['currency']!=currency:
            raise ValueError('Currency не совпадает с Search')
        response=requests.get('https://markets.businessinsider.com/ajax/SearchController_Suggest',
            params={'max_results':25,'query':symbol},headers=headers,timeout=20)
        response.raise_for_status();isin=_exact_isins(response.text,symbol,kind)
        return {'status':'CONFIRMED','isin':isin,'symbol':symbol,'venue':venue,'currency':currency,
                'source':'proxy','timezone':meta.get('exchangeTimezoneName'),
                'evidence':{'listing':'Yahoo chart metadata','isin':'Business Insider exact symbol','checksum':True}}
    except Exception as error:
        return {'status':'BLOCKED','error':'Identity BLOCKED: '+str(error)}

def history(symbol,period='8d',interval='15m',currency='EUR'):
    if currency!='EUR':raise ValueError('EUR_HISTORY_ONLY: non-EUR legacy stock request rejected before HTTP')
    url='https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol,safe='')
    result=requests.get(url,params={'range':period,'interval':interval,'includePrePost':'true'},
                        headers={'User-Agent':'Mozilla/5.0'},timeout=25)
    result.raise_for_status(); obj=result.json()['chart']['result'][0]
    if obj.get('meta',{}).get('currency')!=currency:
        raise ValueError('Валюта proxy не совпадает с валютой инструмента; смешивание запрещено.')
    stamps=obj.get('timestamp') or []; q=obj['indicators']['quote'][0]
    if not stamps:return pd.DataFrame()
    idx=pd.to_datetime(stamps,unit='s',utc=True).tz_convert('Europe/Berlin').tz_localize(None)
    if interval=='1d':idx=idx.normalize()
    frame=pd.DataFrame({k:q.get(k,[]) for k in ('open','high','low','close','volume')},index=idx)
    frame=frame.apply(pd.to_numeric,errors='coerce').dropna(subset=['open','high','low','close'])
    frame['volume']=frame.volume.fillna(0); return frame.sort_index()

def clean(value):
    if isinstance(value,dict):return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [clean(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value):return None
    if isinstance(value,(str,float,int,bool)) or value is None:return value
    return str(value)

def company_card(symbol,cache_dir):
    import yfinance as yf
    yf.set_tz_cache_location(str(cache_dir))
    ticker=yf.Ticker(symbol)
    result={'symbol':symbol,'source':'Yahoo Finance (через yfinance), не GETTEX',
            'fetched_at':datetime.now(timezone.utc).isoformat(),'errors':{}}
    for name,call in [('overview',ticker.get_info),('reports',lambda:ticker.get_income_stmt(freq='quarterly')),
                      ('news',lambda:ticker.get_news(count=8)),('events',ticker.get_calendar),
                      ('filings',ticker.get_sec_filings)]:
        try:
            value=call()
            if isinstance(value,pd.DataFrame):value=value.iloc[:,:4].to_dict()
            result[name]=clean(value)
        except Exception as e:result['errors'][name]=type(e).__name__+' — данные временно недоступны'
    return result

