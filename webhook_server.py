"""
webhook_server.py
------------------
The real webhook server. Receives a trade alert from TradingView, then:

  1. Validates the alert (secret, pair whitelist, price sanity, stop/target
     logic, duplicate protection, existing position check, risk ceiling)
  2. Calculates position size based on account balance and risk %
  3. Places the entry order on Binance Demo Trading
  4. Places protective stop-loss and take-profit orders
  5. Sends a Telegram alert confirming what happened
  6. Logs the trade to the Google Sheets journal

Run this locally first with: python webhook_server.py
Then use ngrok to expose it to TradingView for real end-to-end testing.
Later, this same logic gets adapted into Vercel's serverless format.
"""

import time
import hmac
import hashlib
import requests
from flask import Flask, request, jsonify
import config

app = Flask(__name__)

# Keeps track of recently seen alerts in memory, to catch duplicates/replays.
# NOTE: this resets if the server restarts — fine for local testing, but
# once we move to Vercel (serverless, no persistent memory between calls)
# this specific check will need to move into the Google Sheet instead.
recent_alerts = {}


# ─────────────────────────────
# Binance helper functions (same signing logic proven in our test scripts)
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


def get_usdt_balance():
    balances = binance_get("/fapi/v2/balance")
    if not isinstance(balances, list):
        print(f"[WARNING] Unexpected response from Binance when fetching balance: {balances}")
        return 0.0
    for asset in balances:
        if asset["asset"] == "USDT":
            return float(asset["availableBalance"])
    return 0.0


def get_current_price(symbol):
    response = requests.get(f"{config.BINANCE_DEMO_BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol})
    return float(response.json()["price"])


def get_open_positions():
    """Returns a list of symbols that currently have a non-zero position."""
    positions = binance_get("/fapi/v2/positionRisk")
    if not isinstance(positions, list):
        print(f"[WARNING] Unexpected response from Binance when fetching positions: {positions}")
        return []
    return [p["symbol"] for p in positions if float(p["positionAmt"]) != 0]


def get_open_positions_detailed():
    """Returns full details for every currently open position — used by /positions."""
    positions = binance_get("/fapi/v2/positionRisk")
    if not isinstance(positions, list):
        print(f"[WARNING] Unexpected response from Binance when fetching positions: {positions}")
        return []
    return [p for p in positions if float(p["positionAmt"]) != 0]


def close_symbol_position(symbol):
    """Closes whatever open position exists on a given symbol, if any."""
    positions = binance_get("/fapi/v2/positionRisk", {"symbol": symbol})
    position = positions[0] if positions else None

    if not position or float(position["positionAmt"]) == 0:
        return None  # Nothing to close

    amt = float(position["positionAmt"])
    side = "SELL" if amt > 0 else "BUY"
    quantity = abs(amt)

    return binance_post("/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
        "reduceOnly": "true",
    })


def place_market_order(symbol, side, quantity):
    return binance_post("/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
    })


def get_atr(symbol, period=14):
    """Recent 1H ATR, used to size the trailing-stop callback rate."""
    klines = requests.get(
        f"{config.BINANCE_DEMO_BASE_URL}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1h", "limit": period + 1}
    ).json()
    true_ranges = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i - 1][4])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0


def calc_callback_rate(symbol, entry):
    """1.5x ATR (config.TRAIL_ATR_MULTIPLIER) as a % of price, rounded to Binance's
    0.1 step and clamped to its 0.1-10 allowed range."""
    atr = get_atr(symbol, config.ATR_PERIOD)
    rate = (atr * config.TRAIL_ATR_MULTIPLIER) / entry * 100
    return min(10.0, max(0.1, round(rate, 1)))


def place_protective_orders(symbol, side, quantity, stop_price, target_price, strategy=None, entry=None):
    """
    IMPORTANT: As of Dec 9, 2025, Binance requires all conditional orders
    (STOP_MARKET, TAKE_PROFIT_MARKET, etc.) to go through the separate
    /fapi/v1/algoOrder endpoint instead of the regular /fapi/v1/order
    endpoint. Using the old endpoint now returns error -4120. The two
    key differences: an "algoType": "CONDITIONAL" field is required, and
    the trigger price parameter is called "triggerPrice", not "stopPrice".

    Exit style depends on the strategy:
      - Zone / Momentum (config.TRAILING_ELIGIBLE_STRATEGIES): no fixed
        take-profit. A TRAILING_STOP_MARKET order is placed that stays
        dormant until price reaches the 3:1 level (target_price), then
        trails by callbackRate % behind price.
      - Everything else (Mean Reversion): fixed TAKE_PROFIT_MARKET.
    If the trailing order is rejected, falls back to a fixed take-profit so
    the trade is never left without a target.

    Returns (stop_order, target_order, mode, callback_rate) where mode is
    "trailing" or "fixed".
    """
    opposite_side = "SELL" if side == "BUY" else "BUY"

    stop_order = binance_post("/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": opposite_side,
        "type": "STOP_MARKET",
        "quantity": quantity,
        "triggerPrice": stop_price,
        "reduceOnly": "true",
    })

    mode = "fixed"
    callback_rate = None
    target_order = None

    if strategy in config.TRAILING_ELIGIBLE_STRATEGIES:
        try:
            callback_rate = calc_callback_rate(symbol, entry if entry else target_price)
            target_order = binance_post("/fapi/v1/algoOrder", {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": opposite_side,
                "type": "TRAILING_STOP_MARKET",
                "quantity": quantity,
                "activatePrice": target_price,
                "callbackRate": callback_rate,
                "reduceOnly": "true",
            })
            if "algoId" in target_order:
                mode = "trailing"
            else:
                print(f"[WARNING] Trailing order rejected for {symbol}: {target_order} -- falling back to fixed take-profit")
                target_order = None
        except Exception as e:
            print(f"[WARNING] Trailing order failed for {symbol}: {e} -- falling back to fixed take-profit")
            target_order = None

    if target_order is None:
        target_order = binance_post("/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": opposite_side,
            "type": "TAKE_PROFIT_MARKET",
            "quantity": quantity,
            "triggerPrice": target_price,
            "reduceOnly": "true",
        })
        mode = "fixed"

    return stop_order, target_order, mode, callback_rate


# ─────────────────────────────
# Telegram helper
# ─────────────────────────────

def send_telegram_message(text):
    """
    Sends a Telegram alert. Wrapped in try/except deliberately — a failed
    Telegram message (e.g. your PC's internet drops) should never be able
    to crash the webhook or block a trade from being processed. We print
    the failure to the console instead, so it's still visible while
    testing locally.
    """
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": config.TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print(f"[WARNING] Failed to send Telegram message: {e}")
        print(f"[WARNING] Message was: {text}")


# ─────────────────────────────
# Journal helper
# ─────────────────────────────

def log_to_journal(data):
    try:
        payload = {"secret": config.APPS_SCRIPT_SECRET, **data}
        response = requests.post(config.APPS_SCRIPT_URL, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"[WARNING] Failed to log trade to journal: {e}")
        return None


def add_open_trade(trade_row_number, symbol, side, quantity, stop, target, stop_algo_id, target_algo_id, strategy):
    try:
        payload = {
            "secret": config.APPS_SCRIPT_SECRET,
            "action": "addOpenTrade",
            "tradeRowNumber": trade_row_number,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entryTime": time.time(),
            "originalStop": stop,
            "originalTarget": target,
            "stopAlgoId": stop_algo_id,
            "targetAlgoId": target_algo_id,
            "strategy": strategy,
        }
        requests.post(config.APPS_SCRIPT_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"[WARNING] Failed to record open trade for monitoring: {e}")


# ─────────────────────────────
# Kill-switch helpers (state lives in the Google Sheet, not in Python,
# since Vercel serverless functions have no memory between invocations)
# ─────────────────────────────

def get_bot_status():
    """Returns 'ON' or 'OFF'. Defaults to 'ON' if the check fails, so a
    temporary network hiccup can't accidentally freeze trading forever —
    but logs a warning so the failure is still visible."""
    try:
        payload = {"secret": config.APPS_SCRIPT_SECRET, "action": "getStatus"}
        response = requests.post(config.APPS_SCRIPT_URL, json=payload, timeout=10)
        return response.json().get("botStatus", "ON")
    except Exception as e:
        print(f"[WARNING] Failed to check bot status, defaulting to ON: {e}")
        return "ON"


def set_bot_status(new_status):
    try:
        payload = {"secret": config.APPS_SCRIPT_SECRET, "action": "setStatus", "newStatus": new_status}
        requests.post(config.APPS_SCRIPT_URL, json=payload, timeout=10)
        return True
    except Exception as e:
        print(f"[WARNING] Failed to set bot status: {e}")
        return False


# ─────────────────────────────
# Validation layer — the "not blind" safety checks
# ─────────────────────────────

def validate_alert(data):
    """
    Returns (True, None) if the alert passes all checks,
    or (False, "reason") if it should be rejected.
    """

    # 1. Kill-switch check — if trading is paused, reject everything immediately,
    # before even checking the secret, so a paused bot never processes anything.
    if get_bot_status() == "OFF":
        return False, "Trading is currently PAUSED (kill-switch is OFF) — send /start in Telegram to resume"

    # 2. Secret check
    if data.get("secret") != config.WEBHOOK_SECRET:
        return False, "Invalid webhook secret"

    # 3. Required fields present
    # "grade" and "session" are required here (not optional) so that a
    # gatekeeper that forgets to send them fails loudly and immediately,
    # instead of the pair-grade filter or conditional-tier check below
    # silently misbehaving on a missing value (e.g. ETH getting blocked
    # entirely instead of correctly filtered, or a conditional tier
    # trading unconditionally because "session" was never matched).
    required = ["symbol", "side", "entry", "stop", "target", "strategy", "grade", "session"]
    for field in required:
        if field not in data:
            return False, f"Missing required field: {field}"

    symbol = data["symbol"]
    side = data["side"].upper()
    strategy_name = data["strategy"]
    grade = data["grade"]
    session = data["session"]

    # 4. Pair whitelist
    if symbol not in config.ALLOWED_PAIRS:
        return False, f"{symbol} is not in the allowed pairs whitelist"

    # 4b. Pair-level grade filter (currently: ETH → A/A+ only)
    if symbol in config.PAIR_GRADE_FILTERS:
        allowed_grades = config.PAIR_GRADE_FILTERS[symbol]
        if grade not in allowed_grades:
            return False, (f"{symbol} setup graded '{grade}' — only {sorted(allowed_grades)} "
                            f"grades are allowed for this pair")

    # 5. Side must be BUY or SELL
    if side not in ["BUY", "SELL"]:
        return False, f"Invalid side: {side}"

    try:
        entry = float(data["entry"])
        stop = float(data["stop"])
        target = float(data["target"])
    except (ValueError, TypeError):
        return False, "Entry, stop, and target must be numbers"

    # 6. Stop/target direction logic must make sense
    if side == "BUY":
        if not (stop < entry < target):
            return False, "For a BUY, stop must be below entry and target must be above entry"
    else:  # SELL
        if not (target < entry < stop):
            return False, "For a SELL, target must be below entry and stop must be above entry"

    # 7. Price sanity — compare alert's entry price to the real current market price
    try:
        live_price = get_current_price(symbol)
        price_diff_percent = abs(entry - live_price) / live_price * 100
        if price_diff_percent > 2.0:  # more than 2% off from real price = suspicious
            return False, (f"Alert entry price ({entry}) is {price_diff_percent:.2f}% away "
                            f"from live market price ({live_price}) — rejecting as stale/suspicious")
    except Exception as e:
        return False, f"Could not verify live price: {e}"

    # 8. Duplicate/replay protection — reject identical alerts within 10 seconds
    alert_key = f"{symbol}_{side}_{entry}_{stop}_{target}"
    now = time.time()
    if alert_key in recent_alerts and (now - recent_alerts[alert_key]) < 10:
        return False, "Duplicate alert received within 10 seconds — ignoring"
    recent_alerts[alert_key] = now

    # 9. Existing position check
    open_positions = get_open_positions()
    if symbol in open_positions:
        return False, f"Already have an open position on {symbol} — not stacking another"

    # 9b. Conditional tiers (Momentum-US, Reversion-Europe) — only allowed
    # to fire if the account is currently completely flat. Reuses the
    # open_positions list already fetched above rather than a second
    # API call. Skipped, not treated as "bad" — just deprioritized
    # whenever something else already has capital committed.
    if (strategy_name, session) in config.CONDITIONAL_TIERS:
        if open_positions:
            return False, (f"{strategy_name} ({session}) is a conditional tier — skipped because "
                            f"{open_positions} already has an open position")

    # 10. Max concurrent trades check
    if len(open_positions) >= config.MAX_CONCURRENT_TRADES:
        return False, (f"Already at max concurrent trades ({config.MAX_CONCURRENT_TRADES}) — "
                        f"rejecting new signal")

    return True, None


# ─────────────────────────────
# Main webhook route
# ─────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "No JSON payload received"}), 400

    is_valid, reason = validate_alert(data)
    if not is_valid:
        print(f"[REJECTED] {reason}")
        send_telegram_message(f"⚠️ Trade REJECTED: {reason}")
        return jsonify({"status": "rejected", "reason": reason}), 400

    symbol = data["symbol"]
    side = data["side"].upper()
    entry = float(data["entry"])
    stop = float(data["stop"])
    target = float(data["target"])
    strategy = data["strategy"]

    # ── Position sizing based on risk % ──
    balance = get_usdt_balance()
    risk_amount = round(balance * (config.RISK_PERCENT_PER_TRADE / 100), 2)
    per_unit_risk = abs(entry - stop)
    quantity = round(risk_amount / per_unit_risk, 3)

    # ── Notional cap (real-world liquidity/market-impact ceiling) ──
    # 10% of balance compounds without limit otherwise -- fine at $100,
    # not fine once it implies a multi-hundred-thousand-dollar single
    # order, which no real order book for these pairs could absorb at
    # the modeled price. Once notional would exceed the cap, position
    # size is capped at the cap's worth instead of scaling further --
    # the account still grows from wins past this point, just without
    # the position size continuing to grow alongside it. This caps
    # ORDER SIZE only; it never blocks or rejects the trade itself.
    notional = quantity * entry
    if notional > config.MAX_POSITION_NOTIONAL_USD:
        capped_quantity = round(config.MAX_POSITION_NOTIONAL_USD / entry, 3)
        print(f"[NOTIONAL CAP] {symbol}: uncapped size would be {quantity} (${notional:,.2f} notional) -- "
              f"capped to {capped_quantity} (${config.MAX_POSITION_NOTIONAL_USD:,.2f} notional)")
        quantity = capped_quantity

    # ── Place the entry order ──
    entry_order = place_market_order(symbol, side, quantity)

    if "orderId" not in entry_order:
        send_telegram_message(f"❌ Order FAILED for {symbol}: {entry_order}")
        return jsonify({"status": "error", "detail": entry_order}), 500

    # ── Place protective stop-loss and take-profit ──
    stop_order, target_order, exit_mode, callback_rate = place_protective_orders(
        symbol, side, quantity, stop, target, strategy=strategy, entry=entry)

    if "algoId" not in stop_order:
        send_telegram_message(f"🚨 STOP-LOSS order FAILED for {symbol} — position is UNPROTECTED. Detail: {stop_order}")
    if "algoId" not in target_order:
        send_telegram_message(f"⚠️ Exit order (target/trailing) FAILED for {symbol}. Detail: {target_order}")
    if strategy in config.TRAILING_ELIGIBLE_STRATEGIES and exit_mode == "fixed":
        send_telegram_message(f"⚠️ {symbol}: trailing order was rejected, used a fixed take-profit instead.")

    # ── Telegram confirmation ──
    risk_reward = round(abs(target - entry) / per_unit_risk, 2)
    send_telegram_message(
        f"✅ Trade OPENED\n"
        f"Pair: {symbol}\n"
        f"Side: {side}\n"
        f"Strategy: {strategy}\n"
        f"Entry: {entry}\n"
        f"Stop: {stop}\n"
        f"{'Trail activates at' if exit_mode == 'trailing' else 'Target'}: {target}\n"
        + (f"Trail distance: {callback_rate}%\n" if exit_mode == "trailing" else "") +
        f"R:R: {risk_reward}:1\n"
        f"Risk: ${risk_amount}\n"
        f"Size: {quantity}"
    )

    # ── Journal logging ──
    entry_time_utc = time.strftime("%H:%M:%S UTC", time.gmtime())
    journal_result = log_to_journal({
        "symbol": symbol,
        "timeframe": data.get("timeframe", "1 Hour"),
        "session": entry_time_utc,
        "strategy": strategy,
        "entry": entry,
        "stop": stop,
        "target": target,
        "riskReward": f"{risk_reward}:1",
        "riskAmount": risk_amount,
        "positionSize": quantity,
        "plannedHold": "48Hrs",
        "leverage": f"{config.LEVERAGE}X",
        "note": data.get("note", ""),
    })

    # ── Register this trade for monitoring (48hr timeout, trailing, exit detection) ──
    if journal_result and journal_result.get("status") == "success":
        trade_row_number = journal_result.get("rowWritten")
        stop_algo_id = stop_order.get("algoId")
        target_algo_id = target_order.get("algoId")
        if trade_row_number and stop_algo_id and target_algo_id:
            add_open_trade(trade_row_number, symbol, side, quantity, stop, target,
                            stop_algo_id, target_algo_id, strategy)
        else:
            print("[WARNING] Could not register trade for monitoring — missing row number or algo IDs")

    return jsonify({
        "status": "success",
        "order": entry_order,
        "stop_order": stop_order,
        "target_order": target_order,
    }), 200


@app.route("/", methods=["GET"])
def health_check():
    return "FRED webhook server is running.", 200


# ─────────────────────────────
# Hourly detection trigger — this is the route cron-job.org pings once
# an hour, which runs fred_detectors.py's full check across all 10 pairs
# and all 3 models, forwarding anything that fires back to /webhook
# above (on this exact same deployed server, not a separate one).
# GET is allowed alongside POST since most free cron schedulers default
# to GET requests.
# ─────────────────────────────

@app.route("/run-checks", methods=["GET", "POST"])
def run_checks():
    import fred_detectors
    import io
    import contextlib

    # Capture fred_detectors' own print() output so it can be returned
    # in the HTTP response -- useful for checking cron-job.org's request
    # log to see what happened on a given run, without needing separate
    # logging infrastructure.
    output_buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(output_buffer):
            fred_detectors.run_all_pairs_live()
        return jsonify({"status": "success", "log": output_buffer.getvalue()}), 200
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e), "log": output_buffer.getvalue()}), 500


# ─────────────────────────────
# Trade monitor trigger — pinged by its own cron-job.org job every 5-15
# minutes. Handles the 48hr time stop and detects trades that have closed
# (stop / trailing / target / manual) so the journal gets the real result.
# Light enough to finish well inside cron-job.org's 30s limit.
# ─────────────────────────────

@app.route("/manage-trades", methods=["GET", "POST"])
def manage_trades():
    import trade_monitor
    import io
    import contextlib

    output_buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(output_buffer):
            trade_monitor.run_monitor_cycle()
        return jsonify({"status": "success", "log": output_buffer.getvalue()}), 200
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e), "log": output_buffer.getvalue()}), 500


# ─────────────────────────────
# Telegram command handling — /stop, /start, /positions, /close, /closeall
# ─────────────────────────────

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True, silent=True)
    if not update or "message" not in update:
        return jsonify({"ok": True})  # Telegram just wants a 200, ignore anything unexpected

    message = update["message"]
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()

    # Security: only respond to commands from YOUR chat ID, ignore everyone else,
    # even if they somehow discover this bot or webhook URL.
    if chat_id != str(config.TELEGRAM_CHAT_ID):
        print(f"[SECURITY] Ignored command from unrecognized chat ID: {chat_id}")
        return jsonify({"ok": True})

    try:
        handle_telegram_command(text)
    except Exception as e:
        print(f"[ERROR] Failed to handle Telegram command '{text}': {e}")
        send_telegram_message(f"⚠️ Something went wrong processing that command: {e}")

    return jsonify({"ok": True})


def handle_telegram_command(text):
    if text == "/stop":
        set_bot_status("OFF")
        send_telegram_message(
            "🛑 Trading PAUSED.\n"
            "No new trades will open. Existing open positions are unaffected — "
            "their stop-loss and take-profit orders remain active on Binance.\n"
            "Send /start to resume."
        )

    elif text == "/start":
        set_bot_status("ON")
        send_telegram_message("✅ Trading RESUMED. New signals will now be processed normally.")

    elif text == "/positions":
        positions = get_open_positions_detailed()
        if not positions:
            send_telegram_message("📊 No open positions right now.")
        else:
            lines = ["📊 Open Positions:\n"]
            for p in positions:
                symbol = p["symbol"]
                amt = float(p["positionAmt"])
                entry = float(p["entryPrice"])
                mark = float(p["markPrice"])
                pnl = float(p["unRealizedProfit"])
                side = "LONG" if amt > 0 else "SHORT"
                lines.append(
                    f"{symbol} ({side})\n"
                    f"  Size: {abs(amt)}\n"
                    f"  Entry: {entry}\n"
                    f"  Current: {mark}\n"
                    f"  P&L: ${pnl:.2f}\n"
                )
            send_telegram_message("\n".join(lines))

    elif text.startswith("/close "):
        symbol = text.replace("/close ", "").strip().upper()
        result = close_symbol_position(symbol)
        if result is None:
            send_telegram_message(f"ℹ️ No open position found on {symbol} — nothing to close.")
        elif "orderId" in result:
            send_telegram_message(f"✅ Closed position on {symbol}.")
        else:
            send_telegram_message(f"❌ Failed to close {symbol}: {result}")

    elif text == "/closeall":
        open_positions = get_open_positions()
        if not open_positions:
            send_telegram_message("ℹ️ No open positions to close.")
        else:
            results = []
            for symbol in open_positions:
                result = close_symbol_position(symbol)
                if result and "orderId" in result:
                    results.append(f"✅ {symbol} closed")
                else:
                    results.append(f"❌ {symbol} failed: {result}")
            send_telegram_message("Closing all positions:\n" + "\n".join(results))

    elif text == "/help" or text == "/start_help":
        send_telegram_message(
            "FRED Automation Commands:\n"
            "/stop — pause new trades (existing ones keep running)\n"
            "/start — resume new trades\n"
            "/positions — show currently open positions\n"
            "/close SYMBOL — close one specific position, e.g. /close BTCUSDT\n"
            "/closeall — close every open position immediately"
        )

    else:
        send_telegram_message(f"Unrecognized command: {text}\nSend /help to see available commands.")


if __name__ == "__main__":
    print("Starting FRED webhook server on port 5000...")
    app.run(host="0.0.0.0", port=5000)
