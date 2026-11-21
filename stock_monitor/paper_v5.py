"""V5 hourly opening breakouts and daily-weakness reversals."""
from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil
from zoneinfo import ZoneInfo

import pandas as pd

LEGACY_VERSION = 'opening_range_breadth_1h_v1'
REVERSAL_VERSION = 'opening_range_and_reversal_1h_v2'
VERSION = 'opening_range_reversal_event_filter_1h_v3'
MAX_SHARES = 10
HOLD_MINUTES = 180
HOLD_BY_VERSION = {LEGACY_VERSION: 300, REVERSAL_VERSION: 180, VERSION: HOLD_MINUTES}
REVERSAL_MAX_HOUR = 11
REVERSAL_VOLUME_MULTIPLE = 1.1
REVERSAL_CLOSE_LOCATION = .65
STOP_ATR = Decimal('1.2')
TARGET_ATR = Decimal('2.3')
STOP_FLOOR = Decimal('.003')
TARGET_FLOOR = Decimal('.006')
FIELDS = ('open', 'high', 'low', 'close', 'volume')
NY = ZoneInfo('America/New_York')
# Verified US local dates. An unlisted day outside this interval is unknown.
BLS_CALENDAR = 'https://www.bls.gov/schedule/2026/12_sched_list.htm'
FED_CALENDAR = 'https://www.federalreserve.gov/newsevents/2026-december.htm'
MACRO_DATES = {
    '2026-12-04': ('EMPLOYMENT', '08:30', BLS_CALENDAR),
    '2026-12-09': ('FOMC', '14:00', FED_CALENDAR),
    '2026-12-10': ('CPI', '08:30', BLS_CALENDAR),
    '2026-12-15': ('PPI', '08:30', BLS_CALENDAR),
}
MACRO_VERIFIED_FROM = '2026-11-14'
MACRO_VERIFIED_THROUGH = '2026-12-31'


def macro_release_day(at):
    day=at.astimezone(NY).date().isoformat()
    event=MACRO_DATES.get(day)
    if event:
        kind,hour,source=event
        return dict(state='RELEASE_DAY',kind=kind,source=source,
                    scheduled_at=datetime.fromisoformat(day+'T'+hour).replace(tzinfo=NY).isoformat())
    covered=MACRO_VERIFIED_FROM<=day<=MACRO_VERIFIED_THROUGH
    return dict(state='CLEAR' if covered else 'CALENDAR_UNCONFIRMED')


def reversal_level_lost(strategy, reference):
    if strategy['setup']!='DAILY_REVERSAL':
        return False
    return reference <= Decimal(str(strategy['entry_level']))


def hourly_features(sessions, raw, through, *, available_bars=False):
    """Keep the scheduled grid. An absent or invalid child leaves a hole."""
    candles = []
    quarter = pd.Timedelta(minutes=15)
    for session in sessions.itertuples():
        opening, end = session.open, session.close
        for number, start in enumerate(pd.date_range(opening, end, freq='1h', inclusive='left')):
            close = min(start + pd.Timedelta(hours=1), end)
            if close > through:
                break
            starts = pd.date_range(start, close, freq=quarter, inclusive='left')
            children = raw.reindex(starts)
            coverage=bool(children['volume'].notna().all())
            if available_bars:children=raw.loc[(raw.index>=start)&(raw.index<close)]
            bar = dict(start=start, closed=close, session_end=end, number=number, complete=False,coverage_complete=coverage)
            if not children.empty and children['volume'].notna().all():
                bar.update(open=children.open.iloc[0], close=children.close.iloc[-1],
                           low=children.low.min(), high=children.high.max(),
                           volume=children.volume.sum(), complete=True)
            candles.append(bar)
    if not candles:
        return pd.DataFrame()
    frame = pd.DataFrame(candles, columns=['start','closed','session_end','number','complete','coverage_complete',*FIELDS]).set_index('start')
    previous = frame.close.shift(1)
    tr = pd.concat([frame.high-frame.low, (frame.high-previous).abs(),
                    (frame.low-previous).abs()], axis=1).max(axis=1)
    frame['atr'] = tr.rolling(16, min_periods=10).mean()
    frame['prev_close'] = previous
    frame['prev_high'] = frame.high.shift(1)
    frame['vol_med'] = frame.volume.shift(1).rolling(12, min_periods=8).median()
    frame['open_high'] = frame.high.where(frame.number == 0).groupby(frame.session_end).ffill()
    frame['open_low'] = frame.low.where(frame.number == 0).groupby(frame.session_end).ffill()
    return frame


def clean_quotes(rows, zone, *, available_bars=False):
    """Build V5 input only. The other strategies keep their own CORE inputs."""
    import numpy as np
    raw = pd.DataFrame(rows, columns=['ts', *FIELDS]).set_index('ts', verify_integrity=True)
    stamps = pd.DatetimeIndex(pd.to_datetime(raw.index))
    raw.index = stamps.tz_localize(zone).tz_convert('UTC') if stamps.tz is None else stamps.tz_convert('UTC')
    raw = raw.astype(float)
    prices = raw[['open','high','low','close']]
    valid = (np.isfinite(raw).all(axis=1) & prices.gt(0).all(axis=1)
             & (raw.volume.ge(0) if available_bars else raw.volume.gt(0)) & raw.high.ge(prices.max(axis=1))
             & raw.low.le(prices.min(axis=1)))
    return raw.loc[valid].sort_index()


def context(store, info, identity, signal_at, now, listings):
    """Closed snapshot tied to the normal market and listing revisions."""
    from eur_listings import stored_identity
    from timeframes import schedule

    zone = identity['timezone']
    today = now.tz_convert(zone).date()
    if not signal_at:
        return None
    start = pd.Timestamp(signal_at)
    start = start.tz_localize(zone).tz_convert('UTC') if start.tzinfo is None else start.tz_convert('UTC')
    first = max(today - timedelta(days=10), today.replace(month=1, day=1))
    sessions = schedule(identity, first, today).tail(3)
    if sessions.empty or sessions.index[-1].date() != today:
        return None
    lower = sessions.open.iloc[0].tz_convert(zone).tz_localize(None)
    upper = now.tz_convert(zone).tz_localize(None) - pd.Timedelta(minutes=15)
    isin = info['isin']
    store.read_cache.depend('market:'+isin+':15m')
    store.read_cache.depend('setting:gettex_chart:'+isin+':15m')
    from paper_account import registered_instrument
    with store.connect() as db:
        if not stored_identity(db, info, identity, '15m'):
            return None
        rows = _rows(db, isin, identity, lower, upper)
        opening = sessions.open.iloc[-1]
        frame = hourly_features(sessions, clean_quotes(rows, zone,available_bars=True), now,available_bars=True)
        if frame.empty or start != frame.index[-1]:
            return None
        bar = frame.iloc[-1]
        keys = ('open','high','low','close','atr','prev_close','prev_high','vol_med','volume','open_high','open_low')
        data = {key: float(bar[key]) if pd.notna(bar[key]) else None for key in keys}
        result = dict(data,signal_at=signal_at,complete=bool(bar['complete']),number=int(bar['number']),
                      session_end=bar['session_end'].isoformat(),checked_at=now.isoformat(),version=VERSION)
        result.update(data_policy='gettex_first_v1',coverage_complete=bool(bar['coverage_complete']))
        result['daily_down_closes'] = None
        if not opening_reason(result,now):
            setup = 'OPEN_RANGE'
        elif reversal_hour(result):
            store.read_cache.depend('market:'+isin+':1D')
            store.read_cache.depend('setting:gettex_chart:'+isin+':1D')
            result['daily_down_closes'] = previous_daily_down_closes(db,info,identity,today)
            setup = 'REVERSAL' if (result['daily_down_closes'] or 0)>=2 else None
        else:
            setup = None

        group = []
        required = max(3, ceil(.75*len(listings)))
        close = bar['closed']
        if setup:
            slots = pd.date_range(opening,opening+pd.Timedelta(hours=1),freq='15min',inclusive='left')
            slots = slots.append(pd.date_range(start,close,freq='15min',inclusive='left'))
            for peer, meta in listings.items():
                if any(meta.get(key)!=identity[key] for key in ('source','venue','currency','timezone')):
                    continue
                store.read_cache.depend('instrument:'+peer)
                store.read_cache.depend('market:'+peer+':15m')
                store.read_cache.depend('setting:gettex_chart:'+peer+':15m')
                try:
                    peer_info = registered_instrument(db,peer)
                except ValueError:
                    continue
                if not stored_identity(db,peer_info,meta,'15m'):
                    continue
                peer_rows = rows if peer==isin else _rows(db,peer,meta,
                    opening.tz_convert(zone).tz_localize(None),
                    slots[-1].tz_convert(zone).tz_localize(None))
                observations = clean_quotes(peer_rows,zone,available_bars=True)
                observations=observations.loc[observations.index.isin(slots)]
                if (not observations.empty and observations.index.min()<opening+pd.Timedelta(hours=1)
                        and observations.index.max()>=start and observations.volume.notna().all()):
                    group.append((peer,bool(observations.close.iloc[-1]>observations.open.iloc[0])))
    return dict(result,
                breadth_available=len(group),breadth_positive=sum(rising for _,rising in group),
                breadth_required=required,breadth_inputs=[peer for peer,_ in group])


def previous_daily_down_closes(db, info, identity, today):
    """Use all four previous exchange sessions; today's daily bar is excluded."""
    from eur_listings import stored_identity
    from timeframes import schedule

    if not stored_identity(db,info,identity,'1D'):
        return None
    sessions=schedule({**identity,'timeframe':'1D'},today-timedelta(days=15),
                      today-timedelta(days=1)).tail(4)
    if len(sessions)!=4:
        return None
    days={date.date() for date in sessions.index}
    rows=_rows(db,info['isin'],identity,str(min(days)),str(today),base='1D')
    values=clean_quotes(rows,identity['timezone'])
    dates=values.index.tz_convert(identity['timezone']).date
    selected=values.loc[[date in days for date in dates]]
    if len(selected)!=4 or set(selected.index.tz_convert(identity['timezone']).date)!=days:
        return None
    return int(selected.close.diff().lt(0).sum())


def _rows(db, isin, identity, lower, upper, *, base='15m'):
    return db.execute(
        'SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? '
        'AND venue=? AND currency=? AND symbol=? AND base_tf=? AND ts>=? AND ts<=? ORDER BY ts',
        (isin,identity['source'],identity['venue'],identity['currency'],identity['symbol'],
         base,str(lower),str(upper))).fetchall()


def opening_reason(bar, when):
    if not bar or not bar['complete'] or pd.isna(bar['atr']) or bar['atr'] <= 0:
        return 'V5_INCOMPLETE_HOUR_OR_WARMUP'
    if pd.Timestamp(bar['session_end']) - when < pd.Timedelta(hours=1):
        return 'V5_LAST_HOUR'
    if not 1 <= bar['number'] <= 5:
        return 'V5_OUTSIDE_OPEN_RANGE_WINDOW'
    if any(pd.isna(bar[key]) for key in ('prev_close','open_high','open_low')):
        return 'V5_OPEN_RANGE_UNCONFIRMED'
    if not bar['prev_close'] <= bar['open_high'] < bar['close']:
        return 'V5_NO_FRESH_OPEN_RANGE_BREAK'
    if bar['open_high'] - bar['open_low'] > 2*bar['atr']:
        return 'V5_OPEN_RANGE_TOO_WIDE'
    if pd.isna(bar['vol_med']) or bar['volume'] < bar['vol_med'] or bar['close'] <= bar['open']:
        return 'V5_NO_VOLUME_EXPANSION'
    return ''


def reversal_hour(bar):
    """A rising, high-close candle reclaiming the previous hourly high."""
    if not bar['complete'] or not (1<=bar['number']<=REVERSAL_MAX_HOUR):
        return False
    values=[bar.get(key) for key in ('atr','high','low','prev_high','vol_med')]
    if any(pd.isna(value) for value in values):
        return False
    atr,high,low,previous_high,volume_median=values
    return (atr>0 and high>low and bar['close']>max(bar['open'],previous_high)
            and bar['volume']>=REVERSAL_VOLUME_MULTIPLE*volume_median
            and (bar['close']-low)/(high-low)>=REVERSAL_CLOSE_LOCATION)


def decide(engine, timeframe):
    if timeframe != '1H':
        return dict(action='WAIT', reason='V5_SELECT_1H')
    bar = engine.get('v5_opening_range')
    if not bar or bar.get('version') != VERSION or not engine.get('timestamp') or bar.get('signal_at') != engine['timestamp']:
        return dict(action='WAIT', reason='V5_OPEN_RANGE_UNCONFIRMED')
    reason = opening_reason(bar, pd.Timestamp(bar['checked_at']))
    setup='OPEN_RANGE'
    if reason:
        if not reversal_hour(bar):
            return dict(action='WAIT', reason=reason)
        daily=bar.get('daily_down_closes')
        if daily is None:
            return dict(action='WAIT',reason='V5_REVERSAL_DAILY_UNCONFIRMED')
        if daily<2:
            return dict(action='WAIT',reason='V5_REVERSAL_DAILY_WEAKNESS_NOT_CONFIRMED')
        setup='DAILY_REVERSAL'
    if bar['breadth_available']<bar['breadth_required']:
        return dict(action='WAIT',reason='V5_BREADTH_INPUTS_UNCONFIRMED')
    if bar['breadth_positive']<bar['breadth_required']:
        return dict(action='WAIT',reason='V5_BREADTH_TOO_WEAK')
    stop, target = levels(Decimal(str(bar['close'])), Decimal(str(bar['atr'])))
    level=bar['open_high'] if setup=='OPEN_RANGE' else bar['prev_high']
    return dict(action='BUY', reason='V5_OPEN_RANGE_BREAK' if setup=='OPEN_RANGE' else 'V5_DAILY_REVERSAL',
                model=VERSION, atr=bar['atr'],setup=setup,daily_down_closes=bar.get('daily_down_closes'),
                entry_level=level,
                opening_high=bar['open_high'], relative_volume=bar['volume']/bar['vol_med'],
                breadth_positive=bar['breadth_positive'],breadth_available=bar['breadth_available'],
                breadth_required=bar['breadth_required'],breadth_inputs=bar['breadth_inputs'],
                stop=str(stop), target=str(target), hold_minutes=HOLD_MINUTES)


def levels(reference, atr):
    return (reference - max(STOP_ATR*atr, reference*STOP_FLOOR),
            reference + max(TARGET_ATR*atr, reference*TARGET_FLOOR))
