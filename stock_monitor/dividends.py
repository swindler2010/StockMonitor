"""Gross cash dividends. Unknown coverage/holdings/FX never means zero."""
import math
import pandas as pd

def parse_receipts(text):
    result=[]
    for line in text.splitlines():
        if not line.strip():continue
        date,amount,currency=line.split()
        date=pd.Timestamp(date).date().isoformat();amount=float(amount.replace(',','.'))
        if not math.isfinite(amount) or amount<0 or len(currency)!=3 or not currency.isalpha():raise ValueError('Некорректная выплата')
        result.append(dict(date=date,amount=amount,currency=currency.upper()))
    return result

def model_returns(row,data):
    fills=row.get('fills',[]);events=data.get('events',[])
    if not fills or not data.get('complete'):return total_return(row.get('pnl'),[],lambda _:None,row['identity']['currency'])
    timezone=row['identity'].get('timezone','UTC')
    start=pd.Timestamp(fills[0]['at']).tz_convert(timezone).date().isoformat()
    end=pd.Timestamp.now(tz=timezone).date().isoformat()
    complete=data.get('start','9999')<=start and data.get('end','')>=end
    def quantity_at(day):
        cutoff=pd.Timestamp(day,tz=timezone);quantity=0
        for fill in fills:
            if pd.Timestamp(fill['at'])<cutoff:quantity+=fill['quantity']*(1 if fill['signal'] in ('BUY','ADD') else -1)
        return quantity
    selected=[event for event in events if start<=event['date']<=end]
    return total_return(row.get('pnl'),selected,quantity_at,row['identity']['currency'],complete)

def total_return(price_pnl,events,quantity_at,currency,complete=False):
    if not complete:return {'price_pnl':price_pnl,'dividends':None,'total_return':None}
    amount=0.
    for event in events:
        qty=quantity_at(event['date'])
        if qty is None or event.get('currency')!=currency:return {'price_pnl':price_pnl,'dividends':None,'total_return':None}
        value=float(event['amount'])*float(qty)
        if not math.isfinite(value):raise ValueError('Invalid dividend')
        amount+=value
    return {'price_pnl':price_pnl,'dividends':amount,'total_return':None if price_pnl is None else price_pnl+amount}

def download(symbol):
    import requests,time
    from urllib.parse import quote
    end=int(time.time());start=end-5*366*86400
    response=requests.get('https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol,safe=''),params={'period1':start,'period2':end,'interval':'1d','events':'div'},headers={'User-Agent':'Mozilla/5.0'},timeout=20)
    response.raise_for_status();data=response.json()['chart']['result'][0];meta=data['meta']
    timezone=meta.get('exchangeTimezoneName','UTC');currency=meta.get('currency')
    if not currency:raise ValueError('Dividend currency unavailable')
    events=[{'date':pd.Timestamp(e['date'],unit='s',tz='UTC').tz_convert(timezone).date().isoformat(),'amount':float(e['amount']),'currency':currency} for e in data.get('events',{}).get('dividends',{}).values()]
    stamps=data.get('timestamp') or []
    if not stamps:raise ValueError('Corporate action coverage unavailable')
    coverage_start=pd.Timestamp(min(stamps),unit='s',tz='UTC').tz_convert(timezone).date().isoformat()
    # Quote currency is not proof of dividend currency on secondary listings.
    confirmed_currency=currency if meta.get('exchangeName') in ('NMS','NYQ','NGM','NCM','ASE','PCX') and currency=='USD' else None
    for event in events:event['currency']=confirmed_currency;event['quote_currency']=currency
    return {'events':sorted(events,key=lambda e:e['date']),'start':coverage_start,'end':pd.Timestamp(end,unit='s',tz='UTC').tz_convert(timezone).date().isoformat(),'source':'Yahoo corporate actions','symbol':symbol,'currency':confirmed_currency,'complete':confirmed_currency is not None,'fetched_at':end}
