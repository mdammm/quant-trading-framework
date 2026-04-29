"""
Cross-book aggregate exposure cap.

Problem: daily and 4H traders are independent — they each maintain their own
capital pool and position book. Without cross-book coordination, both can
be long BTC simultaneously, effectively doubling BTC exposure on the same
signal cycle. Seen on 4/22: BTC long in both D ($+87) and 4H ($+320) books.

Fix: before opening a new position, sum the notional exposure already open
on that pair across BOTH books, and reject / resize the new entry if the
combined exposure would exceed config.MAX_PAIR_EXPOSURE_MULTIPLIER × the
pair's allocated capital.

Default cap: 1.5× pair allocation (allows partial overlap but not 2×).
Configurable via config.MAX_PAIR_EXPOSURE_MULTIPLIER.

Usage from within a trader's execute_signal flow:

    from exposure_cap import get_effective_size
    size_multiplier = get_effective_size(pair, proposed_new_usd, current_price)
    # size_multiplier in [0, 1]; 0 = reject entry entirely, 1 = full size

Paper mode: exposure cap is ADVISORY — logged but not enforced. This keeps
paper generating forward data without the cap interfering. Live mode enforces.
"""
import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent

# Defaults — overridable via config
DEFAULT_MAX_PAIR_EXPOSURE_MULTIPLIER = 1.5


def _current_pair_notional(pair: str) -> float:
    """Sum |qty × entry_price| across open positions for this pair in both books."""
    total = 0.0
    for suffix in ["", "_4h"]:
        path = ROOT / f"tracker/paper_trades{suffix}_{pair.lower()}.json"
        if not path.exists():
            continue
        try:
            for t in json.loads(path.read_text()):
                if t.get("status") == "open":
                    qty = abs(t.get("quantity", 0) or 0)
                    entry = t.get("entry_price", 0) or 0
                    total += qty * entry
        except Exception:
            continue
    return total


def _pair_capital(pair: str) -> float:
    """Pair's allocated capital — sum of both books' starting pool."""
    try:
        import config
        allocation_weights = json.loads(
            (ROOT / "tracker/allocation_weights.json").read_text()
        )
        w = allocation_weights.get(pair, 1.0 / len(config.SUPPORTED_PAIRS))
        return config.INITIAL_CAPITAL * w
    except Exception:
        # Fallback: equal-weight
        import config
        return config.INITIAL_CAPITAL / len(config.SUPPORTED_PAIRS)


def get_effective_size(pair: str, proposed_usd: float, current_price: float) -> float:
    """
    Return size multiplier in [0.0, 1.0]. Caller multiplies their intended
    position size by this to respect the aggregate exposure cap.

    - 1.0 → full proposed size (existing exposure below cap)
    - 0.0 → reject entry entirely (already at or above cap)
    - 0.3 → reduce to 30% of proposed size (partial room left under cap)
    """
    import config

    cap_multiplier = getattr(config, "MAX_PAIR_EXPOSURE_MULTIPLIER",
                             DEFAULT_MAX_PAIR_EXPOSURE_MULTIPLIER)
    pair_cap = _pair_capital(pair)
    max_notional = pair_cap * cap_multiplier

    current = _current_pair_notional(pair)
    remaining = max_notional - current

    if remaining <= 0:
        return 0.0  # already at/over cap, reject
    if remaining >= proposed_usd:
        return 1.0  # full size fits

    return remaining / proposed_usd  # partial size


def get_pair_exposure_report() -> dict:
    """Audit report: current exposure vs cap across all pairs."""
    import config
    cap_mult = getattr(config, "MAX_PAIR_EXPOSURE_MULTIPLIER",
                       DEFAULT_MAX_PAIR_EXPOSURE_MULTIPLIER)
    report = {}
    for pair in config.SUPPORTED_PAIRS:
        cur = _current_pair_notional(pair)
        cap = _pair_capital(pair) * cap_mult
        report[pair] = {
            "current_notional_usd": round(cur, 2),
            "cap_notional_usd": round(cap, 2),
            "utilization_pct": round(cur / cap * 100, 1) if cap > 0 else 0,
            "headroom_usd": round(max(0, cap - cur), 2),
            "over_cap": cur > cap,
        }
    return report


if __name__ == "__main__":
    import json
    print(json.dumps(get_pair_exposure_report(), indent=2))
