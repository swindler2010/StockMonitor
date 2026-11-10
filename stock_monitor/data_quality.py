"""Listing-scoped OHLCV audit and bounded, atomic repair. Never substitutes listings."""
from functools import lru_cache
from bisect import bisect_left,bisect_right
import json
import numpy as np
import pandas as pd

IDENTITY=('source','venue','currency','symbol','timezone')
FIELDS=('open','high','low','close','volume')
INTERVAL_SCOPE={'DE0005140008','DE0007236101','DE0007164600','DE0008404005','US0378331005'}

# Exchange announcement p108, not a missing-row inference or a TV licence.
# Equities only: Baader is their sole GETTEX market maker (official GETTEX FAQ).
GETTEX_EQUITY_INTERRUPTION={
    'id':'GETTEX-EQUITIES-20261026', 'start':'2026-10-26 07:30:00',
    'end':'2026-10-26 09:55:00', 'asset_class':'EQUITY', 'market_maker':'Baader Bank AG',
    'reference':'https://www.gettex.de/fileadmin/bulletin/bekanntmachungen.gtx.20261026.pdf#page=108',
    'scope_reference':'https://www.gettex.de/en/about-gettex/faqs/',
    'basis':'official_exchange_trading_interruption'}

def market_closed_proof(meta,start,end,now=None):
    """Curated exchange notice, exact post-trade identity, whole interval only.

    Native vendor chart timestamps are NOT execution timestamps; this notice
    must not be transferred to gettex_chart/proxy or used to repair their bars.
    """
    proof=meta.get('absence_evidence',{})
    if not proof.get('exchange_closures') or str(start)[:10]!='2026-10-26':return None
    if (meta.get('source')!='gettex' or meta.get('venue')!='GETTEX' or meta.get('currency')!='EUR'
        or meta.get('timezone')!='Europe/Berlin' or proof.get('isin')!=meta.get('symbol')
        or not all(proof.get('identity',{}).get(k)==meta.get(k) and meta.get(k) for k in IDENTITY)):
        return None
    try:
        a,b=pd.Timestamp(start),pd.Timestamp(end);clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
        checked=pd.Timestamp(proof.get('closure_checked_at'))
        if a.tzinfo is not None or b.tzinfo is not None or a>=b or checked.tzinfo is None or checked>clock:return None
        for entry in proof.get('exchange_closures',[]):
            if entry!=GETTEX_EQUITY_INTERRUPTION:continue
            left,right=pd.Timestamp(entry['start']),pd.Timestamp(entry['end'])
            if checked<right.tz_localize(meta['timezone']).tz_convert('UTC'):continue
            # The notice is minute-granular; saved/TV boundary publications
            # differ by one native bucket. Do not reinterpret or invalidate
            # boundary bars until execution-time semantics are confirmed.
            if left<a<b<right:return dict(entry,checked_at=checked.isoformat(),
                boundary_policy='Strict interior only; notice boundary buckets require separate source evidence')
    except (ValueError,TypeError,KeyError):pass
    return None

def closure_conflict(meta,start,end,now=None):
    """A positive or unresolved publication must not be waived by a notice."""
    return _closure_conflict(meta,start,end,now,_evidence_lookup(meta))

def _closure_conflict(meta,start,end,now,index):
    if _known_trade(meta,start,end,now,index):return True
    try:
        start,end=pd.Timestamp(start),pd.Timestamp(end)
        for field in ('trade_intervals','zero_quantity_intervals'):
            _,starts,ends=index(field)
            last=bisect_left(starts,end)-1
            if last>=0 and ends[last]>start:return True
        return False
    except (ValueError,TypeError):return True

@lru_cache(maxsize=32)
def _proof_ranges(values):
    return tuple(sorted((pd.Timestamp(a),pd.Timestamp(b)) for a,b in values))

@lru_cache(maxsize=32)
def _proof_index(values):
    """Index the existing immutable evidence ranges, without classifying data."""
    intervals=_proof_ranges(values);ends=[]
    for _,end in intervals:ends.append(max(ends[-1],end) if ends else end)
    return intervals,tuple(a for a,_ in intervals),tuple(ends)

def _evidence_lookup(meta):
    """Prepare immutable range keys once per synchronous batch, never per bar.

    Only geometry is retained. No decision, identity or checked_at is cached;
    the next call rebuilds from the current evidence, including in-place edits.
    Lazy fields preserve fail-closed handling without requiring irrelevant data.
    """
    proof=meta.get('absence_evidence',{});indexes={}
    def index(field):
        if field not in indexes:
            result=_proof_index(tuple(map(tuple,proof.get(field,[]))))
            if any(pd.isna(a) or pd.isna(b) for a,b in result[0]):
                raise ValueError('Invalid evidence timestamp')
            indexes[field]=result
        return indexes[field]
    return index

def attach_interval_evidence(store,frame,isin):
    """Scoped operational evidence, tracked by the existing setting revisions."""
    if isin and all(frame.attrs.get(k) for k in IDENTITY):
        frame.attrs['classify_absence']=True
        proof=store.setting('interval_evidence:'+isin,{})
        if proof.get('isin')==isin and all(proof.get('identity',{}).get(k)==frame.attrs.get(k) for k in IDENTITY):
            frame.attrs['absence_evidence']=proof
    return frame

def no_trades_proof(meta,start,end,now=None):
    """Cover the entire half-open interval with explicit, identity-bound evidence."""
    return _no_trades_proof(meta,start,end,now,_evidence_lookup(meta))

def _no_trades_proof(meta,start,end,now,index):
    proof=meta.get('absence_evidence',{})
    if not all(proof.get('identity',{}).get(k)==meta.get(k) and meta.get(k) for k in IDENTITY):return None
    if proof.get('basis')!='complete_published_trade_files' or not proof.get('reference') or not proof.get('checked_at'):return None
    try:
        clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
        checked=pd.Timestamp(proof['checked_at'])
        if checked.tzinfo is None or checked>clock:return None
        cursor=pd.Timestamp(start);end=pd.Timestamp(end)
        if end.tz_localize(meta['timezone']).tz_convert('UTC')>checked:return None
        trades,starts,ends=index('trade_intervals')
        last=bisect_left(starts,end)-1
        if last>=0 and ends[last]>cursor:return None
        # An unresolved zero-quantity record cannot certify a fully empty
        # interval, even if a contradictory absence range was also saved.
        zero,starts,ends=index('zero_quantity_intervals')
        last=bisect_left(starts,end)-1
        if last>=0 and ends[last]>cursor:return None
        intervals,_,ends=index('no_trades')
        # Prefix maxima preserve overlapping/nested evidence. Skip only ranges
        # whose right edge provably cannot cover this cursor.
        begin=bisect_right(ends,cursor) if cursor<end else 0
        for i in range(begin,len(intervals)):
            left,right=intervals[i]
            if left>cursor:break
            if left<=cursor<right:cursor=right
            if cursor>=end:return {'reference':proof['reference'],'checked_at':proof['checked_at'],'basis':proof['basis']}
    except (ValueError,TypeError,KeyError):return None
    return None

def interval_status(meta,start,end,now=None,published_trade=False):
    """No empty-response inference. Calendar and publication evidence are separate."""
    from timeframes import schedule
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    start,end=pd.Timestamp(start),pd.Timestamp(end)
    result=dict(start=str(start),end=str(end),checked_at=now.isoformat(),identity={k:meta.get(k) for k in IDENTITY})
    try:
        a=start.tz_localize(meta['timezone']).tz_convert('UTC');b=end.tz_localize(meta['timezone']).tz_convert('UTC')
        sessions=schedule(meta,start,end)
        active=[row for _,row in sessions.iterrows() if row.open<b and row.close>a and not (pd.notna(row.break_start) and a>=row.break_start and b<=row.break_end)]
        if not active:return dict(result,state='MARKET_CLOSED',reason='Verified venue calendar; no candle expected',reference='calendar:'+meta['venue'])
        row=active[0];b=min(b,row.close)
        if pd.notna(row.break_start) and a<row.break_start:b=min(b,row.break_start)
        end=b.tz_convert(meta['timezone']).tz_localize(None);result['end']=str(end)
        if b>now:return dict(result,state='PENDING',reason='Interval has not closed')
        if b+pd.Timedelta(minutes=15)>now:return dict(result,state='DELAYED',reason='Existing 15-minute publication grace; actual provider delay unconfirmed')
        closure=market_closed_proof(meta,start,end,now)
        if closure:
            if published_trade or closure_conflict(meta,start,end,now):
                return dict(result,state='UNCONFIRMED',reason='EXCHANGE_NOTICE_CONFLICT: publication during official interruption',evidence=closure)
            return dict(result,state='MARKET_CLOSED',reason='Official exchange trading interruption; no candle expected',evidence=closure)
        if published_trade:return dict(result,state='GAP',reason='Published trade exists but candle is absent')
        proof=no_trades_proof(meta,start,end,now)
        if proof:return dict(result,state='NO_TRADES',reason='No matching trades in the verified complete published file set',evidence=proof)
    except (ValueError,KeyError,TypeError) as error:
        return dict(result,state='UNCONFIRMED',reason='Calendar/evidence unavailable: '+type(error).__name__)
    return dict(result,state='UNCONFIRMED',reason='No complete trade-publication evidence; empty data alone proves nothing')

def known_trade(meta,start,end,now=None):
    return _known_trade(meta,start,end,now,_evidence_lookup(meta))

def _known_trade(meta,start,end,now,index):
    proof=meta.get('absence_evidence',{})
    try:
        checked=pd.Timestamp(proof.get('checked_at'))
        if (proof.get('basis')!='complete_published_trade_files' or not proof.get('reference') or checked.tzinfo is None or
            checked>pd.Timestamp(now or pd.Timestamp.now(tz='UTC')) or
            not all(proof.get('identity',{}).get(k)==meta.get(k) and meta.get(k) for k in IDENTITY)):return False
        start,end=pd.Timestamp(start),pd.Timestamp(end)
        intervals,starts,_=index('trade_intervals')
        # Only intervals starting inside the requested bucket can be wholly
        # contained in it. Reuse the existing sorted evidence index; retain the
        # endpoint, publication-time and identity checks without scanning the
        # complete trade archive for every absent candle.
        left=bisect_left(starts,start);right=bisect_left(starts,end)
        return any(start<=a<b<=end and b.tz_localize(meta['timezone']).tz_convert('UTC')<=checked
                   for a,b in intervals[left:right])
    except (ValueError,TypeError):return False

def covered_empty(meta,stamps,base,now=None,*,closed_only=False):
    """Only proven empty intervals can be removed from expectations."""
    if not meta.get('classify_absence') or not meta.get('absence_evidence') or base not in STEPS:return pd.DatetimeIndex([])
    from timeframes import schedule
    stamps=pd.DatetimeIndex(stamps)
    if stamps.empty:return stamps
    sessions=schedule(meta,stamps.min(),stamps.max())
    # Each day's closing instant is invariant throughout this audit. Avoid a
    # pandas row/column lookup per candle without changing calendar boundaries.
    closing_by_day={day:close.tz_convert(meta['timezone']).tz_localize(None)
                    for day,close in zip(sessions.index,sessions.close)}
    opening_by_day={day:opening.tz_convert(meta['timezone']).tz_localize(None)
                    for day,opening in zip(sessions.index,sessions.open)}
    found=[];index=_evidence_lookup(meta)
    for stamp in stamps:
        day=stamp.normalize()
        closing=closing_by_day.get(day)
        if closing is None:continue
        # A daily label denotes the exchange session, not trading at midnight.
        # Only complete same-source proof for that whole session may waive it.
        begin=opening_by_day[day] if base=='1D' else stamp
        end=min(stamp+STEPS[base],closing)
        closure=market_closed_proof(meta,begin,end,now)
        if closure and not _closure_conflict(meta,begin,end,now,index):found.append(stamp)
        elif not closed_only and _no_trades_proof(meta,begin,end,now,index):found.append(stamp)
    return pd.DatetimeIndex(found)

def absence_report_resolved(previous,frame,base,now=None):
    """Retire only a prior missing-bar diagnosis wholly covered by new evidence."""
    if previous.get('state') not in ('SOURCE_GAP','SOURCE_UNAVAILABLE') or not previous.get('ranges'):return False
    if not previous.get('issues') or any(not str(x).startswith('Пропущенные свечи:') for x in previous['issues']):return False
    if any(previous.get('identity',{}).get(k)!=frame.attrs.get(k) for k in IDENTITY):return False
    try:
        for first,last in previous['ranges']:
            expected=expected_index(frame.attrs,base,first,last,now)
            if not len(expected) or len(covered_empty(frame.attrs,expected,base,now))!=len(expected):return False
    except (ValueError,TypeError,KeyError):return False
    return True
CALENDARS={'NMS':'XNAS','NGM':'XNAS','NCM':'XNAS','NYQ':'XNYS','PCX':'XNYS',
           'ASE':'XNYS','BTS':'XNYS','GER':'XETR','FRA':'XFRA','PAR':'XPAR','LSE':'XLON','GETTEX':'GETTEX',
           'SNP':'XNYS','NIM':'XNAS','ZRH':'STOXX_EUROPE','CXI':'CBOE_VIX'}
TIMEZONES={'XNAS':'America/New_York','XNYS':'America/New_York',
           'XETR':'Europe/Berlin','XFRA':'Europe/Berlin','XPAR':'Europe/Paris','XLON':'Europe/London','GETTEX':'Europe/Berlin',
           'STOXX_EUROPE':'Europe/Zurich','CBOE_VIX':'America/Chicago'}
STEPS={'1D':pd.Timedelta(days=1),**{tf:pd.Timedelta(minutes=m) for tf,m in {'1m':1,'5m':5,'10m':10,'15m':15,'30m':30,'1H':60,'2H':120,'3H':180,'4H':240}.items()}}
XFRA_SESSION_REVISION='equities-2021-11-29-v1'
GETTEX_DAILY_START='2015-01-19'

class QualityError(ValueError):
    def __init__(self, report):
        self.report=report
        super().__init__('; '.join(report['issues']) or report['state'])

def key(info,symbol,base):return 'data_quality:'+info['isin']+':'+symbol+':'+base

@lru_cache(maxsize=64)
def calendar(name,first_year,last_year,daily=False):
    if name=='STOXX_EUROPE':
        # STOXX dissemination calendar, not the Swiss stock-exchange calendar.
        # stoxx.com/web/stoxxcom/res-dissemination
        from types import SimpleNamespace
        from dateutil.easter import easter
        holidays=[]
        for year in range(first_year,last_year+1):
            e=pd.Timestamp(easter(year))
            holidays.extend([pd.Timestamp(year,1,1),e-pd.Timedelta(days=2),e+pd.Timedelta(days=1),pd.Timestamp(year,12,25),pd.Timestamp(year,12,26)])
        dates=pd.bdate_range(f'{first_year}-01-01',f'{last_year}-12-31').difference(pd.DatetimeIndex(holidays))
        opens=(dates+pd.Timedelta(hours=9)).tz_localize('Europe/Zurich').tz_convert('UTC')
        closes=(dates+pd.Timedelta(hours=18)).tz_localize('Europe/Zurich').tz_convert('UTC')
        return SimpleNamespace(schedule=pd.DataFrame({'open':opens,'close':closes,'break_start':pd.NaT,'break_end':pd.NaT},index=dates))
    if name=='CBOE_VIX':
        from types import SimpleNamespace
        from dateutil.easter import easter
        # The official VIX EOD file includes Global Trading Hours observations
        # on several U.S. equity holidays; XNYS alone falsely rejects those rows.
        holidays=[]
        for year in range(first_year,last_year+1):
            e=pd.Timestamp(easter(year))
            holidays.extend([pd.Timestamp(year,1,1),e-pd.Timedelta(days=2),pd.Timestamp(year,12,25)])
        dates=pd.bdate_range(f'{first_year}-01-01',f'{last_year}-12-31').difference(pd.DatetimeIndex(holidays))
        opens=(dates+pd.Timedelta(hours=8,minutes=30)).tz_localize('America/Chicago').tz_convert('UTC')
        closes=(dates+pd.Timedelta(hours=15,minutes=15)).tz_localize('America/Chicago').tz_convert('UTC')
        return SimpleNamespace(schedule=pd.DataFrame({'open':opens,'close':closes,'break_start':pd.NaT,'break_end':pd.NaT},index=dates))
    if name=='GETTEX':
        from types import SimpleNamespace
        # Venue's published calendars, NOT Xetra. 2021/22/23: gettex.de/fileadmin/
        # bulletin/bekanntmachungen_gettex_{20211125,20221202,20230609}.pdf,
        # pages 57/87/87. Only DAILY coverage is extended; historical intraday
        # hours are deliberately not inferred. 2024: Trading_Calendar_2024_
        # Boerse_Muenchen_and_gettex.pdf; 2025: bekanntmachungen.gtx.20241219.pdf
        # p135; 2026: Handelskalender_gettex_Boerse_Muenchen_2026.pdf (gettex.de).
        holidays=['2026-01-01','2026-04-03','2026-04-06','2026-05-01','2026-12-24','2026-12-25','2026-12-31']
        if daily:
            # Official GETTEX/Munich bulletins: 20150508 p17 (GETTEX-specific
            # Whit Monday correction), 20161215 p23, 20170818 p29,
            # 20181205 p22 (corrected calendar), 20191220 p48, 20200714.
            # Exchange launch: issuer press release22Jan2015, live since Mon19.
            # Evidence and exact URLs: history64-evidence/all-history02/calendars.
            holidays+=['2015-01-01','2015-04-03','2015-04-06','2015-05-01','2015-12-24','2015-12-25','2015-12-31',
                       '2016-01-01','2016-03-25','2016-03-28','2016-12-26',
                       '2017-04-14','2017-04-17','2017-05-01','2017-06-05','2017-10-03','2017-10-31','2017-12-25','2017-12-26',
                       '2018-01-01','2018-03-30','2018-04-02','2018-05-01','2018-05-21','2018-10-03','2018-12-24','2018-12-25','2018-12-26','2018-12-31',
                       '2019-01-01','2019-04-19','2019-04-22','2019-05-01','2019-06-10','2019-10-03','2019-12-24','2019-12-25','2019-12-26','2019-12-31',
                       '2020-01-01','2020-04-10','2020-04-13','2020-05-01','2020-06-01','2020-12-24','2020-12-25','2020-12-31']
            holidays+=['2021-01-01','2021-04-02','2021-04-05','2021-05-24','2021-12-24','2021-12-31',
                       '2022-04-15','2022-04-18','2022-12-26',
                       '2023-04-07','2023-04-10','2023-05-01','2023-12-25','2023-12-26',
                       '2024-01-01','2024-03-29','2024-04-01','2024-05-01','2024-12-24','2024-12-25','2024-12-26','2024-12-31',
                       '2025-01-01','2025-04-18','2025-04-21','2025-05-01','2025-12-24','2025-12-25','2025-12-26','2025-12-31']
        dates=pd.bdate_range(GETTEX_DAILY_START if daily else '2026-01-01','2026-12-31').difference(pd.to_datetime(holidays))
        opens=(dates+pd.Timedelta(hours=7,minutes=30)).tz_localize('Europe/Berlin').tz_convert('UTC')
        closes=(dates+pd.Timedelta(hours=23)).tz_localize('Europe/Berlin').tz_convert('UTC')
        if daily:
            # 2025 extension was phased by instrument. Historical EOD bars use
            # a conservative END-OF-LOCAL-DAY availability bound, not invented
            # intraday hours. Never use this schedule to build intraday slots.
            old=dates.year<2026
            opens=opens.where(~old,(dates+pd.Timedelta(0)).tz_localize('Europe/Berlin').tz_convert('UTC'))
            closes=closes.where(~old,(dates+pd.Timedelta(days=1)).tz_localize('Europe/Berlin').tz_convert('UTC'))
        return SimpleNamespace(schedule=pd.DataFrame({'open':opens,'close':closes,'break_start':pd.NaT,'break_end':pd.NaT},index=dates))
    import exchange_calendars as xcals
    cal=xcals.get_calendar(name,start=f'{first_year}-01-01',end=f'{last_year}-12-31',side='left')
    if name=='XFRA':
        # exchange_calendars uses 09:00–17:30 for XFRA as well as XETR.
        # FWB circular 080/2021 confirms equities/ETPs 08:00–22:00 from
        # 2021-11-29, public holidays (Germany/Hesse) 08:00–20:00.
        # Preserve its session dates and special year-end early closes. Older
        # hours are not inferred from this modern announcement.
        # cashmarket.deutsche-boerse.com/.../FWB-Management-Board-2841560
        from types import SimpleNamespace
        from dateutil.easter import easter
        sessions=cal.schedule.copy();dates=sessions.index
        modern=dates>=pd.Timestamp('2021-11-29')
        local_close=sessions.close.dt.tz_convert('Europe/Berlin')
        special=local_close.dt.hour*60+local_close.dt.minute<17*60+30
        holidays=[]
        for year in range(first_year,last_year+1):
            e=pd.Timestamp(easter(year))
            holidays.extend([e+pd.Timedelta(days=n) for n in (39,50,60)])
            holidays.append(pd.Timestamp(year,10,3))
        sessions.loc[modern,'open']=(dates[modern]+pd.Timedelta(hours=8)).tz_localize('Europe/Berlin').tz_convert('UTC')
        regular=modern & ~special.to_numpy()
        hours=np.where(dates[regular].isin(holidays),20,22)
        sessions.loc[regular,'close']=(dates[regular]+pd.to_timedelta(hours,unit='h')).tz_localize('Europe/Berlin').tz_convert('UTC')
        return SimpleNamespace(schedule=sessions)
    # pandas' default HolidayCalendar starts in 1970. Explicit historical
    # rules exist, but CustomBusinessDay omits them before that default.
    if first_year<1970 and cal.regular_holidays is not None:
        from types import SimpleNamespace
        holidays=cal.regular_holidays.holidays(f'{first_year}-01-01','1969-12-31')
        return SimpleNamespace(schedule=cal.schedule.loc[~cal.schedule.index.isin(holidays)])
    return cal

@lru_cache(maxsize=16)
def _intraday_grid(name,zone,base,first_day,last_day,first_year,last_year):
    """Calendar geometry only; no DQ/evidence/clock decision is cached."""
    schedule=calendar(name,first_year,last_year).schedule.loc[first_day:last_day]
    values=[];ready=[];step=STEPS[base]
    for _,row in schedule.iterrows():
        segments=[(row.open,row.close)]
        if pd.notna(row.break_start):segments=[(row.open,row.break_start),(row.break_end,row.close)]
        for opening,closing in segments:
            stamps=pd.date_range(opening,closing,freq=step,inclusive='left')
            ends=(stamps+step).where(stamps+step<closing,closing)+pd.Timedelta(minutes=15)
            values.extend(stamps.tz_convert(zone).tz_localize(None))
            ready.extend(ends.as_unit('ns').asi8)
    return pd.DatetimeIndex(values),np.asarray(ready,dtype='int64')


def expected_index(meta,base,first,last,now=None):
    """Closed bars only; daily dates are listing-local session labels."""
    name=CALENDARS.get(meta.get('venue'))
    if not name:raise ValueError('Нет проверенного календаря для площадки '+str(meta.get('venue')))
    zone=TIMEZONES[name]
    if meta.get('timezone')!=zone:raise ValueError('Timezone не совпадает с площадкой')
    first=pd.Timestamp(first);last=pd.Timestamp(last)
    daily=base in ('1D','1W','1M')
    native=meta.get('native_monthly',{})
    if base=='1M' and first.year<2021 and native.get('version')==1:
        # Native provider MONTHS are calendar periods, not an inferred set of
        # historical exchange sessions. Only past, complete calendar months;
        # current/2021+ periods still use the verified venue calendar below.
        ident={k:meta.get(k) for k in IDENTITY}
        if (native.get('identity')!=ident or ident.get('source')!='gettex_chart'
                or ident.get('venue')!='GETTEX' or ident.get('currency')!='EUR'
                or native.get('samples')!='M' or not native.get('receipt_sha256')
                or first<pd.Timestamp(native['first'])):
            raise ValueError('Native monthly provenance unconfirmed')
        clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
        prefix=pd.date_range(first,min(last,pd.Timestamp('2020-12-01')),freq='MS')
        ends=(prefix+pd.offsets.MonthBegin(1)).tz_localize(zone).tz_convert('UTC')
        prefix=prefix[ends+pd.Timedelta(minutes=30)<=clock]
        tail=expected_index(meta,base,max(first,pd.Timestamp('2021-01-01')),last,clock) if last.year>=2021 else pd.DatetimeIndex([])
        return prefix.append(tail)
    if name=='GETTEX' and base in ('1W','1M') and first.year<2021:
        # An expanded DAILY calendar does not prove native weekly/monthly
        # provenance. Only the existing daily aggregation path may use it;
        # native monthly receipts retain their separate strict gate above.
        if (meta.get('aggregation_base')!='1D' or
            meta.get('alignment')!='verified calendar period coverage' or not meta.get('bar_ends')):
            raise ValueError('Historical derived GETTEX provenance unconfirmed')
    if name=='GETTEX' and not ((int(GETTEX_DAILY_START[:4]) if daily else 2026)<=first.year<=last.year<=2026):
        raise ValueError('Исторический календарь GETTEX для этого TF ещё не подтверждён')
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    if now.tzinfo is None:raise ValueError('Время проверки должно иметь timezone')
    cal=calendar(name,min(first.year,now.year)-1,max(last.year,now.year)+1,daily=daily and name=='GETTEX')
    schedule=cal.schedule.loc[str(first.date()):str(last.date())]
    if base=='1D':
        return pd.DatetimeIndex(schedule.index[schedule.close+pd.Timedelta(minutes=30)<=now])
    if base in ('1W','1M'):
        # Calendar-period labels follow the existing pandas resampler. Read the
        # WHOLE bucket, not merely the first/last requested session date.
        freq='W-FRI' if base=='1W' else 'MS'
        lower=first-pd.Timedelta(days=6) if base=='1W' else first.replace(day=1)
        upper=last+pd.Timedelta(days=7) if base=='1W' else last+pd.offsets.MonthEnd(1)
        whole=cal.schedule.loc[str(lower.date()):str(upper.date())]
        labels=[]
        for stamp,part in whole.groupby(pd.Grouper(freq=freq)):
            if not part.empty and first<=stamp<=last and part.close.iloc[-1]+pd.Timedelta(minutes=30)<=now:labels.append(stamp)
        return pd.DatetimeIndex(labels)
    index,ready=_intraday_grid(name,zone,base,str(first.date()),str(last.date()),
                              min(first.year,now.year)-1,max(last.year,now.year)+1)
    # Re-evaluate closure/publication grace on EVERY read, including a cache hit.
    return index[(ready<=now.value)&(index>=first)&(index<=last)]

def ranges(stamps,expected):
    """Coalesce adjacent affected bars, not the healthy spans between them."""
    stamps=sorted(set(pd.Timestamp(x) for x in stamps))
    positions={v:n for n,v in enumerate(expected)};out=[]
    for stamp in stamps:
        if out and stamp in positions and out[-1][1] in positions and positions[stamp]==positions[out[-1][1]]+1:
            out[-1][1]=stamp
        else:out.append([stamp,stamp])
    return [[str(a),str(b)] for a,b in out]

def audit(frame,base,expected=None,now=None,check_stale=True):
    meta=dict(frame.attrs);issues=[];warnings=[];bad=[];global_invalid=False
    # Audit numeric views, not pandas' repeated deep copies of archive evidence.
    # Metadata is read from meta; neither the caller's rows nor attrs are changed.
    frame=pd.DataFrame(frame,copy=False)
    report={'state':'READY','issues':issues,'warnings':warnings,'ranges':[],
            'checked_at':pd.Timestamp.now(tz='UTC').isoformat(),'identity':{k:meta.get(k) for k in IDENTITY},'base_tf':base}
    if meta.get('intraday_normalization'):
        report['source_exclusions']=meta['intraday_normalization']
        warnings.extend('EXCLUDED_SOURCE '+k+': '+str(v) for k,v in meta['intraday_normalization'].get('excluded',{}).items())
    if meta.get('intraday_blockers'):
        issues.extend(meta['intraday_blockers']);global_invalid=True
    if frame.empty:
        report.update(state='INVALID' if global_invalid else 'SOURCE_UNAVAILABLE',issues=issues+['Источник не вернул пригодные закрытые свечи']);return report
    if meta.get('invalid_parent_provenance'):
        bad.extend(pd.Timestamp(x) for x in meta['invalid_parent_provenance'] if pd.Timestamp(x) in frame.index)
        if bad:issues.append('PARENT_PROVENANCE_CHANGED: повторная проверка исходных младших свечей обязательна')
    for field in IDENTITY:
        if not meta.get(field):issues.append('Не подтверждено '+field);global_invalid=True
        if expected and expected.get(field) and meta.get(field)!=expected[field]:
            issues.append('Изменилась идентичность: '+field);global_invalid=True
        if field in frame and not frame[field].eq(meta.get(field)).all():
            issues.append('Смешение рядов: '+field);global_invalid=True
    try:
        idx=pd.DatetimeIndex(frame.index)
        if idx.hasnans:raise ValueError('NaT')
        if idx.tz is not None:
            if str(idx.tz)!=meta['timezone']:raise ValueError('timezone индекса')
            idx=idx.tz_localize(None)
        idx.tz_localize(meta['timezone'],ambiguous='raise',nonexistent='raise')
    except Exception as error:
        report.update(state='INVALID',issues=issues+['Timezone/DST/timestamps: '+str(error)]);return report
    if idx.has_duplicates:issues.append('Дубликаты timestamps');bad.extend(idx[idx.duplicated(keep=False)])
    if not idx.is_monotonic_increasing:issues.append('Неправильный порядок timestamps');global_invalid=True
    try:
        values=frame[list(FIELDS)].astype(float)
        impossible=(~np.isfinite(values).all(axis=1))|(values[['open','high','low','close']]<=0).any(axis=1)|(values.volume<0)
        impossible|=(values.high<values[['open','low','close']].max(axis=1))|(values.low>values[['open','high','close']].min(axis=1))
        if impossible.any():issues.append('Невозможные OHLC/цены/объёмы');bad.extend(idx[impossible])
        if (values.volume==0).any():warnings.append('ZERO_VOLUME: '+str(int((values.volume==0).sum())))
        # Outliers are visible warnings, not silently corrected split-adjusted prices.
        median=values.volume.rolling(21,min_periods=10).median().replace(0,np.nan)
        if (values.volume>median*100).any():warnings.append('VOLUME_OUTLIER: требуется проверка источника')
    except Exception as error:issues.append('OHLCV: '+str(error));global_invalid=True
    now=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    try:
        span=meta.get('intraday_span') if base!='1D' else None
        first=min(idx.min(),pd.Timestamp(span[0])) if span else idx.min()
        last=max(idx.max(),pd.Timestamp(span[1])) if span else idx.max()
        grid=expected_index(meta,base,first,last,now)
        # Current forming bars are allowed but do not determine closed-bar readiness.
        all_grid=expected_index(meta,base,first,last,now+pd.Timedelta(days=40 if base in ('1W','1M') else 3))
        if base in ('1W','1M'):
            starts=all_grid-pd.Timedelta(days=6) if base=='1W' else all_grid
            all_grid=all_grid[starts<=now.tz_convert(meta['timezone']).tz_localize(None)]
        off=idx.difference(all_grid)
        # Yahoo appends a flat zero-volume quote exactly at session close.
        # Keep its provenance in diagnostics, but never treat it as an OHLCV bar.
        markers=[]
        if meta.get('source')=='proxy' and base!='1D' and len(off):
            name=CALENDARS.get(meta.get('venue'))
            cal=calendar(name,idx.min().year-1,idx.max().year+1)
            closes=pd.DatetimeIndex(cal.schedule.close).tz_convert(meta['timezone']).tz_localize(None)
            for stamp in off:
                rows=frame.loc[frame.index==stamp]
                if stamp in closes and len(rows)==1 and rows.volume.iloc[0]==0 and rows[['open','high','low','close']].iloc[0].nunique()==1:
                    markers.append(stamp)
            if markers:
                report['non_candle_markers']=[str(x) for x in markers]
                warnings.append('PROVIDER_CLOSE_QUOTE: '+str(len(markers))+'; исключается из расчёта свечей')
                off=off.difference(pd.DatetimeIndex(markers))
        if len(off):issues.append('Свечи вне сессии/неправильное выравнивание');bad.extend(off)
        missing=grid.difference(idx)
        classify=bool(meta.get('classify_absence') and base in STEPS and (base!='1D' or meta.get('absence_evidence')))
        if classify:
            proven=covered_empty(meta,missing,base,now)
            market_closed=covered_empty(meta,proven,base,now,closed_only=True)
            contradiction=covered_empty(meta,idx.intersection(grid),base,now)
            halt_rows=[stamp for stamp in idx.intersection(grid)
                       if market_closed_proof(meta,stamp,stamp+STEPS[base],now)]
            if halt_rows:issues.append('EXCHANGE_NOTICE_CONFLICT: свечи во время подтверждённой остановки');bad.extend(halt_rows)
            if len(contradiction):
                issues.append('Trade candles contradict NO_TRADES evidence');bad.extend(contradiction)
            missing=missing.difference(proven)
            index=_evidence_lookup(meta)
            confirmed_gap=pd.DatetimeIndex([stamp for stamp in missing if _known_trade(meta,stamp,stamp+STEPS[base],now,index)])
            unknown=missing.difference(confirmed_gap)
            delayed=[interval_status(meta,stamp,stamp+STEPS[base],now) for stamp in all_grid.difference(grid).difference(idx)]
            report['interval_classification']={
                'expected_open_intervals':len(grid),'with_candles':len(idx.intersection(grid)),
                'NO_TRADES':len(proven)-len(market_closed),'UNCONFIRMED':len(unknown),'GAP':len(confirmed_gap),
                'DELAYED':sum(x['state']=='DELAYED' for x in delayed),'PENDING':sum(x['state']=='PENDING' for x in delayed),
                'no_trades_ranges':ranges(proven.difference(market_closed),all_grid),
                'market_closed_intervals':len(market_closed),'market_closed_ranges':ranges(market_closed,all_grid),
                'exchange_closure_evidence':meta.get('absence_evidence',{}).get('exchange_closures',[]),
                'unconfirmed_ranges':ranges(unknown,all_grid),'confirmed_gap_ranges':ranges(confirmed_gap,all_grid),
                'MARKET_CLOSED':'Excluded by verified calendar; not an expected candle',
                'evidence_reference':meta.get('absence_evidence',{}).get('reference'),
                'evidence_checked_at':meta.get('absence_evidence',{}).get('checked_at')}
        if 'bar_complete' in frame and meta.get('bar_ends'):
            ends=meta['bar_ends']
            if isinstance(ends,dict):ends=[ends.get(str(stamp)) for stamp in frame.index]
            closed=pd.to_datetime(ends,utc=True)+pd.Timedelta(minutes=15)<=now
            incomplete=idx[closed & ~frame.bar_complete.to_numpy(dtype=bool)]
            if len(incomplete):issues.append('Неполные агрегированные свечи: '+str(len(incomplete)));bad.extend(incomplete)
        if len(missing):issues.append(('Неподтверждённые пустые интервалы: ' if classify else 'Пропущенные свечи: ')+str(len(missing)))
        report['missing_count']=len(missing)
        incomplete_source=[]
        proof=meta.get('absence_evidence',{})
        if (base in STEPS and meta.get('source')=='gettex' and proof.get('isin')==meta.get('symbol')
            and all(proof.get('identity',{}).get(k)==meta.get(k) and meta.get(k) for k in IDENTITY)):
            for a,b in proof.get('incomplete_publication_intervals',[]):
                left,right=pd.Timestamp(a),pd.Timestamp(b)
                if left.tzinfo is not None or right.tzinfo is not None or left>=right:continue
                incomplete_source.extend(idx[(idx<right)&(idx+STEPS[base]>left)])
        if incomplete_source:
            incomplete_source=sorted(set(incomplete_source))
            report['source_incomplete_count']=len(incomplete_source)
            report['source_incomplete_ranges']=ranges(incomplete_source,all_grid)
            issues.append('SOURCE_COVERAGE_CONFLICT: полнота официальной публикации не подтверждена; повторная проверка через очередь GETTEX')
        report['ranges']=ranges(list(missing)+bad+incomplete_source,all_grid)
        if check_stale:
            tail=expected_index(meta,base,idx.max(),now.tz_convert(meta['timezone']).tz_localize(None).normalize()+pd.Timedelta(days=1),now)
            stale=tail[tail>idx.max()]
            if classify:
                stale=stale.difference(covered_empty(meta,stale,base,now))
            if len(stale):report['stale_count']=len(stale);report['stale_range']=[str(stale[0]),str(stale[-1])]
    except Exception as error:issues.append('Календарь/timezone: '+str(error));global_invalid=True
    report['state']='INVALID' if global_invalid or bad else 'UNCONFIRMED' if report.get('source_incomplete_count') else ('UNCONFIRMED' if classify and not report.get('interval_classification',{}).get('GAP') else 'GAP') if report.get('missing_count') else 'STALE' if report.get('stale_count') else 'READY'
    report['repairable']=bool(report['ranges']) and not global_invalid and not (report.get('source_incomplete_count') and not report.get('missing_count') and not bad)
    return report

def persist(store,info,symbol,base,report):
    store.set_setting(key(info,symbol,base),report)
    return report

def repair_incoming(info,store,symbol,base,frame,download,report,now=None):
    """Retry only corrupt raw rows before they can enter SQLite."""
    if not report.get('repairable') or len(report['ranges'])>8:raise QualityError(report)
    persist(store,info,symbol,base,{**report,'state':'RECOVERING'})
    result=frame.copy();meta=frame.attrs
    for first,last in report['ranges']:
        start=pd.Timestamp(first).tz_localize(meta['timezone']);end=pd.Timestamp(last).tz_localize(meta['timezone'])+STEPS[base]
        fresh=download(symbol,{'1D':'1d','5m':'5m','15m':'15m','1H':'60m'}[base],start,end=end)
        fresh_report=audit(fresh,base,meta,now,check_stale=False)
        if fresh_report['state']!='READY':
            # A failed range retry cannot replace the diagnosis of the full
            # rejected archive (e.g. an empty gap response before a bad OHLC).
            raise QualityError({**report,'recovery_error':'; '.join(fresh_report['issues']) or fresh_report['state'],
                                'recovery_report':fresh_report})
        if fresh.index.tz is not None:fresh.index=fresh.index.tz_localize(None)
        window=fresh.loc[(fresh.index>=pd.Timestamp(first))&(fresh.index<=pd.Timestamp(last))]
        required=expected_index(meta,base,first,last,now)
        if window.empty or len(required.difference(window.index)):raise QualityError(report)
        result=pd.concat([result.loc[(result.index<pd.Timestamp(first))|(result.index>pd.Timestamp(last))],window]).sort_index()
        result.attrs=meta
    checked=audit(result,base,meta,now,check_stale=False)
    if checked['state']=='INVALID':raise QualityError(checked)
    persist(store,info,symbol,base,checked)
    return result

def inspect_cached(info,store,symbol,base,meta,now=None):
    frame=store.load_official(info['isin'],base) if meta['source']=='gettex' else store.load_series(info['isin'],base,meta['source'],symbol,meta['venue'],meta['currency'])
    frame.attrs.update(meta)
    return frame,audit(frame,base,meta,now)

def repair(info,store,symbol,base,meta,download,now=None,max_ranges=8,target_range=None,network_not_before=None,include_stale_tail=False,local_only=False):
    previous=store.setting(key(info,symbol,base),{})
    frame,report=inspect_cached(info,store,symbol,base,meta,now)
    import cp_experience
    if report.get('repairable'):
        report['experience_plan']=cp_experience.plan(info,meta,base,report)
    clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    from timeframes import MINUTES
    from history_service import recover_from_children,parent_provenance_key
    child_scopes=['market:'+info['isin']+':'+tf for tf in ('1m','5m','15m','1H') if MINUTES[tf]<(1440 if base=='1D' else MINUTES[base])]
    child_revisions=store.read_cache.token_for(child_scopes)
    report['parent_child_revisions']=child_revisions
    tail=report.get('stale_range') if include_stale_tail and base=='1D' and report.get('state') in ('READY','GAP','UNCONFIRMED','STALE') else None
    if report.get('interval_classification',{}).get('NO_TRADES') and not report.get('repairable') and not tail:
        return persist(store,info,symbol,base,report)
    # A series-wide market revision is insufficient to reuse a failed local
    # check: identity/evidence/lineage may change without a new candle. The
    # bounded interval memo below checks these actual dependencies. Existing
    # next_retry is still enforced at the network boundary.
    persist(store,info,symbol,base,report)
    if not report.get('repairable') and not tail:return report
    # A large number of gaps is work to bound, not a reason to disable recovery.
    # Resume by timestamp, newest first; no index cursor invalidated by repairs.
    pending=list(reversed(report['ranges']))
    if tail:pending.append(tail)
    # A rolling source cannot recover bars that have aged out of its archive.
    # Preserve the original DQ/ranges; this is a recovery constraint, not READY.
    if meta['source']=='proxy':
        from history_backfill import intraday_retention_start
        cutoff=intraday_retention_start(base,clock)
        if cutoff is not None:
            allowed=[];excluded=[]
            local_cutoff=cutoff.tz_convert(meta['timezone']).tz_localize(None)
            for first,last in pending:
                if pd.Timestamp(last)<local_cutoff:
                    excluded.append([first,last])
                    # A provider's retention does not erase a genuine local
                    # child archive. Permit local proof; never an old HTTP retry.
                    with store.connect() as db:
                        child_present=any(db.execute('SELECT 1 FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? AND ts>=? AND ts<? LIMIT 1',
                            (info['isin'],meta['source'],meta['venue'],meta['currency'],symbol,s.rsplit(':',1)[1],first,str(pd.Timestamp(last)+STEPS[base]))).fetchone() for s in child_scopes)
                    if child_present:allowed.append([first,last])
                    continue
                if pd.Timestamp(first)<local_cutoff:
                    grid=expected_index(meta,base,first,last,now)
                    available=grid[grid>=local_cutoff]
                    if available.empty:
                        excluded.append([first,last]);continue
                    excluded.append([first,str(grid[grid<local_cutoff].max())])
                    first=str(available[0])
                allowed.append([first,last])
            pending=allowed
            if excluded:
                report['recovery_constraints']={'status':'SOURCE_LIMITED','basis':'existing Yahoo rolling intraday retention policy',
                    'available_from':cutoff.isoformat(),'excluded_ranges':excluded,'identity':{k:meta.get(k) for k in IDENTITY}}
            if not pending:return persist(store,info,symbol,base,report)
    # A continuous missing range can span months. Process real session-sized
    # pieces so one unavailable day cannot prevent restoration of other days.
    # The existing timestamp cursor, cooldown and transaction guards still apply.
    pieces=[]
    for first,last in pending:
        grid=expected_index(meta,base,first,last,clock)
        for day in grid.normalize().unique():
            part=grid[grid.normalize()==day]
            pieces.append([str(part[0]),str(part[-1])])
    pending=sorted(pieces,key=lambda r:r[0],reverse=True)
    if target_range is not None:
        # Owner priority changes which existing repair is serviced, never the
        # audit, provider retention, cooldown or atomic publication checks.
        left,right=map(pd.Timestamp,target_range)
        pending=[[str(max(pd.Timestamp(a),left)),str(min(pd.Timestamp(b),right))]
                 for a,b in pending if pd.Timestamp(a)<=right and pd.Timestamp(b)>=left]
    if not pending:return persist(store,info,symbol,base,report)
    cursor=previous.get('recovery_cursor')
    if cursor and target_range is None:
        older=[r for r in pending if r[0]<cursor]
        pending=older or pending
    batch=pending[:1 if report['state']=='UNCONFIRMED' else max(1,int(max_ranges))]
    persist(store,info,symbol,base,{**report,'state':'RECOVERING'})
    interval={'1D':'1d','5m':'5m','15m':'15m','1H':'60m'}[base]
    before={};attempt_method='local_children'
    failures=[];completed=[];visited=[];waiting=None;local_skips=0;provider_attempted=False
    for first,last in batch:
        visited.append([first,last])
        try:
            attempt_method='local_children'
            replacement=None;required=None;observations=[]
            lineage_key=parent_provenance_key(info['isin'],meta,base)
            scopes=['instrument:'+info['isin'],'market:'+info['isin']+':'+base,'setting:interval_evidence:'+info['isin'],'setting:history_coverage:'+info['isin']+':'+symbol+':'+base,
                    'setting:'+lineage_key,'setting:history_identity:'+info['isin'],*child_scopes,
                    *('setting:data_quality:'+info['isin']+':'+symbol+':'+s.rsplit(':',1)[1] for s in child_scopes)]
            if meta['source']=='gettex_chart':
                scopes.append('setting:gettex_chart:'+info['isin']+':'+base)
            def revisions(db):
                return {s:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or [0])[0] for s in scopes}
            with store.connect() as db:before=revisions(db)
            start=pd.Timestamp(first).tz_localize(meta['timezone']);end=pd.Timestamp(last).tz_localize(meta['timezone'])+STEPS[base]
            replacement,local_reused=cp_experience.local_recovery(info,store,meta,base,first,last,clock,
                lambda:recover_from_children(info,store,meta,base,first,last,clock,observations=observations))
            local_skips+=int(local_reused)
            if replacement is None:
                if local_only:
                    waiting=(clock+pd.Timedelta(minutes=15)).isoformat()
                    break
                # Local recovery does not consume a provider request. Preserve
                # Retry-After/rate reservations at the actual network boundary.
                waits=[pd.Timestamp(x) for x in (previous.get('next_retry'),network_not_before) if x and clock<pd.Timestamp(x)]
                if waits:
                    waiting=max(waits).isoformat()
                    break
                if meta['source']=='proxy' and cutoff is not None and start<cutoff:
                    raise ValueError('SOURCE_LIMITED: no complete compatible local children; historical HTTP skipped')
                attempt_method='provider_range'
                provider_attempted=True
                try:replacement=download(symbol,interval,start,end=end)
                except Exception as provider_error:
                    # A provider-wide wait/error is not permission to try its
                    # auxiliary endpoint. Local data was already checked above.
                    if cp_experience.failure_evidence(provider_error)['code'] in (
                        'WAITING_PROVIDER','RATE_LIMIT_RESPONSE','HTTP_ERROR','INPUT_CHANGED','IDENTITY_REJECTED','DATA_CONFLICT'):
                        raise
                    replacement=recover_from_children(info,store,meta,base,first,last,clock,allow_download=True,observations=observations)
                    if replacement is None:raise
            replacement_report=audit(replacement,base,meta,now,check_stale=False)
            required=expected_index(meta,base,first,last,now)
            if replacement_report['state']!='READY' or not required.isin(replacement.index).all():
                # A partial local candidate is not permission to bypass the
                # provider wait through an auxiliary child-data endpoint.
                waits=[pd.Timestamp(x) for x in (previous.get('next_retry'),network_not_before) if x and clock<pd.Timestamp(x)]
                if local_only or waits:
                    waiting=max(waits).isoformat() if waits else (clock+pd.Timedelta(minutes=15)).isoformat()
                    break
                provider_attempted=True
                alternative=recover_from_children(info,store,meta,base,first,last,clock,allow_download=True,observations=observations)
                if alternative is not None:
                    replacement=alternative;replacement_report=audit(replacement,base,meta,now,check_stale=False)
            if replacement_report['state']!='READY':raise QualityError(replacement_report)
            attempt_method=replacement.attrs.get('recovery_method','provider_range')
            if replacement.index.tz is not None:replacement.index=replacement.index.tz_localize(None)
            window=replacement.loc[(replacement.index>=pd.Timestamp(first))&(replacement.index<=pd.Timestamp(last))]
            # Require every requested bar, even when provider returns an empty/partial window.
            required=expected_index(meta,base,first,last,now)
            if window.empty or len(required.difference(window.index)):raise ValueError('Источник не восстановил весь повреждённый участок')
            rows=[(info['isin'],*[meta[k] for k in IDENTITY[:4]],base,str(ts),*[float(r[k]) for k in FIELDS]) for ts,r in window.iterrows()]
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if revisions(db)!=before:raise ValueError('Recovery input changed; no stale OHLCV write')
                auxiliary=replacement.attrs.get('auxiliary_children')
                if auxiliary is not None:
                    aux_rows=[(info['isin'],*[meta[k] for k in IDENTITY[:4]],replacement.attrs['auxiliary_base'],str(ts),*[float(r[k]) for k in FIELDS]) for ts,r in auxiliary.iterrows()]
                    db.executemany('INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',aux_rows)
                if meta['source']=='gettex':
                    official_rows=[(r[0],r[1],r[5],r[6],*r[7:]) for r in rows]
                    db.executemany('INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)',official_rows)
                else:db.executemany('INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',rows)
                from candle_timing import record_recovery
                record_recovery(db,info['isin'],meta,base,frame,window)
                proofs=replacement.attrs.get('parent_recovery_proofs',{})
                if proofs:
                    from performance_cache import _closed_rows,_digest
                    old=db.execute('SELECT value FROM settings WHERE key=?',(lineage_key,)).fetchone()
                    saved=json.loads(old[0]) if old else {}
                    for stamp,proof in proofs.items():
                        if [_digest(_closed_rows(db,s)) for s in proof['inputs']]!=proof['digests']:
                            raise ValueError('Recovery lineage changed; atomic rollback')
                        proof['input_revisions']=revisions(db)
                        saved[stamp]=proof
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(lineage_key,json.dumps(saved)))
                cp_experience.record(db,info,meta,base,first,last,before,clock,attempt_method,'SUCCESS',
                                     cp_experience.measured_reason('Validated OHLCV and provenance committed atomically; current DQ/CORE remain independent',observations))
            completed.append([first,last])
        except Exception as error:
            evidence=cp_experience.measured_reason(cp_experience.failure_evidence(error,replacement,required),observations)
            retry=store.setting('cp_source_retry:'+meta['source'],None)
            if isinstance(retry,dict):retry=retry.get('retry_after')
            if retry and clock<pd.Timestamp(retry):waiting=max(pd.Timestamp(retry),pd.Timestamp(waiting) if waiting else clock).isoformat()
            if evidence['code']=='WAITING_PROVIDER':
                waiting=waiting or (clock+pd.Timedelta(minutes=5)).isoformat()
                break  # No HTTP attempt, no failed range or failure event.
            if evidence['code']!='WAITING_PROVIDER':
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    cp_experience.record(db,info,meta,base,first,last,before,clock,attempt_method,'FAILED',evidence)
            failures.append(dict(first=first,last=last,code=evidence['code'],quality=error.report.get('state') if isinstance(error,QualityError) else None,
                                 error=str(error)))
            # Only independent data failures may continue within this bounded
            # batch. Never continue through provider-wide waits/races/identity errors.
            if evidence['code'] not in ('EMPTY_RESPONSE','INCOMPLETE_RESPONSE','INVALID_RESPONSE','AVAILABLE_FILES_NO_MATCH','SOURCE_LIMITED'):
                break
    if completed:
        _,after=inspect_cached(info,store,symbol,base,meta,now)
    else:after=dict(report)
    if report.get('experience_plan'):after['experience_plan']=report['experience_plan']
    if report.get('recovery_constraints'):after['recovery_constraints']=report['recovery_constraints']
    after['parent_child_revisions']=child_revisions
    after['recovery_ranges']=visited
    after['recovery_completed_ranges']=completed
    after['recovery_failed_ranges']=[{k:v for k,v in f.items() if k!='error'} for f in failures]
    after['local_recovery_reused']=local_skips
    after['recovery_provider_attempted']=provider_attempted
    after['recovery_cursor']=previous.get('recovery_cursor') if target_range is not None else visited[-1][0] if visited else None
    if waiting:
        after['next_retry']=waiting
        if network_not_before:after['recovery_wait']='PROVIDER'
    if failures:
        last_failure=failures[-1]
        attempts=previous.get('attempts',0)+1 if previous.get('ranges')==report['ranges'] else 1
        state=after['state']
        if after.get('repairable'):
            state='UNCONFIRMED' if state=='UNCONFIRMED' else 'INVALID' if state=='INVALID' else 'SOURCE_GAP' if state=='GAP' and last_failure['quality']=='SOURCE_UNAVAILABLE' else 'SOURCE_UNAVAILABLE'
        after.update(state=state,recovery_quality=last_failure['quality'],recovery_error=last_failure['error'],attempts=attempts)
        retry=clock+pd.Timedelta(minutes=min(60,5*2**min(attempts-1,4)))
        after['next_retry']=max(retry,pd.Timestamp(waiting)).isoformat() if waiting else retry.isoformat()
    return persist(store,info,symbol,base,after)
