"""Daily PAPER entries and a daily trailing stop for the long-term V1."""
from decimal import Decimal
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

MODEL = 'v1_weekly_daily_v1'
PROFILE_KEY = 'v1_entry_profile'
TRAIL_ATR = Decimal('3')
SETUPS = ('DAILY_20_SESSION_BREAKOUT', 'DAILY_MA20_REBOUND')
LOOKBACK = 26


def enabled(cfg):
    return cfg.get('strategy_mode', 'V1') == 'V1' and cfg.get(PROFILE_KEY) == MODEL


def closed_sessions(frame, identity, now, days):
    from timeframes import schedule
    from data_quality import GETTEX_DAILY_START

    local = now.tz_convert(identity['timezone']).tz_localize(None).normalize()
    first = local - pd.Timedelta(days=days)
    if identity['venue'] == 'GETTEX':
        first = max(first, pd.Timestamp(GETTEX_DAILY_START))
    sessions = schedule({**identity, 'timeframe': '1D'}, first, local)
    sessions = sessions.loc[sessions.close <= now]
    if frame is None or frame.empty:
        return None, sessions
    dates = frame.index
    if dates.tz is not None:
        dates = dates.tz_convert(identity['timezone']).tz_localize(None)
    dates = dates.normalize()
    if not dates.is_unique:
        return None, sessions
    return frame.set_axis(dates).reindex(sessions.index), sessions


def daily_setup(daily):
    """The setup uses only complete, contiguous, already-closed session bars."""
    if daily is None or len(daily) < LOOKBACK:
        return {'reason': 'V1_DAILY_HISTORY_WAIT'}
    bars = daily.tail(LOOKBACK)
    prices = bars[['open', 'high', 'low', 'close']]
    valid = prices.gt(0) & prices.lt(float('inf'))
    if (not valid.all().all() or not prices.high.ge(prices.max(axis=1)).all()
            or not prices.low.le(prices.min(axis=1)).all()):
        return {'reason': 'V1_DAILY_HISTORY_WAIT'}
    prior_close = prices.close.shift()
    atr = pd.concat((prices.high-prices.low, (prices.high-prior_close).abs(),
                     (prices.low-prior_close).abs()), axis=1).max(axis=1).tail(14).mean()
    if not 0 < atr < float('inf'):
        return {'reason': 'V1_DAILY_HISTORY_WAIT'}
    day = prices.iloc[-1]
    high = prices.high.iloc[-21:-1].max()
    average = prices.close.rolling(20).mean()
    recent = prices.iloc[-6:-1]
    rebound = (recent.low.le(average.iloc[-6:-1]).any()
               and day.close > max(average.iloc[-1], prices.high.iloc[-2], day.open)
               and average.iloc[-1] >= average.iloc[-6])
    setup = SETUPS[0] if day.close > high else SETUPS[1] if rebound else None
    return dict(setup=setup, signal_day=str(bars.index[-1].date()), signal_close=float(day.close),
                atr=float(atr), support=float(recent.low.min()),
                entry_level=float(high if setup == SETUPS[0]
                                  else max(average.iloc[-1], prices.high.iloc[-2])))


def weekly_rising(weekly):
    if weekly is None or len(weekly) < 31:
        return None
    from timeframes import endpoints
    ends = endpoints(weekly)
    dates = ends.tz_convert(weekly.attrs['timezone']).tz_localize(None).to_period('W-FRI')
    close = weekly.close.tail(40)
    tail = dates[-len(close):]
    if tail.hasnans or not tail.equals(pd.period_range(tail[0], tail[-1], freq='W-FRI')):
        return None
    if not (close.gt(0) & close.lt(float('inf'))).all():
        return None
    fast = close.ewm(span=10, adjust=False).mean()
    slow = close.ewm(span=30, adjust=False).mean()
    return dict(closed_at=ends[-1].isoformat(), close=float(close.iloc[-1]),
                ema10=float(fast.iloc[-1]), ema30=float(slow.iloc[-1]),
                rising=bool(close.iloc[-1] > fast.iloc[-1] > slow.iloc[-1]
                            and fast.iloc[-1] > fast.iloc[-2] and slow.iloc[-1] > slow.iloc[-2]))


def entry_session(identity, sessions, now):
    from timeframes import schedule

    day = now.tz_convert(identity['timezone']).tz_localize(None).normalize()
    calendar = schedule({**identity, 'timeframe': '1D'}, sessions.index[-1], day)
    if len(calendar) == 2 and calendar.open.iloc[-1] <= now < calendar.close.iloc[-1]:
        return dict(session_open=calendar.open.iloc[-1].isoformat(),
                    session_close=calendar.close.iloc[-1].isoformat())
    return {}


def context(daily_frame, weekly_frame, identity, now, *, held):
    from trading_context import closed_period_end

    daily, sessions = closed_sessions(daily_frame, identity, now, 60)
    evidence = daily_setup(daily)
    if 'reason' in evidence:
        return evidence
    signal_close = sessions.close.iloc[-1]
    evidence['signal_closed'] = signal_close.isoformat()
    if held:
        return evidence
    trend = weekly_rising(weekly_frame)
    if trend is None or pd.Timestamp(trend['closed_at']) != closed_period_end(identity, '1W', signal_close):
        return dict(evidence, reason='V1_WEEKLY_HISTORY_WAIT')
    evidence['weekly'] = trend
    if not trend['rising']:
        return dict(evidence, reason='V1_WEEKLY_TREND_WAIT')
    if evidence['setup'] is None:
        return dict(evidence, reason='V1_DAILY_SETUP_WAIT')
    window = entry_session(identity,sessions,now)
    return dict(evidence, **window) if window else dict(evidence, reason='V1_NEXT_SESSION_WAIT')


def screen(store, info, identity, now):
    """Skip full CORE only when exact native daily rows cannot make an entry."""
    from paper_v4 import _series

    if identity['source'] != 'gettex_chart':
        return None
    date = now.tz_convert(identity['timezone']).date()
    start = date-pd.Timedelta(days=60)
    with store.connect() as db:
        raw = _series(store,db,info,identity,'1D',f'{start} 00:00:00',f'{date} 23:59:59')
    daily,sessions = closed_sessions(raw,identity,now,60)
    evidence = daily_setup(daily)
    if 'reason' in evidence:
        return evidence
    if evidence['setup'] is None:
        return dict(evidence,reason='V1_DAILY_SETUP_WAIT')
    if not entry_session(identity,sessions,now):
        return dict(evidence,reason='V1_NEXT_SESSION_WAIT')
    return None


def decision(engine):
    evidence = engine.get('v1_long_term', {})
    if (not evidence.get('session_open') or evidence.get('setup') not in SETUPS
            or evidence.get('weekly', {}).get('rising') is not True):
        return dict(action='WAIT', reason=evidence.get('reason', 'V1_DAILY_HISTORY_WAIT'))
    return dict(evidence, action='BUY', reason=evidence['setup'], model=MODEL)


def initial_stop(signal, bar):
    closed = pd.Timestamp(bar['close_at'])
    if not pd.Timestamp(signal['session_open']) < closed <= pd.Timestamp(signal['session_close']):
        raise ValueError('PAPER_V1_QUOTE_OUTSIDE_SIGNAL_SESSION')
    price = Decimal(str(bar['close']))
    if price <= Decimal(str(signal['entry_level'])):
        raise ValueError('PAPER_V1_ENTRY_LEVEL_LOST')
    return min(price - 2*Decimal(str(signal['atr'])), Decimal(str(signal['support']))*Decimal('.995'))


def protection_plan(result, signal, size, now):
    from paper_protection import STRATEGY_VERSION, POLICY
    return dict(version=STRATEGY_VERSION, model=MODEL, strategy_mode='V1',
        identity=result['identity'].copy(), signal_tf='1D', quote_tf=result['execution_quote']['timeframe'],
        stop=size['stop'], target=None, entry=size['entry'], atr=str(signal['atr']),
        planned_quantity=str(size['quantity']), estimated_stop_risk=size['estimated_risk'],
        risk_is_estimate=True, armed_at=now, trigger=POLICY['trigger'], fill=POLICY['fill'])


def trailing_level(result, entry, close_at):
    daily = result['execution_context']['trading_engine'].get('v1_long_term', {})
    day = daily.get('signal_closed')
    if not day or not pd.Timestamp(entry['at']) < pd.Timestamp(day) < pd.Timestamp(close_at):
        return None
    close, atr = (Decimal(str(daily[k])) for k in ('signal_close', 'atr'))
    if not all(v.is_finite() and v > 0 for v in (close, atr)):
        return None
    return close - TRAIL_ATR*atr


def benchmark(info):
    from benchmarks import CATALOG, default_benchmark
    code = default_benchmark(info)
    # Match the quote currency. The broad European price index is the EUR
    # comparator when the issuer's own index is only stored in USD.
    return CATALOG[code] if code in CATALOG and CATALOG[code]['currency'] == 'EUR' else CATALOG['INDEX:STOXX600']


def relative_strength(stock, index, cutoff):
    """Common, closed endpoints, as known at the signal; no filled gaps."""
    common = pd.concat([stock.rename('stock'), index.rename('index')], axis=1,
                       join='inner').sort_index().dropna()
    common = common.loc[:pd.Timestamp(cutoff).normalize()]
    if common.empty or common.index[-1] != pd.Timestamp(cutoff).normalize():
        return {}
    last = common.iloc[-1]
    scores = {}
    for months in (3, 6):
        begin = last.name-pd.DateOffset(months=months)
        prior = common.loc[:begin]
        if not prior.empty and begin-prior.index[-1] <= pd.Timedelta(days=7):
            relative = (last/prior.iloc[-1]-1)*100
            scores[f'{months}m_pp'] = round(float(relative.stock-relative['index']), 6)
    return scores


def rank_inputs(db, info, identity, now, index_cache):
    from benchmarks import identity as index_identity
    from eur_listings import stored_identity
    from paper_v5 import _rows, clean_quotes

    if not stored_identity(db, info, identity, '1D'):
        return {}
    code = benchmark(info)
    meta = index_identity(code)
    values = []
    for isin, listing in ((info['isin'], identity), (code['isin'], meta)):
        cached = isin in index_cache
        if cached:
            values.append(index_cache[isin])
            continue
        zone = listing['timezone']
        end = now.tz_convert(zone).date()
        start = end-pd.Timedelta(days=200)
        rows = _rows(db, isin, listing, f'{start} 00:00:00', f'{end} 23:59:59', base='1D')
        frame = clean_quotes(rows, zone, available_bars=True)
        close, sessions = closed_sessions(frame, listing, now, 200)
        if close is None:
            return {}
        item = (close.close, sessions.index[-1])
        if isin == code['isin']:
            index_cache[isin] = item
        values.append(item)
    stock, day = values[0]
    scores = relative_strength(stock, values[1][0], day)
    return dict(scores, benchmark=code['isin'], currency='EUR', signal_day=str(day.date())) if scores else {}


def ranked_isins(path, identities, ids, now):
    """Once per scheduled refresh, rank the observable candidates off Tk."""
    from paper_account import independent_portfolios,ledger,registered_instrument

    uri = Path(path).resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        account='V1' if independent_portfolios(db) is not None else None
        held = {p['isin'] for p in ledger(db,account).get('positions', []) if Decimal(p['quantity']) > 0}
        cache = {}
        scores = {}
        for isin in ids:
            try:
                info = registered_instrument(db, isin)
                scores[isin] = rank_inputs(db, info, identities[isin], now, cache)
            except (ValueError, TypeError, ArithmeticError):
                continue
    def priority(isin):
        evidence = scores.get(isin, {})
        return (isin not in held, -evidence.get('3m_pp', float('-inf')),
                -evidence.get('6m_pp', float('-inf')))
    return sorted(ids, key=priority), scores

