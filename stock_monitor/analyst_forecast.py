"""Auxiliary analyst forecasts: provider currency, bounded cache, no execution hooks."""
from datetime import datetime, timezone, timedelta
import math
from collections.abc import Mapping
import re

PREFIX='analyst_forecast:'
TTL=86400
RETRY=900
RATING_KEYS=('strongBuy','buy','hold','sell','strongSell')
IDENTITY=('isin','profile_symbol','history_symbol','venue','currency','kind')
SOURCE='Yahoo Finance / yfinance'


def identity(info):return {key:info.get(key) for key in IDENTITY}

def matches(packet,info):return isinstance(packet,dict) and packet.get('identity')==identity(info)

def number(raw,positive=False):
    if isinstance(raw,bool) or not isinstance(raw,(int,float)):return None
    return float(raw) if math.isfinite(raw) and (not positive or raw>0) else None

def moment(raw):
    try:
        dt=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError,ValueError,OverflowError):return None

def due(packet,info,now=None):
    now=now or datetime.now(timezone.utc)
    if not matches(packet,info):return True
    attempted=moment(packet.get('attempted_at'))
    if attempted is not None and 0<=(now-attempted).total_seconds()<RETRY:return False
    stamp=moment(packet.get('updated_at'))
    return bool(packet.get('errors')) or stamp is None or not 0<=(now-stamp).total_seconds()<TTL

def state(packet,now=None):
    now=now or datetime.now(timezone.utc)
    stamp=moment(packet.get('updated_at'))
    if stamp is None:return 'UNAVAILABLE'
    if stamp>now:return 'FUTURE_TIMESTAMP'
    if (now-stamp).total_seconds()>=TTL:return 'STALE'
    return 'PARTIAL' if packet.get('errors') else 'AVAILABLE'

def targets(info):
    fields={'low':'targetLowPrice','mean':'targetMeanPrice','high':'targetHighPrice'}
    values={key:number(info.get(field),True) for key,field in fields.items()}
    current=number(info.get('currentPrice'),True) or number(info.get('regularMarketPrice'),True)
    currency=info.get('currency')
    if not isinstance(currency,str) or not re.fullmatch('[A-Za-z]{3}',currency):currency=None
    # A financial-reporting currency is never the currency of a quote/price target.
    raw_count=number(info.get('numberOfAnalystOpinions'))
    count=int(raw_count) if raw_count is not None and raw_count>=0 and raw_count.is_integer() else None
    present=[values[k] for k in ('low','mean','high') if values[k] is not None]
    ordered=all(a<=b for a,b in zip(present,present[1:]))
    if not ordered:values={key:None for key in values}
    return dict(**values,current=current,currency=currency,analysts=count,
                status='TARGET_RANGE_INVALID' if not ordered else 'CURRENCY_UNKNOWN' if not currency else 'AVAILABLE' if present else 'UNAVAILABLE',
                quote_at=info.get('regularMarketTime'),horizon_months=12,exact_target_date=None)

def ratings(frame):
    if frame is None or getattr(frame,'empty',True):return None
    # Periods overlap: never sum 0m/-1m/-2m as different analysts.
    rows=frame.to_dict('records')
    row=next((r for r in rows[:12] if r.get('period')=='0m'),None)
    if row is None:return None
    counts=[]
    for key in RATING_KEYS:
        raw=number(row.get(key))
        if raw is None or raw<0 or not raw.is_integer():return None
        counts.append(int(raw))
    total=sum(counts)
    return dict(period='0m',counts=counts,total=total,
                score=sum(n*w for n,w in zip(counts,(1,.5,0,-.5,-1)))/total if total else None)

def decisions(frame,now=None):
    if frame is None or getattr(frame,'empty',True):return []
    now=now or datetime.now(timezone.utc);rows=[]
    for at,row in frame.sort_index(ascending=False).head(100).iterrows():
        stamp=moment(at.isoformat())
        if stamp is None or stamp>now:continue
        rows.append(dict(at=stamp.isoformat(),firm=str(row.get('Firm') or '')[:180],
             analyst=str(row.get('Analyst') or '')[:120] if isinstance(row.get('Analyst'),str) else None,
             action=str(row.get('Action') or '')[:50],from_grade=str(row.get('FromGrade') or '')[:80],
             to_grade=str(row.get('ToGrade') or '')[:80],
             target_before=number(row.get('priorPriceTarget'),True),target_after=number(row.get('currentPriceTarget'),True),
             target_action=str(row.get('priceTargetAction') or '')[:50],
             # Yahoo upgrade history does not supply a dependable target currency.
             target_currency=None))
    return rows

def history(frame,metadata,symbol,currency):
    if not isinstance(metadata,Mapping) or metadata.get('symbol')!=symbol or metadata.get('currency')!=currency or not currency:
        return []
    if frame is None or getattr(frame,'empty',True) or 'Close' not in frame:return []
    rows={}
    for at,raw in frame['Close'].tail(550).items():
        value=number(raw,True)
        if value is not None:rows[at.date().isoformat()]=value
    return sorted(rows.items())

def _isin_symbols(isin):
    """Actual auxiliary symbols from the existing independent exact-ISIN source."""
    import requests
    import json
    response=requests.get('https://markets.businessinsider.com/ajax/SearchController_Suggest',
        params={'max_results':25,'query':isin},headers={'User-Agent':'Mozilla/5.0 StockMonitor/0.16'},timeout=8)
    response.raise_for_status()
    token=r'"(?:\\.|[^"\\])*"';symbols=[]
    for match in re.finditer(r'new Array\(\s*('+token+r'(?:\s*,\s*'+token+r'){5})\s*\)',response.text):
        row=json.loads('['+match.group(1)+']');parts=row[2].split('|')
        if row[1]=='Stocks' and len(parts)>=2 and parts[1]==isin and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.^=-]{0,24}',parts[0]):
            if parts[0] not in symbols:symbols.append(parts[0])
    return symbols[:2]

def _class_profile(info,item):
    """One US share-class separator only; independently prove listing and exact ISIN."""
    symbol=item.get('symbol','');match=re.fullmatch(r'([A-Z0-9]{1,6})-([A-Z])',symbol)
    if not match or not str(info.get('isin','')).startswith('US'):return None
    import requests
    from urllib.parse import quote
    from market import _exact_isins
    reference=match[1]+'.'+match[2];headers={'User-Agent':'Mozilla/5.0 StockMonitor/0.16'}
    try:
        response=requests.get('https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol,safe=''),
            params={'range':'1d','interval':'1d'},headers=headers,timeout=8);response.raise_for_status()
        payload=response.json().get('chart',{})
        if payload.get('error') or len(payload.get('result') or [])!=1:return None
        meta=payload['result'][0].get('meta',{})
        if meta.get('symbol')!=symbol or meta.get('instrumentType')!='EQUITY' or meta.get('exchangeName')!=item.get('exchange'):return None
        currency=meta.get('currency')
        if not isinstance(currency,str) or not re.fullmatch('[A-Za-z]{3}',currency) or item.get('currency') not in (None,currency):return None
        response=requests.get('https://markets.businessinsider.com/ajax/SearchController_Suggest',
            params={'max_results':25,'query':reference},headers=headers,timeout=8);response.raise_for_status()
        if _exact_isins(response.text,reference,'EQUITY')!=info['isin']:return None
    except Exception:return None
    return dict(status='CONFIRMED',isin=info['isin'],symbol=symbol,venue=meta['exchangeName'],currency=currency,
        source='proxy',timezone=meta.get('exchangeTimezoneName'),
        evidence={'listing':'Yahoo exact symbol/type/venue/currency','isin':'Business Insider exact reference symbol',
                  'class_separator':{'provider':symbol,'reference':reference,'rule':'same US share class; dot/dash separator only'},'checksum':True})

def _error(error):
    text=str(error)
    return text if isinstance(error,ValueError) and text in ('SOURCE_EMPTY','IDENTITY_MISMATCH') else type(error).__name__

def resolve_profile(info,cache_dir,previous=None,now=None,exclude_symbols=()):
    """Auxiliary binding only; never alter the user's listing/source settings."""
    now=now or datetime.now(timezone.utc);previous=previous or {}
    old=previous.get('resolved_profile') or {};at=moment(old.get('resolved_at'))
    if (old.get('status')=='CONFIRMED' and old.get('symbol') not in exclude_symbols and old.get('isin')==info.get('isin') and old.get('symbol')
            and at is not None and 0<=(now-at).total_seconds()<7*TTL):return dict(old)
    from market import search,resolve_symbol,valid_isin
    isin=info.get('isin')
    if not valid_isin(isin):raise ValueError('PROFILE_ISIN_INVALID')
    candidates=search(isin)
    candidates=[q for q in candidates if q.get('quoteType')=='EQUITY' and q.get('symbol') and q['symbol'] not in exclude_symbols]
    candidates.sort(key=lambda q:q.get('currency')!=info.get('currency'))
    attempted=set();different=[];checks=0
    def check(item):
        nonlocal checks
        checks+=1
        attempted.add(item['symbol'])
        result=resolve_symbol(item['symbol'],cache_dir,search_result=item)
        if result.get('status')=='CONFIRMED' and result.get('symbol')==item['symbol']:
            if result.get('isin')==isin:return dict(result,resolved_at=now.isoformat())
            different.append(result.get('isin'))
        if result.get('status')!='CONFIRMED':
            alias=_class_profile(info,item)
            if alias:return dict(alias,resolved_at=now.isoformat())
    for item in candidates[:2]:
        result=check(item)
        if result:return result
    # Some listings have no literal Yahoo ticker in BI's symbol search.
    # Reuse actual symbols from BI's exact-ISIN records; never invent an alias.
    if checks<2:
        for symbol in _isin_symbols(isin):
            if symbol in attempted or symbol in exclude_symbols:continue
            matches=[q for q in search(symbol) if q.get('symbol')==symbol and q.get('quoteType')=='EQUITY']
            if len(matches)!=1:continue
            result=check(matches[0])
            if result:return result
            if checks>=2:break
    if different:raise ValueError('PROFILE_ISIN_MISMATCH:'+isin+':'+','.join(sorted(set(different))))
    raise ValueError('PROFILE_IDENTITY_UNCONFIRMED')

def _fetch_bound(info,cache_dir,previous=None,ticker=None,now=None,symbol=None,binding=None):
    """Run only in the existing Dispatcher; four bounded auxiliary provider reads."""
    now=now or datetime.now(timezone.utc);stamp=now.isoformat()
    symbol=symbol or info.get('profile_symbol') or info.get('history_symbol')
    old=previous if matches(previous,info) else {}
    if old.get('symbol') not in (None,symbol):old={}
    packet=dict(old,identity=identity(info),symbol=symbol,source=SOURCE,attempted_at=stamp,errors={})
    if info.get('kind')=='benchmark':
        packet.update(errors={'identity':'NO_COMPANY_PROFILE'});return packet
    if not symbol:
        try:
            binding=resolve_profile(info,cache_dir,old,now)
        except Exception as error:
            packet['errors']['identity']=str(error) if isinstance(error,ValueError) else type(error).__name__
            return packet
        packet['resolved_profile']=binding;symbol=binding['symbol'];packet['symbol']=symbol
    if binding:packet['resolved_profile']=dict(binding)
    session=None
    if ticker is None:
        import yfinance as yf
        from curl_cffi.requests import Session
        yf.set_tz_cache_location(str(cache_dir))
        session=Session(impersonate='chrome',timeout=8)
        ticker=yf.Ticker(symbol,session=session)
    packet['source_url']='https://finance.yahoo.com/quote/'+symbol+'/analysis/'
    packet['rate_limited']=False
    successes=[]
    try:
        try:
            raw=ticker.get_info() or {}
            if not raw:raise ValueError('SOURCE_EMPTY')
            if raw.get('symbol')!=symbol:raise ValueError('IDENTITY_MISMATCH')
        except Exception as error:
            packet['errors']['profile']=_error(error)
            if type(error).__name__=='YFRateLimitError':packet['rate_limited']=True
            # No other new section can be bound to an unconfirmed company response.
            return packet
        packet['company']=str(raw.get('longName') or raw.get('shortName') or symbol)[:200]
        for key,loader in (
            ('targets',lambda:targets(raw)),
            ('ratings',lambda:ratings(ticker.get_recommendations())),
            ('decisions',lambda:decisions(ticker.get_upgrades_downgrades(),now)),
            ('history',lambda:history(ticker.history(period='2y',interval='1d',auto_adjust=False,actions=False,timeout=8),
                                      ticker.get_history_metadata(),symbol,raw.get('currency')))):
            try:
                value=loader()
                if value is None or value==[]:raise ValueError('SOURCE_EMPTY')
            except Exception as error:
                packet['errors'][key]=_error(error)
                if type(error).__name__=='YFRateLimitError':
                    packet['rate_limited']=True;break
            else:
                packet[key]=value;packet[key+'_at']=stamp;successes.append(key)
        if successes:packet['updated_at']=stamp
        return packet
    finally:
        if session is not None:session.close()

def fetch(info,cache_dir,previous=None,ticker=None,now=None):
    """Coherent provider snapshot; an alternative must independently prove the same ISIN."""
    now=now or datetime.now(timezone.utc);configured=info.get('profile_symbol') or info.get('history_symbol')
    old=previous if matches(previous,info) else {};binding=None
    if old.get('use_resolved_profile') and configured:
        try:binding=resolve_profile(info,cache_dir,old,now,exclude_symbols=(configured,))
        except Exception:binding=None
    packet=_fetch_bound(info,cache_dir,old,ticker,now,symbol=binding.get('symbol') if binding else None,binding=binding)
    if binding:packet.update(configured_symbol=configured,use_resolved_profile=True,switch_reason='ANALYST_COVERAGE_MISSING')
    missing=((packet.get('targets') or {}).get('mean') is None and not (packet.get('ratings') or {}).get('total'))
    if not configured or not missing or info.get('kind')=='benchmark' or packet.get('rate_limited') or packet.get('errors',{}).get('profile')=='IDENTITY_MISMATCH':return packet
    try:
        alternate=resolve_profile(info,cache_dir,old,now,exclude_symbols=(packet.get('symbol'),configured))
        alternative=_fetch_bound(info,cache_dir,None,None,now,symbol=alternate['symbol'],binding=alternate)
    except Exception as error:
        packet['coverage_note']=str(error) if isinstance(error,ValueError) else type(error).__name__
        return packet
    if ((alternative.get('targets') or {}).get('mean') is not None or (alternative.get('ratings') or {}).get('total')):
        alternative.update(configured_symbol=configured,use_resolved_profile=True,switch_reason='ANALYST_COVERAGE_MISSING',
            source_attempts=[dict(symbol=packet.get('symbol'),at=packet.get('attempted_at'),errors=packet.get('errors'))])
        return alternative
    packet['coverage_note']='NO_ANALYST_COVERAGE_FOR_CONFIRMED_ALTERNATIVE:'+alternate['symbol']
    return packet

def presentation_key(packet,now=None):
    """Run off Tk: skip unchanged paints while still observing each real deadline."""
    import json,hashlib
    now=now or datetime.now(timezone.utc);sections=[]
    for key in ('targets','ratings','decisions','history'):
        stamp=moment(packet.get(key+'_at'))
        status=('UNAVAILABLE' if stamp is None else 'FUTURE_TIMESTAMP' if stamp>now else
                'STALE' if key in packet.get('errors',{}) or (now-stamp).total_seconds()>=TTL else 'AVAILABLE')
        sections.append(status)
    digest=hashlib.sha256(json.dumps(packet,ensure_ascii=True,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return digest,state(packet,now),tuple(sections)

def saved(store,info):
    value=store.setting(PREFIX+info.get('isin',''),{})
    if matches(value,info):return value
    # Reuse a saved card without pretending it was fetched again today.
    card=store.setting('card:'+info.get('isin',''),{});symbol=info.get('profile_symbol') or info.get('history_symbol')
    if not isinstance(card,dict) or card.get('symbol')!=symbol or not symbol:return {}
    raw=card.get('overview');stamp=moment(card.get('fetched_at'))
    if not isinstance(raw,dict) or raw.get('symbol')!=symbol or stamp is None or stamp>datetime.now(timezone.utc):return {}
    return dict(identity=identity(info),symbol=symbol,source=card.get('source') or SOURCE,
                targets=targets(raw),targets_at=stamp.isoformat(),updated_at=stamp.isoformat(),
                errors={},legacy_card=True)

def load(app,info,refresh=False,force=False):
    packet=saved(app.store,info)
    attempted=moment(packet.get('attempted_at'))
    may_force=attempted is None or (datetime.now(timezone.utc)-attempted).total_seconds()>=RETRY
    if refresh and (due(packet,info) or force and may_force):
        packet=fetch(info,app.store.directory/'provider_cache',packet)
        current=next((dict(r) for r in app.watchlist if r.get('isin')==info.get('isin')),None)
        if current is not None and identity(current)==identity(info):
            app.store.set_setting(PREFIX+info['isin'],packet)
        else:return {}
    return packet
