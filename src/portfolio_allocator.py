"""
Portfolio Allocator — computes dynamic per-pair capital weights for
BTC/ETH/SOL based on recent performance metrics.

Pure weight functions with no backtest logic. Each method takes a window
of per-pair daily returns and returns {pair: weight} summing to 1.0.
A shared post-processor enforces floor/cap/correlation constraints.
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Allocation methods
# ---------------------------------------------------------------------------

def _equal_weights(pairs):
    """Baseline: 1/N."""
    n = len(pairs)
    return {p: 1.0 / n for p in pairs}


def _trailing_sharpe_weights(returns_window, pairs):
    """Weight proportional to max(rolling Sharpe, 0). Annualized."""
    raw = {}
    for p in pairs:
        r = returns_window[p].dropna()
        if len(r) < 20 or r.std() == 0:
            raw[p] = 0.0
        else:
            raw[p] = max(r.mean() / r.std() * np.sqrt(252), 0.0)

    total = sum(raw.values())
    if total == 0:
        return _equal_weights(pairs)
    return {p: raw[p] / total for p in pairs}


def _inverse_vol_weights(returns_window, pairs):
    """Weight proportional to 1 / rolling std."""
    raw = {}
    for p in pairs:
        r = returns_window[p].dropna()
        if len(r) < 20 or r.std() == 0:
            raw[p] = 0.0
        else:
            raw[p] = 1.0 / r.std()

    total = sum(raw.values())
    if total == 0:
        return _equal_weights(pairs)
    return {p: raw[p] / total for p in pairs}


def _risk_parity_weights(returns_window, pairs, max_iter=50, tol=1e-6):
    """
    Equal risk contribution via cyclical coordinate descent on the
    rolling covariance matrix. Each pair contributes equal marginal risk.
    """
    valid_cols = [p for p in pairs if returns_window[p].dropna().shape[0] >= 20]
    if len(valid_cols) < len(pairs):
        return _equal_weights(pairs)

    cov = returns_window[valid_cols].dropna().cov().values
    n = len(valid_cols)

    # Check for singular covariance
    if np.linalg.det(cov) < 1e-12:
        return _equal_weights(pairs)

    w = np.ones(n) / n
    for _ in range(max_iter):
        w_prev = w.copy()
        port_vol = np.sqrt(w @ cov @ w)
        if port_vol == 0:
            return _equal_weights(pairs)
        mrc = (cov @ w) / port_vol  # marginal risk contribution
        target_rc = port_vol / n
        for j in range(n):
            if mrc[j] > 0:
                w[j] = target_rc / mrc[j]
        w = w / w.sum()
        if np.max(np.abs(w - w_prev)) < tol:
            break

    return {pairs[i]: float(w[i]) for i in range(n)}


def _momentum_weights(returns_window, pairs, momentum_days=20):
    """Weight proportional to max(trailing 20d return, 0)."""
    raw = {}
    for p in pairs:
        r = returns_window[p].dropna()
        if len(r) < momentum_days:
            raw[p] = 0.0
        else:
            cum = (1 + r.tail(momentum_days)).prod() - 1
            raw[p] = max(cum, 0.0)

    total = sum(raw.values())
    if total == 0:
        return _equal_weights(pairs)
    return {p: raw[p] / total for p in pairs}


def _hybrid_weights(returns_window, pairs):
    """Trailing-Sharpe weighted, then floor/cap applied by post-processor."""
    return _trailing_sharpe_weights(returns_window, pairs)


# ---------------------------------------------------------------------------
# Constraint post-processor
# ---------------------------------------------------------------------------

def apply_constraints(weights, pairs, floor_pct=0.12, cap_pct=0.55,
                      corr_cap_pairs=("BTC", "ETH"), corr_cap=0.75):
    """
    Enforce allocation constraints on raw weights:
      1. Floor each pair at floor_pct
      2. Cap each pair at cap_pct
      3. BTC+ETH <= corr_cap (push excess to SOL)
      4. Renormalize to sum to 1.0
    Iterates up to 5 passes until all constraints are satisfied simultaneously.

    ADAPTIVE FLOOR (added 2026-04-28): if floor_pct * len(pairs) > 0.50, the
    floor mathematically forces equal weight after renormalize (raw signal is
    discarded — bug shipped silently when the universe expanded from 3 → 15
    pairs on 2026-04-19; floor=0.12 × 15 = 1.80 → uniform 0.0667). To prevent
    this, clamp floor_pct to min(floor_pct, 0.50/N) so the method always has
    at least 50% of the book to differentiate.
    """
    n = len(pairs)
    if n > 0:
        max_safe_floor = 0.50 / n
        if floor_pct > max_safe_floor:
            print(f"[allocator] floor_pct={floor_pct} clamped to {max_safe_floor:.4f} "
                  f"({n} pairs × floor must leave ≥50% for differentiation)")
            floor_pct = max_safe_floor

    w = {p: weights.get(p, 0.0) for p in pairs}

    for _pass in range(5):
        # Floor
        for p in pairs:
            w[p] = max(w[p], floor_pct)

        # Renormalize after floor
        total = sum(w.values())
        if total > 0:
            w = {p: w[p] / total for p in pairs}

        # Cap — redistribute excess to uncapped pairs
        capped = {}
        excess = 0.0
        for p in pairs:
            if w[p] > cap_pct:
                excess += w[p] - cap_pct
                capped[p] = cap_pct
            else:
                capped[p] = w[p]

        if excess > 0:
            uncapped = [p for p in pairs if capped[p] < cap_pct]
            uncapped_total = sum(capped[p] for p in uncapped)
            if uncapped_total > 0:
                for p in uncapped:
                    capped[p] += excess * (capped[p] / uncapped_total)
                    capped[p] = min(capped[p], cap_pct)
            w = capped

        # Correlation cap: BTC + ETH <= corr_cap
        corr_pair_keys = [p for p in corr_cap_pairs if p in w]
        other_keys = [p for p in pairs if p not in corr_cap_pairs]
        corr_sum = sum(w[p] for p in corr_pair_keys)
        if corr_sum > corr_cap and corr_sum > 0:
            scale = corr_cap / corr_sum
            excess = corr_sum - corr_cap
            for p in corr_pair_keys:
                w[p] *= scale
            for p in other_keys:
                room = cap_pct - w[p]
                add = min(excess, room)
                w[p] += add
                excess -= add

        # Final renormalize
        total = sum(w.values())
        if total > 0:
            w = {p: w[p] / total for p in pairs}

        # Check convergence: all constraints met?
        all_ok = True
        for p in pairs:
            if w[p] < floor_pct - 1e-9 or w[p] > cap_pct + 1e-9:
                all_ok = False
                break
        corr_sum = sum(w[p] for p in corr_pair_keys)
        if corr_sum > corr_cap + 1e-6:
            all_ok = False
        if all_ok:
            break

    return w


# ---------------------------------------------------------------------------
# Rebalance schedule
# ---------------------------------------------------------------------------

def rebalance_schedule(dates, cadence, positions_df=None):
    """
    Return set of dates on which to rebalance.
      cadence="weekly" → every Monday in the date index
      cadence="signal_entry" → dates where any pair's position changes
    """
    dates_idx = pd.DatetimeIndex(dates)

    if cadence == "weekly":
        return set(dates_idx[dates_idx.weekday == 0])  # Monday = 0

    elif cadence == "signal_entry":
        if positions_df is None:
            raise ValueError("positions_df required for signal_entry cadence")
        changes = positions_df.diff().abs().sum(axis=1) > 0
        return set(dates_idx[changes])

    else:
        raise ValueError(f"Unknown cadence: {cadence}")


# ---------------------------------------------------------------------------
# Main allocator class
# ---------------------------------------------------------------------------

METHOD_MAP = {
    "equal": lambda rw, pairs: _equal_weights(pairs),
    "trailing_sharpe": _trailing_sharpe_weights,
    "inverse_vol": _inverse_vol_weights,
    "risk_parity": _risk_parity_weights,
    "momentum": _momentum_weights,
    "hybrid": _hybrid_weights,
}


class PortfolioAllocator:
    """
    Computes per-pair allocation weights given recent returns.

    Parameters:
        method: one of "equal", "trailing_sharpe", "inverse_vol",
                "risk_parity", "momentum", "hybrid"
        lookback: days of history to use for weight computation
        floor_pct: minimum weight per pair (default 0.12)
        cap_pct: maximum weight per pair (default 0.55)
        corr_cap: maximum combined weight for BTC+ETH (default 0.75)
    """

    def __init__(self, method="equal", lookback=60, floor_pct=0.12,
                 cap_pct=0.55, corr_cap=0.75):
        if method not in METHOD_MAP:
            raise ValueError(f"Unknown method '{method}'. Choose from: {list(METHOD_MAP.keys())}")
        self.method = method
        self.lookback = lookback
        self.floor_pct = floor_pct
        self.cap_pct = cap_pct
        self.corr_cap = corr_cap

    def compute_weights(self, returns_df, as_of_idx, pairs):
        """
        Compute constrained weights using returns up to (and including) as_of_idx.

        Parameters:
            returns_df: DataFrame with columns = pair names, index = trade_date
            as_of_idx: integer iloc position in returns_df (inclusive end of window)
            pairs: list of pair names

        Returns:
            dict {pair: weight} summing to 1.0
        """
        start = max(0, as_of_idx - self.lookback + 1)
        window = returns_df.iloc[start:as_of_idx + 1]

        if len(window) < 20:
            raw = _equal_weights(pairs)
        else:
            fn = METHOD_MAP[self.method]
            raw = fn(window, pairs)

        return apply_constraints(
            raw, pairs,
            floor_pct=self.floor_pct,
            cap_pct=self.cap_pct,
            corr_cap=self.corr_cap,
        )
