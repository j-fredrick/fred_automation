"""
api_routes.py
--------------
The API the FRED dashboard talks to. Everything that CHANGES something goes through here
(the dashboard can only read from Supabase directly). Every request must carry the
dashboard user's Supabase login token, and only the owner account is accepted.

Endpoints
    GET  /api/me              who is logged in + whether trading is on
    GET  /api/positions       live positions with stop/target distances
    POST /api/pause           pause new trades
    POST /api/resume          resume new trades
    POST /api/settings        change settings (validated against the database limits)
    POST /api/close           close ONE pair  {"symbol": "BTCUSDT"}   (no close-all, by design)
    POST /api/events/resolve  dismiss the red banner  {"id": 12} or {"all": true}

Optional environment variable:
    DASHBOARD_ORIGIN   the dashboard's address, e.g. https://fred-dashboard.vercel.app
                       (comma-separated for several). If unset, any origin may call the API,
                       which is still safe because every call needs your login token.

Registered from webhook_server.py with:  api_routes.register(app, <webhook_server module>)
"""

import os
import time
import calendar
from functools import wraps
from flask import Blueprint, request, jsonify, make_response
import config
import supa

api = Blueprint("api", __name__, url_prefix="/api")
_ws = None  # the webhook_server module (Binance + Telegram helpers), set by register()

KNOWN_STRATEGIES = ["Strong Candle Zone", "Momentum Continuation", "Mean Reversion (50-MA)"]


def register(app, webhook_module):
    global _ws
    _ws = webhook_module
    app.register_blueprint(api)


# ─────────────────────────────
# CORS (the dashboard lives on a different address than this server)
# ─────────────────────────────

def _allowed_origin():
    origin = request.headers.get("Origin", "")
    configured = [o.strip().rstrip("/") for o in os.environ.get("DASHBOARD_ORIGIN", "").split(",") if o.strip()]
    if not configured:
        return origin or "*"
    return origin if origin.rstrip("/") in configured else None


@api.after_request
def add_cors_headers(response):
    origin = _allowed_origin()
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


# ─────────────────────────────
# Login check
# ─────────────────────────────

def owner_only(handler):
    @wraps(handler)
    def wrapper(*args, **kwargs):
        if request.method == "OPTIONS":
            return make_response("", 204)
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            return jsonify({"ok": False, "error": "Not signed in"}), 401
        user_id = supa.verify_token(token)
        if not user_id:
            return jsonify({"ok": False, "error": "Sign-in expired or invalid"}), 401
        owner_id = supa.get_user_id()
        if not owner_id:
            return jsonify({"ok": False, "error": "Database is unreachable, try again"}), 503
        if user_id != owner_id:
            supa.log_event("warning", "dashboard", "Blocked API call from a non-owner account", None, None,
                           {"user_id": user_id, "path": request.path})
            return jsonify({"ok": False, "error": "Not allowed"}), 403
        return handler(*args, **kwargs)
    return wrapper


def _body():
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def _bad(message, status=400):
    return jsonify({"ok": False, "error": message}), status


# ─────────────────────────────
# Settings validation (mirrors the database CHECK limits, so errors are readable)
# ─────────────────────────────

def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_whole(v):
    return _is_number(v) and float(v) == int(v)


def validate_settings(payload):
    """Returns (clean_dict, None) or (None, "what is wrong")."""
    allowed = {"max_concurrent_trades", "risk_percent", "max_position_notional_usd", "max_hold_hours",
               "leverage", "margin_mode", "enabled_strategies", "enabled_pairs"}
    unknown = set(payload) - allowed
    if unknown:
        return None, f"Unknown or protected setting(s): {', '.join(sorted(unknown))}"
    clean = {}

    for key, low, high in (("max_concurrent_trades", 1, 10), ("max_hold_hours", 1, 168), ("leverage", 1, 10)):
        if key in payload:
            v = payload[key]
            if not _is_whole(v) or not (low <= int(v) <= high):
                return None, f"{key} must be a whole number from {low} to {high}"
            clean[key] = int(v)

    if "risk_percent" in payload:
        v = payload["risk_percent"]
        if not _is_number(v) or not (0 < float(v) <= 10):
            return None, "risk_percent must be more than 0 and at most 10"
        clean["risk_percent"] = float(v)

    if "max_position_notional_usd" in payload:
        v = payload["max_position_notional_usd"]
        if not _is_number(v) or not (0 < float(v) <= 100000):
            return None, "max_position_notional_usd must be more than 0 and at most 100000"
        clean["max_position_notional_usd"] = float(v)

    if "margin_mode" in payload:
        v = str(payload["margin_mode"]).upper()
        if v not in ("ISOLATED", "CROSSED"):
            return None, "margin_mode must be ISOLATED or CROSSED"
        clean["margin_mode"] = v

    if "enabled_strategies" in payload:
        v = payload["enabled_strategies"]
        if not isinstance(v, list) or any(s not in KNOWN_STRATEGIES for s in v):
            return None, "enabled_strategies must be a list of known strategy names"
        clean["enabled_strategies"] = list(dict.fromkeys(v))

    if "enabled_pairs" in payload:
        v = payload["enabled_pairs"]
        if not isinstance(v, list) or any(p not in config.ALLOWED_PAIRS for p in v):
            return None, "enabled_pairs may only contain pairs from the validated list"
        clean["enabled_pairs"] = list(dict.fromkeys(v))

    if not clean:
        return None, "Nothing to change"
    return clean, None


# ─────────────────────────────
# Routes
# ─────────────────────────────

@api.route("/me", methods=["GET", "OPTIONS"])
@owner_only
def me():
    settings = supa.get_settings(force=True)
    return jsonify({"ok": True, "role": "owner", "trading_enabled": settings.get("trading_enabled", True),
                    "settings_from_database": settings.get("_from_db", False)})


@api.route("/pause", methods=["POST", "OPTIONS"])
@owner_only
def pause():
    saved = supa.set_trading_enabled(False, "dashboard")
    _ws.set_bot_status("OFF")  # keep the Google Sheet switch in step as a fallback
    if not saved:
        return _bad("Could not save to the database. The Google Sheet switch was set, so trading is paused, "
                    "but the dashboard may not show it yet.", 502)
    supa.log_event("info", "dashboard", "Trading paused from the dashboard")
    _ws.send_telegram_message("🛑 Trading PAUSED from the dashboard. Open positions keep their stop-loss and exit orders.")
    return jsonify({"ok": True, "trading_enabled": False})


@api.route("/resume", methods=["POST", "OPTIONS"])
@owner_only
def resume():
    saved = supa.set_trading_enabled(True, "dashboard")
    _ws.set_bot_status("ON")
    if not saved:
        return _bad("Could not save to the database. The Google Sheet switch was set, so trading resumes, "
                    "but the dashboard may not show it yet.", 502)
    supa.log_event("info", "dashboard", "Trading resumed from the dashboard")
    _ws.send_telegram_message("✅ Trading RESUMED from the dashboard.")
    return jsonify({"ok": True, "trading_enabled": True})


@api.route("/settings", methods=["POST", "OPTIONS"])
@owner_only
def change_settings():
    clean, problem = validate_settings(_body())
    if problem:
        return _bad(problem)
    row = supa.update_settings(clean, "dashboard")
    if not row:
        return _bad("Could not save the settings. Nothing was changed.", 502)
    _ws.send_telegram_message("⚙️ Settings changed from the dashboard: " +
                              ", ".join(f"{k} = {v}" for k, v in clean.items()))
    return jsonify({"ok": True, "settings": row})


@api.route("/close", methods=["POST", "OPTIONS"])
@owner_only
def close_one():
    symbol = str(_body().get("symbol", "")).upper().strip()
    if symbol not in config.ALLOWED_PAIRS:
        return _bad("Unknown pair")
    result = _ws.close_symbol_position(symbol)
    if result is None:
        return _bad(f"No open position on {symbol}, nothing to close", 404)
    if "orderId" not in result:
        supa.log_event("error", "dashboard", f"Close from dashboard FAILED on {symbol}", result, symbol)
        _ws.send_telegram_message(f"❌ Dashboard close failed for {symbol}: {result}")
        return _bad(f"Binance refused the close: {result}", 502)
    supa.log_event("info", "dashboard", f"{symbol} closed manually from the dashboard", None, symbol)
    _ws.send_telegram_message(f"✅ {symbol} position closed from the dashboard. The monitor will record the result "
                              f"and clean up its leftover orders on its next run.")
    return jsonify({"ok": True, "symbol": symbol})


@api.route("/events/resolve", methods=["POST", "OPTIONS"])
@owner_only
def resolve():
    body = _body()
    if body.get("all") is True:
        count = supa.resolve_events(None)
    elif "id" in body:
        try:
            count = supa.resolve_events(int(body["id"]))
        except (TypeError, ValueError):
            return _bad("id must be a number")
    else:
        return _bad("Send {\"id\": n} or {\"all\": true}")
    if count is None:
        return _bad("Could not reach the database", 502)
    return jsonify({"ok": True, "resolved": count})


def _pct(a, b):
    """How far price a is from the current price b, as a positive percentage of b."""
    return round(abs(a - b) / b * 100, 2) if b else None


@api.route("/positions", methods=["GET", "OPTIONS"])
@owner_only
def positions():
    live = _ws.get_open_positions_detailed()
    user_id = supa.get_user_id()
    rows = supa.select("trades", {"select": "*", "user_id": f"eq.{user_id}", "status": "eq.open"}) or []
    by_symbol = {}
    for r in sorted(rows, key=lambda r: r.get("opened_at") or ""):
        by_symbol[r["symbol"]] = r  # newest open trade per symbol wins
    max_hold = float(supa.get_settings()["max_hold_hours"])

    out = []
    for p in live:
        amt = float(p["positionAmt"])
        mark = float(p["markPrice"])
        entry = float(p["entryPrice"])
        trade = by_symbol.get(p["symbol"], {})
        stop = trade.get("stop_price")
        target = trade.get("target_price")
        opened = trade.get("opened_at")
        hours_open = None
        if opened:
            try:
                hours_open = round((time.time() - calendar.timegm(time.strptime(opened[:19], "%Y-%m-%dT%H:%M:%S"))) / 3600, 2)
            except ValueError:
                hours_open = None
        out.append({
            "symbol": p["symbol"],
            "side": "LONG" if amt > 0 else "SHORT",
            "size": abs(amt),
            "entry": entry,
            "mark": mark,
            "pnl_usd": round(float(p["unRealizedProfit"]), 2),
            "leverage": int(float(p.get("leverage", 0) or 0)) or None,
            "strategy": trade.get("strategy"),
            "exit_mode": trade.get("exit_mode"),
            "stop": stop,
            "target": target,
            "stop_distance_pct": _pct(float(stop), mark) if stop else None,
            "target_distance_pct": _pct(float(target), mark) if target else None,
            "opened_at": opened,
            "hours_open": hours_open,
            "max_hold_hours": max_hold,
            "tracked": bool(trade),
        })
    return jsonify({"ok": True, "positions": out})
