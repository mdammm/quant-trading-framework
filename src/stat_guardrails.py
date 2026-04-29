"""
Statistical Guardrails — prevents overfitting and ensures signal robustness.

Four tools:
  1. bootstrap_ci: confidence intervals on hit rates (replaces point estimates)
  2. benjamini_hochberg: multiple testing correction (we tested ~20 hypotheses)
  3. minimum_sample_size: how many observations needed to trust a signal
  4. validate_signals: run all guardrails and flag which signals survive

Usage:
    from stat_guardrails import validate_signals
    results = validate_signals(df)  # returns DataFrame with CI, corrected p-values
"""

import numpy as np
import pandas as pd
from math import sqrt, ceil, erf


def _norm_ppf(p):
    """Inverse normal CDF (percent point function). Pure Python, no scipy."""
    # Rational approximation (Abramowitz & Stegun 26.2.23)
    if p <= 0:
        return -10.0
    if p >= 1:
        return 10.0
    if p == 0.5:
        return 0.0
    if p > 0.5:
        return -_norm_ppf(1 - p)
    t = sqrt(-2 * np.log(p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return -(t - (c0 + c1 * t + c2 * t**2) / (1 + d1 * t + d2 * t**2 + d3 * t**3))


def _norm_cdf(x):
    """Normal CDF. Pure Python, no scipy."""
    return 0.5 * (1 + erf(x / sqrt(2)))


# ---------------------------------------------------------------------------
# Bootstrap Confidence Intervals
# ---------------------------------------------------------------------------

def bootstrap_ci(hits_misses, n_bootstrap=10000, alpha=0.05, seed=42):
    """
    Compute bootstrap confidence interval for hit rate.

    Args:
        hits_misses: array-like of 1s (hit) and 0s (miss)
        n_bootstrap: number of bootstrap samples
        alpha: significance level (0.05 = 95% CI)
        seed: random seed for reproducibility

    Returns:
        dict with point_estimate, ci_lower, ci_upper, ci_width
    """
    data = np.array(hits_misses, dtype=float)
    n = len(data)

    if n < 5:
        return {
            "point_estimate": np.mean(data) * 100 if n > 0 else 0,
            "ci_lower": 0,
            "ci_upper": 100,
            "ci_width": 100,
            "n": n,
            "sufficient": False,
        }

    rng = np.random.default_rng(seed)
    boot_means = np.array([
        rng.choice(data, size=n, replace=True).mean()
        for _ in range(n_bootstrap)
    ])

    lower = np.percentile(boot_means, alpha / 2 * 100) * 100
    upper = np.percentile(boot_means, (1 - alpha / 2) * 100) * 100
    point = np.mean(data) * 100

    return {
        "point_estimate": round(point, 1),
        "ci_lower": round(lower, 1),
        "ci_upper": round(upper, 1),
        "ci_width": round(upper - lower, 1),
        "n": n,
        "sufficient": lower > 50,  # CI lower bound above coin flip
    }


# ---------------------------------------------------------------------------
# Multiple Testing Correction
# ---------------------------------------------------------------------------

def benjamini_hochberg(p_values, alpha=0.05):
    """
    Benjamini-Hochberg procedure for controlling False Discovery Rate.

    Less conservative than Bonferroni. Better suited for signal discovery
    where we want to find real signals, not avoid all false positives.

    Args:
        p_values: dict of {signal_name: p_value}
        alpha: target FDR (0.05 = accept 5% false discovery rate)

    Returns:
        dict of {signal_name: {"p_value": float, "p_adjusted": float,
                                "survives": bool, "rank": int}}
    """
    names = list(p_values.keys())
    pvals = np.array([p_values[n] for n in names])
    m = len(pvals)

    # Sort by p-value
    sorted_idx = np.argsort(pvals)
    sorted_pvals = pvals[sorted_idx]
    sorted_names = [names[i] for i in sorted_idx]

    # BH adjustment
    adjusted = np.zeros(m)
    for i in range(m):
        rank = i + 1
        adjusted[i] = sorted_pvals[i] * m / rank

    # Enforce monotonicity (adjusted p-values should be non-decreasing from right)
    for i in range(m - 2, -1, -1):
        adjusted[i] = min(adjusted[i], adjusted[i + 1])
    adjusted = np.clip(adjusted, 0, 1)

    # Build results
    results = {}
    for i, name in enumerate(sorted_names):
        results[name] = {
            "p_value": round(float(sorted_pvals[i]), 6),
            "p_adjusted": round(float(adjusted[i]), 6),
            "survives": bool(adjusted[i] <= alpha),
            "rank": i + 1,
        }

    return results


def bonferroni(p_values, alpha=0.05):
    """
    Bonferroni correction — most conservative. Divides alpha by number of tests.

    Args:
        p_values: dict of {signal_name: p_value}
        alpha: family-wise error rate

    Returns:
        dict of {signal_name: {"p_value": float, "threshold": float, "survives": bool}}
    """
    m = len(p_values)
    threshold = alpha / m

    return {
        name: {
            "p_value": round(pval, 6),
            "threshold": round(threshold, 6),
            "survives": pval <= threshold,
        }
        for name, pval in p_values.items()
    }


# ---------------------------------------------------------------------------
# Minimum Sample Size
# ---------------------------------------------------------------------------

def minimum_sample_size(target_hit_rate_pct, power=0.80, alpha=0.05, null_rate=50.0):
    """
    Compute minimum n needed to detect a hit rate with statistical power.

    Uses normal approximation to the binomial test.

    Args:
        target_hit_rate_pct: expected true hit rate (e.g., 65%)
        power: statistical power (0.80 = 80% chance of detecting real effect)
        alpha: significance level
        null_rate: null hypothesis hit rate (50% = coin flip)

    Returns:
        dict with min_n and explanation
    """
    p1 = target_hit_rate_pct / 100
    p0 = null_rate / 100

    if p1 <= p0:
        return {"min_n": float("inf"), "reason": "Target rate must exceed null rate"}

    z_alpha = _norm_ppf(1 - alpha / 2)
    z_beta = _norm_ppf(power)

    # Sample size formula for one-proportion z-test
    n = ((z_alpha * sqrt(p0 * (1 - p0)) + z_beta * sqrt(p1 * (1 - p1))) / (p1 - p0)) ** 2

    return {
        "min_n": ceil(n),
        "target_hit_rate": target_hit_rate_pct,
        "power": power,
        "alpha": alpha,
        "explanation": f"Need n≥{ceil(n)} to detect {target_hit_rate_pct}% hit rate "
                       f"with {power*100:.0f}% power at p<{alpha}",
    }


# ---------------------------------------------------------------------------
# Signal Validation (run all guardrails)
# ---------------------------------------------------------------------------

def validate_signals(df, forward_window=10, execution_lag=1):
    """
    Run all statistical guardrails on signal hit rates.

    Returns DataFrame with:
      signal, n, hit_rate, ci_lower, ci_upper, p_value, p_adjusted,
      survives_bh, survives_bonf, min_n_needed, has_enough_data
    """
    from signals import SIGNAL_REGISTRY, ROBUST_SIGNALS, CONTRARIAN_SIGNALS_HEDGE

    # Short signals predict BTC DOWN (hit = fwd < 0)
    short_signals = {"vol_compression", "volume_shock_down",
                     "funding_extreme", "extreme_greed", "iv_crush",
                     "short_liq_cascade"}

    fwd_return = (
        df["btc_return"]
        .shift(-execution_lag)
        .rolling(forward_window).sum()
        .shift(-(forward_window - 1))
    )

    results = []
    p_values = {}

    for name in SIGNAL_REGISTRY:
        col = f"sig_{name}"
        if col not in df.columns:
            continue

        mask = df[col].fillna(False)
        fwd = fwd_return[mask].dropna()

        if len(fwd) < 5:
            continue

        # Hit rate and t-test — direction-aware for short signals
        if name in short_signals:
            hits = (fwd < 0).astype(int)
        else:
            hits = (fwd > 0).astype(int)
        n = len(hits)
        hit_rate = hits.mean() * 100
        se = hits.std() / sqrt(n) if n > 1 else 1
        t_stat = (hits.mean() - 0.5) / se if se > 0 else 0
        # One-sided p-value from t-stat (normal approx for large n)
        p_value = 1 - _norm_cdf(t_stat) if t_stat > 0 else _norm_cdf(t_stat)

        # Bootstrap CI
        ci = bootstrap_ci(hits.values)

        # Minimum sample size for this hit rate
        min_n = minimum_sample_size(hit_rate if hit_rate > 50 else 51)

        p_values[name] = p_value

        results.append({
            "signal": name,
            "n": n,
            "hit_rate": round(hit_rate, 1),
            "ci_lower": ci["ci_lower"],
            "ci_upper": ci["ci_upper"],
            "ci_width": ci["ci_width"],
            "ci_above_50": ci["sufficient"],
            "t_stat": round(t_stat, 2),
            "p_value": round(p_value, 6),
            "min_n_needed": min_n["min_n"],
            "has_enough_data": n >= min_n["min_n"],
            "is_robust": name in ROBUST_SIGNALS,
        })

    # Apply multiple testing corrections
    if p_values:
        bh = benjamini_hochberg(p_values)
        bonf = bonferroni(p_values)

        for r in results:
            name = r["signal"]
            if name in bh:
                r["p_adjusted_bh"] = bh[name]["p_adjusted"]
                r["survives_bh"] = bh[name]["survives"]
            if name in bonf:
                r["survives_bonf"] = bonf[name]["survives"]

    result_df = pd.DataFrame(results).sort_values("hit_rate", ascending=False)
    return result_df


def print_validation_report(df):
    """Pretty-print the validation results."""
    results = validate_signals(df)

    print("\n" + "=" * 90)
    print("  STATISTICAL VALIDATION REPORT")
    print("=" * 90)

    print(f"\n  Tested {len(results)} signals. Multiple testing correction applied (Benjamini-Hochberg).")
    print(f"  Minimum sample sizes computed at 80% power, p<0.05.\n")

    print(f"  {'Signal':<25} {'n':>5} {'Hit%':>6} {'95% CI':>14} {'p-adj':>8} {'BH':>5} {'Bonf':>6} {'MinN':>6} {'Enough':>7}")
    print(f"  {'-'*82}")

    for _, r in results.iterrows():
        ci_str = f"[{r['ci_lower']:.0f}-{r['ci_upper']:.0f}%]"
        bh = "✓" if r.get("survives_bh") else "✗"
        bonf = "✓" if r.get("survives_bonf") else "✗"
        enough = "✓" if r["has_enough_data"] else f"need {r['min_n_needed']}"
        robust = " ← ROBUST" if r["is_robust"] else ""
        above50 = "✓" if r["ci_above_50"] else ""

        print(f"  {r['signal']:<25} {r['n']:>5} {r['hit_rate']:>5.1f}% {ci_str:>14} "
              f"{r.get('p_adjusted_bh', 1):>8.4f} {bh:>5} {bonf:>6} {r['min_n_needed']:>6} {enough:>7}{robust}")

    # Summary
    survives_bh = results.get("survives_bh", pd.Series()).sum() if "survives_bh" in results.columns else 0
    survives_bonf = results.get("survives_bonf", pd.Series()).sum() if "survives_bonf" in results.columns else 0
    ci_above = results["ci_above_50"].sum()
    enough_data = results["has_enough_data"].sum()

    print(f"\n  Summary:")
    print(f"    Signals with CI lower bound > 50%: {ci_above}/{len(results)}")
    print(f"    Survive Benjamini-Hochberg (FDR 5%): {survives_bh}/{len(results)}")
    print(f"    Survive Bonferroni (FWER 5%): {survives_bonf}/{len(results)}")
    print(f"    Have sufficient sample size: {enough_data}/{len(results)}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from data_loader import load_cached_or_build
    from signals import compute_all_signals

    df = load_cached_or_build(use_cache=True)
    df = compute_all_signals(df)
    print_validation_report(df)
