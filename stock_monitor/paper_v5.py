"""V5 hourly opening breakouts and daily-weakness reversals."""
from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil
from zoneinfo import ZoneInfo

import pandas as pd
from paper_v5_daily_pullback import HOLD_MINUTES as DAILY_PULLBACK_HOLD, MODEL as DAILY_PULLBACK_MODEL
from paper_v5_four_hour import HOLD_MINUTES as FOUR_HOUR_HOLD, MODEL as FOUR_HOUR_MODEL

LEGACY_VERSION = 'opening_range_breadth_1h_v1'
REVERSAL_VERSION = 'opening_range_and_reversal_1h_v2'
VERSION = 'opening_range_reversal_event_filter_1h_v3'
MULTI_VERSION = 'v5_core_confirmed_breakout_v1'
PORTFOLIO_VERSION = 'v5_monthly_portfolio_v2'
PREVIOUS_UNIFIED_VERSION = 'v5_unified_entry_v3'
UNIFIED_VERSION = 'v5_unified_entry_v4'
AUTO_ENTRY_MODELS = (UNIFIED_VERSION, FOUR_HOUR_MODEL, DAILY_PULLBACK_MODEL)
HELD_GAP = 'HELD_GAP_1_ATR'
GAP_MAX_DISPLACEMENT = Decimal('.50')
GAP_HOLD_SESSIONS = 3
GAP_TARGET_ATR = Decimal('1.25')
PORTFOLIO_MAX_DISTANCE = Decimal('.75')
MAX_SHARES = 10
HOLD_MINUTES = 180
HOLD_BY_VERSION = {LEGACY_VERSION: 300, REVERSAL_VERSION: 180, VERSION: HOLD_MINUTES, MULTI_VERSION: HOLD_MINUTES,
                   PORTFOLIO_VERSION: HOLD_MINUTES, PREVIOUS_UNIFIED_VERSION: HOLD_MINUTES,
                   UNIFIED_VERSION: HOLD_MINUTES, FOUR_HOUR_MODEL: FOUR_HOUR_HOLD,
                   DAILY_PULLBACK_MODEL: DAILY_PULLBACK_HOLD}
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


def context(store, info, identity, signal_at, now, listings, *, peer_inputs=None):
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
                      session_end=bar['session_end'].isoformat(),checked_at=now.isoformat(),version=VERSION,
                      closed=bar['closed'].isoformat())
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
                observed = rows if peer==isin else peer_rows(db,peer,meta,
                    opening.tz_convert(zone).tz_localize(None),
                    slots[-1].tz_convert(zone).tz_localize(None),peer_inputs)
                observations = clean_quotes(observed,zone,available_bars=True)
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


def peer_rows(db,isin,identity,lower,upper,inputs,*,base='15m'):
    """Bind the exact peer prices used in market breadth, including absences."""
    rows=_rows(db,isin,identity,lower,upper,base=base)
    if inputs is not None:
        from paper_hourly import digest
        inputs['market:'+isin+':'+base]=(isin,dict(identity),str(lower),str(upper),base,digest(rows))
    return rows


def peer_input_current(db,proof):
    from paper_hourly import digest
    isin,identity,lower,upper,base,expected=proof
    return digest(_rows(db,isin,identity,lower,upper,base=base))==expected


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
        # Nonhourly signals require both the selected CORE trigger and a
        # V5 breakout/reversal; group, costs and bracket rules still apply.
        if engine.get('signal')!='BUY':
            return dict(action='WAIT',reason='V5_SELECTED_CORE_WAIT')
        from paper_v4 import decide as selected_interval
        outcome=selected_interval(engine,timeframe)
        if outcome['action']!='BUY':
            return outcome
        if outcome.get('setup') not in ('OPEN_RANGE','DAILY_REVERSAL'):
            return dict(action='WAIT',reason='V5_SELECTED_PATTERN_WAIT')
        return dict(outcome,model=MULTI_VERSION,reason='V5_'+outcome['setup'])
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
                stop=str(stop), target=str(target), hold_minutes=HOLD_MINUTES,
                signal_closed=bar.get('closed'))


def levels(reference, atr, *, target_atr=TARGET_ATR):
    return (reference - max(STOP_ATR*atr, reference*STOP_FLOOR),
            reference + max(target_atr*atr, reference*TARGET_FLOOR))

def _benchmark_group(info):
    from benchmarks import default_benchmark
    return info.get('benchmark_id', default_benchmark(info))


def daily_group(db, group):
    """Freeze the confirmed comparison group, including missing histories."""
    import json
    from paper_account import instrument_allowed
    members = {}
    if group is None:
        return members
    for isin, payload in db.execute('SELECT isin,payload FROM instruments ORDER BY isin'):
        info = json.loads(payload)
        if instrument_allowed(info) and _benchmark_group(info) == group:
            members[isin] = info
    return members


def daily_flags(frame, peers):
    """Indicators from GETTEX sessions known before the execution day."""
    import numpy as np
    from paper_v4 import features

    close = frame.close
    returns = close.pct_change(fill_method=None)
    last = close.iloc[-1]
    peak = frame.high.max()
    near = None
    if len(frame) == 252 and close.notna().sum() >= 240 and pd.notna(last) and pd.notna(peak):
        if not returns.abs().gt(.35).any():
            near = bool(last >= .95 * peak)
    quiet = None
    if near is not None:
        atr = features(frame).atr.div(close)
        recent, prior = atr.iloc[-6:-1], atr.iloc[-66:-6]
        if recent.notna().sum() == 5 and prior.notna().sum() == 60:
            quiet = bool(near and recent.median() < prior.median())

    relative = None
    if len(frame) >= 22 and len(peers) >= 4:
        sample = pd.DataFrame(peers, index=frame.index[-22:]).pct_change(fill_method=None)
        median = sample.median(axis=1).where(sample.count(axis=1).ge(4))
        moves = (np.log1p(returns.iloc[-22:]) - np.log1p(median)).iloc[1:]
        if moves.notna().all() and not returns.iloc[-21:].abs().gt(.35).any():
            path = moves.cumsum()
            relative = bool(path.iloc[-1] > path.iloc[:-1].max())
    return dict(near52=near, relative20=relative, quiet=quiet)


def steady_relative_growth(frame, peers):
    """Evaluate 55 closed relative returns, ignoring the six newest sessions."""
    import numpy as np

    if len(frame)<62 or len(peers)<4:
        return None
    window = frame.index[-62:]
    comparison = pd.DataFrame({isin:peer.close for isin,peer in peers.items()},index=window)
    peer_returns = comparison.pct_change(fill_method=None)
    peer_returns = np.log1p(peer_returns.where(peer_returns.abs().le(.35)))
    peer_median = peer_returns.median(axis=1).where(peer_returns.count(axis=1).ge(4))
    own = frame.close.reindex(window).pct_change(fill_method=None)
    path = (np.log1p(own.where(own.abs().le(.35)))-peer_median).shift(6).iloc[-55:].dropna()
    if len(path)<50:
        return None
    positive = path.clip(lower=0)
    total = positive.sum()
    return bool(path.sum()>.025 and path.gt(0).mean()>=.55 and total>0 and positive.max()/total<=.20)


def held_gap_window(identity, when):
    """Only the second closed 15m candle of a verified GETTEX session."""
    from timeframes import schedule

    clock = when.tz_convert(identity['timezone'])
    if identity.get('venue') != 'GETTEX' or (clock.hour, clock.minute // 15) != (8, 0):
        return None
    day = clock.tz_localize(None).normalize()
    last = min(day + pd.Timedelta(days=12), pd.Timestamp(f'{day.year}-12-31'))
    sessions = schedule(identity, day, last)
    if len(sessions) <= GAP_HOLD_SESSIONS or sessions.index[0] != day:
        return None
    quote_at = sessions.open.iloc[0] + pd.Timedelta(minutes=30)
    if not quote_at <= when < quote_at + pd.Timedelta(minutes=15):
        return None
    exit_at = sessions.open.iloc[GAP_HOLD_SESSIONS] + pd.Timedelta(minutes=30)
    return dict(entry_quote_at=quote_at.isoformat(), exit_at=exit_at.isoformat())


def held_gap_impulse(own):
    """Price, trend, and volume conditions of the held-gap entry."""
    from math import isfinite
    from paper_v4 import features

    if len(own) < 46 or own.close.tail(35).isna().any():
        return None
    last = features(own.tail(51)).iloc[-1]
    prior = float(last.prev_close)
    price = float(last.close)
    opening = float(last.open)
    atr = float(last.atr)
    vol_med = float(last.vol_med)
    volume = float(last.volume)
    trend = own.close.iloc[-51:-1].dropna()
    if not all(isfinite(v) and v > 0 for v in (prior, price, opening, atr, vol_med, volume)):
        return None
    if (len(trend) < 45 or prior <= trend.mean() or abs(price / prior - 1) > .35
            or last.volume < vol_med or not last.high > last.low >= prior
            or Decimal(str(price)) - Decimal(str(opening)) < Decimal(str(atr))
            or (price - last.low) / (last.high - last.low) < 2 / 3):
        return None
    gap = opening / prior - 1
    if not .006 <= gap <= .12:
        return None
    return dict(signal_close=price,opening=opening,previous_close=prior,
                daily_high=float(last.high),daily_low=float(last.low),gap=gap,
                body_atr=(price-opening)/atr,volume_ratio=volume/vol_med,atr=atr)


def held_gap_setup(own, peer_frames, previous_close_at, window):
    """Add the confirmed peer comparison to the completed own daily signal."""
    from math import isfinite
    from statistics import median

    impulse = held_gap_impulse(own)
    if impulse is None:
        return None
    peer_gaps = []
    for frame in peer_frames.values():
        peer_prior = frame.close.iloc[-2]
        peer_open = frame.open.iloc[-1]
        if not (isfinite(peer_prior) and isfinite(peer_open) and peer_prior > 0 and peer_open > 0):
            continue
        change = peer_open / peer_prior - 1
        if abs(change) <= .12:
            peer_gaps.append(change)
    if len(peer_gaps) < 4:
        return None
    peer_gap = median(peer_gaps)
    if impulse['gap'] - peer_gap < .007:
        return None
    return dict(window,**impulse,signal_closed=previous_close_at.isoformat(),
                signal_day=previous_close_at.tz_convert('Europe/Berlin').date().isoformat(),
                peer_gap_median=peer_gap,peer_count=len(peer_gaps))


def opening_gap_candidates(path, cfg, now):
    """Screen already admitted stocks on saved 1D data in the existing trading lane."""
    from pathlib import Path
    import sqlite3
    from eur_listings import stored_identity
    from paper_account import registered_instrument
    from timeframes import schedule

    zone = 'Europe/Berlin'
    day = now.tz_convert(zone).tz_localize(None).normalize()
    meta = dict(venue='GETTEX',timezone=zone,timeframe='1D')
    sessions = schedule(meta,day-pd.Timedelta(days=100),day)
    if len(sessions)<46 or sessions.index[-1]!=day:
        return []
    previous = sessions.index[:-1]
    start,end = str(previous[0].date()),str(previous[-1].date())+' 23:59:59'
    candidates = []
    with sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=.2) as db:
        db.execute('BEGIN')
        for isin,identity in cfg['identities'].items():
            if identity.get('venue')!='GETTEX' or identity.get('timezone')!=zone:
                continue
            info = registered_instrument(db,isin)
            if not info or not stored_identity(db,info,identity,'1D'):
                continue
            frame = clean_quotes(_rows(db,isin,identity,start,end,base='1D'),zone,available_bars=True)
            frame.index = frame.index.tz_convert(zone).tz_localize(None).normalize()
            if held_gap_impulse(frame.reindex(previous)) is not None:
                candidates.append(isin)
    return candidates


def held_gap_quote_blocker(gap, bar, now=None):
    quote_at = pd.Timestamp(gap['entry_quote_at'])
    if pd.Timestamp(bar.get('close_at')) != quote_at:
        return 'PAPER_V5_GAP_ENTRY_QUOTE'
    if now is not None and not quote_at <= now < quote_at + pd.Timedelta(minutes=15):
        return 'PAPER_V5_GAP_ENTRY_WINDOW'
    price = Decimal(str(bar['close']))
    signal = Decimal(str(gap['signal_close']))
    if (price <= Decimal(str(gap['previous_close']))
            or abs(price-signal) > GAP_MAX_DISPLACEMENT*Decimal(str(gap['atr']))):
        return 'PAPER_V5_GAP_LEVEL_OR_DISTANCE'
    return None


def portfolio_context(store, info, identity, when, timeframe, inputs, *, gap_window=None):
    """Bind the same closed daily window used by the tested entry policy."""
    import json
    from eur_listings import stored_identity
    from timeframes import schedule

    zone = identity['timezone']
    today = when.tz_convert(zone).date()
    sessions = schedule({**identity, 'timeframe':'1D'}, today-timedelta(days=430),
                        today-timedelta(days=1)).tail(252)
    if sessions.empty or sessions.close.iloc[-1] > when:
        return {}
    end = sessions.index[-1]
    peer_days = sessions.index[-62:] if timeframe=='1H' else sessions.index[-22:]

    def history(db, instrument, meta, days):
        isin = instrument['isin']
        store.read_cache.depend('instrument:'+isin)
        store.read_cache.depend('setting:gettex_chart:'+isin+':1D')
        if not stored_identity(db,instrument,meta,'1D'):
            return None
        store.read_cache.depend('market:'+isin+':1D')
        rows = peer_rows(db, isin, meta, str(days[0].date()), str(end.date())+' 23:59:59', inputs, base='1D')
        frame = clean_quotes(rows, zone, available_bars=True)
        frame.index = frame.index.tz_convert(zone).tz_localize(None).normalize()
        return frame.reindex(days)

    with store.connect() as db:
        own = history(db, info, identity, sessions.index)
        if own is None:
            return {}
        empty = daily_flags(own, {})
        if timeframe == '4H' and empty['near52'] is not True:
            return dict(empty, day=end.date().isoformat())
        benchmark = _benchmark_group(info)
        group = daily_group(db, benchmark)
        peers = {}
        for isin, instrument in group.items():
            if isin == info['isin']:
                continue
            key = 'gettex_chart:'+isin+':1D'
            store.read_cache.depend('instrument:'+isin)
            store.read_cache.depend('setting:'+key)
            row = db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            peer = json.loads(row[0]).get('identity',{}) if row else {}
            if any(peer.get(k)!=identity[k] for k in ('source','venue','currency','timezone')):
                continue
            frame = history(db, instrument, peer, peer_days)
            if frame is not None:
                peers[isin] = frame
    context = dict(daily_flags(own, {isin: frame.close for isin, frame in peers.items()}),
                   day=end.date().isoformat(), benchmark=benchmark, cohort=list(group))
    if timeframe=='1H':
        context['steady_relative'] = steady_relative_growth(own, peers)
    if gap_window is not None:
        gap = held_gap_setup(own, peers, sessions.close.iloc[-1], gap_window)
        if gap is not None:
            context['held_gap'] = gap
    return context


def confirmed_entry(engine, timeframe):
    """Apply the confirmed entry rules to the original V5 patterns."""
    if engine.get('signal') != 'BUY':
        return dict(action='WAIT', reason='V5_SELECTED_CORE_WAIT')
    prior = decide(engine, timeframe)
    if prior['action'] != 'BUY':
        return prior
    context = engine.get('v5_portfolio_context') or {}
    if timeframe=='1H' and context.get('steady_relative') is not True:
        reason = ('V5_STEADY_DAILY_DATA_WAIT' if context.get('steady_relative') is None
                  else 'V5_STEADY_GROWTH_WAIT')
        return dict(action='WAIT',reason=reason)
    if timeframe == '4H' and prior['setup'] != 'OPEN_RANGE':
        return dict(action='WAIT', reason='V5_PORTFOLIO_4H_BREAKOUT_ONLY')
    if timeframe == '4H' and context.get('near52') is not True:
        code = 'V5_PORTFOLIO_52W_WAIT' if context.get('near52') is False else 'V5_PORTFOLIO_DAILY_DATA_WAIT'
        return dict(action='WAIT', reason=code)
    if timeframe == '1D' and not (context.get('relative20') or context.get('quiet')):
        code = 'V5_PORTFOLIO_1D_STRENGTH_WAIT'
        if context.get('relative20') is None or context.get('quiet') is None:
            code = 'V5_PORTFOLIO_DAILY_DATA_WAIT'
        return dict(action='WAIT', reason=code)
    return dict(prior, model=UNIFIED_VERSION, daily_context=context)


def entry_decision(engine, timeframe, quote=None):
    decision = confirmed_entry(engine, timeframe)
    if decision['action'] == 'BUY' or timeframe != '1D':
        return decision
    context = engine.get('v5_portfolio_context') or {}
    gap = context.get('held_gap')
    if not gap:
        return decision
    stamp = pd.Timestamp(engine.get('timestamp'))
    if pd.isna(stamp):
        return decision
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert('Europe/Berlin')
    if stamp.date().isoformat() != gap['signal_day']:
        return decision
    bars = (quote or {}).get('bars', ())
    if not bars:
        return dict(action='WAIT', reason='PAPER_V5_GAP_ENTRY_QUOTE')
    blocker = held_gap_quote_blocker(gap, bars[-1])
    if blocker is not None:
        return dict(action='WAIT', reason=blocker)
    atr = Decimal(str(gap['atr']))
    stop, target = levels(Decimal(str(gap['signal_close'])),atr,target_atr=GAP_TARGET_ATR)
    return dict(action='BUY', reason='V5_HELD_GAP_1_ATR', setup=HELD_GAP,
                model=UNIFIED_VERSION, atr=gap['atr'], stop=str(stop), target=str(target),
                entry_level=gap['previous_close'], signal_closed=gap['signal_closed'],
                daily_context=context, gap_evidence=gap)


def priority_score(result, signal, intervals):
    """Compare only known signals and the actual quoted closing price."""
    tf = result['timeframe']
    if signal.get('model')==DAILY_PULLBACK_MODEL:
        score=Decimal(str(signal['pattern_score']))
        if not score.is_finite():
            raise ValueError('PAPER_V5_RANK_INPUT_INVALID')
        return (True,intervals.index(tf),-score,result['isin'])
    bar = result['execution_quote']['bars'][-1]
    inputs = signal['daily_context']
    price, level, atr, volume = (Decimal(str(x)) for x in
                                (bar['close'],signal['entry_level'],signal['atr'],bar['volume']))
    if not (all(x.is_finite() for x in (price,level,atr,volume)) and atr>0):
        raise ValueError('PAPER_V5_RANK_INPUT_INVALID')
    return (tf!='4H', intervals.index(tf), not inputs.get('near52'), not inputs.get('relative20'),
            max(Decimal(0),(price-level)/atr), -volume,result['isin'])

