"""
test_telegram.py
-----------------
A tiny standalone script to confirm your bot can message you.
Run this once to prove the Telegram connection works, before we
build it into the main webhook server.
"""

import requests
import config

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
    }
    response = requests.post(url, data=payload)
    return response.json()


if __name__ == "__main__":
    result = send_telegram_message("✅ FRED Automation: Telegram connection is working!")
    print(result)
