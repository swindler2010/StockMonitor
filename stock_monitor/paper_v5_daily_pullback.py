
"""Frozen V5 daily pullback. Inputs are prior closed GETTEX sessions."""
from datetime import timedelta
from decimal import Decimal
from math import isfinite

import pandas as pd

MODEL = "v5_daily_pullback_defensive_v1"
PROFILE_KEY = "v5_daily_pullback_profile"
SETUP = "TREND_PULLBACK"
HOLD_MINUTES = 4320
QUARTER = pd.Timedelta(minutes=15)
HISTORY_START = "2023-10-01"
PATTERN_START = "2025-11-20"
MAX_DISTANCE = Decimal("0.80")
WAIT_QUOTE = "V5_D1_WAIT_SECOND_QUOTE"
NO_HISTORY = "V5_D1_PULLBACK_DATA_WAIT"
WAIT_PATTERN = "V5_D1_PULLBACK_PATTERN_WAIT"
WAIT_MODEL = "V5_D1_DEFENSIVE_SCORE_WAIT"
WAIT_MARKET = "V5_D1_MARKET_CONTEXT_WAIT"
WAIT_PRICE = "V5_D1_PULLBACK_PRICE_WAIT"
BUY_REASON = "V5_D1_DEFENSIVE_PULLBACK"

# Frozen training run: 228 observations from Aug 2025 through Apr 2026.
# The peer set must stay fixed: changing it would change the model's features.
COHORT = frozenset((
    "CH0012005267", "DE0005140008", "DE0005190003", "DE0005419105",
    "DE0005439004", "DE0005552004", "DE0005557508", "DE0005810055",
    "DE0006231004", "DE0007030009", "DE0007037129", "DE0007100000",
    "DE0007164600", "DE0007236101", "DE0007500001", "DE0008404005",
    "DE0008430026", "DE000A0D6554", "DE000BASF111", "DE000BAY0017",
    "DE000ENAG999", "DE000ENER6Y0", "DE000HAG0005", "DK0062498333",
    "FR0000120321", "FR0000121014", "NL0000235190", "NL0010273215",
    "US00287Y1091", "US00724F1012", "US0079031078", "US02079K1079",
    "US0231351067", "US0378331005", "US0846707026", "US11135F1012",
    "US30303M1027", "US4781601046", "US58933Y1055", "US5949181045",
    "US5951121038", "US6541061031", "US67066G1040", "US69608A1088",
    "US7427181091", "US79466L3024", "US84615Q1031", "US88160R1014",
    "US9311421039",
))
BIAS = -0.3464678132476396
THRESHOLD = -0.35147324559105153
# Feature name, training lower/upper clips, mean, scale and coefficient.
FEATURES = (
    ("short_vol_ratio", 0.4098802768557829, 2.054672015380585, 0.932355529499906, 0.36259569414495435, 0.11736317552353241),
    ("range_percent", 0.014692176613102178, 0.05530059281466225, 0.027575153619435216, 0.010284779643456247, 0.09327246807233773),
    ("slope_20", 0.1028794720371843, 1.1900899367165862, 0.5014845349061408, 0.27861547987154583, -0.06695290531805605),
    ("spread_5", 0.026979832603506337, 0.09352829387757691, 0.048959903124799094, 0.014369600465248511, -0.0511081720705241),
    ("market_5", -0.032035558192538484, 0.02817678452380068, 0.0033429471950565726, 0.013685566142790297, -0.05282826390548982),
    ("recovery", 0.5931126334723856, 1, 0.8320697744046718, 0.11433898918906936, -0.057137723924078815),
    ("overnight_proxy", 0.0010972966969027109, 0.008726001253296768, 0.0033520628291288787, 0.0018531867665565797, -0.09615616860974395),
)


def enabled(cfg):
    return cfg.get(PROFILE_KEY) == MODEL


def opening_window(identity, now):
    from timeframes import schedule

    local = now.tz_convert(identity["timezone"]).date()
    sessions = schedule({**identity, "timeframe": "1D"}, local-timedelta(days=25), local)
    if len(sessions) < 2 or sessions.index[-1].date() != local:
        return None
    session = sessions.iloc[-1]
    opening = session.open + QUARTER
    quote_end = opening + QUARTER
    if not quote_end <= now < min(quote_end + QUARTER, session.close):
        return None
    return sessions.index[-2], sessions.close.iloc[-2], opening, quote_end


def pullback(frame):
    """Return only the last session's setup and own-stock model features."""
    from paper_v4 import features

    day = features(frame)
    close = day.close.where(day.close.gt(0))
    if len(close) < 61 or not close.iloc[-2:].notna().all():
        return None, NO_HISTORY
    atr = day.atr.shift(1).iloc[-1]
    median_volume = day.vol_med.iloc[-1]
    if not all(isfinite(v) and v > 0 for v in (atr, median_volume)):
        return None, NO_HISTORY
    pattern = close.loc[PATTERN_START:]
    if len(pattern) < 14:
        return None, NO_HISTORY
    ema12 = pattern.ewm(span=12,min_periods=10,adjust=False,ignore_na=True).mean()
    ema20 = close.ewm(span=20,min_periods=20,adjust=False).mean()
    p,previous = close.iloc[-1],close.iloc[-2]
    low,high,volume = day.low.iloc[-1],day.high.iloc[-1],day.volume.iloc[-1]
    spread = high-low
    if not isfinite(spread) or spread <= 0:
        return None, WAIT_PATTERN
    anchor = ema12.iloc[-2]
    location = (p-low)/spread
    ready = (volume >= .75*median_volume and abs(p/previous-1) <= .20
        and anchor > ema12.iloc[-5] and low <= anchor+.15*atr
        and p > anchor+.04*atr and p > previous and location >= .55
        and previous > ema20.iloc[-2] > ema20.iloc[-7])
    if not ready:
        return None, WAIT_PATTERN
    changes=close.pct_change(fill_method=None)
    short=changes.rolling(10,min_periods=10).std().iloc[-1]
    long=changes.shift(10).rolling(50,min_periods=40).std().iloc[-1]
    overnight=(day.open/close.shift(1)-1).abs().where(close.notna())
    values=dict(short_vol_ratio=short/long if long>0 else float("nan"),
        range_percent=atr/p,slope_20=(ema20.iloc[-1]-ema20.iloc[-6])/atr,
        recovery=location,overnight_proxy=overnight.rolling(10,min_periods=7).median().iloc[-1])
    if not all(isfinite(v) for v in values.values()):
        return None, NO_HISTORY
    return dict(atr=float(atr),signal_close=float(p),pattern_score=float((p-anchor)/atr),
                relative_volume=float(volume/median_volume),inputs=values),None


def market_features(frames, dates):
    close=pd.DataFrame({isin:frame.close.reindex(dates) for isin,frame in frames.items()},index=dates)
    current=close.iloc[-1]
    observed=current.div(close.iloc[-5]).sub(1).replace([float("inf"),-float("inf")],float("nan")).dropna()
    changes=current.div(close.iloc[-6]).sub(1).replace([float("inf"),-float("inf")],float("nan")).dropna()
    complete=changes[close.count().reindex(changes.index).eq(6)]
    if min(len(observed),len(changes),len(complete)) < 30:
        return None
    return dict(market_5=float(changes.median()),spread_5=float(changes.quantile(.75)-changes.quantile(.25)),
                complete_median=float(complete.median()),observed=len(observed),
                complete_count=len(complete))


def defensive_score(values):
    if not all(isfinite(values[name]) for name,*_ in FEATURES):
        return None
    return BIAS+sum((min(max(values[name],lo),hi)-mean)*weight/scale
                    for name,lo,hi,mean,scale,weight in FEATURES)


def context(store, info, identity, cfg, window, inputs):
    from eur_listings import stored_identity
    from paper_account import registered_instrument
    from paper_v4 import _series
    from timeframes import schedule

    isin=info["isin"]
    if isin not in COHORT:
        return {"reason":"V5_D1_OUTSIDE_FROZEN_COHORT"}
    day,signal_closed,opening,quote_end=window
    sessions=schedule({**identity,"timeframe":"1D"},HISTORY_START,day)
    end=f"{day.date()} 23:59:59"

    def aligned(raw):
        if raw is None or raw.empty:
            return None
        dates=raw.index.tz_convert(identity["timezone"]).tz_localize(None).normalize()
        return raw.set_axis(dates) if dates.is_unique else None

    with store.connect() as db:
        own=aligned(_series(store,db,info,identity,"1D",HISTORY_START,end,inputs))
        if own is None:
            return {"reason":NO_HISTORY}
        evidence,reason=pullback(own.reindex(sessions.index))
        if reason:
            return {"reason":reason}
        dates=sessions.index[-6:]
        peer_start=str(dates[0].date())
        frames={isin:own}
        for peer in sorted(COHORT-{isin}):
            meta=cfg["identities"].get(peer)
            if not meta:
                continue
            store.read_cache.depend("instrument:"+peer)
            try:
                other=registered_instrument(db,peer)
            except ValueError as error:
                if str(error)!="PAPER_INSTRUMENT_BLOCKED":
                    raise
                continue
            if not stored_identity(db,other,meta,"1D"):
                store.read_cache.depend("setting:gettex_chart:"+peer+":1D")
                continue
            frame=aligned(_series(store,db,other,meta,"1D",peer_start,end,inputs))
            if frame is not None:
                frames[peer]=frame
    market=market_features(frames,dates)
    if market is None or market["complete_median"]<0:
        return {"reason":WAIT_MARKET,"market":market}
    score=defensive_score({**evidence.pop("inputs"),**market})
    if score is None or score<THRESHOLD:
        return {"reason":WAIT_MODEL,"defensive_score":score,"market":market}
    return dict(evidence,setup=SETUP,entry_rule=MODEL,signal_day=str(day.date()),
                signal_closed=signal_closed.isoformat(),opening=opening.isoformat(),
                quote_end=quote_end.isoformat(),defensive_score=score,market=market)


def quote_blocker(evidence, bar, now=None):
    close=pd.Timestamp(evidence["quote_end"])
    if (pd.Timestamp(bar.get("open_at"))!=pd.Timestamp(evidence["opening"])
            or pd.Timestamp(bar.get("close_at"))!=close):
        return WAIT_QUOTE
    if now is not None and not close<=now<close+QUARTER:
        return "V5_D1_OPENING_WINDOW_EXPIRED"
    price=Decimal(str(bar["close"]))
    if abs(price-Decimal(str(evidence["signal_close"])))>MAX_DISTANCE*Decimal(str(evidence["atr"])):
        return WAIT_PRICE
    return None


def levels(reference, atr):
    stop=reference-max(Decimal(".003")*reference,Decimal("1.4")*atr)
    target=reference+max(Decimal(".006")*reference,Decimal(3)*atr)
    return stop,target


def decision(engine, quote):
    evidence=engine.get("v5_daily_pullback") or {"reason":WAIT_QUOTE}
    if evidence.get("reason"):
        return dict(action="WAIT",reason=evidence["reason"])
    stamp=engine.get("timestamp")
    if (evidence.get("entry_rule")!=MODEL or not stamp
            or str(pd.Timestamp(stamp).date())!=evidence["signal_day"]):
        return dict(action="WAIT",reason="V5_D1_SIGNAL_CANDLE_CHANGED")
    bars=(quote or {}).get("bars",())
    if not bars:
        return dict(action="WAIT",reason=WAIT_QUOTE)
    bar=bars[-1]
    why=quote_blocker(evidence,bar)
    if why:
        return dict(action="WAIT",reason=why)
    stop,target=levels(Decimal(str(bar["close"])),Decimal(str(evidence["atr"])))
    return dict(evidence,action="BUY",reason=BUY_REASON,model=MODEL,
                stop=str(stop),target=str(target),hold_minutes=HOLD_MINUTES)

