"""
trade_monitor.py
-----------------
Runs continuously in the background, checking every open trade every few
minutes to handle three jobs:

  1. TIME STOP — if a trade has been open longer than MAX_HOLD_HOURS,
     close it at market price.
  2. TRAILING STOP — for Strong Candle Zone / Momentum Continuation trades
     only: once price reaches the original 3:1 target, lock in that profit
     and start trailing the stop behind price using ATR distance, instead
     of closing at the fixed target.
  3. EXIT DETECTION — if a trade has closed (stop or target filled) since
     the last check, fill in the journal's exit columns and stop tracking it.

Run this locally with: python trade_monitor.py
It will loop forever, checking every MONITOR_INTERVAL_SECONDS, until you
stop it with Ctrl+C. Later, this becomes a scheduled Vercel function
instead of an infinite loop.
"""

import time
import hmac
import hashlib
import requests
import config


# ─────────────────────────────
# Binance helpers (same signing pattern used everywhere else)
# ─────────────────────────────

def sign_request(params):
    query_string = "&".join([f"{key}={value}" for key, value in params.items()])
    return hmac.new(
        config.BINANCE_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def binance_get(endpoint, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign_request(params)
    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    response = requests.get(config.BINANCE_DEMO_BASE_URL + endpoint, headers=headers, params=params)
    return response.json()


def binance_post(endpoint, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign_request(params)
    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    response = requests.post(config.BINANCE_DEMO_BASE_URL + endpoint, headers=headers, params=params)
    return response.json()


def binance_delete(endpoint, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign_request(params)
    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    response = requests.delete(config.BINANCE_DEMO_BASE_URL + endpoint, headers=headers, params=params)
    return response.json()


def get_current_price(symbol):
    response = requests.get(f"{config.BINANCE_DEMO_BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol})
    return float(response.json()["price"])


def get_position_amount(symbol):
    positions = binance_get("/fapi/v2/positionRisk", {"symbol": symbol})
    if not isinstance(positions, list) or not positions:
        return 0.0
    return float(positions[0]["positionAmt"])


def get_atr(symbol, period=14):
    """Fetches recent 1H candles and calculates ATR — used for trailing distance."""
    klines = requests.get(
        f"{config.BINANCE_DEMO_BASE_URL}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1h", "limit": period + 1}
    ).json()

    true_ranges = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i - 1][4])
        true_range = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(true_range)

    return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0


def cancel_algo_order(symbol, algo_id):
    return binance_delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})


def place_stop_algo_order(symbol, side, quantity, stop_price):
    """side here is the ORIGINAL trade side — this places the opposite-side protective order."""
    opposite_side = "SELL" if side == "BUY" else "BUY"
    return binance_post("/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": opposite_side,
        "type": "STOP_MARKET",
        "quantity": quantity,
        "triggerPrice": stop_price,
        "reduceOnly": "true",
    })


def close_market(symbol, side, quantity):
    opposite_side = "SELL" if side == "BUY" else "BUY"
    return binance_post("/fapi/v1/order", {
        "symbol": symbol,
        "side": opposite_side,
        "type": "MARKET",
        "quantity": quantity,
        "reduceOnly": "true",
    })


# ─────────────────────────────
# Apps Script helpers
# ─────────────────────────────

def get_open_trades():
    try:
        payload = {"secret": config.APPS_SCRIPT_SECRET, "action": "getOpenTrades"}
        response = requests.post(config.APPS_SCRIPT_URL, json=payload, timeout=25)
        return response.json().get("trades", [])
    except Exception as e:
        print(f"[WARNING] Failed to fetch open trades: {e}")
        return []


def update_open_trade(sheet_row, **kwargs):
    try:
        payload = {"secret": config.APPS_SCRIPT_SECRET, "action": "updateOpenTrade", "sheetRow": sheet_row, **kwargs}
        requests.post(config.APPS_SCRIPT_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"[WARNING] Failed to update open trade tracking: {e}")


def remove_open_trade(sheet_row):
    try:
        payload = {"secret": config.APPS_SCRIPT_SECRET, "action": "removeOpenTrade", "sheetRow": sheet_row}
        requests.post(config.APPS_SCRIPT_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"[WARNING] Failed to remove open trade tracking: {e}")


def close_trade_in_journal(trade_row_number, exit_reason, actual_hold_hours, exit_price, exit_type, result, r_gained_lost):
    try:
        payload = {
            "secret": config.APPS_SCRIPT_SECRET,
            "action": "closeTradeInJournal",
            "tradeRowNumber": trade_row_number,
            "exitReason": exit_reason,
            "actualHoldHours": round(actual_hold_hours, 2),
            "exitPrice": exit_price,
            "exitType": exit_type,
            "result": result,
            "rGainedLost": r_gained_lost,
        }
        requests.post(config.APPS_SCRIPT_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"[WARNING] Failed to write exit details to journal: {e}")


def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": config.TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print(f"[WARNING] Failed to send Telegram message: {e}")


# ─────────────────────────────
# Main monitoring logic
# ─────────────────────────────

def check_trade(trade):
    symbol = trade["symbol"]
    side = trade["side"]
    quantity = float(trade["quantity"])
    entry_time = float(trade["entryTime"])
    original_stop = float(trade["originalStop"])
    original_target = float(trade["originalTarget"])
    strategy = trade["strategy"]
    trailing_active = str(trade["trailingActive"]).upper() == "TRUE"
    current_stop_price = float(trade["currentStopPrice"])
    sheet_row = trade["_sheetRow"]
    trade_row_number = trade["tradeRowNumber"]

    hours_open = (time.time() - entry_time) / 3600
    position_amt = get_position_amount(symbol)

    # ── Case 1: Position already closed (stop or target filled naturally) ──
    if position_amt == 0:
        current_price = get_current_price(symbol)
        # Estimate which side it exited on, based on which is closer to current price
        hit_stop = abs(current_price - current_stop_price) < abs(current_price - original_target)
        exit_price = current_stop_price if hit_stop else original_target
        exit_reason = "Stop Loss" if hit_stop else ("Take Profit" if not trailing_active else "Take Profit")
        exit_type = exit_reason

        per_unit_risk = abs(original_target - original_stop) / 3  # back out original 1R distance
        result = (exit_price - float(trade["originalStop"])) * quantity if side == "BUY" else (float(trade["originalStop"]) - exit_price) * quantity
        r_gained_lost = round(result / (per_unit_risk * quantity), 2) if per_unit_risk > 0 else 0

        close_trade_in_journal(trade_row_number, exit_reason, hours_open, exit_price, exit_type, round(result, 2), r_gained_lost)
        remove_open_trade(sheet_row)
        send_telegram_message(f"📘 {symbol} trade closed ({exit_reason}). Journal updated.")
        print(f"[CLOSED] {symbol} — {exit_reason} @ {exit_price}")
        return

    # ── Case 2: 48-hour time stop ──
    if hours_open >= config.MAX_HOLD_HOURS:
        close_market(symbol, side, quantity)
        cancel_algo_order(symbol, trade["stopAlgoId"])
        if not trailing_active:
            cancel_algo_order(symbol, trade["targetAlgoId"])

        exit_price = get_current_price(symbol)
        per_unit_risk = abs(original_target - original_stop) / 3
        result = (exit_price - original_stop) * quantity if side == "BUY" else (original_stop - exit_price) * quantity
        r_gained_lost = round(result / (per_unit_risk * quantity), 2) if per_unit_risk > 0 else 0

        close_trade_in_journal(trade_row_number, "Time Stop", hours_open, exit_price, "Time Stop", round(result, 2), r_gained_lost)
        remove_open_trade(sheet_row)
        send_telegram_message(f"⏰ {symbol} force-closed — 48hr Time Stop reached.")
        print(f"[TIME STOP] {symbol} closed after {hours_open:.1f} hours")
        return

    # ── Case 3: Trailing stop logic (Zone Entry / Momentum Continuation only) ──
    if strategy not in config.TRAILING_ELIGIBLE_STRATEGIES:
        return

    current_price = get_current_price(symbol)
    atr = get_atr(symbol, config.ATR_PERIOD)
    trail_distance = atr * config.TRAIL_ATR_MULTIPLIER

    if not trailing_active:
        # Has price reached the original fixed target yet?
        target_reached = (current_price >= original_target) if side == "BUY" else (current_price <= original_target)
        if not target_reached:
            return  # Nothing to do yet, still waiting for original target

        # Lock in the original win, cancel fixed orders, start trailing
        cancel_algo_order(symbol, trade["stopAlgoId"])
        cancel_algo_order(symbol, trade["targetAlgoId"])

        new_stop_price = original_target  # Lock in the 3:1 profit as the new floor
        new_stop_order = place_stop_algo_order(symbol, side, quantity, new_stop_price)

        update_open_trade(sheet_row, stopAlgoId=new_stop_order.get("algoId"),
                           trailingActive="TRUE", currentStopPrice=new_stop_price)
        send_telegram_message(f"🎯 {symbol} hit original target — now trailing to capture more upside.")
        print(f"[TRAILING STARTED] {symbol} — stop locked at {new_stop_price}")
        return

    # Already trailing — check if the stop should move further in our favor
    if side == "BUY":
        candidate_stop = current_price - trail_distance
        should_update = candidate_stop > current_stop_price
    else:
        candidate_stop = current_price + trail_distance
        should_update = candidate_stop < current_stop_price

    if should_update:
        cancel_algo_order(symbol, trade["stopAlgoId"])
        new_stop_order = place_stop_algo_order(symbol, side, quantity, round(candidate_stop, 2))
        update_open_trade(sheet_row, stopAlgoId=new_stop_order.get("algoId"),
                           currentStopPrice=round(candidate_stop, 2))
        print(f"[TRAIL UPDATED] {symbol} — new stop {candidate_stop:.2f}")


def run_monitor_cycle():
    trades = get_open_trades()
    if not trades:
        print("No open trades to check.")
        return

    print(f"Checking {len(trades)} open trade(s)...")
    for trade in trades:
        try:
            check_trade(trade)
        except Exception as e:
            print(f"[ERROR] Failed checking {trade.get('symbol', 'unknown')}: {e}")


if __name__ == "__main__":
    print(f"Starting trade monitor — checking every {config.MONITOR_INTERVAL_SECONDS} seconds. Press Ctrl+C to stop.")
    while True:
        run_monitor_cycle()
        time.sleep(config.MONITOR_INTERVAL_SECONDS)
