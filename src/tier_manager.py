"""
Per-pair tier-based graduation system.

Replaces the binary 30-trade go-live gate with a continuous ramp: each pair
is independently promoted/demoted through tiers based on its own forward
performance. This lets us deploy real capital on BTC/LINK/NEAR when they
earn it without waiting for MANA/DOT to hit the same bar.

Tiers (ascending, live capital %):
  paper  (0%)   — new pairs, default; tracking only
  micro  (10%)  — 5+ closed trades, WR>=50%, P&L >= -0.5% of pair capital
  half   (50%)  — 10+ closed, WR>=55%, P&L positive, realized Sharpe>0.8
  full   (100%) — 20+ closed, WR>=55%, P&L positive, realized Sharpe>=0.5×backtest
  paused (0%)   — circuit breaker: any week with -3% P&L or WR drops to <=40%,
                  reverts to paper after 2-week cooldown

Evaluated weekly (tier_eval.py runs Sun 9am via launchd).
Persisted to tracker/pair_tiers.json.
"""
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np

# Portable: resolve relative to this file (tier_manager.py sits in repo root)
ROOT = Path(__file__).resolve().parent
TIER_FILE = ROOT / "tracker/pair_tiers.json"

TIER_MULTIPLIERS = {
    "paper":  0.0,
    "micro":  0.10,
    "half":   0.50,
    "full":   1.00,
    "paused": 0.0,
}

# Promotion gates — all must pass. Per-pair evaluation.
PROMOTION_GATES = {
    "paper_to_micro": {
        "min_closed_trades": 5,
        "min_win_rate": 0.50,
        "max_pnl_drawdown_pct": -0.5,  # as % of pair capital
    },
    "micro_to_half": {
        "min_closed_trades": 10,
        "min_win_rate": 0.55,
        "min_pnl_positive": True,
        "min_sharpe": 0.8,
    },
    "half_to_full": {
        "min_closed_trades": 20,
        "min_win_rate": 0.55,
        "min_pnl_positive": True,
        "min_sharpe_pct_of_backtest": 0.5,
    },
}

# Demotion (circuit breaker) — any trigger = paused
DEMOTION_TRIGGERS = {
    "weekly_loss_pct": -3.0,   # -3% of pair capital in a 7-day window
    "win_rate_floor": 0.40,    # WR drops below 40%
}

PAUSE_COOLDOWN_DAYS = 14  # paused pairs revert to paper after this


def _default_state() -> Dict:
    """Initial tier state: every pair starts at paper."""
    import config
    return {
        "last_eval": None,
        "pairs": {
            pair: {
                "tier": "paper",
                "last_change": datetime.now().isoformat(),
                "reason": "initial (new pair or migration)",
                "history": [],
            }
            for pair in config.SUPPORTED_PAIRS
        },
    }


def load_tiers() -> Dict:
    """Load current tier state; initialize if missing."""
    if not TIER_FILE.exists():
        state = _default_state()
        save_tiers(state)
        return state
    return json.loads(TIER_FILE.read_text())


def save_tiers(state: Dict) -> None:
    from safe_io import atomic_write_json
    atomic_write_json(TIER_FILE, state)


def get_tier(pair: str) -> str:
    """Return current tier for a pair. Default 'paper' if unknown."""
    state = load_tiers()
    return state["pairs"].get(pair, {}).get("tier", "paper")


def get_tier_multiplier(pair: str) -> float:
    """Return the capital multiplier (0.0 to 1.0) for a pair's current tier."""
    return TIER_MULTIPLIERS.get(get_tier(pair), 0.0)


def _pair_stats(pair: str) -> Dict:
    """
    Compute performance stats for a pair across daily + 4H paper trades.
    Returns dict: closed_trades, win_rate, pnl_pct, realized_sharpe,
                  last_7d_pnl_pct, tier, backtest_sharpe.
    """
    paths = [
        ROOT / f"tracker/paper_trades_{pair.lower()}.json",
        ROOT / f"tracker/paper_trades_4h_{pair.lower()}.json",
    ]
    closed = []
    for p in paths:
        if not p.exists():
            continue
        try:
            for t in json.loads(p.read_text()):
                if t.get("status") == "closed" and t.get("pnl_pct") is not None:
                    closed.append(t)
        except Exception:
            continue

    import config
    pair_capital = config.INITIAL_CAPITAL / len(config.SUPPORTED_PAIRS)

    if not closed:
        return {
            "closed_trades": 0, "win_rate": 0.0, "pnl_pct": 0.0,
            "pnl_usd": 0.0, "realized_sharpe": 0.0,
            "last_7d_pnl_pct": 0.0, "pair_capital": pair_capital,
        }

    # Sort by entry_date
    closed.sort(key=lambda t: t.get("entry_date", ""))
    pnl_pcts = [t["pnl_pct"] / 100 for t in closed]  # convert to decimal
    pnl_usds = [t.get("pnl_usd", 0) or 0 for t in closed]
    wins = sum(1 for p in pnl_pcts if p > 0)
    win_rate = wins / len(closed)
    total_pnl_usd = sum(pnl_usds)
    pnl_pct = total_pnl_usd / pair_capital * 100

    # Realized Sharpe — annualized from per-trade returns (rough, noisy at low n)
    if len(pnl_pcts) >= 3:
        mu = np.mean(pnl_pcts)
        sd = np.std(pnl_pcts)
        # Assume ~40 trades/yr per pair as rough annualization base
        sharpe = (mu / sd * np.sqrt(40)) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    # Last-7d P&L for circuit breaker
    cutoff = datetime.now() - timedelta(days=7)
    last_7d = [t for t in closed
               if datetime.fromisoformat(t.get("exit_date", "1970-01-01T00:00")[:19]) >= cutoff]
    last_7d_pnl_pct = sum(t.get("pnl_usd", 0) or 0 for t in last_7d) / pair_capital * 100

    return {
        "closed_trades": len(closed),
        "win_rate": win_rate,
        "pnl_pct": pnl_pct,
        "pnl_usd": total_pnl_usd,
        "realized_sharpe": sharpe,
        "last_7d_pnl_pct": last_7d_pnl_pct,
        "pair_capital": pair_capital,
    }


def _backtest_sharpe(pair: str) -> float:
    """Pull backtest Sharpe for a pair from CLAUDE.md reference table."""
    # Hardcoded from CLAUDE.md baselines (2026-04-19). Falls back to 1.0
    # for pairs without backtest (LTC/XRP/BAT/ETC/BCH/new pairs).
    BACKTEST_SHARPE = {
        "BTC": 2.11, "ETH": 1.17, "SOL": 1.23,
    }
    return BACKTEST_SHARPE.get(pair, 1.0)


def evaluate_pair(pair: str) -> Dict:
    """
    Apply promotion/demotion rules to one pair. Returns new state dict.
    Does not persist — caller saves.
    """
    state = load_tiers()
    current = state["pairs"].get(pair, {"tier": "paper", "history": []})
    tier = current["tier"]
    stats = _pair_stats(pair)
    backtest_sharpe = _backtest_sharpe(pair)
    now_iso = datetime.now().isoformat()
    new_tier = tier
    reason = "no change"

    # --- Circuit breaker (demotion) — applies from any tier ---
    if tier in ("micro", "half", "full"):
        if stats["last_7d_pnl_pct"] <= DEMOTION_TRIGGERS["weekly_loss_pct"]:
            new_tier = "paused"
            reason = (f"circuit breaker: last 7d P&L {stats['last_7d_pnl_pct']:.1f}% "
                      f"<= {DEMOTION_TRIGGERS['weekly_loss_pct']}%")
        elif stats["win_rate"] <= DEMOTION_TRIGGERS["win_rate_floor"] and stats["closed_trades"] >= 5:
            new_tier = "paused"
            reason = (f"circuit breaker: WR {stats['win_rate']*100:.0f}% "
                      f"<= {DEMOTION_TRIGGERS['win_rate_floor']*100:.0f}% floor")

    # --- Paused → paper after cooldown ---
    if tier == "paused" and new_tier == "paused":
        last_change = datetime.fromisoformat(current.get("last_change", now_iso))
        if datetime.now() - last_change > timedelta(days=PAUSE_COOLDOWN_DAYS):
            new_tier = "paper"
            reason = f"cooldown elapsed ({PAUSE_COOLDOWN_DAYS}d) — back to paper for re-qualification"

    # --- Promotion path (only if not demoted this cycle) ---
    if new_tier == tier:
        if tier == "paper":
            g = PROMOTION_GATES["paper_to_micro"]
            if (stats["closed_trades"] >= g["min_closed_trades"]
                    and stats["win_rate"] >= g["min_win_rate"]
                    and stats["pnl_pct"] >= g["max_pnl_drawdown_pct"]):
                new_tier = "micro"
                reason = (f"promoted: {stats['closed_trades']} trades, "
                          f"WR {stats['win_rate']*100:.0f}%, P&L {stats['pnl_pct']:+.2f}%")
        elif tier == "micro":
            g = PROMOTION_GATES["micro_to_half"]
            if (stats["closed_trades"] >= g["min_closed_trades"]
                    and stats["win_rate"] >= g["min_win_rate"]
                    and stats["pnl_pct"] > 0
                    and stats["realized_sharpe"] >= g["min_sharpe"]):
                new_tier = "half"
                reason = (f"promoted: {stats['closed_trades']} trades, "
                          f"WR {stats['win_rate']*100:.0f}%, Sharpe {stats['realized_sharpe']:.2f}")
        elif tier == "half":
            g = PROMOTION_GATES["half_to_full"]
            bar = backtest_sharpe * g["min_sharpe_pct_of_backtest"]
            if (stats["closed_trades"] >= g["min_closed_trades"]
                    and stats["win_rate"] >= g["min_win_rate"]
                    and stats["pnl_pct"] > 0
                    and stats["realized_sharpe"] >= bar):
                new_tier = "full"
                reason = (f"promoted: {stats['closed_trades']} trades, "
                          f"WR {stats['win_rate']*100:.0f}%, "
                          f"Sharpe {stats['realized_sharpe']:.2f} >= {bar:.2f}")

    # Persist
    if new_tier != tier:
        history = current.get("history", [])
        history.append({
            "at": now_iso, "from": tier, "to": new_tier, "reason": reason,
            "snapshot": {
                "closed_trades": stats["closed_trades"],
                "win_rate_pct": round(stats["win_rate"] * 100, 1),
                "pnl_pct": round(stats["pnl_pct"], 2),
                "realized_sharpe": round(stats["realized_sharpe"], 2),
                "last_7d_pnl_pct": round(stats["last_7d_pnl_pct"], 2),
            },
        })
        state["pairs"][pair] = {
            "tier": new_tier,
            "last_change": now_iso,
            "reason": reason,
            "history": history,
        }
    else:
        # Just update stats snapshot without changing tier
        state["pairs"][pair]["last_stats"] = {
            "closed_trades": stats["closed_trades"],
            "win_rate_pct": round(stats["win_rate"] * 100, 1),
            "pnl_pct": round(stats["pnl_pct"], 2),
            "realized_sharpe": round(stats["realized_sharpe"], 2),
            "checked_at": now_iso,
        }

    return {
        "pair": pair, "from_tier": tier, "to_tier": new_tier,
        "reason": reason, "stats": stats, "state": state,
    }


def evaluate_all() -> Dict:
    """Run evaluation across all pairs; persist and return report."""
    import config
    state = load_tiers()
    changes = []
    for pair in config.SUPPORTED_PAIRS:
        result = evaluate_pair(pair)
        state = result["state"]
        if result["from_tier"] != result["to_tier"]:
            changes.append({
                "pair": pair,
                "from": result["from_tier"],
                "to": result["to_tier"],
                "reason": result["reason"],
            })
    state["last_eval"] = datetime.now().isoformat()
    save_tiers(state)
    return {
        "ran_at": datetime.now().isoformat(),
        "changes": changes,
        "tier_distribution": _tier_distribution(state),
    }


def _tier_distribution(state: Dict) -> Dict:
    from collections import Counter
    return dict(Counter(p.get("tier", "paper") for p in state["pairs"].values()))


def current_summary() -> str:
    """Human-readable dump of current tier state for debugging."""
    state = load_tiers()
    lines = [f"Tier state (last eval: {state.get('last_eval', 'never')})"]
    lines.append(f"Distribution: {_tier_distribution(state)}")
    for pair in sorted(state["pairs"]):
        p = state["pairs"][pair]
        mult = TIER_MULTIPLIERS[p["tier"]]
        lines.append(f"  {pair:6s}  {p['tier']:7s}  ({mult*100:3.0f}% capital)  {p.get('reason', '')}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "eval":
        result = evaluate_all()
        print(json.dumps(result, indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "init":
        save_tiers(_default_state())
        print("Initialized default tier state (all pairs → paper)")
    else:
        print(current_summary())
