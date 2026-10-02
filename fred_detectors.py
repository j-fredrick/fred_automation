"""
fred_detectors.py
-------------------
ALL THREE FRED entry models -- Zone Entry, Momentum Continuation, Mean
Reversion -- consolidated into one file, ported directly from
FRED_Combined_DataCollection_v11_GATEKEEPER.pine. No logic changed or
tuned versus the individually-tested files this replaces
(zone_entry_detector.py, momentum_detector.py, reversion_detector.py,
fred_combined_runner.py).

WHY THIS IS STRUCTURED AS PURE FUNCTIONS (state in, state out -- no
file I/O inside the detection logic itself):
This is a deliberate refactor made while consolidating, so that this
exact same detection code can be used TWO ways without duplicating it:
  1. LIVE (see run_all_pairs_live() near the bottom): loads state from a
     local JSON file, calls the pure check function, saves the result
     back to that file, forwards any fired signal to the webhook.
  2. HISTORICAL COMPARISON (in the separate fred_historical_check.py):
     keeps state in memory while replaying a full year of past candles,
     never touching a file at all, much faster for a long backtest-style
     run, and guaranteed to be running the exact same logic as live.
Zone and Reversion's check functions take a `state` dict and RETURN a
new one (candles in, state in, -> result, new_state). Momentum needs no
state at all (see its own section for why), so its check function only
takes candles.

STATE STORAGE CAVEAT (unchanged from before): the LIVE wrappers here use
local JSON files, which will not survive on Vercel's stateless functions.
Before deploying, swap load_zone_state/save_zone_state and
load_reversion_state/save_reversion_state to read/write your Google
Sheet instead.
"""

import json
import os
import time
import requests

import config


# ═══════════════════════════════════════════════════════════════════════
# SHARED: candle fetching, pagination, and indicator calculations
# (used by all three models)
# ═══════════════════════════════════════════════════════════════════════
BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
INTERVAL = "1h"
CANDLES_NEEDED = 120  # warm-up for ATR(14), avgRange SMA(20), ma50 SMA(50), vol_sma(50), plus headroom

MODE = "Soft"
WICK_PCT_STRICT = 0.05
WICK_PCT_SOFT = 0.15
BODY_ATR_MULT = 0.5
VOL_MULT = 1.2
VOL_LEN = 50
ATR_LEN = 14
USE_VOLUME_FILTER = True

MA_LEN = 50
CLOSE_STRENGTH = 0.65
RANGE_FACTOR = 1.3


def fetch_candles(symbol):
    """Pulls the most recent CANDLES_NEEDED 1H candles for `symbol`.
    Drops the still-forming current candle, matching Pine's "only
    evaluate closed bars" behavior."""
    params = {"symbol": symbol, "interval": INTERVAL, "limit": CANDLES_NEEDED + 1}
    response = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
    response.raise_for_status()
    raw = response.json()
    candles = [{
        "open_time": row[0], "open": float(row[1]), "high": float(row[2]),
        "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
        "close_time": row[6],
    } for row in raw]
    return candles[:-1]


def fetch_historical_candles(symbol, days_back):
    """
    Pulls `days_back` days of 1H candles, paging through Binance's
    1500-candle-per-request cap as needed. Used only by the historical
    comparison tool (fred_historical_check.py), not by live detection.
    """
    end_time = int(time.time() * 1000)
    start_time = end_time - (days_back * 24 * 60 * 60 * 1000)
    all_candles = []
    cursor = start_time

    while cursor < end_time:
        params = {"symbol": symbol, "interval": INTERVAL, "startTime": cursor, "limit": 1500}
        response = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
        response.raise_for_status()
        raw = response.json()
        if not raw:
            break
        batch = [{
            "open_time": row[0], "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
            "close_time": row[6],
        } for row in raw]
        all_candles.extend(batch)
        last_close_time = batch[-1]["close_time"]
        if last_close_time <= cursor:
            break
        cursor = last_close_time + 1
        if len(raw) < 1500:
            break

    seen = set()
    deduped = []
    for c in all_candles:
        if c["open_time"] not in seen:
            seen.add(c["open_time"])
            deduped.append(c)
    deduped.sort(key=lambda c: c["open_time"])
    return deduped


def sma(values, length):
    """Simple moving average of the LAST `length` values in the list
    (used by the live detectors, which always pass a short, bounded
    window -- here, recomputing from scratch each call is fine since
    the window is small)."""
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def rolling_sma_series(values, length):
    """
    O(n) rolling-window SMA across an entire series at once, using a
    running sum (add the new value, subtract the one that aged out of
    the window) instead of re-summing the last `length` values from
    scratch at every index. Returns a list the same length as `values`,
    with None for indices before `length` values exist yet -- the exact
    same None-until-enough-history behavior as calling sma() repeatedly,
    just computed in O(n) total instead of O(n^2).

    This matters a lot for the historical comparison tool, which computes
    indicators across up to a full year of hourly candles (~8,760) --
    the old per-index sma() approach made that effectively unusable
    (an hour-plus with no result). This produces IDENTICAL numbers, just
    computed efficiently; verified by direct regression test against the
    original method.
    """
    n = len(values)
    result = [None] * n
    running_sum = 0.0
    for i in range(n):
        running_sum += values[i]
        if i >= length:
            running_sum -= values[i - length]
        if i >= length - 1:
            result[i] = running_sum / length
    return result


def atr(candles, length):
    if len(candles) < length + 1:
        return [None] * len(candles)
    true_ranges = [None]
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr_values = [None] * len(candles)
    seed = sum(true_ranges[1:length + 1]) / length
    atr_values[length] = seed
    for i in range(length + 1, len(candles)):
        atr_values[i] = (atr_values[i - 1] * (length - 1) + true_ranges[i]) / length
    return atr_values


def close_pos(candle):
    c, o, h, l = candle["close"], candle["open"], candle["high"], candle["low"]
    if h == l:
        return 0.5
    return (c - l) / (h - l) if c > o else (h - c) / (h - l)


def compute_indicators(candles):
    n = len(candles)
    closes = [c["close"] for c in candles]
    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    bodies = [abs(closes[i] - opens[i]) for i in range(n)]
    ranges = [highs[i] - lows[i] for i in range(n)]
    upper_wicks = [highs[i] - max(closes[i], opens[i]) for i in range(n)]
    lower_wicks = [min(closes[i], opens[i]) - lows[i] for i in range(n)]

    atr_values = atr(candles, ATR_LEN)
    # O(n) rolling computation, not per-index re-summing -- see
    # rolling_sma_series()'s docstring for why this matters.
    avg_range = rolling_sma_series(ranges, 20)
    vol_sma = rolling_sma_series(volumes, VOL_LEN)
    ma50 = rolling_sma_series(closes, MA_LEN)

    bull = [closes[i] > opens[i] for i in range(n)]
    bear = [closes[i] < opens[i] for i in range(n)]
    body_nonzero = [b if b != 0 else 1e-10 for b in bodies]
    upper_wick_ratio = [upper_wicks[i] / body_nonzero[i] for i in range(n)]
    lower_wick_ratio = [lower_wicks[i] / body_nonzero[i] for i in range(n)]

    strong_buy = [False] * n
    strong_sell = [False] * n
    intent_bull = [False] * n
    intent_bear = [False] * n
    trend_up = [False] * n
    trend_down = [False] * n

    for i in range(n):
        if atr_values[i] is None or avg_range[i] is None or vol_sma[i] is None or ma50[i] is None:
            continue

        strict_bull = (bull[i] and lower_wick_ratio[i] <= WICK_PCT_STRICT and upper_wick_ratio[i] <= WICK_PCT_STRICT
                       and bodies[i] >= atr_values[i] * BODY_ATR_MULT
                       and (not USE_VOLUME_FILTER or volumes[i] >= vol_sma[i] * VOL_MULT))
        strict_bear = (bear[i] and upper_wick_ratio[i] <= WICK_PCT_STRICT and lower_wick_ratio[i] <= WICK_PCT_STRICT
                       and bodies[i] >= atr_values[i] * BODY_ATR_MULT
                       and (not USE_VOLUME_FILTER or volumes[i] >= vol_sma[i] * VOL_MULT))
        soft_bull = (bull[i] and lower_wick_ratio[i] <= WICK_PCT_SOFT and upper_wick_ratio[i] <= WICK_PCT_SOFT * 2
                     and bodies[i] >= atr_values[i] * (BODY_ATR_MULT * 0.8)
                     and (not USE_VOLUME_FILTER or volumes[i] >= vol_sma[i] * (VOL_MULT * 0.9)))
        soft_bear = (bear[i] and upper_wick_ratio[i] <= WICK_PCT_SOFT and lower_wick_ratio[i] <= WICK_PCT_SOFT * 2
                     and bodies[i] >= atr_values[i] * (BODY_ATR_MULT * 0.8)
                     and (not USE_VOLUME_FILTER or volumes[i] >= vol_sma[i] * (VOL_MULT * 0.9)))

        strong_buy[i] = strict_bull if MODE == "Strict" else (strict_bull or soft_bull)
        strong_sell[i] = strict_bear if MODE == "Strict" else (strict_bear or soft_bear)

        cp = close_pos(candles[i])
        intent_bull[i] = bull[i] and ranges[i] >= avg_range[i] * RANGE_FACTOR and cp >= CLOSE_STRENGTH
        intent_bear[i] = bear[i] and ranges[i] >= avg_range[i] * RANGE_FACTOR and cp >= CLOSE_STRENGTH

        trend_up[i] = closes[i] > ma50[i]
        trend_down[i] = closes[i] < ma50[i]

    return {
        "closes": closes, "opens": opens, "highs": highs, "lows": lows,
        "bodies": bodies, "avg_range": avg_range, "ma50": ma50,
        "strong_buy": strong_buy, "strong_sell": strong_sell,
        "intent_bull": intent_bull, "intent_bear": intent_bear,
        "trend_up": trend_up, "trend_down": trend_down,
        "ranges": ranges, "upper_wicks": upper_wicks, "lower_wicks": lower_wicks,
        "atr_values": atr_values, "bull": bull, "bear": bear,
    }


def session_for(close_time_ms):
    session_hour = time.gmtime(close_time_ms / 1000).tm_hour
    return "Asia" if session_hour < 8 else "Europe" if session_hour < 16 else "US"


# ═══════════════════════════════════════════════════════════════════════
# MODEL 1 -- ZONE ENTRY (pure function: candles + state in, result + new state out)
# ═══════════════════════════════════════════════════════════════════════
PULLBACK_MAX_BARS = 20
WEAK_PULLBACK_BODY_MULT = 0.8
ZONE_RISK_REWARD_RATIO = 3.0
FIB_ANCHOR_METHOD = "Single Candle"  # "Single Candle" (default, proven) or "Extended Move" (tested, rejected -- see decisions log)


def default_zone_state():
    return {
        "zoneActive": False, "zoneTop": None, "zoneBottom": None,
        "zoneOriginHigh": None, "zoneOriginLow": None, "zoneIsBullish": None,
        "zoneOriginTimestamp": None, "pullbackInZone": False, "pullbackExtreme": None,
        "zoneExtendedHigh": None, "zoneExtendedLow": None, "zoneExtending": False,
    }


def zone_entry_check(symbol, candles, state, precomputed_ind=None, index=None):
    """
    Pure Zone Entry check. Returns (result_or_None, new_state).

    precomputed_ind / index: OPTIONAL performance path, used only by
    fred_historical_check.py. Live calls never pass these -- they're
    None by default, so compute_indicators(candles) runs exactly as
    before and `i` is derived from len(candles)-1, exactly as before.
    When the historical tool passes a precomputed `ind` (computed ONCE
    across a full year of candles) and an explicit `index`, this skips
    recomputing indicators from scratch on every single candle of the
    replay -- which is what made a 365-day check take over an hour.
    Both paths produce identical results; only the live path's behavior
    is "the real one" in the sense that it's what actually runs live,
    the historical path is purely a speed optimization using the same
    underlying numbers.
    """
    if precomputed_ind is not None:
        ind = precomputed_ind
        i = index
    else:
        ind = compute_indicators(candles)
        i = len(candles) - 1
    if i < 1:
        return None, state

    state = dict(state)  # never mutate the caller's dict in place
    strong_buy, strong_sell = ind["strong_buy"][i], ind["strong_sell"][i]

    if (strong_buy or strong_sell) and not state["zoneActive"]:
        state["zoneActive"] = True
        state["zoneTop"] = max(ind["opens"][i], ind["closes"][i])
        state["zoneBottom"] = min(ind["opens"][i], ind["closes"][i])
        state["zoneOriginHigh"] = ind["highs"][i]
        state["zoneOriginLow"] = ind["lows"][i]
        state["zoneIsBullish"] = bool(strong_buy)
        state["zoneOriginTimestamp"] = candles[i]["open_time"]
        state["pullbackInZone"] = False
        state["pullbackExtreme"] = None
        state["zoneExtendedHigh"] = ind["highs"][i]
        state["zoneExtendedLow"] = ind["lows"][i]
        state["zoneExtending"] = True
        return None, state

    if not state["zoneActive"]:
        return None, state

    if state["zoneExtending"]:
        if state["zoneIsBullish"]:
            if ind["closes"][i] > ind["closes"][i - 1]:
                state["zoneExtendedHigh"] = max(state["zoneExtendedHigh"], ind["highs"][i])
            else:
                state["zoneExtending"] = False
        else:
            if ind["closes"][i] < ind["closes"][i - 1]:
                state["zoneExtendedLow"] = min(state["zoneExtendedLow"], ind["lows"][i])
            else:
                state["zoneExtending"] = False

    # 1H candles are evenly spaced, so the origin candle's index can be
    # computed directly by arithmetic instead of scanning the whole list
    # every single check -- the scan version made the historical replay
    # quadratic all over again, on top of the SMA fix above. Falls back
    # to a real scan only if the arithmetic guess is wrong (e.g. a real
    # gap in fetched data), so correctness is never sacrificed for speed.
    guessed_index = round((state["zoneOriginTimestamp"] - candles[0]["open_time"]) / 3600000)
    if 0 <= guessed_index < len(candles) and candles[guessed_index]["open_time"] == state["zoneOriginTimestamp"]:
        origin_index = guessed_index
    else:
        origin_index = next((idx for idx, c in enumerate(candles) if c["open_time"] == state["zoneOriginTimestamp"]), None)
    if origin_index is None:
        state["zoneActive"] = False
        return None, state

    bars_since_origin = i - origin_index
    if bars_since_origin > PULLBACK_MAX_BARS:
        state["zoneActive"] = False
        return None, state

    price_in_zone = (bars_since_origin > 0 and ind["lows"][i] <= state["zoneTop"] and ind["highs"][i] >= state["zoneBottom"])
    if price_in_zone:
        state["pullbackInZone"] = True
        if state["zoneIsBullish"]:
            state["pullbackExtreme"] = ind["lows"][i] if state["pullbackExtreme"] is None else min(state["pullbackExtreme"], ind["lows"][i])
        else:
            state["pullbackExtreme"] = ind["highs"][i] if state["pullbackExtreme"] is None else max(state["pullbackExtreme"], ind["highs"][i])

    pullback_avg_body = sma(ind["bodies"][:i + 1], 3)
    pullback_too_strong = (state["pullbackInZone"] and pullback_avg_body is not None
                            and ind["avg_range"][i] is not None
                            and pullback_avg_body >= ind["avg_range"][i] * WEAK_PULLBACK_BODY_MULT)

    fib_anchor_high = state["zoneExtendedHigh"] if FIB_ANCHOR_METHOD == "Extended Move" else state["zoneOriginHigh"]
    fib_anchor_low = state["zoneExtendedLow"] if FIB_ANCHOR_METHOD == "Extended Move" else state["zoneOriginLow"]
    fib_range = fib_anchor_high - fib_anchor_low
    fib_618_bull = fib_anchor_high - fib_range * 0.618
    fib_618_bear = fib_anchor_low + fib_range * 0.618
    fib_382_bull = fib_anchor_high - fib_range * 0.382
    fib_382_bear = fib_anchor_low + fib_range * 0.382

    fib_broken = False
    fib_tier = None
    if state["pullbackExtreme"] is not None:
        if state["zoneIsBullish"]:
            fib_broken = state["pullbackExtreme"] < fib_618_bull
            fib_tier = "A" if state["pullbackExtreme"] >= fib_382_bull else "B"
        else:
            fib_broken = state["pullbackExtreme"] > fib_618_bear
            fib_tier = "A" if state["pullbackExtreme"] <= fib_382_bear else "B"

    if pullback_too_strong or fib_broken:
        state["zoneActive"] = False
        state["pullbackInZone"] = False
        return None, state

    not_opposing_momentum = (not ind["intent_bear"][i]) if state["zoneIsBullish"] else (not ind["intent_bull"][i])
    cp = close_pos(candles[i])

    zone_confirm_bull = (state["zoneIsBullish"] and state["pullbackInZone"]
                         and ind["closes"][i] > ind["opens"][i] and cp >= CLOSE_STRENGTH
                         and ind["closes"][i] > ind["highs"][i - 1] and ind["trend_up"][i] and not_opposing_momentum)
    zone_confirm_bear = ((not state["zoneIsBullish"]) and state["pullbackInZone"]
                         and ind["closes"][i] < ind["opens"][i] and cp >= CLOSE_STRENGTH
                         and ind["closes"][i] < ind["lows"][i - 1] and ind["trend_down"][i] and not_opposing_momentum)

    if not (zone_confirm_bull or zone_confirm_bear):
        return None, state

    entry_price = ind["closes"][i]
    is_long = zone_confirm_bull
    stop_price = state["zoneBottom"] if is_long else state["zoneTop"]
    target_price = (entry_price + (entry_price - stop_price) * ZONE_RISK_REWARD_RATIO if is_long
                     else entry_price - (stop_price - entry_price) * ZONE_RISK_REWARD_RATIO)
    grade = fib_tier if fib_tier is not None else "B"
    session = session_for(candles[i]["close_time"])

    result = {
        "secret": config.WEBHOOK_SECRET, "symbol": symbol, "side": "BUY" if is_long else "SELL",
        "entry": str(entry_price), "stop": str(stop_price), "target": str(target_price),
        "strategy": "Strong Candle Zone", "grade": grade, "session": session,
    }
    state["zoneActive"] = False
    state["pullbackInZone"] = False
    return result, state


# ═══════════════════════════════════════════════════════════════════════
# MODEL 2 -- MOMENTUM CONTINUATION (pure function, stateless: see original
# module note on why no state is needed -- every check is self-contained
# using only the last 2-4 already-closed candles)
# ═══════════════════════════════════════════════════════════════════════
MOMENTUM_STOP_PERCENT = 1.0
MOMENTUM_RISK_REWARD_RATIO = 3.0


def grade_from_wick_pct(wick_pct):
    if wick_pct <= 10:
        return "A+"
    elif wick_pct <= 20:
        return "A"
    elif wick_pct <= 30:
        return "B"
    return "C"


def momentum_check(symbol, candles, precomputed_ind=None, index=None):
    """Pure Momentum Continuation check. Returns result_or_None.
    See zone_entry_check's docstring for what precomputed_ind/index are
    for -- same optional performance path, same guarantee of identical
    results either way."""
    if precomputed_ind is not None:
        ind = precomputed_ind
        i = index
    else:
        ind = compute_indicators(candles)
        i = len(candles) - 1
    if i < 3:
        return None

    strong_buy, strong_sell = ind["strong_buy"], ind["strong_sell"]
    intent_bull, intent_bear = ind["intent_bull"], ind["intent_bear"]

    pattern_a_bull = strong_buy[i - 2] and intent_bull[i - 1] and intent_bull[i]
    pattern_b_bull = strong_buy[i - 3] and intent_bull[i - 2] and intent_bull[i - 1] and intent_bull[i]
    block_bull = pattern_a_bull or pattern_b_bull

    pattern_a_bear = strong_sell[i - 2] and intent_bear[i - 1] and intent_bear[i]
    pattern_b_bear = strong_sell[i - 3] and intent_bear[i - 2] and intent_bear[i - 1] and intent_bear[i]
    block_bear = pattern_a_bear or pattern_b_bear

    if not (block_bull or block_bear):
        return None

    ranges_i = ind["highs"][i] - ind["lows"][i]
    if ranges_i == 0:
        return None

    upper_wick = ind["highs"][i] - max(ind["closes"][i], ind["opens"][i])
    lower_wick = min(ind["closes"][i], ind["opens"][i]) - ind["lows"][i]

    if block_bull:
        wick_pct = (upper_wick / ranges_i) * 100
        grade = grade_from_wick_pct(wick_pct)
        pattern = "B" if pattern_b_bull else "A"
        is_long = True
    else:
        wick_pct = (lower_wick / ranges_i) * 100
        grade = grade_from_wick_pct(wick_pct)
        pattern = "B" if pattern_b_bear else "A"
        is_long = False

    if grade == "C":
        return None

    entry_price = ind["closes"][i]
    if is_long:
        stop_price = entry_price * (1 - MOMENTUM_STOP_PERCENT / 100)
        target_price = entry_price * (1 + (MOMENTUM_STOP_PERCENT * MOMENTUM_RISK_REWARD_RATIO) / 100)
    else:
        stop_price = entry_price * (1 + MOMENTUM_STOP_PERCENT / 100)
        target_price = entry_price * (1 - (MOMENTUM_STOP_PERCENT * MOMENTUM_RISK_REWARD_RATIO) / 100)

    session = session_for(candles[i]["close_time"])

    return {
        "secret": config.WEBHOOK_SECRET, "symbol": symbol, "side": "BUY" if is_long else "SELL",
        "entry": str(entry_price), "stop": str(stop_price), "target": str(target_price),
        "strategy": "Momentum Continuation", "grade": grade, "session": session,
        "pattern": pattern, "wick_pct": round(wick_pct, 2),
    }


# ═══════════════════════════════════════════════════════════════════════
# MODEL 3 -- MEAN REVERSION (pure function: candles + state in, result + new state out)
# ═══════════════════════════════════════════════════════════════════════
EXHAUSTION_WICK_PCT = 0.40
OVEREXTENSION_ATR = 3.0
OVEREXTENSION_METHOD = "ATR Multiple"
PERCENT_THRESHOLD = 3.0
RAPID_LOOKBACK = 3
REVERSION_STOP_METHOD = "Fixed 1%"  # locked decision after 10-pair testing -- v11's own script default is "Exhaustion Structure"
REVERSION_STOP_PERCENT = 1.0


def default_reversion_state():
    return {
        "waitingBear": False, "exhBearLow": None, "exhBearHigh": None,
        "exhBearWickPct": None, "exhBearDistATR": None,
        "waitingBull": False, "exhBullHigh": None, "exhBullLow": None,
        "exhBullWickPct": None, "exhBullDistATR": None,
    }


def session_grade(session):
    return "A+" if session == "US" else "A" if session == "Asia" else "B"


def _overextended_at(ind, idx):
    ma, a = ind["ma50"][idx], ind["atr_values"][idx]
    if ma is None or a is None:
        return None
    dist = abs(ind["closes"][idx] - ma)
    if OVEREXTENSION_METHOD == "ATR Multiple":
        return dist > a * OVEREXTENSION_ATR
    return (dist / ma) * 100 > PERCENT_THRESHOLD


def reversion_check(symbol, candles, state, precomputed_ind=None, index=None):
    """Pure Mean Reversion check. Returns (result_or_None, new_state).
    See zone_entry_check's docstring for what precomputed_ind/index are
    for -- same optional performance path, same guarantee of identical
    results either way."""
    if precomputed_ind is not None:
        ind = precomputed_ind
        i = index
    else:
        ind = compute_indicators(candles)
        i = len(candles) - 1
    if i < RAPID_LOOKBACK + 1:
        return None, state

    state = dict(state)
    ma, a = ind["ma50"][i], ind["atr_values"][i]
    if ma is None or a is None or ind["avg_range"][i] is None:
        return None, state

    close, open_, high, low = ind["closes"][i], ind["opens"][i], ind["highs"][i], ind["lows"][i]
    ranges_i = ind["ranges"][i]
    if ranges_i == 0:
        return None, state

    upper_wick_frac = ind["upper_wicks"][i] / ranges_i
    lower_wick_frac = ind["lower_wicks"][i] / ranges_i
    dist_from_ma = abs(close - ma)

    over_now = _overextended_at(ind, i)
    over_before = _overextended_at(ind, i - RAPID_LOOKBACK)
    if over_now is None or over_before is None:
        return None, state
    rapid_move = over_now and (not over_before)

    exhaust_up = ind["trend_up"][i] and rapid_move and upper_wick_frac > EXHAUSTION_WICK_PCT
    exhaust_down = ind["trend_down"][i] and rapid_move and lower_wick_frac > EXHAUSTION_WICK_PCT

    if exhaust_up and not state["waitingBear"] and not state["waitingBull"]:
        state["waitingBear"] = True
        state["exhBearLow"] = low
        state["exhBearHigh"] = high
        state["exhBearWickPct"] = upper_wick_frac * 100
        state["exhBearDistATR"] = dist_from_ma / a

    if exhaust_down and not state["waitingBear"] and not state["waitingBull"]:
        state["waitingBull"] = True
        state["exhBullHigh"] = high
        state["exhBullLow"] = low
        state["exhBullWickPct"] = lower_wick_frac * 100
        state["exhBullDistATR"] = dist_from_ma / a

    prev_high, prev_low = ind["highs"][i - 1], ind["lows"][i - 1]
    continuation_up = (ind["trend_up"][i] and ind["intent_bull"][i - 1]
                       and close > prev_high * 0.5 + prev_low * 0.5 and close > open_ and lower_wick_frac < 0.35)
    continuation_down = (ind["trend_down"][i] and ind["intent_bear"][i - 1]
                         and close < prev_high * 0.5 + prev_low * 0.5 and close < open_ and upper_wick_frac < 0.35)
    strong_cont_up = continuation_up and close >= high - ranges_i * 0.25
    strong_cont_down = continuation_down and close <= low + ranges_i * 0.25

    if state["waitingBear"] and strong_cont_up:
        state["waitingBear"] = False
        state["exhBearLow"] = None
        state["exhBearHigh"] = None

    if state["waitingBull"] and strong_cont_down:
        state["waitingBull"] = False
        state["exhBullHigh"] = None
        state["exhBullLow"] = None

    cp = close_pos(candles[i])
    confirm_bear = state["waitingBear"] and close < open_ and cp >= CLOSE_STRENGTH and close < state["exhBearLow"]
    confirm_bull = state["waitingBull"] and close > open_ and cp >= CLOSE_STRENGTH and close > state["exhBullHigh"]

    if not (confirm_bear or confirm_bull):
        return None, state

    is_long = confirm_bull
    if REVERSION_STOP_METHOD == "Fixed 1%":
        stop_price = close * (1 - REVERSION_STOP_PERCENT / 100) if is_long else close * (1 + REVERSION_STOP_PERCENT / 100)
    else:
        stop_price = state["exhBullLow"] if is_long else state["exhBearHigh"]

    session = session_for(candles[i]["close_time"])

    result = {
        "secret": config.WEBHOOK_SECRET, "symbol": symbol, "side": "BUY" if is_long else "SELL",
        "entry": str(close), "stop": str(stop_price), "target": str(ma),
        "strategy": "Mean Reversion (50-MA)", "grade": session_grade(session), "session": session,
    }

    if is_long:
        state["waitingBull"] = False
        state["exhBullHigh"] = None
        state["exhBullLow"] = None
    else:
        state["waitingBear"] = False
        state["exhBearHigh"] = None
        state["exhBearLow"] = None
    return result, state


# ═══════════════════════════════════════════════════════════════════════
# LIVE WRAPPERS -- file-based state I/O, webhook forwarding, and the
# combined per-hour runner. This is the ONLY section that touches disk
# or the network for state; everything above is pure and I/O-free.
# ═══════════════════════════════════════════════════════════════════════
ZONE_STATE_FILE = "zone_state.json"        # used only when config.STATE_BACKEND == "local"
REVERSION_STATE_FILE = "reversion_state.json"  # used only when config.STATE_BACKEND == "local"


def _load_json_state(path, symbol, default_factory):
    """Local-file backend. Only used when config.STATE_BACKEND == 'local'."""
    if not os.path.exists(path):
        return default_factory()
    with open(path, "r") as f:
        all_state = json.load(f)
    return all_state.get(symbol, default_factory())


def _save_json_state(path, symbol, state):
    """Local-file backend. Only used when config.STATE_BACKEND == 'local'."""
    all_state = {}
    if os.path.exists(path):
        with open(path, "r") as f:
            all_state = json.load(f)
    all_state[symbol] = state
    with open(path, "w") as f:
        json.dump(all_state, f, indent=2)


def _load_sheet_state(symbol, model, default_factory):
    """
    Google Sheets backend, via FRED_State_Storage_AppsScript.gs.
    On any network/server error, falls back to the default state rather
    than crashing the whole hourly check -- a missed state read for one
    pair shouldn't take down every other pair's check in the same run.
    That does mean a transient Sheets outage could cause a zone/waiting
    state to be lost for that one cycle; this is a deliberate
    availability-over-strictness tradeoff, worth knowing about.
    """
    try:
        response = requests.post(config.GOOGLE_STATE_SCRIPT_URL, json={
            "secret": config.GOOGLE_STATE_SCRIPT_SECRET,
            "action": "get_state",
            "symbol": symbol,
            "model": model,
        }, timeout=10)
        result = response.json()
        if result.get("success") and result.get("state") is not None:
            return result["state"]
        return default_factory()
    except Exception as e:
        print(f"[WARNING] Failed to load {model} state for {symbol} from Google Sheets: {e}. Using default state.")
        return default_factory()


def _save_sheet_state(symbol, model, state):
    """Google Sheets backend, via FRED_State_Storage_AppsScript.gs."""
    try:
        response = requests.post(config.GOOGLE_STATE_SCRIPT_URL, json={
            "secret": config.GOOGLE_STATE_SCRIPT_SECRET,
            "action": "set_state",
            "symbol": symbol,
            "model": model,
            "state": state,
        }, timeout=10)
        result = response.json()
        if not result.get("success"):
            print(f"[WARNING] Failed to save {model} state for {symbol}: {result.get('error')}")
    except Exception as e:
        print(f"[WARNING] Failed to save {model} state for {symbol} to Google Sheets: {e}")


def load_zone_state(symbol):
    if config.STATE_BACKEND == "google_sheets":
        return _load_sheet_state(symbol, "zone", default_zone_state)
    return _load_json_state(ZONE_STATE_FILE, symbol, default_zone_state)


def save_zone_state(symbol, state):
    if config.STATE_BACKEND == "google_sheets":
        _save_sheet_state(symbol, "zone", state)
    else:
        _save_json_state(ZONE_STATE_FILE, symbol, state)


def load_reversion_state(symbol):
    if config.STATE_BACKEND == "google_sheets":
        return _load_sheet_state(symbol, "reversion", default_reversion_state)
    return _load_json_state(REVERSION_STATE_FILE, symbol, default_reversion_state)


def save_reversion_state(symbol, state):
    if config.STATE_BACKEND == "google_sheets":
        _save_sheet_state(symbol, "reversion", state)
    else:
        _save_json_state(REVERSION_STATE_FILE, symbol, state)


def forward_to_webhook(payload):
    try:
        response = requests.post(config.LOCAL_WEBHOOK_URL, json=payload, timeout=10)
        print(f"[FORWARDED] {payload['symbol']} {payload['side']} ({payload['strategy']}) -- "
              f"status {response.status_code}: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Failed to reach webhook_server.py: {e}")


def check_all_models_live(symbol):
    """Live version: loads state from disk, runs all 3 pure checks,
    saves state back to disk. Returns a list of (model_name, result) for
    whatever fired (empty list if nothing did)."""
    fired = []

    candles = fetch_candles(symbol)

    zone_state = load_zone_state(symbol)
    zone_result, zone_state = zone_entry_check(symbol, candles, zone_state)
    save_zone_state(symbol, zone_state)
    if zone_result:
        fired.append(("Zone Entry", zone_result))

    momentum_result = momentum_check(symbol, candles)
    if momentum_result:
        fired.append(("Momentum Continuation", momentum_result))

    reversion_state = load_reversion_state(symbol)
    reversion_result, reversion_state = reversion_check(symbol, candles, reversion_state)
    save_reversion_state(symbol, reversion_state)
    if reversion_result:
        fired.append(("Mean Reversion", reversion_result))

    return fired


def run_all_pairs_live():
    """The hourly entry point -- what cron-job.org will eventually
    trigger on Vercel. Checks all pairs, all 3 models, forwards anything
    that fired."""
    print(f"=== FRED Combined Runner -- checking {len(config.ALLOWED_PAIRS)} pairs across 3 models ===\n")
    total_fired = 0
    for symbol in config.ALLOWED_PAIRS:
        try:
            fired = check_all_models_live(symbol)
        except Exception as e:
            print(f"[ERROR] {symbol} check failed: {e}")
            continue
        if not fired:
            print(f"[checked] {symbol} -- no signal from any model")
        else:
            for model_name, payload in fired:
                total_fired += 1
                print(f"[SIGNAL] {symbol} -- {model_name} fired -- {payload['side']} Grade {payload['grade']} ({payload['session']})")
                forward_to_webhook(payload)
    print(f"\n=== Done. {total_fired} signal(s) fired and forwarded. ===")


if __name__ == "__main__":
    run_all_pairs_live()
