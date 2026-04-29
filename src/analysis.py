"""
Analysis — performance metrics, signal hit rates, and visualization.
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path

import config


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------

def compute_metrics(result):
    """
    Compute standard backtest metrics from a run_backtest() result dict.

    Returns dict with:
        total_return, annualized_return, sharpe_ratio, max_drawdown,
        max_dd_duration_days, win_rate, avg_return_per_trade,
        profit_factor, total_trades, avg_hold_days, calmar_ratio
    """
    returns = result["returns"]
    equity = result["equity_curve"]
    trades = result["trades"]

    # Total and annualized return
    # bars_per_year accounts for bar interval (365 for daily, 2190 for 4H)
    bars_per_day = config.BARS_PER_DAY.get(config.BAR_INTERVAL, 1)
    bars_per_year = 365 * bars_per_day

    total_return = (equity.iloc[-1] / equity.iloc[0] - 1) * 100
    n_bars = len(returns)
    ann_factor = bars_per_year / max(n_bars, 1)
    annualized_return = ((1 + total_return / 100) ** ann_factor - 1) * 100

    # Sharpe ratio (annualized, 0% risk-free)
    bar_mean = returns.mean()
    bar_std = returns.std()
    sharpe = (bar_mean / bar_std * np.sqrt(bars_per_year)) if bar_std > 0 else 0

    # Max drawdown
    peak = equity.cummax()
    drawdown = (equity - peak) / peak
    max_dd = drawdown.min() * 100

    # Max drawdown duration
    is_dd = drawdown < 0
    dd_groups = (~is_dd).cumsum()
    if is_dd.any():
        dd_durations = is_dd.groupby(dd_groups).sum()
        max_dd_duration = dd_durations.max()
    else:
        max_dd_duration = 0

    # Trade-level metrics
    total_trades = len(trades)
    if total_trades > 0:
        wins = trades[trades["net_return"] > 0]
        losses = trades[trades["net_return"] <= 0]
        win_rate = len(wins) / total_trades * 100
        avg_return = trades["net_return"].mean() * 100
        gross_profit = wins["net_return"].sum() if len(wins) > 0 else 0
        gross_loss = abs(losses["net_return"].sum()) if len(losses) > 0 else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_hold = trades["hold_days"].mean()
    else:
        win_rate = 0
        avg_return = 0
        profit_factor = 0
        avg_hold = 0

    # Calmar ratio
    calmar = annualized_return / abs(max_dd) if max_dd != 0 else 0

    return {
        "total_return_pct": round(total_return, 2),
        "annualized_return_pct": round(annualized_return, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "max_dd_duration_days": int(max_dd_duration),
        "win_rate_pct": round(win_rate, 1),
        "avg_return_per_trade_pct": round(avg_return, 3),
        "profit_factor": round(profit_factor, 2),
        "total_trades": total_trades,
        "avg_hold_days": round(avg_hold, 1),
        "calmar_ratio": round(calmar, 2),
    }


# ---------------------------------------------------------------------------
# Diligence #4 + #7: trade-distribution + drawdown-distribution stats
# Added 2026-04-20 per DILIGENCE_AUDIT.md. First question any allocator asks
# is "is P&L concentrated in a few trades?" and "how long do you sit underwater?"
# These functions answer both with audit-grade numbers.
# ---------------------------------------------------------------------------

def compute_trade_distribution(result: dict) -> dict:
    """
    Trade-level return distribution — percentiles, top contributors, skew.

    Answers: "is the backtest a 70% WR of small gains, or 3 lucky trades
    dragging the average?" Diligence flags if top-10 trades are >60% of P&L.

    Returns:
        total_trades, return percentiles (p10/p25/p50/p75/p90),
        largest_win, largest_loss, skewness, kurtosis,
        top10_pct_of_total, top10_trades, bottom10_trades
    """
    import pandas as pd
    trades = result.get("trades")
    if trades is None or len(trades) == 0:
        return {"total_trades": 0, "error": "no trades"}

    rets = trades["net_return"] * 100  # pct
    total = rets.sum()

    # Percentiles
    pct = {f"p{p}": round(rets.quantile(p / 100), 3) for p in (10, 25, 50, 75, 90)}

    # Extremes
    largest_win = float(rets.max())
    largest_loss = float(rets.min())
    win_loss_ratio = abs(largest_win / largest_loss) if largest_loss != 0 else float("inf")

    # Concentration: how much of total return comes from top 10?
    sorted_rets = rets.abs().sort_values(ascending=False)
    top10_count = min(10, len(rets))
    top10_contribution = sorted_rets.head(top10_count).sum()
    total_abs = rets.abs().sum()
    top10_pct_of_abs = (top10_contribution / total_abs * 100) if total_abs > 0 else 0

    # Sorted net (signed) for top/bottom trades list
    sorted_signed = trades.assign(net_pct=rets).sort_values("net_pct", ascending=False)
    top10 = sorted_signed.head(10)[["entry_date", "exit_date", "direction", "net_pct"]]
    bottom10 = sorted_signed.tail(10)[["entry_date", "exit_date", "direction", "net_pct"]]

    # Distribution shape
    skew = float(rets.skew()) if len(rets) > 2 else 0.0
    kurt = float(rets.kurtosis()) if len(rets) > 3 else 0.0

    return {
        "total_trades": len(rets),
        "avg_return_pct": round(float(rets.mean()), 3),
        "median_return_pct": round(float(rets.median()), 3),
        "return_std_pct": round(float(rets.std()), 3),
        "percentiles": pct,
        "largest_win_pct": round(largest_win, 2),
        "largest_loss_pct": round(largest_loss, 2),
        "win_loss_magnitude_ratio": round(win_loss_ratio, 2),
        "skewness": round(skew, 2),
        "kurtosis": round(kurt, 2),
        "top10_pct_of_abs_return": round(top10_pct_of_abs, 1),
        "concentration_flag": top10_pct_of_abs > 60,
        "top_trades": top10.to_dict("records"),
        "bottom_trades": bottom10.to_dict("records"),
    }


def compute_drawdown_distribution(result: dict) -> dict:
    """
    Drawdown depth + duration distribution. Previously analysis only captured
    max_dd and its duration — a single point. Allocator questions:
      - How many times did we draw down >5/10/15%?
      - Median recovery time?
      - What % of time were we underwater?

    Returns:
        max_dd, count by threshold, median/p75/p95 duration,
        time_underwater_pct, dd_events (list with depth + duration)
    """
    import pandas as pd
    equity = result.get("equity_curve")
    if equity is None or len(equity) == 0:
        return {"error": "no equity curve"}

    peak = equity.cummax()
    drawdown = (equity - peak) / peak  # negative values

    # Separate distinct DD events: an event starts when we drop below peak
    # and ends when we hit a new peak.
    below = drawdown < -0.001  # >0.1% to ignore tiny noise
    event_id = (below & ~below.shift(1, fill_value=False)).cumsum()

    events = []
    for eid, grp in drawdown.groupby(event_id):
        if eid == 0 or not below.loc[grp.index].any():
            continue
        depth = float(grp.min())
        duration = int((grp < 0).sum())
        events.append({
            "start_date": str(grp.index[0]) if hasattr(grp.index[0], "date") else str(grp.index[0]),
            "depth_pct": round(depth * 100, 2),
            "duration_days": duration,
        })

    # Count by depth threshold
    count_gt5 = sum(1 for e in events if abs(e["depth_pct"]) >= 5)
    count_gt10 = sum(1 for e in events if abs(e["depth_pct"]) >= 10)
    count_gt15 = sum(1 for e in events if abs(e["depth_pct"]) >= 15)
    count_gt25 = sum(1 for e in events if abs(e["depth_pct"]) >= 25)

    # Duration stats (only on events deep enough to matter)
    sig_events = [e for e in events if abs(e["depth_pct"]) >= 5]
    if sig_events:
        durations = [e["duration_days"] for e in sig_events]
        dur_series = pd.Series(durations)
        median_dur = int(dur_series.median())
        p75_dur = int(dur_series.quantile(0.75))
        p95_dur = int(dur_series.quantile(0.95))
    else:
        median_dur = p75_dur = p95_dur = 0

    # Time underwater (any DD > 0.1%)
    time_underwater_pct = float(below.sum() / len(below) * 100) if len(below) else 0

    # Sort events by depth — deepest first
    events_sorted = sorted(events, key=lambda e: e["depth_pct"])[:10]

    return {
        "max_dd_pct": round(float(drawdown.min() * 100), 2),
        "n_events_total": len(events),
        "count_dd_gt5pct": count_gt5,
        "count_dd_gt10pct": count_gt10,
        "count_dd_gt15pct": count_gt15,
        "count_dd_gt25pct": count_gt25,
        "median_duration_days_gt5pct": median_dur,
        "p75_duration_days_gt5pct": p75_dur,
        "p95_duration_days_gt5pct": p95_dur,
        "time_underwater_pct": round(time_underwater_pct, 1),
        "top10_deepest_events": events_sorted,
    }


def compute_beta_correlation(result: dict, btc_returns=None) -> dict:
    """
    Diligence #5: linear regression of strategy daily returns on BTC daily
    returns. Answers "how much of the return is market beta vs alpha?"

    Returns:
        beta, alpha_daily_bps, alpha_ann_pct, r_squared, correlation,
        rolling_60d_corr (list)
    """
    import numpy as np
    import pandas as pd
    ret = result.get("returns")
    if ret is None or len(ret) == 0:
        return {"error": "no returns"}

    if btc_returns is None:
        # Try to pull from master dataset if result doesn't carry it
        return {"error": "btc_returns not provided"}

    # Align
    strat = pd.Series(ret.values if hasattr(ret, "values") else ret).reset_index(drop=True)
    btc = pd.Series(btc_returns.values if hasattr(btc_returns, "values") else btc_returns).reset_index(drop=True)
    n = min(len(strat), len(btc))
    strat = strat.iloc[:n]
    btc = btc.iloc[:n]

    # Regression: strat = alpha + beta * btc + epsilon
    valid = strat.notna() & btc.notna()
    s = strat[valid]
    b = btc[valid]
    if len(s) < 20:
        return {"error": f"insufficient aligned data (n={len(s)})"}

    beta = float(np.cov(s, b)[0, 1] / np.var(b)) if np.var(b) > 0 else 0.0
    alpha_daily = float(s.mean() - beta * b.mean())
    alpha_ann = ((1 + alpha_daily) ** 365 - 1) * 100

    residuals = s - (alpha_daily + beta * b)
    ss_res = float((residuals ** 2).sum())
    ss_tot = float(((s - s.mean()) ** 2).sum())
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    correlation = float(s.corr(b)) if not (s.std() == 0 or b.std() == 0) else 0.0

    # Rolling 60d correlation
    rolling_corr = s.rolling(60).corr(b).dropna().tolist()

    return {
        "beta": round(beta, 3),
        "alpha_daily_bps": round(alpha_daily * 10000, 2),
        "alpha_annualized_pct": round(alpha_ann, 2),
        "r_squared": round(r_squared, 3),
        "correlation": round(correlation, 3),
        "rolling_60d_corr_mean": round(float(np.mean(rolling_corr)), 3) if rolling_corr else 0,
        "rolling_60d_corr_std": round(float(np.std(rolling_corr)), 3) if rolling_corr else 0,
        "n_aligned_days": int(valid.sum()),
    }


# ---------------------------------------------------------------------------
# Signal hit rates
# ---------------------------------------------------------------------------

def compute_signal_hit_rates(df, forward_window=10, execution_lag=1):
    """
    For each signal, compute predictive power with proper lag.

    Timeline: signal fires at T → execution at T+lag → measure return from
    T+lag to T+lag+forward_window. This prevents lookahead bias.

    Returns per signal:
    - hit_rate: % of times BTC moves in expected direction
    - avg_fwd_return: mean forward return (with lag)
    - frequency: how often signal fires
    - sample_size: number of non-overlapping observations
    - t_stat: t-statistic vs zero-mean (statistical significance)
    - worst_trade: worst single forward return
    - best_trade: best single forward return
    - confidence: HIGH (n≥100, |t|>2), MEDIUM (n≥50, |t|>1.5), LOW otherwise
    """
    from signals import SIGNAL_REGISTRY

    # Forward return: skip execution_lag days, compound over forward_window
    # Uses geometric compounding: prod(1+r_i) - 1 for consistency with forward_tracker
    _shifted = df["btc_return"].shift(-execution_lag)
    fwd_return = (
        (1 + _shifted)
        .rolling(forward_window).apply(lambda x: x.prod() - 1, raw=True)
        .shift(-(forward_window - 1))  # align to signal date
    )

    results = []
    for name in SIGNAL_REGISTRY:
        col = f"sig_{name}"
        if col not in df.columns:
            continue

        active = df[col] == True
        n_active = active.sum()
        n_total = len(df)
        freq = n_active / n_total * 100 if n_total > 0 else 0

        if n_active == 0:
            results.append({
                "signal": name, "frequency_pct": round(freq, 1),
                "sample_size": 0, "hit_rate_pct": 0,
                "avg_fwd_return_pct": 0, "t_stat": 0,
                "worst_pct": 0, "best_pct": 0, "confidence": "NO_DATA",
            })
            continue

        fwd_when_active = fwd_return[active].dropna()
        n_obs = len(fwd_when_active)

        if n_obs == 0:
            results.append({
                "signal": name, "frequency_pct": round(freq, 1),
                "sample_size": 0, "hit_rate_pct": 0,
                "avg_fwd_return_pct": 0, "t_stat": 0,
                "worst_pct": 0, "best_pct": 0, "confidence": "NO_DATA",
            })
            continue

        # Hit rate: direction depends on signal type
        short_signals = {"vol_compression", "taker_dump", "volume_shock_down", "funding_extreme"}
        if name in short_signals:
            hits = (fwd_when_active < 0).sum()
        else:
            hits = (fwd_when_active > 0).sum()

        hit_rate = hits / n_obs * 100
        avg_fwd = fwd_when_active.mean() * 100
        std_fwd = fwd_when_active.std()
        t_stat = (fwd_when_active.mean() / (std_fwd / np.sqrt(n_obs))) if std_fwd > 0 and n_obs > 1 else 0

        # Confidence level
        if n_obs >= 100 and abs(t_stat) > 2.0:
            confidence = "HIGH"
        elif n_obs >= 50 and abs(t_stat) > 1.5:
            confidence = "MEDIUM"
        elif n_obs >= 30:
            confidence = "LOW"
        else:
            confidence = "INSUFFICIENT"

        results.append({
            "signal": name,
            "frequency_pct": round(freq, 1),
            "sample_size": n_obs,
            "hit_rate_pct": round(hit_rate, 1),
            "avg_fwd_return_pct": round(avg_fwd, 3),
            "t_stat": round(t_stat, 2),
            "worst_pct": round(fwd_when_active.min() * 100, 2),
            "best_pct": round(fwd_when_active.max() * 100, 2),
            "confidence": confidence,
        })

    return pd.DataFrame(results).sort_values("t_stat", ascending=False)


def compute_confluence_hit_rates(df, forward_window=10, execution_lag=1):
    """
    Compare hit rates when 2+ robust signals agree vs 1 alone.
    Answers: does confluence improve accuracy?
    """
    from signals import compute_confluence

    if "confluence_net" not in df.columns:
        df = compute_confluence(df, min_signals=2)

    fwd_return = (
        df["btc_return"]
        .shift(-execution_lag)
        .rolling(forward_window).sum()
        .shift(-(forward_window - 1))
    )

    results = []
    for min_n in [1, 2, 3]:
        # Short confluence
        short_mask = df["confluence_short_count"] >= min_n
        fwd_short = fwd_return[short_mask].dropna()
        if len(fwd_short) > 0:
            hit = (fwd_short < 0).mean() * 100
            results.append({
                "direction": "short",
                "min_signals": min_n,
                "sample_size": len(fwd_short),
                "hit_rate_pct": round(hit, 1),
                "avg_return_pct": round(fwd_short.mean() * 100, 3),
            })

        # Long confluence
        long_mask = df["confluence_long_count"] >= min_n
        fwd_long = fwd_return[long_mask].dropna()
        if len(fwd_long) > 0:
            hit = (fwd_long > 0).mean() * 100
            results.append({
                "direction": "long",
                "min_signals": min_n,
                "sample_size": len(fwd_long),
                "hit_rate_pct": round(hit, 1),
                "avg_return_pct": round(fwd_long.mean() * 100, 3),
            })

    return pd.DataFrame(results)


def compute_regime_analysis(df, forward_window=10, execution_lag=1):
    """
    Break signal performance by volatility regime.
    Tests: does the signal work differently in high-vol vs low-vol?
    """
    from signals import SIGNAL_REGISTRY

    # Classify vol regime
    vol_median = df["realized_vol_30d"].median()
    high_vol = df["realized_vol_30d"] > vol_median
    low_vol = ~high_vol

    fwd_return = (
        df["btc_return"]
        .shift(-execution_lag)
        .rolling(forward_window).sum()
        .shift(-(forward_window - 1))
    )

    results = []
    for name in SIGNAL_REGISTRY:
        col = f"sig_{name}"
        if col not in df.columns:
            continue

        active = df[col] == True

        for regime_name, regime_mask in [("high_vol", high_vol), ("low_vol", low_vol)]:
            regime_active = active & regime_mask
            fwd_regime = fwd_return[regime_active].dropna()
            n = len(fwd_regime)

            short_signals = {"vol_compression", "taker_dump", "volume_shock_down", "funding_extreme"}
            if n > 0:
                if name in short_signals:
                    hits = (fwd_regime < 0).sum()
                else:
                    hits = (fwd_regime > 0).sum()
                hit_rate = hits / n * 100
                avg_ret = fwd_regime.mean() * 100
            else:
                hit_rate = 0
                avg_ret = 0

            results.append({
                "signal": name,
                "regime": regime_name,
                "sample_size": n,
                "hit_rate_pct": round(hit_rate, 1),
                "avg_fwd_return_pct": round(avg_ret, 3),
            })

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(all_results):
    """
    Generate text summary table comparing all strategies.
    all_results: dict of {strategy_name: run_backtest() result}
    """
    rows = []
    for name, result in all_results.items():
        m = compute_metrics(result)
        rows.append({"Strategy": name, **m})

    df = pd.DataFrame(rows)

    # Format as text table
    lines = ["\n" + "=" * 100]
    lines.append("BACKTESTING RESULTS SUMMARY")
    lines.append("=" * 100)

    header = (
        f"{'Strategy':<20} {'Return%':>8} {'Ann.Ret%':>9} {'Sharpe':>7} "
        f"{'MaxDD%':>8} {'WinRate':>8} {'Trades':>7} {'AvgRet%':>8} {'PF':>6}"
    )
    lines.append(header)
    lines.append("-" * 100)

    for _, row in df.iterrows():
        line = (
            f"{row['Strategy']:<20} "
            f"{row['total_return_pct']:>8.1f} "
            f"{row['annualized_return_pct']:>9.1f} "
            f"{row['sharpe_ratio']:>7.2f} "
            f"{row['max_drawdown_pct']:>8.1f} "
            f"{row['win_rate_pct']:>7.1f}% "
            f"{row['total_trades']:>7} "
            f"{row['avg_return_per_trade_pct']:>8.3f} "
            f"{row['profit_factor']:>6.2f}"
        )
        lines.append(line)

    lines.append("=" * 100)

    # Identify best strategy (by Sharpe, excluding baselines)
    non_baseline = df[~df["Strategy"].isin(["buy_and_hold", "random_entry"])]
    if len(non_baseline) > 0:
        best = non_baseline.loc[non_baseline["sharpe_ratio"].idxmax()]
        lines.append(f"\nBest signal strategy (by Sharpe): {best['Strategy']} "
                      f"(Sharpe {best['sharpe_ratio']:.2f}, Return {best['total_return_pct']:.1f}%)")

    # Context: BTC over the period
    lines.append("")
    lines.append("Note: All strategies use T+1 execution lag (signal at close T → trade at open T+1)")
    lines.append("      Costs: {:.0f} bps per trade (fee + slippage)".format(
        config.TRADING_FEE_BPS + config.SLIPPAGE_BPS))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

def plot_equity_curves(all_results, df, save_path=None):
    """Plot equity curves for all strategies on one chart."""
    fig, ax = plt.subplots(figsize=(14, 7))

    for name, result in all_results.items():
        equity = result["equity_curve"]
        dates = df["trade_date"].iloc[:len(equity)]
        style = "--" if name in ("buy_and_hold", "random_entry") else "-"
        alpha = 0.5 if name in ("buy_and_hold", "random_entry") else 0.9
        ax.plot(dates, equity, style, label=name, alpha=alpha)

    ax.set_title("Strategy Equity Curves", fontsize=14)
    ax.set_xlabel("Date")
    ax.set_ylabel("Portfolio Value ($)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    fig.autofmt_xdate()

    save_path = save_path or config.PROJECT_DIR / "equity_curves.png"
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved equity curves to {save_path}")


def plot_signal_dashboard(df, save_path=None):
    """
    4-panel chart:
    1. BTC price with volatility regime shading
    2. Platform volume bars with shock markers
    3. Taker ratio time series
    4. Signal activation heatmap
    """
    from signals import SIGNAL_REGISTRY

    fig, axes = plt.subplots(4, 1, figsize=(14, 16), sharex=True)
    dates = df["trade_date"]

    # Panel 1: BTC price + vol regime shading
    ax1 = axes[0]
    ax1.plot(dates, df["btc_close"], "k-", linewidth=1)
    if "sig_vol_expansion" in df.columns:
        expansion = df["sig_vol_expansion"]
        ax1.fill_between(dates, df["btc_close"].min(), df["btc_close"].max(),
                         where=expansion, alpha=0.15, color="red", label="Vol Expansion")
    if "sig_vol_compression" in df.columns:
        compression = df["sig_vol_compression"]
        ax1.fill_between(dates, df["btc_close"].min(), df["btc_close"].max(),
                         where=compression, alpha=0.15, color="blue", label="Vol Compression")
    ax1.set_ylabel("BTC Price ($)")
    ax1.set_title("BTC Price with Volatility Regime")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)

    # Panel 2: Platform volume
    ax2 = axes[1]
    ax2.bar(dates, df["exchange_volume"], width=1, alpha=0.6, color="steelblue")
    if "sig_volume_shock_up" in df.columns:
        shocks = df[df["sig_volume_shock_up"]]
        ax2.scatter(shocks["trade_date"], shocks["exchange_volume"],
                    color="red", marker="^", s=30, zorder=5, label="Volume Shock")
    ax2.set_ylabel("Platform Volume ($)")
    ax2.set_title("Daily Platform Volume")
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(True, alpha=0.3)

    # Panel 3: Taker ratio
    ax3 = axes[2]
    ax3.plot(dates, df["taker_ratio"], "g-", linewidth=1)
    ax3.axhline(y=0.55, color="red", linestyle="--", alpha=0.5, label="Spike threshold")
    ax3.axhline(y=0.45, color="blue", linestyle="--", alpha=0.5, label="Dump threshold")
    ax3.set_ylabel("Taker Ratio")
    ax3.set_title("Taker Ratio (Buy Volume / Total)")
    ax3.legend(loc="upper left", fontsize=8)
    ax3.grid(True, alpha=0.3)

    # Panel 4: Signal heatmap
    ax4 = axes[3]
    sig_cols = [f"sig_{name}" for name in SIGNAL_REGISTRY if f"sig_{name}" in df.columns]
    if sig_cols:
        heatmap_data = df[sig_cols].astype(float).T
        heatmap_data.index = [c.replace("sig_", "") for c in sig_cols]
        ax4.imshow(heatmap_data.values, aspect="auto", cmap="YlOrRd",
                   interpolation="nearest", extent=[0, len(dates), 0, len(sig_cols)])
        ax4.set_yticks(np.arange(len(sig_cols)) + 0.5)
        ax4.set_yticklabels(heatmap_data.index, fontsize=7)
        ax4.set_title("Signal Activation Heatmap")

    ax4.set_xlabel("Date")
    fig.tight_layout()

    save_path = save_path or config.PROJECT_DIR / "signal_dashboard.png"
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved signal dashboard to {save_path}")


def plot_drawdown(all_results, df, save_path=None):
    """Plot drawdown curves for each strategy."""
    fig, ax = plt.subplots(figsize=(14, 5))

    for name, result in all_results.items():
        equity = result["equity_curve"]
        peak = equity.cummax()
        dd = (equity - peak) / peak * 100
        dates = df["trade_date"].iloc[:len(dd)]
        ax.plot(dates, dd, label=name, alpha=0.7)

    ax.set_title("Strategy Drawdowns", fontsize=14)
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown (%)")
    ax.legend(loc="lower left", fontsize=8)
    ax.grid(True, alpha=0.3)

    save_path = save_path or config.PROJECT_DIR / "drawdowns.png"
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved drawdown chart to {save_path}")
