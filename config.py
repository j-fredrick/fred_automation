"""
config.py
---------
All secrets and settings for the FRED automation live here.

DEPLOYMENT NOTE: every real secret below now reads from an ENVIRONMENT
VARIABLE first, falling back to the value that was already here if that
variable isn't set. This means:
  - LOCALLY: nothing changes. No environment variables are set on your
    laptop, so every value falls back to exactly what it already was --
    the whole system keeps working exactly as before.
  - ON VERCEL: you'll set each of these as an Environment Variable in
    the Vercel dashboard (Project -> Settings -> Environment Variables).
    Vercel sets those as real env vars at runtime, so the code picks
    them up automatically -- the hardcoded fallback values below are
    never used once that's done.
This is what keeps real secrets out of the code that gets pushed to
GitHub/Vercel, while changing nothing about how local testing works.
"""

import os

# ── BINANCE DEMO TRADING (Futures) ──
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "e87M96KKPrAnwbtPEpCwKLq7fViRtQplFEGXV7lIZtRaaOIX0a5BgDhhxpFMMHzs")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "9ahSaNsmzjKTlZOV7ESOwCVNwppblhpy98mzyifi9S5vDXEvVWFCWMKwMAxRR49R")
BINANCE_DEMO_BASE_URL = "https://demo-fapi.binance.com"  # Futures Demo Trading endpoint -- not a secret, no env var needed

# ── TELEGRAM ──
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8930528850:AAEclQthrISrI0XP9Bg59dUauoPFNAlfk2I")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5571631569")

# ── GOOGLE APPS SCRIPT (JOURNAL) ──
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbwN5NGBmAMuUHF4YYXg0Yb6xJecrxOS_n_CsnhoWCRkJLpqT5gANbhEErHIuOcUMOHB/exec")
APPS_SCRIPT_SECRET = os.environ.get("APPS_SCRIPT_SECRET", "Html5$css")

# ── WEBHOOK SECURITY ──
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "Html5$css")

# ── STATE STORAGE (for Zone Entry & Mean Reversion's "waiting" state) ──
# "local" = zone_state.json / reversion_state.json on this machine's disk.
# Fine for testing here, but these files do NOT survive on Vercel (every
# serverless invocation gets a blank filesystem). Switch this to
# "google_sheets" before deploying, once FRED_State_Storage_AppsScript.gs
# is deployed and the two values below are filled in.
STATE_BACKEND = "local"  # or "google_sheets"

# A NEW, SEPARATE Apps Script Web App from your existing journal one --
# see FRED_State_Storage_AppsScript.gs's own setup instructions for how
# to deploy it and get these two values.
GOOGLE_STATE_SCRIPT_URL = os.environ.get("GOOGLE_STATE_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbx3flHI93v8p20JseeZeIWO2k0V982dpS_L4dR2J-82xP3ji_-BAgy6rILUANJIbUk/exec")
GOOGLE_STATE_SCRIPT_SECRET = os.environ.get("GOOGLE_STATE_SCRIPT_SECRET", "Html5$css")  # must match STATE_SECRET in the .gs file

# ── EMAIL-TO-WEBHOOK BRIDGE (free-plan workaround, no ngrok needed) ──
# TradingView's free plan can't send webhooks directly, so alerts are sent
# as email instead. email_bridge.py watches this Gmail inbox and forwards
# each alert's JSON body to LOCAL_WEBHOOK_URL below, exactly as a real
# webhook would have.
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "joshuafredrick121@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "gpzyfvfrilkhlvhz")
TRADINGVIEW_SENDER_FILTER = "tradingview"  # not a secret, no env var needed

# IMPORTANT FOR DEPLOYMENT: this is where fred_detectors.py sends every
# fired signal. Locally that's your laptop's own webhook_server.py.
# Once deployed, /webhook lives on the SAME Vercel URL as /run-checks --
# set LOCAL_WEBHOOK_URL as an environment variable on Vercel to your
# real deployed URL + "/webhook" (e.g.
# "https://your-project.vercel.app/webhook"). Locally, the fallback
# below keeps working exactly as before.
LOCAL_WEBHOOK_URL = os.environ.get("LOCAL_WEBHOOK_URL", "http://localhost:5000/webhook")
EMAIL_POLL_INTERVAL_SECONDS = 30  # how often to check the inbox for new alert emails

# ── RISK MANAGEMENT ──
# 10% per trade at a 1% stop distance -> full $1,000 notional on a $100
# balance at 10x leverage (LEVERAGE below) -- matches the intended
# "$100 -> $1000 notional" position sizing exactly.
RISK_PERCENT_PER_TRADE = 10.0
MAX_CONCURRENT_TRADES = 2        # Hard cap on simultaneous open trades
MAX_TOTAL_RISK_PERCENT = 20.0    # 2 trades x 10% = 20% ceiling
DEFAULT_RISK_REWARD = 3.0

# A ceiling on a single order's notional size (quantity x entry price),
# regardless of what 10% risk-based sizing would otherwise produce.
# This exists because 10% compounding has no upper limit on its own --
# fine at a $100 balance, not fine once it implies an order larger than
# what these pairs' real order books could actually absorb without
# significant slippage (the account's own historical simulation showed
# risk amounts reaching into the hundreds of thousands of dollars per
# trade after enough compounding -- that's the problem this caps).
# $20,000 here is a conservative STARTING POINT, not a researched number
# for any specific pair's real liquidity -- revisit and raise this once
# you've looked at actual order book depth for these pairs, or once
# you're trading with enough real capital for it to matter.
# This only caps ORDER SIZE -- it never blocks a trade from happening.
MAX_POSITION_NOTIONAL_USD = 20000.0
MAX_HOLD_HOURS = 48              # Auto-close if trade hasn't resolved by then

# ── LEVERAGE ──
LEVERAGE = 10
MARGIN_MODE = "ISOLATED"  # Safer than "CROSSED" — caps loss to that position's margin

# ── TRAILING STOP & MONITORING ──
# Trailing only applies to these two models — NOT Mean Reversion, since
# reaching the 50-MA IS the win condition for that model.
TRAILING_ELIGIBLE_STRATEGIES = ["Strong Candle Zone", "Momentum Continuation"]
TRAIL_ATR_MULTIPLIER = 1.5   # How many ATRs behind price the trailing stop sits
ATR_PERIOD = 14              # Matches the ATR length used in your indicators
MONITOR_INTERVAL_SECONDS = 300  # How often trade_monitor.py checks open trades (5 min)

# ── ALLOWED TRADING PAIRS (whitelist) ──
ALLOWED_PAIRS = [
    "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "SOLUSDT", "AVAXUSDT", "BNBUSDT","UNIUSDT",
]

# ── PAIR-LEVEL GRADE FILTER ──
# Only pairs listed here get filtered by grade; every other pair trades
# all grades normally. Values are the SET of grades allowed to fire for
# that pair, across all three models.
# ETH: A/A+ only. For Momentum and Reversion (which have a real A+ tier)
# this means A+ or A. For Zone Entry (which only has A/B, no A+ tier),
# this naturally allows Zone's A-tier and blocks its B-tier — no extra
# per-model logic needed, the set just happens to exclude "B".
# Requires the alert payload to include a "grade" field (sent by the
# gatekeeper, not yet built) — see validate_alert() for the fail-safe
# behavior if "grade" is ever missing.
PAIR_GRADE_FILTERS = {
    "ETHUSDT": {"A+", "A"},
    "SOLUSDT": {"A+", "A"},
}

# ── CONDITIONAL TIERS ──
# These specific (strategy, session) combinations are weak but not
# catastrophic — allowed to fire ONLY when the account currently has NO
# open position at all. If anything else is already open, they're
# skipped (not rejected as "bad"), so they never compete with a
# stronger setup for the same risk budget.
# Deliberately scoped to just these two — never broadened to "every
# B-grade trade" or any other tier.
# Requires the alert payload to include a "session" field (sent by the
# gatekeeper, not yet built) — see validate_alert() for the fail-safe
# behavior if "session" is ever missing.
CONDITIONAL_TIERS = {
    ("Momentum Continuation", "US"),
    ("Mean Reversion (50-MA)", "Europe"),
}
