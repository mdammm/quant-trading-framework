"""
Public-data signals — replacements for the exchange-internal signals from the
internal edition. All inputs are free public APIs:

- OKX: OHLCV, funding rates, open interest, taker ratio
- Alternative.me: Fear & Greed Index
- DefiLlama: stablecoin total market cap
- Coinalyze: liquidation history (free tier)
- Deribit: BTC options summary (P/C ratio, IV)
- CCXT: order book depth snapshot

The internal edition uses exchange-internal flow data (institutional fill
divergence, segment-specific accumulation, internalization rate). Those
signals carry most of the alpha and are not in this repo. The signals
here are public-data analogs that follow the same structural pattern:
volume/flow regime detection at multiple timescales.

Each signal returns a boolean Series indexed like the input dataframe.
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Volume regime signals (OKX OHLCV only)
# ---------------------------------------------------------------------------

def vol_compression(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """Bollinger band width below the 20th percentile of trailing 60d.

    Compression = vol coiled, often precedes a directional move.
    """
    if "btc_close" not in df.columns:
        return pd.Series(False, index=df.index)
    bb_width = (
        df["btc_close"].rolling(lookback).std()
        / df["btc_close"].rolling(lookback).mean()
    )
    threshold = bb_width.rolling(60).quantile(0.20)
    return (bb_width < threshold).fillna(False)


def volume_shock_up(df: pd.DataFrame, multiplier: float = 2.0) -> pd.Series:
    """Daily volume > 2x trailing 20d median, with a green close."""
    if "btc_volume" not in df.columns:
        return pd.Series(False, index=df.index)
    median20 = df["btc_volume"].rolling(20).median()
    return ((df["btc_volume"] > median20 * multiplier)
            & (df["btc_return"] > 0)).fillna(False)


def volume_shock_down(df: pd.DataFrame, multiplier: float = 2.0) -> pd.Series:
    """Volume shock with a red close — distribution candle."""
    if "btc_volume" not in df.columns:
        return pd.Series(False, index=df.index)
    median20 = df["btc_volume"].rolling(20).median()
    return ((df["btc_volume"] > median20 * multiplier)
            & (df["btc_return"] < 0)).fillna(False)


def compression_breakout(df: pd.DataFrame) -> pd.Series:
    """Compression yesterday + strong-volume close today = breakout."""
    if "btc_volume" not in df.columns:
        return pd.Series(False, index=df.index)
    compressed_yesterday = vol_compression(df).shift(1).fillna(False)
    high_vol_today = volume_shock_up(df, multiplier=1.5)
    return compressed_yesterday & high_vol_today


# ---------------------------------------------------------------------------
# Funding rate signals (OKX, free)
# ---------------------------------------------------------------------------

def funding_extreme(df: pd.DataFrame, threshold: float = 0.001) -> pd.Series:
    """Funding rate > 0.10% (extreme positioning, often a contrarian short)."""
    if "btc_funding_rate" not in df.columns:
        return pd.Series(False, index=df.index)
    return (df["btc_funding_rate"].abs() > threshold).fillna(False)


def funding_flip(df: pd.DataFrame) -> pd.Series:
    """Funding rate sign flips after >= 5 days same direction."""
    if "btc_funding_rate" not in df.columns:
        return pd.Series(False, index=df.index)
    fr = df["btc_funding_rate"]
    sign = np.sign(fr)
    runs = (sign != sign.shift(1)).cumsum()
    run_len = runs.groupby(runs).cumcount() + 1
    return ((sign != sign.shift(1)) & (run_len.shift(1) >= 5)).fillna(False)


# ---------------------------------------------------------------------------
# Sentiment signals (Alternative.me Fear & Greed)
# ---------------------------------------------------------------------------

def extreme_fear(df: pd.DataFrame, threshold: int = 25) -> pd.Series:
    """F&G < 25. Often a contrarian buying opportunity."""
    if "fear_greed_value" not in df.columns:
        return pd.Series(False, index=df.index)
    return (df["fear_greed_value"] < threshold).fillna(False)


def extreme_greed(df: pd.DataFrame, threshold: int = 75) -> pd.Series:
    """F&G > 75. Often a contrarian shorting opportunity."""
    if "fear_greed_value" not in df.columns:
        return pd.Series(False, index=df.index)
    return (df["fear_greed_value"] > threshold).fillna(False)


# ---------------------------------------------------------------------------
# Stablecoin flow signals (DefiLlama, free)
# ---------------------------------------------------------------------------

def stablecoin_inflow(df: pd.DataFrame, threshold: float = 0.005) -> pd.Series:
    """Stablecoin total mcap grew > 0.5% week-over-week. Capital coming in."""
    if "stablecoin_mcap" not in df.columns:
        return pd.Series(False, index=df.index)
    wow = df["stablecoin_mcap"].pct_change(7, fill_method=None)
    return (wow > threshold).fillna(False)


# ---------------------------------------------------------------------------
# Liquidation signals (Coinalyze, free tier)
# ---------------------------------------------------------------------------

def short_liq_cascade(df: pd.DataFrame, threshold_usd: float = 5e7) -> pd.Series:
    """> $<redacted> of short liquidations in a day. Often a short-squeeze setup."""
    if "short_liquidations_usd" not in df.columns:
        return pd.Series(False, index=df.index)
    return (df["short_liquidations_usd"] > threshold_usd).fillna(False)


def liq_imbalance_long(df: pd.DataFrame, ratio: float = 3.0) -> pd.Series:
    """Long liqs > 3x short liqs in same day. Local capitulation low."""
    if "long_liquidations_usd" not in df.columns:
        return pd.Series(False, index=df.index)
    if "short_liquidations_usd" not in df.columns:
        return pd.Series(False, index=df.index)
    longs = df["long_liquidations_usd"]
    shorts = df["short_liquidations_usd"].replace(0, np.nan)
    return (longs / shorts > ratio).fillna(False)


# ---------------------------------------------------------------------------
# Options-derived signals (Deribit, free snapshots)
# ---------------------------------------------------------------------------

def put_call_low(df: pd.DataFrame, threshold: float = 0.5) -> pd.Series:
    """Put/call ratio below 0.5 — bullish positioning."""
    if "put_call_ratio" not in df.columns:
        return pd.Series(False, index=df.index)
    return (df["put_call_ratio"] < threshold).fillna(False)


def iv_crush(df: pd.DataFrame, drop_pct: float = 0.20) -> pd.Series:
    """Implied vol drops > 20% week-over-week — often after event resolution."""
    if "btc_iv" not in df.columns:
        return pd.Series(False, index=df.index)
    iv_change = df["btc_iv"].pct_change(7, fill_method=None)
    return (iv_change < -drop_pct).fillna(False)


# ---------------------------------------------------------------------------
# Order book signals (CCXT free snapshot)
# ---------------------------------------------------------------------------

def depth_imbalance_bid(df: pd.DataFrame, threshold: float = 0.65) -> pd.Series:
    """Bid depth > 65% of total within 1% of mid. Buyers stacking."""
    if "depth_bid_share" not in df.columns:
        return pd.Series(False, index=df.index)
    return (df["depth_bid_share"] > threshold).fillna(False)


# ---------------------------------------------------------------------------
# Registry — name → callable
# ---------------------------------------------------------------------------

PUBLIC_SIGNAL_REGISTRY = {
    "vol_compression": vol_compression,
    "volume_shock_up": volume_shock_up,
    "volume_shock_down": volume_shock_down,
    "compression_breakout": compression_breakout,
    "funding_extreme": funding_extreme,
    "funding_flip": funding_flip,
    "extreme_fear": extreme_fear,
    "extreme_greed": extreme_greed,
    "stablecoin_inflow": stablecoin_inflow,
    "short_liq_cascade": short_liq_cascade,
    "liq_imbalance_long": liq_imbalance_long,
    "put_call_low": put_call_low,
    "iv_crush": iv_crush,
    "depth_imbalance_bid": depth_imbalance_bid,
}

# Subset that historically validated as edge-bearing on public-data backtests
ROBUST_PUBLIC_SIGNALS = {
    "compression_breakout",
    "extreme_fear",
    "stablecoin_inflow",
    "liq_imbalance_long",
    "depth_imbalance_bid",
}

# Contrarian (short-biased) — fire when overheated
CONTRARIAN_PUBLIC_SIGNALS = {
    "funding_extreme",
    "extreme_greed",
    "iv_crush",
    "short_liq_cascade",
    "volume_shock_down",
}


def compute_public_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Add sig_<name> columns for every signal in the public registry."""
    additions = {}
    for name, fn in PUBLIC_SIGNAL_REGISTRY.items():
        col = f"sig_{name}"
        if col in df.columns:
            df = df.drop(columns=[col])
        additions[col] = fn(df).astype(bool)
    df = pd.concat([df, pd.DataFrame(additions, index=df.index)], axis=1)
    return df
