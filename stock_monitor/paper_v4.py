"""V4 applies the V5 breakout and reversal family to the selected PAPER interval."""
from datetime import timedelta
from decimal import Decimal
from math import ceil

import pandas as pd

from paper_v5 import HOLD_MINUTES, clean_quotes, levels, previous_daily_down_closes, _rows
from timeframes import MINUTES, schedule

VERSION = 'v5_entries_selected_timeframe_v4_v1'
MODE = 'MOMENTUM'
EARNINGS_LOOKBACK = 5
# Dates of already published quarterly releases, in the issuer's local date.
# Add verified releases here. A scheduled call alone is not a published result.
PUBLISHED_REPORTS = {
    'US0378331005': ('2026-07-30', '2026-11-02'),
    'US67066G1040': ('2026-08-26',),
    'US0231351067': ('2026-07-30', '2026-10-29'),
    'US30303M1027': ('2026-07-29', '2026-10-21'),
}


def report_for(sessions, dates, signal_day):
    """Return the already published date if the signal belongs to its next five sessions."""
    reported = [date for date in dates if date < signal_day]
    if not reported:
        return None
    date = max(reported)
    days = sessions.index
    count = days.searchsorted(pd.Timestamp(signal_day), side='right') - days.searchsorted(
        pd.Timestamp(date), side='right')
    return date if 1 <= count <= EARNINGS_LOOKBACK else None


def features(frame):
    previous = frame.close.shift(1)
    tr = pd.concat([frame.high-frame.low, (frame.high-previous).abs(),
                    (frame.low-previous).abs()], axis=1).max(axis=1)
    return frame.assign(prev_close=previous, prev_high=frame.high.shift(1),
                        atr=tr.rolling(16, min_periods=10).mean(),
                        vol_med=frame.volume.shift().rolling(12, min_periods=8).median())


def intraday(sessions, raw, timeframe, through, *, available_bars=False):
    """Session-aligned candles and the original first 60 minutes of the session."""
    candle = pd.Timedelta(minutes=MINUTES[timeframe])
    quarter = pd.Timedelta(minutes=15)
    hour = pd.Timedelta(hours=1)
    rows = []
    for session in sessions.itertuples():
        first_hour = raw.reindex(pd.date_range(session.open, session.open+hour, freq=quarter, inclusive='left'))
        if available_bars:first_hour=raw.loc[(raw.index>=session.open)&(raw.index<session.open+hour)]
        valid_range = not first_hour.empty and first_hour.volume.notna().all()
        for start in pd.date_range(session.open, session.close, freq=candle, inclusive='left'):
            closed = min(start+candle, session.close)
            if closed>through:
                break
            children = raw.reindex(pd.date_range(start,closed,freq=quarter,inclusive='left'))
            coverage=bool(children.volume.notna().all())
            if available_bars:children=raw.loc[(raw.index>=start)&(raw.index<closed)]
            complete = not children.empty and children.volume.notna().all()
            elapsed = (closed-session.open).total_seconds()/60
            row = dict(start=start,closed=closed,session_end=session.close,session_open=session.open,
                       elapsed=elapsed,complete=bool(complete),coverage_complete=coverage,session_day=session.Index.strftime('%Y-%m-%d'))
            if complete:
                row.update(open=children.open.iloc[0],close=children.close.iloc[-1],
                           low=children.low.min(),high=children.high.max(),volume=children.volume.sum())
            if elapsed>60 and valid_range:
                row.update(open_high=first_hour.high.max(),open_low=first_hour.low.min(),
                           range_close=first_hour.close.iloc[-1])
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows,columns=('start','closed','session_end','session_open','elapsed','session_day',
        'complete','coverage_complete','open','close','low','high','volume','open_high','open_low','range_close')).set_index('start')
    return features(frame)


def daily(sessions, raw, through):
    completed = sessions[sessions.close<=through]
    frame = raw.reindex(completed.index).copy()
    frame['closed'] = completed.close
    frame['complete'] = frame.volume.notna()
    frame['session_day'] = frame.index.strftime('%Y-%m-%d')
    frame['daily_down_closes'] = frame.close.diff().lt(0).where(frame.close.notna() & frame.close.shift().notna()).shift().rolling(3,min_periods=3).sum()
    return features(frame)


def price_setup(bar, timeframe, when, *, earnings_only=False):
    required = ('atr','open','close','volume','vol_med','high','low')
    if not bar or not bar['complete'] or any(pd.isna(bar.get(key)) for key in required):
        return None
    if bar['atr']<=0 or bar['vol_med']<=0 or bar['close']<=bar['open']:
        return None
    prev_close,prev_high = bar.get('prev_close'),bar.get('prev_high')
    crossed = pd.notna(prev_close) and pd.notna(prev_high) and prev_close<=prev_high<bar['close']
    if not earnings_only and timeframe=='1D':
        if crossed and bar['volume']>=bar['vol_med']:
            return 'OPEN_RANGE',prev_high
    elif not earnings_only:
        elapsed = bar['elapsed']
        hi,lo = bar.get('open_high'),bar.get('open_low')
        # On 4H the first signal candle contains the entire opening range.
        before = bar.get('range_close') if elapsed==240 and timeframe=='4H' else prev_close
        if (60<elapsed<=360 and pd.Timestamp(bar['session_end'])-when>=pd.Timedelta(hours=1)
                and all(pd.notna(v) for v in (hi,lo,before))
                and before<=hi<bar['close'] and hi-lo<=2*bar['atr'] and bar['volume']>=bar['vol_med']):
            return 'OPEN_RANGE',hi
    location = (bar['close']-bar['low'])/(bar['high']-bar['low']) if bar['high']>bar['low'] else 0
    if (timeframe=='1D' or 60<bar['elapsed']<=720) and pd.notna(prev_high) and bar['close']>prev_high:
        if (not earnings_only and (bar.get('daily_down_closes') or 0)>=2
                and location>=.65 and bar['volume']>=1.1*bar['vol_med']):
            return 'DAILY_REVERSAL',prev_high
        if ((timeframe=='1D' or bar['elapsed']<=660) and bar.get('report_date') and crossed
                and location>=.60 and bar['volume']>=1.2*bar['vol_med']):
            return 'POST_EARNINGS',prev_high
    return None


def decide(engine, timeframe):
    bar = engine.get('v4_selected_context')
    if (not bar or bar['timeframe']!=timeframe or bar['signal_at']!=engine.get('timestamp')):
        return dict(action='WAIT',reason='V4_SELECTED_CANDLE_UNCONFIRMED')
    choice = price_setup(bar,timeframe,pd.Timestamp(bar['checked_at']))
    if not choice:
        return dict(action='WAIT',reason='V4_ENTRY_NOT_CONFIRMED')
    setup,level = choice
    if setup!='POST_EARNINGS' and bar['breadth_positive']<bar['breadth_required']:
        choice = price_setup(bar,timeframe,pd.Timestamp(bar['checked_at']),earnings_only=True)
        if choice is None:
            reason = ('V5_BREADTH_INPUTS_UNCONFIRMED' if bar['breadth_available']<bar['breadth_required']
                      else 'V5_BREADTH_TOO_WEAK')
            return dict(action='WAIT',reason=reason)
        setup,level = choice
    stop,target = levels(Decimal(str(bar['close'])),Decimal(str(bar['atr'])))
    return dict(action='BUY',reason='V4_'+setup, model=VERSION,setup=setup,entry_level=level,
                opening_high=bar.get('open_high'), daily_down_closes=bar.get('daily_down_closes'),
                report_date=bar.get('report_date'), relative_volume=bar['volume']/bar['vol_med'],
                atr=bar['atr'], stop=str(stop),target=str(target),hold_minutes=HOLD_MINUTES,
                signal_closed=bar['closed'],
                **{key:bar[key] for key in ('breadth_available','breadth_positive','breadth_required','breadth_inputs')})


def _series(store, db, info, identity, base, first, last):
    from eur_listings import stored_identity
    isin = info['isin']
    store.read_cache.depend('instrument:'+isin)
    store.read_cache.depend('market:'+isin+':'+base)
    store.read_cache.depend('setting:gettex_chart:'+isin+':'+base)
    if not stored_identity(db,info,identity,base):
        return None
    return clean_quotes(_rows(db,isin,identity,first,last,base=base),identity['timezone'],available_bars=True)


def context(store, info, identity, signal, when, listings, timeframe):
    """Use exact native inputs and retain every source revision before execution."""
    from paper_account import registered_instrument

    if not signal:
        return None
    zone = identity['timezone']
    today = when.tz_convert(zone).date()
    current = pd.Timestamp(today)
    is_daily = timeframe=='1D'
    calendar = schedule({**identity,'timeframe':'1D'},today-timedelta(days=45),today)
    if current not in calendar.index:
        return None
    if not is_daily:
        count = 9 if timeframe=='4H' else 3
        sessions = calendar.tail(count)
        stamp = pd.Timestamp(signal)
        stamp = stamp.tz_localize(zone).tz_convert('UTC') if stamp.tzinfo is None else stamp.tz_convert('UTC')
        first = sessions.open.iloc[0].tz_convert(zone).tz_localize(None)
        last = (when-pd.Timedelta(minutes=15)).tz_convert(zone).tz_localize(None)
        base = '15m'
    else:
        # Closed daily decisions enter on a fresh quote in the following session.
        sessions = calendar[calendar.index<current]
        stamp = pd.Timestamp(signal).tz_localize(None).normalize()
        if sessions.empty or stamp!=sessions.index[-1]:
            return None
        first,last = str(sessions.index[0].date()),str(today)
        base = '1D'
    with store.connect() as db:
        raw = _series(store,db,info,identity,base,first,last)
        if raw is None:
            return None
        if is_daily:
            raw.index = raw.index.tz_convert(zone).tz_localize(None).normalize()
            frame = daily(sessions,raw,when)
        else:
            frame = intraday(sessions,raw,timeframe,when,available_bars=True)
        if frame.empty or stamp!=frame.index[-1] or not frame['complete'].iloc[-1]:
            return None
        bar = frame.iloc[-1].to_dict()
        if not is_daily and bar['session_day']!=today.isoformat():
            return None
        bar.update(timeframe=timeframe,signal_at=signal,checked_at=when.isoformat(),
                   closed=bar['closed'].isoformat(),
                   report_date=report_for(calendar,PUBLISHED_REPORTS.get(info['isin'],()),bar['session_day']))
        if not is_daily:
            store.read_cache.depend('market:'+info['isin']+':1D')
            store.read_cache.depend('setting:gettex_chart:'+info['isin']+':1D')
            bar['daily_down_closes'] = previous_daily_down_closes(db,info,identity,today) or 0
        choice = price_setup(bar,timeframe,when)
        peers = []
        if choice and choice[0]!='POST_EARNINGS':
            for isin, meta in listings.items():
                if any(meta.get(key)!=identity[key] for key in ('source','venue','currency','timezone')):
                    continue
                try:
                    peer = registered_instrument(db,isin)
                except ValueError:
                    continue
                if is_daily:
                    date = bar['session_day']
                    values = _series(store,db,peer,meta,base,date,date+' 23:59:59')
                    if values is not None and len(values)==1:
                        peers.append((isin,bool(values.close.iloc[0]>values.open.iloc[0])))
                else:
                    opening = bar['session_open']
                    end = pd.Timestamp(bar['closed'])
                    quarter = pd.Timedelta(minutes=15)
                    slots = pd.date_range(opening,opening+pd.Timedelta(hours=1),freq=quarter,inclusive='left').union(
                        pd.date_range(stamp,end,freq=quarter,inclusive='left'))
                    values = raw if isin==info['isin'] else _series(store,db,peer,meta,base,
                        slots[0].tz_convert(zone).tz_localize(None),slots[-1].tz_convert(zone).tz_localize(None))
                    if values is not None:
                        selected = values.loc[values.index.isin(slots)]
                        if (not selected.empty and selected.index.min()<opening+pd.Timedelta(hours=1)
                                and selected.index.max()>=stamp and selected.volume.notna().all()):
                            peers.append((isin,bool(selected.close.iloc[-1]>selected.open.iloc[0])))
        bar.update(data_policy='gettex_first_v1',breadth_available=len(peers),breadth_positive=sum(up for _,up in peers),
                   breadth_required=max(3,ceil(.75*len(listings))),breadth_inputs=[isin for isin,_ in peers])
        return {key:(None if pd.isna(value) else float(value)) if isinstance(value,float) else
                value.isoformat() if isinstance(value,pd.Timestamp) else value for key,value in bar.items()}

