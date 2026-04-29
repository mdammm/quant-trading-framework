"""
Public-data long/short strategy. Mirrors the structure of the internal
LongShortStrategy but reads only public_signals.py outputs. Same gates,
same hold periods, same FIR exit ramp — only the input signal set differs.
"""

import numpy as np
import pandas as pd

from public_signals import (
    PUBLIC_SIGNAL_REGISTRY,
    ROBUST_PUBLIC_SIGNALS,
    CONTRARIAN_PUBLIC_SIGNALS,
    compute_public_signals,
)


class PublicLongShortStrategy:
    """Long when robust public signals confluence (>=1) AND BTC > MA50.
    Short when contrarian signals fire AND BTC < MA50.
    Hold 10d for longs, 5d for shorts (validated on internal edition,
    same forward-window optima here).
    """

    name = "public_long_short"
    hold_days = 10
    hold_days_short = 5

    def __init__(
        self,
        long_sigs=None,
        short_sigs=None,
        use_btc_trend_gate=True,
    ):
        self.long_sigs = long_sigs or list(ROBUST_PUBLIC_SIGNALS)
        self.short_sigs = short_sigs or list(CONTRARIAN_PUBLIC_SIGNALS)
        self.use_btc_trend_gate = use_btc_trend_gate

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        # MA50 trend gate
        ma50 = df["btc_close"].rolling(50).mean()
        in_uptrend = df["btc_close"] > ma50
        in_downtrend = df["btc_close"] < ma50

        # Long score = sum of robust public signals firing
        long_score = pd.Series(0.0, index=df.index)
        for sig in self.long_sigs:
            col = f"sig_{sig}"
            if col in df.columns:
                long_score += df[col].astype(float)

        # Short score = sum of contrarian public signals
        short_score = pd.Series(0.0, index=df.index)
        for sig in self.short_sigs:
            col = f"sig_{sig}"
            if col in df.columns:
                short_score += df[col].astype(float)

        # ATR sizing — inverse-vol position sizing
        if "atr_pct" in df.columns:
            atr_med = df["atr_pct"].rolling(60).median()
            size = (atr_med / df["atr_pct"].replace(0, np.nan)).clip(0.3, 1.0).fillna(1.0)
        else:
            size = pd.Series(1.0, index=df.index)

        pos = pd.Series(0.0, index=df.index)
        long_ok = (long_score >= 1)
        short_ok = (short_score >= 1)
        if self.use_btc_trend_gate:
            long_ok = long_ok & in_uptrend
            short_ok = short_ok & in_downtrend
        pos[long_ok] = size[long_ok]
        pos[short_ok] = -size[short_ok]
        return pos
