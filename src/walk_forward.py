#!/usr/bin/env python3
"""
Walk-Forward Validation — rolling train/test split to detect overfitting.

Instead of a single IS/OOS split, this slides a window forward:
  - Train on 180 days → test on next 30 days → slide forward 30 days → repeat

This gives multiple independent test periods and reveals whether a strategy
is consistently profitable or just captured one lucky regime.

Usage:
    python walk_forward.py              # run walk-forward on 2-year data
    python walk_forward.py --cache      # use cached dataset
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from signals import compute_all_signals, SIGNAL_REGISTRY, ROBUST_SIGNALS, REVERSED_SIGNALS
from strategies import get_all_strategies
from backtest_engine import run_backtest
from analysis import compute_metrics


DEFAULT_TRAIN_DAYS = 180
DEFAULT_TEST_DAYS = 30


def walk_forward_backtest(df, strategy, train_days=DEFAULT_TRAIN_DAYS,
                          test_days=DEFAULT_TEST_DAYS, fee_bps=None, slippage_bps=None,
                          hold_days=None, verbose=False):
    """
    Run walk-forward validation for a single strategy.

    Slides a window: [i : i+train_days] is WARMUP (gives rolling indicators
    enough history to compute), [i+train_days : i+train_days+test_days] is
    the out-of-sample test window whose metrics we report.

    The strategy doesn't actually "train" (no parameter fitting) — this
    tests whether the signal's edge persists across different time windows.

    Fix 2026-04-20: previously passed only the 30-day test slice to
    run_backtest, which made all rolling indicators NaN (signals need
    50d+ history). Result: 0 trades, 0 Sharpe across ALL robust strategies.
    Now passes warmup+test slice and filters trades to only those entering
    the test window.

    Returns DataFrame of per-window results.
    """
    fee_bps = fee_bps if fee_bps is not None else config.TRADING_FEE_BPS
    slippage_bps = slippage_bps if slippage_bps is not None else config.SLIPPAGE_BPS
    hold_days = hold_days if hold_days is not None else config.DEFAULT_HOLD_DAYS

    n = len(df)
    windows = []
    step = test_days
    errors = 0

    i = 0
    while i + train_days + test_days <= n:
        test_start = i + train_days
        test_end = test_start + test_days

        # Warmup+test slice — preserves rolling-indicator history
        full_slice = df.iloc[i:test_end].copy().reset_index(drop=True)
        test_start_local = train_days  # index within the slice where test window begins

        if len(full_slice) < (train_days + test_days) * 0.8:
            i += step
            continue

        try:
            # Use asymmetric hold (10d long / 5d short) to match the canonical
            # backtest configuration. Without hold_days_short, shorts held 10d
            # and lost their 5d alpha — dropped OOS Sharpe ~0.5 artificially.
            hold_short = config.DEFAULT_HOLD_DAYS_SHORT if hold_days == config.DEFAULT_HOLD_DAYS else None
            result = run_backtest(full_slice, strategy, fee_bps=fee_bps,
                                  slippage_bps=slippage_bps, hold_days=hold_days,
                                  hold_days_short=hold_short,
                                  stop_atr_mult=getattr(strategy, 'stop_atr_mult', None),
                                  tp_atr_mult=getattr(strategy, 'tp_atr_mult', None))

            # Filter trades to those entering in the TEST window only
            trades = result["trades"]
            if not trades.empty:
                test_window_start = full_slice["trade_date"].iloc[test_start_local]
                trades = trades[trades["entry_date"] >= test_window_start].copy()

            # Recompute metrics on the test-window-only trades + returns
            positions = result["positions"]
            returns = result["returns"]
            # Slice returns to the test window only
            test_returns = returns.iloc[test_start_local:].copy()
            if len(test_returns) == 0 or test_returns.isna().all():
                i += step
                continue
            test_equity = config.INITIAL_CAPITAL * (1 + test_returns).cumprod()

            window_result = {
                "equity_curve": test_equity,
                "returns": test_returns,
                "trades": trades,
                "positions": positions.iloc[test_start_local:],
                "strategy_name": strategy.name,
            }
            metrics = compute_metrics(window_result)

            windows.append({
                "window_start": df["trade_date"].iloc[test_start].strftime("%Y-%m-%d"),
                "window_end": df["trade_date"].iloc[test_end - 1].strftime("%Y-%m-%d"),
                "return_pct": metrics["total_return_pct"],
                "sharpe": metrics["sharpe_ratio"],
                "max_dd_pct": metrics["max_drawdown_pct"],
                "trades": metrics["total_trades"],
                "win_rate_pct": metrics["win_rate_pct"],
            })
        except Exception as e:
            errors += 1
            if verbose:
                print(f"  [{strategy.name}] window {i}: {type(e).__name__}: {e}")

        i += step

    if verbose and errors:
        print(f"  [{strategy.name}] {errors} window(s) raised (see above)")

    return pd.DataFrame(windows)


def walk_forward_signals(df, train_days=DEFAULT_TRAIN_DAYS, test_days=DEFAULT_TEST_DAYS,
                         forward_window=10, execution_lag=1):
    """
    Walk-forward signal hit rate validation.
    For each rolling test window, compute per-signal hit rates.
    Returns consistency metrics across windows.
    """
    n = len(df)
    step = test_days
    signal_windows = {name: [] for name in SIGNAL_REGISTRY}

    fwd_return = (
        df["btc_return"]
        .shift(-execution_lag)
        .rolling(forward_window).sum()
        .shift(-(forward_window - 1))
    )

    short_signals = {"vol_compression", "taker_dump", "volume_shock_down", "funding_extreme"}

    i = 0
    while i + train_days + test_days <= n:
        test_start = i + train_days
        test_end = test_start + test_days

        for name in SIGNAL_REGISTRY:
            col = f"sig_{name}"
            if col not in df.columns:
                continue

            active = df[col].iloc[test_start:test_end]
            fwd = fwd_return.iloc[test_start:test_end]

            active_mask = active == True
            fwd_active = fwd[active_mask].dropna()

            if len(fwd_active) >= 3:  # need at least 3 observations per window
                if name in short_signals:
                    hit_rate = (fwd_active < 0).mean() * 100
                else:
                    hit_rate = (fwd_active > 0).mean() * 100
                avg_ret = fwd_active.mean() * 100
                signal_windows[name].append({
                    "hit_rate": hit_rate,
                    "avg_ret": avg_ret,
                    "n": len(fwd_active),
                })

        i += step

    return signal_windows


def main():
    parser = argparse.ArgumentParser(description="Walk-Forward Validation")
    parser.add_argument("--cache", action="store_true", help="Use cached 2-year dataset")
    parser.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS)
    parser.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    args = parser.parse_args()

    # Load the current master dataset — same file run_backtest.py uses.
    # Fix 2026-04-20: previously loaded a stale `master_dataset_2yr.csv`
    # that had older signal definitions (17 microflow_signal_B fires vs the
    # current 42+; 18 contrarian_signal_B vs 63). The walk-forward was measuring
    # against an obsolete feature set. Now aligned with run_backtest input.
    master = config.CACHE_DIR / "master_dataset.csv"
    if args.cache and master.exists():
        print(f"Loading cached master dataset...")
        df = pd.read_csv(master, parse_dates=["trade_date"])
    else:
        from data_loader import load_cached_or_build
        df = load_cached_or_build(use_cache=False)

    df = compute_all_signals(df)

    print("=" * 95)
    print(f"WALK-FORWARD VALIDATION — Train: {args.train_days}d, Test: {args.test_days}d")
    print(f"Period: {df['trade_date'].iloc[0].date()} to {df['trade_date'].iloc[-1].date()}")
    print("=" * 95)

    # --- Strategy walk-forward ---
    print("\n--- Strategy Performance Across Windows ---")
    strategies = get_all_strategies()
    summary_rows = []

    for strat in strategies:
        wf = walk_forward_backtest(df, strat, args.train_days, args.test_days)
        if wf.empty:
            continue

        n_windows = len(wf)
        n_positive = (wf["return_pct"] > 0).sum()
        avg_ret = wf["return_pct"].mean()
        std_ret = wf["return_pct"].std()
        avg_sharpe = wf["sharpe"].mean()
        worst_window = wf["return_pct"].min()
        best_window = wf["return_pct"].max()

        consistency = n_positive / n_windows * 100 if n_windows > 0 else 0

        summary_rows.append({
            "strategy": strat.name,
            "windows": n_windows,
            "consistency_%": round(consistency, 0),
            "avg_return_%": round(avg_ret, 2),
            "std_return_%": round(std_ret, 2),
            "avg_sharpe": round(avg_sharpe, 2),
            "worst_%": round(worst_window, 2),
            "best_%": round(best_window, 2),
        })

    summary_df = pd.DataFrame(summary_rows)
    print(f"\n{'Strategy':<20} {'Win%':>6} {'AvgRet%':>8} {'StdRet%':>8} {'AvgShp':>7} {'Worst%':>8} {'Best%':>7} {'N':>4}")
    print("-" * 95)
    for _, row in summary_df.sort_values("consistency_%", ascending=False).iterrows():
        print(
            f"{row['strategy']:<20} "
            f"{row['consistency_%']:>5.0f}% "
            f"{row['avg_return_%']:>8.2f} "
            f"{row['std_return_%']:>8.2f} "
            f"{row['avg_sharpe']:>7.2f} "
            f"{row['worst_%']:>8.2f} "
            f"{row['best_%']:>7.2f} "
            f"{int(row['windows']):>4}"
        )

    # --- Signal walk-forward ---
    print("\n\n--- Signal Consistency Across Windows ---")
    sig_windows = walk_forward_signals(df, args.train_days, args.test_days)

    print(f"\n{'Signal':<25} {'Windows':>8} {'AvgHit%':>8} {'StdHit%':>8} {'Consistent':>11} {'OOS':>10}")
    print("-" * 80)

    sig_summary = []
    for name, windows in sig_windows.items():
        if not windows:
            continue
        hits = [w["hit_rate"] for w in windows]
        n_win = len(hits)
        avg_hit = np.mean(hits)
        std_hit = np.std(hits)
        # Consistent = hit rate > 50% in majority of windows
        n_consistent = sum(1 for h in hits if h > 50)
        consistency = n_consistent / n_win * 100 if n_win > 0 else 0

        oos_tag = "ROBUST" if name in ROBUST_SIGNALS else ("REVERSED" if name in REVERSED_SIGNALS else "-")

        sig_summary.append((consistency, name, n_win, avg_hit, std_hit, oos_tag))

    for consistency, name, n_win, avg_hit, std_hit, oos_tag in sorted(sig_summary, reverse=True):
        print(
            f"{name:<25} {n_win:>8} {avg_hit:>8.1f} {std_hit:>8.1f} "
            f"{consistency:>10.0f}% {oos_tag:>10}"
        )

    # --- Final verdict ---
    print("\n\n--- VERDICT ---")
    robust_strats = summary_df[summary_df["consistency_%"] >= 55].sort_values("avg_sharpe", ascending=False)
    if not robust_strats.empty:
        print("Strategies with >55% positive windows:")
        for _, row in robust_strats.iterrows():
            print(f"  {row['strategy']}: {row['consistency_%']:.0f}% consistency, "
                  f"avg Sharpe {row['avg_sharpe']:.2f}, avg return {row['avg_return_%']:.2f}%")
    else:
        print("No strategy achieved >55% consistency across walk-forward windows.")

    # --- Aggregate rolling-OOS metric for headline strategies ---
    # This is what diligence actually asks for: stitch all OOS daily returns
    # into one continuous series and compute a single Sharpe. Averaging
    # per-window Sharpes (above) is noisy on 30-day samples.
    print("\n\n--- AGGREGATE ROLLING OOS (stitched daily returns) ---")
    aggregate_rows = []
    for strat_name in ["long_short", "confluence", "vol_sized", "enhanced",
                       "prop_vol_sized", "robust_composite"]:
        strat = next((s for s in strategies if s.name == strat_name), None)
        if strat is None:
            continue
        stitched = []
        i = 0
        while i + args.train_days + args.test_days <= len(df):
            full_slice = df.iloc[i:i + args.train_days + args.test_days].copy().reset_index(drop=True)
            try:
                r = run_backtest(full_slice, strat,
                                 hold_days=config.DEFAULT_HOLD_DAYS,
                                 hold_days_short=config.DEFAULT_HOLD_DAYS_SHORT,
                                 stop_atr_mult=getattr(strat, 'stop_atr_mult', None),
                                 tp_atr_mult=getattr(strat, 'tp_atr_mult', None))
                stitched.extend(r["returns"].iloc[args.train_days:].tolist())
            except Exception:
                pass
            i += args.test_days

        sr = pd.Series(stitched).dropna()
        if len(sr) > 10 and sr.std() > 0:
            oos_sharpe = sr.mean() / sr.std() * np.sqrt(365)
            oos_total = ((1 + sr).prod() - 1) * 100
            oos_ann = ((1 + oos_total/100) ** (365 / len(sr)) - 1) * 100
            aggregate_rows.append({
                "strategy": strat_name,
                "oos_days": len(sr),
                "oos_total_pct": round(oos_total, 1),
                "oos_ann_pct": round(oos_ann, 1),
                "oos_sharpe": round(oos_sharpe, 2),
            })

    if aggregate_rows:
        print(f"\n{'Strategy':<20} {'OOS days':>9} {'OOS total':>10} {'OOS ann':>9} {'OOS Sharpe':>11}")
        print("-" * 70)
        for row in sorted(aggregate_rows, key=lambda r: -r["oos_sharpe"]):
            print(f"{row['strategy']:<20} {row['oos_days']:>9} "
                  f"{row['oos_total_pct']:>9.1f}% {row['oos_ann_pct']:>8.1f}% "
                  f"{row['oos_sharpe']:>11.2f}")

    # Save
    out_path = config.PROJECT_DIR / "walk_forward_results.csv"
    summary_df.to_csv(out_path, index=False)
    if aggregate_rows:
        agg_path = config.PROJECT_DIR / "walk_forward_aggregate.csv"
        pd.DataFrame(aggregate_rows).to_csv(agg_path, index=False)
        print(f"\nPer-window results → {out_path}")
        print(f"Aggregate OOS   → {agg_path}")
    else:
        print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
