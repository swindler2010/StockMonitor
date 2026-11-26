"""Frozen V2 30m research feature functions, extracted without changing predicates.
Source: owner research 23–24 November 2026; no training or orders in this module.
"""
from dataclasses import dataclass
from types import SimpleNamespace
import numpy as np
import pandas as pd
FEATURES = ('relative_30m','relative_60m','own_120m','own_30m','turn','close_location','range','activity','friction','atr_fraction','session_elapsed','us_session','peer_60m')
INTERIM = ('interim_move','interim_relative','interim_activity')
EXITS = {'step30':2,'lock75':5}
ENTRIES = ('pullback','trend_room','recovery','continuation')
PATTERNS = ('false_break','gap_recovery')
RANGE = ('1970-01-01','9999-12-31')
DIAGNOSTIC_DAY = '9999-12-31'
PULLBACK_OFFSET_ATR=.65
EXTRA=('floor_margin','interim_move','drift_90','falling_closes','lower_lows','pressure','drawdown','interim_relative','own_120m')
@dataclass(frozen=True)
class Policy:
    entry: str
    band: str
    exit: str
    @property
    def name(self): return '/'.join((self.entry,self.band,self.exit))
i=SimpleNamespace(Policy=Policy)
m=SimpleNamespace(RANGE=RANGE,EXITS=EXITS)


def half_hours(tape, grid):
    native=tape.reindex(grid.index)
    previous=native.shift()
    even = grid.minutes.mod(30).eq(0)
    bars=pd.DataFrame({
        'open':previous.open,
        'high':pd.concat([native.high,previous.high],axis=1).max(axis=1,skipna=False),
        'low':pd.concat([native.low,previous.low],axis=1).min(axis=1,skipna=False),
        'close':native.close,
        'volume':native.volume+previous.volume,
    }).loc[even]
    bars=bars.where(bars.notna().all(axis=1))
    bars['ret']=np.log(bars.close/bars.open)
    prior=bars.close.shift()
    tr=pd.concat([bars.high-bars.low,(bars.high-prior).abs(),(bars.low-prior).abs()],axis=1).max(axis=1,skipna=False)
    bars['atr']=tr.shift().rolling(16,min_periods=12).median()
    bars['risk_move']=bars.ret.shift().abs().rolling(16,min_periods=12).median().clip(lower=.0007)
    bars['vol_ratio']=bars.volume/bars.volume.shift().rolling(16,min_periods=12).median().where(lambda s:s>0)
    return native,bars


def sample_features(s, grid, tapes):
    returns = pd.DataFrame({key: np.log(tape.bars.close/tape.bars.close.shift(2))
                            for key, tape in tapes.items()})
    impulses = pd.DataFrame({key: tape.bars.ret for key, tape in tapes.items()})
    days = grid.day.reindex(returns.index)
    minute = grid.minutes.reindex(returns.index)
    us_hour = returns.index.tz_convert('America/New_York')
    us_minute = us_hour.hour*60+us_hour.minute
    us_cash = (us_minute >= 570) & (us_minute < 960)
    sections = []
    for key, tape in tapes.items():
        bars = tape.bars
        positive_atr = bars.atr.where(bars.atr.gt(0))
        atr_fraction = positive_atr/bars.close
        others = returns.drop(columns=key)
        market = others.median(axis=1).where(others.notna().sum(axis=1)>=12)
        others30 = impulses.drop(columns=key)
        market30 = others30.median(axis=1).where(others30.notna().sum(axis=1)>=12)
        location = (bars.close-bars.low)/(bars.high-bars.low).where(bars.high>bars.low)
        feat = pd.DataFrame({
            'relative_30m': (bars.ret-market30)/atr_fraction,
            'relative_60m': (returns[key]-market)/atr_fraction,
            'own_120m': np.log(bars.close/bars.close.shift(4))/atr_fraction,
            'own_30m': bars.ret/atr_fraction,
            'turn': (bars.ret-bars.ret.shift())/atr_fraction,
            'close_location': location,
            'range': (bars.high-bars.low)/positive_atr,
            'activity': np.log(bars.vol_ratio.where(bars.vol_ratio>0)),
            'friction': (2*s.FEE/(s.base.MAX_SHARES*bars.close)+2*s.HALF_SPREAD)/atr_fraction,
            'atr_fraction': np.log(atr_fraction),
            'session_elapsed': minute/900.,
            'us_session': us_cash.astype(float),
            'peer_60m': market,
            'reference': bars.close,
            'atr': bars.atr,
            'day': days,
            'isin': key,
        })
        eligible = (minute.ge(150) & days.eq(days.shift(4)) & ~days.isin(s.HALT_DAYS)
                    & bars.atr.gt(0) & bars.risk_move.notna() & bars.vol_ratio.ge(.85)
                    & days.between(RANGE[0], DIAGNOSTIC_DAY))
        sample = feat.loc[eligible].replace([np.inf, -np.inf], np.nan).dropna()
        sections.append(sample)
    frame = pd.concat(sections).sort_index(kind='stable')
    frame['slot'] = grid.index.get_indexer(frame.index)
    return frame


def interim_features(s, grid, tapes, frame, cutoff):
    # The first 15m after the signal closes 15m before the entry quote.
    # No information from the quarter used for execution enters this filter.
    prices=pd.DataFrame({key:tape.close for key,tape in tapes.items()},index=grid.index)
    returns=np.log(prices/prices.shift())
    expected=frame.index+s.Q
    array=grid.index.get_indexer(expected)
    same_session=grid.day.reindex(expected).to_numpy()==frame.day.to_numpy()
    available=(array>=0)&same_session&(expected<=cutoff)
    result=pd.DataFrame(np.nan,index=range(len(frame)),columns=INTERIM)
    for key,tape in tapes.items():
        selected=np.flatnonzero(frame['isin'].eq(key).to_numpy()&available)
        locations=array[selected]
        rows=frame.iloc[selected]
        peers=returns.iloc[locations].drop(columns=key)
        median=peers.median(axis=1).where(peers.notna().sum(axis=1)>=12).to_numpy()
        price,volume=tape.close[locations],tape.volume[locations]
        move=(price-rows.reference.to_numpy())/rows.atr.to_numpy()
        relative=move-median*rows.reference.to_numpy()/rows.atr.to_numpy()
        result.iloc[selected]=np.column_stack((move,relative,np.log1p(volume)))
    return result.to_numpy()


def basis(values):
    # Modest quadratic basis. No ticker, earnings calendar, or exact half-hour ID.
    return np.column_stack((np.ones(len(values)), values, values**2,
                            values[:,0]*values[:,8], values[:,1]*values[:,11],
                            values[:,2]*values[:,3], values[:,3]*values[:,10]))


def enrich(tapes,frame,interim):
    features=frame[['isin','atr','own_120m','day']].copy()
    features['interim_move']=interim[:,0]
    features['interim_relative']=interim[:,1]
    storage={name:np.full(len(frame),np.nan) for name in EXTRA[0:1]+EXTRA[2:7]}
    for code,tape in tapes.items():
        selected=np.flatnonzero(frame['isin'].eq(code).to_numpy())
        rows=frame.iloc[selected]
        bars=tape.bars
        prior=bars.close.shift()
        prior_floor=bars.low.shift().rolling(3,min_periods=3).min()
        recent_high=bars.high.rolling(4,min_periods=4).max()
        falling=bars.close.lt(prior).where(bars.close.notna()&prior.notna()).rolling(3,min_periods=3).sum()
        descending=bars.low.lt(bars.low.shift()) & bars.low.shift().lt(bars.low.shift(2))
        observed=frame.reference.to_numpy()[selected]+interim[selected,0]*rows.atr.to_numpy()
        at=rows.index
        atr=rows.atr.to_numpy()
        down_volume=bars.volume.where(bars.close.lt(prior),0.).rolling(3,min_periods=3).sum()
        total_volume=bars.volume.rolling(3,min_periods=3).sum()
        storage['floor_margin'][selected]=(observed-prior_floor.reindex(at).to_numpy())/atr
        storage['drift_90'][selected]=((bars.close-bars.close.shift(3))/bars.atr).reindex(at).to_numpy()
        storage['falling_closes'][selected]=falling.reindex(at).to_numpy()
        observed_lows=bars.low.rolling(3,min_periods=3).count().eq(3)
        storage['lower_lows'][selected]=descending.where(observed_lows).reindex(at).to_numpy(dtype=float)
        storage['pressure'][selected]=(down_volume/total_volume).reindex(at).to_numpy()
        storage['drawdown'][selected]=(recent_high.reindex(at).to_numpy()-observed)/atr
    return features.assign(**storage)


def enough_room(s, grid, tapes, frame):
    """Compare confirmed local highs against the worst allowed entry, after costs."""
    result = np.full(len(frame), np.nan)
    resistance = np.full(len(frame), np.nan)
    cleared_peak = np.zeros(len(frame), dtype=bool)
    for code, tape in tapes.items():
        selected = np.flatnonzero(frame['isin'].eq(code).to_numpy())
        rows = frame.iloc[selected]
        at = rows.index
        ref, atr = rows.reference.to_numpy(), rows.atr.to_numpy()
        highs = tape.bars.high
        days = grid.day.reindex(highs.index)
        # A central high is usable only after the confirming bar closes.
        previous = highs.shift()
        pivot = previous.where(previous.gt(highs.shift(2)) & previous.ge(highs)
                               & days.eq(days.shift(2)))
        pivots = np.column_stack([pivot.shift(i).where(days.eq(days.shift(i))).reindex(at)
                                  for i in range(8)])
        overhead = np.where(pivots>ref[:,None],pivots,np.inf).min(axis=1)
        overhead[~np.isfinite(overhead)] = np.nan
        complete = pd.concat([highs.shift(i).where(days.eq(days.shift(i))) for i in range(1,9)],
                             axis=1).reindex(at).to_numpy()
        previous_peak = np.max(complete,axis=1)
        cleared_peak[selected] = np.isfinite(previous_peak)&(ref>=previous_peak)
        buy = ref*(1+s.HALF_SPREAD)
        expected_risk = buy-(ref-np.maximum(1.15*atr,.0025*ref))*(1-s.HALF_SPREAD)
        amount = np.minimum.reduce((np.full(len(ref),s.base.MAX_SHARES),
            np.floor((s.CASH*s.base.POLICY['allocation']-s.FEE)/buy),
            np.floor((s.CASH*s.base.POLICY['early_risk']-2*s.FEE)/expected_risk)))
        breakeven = (buy+2*s.FEE/np.where(amount>=1,amount,np.nan))/(1-s.HALF_SPREAD)
        result[selected] = (overhead-breakeven)/atr
        resistance[selected] = overhead
    return result, resistance, cleared_peak


def entry_patterns(s, grid, tapes, frame, cutoff):
    hits = {pattern: np.zeros(len(frame), dtype=bool) for pattern in PATTERNS}
    levels = {pattern: np.full(len(frame), np.nan) for pattern in PATTERNS}
    exact_quotes = np.zeros(len(frame), dtype=bool)
    first_prices = np.full(len(frame), np.nan)
    next_quarter = frame.index + s.Q
    entry_due = frame.index + 2 * s.Q
    interim_slots = grid.index.get_indexer(next_quarter)
    entry_slots = grid.index.get_indexer(entry_due)
    usable = ((interim_slots >= 0) & (entry_slots >= 0) & (entry_due <= cutoff)
              & (grid.day.reindex(entry_due).to_numpy() == frame.day.to_numpy()))
    near_close = grid.session_close.sub(grid.index.to_series()).le(pd.Timedelta(hours=1))
    sessions = grid.day.unique()
    delayed_prices = np.full(len(frame), np.nan)
    for code, tape in tapes.items():
        selected = np.flatnonzero(frame['isin'].eq(code).to_numpy())
        rows = frame.iloc[selected]
        bars = tape.bars
        session = grid.day.reindex(bars.index)
        minutes = grid.minutes.reindex(bars.index)
        floor = bars.low.shift().rolling(4).min().where(session.eq(session.shift(4)))
        at_signal = bars.reindex(rows.index)
        floor = floor.reindex(rows.index).to_numpy()
        atr = rows.atr.to_numpy()
        price = rows.reference.to_numpy()
        started = bars.loc[minutes.eq(30), ['open', 'high']].copy()
        started.index = session.loc[started.index]
        started = started.reindex(rows.day)
        day_open = started.open.to_numpy()
        opening_high = started.high.to_numpy()
        last_quote = pd.Series(tape.close, index=grid.index).where(near_close & (tape.volume > 0))
        previous_close = last_quote.groupby(grid.day).last().reindex(sessions).shift().reindex(rows.day).to_numpy()
        opening_gap = previous_close - day_open
        minimum_gap = np.maximum(.003 * previous_close, .35 * atr)
        preceding_bar = bars.close.shift().reindex(rows.index).to_numpy()

        hits['false_break'][selected] = ((at_signal.low.to_numpy() <= floor - .10 * atr)
                                         & (price >= floor + .05 * atr)
                                         & rows.close_location.ge(.55).to_numpy())
        hits['gap_recovery'][selected] = ((opening_gap >= minimum_gap)
                                          & (preceding_bar <= opening_high)
                                          & (price > opening_high)
                                          & grid.minutes.reindex(rows.index).le(330).to_numpy())
        levels['false_break'][selected] = floor
        levels['gap_recovery'][selected] = opening_high
        positions = selected[usable[selected]]
        first = interim_slots[positions]
        later = entry_slots[positions]
        exact_quotes[positions] = (tape.volume[first] > 0) & (tape.volume[later] > 0)
        first_prices[positions] = tape.close[first]
        delayed_prices[positions] = tape.close[later]

    held = {pattern: (hits[pattern] & exact_quotes
                      & (first_prices >= levels[pattern]) & (delayed_prices >= levels[pattern]))
            for pattern in PATTERNS}
    period = frame.day.between(*m.RANGE).to_numpy()
    counts = {pattern: dict(signals=int((hits[pattern] & period).sum()),
                            held=int((held[pattern] & period).sum()),
                            signal_days=int(frame.loc[hits[pattern] & period, 'day'].nunique()),
                            signal_stocks=int(frame.loc[hits[pattern] & period, 'isin'].nunique()))
              for pattern in PATTERNS}
    anchor = frame.reference.to_numpy() - .30 * frame.atr.to_numpy()
    policies = {i.Policy(pattern, 'any', exit_rule): held[pattern]
                for pattern in PATTERNS for exit_rule in m.EXITS}
    return (frame, dict.fromkeys(PATTERNS, anchor), policies, {}, {}, {}), levels, counts
