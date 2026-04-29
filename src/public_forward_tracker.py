"""
Public Forward Tracker — daily prediction logging + scoring.

Mirrors the internal forward_tracker structure but reads only
public-data signals. Predictions go to tracker/public/predictions.json;
score goes to tracker/public/scorecard.csv. Designed to commit to git
daily so each entry's timestamp is independently verifiable.

The proof is the file's git history, not the prediction itself.

Usage:
    python public_forward_tracker.py --report   # log today's predictions
    python public_forward_tracker.py --score    # score predictions >=10d old
    python public_forward_tracker.py --digest   # cumulative hit rate by signal

Wire into a daily cron at any time after the bar close.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from public_signals import (
    PUBLIC_SIGNAL_REGISTRY,
    ROBUST_PUBLIC_SIGNALS,
    CONTRARIAN_PUBLIC_SIGNALS,
    compute_public_signals,
)
from safe_io import atomic_write_json, file_lock, safe_read_json

TRACKER_DIR = ROOT / "tracker" / "public"
PREDICTIONS_FILE = TRACKER_DIR / "predictions.json"
SCORECARD_FILE = TRACKER_DIR / "scorecard.csv"

FORWARD_WINDOW_BARS = 10  # 10-day forward return window
EXECUTION_LAG_BARS = 1   # T+1 entry to prevent same-bar lookahead


# ---------------------------------------------------------------------------
# Dataset loading — replace with your public-data builder
# ---------------------------------------------------------------------------

def _load_dataset() -> pd.DataFrame:
    """Load the public dataset.

    Expected columns: trade_date, btc_close, btc_return, btc_volume, optionally
    btc_funding_rate, fear_greed_value, stablecoin_mcap, short_liquidations_usd,
    long_liquidations_usd, put_call_ratio, btc_iv, depth_bid_share.

    The internal edition reads from `cache/master_dataset.csv` produced by
    `data_loader.build_master_dataset`. The public edition expects you to
    write a `build_public_dataset.py` that fetches the public APIs.
    """
    public_csv = ROOT / "cache" / "public_dataset.csv"
    if public_csv.exists():
        return pd.read_csv(public_csv, parse_dates=["trade_date"])

    # Fallback: try the internal cache and strip platform-specific columns
    # (in case you're running this against the internal data layer in dev).
    internal_csv = ROOT.parent / "cache" / "master_dataset.csv"
    if internal_csv.exists():
        df = pd.read_csv(internal_csv, parse_dates=["trade_date"])
        sfox_cols = [c for c in df.columns
                     if any(p in c for p in
                            ("prop_", "hedge_", "internalization", "platform_"))]
        return df.drop(columns=sfox_cols, errors="ignore")

    raise FileNotFoundError(
        f"No public dataset found. Build one with: "
        f"python src/build_public_dataset.py"
    )


# ---------------------------------------------------------------------------
# Logging predictions
# ---------------------------------------------------------------------------

def log_predictions(df: pd.DataFrame, target_date=None) -> dict:
    """For each public signal firing on target_date, log a prediction:
    direction (long/short), expected (BTC up/down), 10d forward window."""
    df = compute_public_signals(df)
    target_date = pd.Timestamp(target_date or df["trade_date"].max())
    today_row = df[df["trade_date"] == target_date]
    if today_row.empty:
        return {"date": str(target_date.date()), "predictions": [], "no_data": True}
    row = today_row.iloc[0]

    predictions = []
    for name in PUBLIC_SIGNAL_REGISTRY:
        col = f"sig_{name}"
        if col not in df.columns or not bool(row[col]):
            continue
        if name in CONTRARIAN_PUBLIC_SIGNALS:
            direction, expected = "short", "BTC down"
        else:
            direction, expected = "long", "BTC up"
        predictions.append({
            "signal": name,
            "direction": direction,
            "expected": expected,
            "robust": bool(name in ROBUST_PUBLIC_SIGNALS),
            "btc_close": float(row["btc_close"]),
        })

    entry = {
        "date": str(target_date.date()),
        "logged_at": datetime.now().isoformat(),
        "btc_close": float(row["btc_close"]),
        "predictions": predictions,
        "scored": False,
    }

    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    with file_lock(PREDICTIONS_FILE):
        all_preds = safe_read_json(PREDICTIONS_FILE, default={})
        all_preds[entry["date"]] = entry
        atomic_write_json(PREDICTIONS_FILE, all_preds)

    return entry


# ---------------------------------------------------------------------------
# Scoring predictions
# ---------------------------------------------------------------------------

def score_predictions(df: pd.DataFrame) -> int:
    """Score every prediction whose forward window has now closed.

    A prediction logged on date D with forward_window=10 can be scored on
    date D + 11 (the +1 covers T+1 execution lag).
    """
    df = df.sort_values("trade_date").reset_index(drop=True)
    today = pd.Timestamp(df["trade_date"].max())

    with file_lock(PREDICTIONS_FILE):
        all_preds = safe_read_json(PREDICTIONS_FILE, default={})
        scored_count = 0

        for date_str, entry in list(all_preds.items()):
            if entry.get("scored"):
                continue
            log_date = pd.Timestamp(date_str)
            close_date = log_date + pd.Timedelta(days=FORWARD_WINDOW_BARS + EXECUTION_LAG_BARS)
            if close_date > today:
                continue

            entry_idx = df.index[df["trade_date"] == log_date]
            close_idx = df.index[df["trade_date"] == close_date]
            if len(entry_idx) == 0 or len(close_idx) == 0:
                continue

            ai = entry_idx[0] + EXECUTION_LAG_BARS
            ci = close_idx[0]
            if ai >= len(df) or ci >= len(df):
                continue

            entry_px = df.iloc[ai]["btc_close"]
            exit_px = df.iloc[ci]["btc_close"]
            fwd_return = exit_px / entry_px - 1

            results = []
            for pred in entry["predictions"]:
                if pred["direction"] == "long":
                    correct = fwd_return > 0
                else:
                    correct = fwd_return < 0
                results.append({
                    "signal": pred["signal"],
                    "direction": pred["direction"],
                    "fwd_return": round(fwd_return, 4),
                    "correct": bool(correct),
                })

            entry["outcome"] = {
                "scored_at": datetime.now().isoformat(),
                "entry_price": float(entry_px),
                "exit_price": float(exit_px),
                "fwd_return": round(fwd_return, 4),
                "results": results,
            }
            entry["scored"] = True
            all_preds[date_str] = entry
            scored_count += 1

        if scored_count:
            atomic_write_json(PREDICTIONS_FILE, all_preds)

    return scored_count


# ---------------------------------------------------------------------------
# Digest — cumulative hit rate per signal
# ---------------------------------------------------------------------------

def digest() -> pd.DataFrame:
    all_preds = safe_read_json(PREDICTIONS_FILE, default={})
    rows = []
    for date_str, entry in all_preds.items():
        if not entry.get("scored"):
            continue
        for r in entry.get("outcome", {}).get("results", []):
            rows.append({
                "signal": r["signal"],
                "direction": r["direction"],
                "correct": r["correct"],
                "fwd_return": r["fwd_return"],
                "date": date_str,
            })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    summary = (
        df.groupby("signal")
        .agg(n=("correct", "size"),
             hits=("correct", "sum"),
             avg_fwd_return=("fwd_return", "mean"))
        .reset_index()
    )
    summary["hit_rate_pct"] = (summary["hits"] / summary["n"] * 100).round(1)
    summary["avg_fwd_return_pct"] = (summary["avg_fwd_return"] * 100).round(2)
    return summary[["signal", "n", "hits", "hit_rate_pct", "avg_fwd_return_pct"]]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                    help="Log today's predictions")
    ap.add_argument("--score", action="store_true",
                    help="Score predictions whose forward window has closed")
    ap.add_argument("--digest", action="store_true",
                    help="Cumulative hit rate per signal")
    ap.add_argument("--date", help="Override target date (YYYY-MM-DD)")
    args = ap.parse_args()

    df = _load_dataset()

    if args.report:
        entry = log_predictions(df, target_date=args.date)
        print(json.dumps(entry, indent=2, default=str))
    if args.score:
        n = score_predictions(df)
        print(f"Scored {n} predictions")
    if args.digest:
        d = digest()
        if d.empty:
            print("No scored predictions yet")
        else:
            print(d.to_string(index=False))

    if not (args.report or args.score or args.digest):
        # Default: full daily run — log + score + digest
        entry = log_predictions(df, target_date=args.date)
        print(f"Logged {len(entry['predictions'])} prediction(s) for {entry['date']}")
        n = score_predictions(df)
        print(f"Scored {n} predictions whose 10d window closed")
        d = digest()
        if not d.empty:
            print("\nDigest:")
            print(d.to_string(index=False))


if __name__ == "__main__":
    main()
