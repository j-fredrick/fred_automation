"""
test_binance_order.py
----------------------
Places ONE small test order on Binance Demo Trading Futures, confirms
it went through, then immediately closes it out — proving the "write"
side of the API connection works (not just reading balance).

This uses fake demo money only. Nothing here touches your real account.
"""

import time
import hmac
import hashlib
import requests
import config

SYMBOL = "BTCUSDT"
TEST_QUANTITY = 0.002  # Small enough to be a safe test, big enough to clear Binance's minimum order size


def sign_request(params):
    query_string = "&".join([f"{key}={value}" for key, value in params.items()])
    signature = hmac.new(
        config.BINANCE_API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return signature


def place_market_order(symbol, side, quantity):
    """
    side: "BUY" or "SELL"
    Places a market order — executes immediately at current price.
    """
    endpoint = "/fapi/v1/order"
    timestamp = int(time.time() * 1000)

    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
        "timestamp": timestamp,
    }
    params["signature"] = sign_request(params)

    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    url = config.BINANCE_DEMO_BASE_URL + endpoint

    response = requests.post(url, headers=headers, params=params)
    return response.json()


if __name__ == "__main__":
    print(f"Step 1: Opening a test BUY position on {SYMBOL} (quantity: {TEST_QUANTITY})...")
    open_result = place_market_order(SYMBOL, "BUY", TEST_QUANTITY)
    print(open_result)

    if "orderId" in open_result:
        print("\nOrder placed successfully. Waiting 3 seconds before closing it out...")
        time.sleep(3)

        print(f"\nStep 2: Closing the test position (SELL {TEST_QUANTITY})...")
        close_result = place_market_order(SYMBOL, "SELL", TEST_QUANTITY)
        print(close_result)
    else:
        print("\nOrder did NOT go through — see the error details above. Nothing to close.")
