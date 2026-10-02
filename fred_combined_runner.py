"""
fred_combined_runner.py
-------------------------
The single entry point that checks all THREE models, across all TEN
pairs, in one execution. This is the script cron-job.org will trigger
once an hour, once this is deployed to Vercel -- it replaces running
zone_entry_detector.py / momentum_detector.py / reversion_detector.py
separately by hand.

No new detection logic lives here. This file only orchestrates the three
already-tested detectors: for each pair, run Zone's check, then
Momentum's, then Reversion's, forward anything that fires, and print a
clean summary at the end. If a bug is ever found in how a model detects
a signal, fix it in that model's own file (zone_entry_detector.py,
momentum_detector.py, or reversion_detector.py) -- never here.

WHY ALL THREE ARE CHECKED FOR EVERY PAIR, EVEN THOUGH ONLY ONE MODEL CAN
ACTUALLY TRADE A GIVEN SYMBOL AT A TIME LIVE: webhook_server.py's own
validate_alert() already rejects a second signal on a symbol that
already has an open position, regardless of which model sent it. So this
script doesn't need to pick a "priority order" between the three models
itself -- it can safely report everything that fires and let the webhook
make the real, live-position-aware decision. This mirrors exactly how
v11's Pine script keeps the three models' entry CONDITIONS fully
independent, with only the live account's real position state as the
actual gate.

CURRENT STATE STORAGE CAVEAT (same as the individual detector files):
zone_state.json and reversion_state.json are local files right now.
This is fine for testing on your own machine, but will NOT survive once
this runs on Vercel (each serverless invocation gets a blank filesystem).
Before deploying, load_state()/save_state() in both zone_entry_detector.py
and reversion_detector.py need to be swapped to read/write an external
store -- most likely a dedicated tab in your existing Google Sheet.
Momentum needs no such change, since it keeps no state between runs.

USAGE:
    python fred_combined_runner.py
(This is also the function Vercel will eventually expose as an HTTP
endpoint for cron-job.org to call -- that wiring is a later step, not
part of this file yet.)
"""

import config
import zone_entry_detector as zone
import momentum_detector as momentum
import reversion_detector as reversion


def check_all_models_for_pair(symbol):
    """
    Runs Zone, then Momentum, then Reversion for a single pair, in that
    order (matching the order they're presented in v11 and everywhere
    else in this project -- an arbitrary but consistent convention, not
    a priority ranking, since the webhook is what actually decides which
    signal wins if more than one somehow fires on the same symbol at
    the exact same hour).

    Returns a list of (model_name, result_or_None) tuples so the caller
    can report on all three, whether or not anything fired.
    """
    results = []

    try:
        zone_result = zone.check_zone_entry(symbol)
    except Exception as e:
        print(f"[ERROR] {symbol} Zone Entry check failed: {e}")
        zone_result = None
    results.append(("Zone Entry", zone_result))

    try:
        momentum_result = momentum.check_momentum_continuation(symbol)
    except Exception as e:
        print(f"[ERROR] {symbol} Momentum Continuation check failed: {e}")
        momentum_result = None
    results.append(("Momentum Continuation", momentum_result))

    try:
        reversion_result = reversion.check_reversion(symbol)
    except Exception as e:
        print(f"[ERROR] {symbol} Mean Reversion check failed: {e}")
        reversion_result = None
    results.append(("Mean Reversion", reversion_result))

    return results


def forward_signal(model_name, payload):
    """Dispatches a fired signal to the right detector's own
    forward_to_webhook() -- each already knows how to reach the webhook
    correctly, no need to duplicate that logic here."""
    if model_name == "Zone Entry":
        zone.forward_to_webhook(payload)
    elif model_name == "Momentum Continuation":
        momentum.forward_to_webhook(payload)
    elif model_name == "Mean Reversion":
        reversion.forward_to_webhook(payload)


def run_all_pairs():
    """The main entry point: checks every pair in config.ALLOWED_PAIRS
    against all three models, forwards anything that fired, and prints
    a clean per-pair, per-model summary."""
    total_checked = 0
    total_fired = 0

    print(f"=== FRED Combined Runner -- checking {len(config.ALLOWED_PAIRS)} pairs across 3 models ===\n")

    for symbol in config.ALLOWED_PAIRS:
        results = check_all_models_for_pair(symbol)
        total_checked += 1

        fired_this_pair = [(name, payload) for name, payload in results if payload is not None]

        if not fired_this_pair:
            print(f"[checked] {symbol} -- no signal from any model")
        else:
            for model_name, payload in fired_this_pair:
                total_fired += 1
                print(f"[SIGNAL] {symbol} -- {model_name} fired -- "
                      f"{payload['side']} Grade {payload['grade']} ({payload['session']})")
                forward_signal(model_name, payload)

    print(f"\n=== Done. {total_checked} pairs checked, {total_fired} signal(s) fired and forwarded. ===")


if __name__ == "__main__":
    run_all_pairs()
