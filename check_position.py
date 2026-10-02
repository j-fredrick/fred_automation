"""
check_position.py
------------------
Checks your current open positions on Binance Demo Trading —
lets us verify whether the last test orders actually filled.
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


def get_position_info(symbol=None):
    endpoint = "/fapi/v2/positionRisk"
    timestamp = int(time.time() * 1000)

    params = {"timestamp": timestamp}
    if symbol:
        params["symbol"] = symbol
    params["signature"] = sign_request(params)

    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    url = config.BINANCE_DEMO_BASE_URL + endpoint

    response = requests.get(url, headers=headers, params=params)
    return response.json()


if __name__ == "__main__":
    result = get_position_info("BTCUSDT")
    print(result)
