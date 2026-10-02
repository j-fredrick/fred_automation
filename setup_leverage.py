"""
setup_leverage.py
------------------
Explicitly sets leverage and margin mode (from config.py) for every
whitelisted trading pair on Binance Demo Trading — run this ONCE before
the bot ever places a real trade, so we're never relying on whatever
Binance defaulted to on your account.

Run this again any time you change LEVERAGE or MARGIN_MODE in config.py.
"""

import time
import hmac
import hashlib
import requests
import config


def sign_request(params):
    query_string = "&".join([f"{key}={value}" for key, value in params.items()])
    signature = hmac.new(
        config.BINANCE_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return signature


def set_leverage(symbol, leverage):
    endpoint = "/fapi/v1/leverage"
    timestamp = int(time.time() * 1000)

    params = {
        "symbol": symbol,
        "leverage": leverage,
        "timestamp": timestamp,
    }
    params["signature"] = sign_request(params)

    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    url = config.BINANCE_DEMO_BASE_URL + endpoint

    response = requests.post(url, headers=headers, params=params)
    return response.json()


def set_margin_type(symbol, margin_type):
    endpoint = "/fapi/v1/marginType"
    timestamp = int(time.time() * 1000)

    params = {
        "symbol": symbol,
        "marginType": margin_type,  # "ISOLATED" or "CROSSED"
        "timestamp": timestamp,
    }
    params["signature"] = sign_request(params)

    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    url = config.BINANCE_DEMO_BASE_URL + endpoint

    response = requests.post(url, headers=headers, params=params)
    return response.json()


if __name__ == "__main__":
    for symbol in config.ALLOWED_PAIRS:
        print(f"\n--- {symbol} ---")

        margin_result = set_margin_type(symbol, config.MARGIN_MODE)
        if margin_result.get("code") == -4046:
            print(f"Margin type already set to {config.MARGIN_MODE} — no change needed.")
        else:
            print("Margin type result:", margin_result)

        leverage_result = set_leverage(symbol, config.LEVERAGE)
        print("Leverage result:", leverage_result)

    print("\nDone. All whitelisted pairs are now configured for "
          f"{config.LEVERAGE}x leverage, {config.MARGIN_MODE} margin.")
