"""
close_position.py
------------------
Immediately closes an open position at market price. Use this any time
you need to flatten a position manually — e.g., cleaning up after a test.
"""

import time
import hmac
import hashlib
import requests
import config

SYMBOL = "BTCUSDT"


def sign_request(params):
    query_string = "&".join([f"{key}={value}" for key, value in params.items()])
    return hmac.new(
        config.BINANCE_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def get_position(symbol):
    endpoint = "/fapi/v2/positionRisk"
    timestamp = int(time.time() * 1000)
    params = {"symbol": symbol, "timestamp": timestamp}
    params["signature"] = sign_request(params)
    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    response = requests.get(config.BINANCE_DEMO_BASE_URL + endpoint, headers=headers, params=params)
    return response.json()


def close_position(symbol, position_amt):
    """
    If positionAmt is positive, we're long -> close with a SELL.
    If positionAmt is negative, we're short -> close with a BUY.
    """
    side = "SELL" if position_amt > 0 else "BUY"
    quantity = abs(position_amt)

    endpoint = "/fapi/v1/order"
    timestamp = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
        "reduceOnly": "true",
        "timestamp": timestamp,
    }
    params["signature"] = sign_request(params)
    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    response = requests.post(config.BINANCE_DEMO_BASE_URL + endpoint, headers=headers, params=params)
    return response.json()


if __name__ == "__main__":
    position = get_position(SYMBOL)[0]
    amt = float(position["positionAmt"])

    if amt == 0:
        print(f"No open position on {SYMBOL} — nothing to close.")
    else:
        print(f"Found open position: {amt} {SYMBOL}. Closing now...")
        result = close_position(SYMBOL, amt)
        print(result)
