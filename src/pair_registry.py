"""
Centralized pair registry — the SINGLE SOURCE OF TRUTH for pair lists and
per-pair styling. Every component that needs "which pairs exist" or "what
color is BTC" should import from here instead of hardcoding.

Created 2026-04-19 after an audit found pair lists hardcoded in 9+ places,
causing bugs whenever a new pair was added. The single source replaces:
  - ["BTC", "ETH", "SOL"] loops in changelog, generate_historical_briefs,
    system_check, dashboard_data
  - Duplicate PAIR_COLORS dicts in overview/paper_trading/multi_pair
  - Hardcoded 8-pair lists in dashboard.py, dashboard_data.get_pnl_series

Rule: if code references a specific pair name (BTC/ETH/SOL/etc), it should
come from here. Only exception: BTC is still the "primary" pair for the
dataset column prefix convention (btc_close gets remapped for other pairs).
"""
from typing import Dict, List, Tuple

import config

# Default color palette (visually distinct, dark-theme friendly).
# Primary pairs (BTC/ETH/SOL) use the project palette from dashboard_styles;
# remaining pairs use curated distinct hex codes.
_PAIR_COLOR_PALETTE = {
    "BTC":  "#fbbf24",  # amber
    "ETH":  "#42a5f5",  # blue
    "SOL":  "#ab47bc",  # purple
    "LTC":  "#bdbdbd",  # silver
    "XRP":  "#26c6da",  # cyan
    "BAT":  "#ef5350",  # red
    "ETC":  "#66bb6a",  # green
    "BCH":  "#ff7043",  # deep orange
    "ADA":  "#3cb3c0",  # teal
    "DOGE": "#c4a94a",  # gold
    "AVAX": "#e84142",  # avax red
    "DOT":  "#e6007a",  # polkadot pink
    "LINK": "#2a5ada",  # chainlink blue
    "MANA": "#ff2d55",  # decentraland pink
    "NEAR": "#00c08b",  # near green
}

# Bootstrap accent names (for KPI cards). Maps to dashboard_styles colors.
_PAIR_ACCENT_PALETTE = {
    "BTC":  "amber",   "ETH":  "blue",   "SOL":  "purple",
    "LTC":  "blue",    "XRP":  "blue",   "BAT":  "red",
    "ETC":  "green",   "BCH":  "orange", "ADA":  "teal",
    "DOGE": "amber",   "AVAX": "red",    "DOT":  "red",
    "LINK": "blue",    "MANA": "red",    "NEAR": "green",
}

# Fallback color cycle for pairs not in the curated palette (future additions)
_FALLBACK_COLORS = ["#ff9800", "#4caf50", "#2196f3", "#e91e63", "#9c27b0",
                    "#00bcd4", "#ffeb3b", "#795548", "#607d8b", "#f44336"]


def get_pairs() -> List[str]:
    """Return the canonical ordered list of supported pairs."""
    return list(config.SUPPORTED_PAIRS.keys())


def get_pair_prefixes() -> List[str]:
    """Return lowercase column prefixes for all pairs (e.g., 'btc', 'eth')."""
    return [cfg["col_prefix"] for cfg in config.SUPPORTED_PAIRS.values()]


def get_pair_close_cols() -> List[str]:
    """Return all `<pair>_close` column names for the dataset."""
    return [f"{p}_close" for p in get_pair_prefixes()]


def pair_color(pair: str) -> str:
    """Return hex color for a pair. Falls back to a distinct cycle color."""
    if pair in _PAIR_COLOR_PALETTE:
        return _PAIR_COLOR_PALETTE[pair]
    # Fallback: deterministic from pair name hash
    idx = hash(pair) % len(_FALLBACK_COLORS)
    return _FALLBACK_COLORS[idx]


def pair_accent(pair: str) -> str:
    """Return bootstrap accent name for a pair."""
    return _PAIR_ACCENT_PALETTE.get(pair, "blue")


def all_pair_colors() -> Dict[str, str]:
    """Return {pair: hex_color} for all supported pairs."""
    return {pair: pair_color(pair) for pair in get_pairs()}


def all_pair_accents() -> Dict[str, str]:
    """Return {pair: accent_name} for all supported pairs."""
    return {pair: pair_accent(pair) for pair in get_pairs()}


def paper_trade_files(source: str = "both") -> List[Tuple[str, str]]:
    """
    Return (pair, filename) tuples for paper trade files.
    source: 'daily' | '4h' | 'both'
    """
    pairs = get_pairs()
    result = []
    if source in ("daily", "both"):
        result += [(p, f"paper_trades_{p.lower()}.json") for p in pairs]
    if source in ("4h", "both"):
        result += [(p, f"paper_trades_4h_{p.lower()}.json") for p in pairs]
    return result


# --- Convenience: primary pairs (BTC/ETH/SOL) for contexts where we want
# just the three most-liquid. Use sparingly and only where intentional.
PRIMARY_PAIRS = ["BTC", "ETH", "SOL"]


def is_primary(pair: str) -> bool:
    return pair in PRIMARY_PAIRS
