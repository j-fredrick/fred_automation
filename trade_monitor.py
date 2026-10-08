"""
trade_monitor.py
-----------------
Checks every open trade and handles two jobs:

  1. TIME STOP — if a trade has been open longer than MAX_HOLD_HOURS,
     close it at market price and cancel its leftover protective orders.
  2. EXIT DETECTION — if a trade has closed (stop, trailing stop, target,
     or a manual close) since the last check, read the REAL fills from
     Binance, write the true exit price / result / R to the journal,
     cancel any leftover protective orders, and stop tracking it.

Trailing stops are NOT handled here anymore. For Zone / Momentum trades the
webhook places a native Binance TRAILING_STOP_MARKET order that activates at
the 3:1 level, so Binance does the trailing itself.

Runs as a scheduled call to /manage-trades in webhook_server.py (cron-job.org
every 5-15 minutes). For local testing you can still run:
    python trade_monitor.py
which loops every MONITOR_INTERVAL_SECONDS until Ctrl+C.
"""

import time
import hmac
import hashlib
import requests
import config

# An exit price within this fraction of the original stop/target is treated
# as having hit that level (covers normal slippage).
EXIT_MATCH_TOLERANCE = 0.003  # 0.3%

# Fills are searched from slightly BEFORE the recorded entry time, because the
# journal's entryTime is stamped after the entry order was already filled.
FILL_LOOKBACK_SECONDS = 120


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
    response = requests.get(config.BINANCE_DEMO_BASE_URL + endpoint, headers=headers, params=params, timeout=15)
    return response.json()


def binance_post(endpoint, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign_request(params)
    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    response = requests.post(config.BINANCE_DEMO_BASE_URL + endpoint, headers=headers, params=params, timeout=15)
    return response.json()


def binance_delete(endpoint, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign_request(params)
    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    response = requests.delete(config.BINANCE_DEMO_BASE_URL + endpoint, headers=headers, params=params, timeout=15)
    return response.json()


def get_current_price(symbol):
    response = requests.get(f"{config.BINANCE_DEMO_BASE_URL}/fapi/v1/ticker/price",
                            params={"symbol": symbol}, timeout=15)
    return float(response.json()["price"])


def get_position_amount(symbol):
    """
    Returns the open position size, or None if Binance's answer couldn't be
    read. None must NEVER be treated as "position closed" — that would
    wrongly close out a live trade in the journal on a temporary API error.
    """
    positions = binance_get("/fapi/v2/positionRisk", {"symbol": symbol})
    if not isinstance(positions, list) or not positions:
        print(f"[WARNING] Could not read position for {symbol}: {positions}")
        return None
    return float(positions[0]["positionAmt"])


def get_trade_fills(symbol, since_ms):
    """Returns a list of fills since since_ms, or None if the request failed."""
    fills = binance_get("/fapi/v1/userTrades", {"symbol": symbol, "startTime": since_ms, "limit": 1000})
    if not isinstance(fills, list):
        print(f"[WARNING] Could not fetch fills for {symbol}: {fills}")
        return None
    return fills


def cancel_algo_order(symbol, algo_id):
    """Best effort — an order that already triggered or expired just returns an error, which is fine."""
    if not algo_id:
        return None
    try:
        return binance_delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})
    except Exception as e:
        print(f"[WARNING] Could not cancel algo order {algo_id} on {symbol}: {e}")
        return None


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
# Exit accounting from REAL fills
# ─────────────────────────────

def summarize_fills(trade, fills):
    """
    Builds the real entry/exit/PnL picture for a trade from Binance fills.
    Returns None if the fills don't contain both an opening and a closing side.
    Result is NET of commissions (what actually hit the account).
    """
    side = trade["side"].upper()
    opening = [f for f in fills if f["side"] == side]
    closing = [f for f in fills if f["side"] != side]
    if not opening or not closing:
        return None

    open_qty = sum(float(f["qty"]) for f in opening)
    close_qty = sum(float(f["qty"]) for f in closing)
    if open_qty <= 0 or close_qty <= 0:
        return None

    avg_entry = sum(float(f["price"]) * float(f["qty"]) for f in opening) / open_qty
    avg_exit = sum(float(f["price"]) * float(f["qty"]) for f in closing) / close_qty
    gross = sum(float(f.get("realizedPnl", 0)) for f in closing)
    fees = sum(float(f.get("commission", 0)) for f in fills if f.get("commissionAsset") == "USDT")
    net = gross - fees

    risk_per_unit = abs(avg_entry - float(trade["originalStop"]))
    r_multiple = round(net / (risk_per_unit * close_qty), 2) if risk_per_unit > 0 else 0

    return {"avg_entry": avg_entry, "avg_exit": avg_exit, "net": net, "r": r_multiple}


def classify_exit(trade, summary):
    """Labels how the trade ended, based on where the real exit price landed."""
    exit_price = summary["avg_exit"]
    stop = float(trade["originalStop"])
    target = float(trade["originalTarget"])
    tol = exit_price * EXIT_MATCH_TOLERANCE

    if abs(exit_price - stop) <= tol:
        return "Stop Loss"

    if trade["strategy"] in config.TRAILING_ELIGIBLE_STRATEGIES:
        # These trades have no fixed target; a profitable exit means the trailing stop fired.
        if summary["net"] > 0:
            return "Trailing Stop"
    elif abs(exit_price - target) <= tol:
        return "Take Profit"

    return "Manual / Other Close"


def finish_trade(trade, forced_reason=None):
    """
    Cleans up a trade that is no longer open: cancels leftover protective
    orders, writes the real result to the journal, stops tracking it, and
    sends a Telegram summary. Returns True if the trade was finalized, False
    if it should be retried next cycle (Binance fills couldn't be fetched).
    """
    symbol = trade["symbol"]
    entry_time = float(trade["entryTime"])
    hours_open = (time.time() - entry_time) / 3600
    sheet_row = trade["_sheetRow"]
    trade_row_number = trade["tradeRowNumber"]

    since_ms = int((entry_time - FILL_LOOKBACK_SECONDS) * 1000)
    fills = get_trade_fills(symbol, since_ms)
    if fills is None:
        return False  # Binance hiccup — keep tracking and retry next cycle

    # Whatever protective orders are left (the one that didn't trigger) must go,
    # or they could fire on the next trade for this symbol.
    cancel_algo_order(symbol, trade.get("stopAlgoId"))
    cancel_algo_order(symbol, trade.get("targetAlgoId"))

    summary = summarize_fills(trade, fills)

    if summary:
        reason = forced_reason or classify_exit(trade, summary)
        net = round(summary["net"], 2)
        close_trade_in_journal(trade_row_number, reason, hours_open, summary["avg_exit"], reason, net, summary["r"])
        send_telegram_message(
            f"📘 {symbol} trade closed ({reason}).\n"
            f"Exit: {summary['avg_exit']:.6g}\n"
            f"Result: ${net} ({summary['r']}R)\n"
            f"Held: {hours_open:.1f}h. Journal updated."
        )
        print(f"[CLOSED] {symbol} — {reason} @ {summary['avg_exit']} | ${net} | {summary['r']}R")
    else:
        reason = forced_reason or "Closed (fills not found)"
        close_trade_in_journal(trade_row_number, reason, hours_open, "", reason, "", "")
        send_telegram_message(
            f"📘 {symbol} trade closed ({reason}). Could not match fills to fill in the result — "
            f"please check Binance and the journal row manually."
        )
        print(f"[CLOSED] {symbol} — {reason} (no matching fills)")

    remove_open_trade(sheet_row)
    return True


# ─────────────────────────────
# Main monitoring logic
# ─────────────────────────────

def check_trade(trade):
    symbol = trade["symbol"]
    side = trade["side"].upper()
    entry_time = float(trade["entryTime"])

    hours_open = (time.time() - entry_time) / 3600
    position_amt = get_position_amount(symbol)

    if position_amt is None:
        return  # Couldn't read the position — do nothing this cycle rather than guess

    # ── Case 1: Position is gone (stop, trailing stop, target, or manual close) ──
    if position_amt == 0:
        finish_trade(trade)
        return

    # ── Case 2: Time stop ──
    if hours_open >= config.MAX_HOLD_HOURS:
        result = close_market(symbol, side, abs(position_amt))
        if "orderId" not in result:
            send_telegram_message(f"❌ Time stop FAILED to close {symbol}: {result}")
            print(f"[ERROR] Time stop close failed for {symbol}: {result}")
            return
        time.sleep(2)  # give Binance a moment to record the closing fill
        finish_trade(trade, forced_reason="Time Stop")
        print(f"[TIME STOP] {symbol} closed after {hours_open:.1f} hours")
        return

    # Otherwise: trade is open and within its hold window. Binance handles the
    # stop-loss and the trailing / take-profit exit on its own.


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
