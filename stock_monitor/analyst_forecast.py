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
    return 'STALE' if packet.get('errors') or (now-stamp).total_seconds()>=TTL else 'AVAILABLE'

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

def resolve_profile(info,cache_dir,previous=None,now=None):
    """Auxiliary binding only; never alter the user's listing/source settings."""
    now=now or datetime.now(timezone.utc);previous=previous or {}
    old=previous.get('resolved_profile') or {};at=moment(old.get('resolved_at'))
    if (old.get('status')=='CONFIRMED' and old.get('isin')==info.get('isin') and old.get('symbol')
            and at is not None and 0<=(now-at).total_seconds()<7*TTL):return dict(old)
    from market import search,resolve_symbol,valid_isin
    isin=info.get('isin')
    if not valid_isin(isin):raise ValueError('PROFILE_ISIN_INVALID')
    candidates=search(isin)
    candidates=[q for q in candidates if q.get('quoteType')=='EQUITY' and q.get('symbol')]
    candidates.sort(key=lambda q:q.get('currency')!=info.get('currency'))
    # Exact symbol/listing/ISIN proof, bounded to two candidate checks per turn.
    for item in candidates[:2]:
        result=resolve_symbol(item['symbol'],cache_dir,search_result=item)
        if result.get('status')=='CONFIRMED' and result.get('isin')==isin and result.get('symbol')==item['symbol']:
            return dict(result,resolved_at=now.isoformat())
    raise ValueError('PROFILE_IDENTITY_UNCONFIRMED')

def fetch(info,cache_dir,previous=None,ticker=None,now=None):
    """Run only in the existing Dispatcher; four bounded auxiliary provider reads."""
    now=now or datetime.now(timezone.utc);stamp=now.isoformat()
    symbol=info.get('profile_symbol') or info.get('history_symbol')
    old=previous if matches(previous,info) else {}
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
            if raw.get('symbol')!=symbol:raise ValueError('IDENTITY_MISMATCH')
        except Exception as error:
            packet['errors']['profile']=type(error).__name__
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
                packet['errors'][key]=type(error).__name__
                if type(error).__name__=='YFRateLimitError':
                    packet['rate_limited']=True;break
            else:
                packet[key]=value;packet[key+'_at']=stamp;successes.append(key)
        if successes:packet['updated_at']=stamp
        return packet
    finally:
        if session is not None:session.close()

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

def load(app,info,refresh=False):
    packet=saved(app.store,info)
    if refresh and due(packet,info):
        packet=fetch(info,app.store.directory/'provider_cache',packet)
        current=next((dict(r) for r in app.watchlist if r.get('isin')==info.get('isin')),None)
        if current is not None and identity(current)==identity(info):
            app.store.set_setting(PREFIX+info['isin'],packet)
        else:return {}
    return packet
