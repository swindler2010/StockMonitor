"""Individual V5 compression entries on closed, session-aligned 4H candles."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal

import pandas as pd

MODEL = "v5_four_hour_compression_v1"
PROFILE_KEY = "v5_four_hour_profile"
INDIVIDUAL_KEY = "v5_four_hour_individual"
INDIVIDUAL_WINDOW_WAIT = "V5_4H_INDIVIDUAL_WINDOW_WAIT"
SETUP = "COMPRESSION_4H"
ENTRY_RULE = "v5_4h_recovery_weighted_entry_v1"
RECLAIM_KEY = "v5_4h_reclaim_enabled"
RECLAIM_RULE = "v5_4h_quote_reclaim_v1"
RECLAIM_EXCLUSIONS = frozenset({"US5951121038"})  # Micron keeps the original entry.
LEVEL_LOST = "PAPER_V5_ENTRY_LEVEL_LOST"
ENTRY_TOO_FAR = "PAPER_V5_4H_ENTRY_TOO_FAR"
RECLAIM_DATA_WAIT = "V5_4H_RECLAIM_FIRST_QUOTE_UNCONFIRMED"
RECLAIM_EARLIER_WAIT = "V5_4H_RECLAIM_EARLIER_QUOTE_ELIGIBLE"
RECLAIM_BUY = "V5_4H_QUOTE_RECLAIM_BUY"
HOLD_MINUTES = 720
TARGET_ATR = Decimal("3.0")
WEEKLY_RULE = "v5_weekly_ema10_30_v1"
RECENT_LISTING_RULE = "v5_weekly_ema10_20_recent_listing_v1"
# GETTEX listing notice (page 86), not inferred from the first stored candle.
# https://www.gettex.de/fileadmin/bulletin/bekanntmachungen.gtx.20260615.pdf#page=86
GETTEX_LISTING_START = {"US84615Q1031": "2026-06-12"}
WEEKLY_HISTORY_START = "2023-07-01"
WEEKLY_UNAVAILABLE = "V5_4H_WEEKLY_DATA_WAIT"
WEEKLY_NOT_RISING = "V5_4H_WEEKLY_TREND_WAIT"
RECOVERY_UNAVAILABLE = "V5_4H_DAILY_RECOVERY_DATA_WAIT"
WEIGHTED_UNAVAILABLE = "V5_4H_WEIGHTED_DATA_WAIT"
WEIGHTED_PRICE_WAIT = "V5_4H_WEIGHTED_PRICE_WAIT"
QUARTER = pd.Timedelta(minutes=15)
FOUR_HOURS = pd.Timedelta(hours=4)


def enabled(cfg):
    return cfg.get("strategy_mode") == "AGGRESSIVE" and cfg.get(PROFILE_KEY) == MODEL


def reclaim_enabled(cfg, isin):
    identity = cfg.get("identities", {}).get(isin, {})
    return (enabled(cfg) and cfg.get(RECLAIM_KEY, True) is True
            and isin not in RECLAIM_EXCLUSIONS
            and identity.get("venue") == "GETTEX" and identity.get("currency") == "EUR")


def valid_close(value):
    try:
        stamp = time.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return stamp.strftime('%H:%M') == value and stamp.minute % 15 == 0


def valid_hold(minutes):
    return type(minutes) is int and minutes > 0 and minutes % 15 == 0


def validate_individual_rules(rules, identities):
    if not isinstance(rules, dict) or not rules.keys() <= identities.keys():
        raise ValueError("PAPER_V5_INDIVIDUAL_RULES_INVALID")
    for rule in rules.values():
        if not isinstance(rule, dict) or not rule or rule.keys() - {"hold_by_close", "allowed_closes"}:
            raise ValueError("PAPER_V5_INDIVIDUAL_RULES_INVALID")
        holds = rule.get("hold_by_close", {})
        allowed = rule.get("allowed_closes")
        if (not isinstance(holds, dict)
                or any(not valid_close(clock) or not valid_hold(minutes) for clock, minutes in holds.items())
                or (allowed is not None and (not isinstance(allowed, list) or not allowed
                    or not all(valid_close(clock) for clock in allowed)
                    or len(set(allowed)) != len(allowed) or holds.keys() - set(allowed)))):
            raise ValueError("PAPER_V5_INDIVIDUAL_RULES_INVALID")


def individual_decision(cfg, result, signal):
    """An individual rule can qualify an already valid compression signal."""
    if signal["action"] != "BUY":
        return signal
    policy = cfg.get(INDIVIDUAL_KEY, {})
    isin = result["isin"]
    rule = policy.get("rules", {}).get(isin)
    if not rule:
        return signal
    clock = pd.Timestamp(signal["quote_end"]).tz_convert(result["identity"]["timezone"]).strftime("%H:%M")
    minutes = rule.get("hold_by_close", {}).get(clock, HOLD_MINUTES)
    profile = dict(revision=policy["revision"], isin=isin, close_window=clock, hold_minutes=minutes)
    allowed = rule.get("allowed_closes")
    if allowed is not None and clock not in allowed:
        return dict(action="WAIT", reason=INDIVIDUAL_WINDOW_WAIT, individual_4h=profile)
    return dict(signal, hold_minutes=minutes, individual_4h=profile)


def position_minutes(entry):
    receipt = entry.get("smart_eligibility", {})
    profile = receipt.get("entry_evidence", {}).get("individual_4h")
    if profile is None:
        return HOLD_MINUTES
    try:
        closes = datetime.fromisoformat(entry["bar_close_at"])
        zone = ZoneInfo(entry["identity"]["timezone"])
        matches = (
            receipt["strategy_model"] == MODEL and entry["timeframe"] == "4H"
            and profile["isin"] == entry["isin"] and valid_hold(profile["hold_minutes"])
            and closes.tzinfo is not None
            and profile["close_window"] == closes.astimezone(zone).strftime("%H:%M")
            and isinstance(profile["revision"], str) and bool(profile["revision"].strip())
        )
    except (KeyError, TypeError, ValueError):
        matches = False
    if not matches:
        raise ValueError("PAPER_PROTECTION_BINDING_CHANGED")
    return profile["hold_minutes"]


def compression(frame):
    """Vectorized causal reference used with the normal 4H frame."""
    quality = frame.coverage_complete
    c, h, l = (frame[k].where(quality) for k in ("close", "high", "low"))
    previous = c.shift()
    tr = pd.concat((h-l, (h-previous).abs(), (l-previous).abs()), axis=1).max(axis=1)
    atr = tr.rolling(16, min_periods=10).mean().where(quality)
    ceiling = h.shift().rolling(5, min_periods=3).max()
    floor = l.shift().rolling(5, min_periods=3).min()
    median = frame.volume.where(quality).shift().rolling(16, min_periods=8).median()
    relative = frame.volume.div(median.where(median > 0))
    location = (c-l).div((h-l).where(h > l))
    mask = (quality & atr.gt(0) & c.gt(frame.open) & location.ge(.57)
            & relative.ge(.85) & ((c-frame.open)/atr).ge(.10) & previous.le(ceiling)
            & c.gt(ceiling) & (ceiling-floor).le(3*atr) & ceiling.gt(0))
    return pd.DataFrame({"eligible": mask, "atr": atr, "entry_level": ceiling, "relative_volume": relative},
                        index=frame.index)


def opening_window(calendar, now, *, reclaim=False):
    """Locate the preceding signal and the current scheduled execution quote."""
    if len(calendar) < 2:
        return None
    session = calendar.iloc[-1]
    if not session.open <= now < session.close:
        return None
    opens = pd.date_range(session.open, session.close, freq=FOUR_HOURS, inclusive="left")
    index = opens.searchsorted(now, side="right") - 1
    opening = opens[index]
    quarters = int((now - opening) // QUARTER) if reclaim else 1
    if not 1 <= quarters <= 4:
        return None
    quote_end = opening + quarters * QUARTER
    if not quote_end <= now < min(quote_end+QUARTER, session.close):
        return None
    if index:
        signal_start, signal_end = opens[index-1], opening
    else:
        prior = calendar.iloc[-2]
        signal_start = pd.date_range(prior.open, prior.close, freq=FOUR_HOURS, inclusive="left")[-1]
        signal_end = prior.close
    return signal_start, signal_end, opening, quote_end


def weekly_trend(sessions, close, listing_start=None):
    """Apply the EMA rules to complete, session-aligned weeks."""
    weeks = sessions.index.to_period("W-FRI")
    counts = sessions.close.groupby(weeks).size()
    observed = close.reindex(sessions.index).groupby(weeks).agg(["last", "count"])
    weekly = observed["last"].where(observed["count"].eq(counts))
    if listing_start is not None:
        first_sessions = sessions.index.to_series().groupby(weeks).first()
        weekly = weekly.loc[first_sessions.ge(pd.Timestamp(listing_start))]
    if weekly.tail(2).count() != 2:
        return None
    rule, slow_span = WEEKLY_RULE, 30
    if weekly.tail(40).count() < 30:
        # EMA20 needs two valid values to test its direction. Every full week
        # since a verified new listing must be present for this exception.
        if listing_start is None or not 21 <= len(weekly) < 30 or weekly.isna().any():
            return None
        rule, slow_span = RECENT_LISTING_RULE, 20
    fast = weekly.ewm(span=10, adjust=False, min_periods=10, ignore_na=True).mean()
    slow = weekly.ewm(span=slow_span, adjust=False, min_periods=slow_span, ignore_na=True).mean()
    previous_fast, previous_slow = fast.iloc[-2], slow.iloc[-2]
    if pd.isna(previous_fast) or pd.isna(previous_slow):
        return None
    price, ema10, ema_slow = weekly.iloc[-1], fast.iloc[-1], slow.iloc[-1]
    evidence = dict(rule=rule, closed_at=sessions.close.iloc[-1].isoformat(),
                    rising=bool(price > ema10 > ema_slow and ema10 > previous_fast and ema_slow > previous_slow),
                    close=float(price), ema10=float(ema10))
    evidence[f"ema{slow_span}"] = float(ema_slow)
    if rule == RECENT_LISTING_RULE:
        evidence.update(listing_start=listing_start, complete_weeks=len(weekly))
    return evidence


def daily_recovery(closes):
    """A just-closed recovery from a pullback in the previous five sessions."""
    recent = closes.tail(25)
    if len(recent) < 25 or recent.isna().any():
        return None
    average = recent.rolling(20, min_periods=20).mean()
    previous = recent.iloc[-6:-1]
    price, highest = recent.iloc[-1], previous.max()
    sma20, earlier = average.iloc[-1], average.iloc[-6]
    pullback = previous.le(average.iloc[-6:-1]).any()
    return dict(confirmed=bool(price > highest and sma20 >= earlier and pullback),
                day=recent.index[-1].date().isoformat(), close=float(price),
                previous_five_high=float(highest), sma20=float(sma20),
                sma20_five_sessions_ago=float(earlier), pullback=bool(pullback))


def weekly_context(store, db, info, identity, signal_closed, daily_inputs, *, include_recovery=False):
    from paper_v4 import _series
    from timeframes import schedule
    from trading_context import closed_period_end

    # The tested filter uses the week closed strictly before the signal.
    cutoff = closed_period_end(identity, "1W", signal_closed - pd.Timedelta(nanoseconds=1))
    if cutoff is None:
        return None
    zone = identity["timezone"]
    weekly_day = cutoff.tz_convert(zone).date()
    signal_day = signal_closed.tz_convert(zone).date()
    last = max(weekly_day, signal_day-timedelta(days=1)) if include_recovery else weekly_day
    sessions = schedule({**identity, "timeframe": "1D"}, WEEKLY_HISTORY_START, last)
    raw = _series(store, db, info, identity, "1D", WEEKLY_HISTORY_START,
                  f"{last} 23:59:59", daily_inputs)
    if raw is None or raw.empty:
        return None
    closes = raw.close.set_axis(raw.index.tz_convert(zone).tz_localize(None).normalize())
    if not closes.index.is_unique:
        return None
    listing = (GETTEX_LISTING_START.get(info["isin"])
               if identity["venue"] == "GETTEX" and identity["currency"] == "EUR" else None)
    trend = weekly_trend(sessions.loc[:str(weekly_day)], closes, listing)
    if trend is not None and include_recovery and not trend["rising"]:
        earlier = sessions.index[sessions.index < pd.Timestamp(signal_day)]
        trend["daily_recovery"] = daily_recovery(closes.reindex(earlier))
    return trend


def trend_watch(trend):
    """Small read-only description of the current higher-timeframe entry gate."""
    recovery = (trend or {}).get("daily_recovery")
    if trend is None:
        state = WEEKLY_UNAVAILABLE
    elif trend["rising"]:
        state = "WEEKLY_RISING"
    elif recovery is None:
        state = RECOVERY_UNAVAILABLE
    elif recovery["confirmed"]:
        state = "DAILY_RECOVERY"
    else:
        state = WEEKLY_NOT_RISING
    return dict(state=state, week_closed=(trend or {}).get("closed_at"),
                recovery_day=(recovery or {}).get("day"))


def weighted_entry_context(sessions, raw, opening):
    """Use only the eight scheduled quarters before the execution candle."""
    previous = [
        pd.date_range(session.open, min(session.close, opening), freq=QUARTER, inclusive="left")
        for session in sessions.itertuples() if session.open < opening
    ]
    expected = pd.DatetimeIndex([stamp for session in previous for stamp in session][-8:])
    if len(expected) != 8:
        return None
    rows = raw.reindex(expected)
    supported = rows.loc[rows.volume.gt(0) & rows.close.notna()]
    if len(supported) < 6:
        return None
    weights = [Decimal(str(v)) for v in supported.volume]
    weighted = sum(Decimal(str(c))*v for c,v in zip(supported.close,weights)) / sum(weights)
    return dict(price=str(weighted), positive_bars=len(supported),
                first_open=expected[0].isoformat(), last_open=expected[-1].isoformat())


def entry_window(identity, now, *, reclaim=False):
    from timeframes import schedule

    local = now.tz_convert(identity["timezone"]).date()
    calendar = schedule({**identity, "timeframe": "1D"}, local-timedelta(days=25), local).tail(9)
    return calendar, opening_window(calendar, now, reclaim=reclaim)


def context(store, info, identity, engine_stamp, now, peer_inputs, daily_inputs, *, reclaim=False):
    """Calculate own closed bars in the existing readonly compute worker."""
    if not engine_stamp:
        return {"reason": "V5_4H_SIGNAL_UNAVAILABLE"}
    calendar, window = entry_window(identity, now, reclaim=reclaim)
    if window is None:
        return {"reason": "V5_4H_WAIT_FIRST_QUOTE"}
    stamp = pd.Timestamp(engine_stamp)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(identity["timezone"])
    if stamp != window[0]:
        return {"reason": "V5_4H_SIGNAL_CANDLE_CHANGED"}
    return closed_setup(store, info, identity, calendar, window, peer_inputs, daily_inputs)


def closed_setup(store, info, identity, cal, window, peer_inputs, daily_inputs):
    from paper_v4 import _series, intraday

    start, closed, opening, quote_end = window
    zone = identity["timezone"]
    first = cal.open.iloc[0].tz_convert(zone).tz_localize(None)
    retry = quote_end > opening + QUARTER
    upper = quote_end - 2 * QUARTER if retry else closed - QUARTER
    last = upper.tz_convert(zone).tz_localize(None)
    with store.connect() as db:
        trend = weekly_context(store, db, info, identity, closed, daily_inputs, include_recovery=True)
        if trend is None:
            return {"reason": WEEKLY_UNAVAILABLE}
        if not trend["rising"]:
            recovery = trend.get("daily_recovery")
            if recovery is None:
                return {"reason": RECOVERY_UNAVAILABLE, "weekly_trend": trend}
            if not recovery["confirmed"]:
                return {"reason": WEEKLY_NOT_RISING, "weekly_trend": trend}
        raw = _series(store, db, info, identity, "15m", first, last, peer_inputs)
    if raw is None or raw.empty:
        return {"reason": "V5_4H_SIGNAL_UNAVAILABLE", "weekly_trend": trend}
    frame = intraday(cal, raw, "4H", closed, available_bars=True)
    if frame.empty or frame.index[-1] != start or not frame.coverage_complete.iloc[-1]:
        return {"reason": "V5_4H_SIGNAL_UNAVAILABLE", "weekly_trend": trend}
    row = compression(frame).iloc[-1]
    if not bool(row.eligible):
        return {"reason": "V5_4H_NO_COMPRESSION_BREAKOUT", "weekly_trend": trend}
    weighted = weighted_entry_context(cal, raw, opening)
    if weighted is None:
        return {"reason": WEIGHTED_UNAVAILABLE, "weekly_trend": trend}
    evidence = dict(
        setup=SETUP, entry_rule=ENTRY_RULE, weekly_trend=trend, weighted_entry=weighted,
        atr=float(row.atr), relative_volume=float(row.relative_volume), entry_level=float(row.entry_level),
        signal_closed=closed.isoformat(), opening=(quote_end-QUARTER).isoformat(), quote_end=quote_end.isoformat(),
    )
    if retry:
        # These are closed signal inputs only. The live order always requires
        # the separate, fresh execution quote for the current 15m interval.
        preceding = raw.loc[(raw.index >= opening) & (raw.index <= upper) & raw.volume.gt(0), "close"]
        if opening not in preceding.index:
            return dict(evidence, reason=RECLAIM_DATA_WAIT)
        first_price = preceding.loc[opening]
        initial_blocker = entry_price_blocker(evidence, first_price)
        if initial_blocker not in (LEVEL_LOST, WEIGHTED_PRICE_WAIT):
            return dict(evidence, reason=initial_blocker or RECLAIM_EARLIER_WAIT)
        if any(entry_price_blocker(evidence, p) is None for p in preceding.drop(index=opening)):
            return dict(evidence, reason=RECLAIM_EARLIER_WAIT)
        evidence["reclaim"] = dict(rule=RECLAIM_RULE, first_opening=opening.isoformat(),
                                   first_price=str(first_price), initial_blocker=initial_blocker)
    return evidence


def entry_price_blocker(evidence, price):
    from paper_v5 import PORTFOLIO_MAX_DISTANCE

    reference = Decimal(str(price))
    atr = Decimal(str(evidence["atr"]))
    distance = reference - Decimal(str(evidence["entry_level"]))
    if distance <= 0:
        return LEVEL_LOST
    if distance > PORTFOLIO_MAX_DISTANCE * atr:
        return ENTRY_TOO_FAR
    weighted = evidence.get("weighted_entry")
    if evidence.get("entry_rule") != ENTRY_RULE or not weighted or weighted.get("positive_bars",0)<6:
        return WEIGHTED_UNAVAILABLE
    if reference <= Decimal(weighted["price"]):
        return WEIGHTED_PRICE_WAIT
    return None


def quote_blocker(signal, bar, now=None):
    expected = pd.Timestamp(signal["quote_end"])
    if (pd.Timestamp(bar.get("open_at")) != pd.Timestamp(signal["opening"])
            or pd.Timestamp(bar.get("close_at")) != expected):
        return "V5_4H_WAIT_FIRST_QUOTE"
    if now is not None and not expected <= now < expected + QUARTER:
        return "V5_4H_OPENING_WINDOW_EXPIRED"
    return None


def decision(engine, quote, *, reclaim=False):
    from paper_v5 import levels

    evidence = engine.get("v5_four_hour") or {"reason": "V5_4H_SIGNAL_UNAVAILABLE"}
    if evidence.get("reason"):
        return dict(action="WAIT", reason=evidence["reason"])
    trend = evidence.get("weekly_trend", {})
    if trend.get("rule") not in (WEEKLY_RULE, RECENT_LISTING_RULE):
        return dict(action="WAIT", reason=WEEKLY_UNAVAILABLE)
    if trend.get("rising") is not True:
        recovery = trend.get("daily_recovery") if trend.get("rising") is False else None
        if recovery is None:
            return dict(action="WAIT", reason=RECOVERY_UNAVAILABLE)
        if recovery.get("confirmed") is not True:
            return dict(action="WAIT", reason=WEEKLY_NOT_RISING)
    bars = (quote or {}).get("bars", ())
    if not bars:
        return dict(action="WAIT", reason="V5_4H_WAIT_FIRST_QUOTE")
    why = quote_blocker(evidence, bars[-1])
    if why:
        return dict(action="WAIT", reason=why)
    recovery = evidence.get("reclaim")
    if recovery:
        offset = pd.Timestamp(evidence["opening"]) - pd.Timestamp(recovery["first_opening"])
        initial = entry_price_blocker(evidence, recovery["first_price"])
        if (not reclaim or recovery.get("rule") != RECLAIM_RULE
                or offset not in (QUARTER, QUARTER*2, QUARTER*3)
                or initial not in (LEVEL_LOST, WEIGHTED_PRICE_WAIT)
                or initial != recovery.get("initial_blocker")):
            return dict(action="WAIT", reason=RECLAIM_DATA_WAIT)
    why = entry_price_blocker(evidence, bars[-1]["close"])
    if why:
        return dict(action="WAIT", reason=why)
    reference = Decimal(str(bars[-1]["close"]))
    atr = Decimal(str(evidence["atr"]))
    stop, target = levels(reference, atr, target_atr=TARGET_ATR)
    return dict(
        evidence, action="BUY", reason=RECLAIM_BUY if recovery else "V5_4H_COMPRESSION_BREAKOUT", model=MODEL,
        stop=str(stop), target=str(target), hold_minutes=HOLD_MINUTES, daily_context={},
    )
