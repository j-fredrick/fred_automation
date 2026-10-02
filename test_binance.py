"""
test_binance.py
----------------
A tiny standalone script to confirm Python can talk to Binance Demo
Trading — first just checking your balance (safe, read-only), then
optionally placing one small test order.

WHY THIS LOOKS DIFFERENT FROM THE TELEGRAM/SHEETS SCRIPTS:
Binance requires every request to be "signed" using your secret key —
think of it like a tamper-proof wax seal on a letter. Binance takes your
request, runs it through a math function (HMAC-SHA256) together with your
secret key, and only accepts the request if the signature matches what
they calculate on their end. This proves the request genuinely came from
someone holding your secret key, without your secret key ever being sent
over the internet itself.
"""

import time
import hmac
import hashlib
import requests
import config


def sign_request(params):
    """
    Takes a dictionary of request parameters, turns them into the
    query-string format Binance expects, then signs that string with
    your secret key. Returns the signature Binance requires as proof
    the request is authentically yours.
    """
    query_string = "&".join([f"{key}={value}" for key, value in params.items()])
    signature = hmac.new(
        config.BINANCE_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return signature


def get_account_balance():
    """
    Fetches your Demo Trading Futures account balance.
    This is a READ-ONLY call — it cannot place trades or move funds,
    safe to run as many times as you like.
    """
    endpoint = "/fapi/v2/balance"
    timestamp = int(time.time() * 1000)

    params = {"timestamp": timestamp}
    params["signature"] = sign_request(params)

    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}

    url = config.BINANCE_DEMO_BASE_URL + endpoint
    response = requests.get(url, headers=headers, params=params)
    return response.json()


if __name__ == "__main__":
    print("Fetching Demo Trading account balance...")
    result = get_account_balance()
    print(result)
