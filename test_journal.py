"""
test_journal.py
----------------
A tiny standalone script to confirm Python can write a trade entry
into your Google Sheet via the Apps Script Web App.

Run this once with FAKE data to prove the connection works, then go
check your "Tradeing Log" tab to confirm the row appeared correctly.
"""

import requests
import config


def log_test_trade():
    payload = {
        "secret": config.APPS_SCRIPT_SECRET,
        "date": "07-Sep-2026 12:00:00 UTC",
        "symbol": "BTCUSDT",
        "timeframe": "1 Hour",
        "session": "Test",
        "strategy": "Strong Candle Zone",
        "entry": 65000.00,
        "stop": 64500.00,
        "target": 66500.00,
        "riskReward": "3:1",
        "riskAmount": 5.00,
        "positionSize": 0.01,
        "plannedHold": "48Hrs",
        "leverage": "10X",
        "note": "This is a TEST row from test_journal.py — safe to delete.",
    }

    response = requests.post(config.APPS_SCRIPT_URL, json=payload)
    return response.json()


if __name__ == "__main__":
    result = log_test_trade()
    print(result)
