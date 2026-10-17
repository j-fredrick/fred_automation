"""
supa.py
--------
Thin Supabase helper for the FRED backend. It talks to Supabase's REST API with plain
`requests`, so nothing new has to be installed on Vercel.

THE ONE RULE: Supabase must never be able to stop the bot from trading or managing trades.
Every function here catches its own errors, uses a short timeout, and returns a harmless
value (None / False / defaults) when Supabase is unreachable. The Google Sheet journal and
Binance stay the source of truth for safety; Supabase feeds the dashboard and holds settings.

Environment variables (set in Vercel -> Settings -> Environment Variables):
    SUPABASE_URL          e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY  the service_role key (server only, never in the browser)
    FRED_USER_ID          optional. If not set, the account with role 'owner' is used.
"""

import os
import time
import requests
import config

TIMEOUT_SECONDS = 5
SETTINGS_CACHE_SECONDS = 10

_owner_id = None
_settings_cache = {"at": 0.0, "value": None}


# ─────────────────────────────
# Low-level request
# ─────────────────────────────

def _base():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    return (url, key) if url and key else (None, None)


def is_configured():
    return _base()[0] is not None


def _request(method, table, params=None, body=None, prefer=None):
    """Returns (ok, data). Never raises."""
    url, key = _base()
    if not url:
        return False, None
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
    }
    # Old-style keys are JWTs (start with "eyJ") and go in both headers. The newer
    # "sb_secret_..." keys are not JWTs and must only be sent as the apikey.
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    if prefer:
        headers["Prefer"] = prefer
    try:
        response = requests.request(method, f"{url}/rest/v1/{table}", headers=headers,
                                    params=params, json=body, timeout=TIMEOUT_SECONDS)
        if response.status_code >= 300:
            print(f"[SUPABASE] {method} {table} failed ({response.status_code}): {response.text[:300]}")
            return False, None
        if not response.text:
            return True, None
        return True, response.json()
    except Exception as e:
        print(f"[SUPABASE] {method} {table} error: {e}")
        return False, None


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _num(value):
    """Float or None (Supabase numeric columns reject empty strings)."""
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def get_user_id():
    """The account all of this bot's data belongs to (single user for now)."""
    global _owner_id
    if _owner_id:
        return _owner_id
    env_id = os.environ.get("FRED_USER_ID", "").strip()
    if env_id:
        _owner_id = env_id
        return _owner_id
    ok, rows = _request("GET", "profiles", params={"select": "id", "role": "eq.owner", "limit": "1"})
    if ok and rows:
        _owner_id = rows[0]["id"]
    return _owner_id


# ─────────────────────────────
# Settings
# ─────────────────────────────

def _default_settings():
    return {
        "_from_db": False,
        "trading_enabled": True,
        "max_concurrent_trades": int(config.MAX_CONCURRENT_TRADES),
        "risk_percent": float(config.RISK_PERCENT_PER_TRADE),
        "max_position_notional_usd": float(config.MAX_POSITION_NOTIONAL_USD),
        "max_hold_hours": float(config.MAX_HOLD_HOURS),
        "leverage": int(config.LEVERAGE),
        "margin_mode": str(config.MARGIN_MODE).upper(),
        "enabled_strategies": None,   # None = every strategy allowed
        "enabled_pairs": None,        # None = every pair in config.ALLOWED_PAIRS
    }


def get_settings(force=False):
    """
    The live settings row, falling back to config.py values if Supabase can't be reached.
    result["_from_db"] tells the caller which one it got. Cached for a few seconds so one
    signal doesn't trigger several identical requests.
    """
    now = time.time()
    if not force and _settings_cache["value"] is not None and now - _settings_cache["at"] < SETTINGS_CACHE_SECONDS:
        return _settings_cache["value"]

    settings = _default_settings()
    user_id = get_user_id()
    if user_id:
        ok, rows = _request("GET", "settings", params={"select": "*", "user_id": f"eq.{user_id}", "limit": "1"})
        if ok and rows:
            row = rows[0]
            for key in settings:
                if key != "_from_db" and row.get(key) is not None:
                    settings[key] = row[key]
            settings["_from_db"] = True
    _settings_cache["value"] = settings
    _settings_cache["at"] = now
    return settings


def set_trading_enabled(enabled, source="telegram"):
    """Pause / resume switch shared by the dashboard and Telegram. True if saved."""
    user_id = get_user_id()
    if not user_id:
        return False
    ok, _ = _request("PATCH", "settings", params={"user_id": f"eq.{user_id}"},
                     body={"trading_enabled": bool(enabled), "updated_by": source})
    _settings_cache["value"] = None
    return ok


# ─────────────────────────────
# Dashboard support: login check, generic read, settings update, banner dismiss
# ─────────────────────────────

def verify_token(token):
    """
    Checks a dashboard login token with Supabase Auth. Returns the user's id, or None if the
    token is missing, expired or fake. Never raises.
    """
    url, key = _base()
    if not url or not token:
        return None
    try:
        response = requests.get(f"{url}/auth/v1/user",
                                headers={"apikey": key, "Authorization": f"Bearer {token}"},
                                timeout=TIMEOUT_SECONDS)
        if response.status_code != 200:
            return None
        return response.json().get("id")
    except Exception as e:
        print(f"[SUPABASE] token check error: {e}")
        return None


def select(table, params):
    """Plain read. Returns a list of rows, or None if the request failed."""
    ok, rows = _request("GET", table, params=params)
    return rows if ok else None


def update_settings(fields, source="dashboard"):
    """Saves already-validated settings. Returns the updated row, or None."""
    user_id = get_user_id()
    if not user_id or not fields:
        return None
    body = dict(fields)
    body["updated_by"] = source
    ok, rows = _request("PATCH", "settings", params={"user_id": f"eq.{user_id}"},
                        body=body, prefer="return=representation")
    _settings_cache["value"] = None
    return rows[0] if ok and rows else None


def resolve_events(event_id=None):
    """Dismisses one problem (by id) or every open problem. Returns how many were dismissed, or None."""
    user_id = get_user_id()
    if not user_id:
        return None
    params = {"user_id": f"eq.{user_id}", "resolved_at": "is.null"}
    if event_id is not None:
        params["id"] = f"eq.{int(event_id)}"
    ok, rows = _request("PATCH", "events", params=params, body={"resolved_at": _now_iso()},
                        prefer="return=representation")
    return len(rows or []) if ok else None


# ─────────────────────────────
# Signals, events, heartbeats, equity
# ─────────────────────────────

def log_signal(data, outcome, reason_code=None, reason_text=None, trade_id=None):
    """Records one signal, traded or not. outcome: 'traded', 'rejected' or 'error'."""
    user_id = get_user_id()
    if not user_id:
        return False
    data = data if isinstance(data, dict) else {}
    ok, _ = _request("POST", "signals", body={
        "user_id": user_id,
        "symbol": str(data.get("symbol") or "UNKNOWN"),
        "strategy": data.get("strategy"),
        "grade": data.get("grade"),
        "session": data.get("session"),
        "side": str(data["side"]).upper() if data.get("side") else None,
        "entry": _num(data.get("entry")),
        "stop": _num(data.get("stop")),
        "target": _num(data.get("target")),
        "outcome": outcome,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "trade_id": trade_id,
    })
    return ok


def log_event(level, source, title, detail=None, symbol=None, meta=None):
    """level: 'info', 'warning' or 'error'. Warnings and errors feed the dashboard's red banner."""
    user_id = get_user_id()
    if not user_id:
        return False
    ok, _ = _request("POST", "events", body={
        "user_id": user_id,
        "level": level,
        "source": source,
        "title": title,
        "detail": None if detail is None else str(detail)[:2000],
        "symbol": symbol,
        "meta": meta,
    })
    return ok


def heartbeat(job, status="ok", detail=None):
    """Stamps 'this scheduled job just ran'. The dashboard shows how long ago that was."""
    ok, _ = _request("POST", "system_heartbeats", prefer="resolution=merge-duplicates",
                     body={"job": job, "last_run_at": _now_iso(), "status": status,
                           "detail": None if detail is None else str(detail)[:500]})
    return ok


def snapshot_equity(balance, unrealized=None, open_trades=None, open_risk=None):
    user_id = get_user_id()
    balance = _num(balance)
    if not user_id or balance is None:
        return False
    ok, _ = _request("POST", "equity_snapshots", body={
        "user_id": user_id, "balance": balance, "unrealized": _num(unrealized),
        "open_trades": open_trades, "open_risk": _num(open_risk),
    })
    return ok


# ─────────────────────────────
# Trades
# ─────────────────────────────

def open_trade(fields):
    """
    Inserts a newly opened trade. `fields` uses the trades table's column names.
    Returns the new trade's id, or None if it couldn't be saved.
    """
    user_id = get_user_id()
    if not user_id:
        return None
    row = {"user_id": user_id, "status": "open"}
    row.update({k: v for k, v in fields.items() if v is not None})
    ok, rows = _request("POST", "trades", body=row, prefer="return=representation")
    if ok and rows:
        return rows[0].get("id")
    return None


def close_trade(trade, exit_label, exit_price, result_usd, r_multiple, hold_hours, exit_note="", entry_price=None):
    """
    Marks a trade closed. `trade` is the monitor's open-trade record. If no matching open
    row exists in Supabase (e.g. the trade was opened before Supabase was connected), a
    complete closed row is created instead so the dashboard history is still right.
    """
    user_id = get_user_id()
    if not user_id:
        return False
    closed = {
        "status": "closed",
        "closed_at": _now_iso(),
        "exit_price": _num(exit_price),
        "exit_label": exit_label,
        "exit_note": exit_note or None,
        "result_usd": _num(result_usd),
        "r_multiple": _num(r_multiple),
        "hold_hours": _num(hold_hours),
    }
    journal_row = trade.get("tradeRowNumber")
    if journal_row not in (None, ""):
        ok, rows = _request("PATCH", "trades", prefer="return=representation",
                            params={"user_id": f"eq.{user_id}", "journal_row": f"eq.{journal_row}", "status": "eq.open"},
                            body=closed)
        if ok and rows:
            return True
        if not ok:
            return False
    # Nothing to update: create the closed row from what the monitor knows.
    entry_time = _num(trade.get("entryTime"))
    opened_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry_time)) if entry_time else _now_iso()
    row = {
        "user_id": user_id,
        "journal_row": int(journal_row) if str(journal_row).isdigit() else None,
        "symbol": trade.get("symbol"),
        "side": str(trade.get("side", "")).upper(),
        "strategy": trade.get("strategy") or "Unknown",
        "entry_price": _num(entry_price),
        "stop_price": _num(trade.get("originalStop")),
        "target_price": _num(trade.get("originalTarget")),
        "quantity": _num(trade.get("quantity")),
        "stop_order_id": str(trade.get("stopAlgoId") or "") or None,
        "target_order_id": str(trade.get("targetAlgoId") or "") or None,
        "opened_at": opened_at,
    }
    row.update({k: v for k, v in closed.items() if v is not None})
    row["status"] = "closed"
    row = {k: v for k, v in row.items() if v is not None}
    ok, _ = _request("POST", "trades", body=row)
    return ok
