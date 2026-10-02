"""
fred_historical_check.py
---------------------------
Fetches a full year (default 365 days) of real Binance 1H candles for
every pair in config.ALLOWED_PAIRS, replays them through all three FRED
models using the exact same pure detection functions from
fred_detectors.py, and -- unlike the earlier per-model harnesses --
WALKS EACH FIRED SIGNAL FORWARD through the following candles to
determine whether it hit its target or its stop first. This produces a
trade count, win rate, and realized-R summary per pair per model, meant
to be compared directly against your TradingView Strategy Tester output
for the same period.

DRY RUN. Never touches the webhook or Binance orders. State is kept
in memory only for the duration of this run -- it never reads or writes
zone_state.json / reversion_state.json, so it can't interfere with (or
be interfered with by) the live system.

═══════════════════════════════════════════════════════════════════════
IMPORTANT SCOPE LIMITATION -- READ BEFORE COMPARING NUMBERS TO v11
═══════════════════════════════════════════════════════════════════════
This tool does NOT simulate v11's trailing-stop logic (Zone and
Momentum's post-3:1 ATR trail). It only checks whether the ORIGINAL
flat stop or the ORIGINAL flat target is hit first. Per the project's
own Decisions Log: "Trailing only ever activates AFTER the original 3:1
target was already reached... any exit while trailing is still a win —
just a bigger one." That means:
  - WIN/LOSS CLASSIFICATION should closely match v11's backtest, because
    trailing never turns a would-be win into a loss.
  - EXACT DOLLAR TOTALS WILL NOT MATCH, because a trailed win here is
    still just counted as a flat +3R, when v11's real trail could have
    captured more (e.g. the +3.31R example from earlier testing).
So: use this tool to sanity-check TRADE COUNT and WIN RATE against
TradingView's Strategy Tester for the same pairs/period. Don't expect
net P&L to match exactly -- it isn't meant to.

A second, smaller limitation: if a signal's outcome hasn't resolved by
the end of the fetched data (still running when history runs out), it's
reported as "OPEN AT END" rather than forced into a win or loss.

USAGE:
    python fred_historical_check.py               # all pairs, 365 days
    python fred_historical_check.py BTCUSDT 180    # one pair, custom days
"""

import csv
import sys
import time

import fred_detectors as fd
import config

CSV_OUTPUT_FILE = "fred_historical_trades.csv"


def simulate_outcome(candles, entry_index, side, stop, target):
    """
    Walks forward from the candle AFTER entry_index, checking each
    subsequent candle's high/low against stop and target. Returns
    ('WIN', exit_index), ('LOSS', exit_index), or ('OPEN', None) if
    neither is hit before the data runs out.

    Same-candle stop-and-target ambiguity (a single volatile candle's
    wick reaches both): resolved as a LOSS, the conservative assumption
    used by most backtesting conventions, since it can't be known which
    was actually touched first without intra-candle (tick-level) data.
    """
    stop = float(stop)
    target = float(target)
    is_long = side == "BUY"

    for j in range(entry_index + 1, len(candles)):
        high, low = candles[j]["high"], candles[j]["low"]
        hit_stop = (low <= stop) if is_long else (high >= stop)
        hit_target = (high >= target) if is_long else (low <= target)

        if hit_stop and hit_target:
            return "LOSS", j  # conservative tie-break, see docstring
        if hit_stop:
            return "LOSS", j
        if hit_target:
            return "WIN", j

    return "OPEN", None


def realized_r(side, entry, stop, target, outcome):
    """R-multiple for this trade: +reward/risk on a win, -1 on a loss."""
    entry, stop, target = float(entry), float(stop), float(target)
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0
    if outcome == "WIN":
        return abs(target - entry) / risk
    elif outcome == "LOSS":
        return -1.0
    return 0.0  # OPEN trades don't contribute to realized R


def run_check_for_pair(symbol, days_back):
    print(f"\n{'='*70}\n{symbol} -- fetching {days_back} days of 1H candles...")
    candles = fd.fetch_historical_candles(symbol, days_back)
    print(f"Got {len(candles)} candles, spanning "
          f"{time.strftime('%Y-%m-%d', time.gmtime(candles[0]['open_time']/1000))} to "
          f"{time.strftime('%Y-%m-%d', time.gmtime(candles[-1]['open_time']/1000))} UTC.")

    start_index = fd.CANDLES_NEEDED + 1
    if len(candles) <= start_index:
        print(f"Not enough candles ({len(candles)}) for the {fd.CANDLES_NEEDED}-candle warm-up. Skipping.")
        return {}, []

    zone_state = fd.default_zone_state()
    reversion_state = fd.default_reversion_state()

    # PERFORMANCE: indicators are computed ONCE across the full fetched
    # history, not re-derived from scratch on every single candle of the
    # replay. Every indicator value at index i only ever depends on
    # candles at or before i (fully causal), so this produces IDENTICAL
    # results to the old per-candle-slice approach -- it was purely
    # wasted, repeated work before, which is what made a 365-day check
    # effectively never finish. Verified via direct regression test
    # against the original slower method.
    print("Computing indicators across the full fetched history...")
    ind = fd.compute_indicators(candles)

    # trades[model] = list of dicts: {entry_index, side, entry, stop, target, timestamp}
    trades = {"Zone Entry": [], "Momentum Continuation": [], "Mean Reversion": []}

    for i in range(start_index, len(candles)):
        zone_result, zone_state = fd.zone_entry_check(symbol, candles, zone_state, precomputed_ind=ind, index=i)
        if zone_result:
            trades["Zone Entry"].append({"entry_index": i, **zone_result})

        momentum_result = fd.momentum_check(symbol, candles, precomputed_ind=ind, index=i)
        if momentum_result:
            trades["Momentum Continuation"].append({"entry_index": i, **momentum_result})

        reversion_result, reversion_state = fd.reversion_check(symbol, candles, reversion_state, precomputed_ind=ind, index=i)
        if reversion_result:
            trades["Mean Reversion"].append({"entry_index": i, **reversion_result})

    # Now resolve every fired trade's outcome by walking forward
    summary = {}
    for model_name, model_trades in trades.items():
        wins = losses = opens = 0
        total_r = 0.0
        for tr in model_trades:
            outcome, exit_idx = simulate_outcome(candles, tr["entry_index"], tr["side"], tr["stop"], tr["target"])
            tr["outcome"] = outcome
            tr["exit_index"] = exit_idx  # None if still OPEN at the end of fetched data
            r = realized_r(tr["side"], tr["entry"], tr["stop"], tr["target"], outcome)
            tr["r"] = r
            total_r += r
            if outcome == "WIN":
                wins += 1
            elif outcome == "LOSS":
                losses += 1
            else:
                opens += 1

        resolved = wins + losses
        win_rate = (wins / resolved * 100) if resolved > 0 else 0.0
        summary[model_name] = {
            "trades": len(model_trades), "wins": wins, "losses": losses,
            "open": opens, "win_rate": win_rate, "total_r": total_r,
        }

        print(f"\n  {model_name}: {len(model_trades)} signals -- "
              f"{wins}W / {losses}L / {opens} still open -- "
              f"win rate {win_rate:.1f}% -- total {total_r:+.2f}R")
        for tr in model_trades:
            ts = time.strftime('%Y-%m-%d %H:%M', time.gmtime(candles[tr['entry_index']]['close_time'] / 1000))
            print(f"    {ts} UTC -- {tr['side']} Grade {tr['grade']} -- {tr['outcome']} ({tr['r']:+.2f}R)")

    # Flatten every individual trade (across all 3 models) into one list,
    # with a human-readable timestamp and the symbol attached, ready to
    # hand off to the CSV writer -- this is the "List of Trades" analog.
    flat_trades = []
    for model_name, model_trades in trades.items():
        for tr in model_trades:
            # exit_timestamp: None (still OPEN at end of data) is written
            # as an empty CSV cell -- the simulator treats that case as
            # "occupies its slot until the data window ends," the most
            # conservative assumption available without more data.
            exit_ts = (time.strftime('%Y-%m-%d %H:%M', time.gmtime(candles[tr['exit_index']]['close_time'] / 1000))
                       if tr["exit_index"] is not None else "")
            flat_trades.append({
                "symbol": symbol,
                "strategy": model_name,
                "entry_timestamp": time.strftime('%Y-%m-%d %H:%M', time.gmtime(candles[tr['entry_index']]['close_time'] / 1000)),
                "exit_timestamp": exit_ts,
                "side": tr["side"],
                "entry": tr["entry"],
                "stop": tr["stop"],
                "target": tr["target"],
                "grade": tr["grade"],
                "session": tr["session"],
                "outcome": tr["outcome"],
                "r_multiple": round(tr["r"], 3),
            })

    return summary, flat_trades


def export_trades_to_csv(all_trades, filename=CSV_OUTPUT_FILE):
    """
    Writes every trade found across every pair/model to a single CSV,
    in the same spirit as TradingView's own Strategy Tester "List of
    Trades" export. Import this into Google Sheets via
    File -> Import -> Upload, choosing "Insert new sheet" or "Replace
    current sheet" as you prefer.
    """
    if not all_trades:
        print(f"\nNo trades to export -- {filename} not created.")
        return

    fieldnames = ["symbol", "strategy", "entry_timestamp", "exit_timestamp", "side", "entry", "stop",
                  "target", "grade", "session", "outcome", "r_multiple"]
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for trade in all_trades:
            writer.writerow(trade)

    print(f"\nExported {len(all_trades)} trades to {filename}")
    print(f"To view in Google Sheets: open sheets.google.com -> File -> Import -> "
          f"Upload -> select {filename} -> Insert new sheet (or Replace, your choice).")


def run_all(symbols, days_back):
    grand_totals = {"Zone Entry": {"trades": 0, "wins": 0, "losses": 0, "total_r": 0.0},
                     "Momentum Continuation": {"trades": 0, "wins": 0, "losses": 0, "total_r": 0.0},
                     "Mean Reversion": {"trades": 0, "wins": 0, "losses": 0, "total_r": 0.0}}
    all_trades_for_csv = []

    for symbol in symbols:
        try:
            summary, flat_trades = run_check_for_pair(symbol, days_back)
        except Exception as e:
            print(f"[ERROR] {symbol} failed: {e}")
            continue
        all_trades_for_csv.extend(flat_trades)
        for model_name, stats in summary.items():
            grand_totals[model_name]["trades"] += stats["trades"]
            grand_totals[model_name]["wins"] += stats["wins"]
            grand_totals[model_name]["losses"] += stats["losses"]
            grand_totals[model_name]["total_r"] += stats["total_r"]

    print(f"\n\n{'='*70}\nGRAND TOTALS ACROSS ALL {len(symbols)} PAIRS ({days_back} days)\n{'='*70}")
    for model_name, t in grand_totals.items():
        resolved = t["wins"] + t["losses"]
        wr = (t["wins"] / resolved * 100) if resolved > 0 else 0.0
        print(f"{model_name}: {t['trades']} signals -- {t['wins']}W / {t['losses']}L -- "
              f"win rate {wr:.1f}% -- total {t['total_r']:+.2f}R")
    print(f"\nCompare trade counts and win rates above against your TradingView Strategy")
    print(f"Tester results for the same pairs and period. Remember: total R here will run")
    print(f"lower than v11's real backtest, since trailing-stop upside isn't simulated here.")

    export_trades_to_csv(all_trades_for_csv)


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        run_all([sys.argv[1].upper()], int(sys.argv[2]))
    elif len(sys.argv) == 2:
        run_all([sys.argv[1].upper()], 365)
    else:
        run_all(config.ALLOWED_PAIRS, 365)
