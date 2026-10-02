"""
email_bridge.py
----------------
Free-plan workaround for TradingView's paywalled webhook feature.

TradingView's Basic (free) plan cannot send webhook alerts directly — but
it CAN send alerts as email, which is free on every plan. This script:

  1. Logs into Gmail via IMAP using an App Password (never your real
     Gmail password — Gmail blocks plain-password script logins).
  2. Polls the inbox every EMAIL_POLL_INTERVAL_SECONDS for new, unread
     emails from TradingView.
  3. Extracts the alert's JSON payload from the email body (the exact
     string built by the Pine script's alert() calls).
  4. POSTs that JSON straight to webhook_server.py's /webhook route,
     running locally — no ngrok needed, since this script and the
     webhook server both run on the same machine.
  5. Marks the email as read/seen so it's never processed twice.

Run this in its own terminal, alongside webhook_server.py:
    python email_bridge.py

HONEST LIMITATION vs a real webhook: email delivery isn't instant — expect
anywhere from a few seconds to a couple of minutes of lag depending on
Gmail and your internet connection, versus a webhook's near-instant
delivery. For a 1-hour-timeframe strategy this is very unlikely to matter,
but it is a real difference worth knowing, not treating as equivalent.

ONE THING TO VERIFY ONCE YOU RECEIVE YOUR FIRST REAL ALERT EMAIL: the
exact sender address TradingView emails come from can vary. If this
script isn't picking up alert emails, open one in Gmail, check the actual
"From:" address, and update TRADINGVIEW_SENDER_FILTER in config.py to
match a distinctive part of it.
"""

import time
import re
import json
import email
import imaplib
from email.header import decode_header

import requests
import config


IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Matches the first {...} JSON-looking block in a string — this is how
# we pull the alert payload out of the email body, which may have extra
# TradingView boilerplate text around it.
JSON_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def connect():
    """Opens a fresh IMAP connection and logs in. Raises on failure —
    the caller decides how to handle a failed connection (retry loop)."""
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
    return conn


def get_email_body(msg):
    """
    Extracts the plain-text body from an email.message.Message object.
    Handles both simple plain-text emails and multipart emails (which
    Gmail/TradingView commonly send — a plain-text part alongside an
    HTML part). Always prefers the plain-text part when both exist.
    """
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                charset = part.get_content_charset() or "utf-8"
                try:
                    return part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    return part.get_payload(decode=True).decode("utf-8", errors="replace")
        # Fallback: no plain-text part found, try the first text/html part
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        return ""
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            return msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            return msg.get_payload(decode=True).decode("utf-8", errors="replace")


def decode_mime_header(raw_header):
    """Decodes an email header that may be MIME-encoded (e.g. non-ASCII
    sender names) into a plain string for logging/filtering."""
    if raw_header is None:
        return ""
    decoded_parts = decode_header(raw_header)
    result = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(encoding or "utf-8", errors="replace")
        else:
            result += part
    return result


def extract_json_payload(body_text):
    """
    Finds and parses the JSON alert payload embedded in the email body.
    Returns the parsed dict, or None if no valid JSON block was found.
    """
    match = JSON_PATTERN.search(body_text)
    if not match:
        return None
    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def forward_to_webhook(payload_dict):
    """Sends the extracted alert payload to the local webhook server,
    exactly as TradingView's own webhook delivery would have."""
    try:
        response = requests.post(config.LOCAL_WEBHOOK_URL, json=payload_dict, timeout=10)
        print(f"[FORWARDED] Status {response.status_code}: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Failed to forward to webhook_server.py: {e}")
        print("[ERROR] Is webhook_server.py actually running on this machine?")


def process_unread_alerts(conn):
    """
    Checks the inbox for unread emails from TradingView, forwards any
    valid JSON payloads found, and marks each processed email as read
    so it's never picked up again on the next poll.
    """
    conn.select("inbox")
    status, message_ids = conn.search(None, "UNSEEN")
    if status != "OK":
        print("[WARNING] IMAP search failed — skipping this poll cycle.")
        return

    id_list = message_ids[0].split()
    if not id_list:
        return  # nothing new, quiet and expected most cycles

    for msg_id in id_list:
        status, msg_data = conn.fetch(msg_id, "(RFC822)")
        if status != "OK":
            print(f"[WARNING] Could not fetch message {msg_id} — skipping.")
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        sender = decode_mime_header(msg.get("From"))
        subject = decode_mime_header(msg.get("Subject"))

        if config.TRADINGVIEW_SENDER_FILTER.lower() not in sender.lower():
            # Not a TradingView alert — leave it unread/untouched, it's
            # none of this script's business.
            continue

        body_text = get_email_body(msg)
        payload = extract_json_payload(body_text)

        if payload is None:
            print(f"[WARNING] TradingView email found (subject: '{subject}') "
                  f"but no valid JSON payload could be extracted from its body. "
                  f"Check the email's actual content — the alert's Message "
                  f"field or Condition setting may not be sending the raw "
                  f"alert() string as expected.")
        else:
            print(f"[RECEIVED] Alert email (subject: '{subject}') — forwarding payload...")
            forward_to_webhook(payload)

        # Mark as read regardless of outcome, so a malformed alert email
        # doesn't get retried forever and clutter every future poll.
        conn.store(msg_id, "+FLAGS", "\\Seen")


def run_forever():
    print("Starting FRED email-to-webhook bridge...")
    print(f"Watching {config.GMAIL_ADDRESS} for alerts from senders containing "
          f"'{config.TRADINGVIEW_SENDER_FILTER}'")
    print(f"Forwarding to {config.LOCAL_WEBHOOK_URL}")
    print(f"Polling every {config.EMAIL_POLL_INTERVAL_SECONDS} seconds. Ctrl+C to stop.\n")

    while True:
        try:
            conn = connect()
            try:
                process_unread_alerts(conn)
            finally:
                conn.logout()
        except imaplib.IMAP4.error as e:
            print(f"[ERROR] IMAP login/connection failed: {e}")
            print("[ERROR] Double-check GMAIL_ADDRESS and GMAIL_APP_PASSWORD in config.py, "
                  "and that 2-Step Verification is still enabled on the Google account.")
        except Exception as e:
            print(f"[ERROR] Unexpected error during poll cycle: {e}")

        time.sleep(config.EMAIL_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
