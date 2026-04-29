#!/usr/bin/env python3
"""
Piper Changelog — structured log of fixes, features, optimizations, and their metric impact.

CLI usage:
    python3 changelog.py list                        # show all entries
    python3 changelog.py list --category fix         # filter by category
    python3 changelog.py list --last 5               # last N entries

    python3 changelog.py add \\
        --category optimization \\
        --title "Per-pair stop-loss" \\
        --description "Swept 2.0-4.0 ATR per pair" \\
        --files config.py backtest_engine.py \\
        --tags stop-loss per-pair \\
        --before btc_sharpe=1.52 \\
        --after btc_sharpe=1.56

    python3 changelog.py snapshot                    # capture current metrics

Library usage:
    from changelog import add_entry, snapshot_metrics, load_changelog
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

CHANGELOG_FILE = Path(__file__).resolve().parent / "tracker" / "changelog.json"

VALID_CATEGORIES = {"fix", "feature", "optimization", "test", "refactor", "audit"}

METRIC_KEYS = [
    "btc_sharpe", "eth_sharpe", "sol_sharpe", "portfolio_sharpe",
    "btc_annual_return", "eth_annual_return", "sol_annual_return",
    "btc_max_dd", "eth_max_dd", "sol_max_dd",
    "btc_calmar", "btc_win_rate",
    "forward_hit_rate", "forward_n",
]


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def load_changelog():
    """Load changelog entries from JSON file."""
    if not CHANGELOG_FILE.exists():
        return []
    return json.loads(CHANGELOG_FILE.read_text())


def _save_changelog(entries):
    from safe_io import atomic_write_json
    atomic_write_json(CHANGELOG_FILE, entries)


def _next_id(date_str, entries):
    """Generate sequential ID for a given date: YYYY-MM-DD-NNN."""
    existing = [e["id"] for e in entries if e["id"].startswith(date_str)]
    seq = len(existing) + 1
    return f"{date_str}-{seq:03d}"


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def add_entry(category, title, description="", files_changed=None,
              metrics_before=None, metrics_after=None, tags=None, date=None):
    """
    Add a changelog entry programmatically.

    Args:
        category: one of fix, feature, optimization, test, refactor, audit
        title: short summary (1 line)
        description: detailed explanation
        files_changed: list of filenames
        metrics_before: dict of metric_name -> value (before change)
        metrics_after: dict of metric_name -> value (after change)
        tags: list of string tags
        date: override date (YYYY-MM-DD), defaults to today

    Returns:
        The new entry dict.
    """
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {VALID_CATEGORIES}")

    # Lock around load -> append -> save so two concurrent add_entry callers
    # (e.g. weekly-review agent + manual edit) cannot lose each other's entry.
    from safe_io import file_lock
    with file_lock(CHANGELOG_FILE):
        entries = load_changelog()
        date_str = date or datetime.now().strftime("%Y-%m-%d")
        entry_id = _next_id(date_str, entries)

        entry = {
            "id": entry_id,
            "date": date_str,
            "category": category,
            "title": title,
            "description": description,
            "files_changed": files_changed or [],
            "metrics_before": metrics_before or {},
            "metrics_after": metrics_after or {},
            "tags": tags or [],
        }

        entries.append(entry)
        _save_changelog(entries)
    return entry


def snapshot_metrics():
    """
    Run backtests and capture current system metrics.
    Returns dict of metric_name -> value.
    """
    import warnings
    warnings.filterwarnings("ignore")

    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from dashboard_data import load_master_dataset, run_all_backtests
        from multi_pair import run_pair_backtest
        from analysis import compute_metrics

    df = load_master_dataset()
    if df.empty:
        print("No data available for snapshot.")
        return {}

    metrics = {}

    # Per-pair long_short metrics (all supported pairs, not just primary 3)
    from pair_registry import get_pairs
    for pair in get_pairs():
        prefix = pair.lower()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_pair_backtest(df, pair, "long_short")
            if result is not None:
                m = compute_metrics(result)
                metrics[f"{prefix}_sharpe"] = round(m["sharpe_ratio"], 3)
                metrics[f"{prefix}_annual_return"] = round(m["annualized_return_pct"], 1)
                metrics[f"{prefix}_max_dd"] = round(m["max_drawdown_pct"], 1)
                metrics[f"{prefix}_calmar"] = round(m["calmar_ratio"], 2)
                if pair == "BTC":
                    metrics["btc_win_rate"] = round(m["win_rate_pct"], 1)
        except Exception as e:
            print(f"  [{pair}] backtest failed: {e}")

    # Forward tracker metrics
    try:
        pred_file = Path(__file__).resolve().parent / "tracker" / "predictions.json"
        if pred_file.exists():
            preds = json.loads(pred_file.read_text())
            scored = [v for v in preds.values() if v.get("scored") and v.get("outcome")]
            if scored:
                total = sum(len(e["outcome"]["results"]) for e in scored)
                correct = sum(
                    sum(1 for r in e["outcome"]["results"] if r.get("correct"))
                    for e in scored
                )
                metrics["forward_hit_rate"] = round(correct / total * 100, 1) if total else 0
                metrics["forward_n"] = total
    except Exception:
        pass

    return metrics


# ---------------------------------------------------------------------------
# CLI display
# ---------------------------------------------------------------------------

def _print_entry(e, verbose=True):
    cat_colors = {
        "fix": "\033[91m", "feature": "\033[92m", "optimization": "\033[93m",
        "test": "\033[94m", "refactor": "\033[95m", "audit": "\033[96m",
    }
    reset = "\033[0m"
    color = cat_colors.get(e["category"], "")

    print(f"  {e['date']}  {color}[{e['category']:^12}]{reset}  {e['title']}")

    if verbose and e.get("description"):
        for line in e["description"].split("\n"):
            print(f"             {line}")

    # Show metric deltas. Numeric values get a delta; non-numeric (e.g. enum
    # transitions like "binary_30_trade" -> "per_pair_tier_graduation") render
    # without a delta. Previously this crashed with TypeError on string-typed
    # metrics (audit fix 2026-04-28).
    before = e.get("metrics_before", {})
    after = e.get("metrics_after", {})
    if before and after:
        deltas = []
        for k in after:
            if k in before:
                bv, av = before[k], after[k]
                if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
                    delta = av - bv
                    sign = "+" if delta >= 0 else ""
                    deltas.append(f"{k}: {bv} -> {av} ({sign}{delta:.3g})")
                else:
                    deltas.append(f"{k}: {bv} -> {av}")
        if deltas:
            print(f"             Impact: {', '.join(deltas)}")

    if verbose and e.get("tags"):
        print(f"             Tags: {', '.join(e['tags'])}")


def _parse_metrics(pairs):
    """Parse key=value pairs into dict."""
    result = {}
    for p in (pairs or []):
        if "=" in p:
            k, v = p.split("=", 1)
            result[k.strip()] = float(v.strip())
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Piper Changelog")
    sub = parser.add_subparsers(dest="command")

    # list
    ls = sub.add_parser("list", help="List changelog entries")
    ls.add_argument("--category", choices=VALID_CATEGORIES)
    ls.add_argument("--last", type=int, help="Show last N entries")
    ls.add_argument("--brief", action="store_true", help="One-line per entry")

    # add
    add = sub.add_parser("add", help="Add a changelog entry")
    add.add_argument("--category", required=True, choices=VALID_CATEGORIES)
    add.add_argument("--title", required=True)
    add.add_argument("--description", default="")
    add.add_argument("--files", nargs="*", default=[])
    add.add_argument("--tags", nargs="*", default=[])
    add.add_argument("--before", nargs="*", help="Metrics before: key=value pairs")
    add.add_argument("--after", nargs="*", help="Metrics after: key=value pairs")
    add.add_argument("--date", help="Override date (YYYY-MM-DD)")

    # snapshot
    sub.add_parser("snapshot", help="Capture current system metrics")

    args = parser.parse_args()

    if args.command == "list":
        entries = load_changelog()
        if args.category:
            entries = [e for e in entries if e["category"] == args.category]
        if args.last:
            entries = entries[-args.last:]

        if not entries:
            print("No changelog entries found.")
            return

        print(f"\n{'='*72}")
        print(f"  PIPER CHANGELOG — {len(entries)} entries")
        print(f"{'='*72}\n")

        for e in entries:
            _print_entry(e, verbose=not args.brief)
            if not args.brief:
                print()

    elif args.command == "add":
        entry = add_entry(
            category=args.category,
            title=args.title,
            description=args.description,
            files_changed=args.files,
            metrics_before=_parse_metrics(args.before),
            metrics_after=_parse_metrics(args.after),
            tags=args.tags,
            date=args.date,
        )
        print(f"Added: {entry['id']} — {entry['title']}")

    elif args.command == "snapshot":
        print("Running backtests to capture current metrics...")
        metrics = snapshot_metrics()
        if metrics:
            print(f"\nCurrent metrics ({datetime.now().strftime('%Y-%m-%d')}):")
            for k, v in sorted(metrics.items()):
                print(f"  {k}: {v}")
        else:
            print("No metrics captured.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
