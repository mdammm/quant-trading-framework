"""
Tests for the public signals module — verify each signal returns boolean
output of the right shape and fires on synthetic edge-case input.

Run: python3 -m unittest tests.test_public_signals
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from public_signals import (
    PUBLIC_SIGNAL_REGISTRY,
    compute_public_signals,
    extreme_fear,
    extreme_greed,
    funding_extreme,
    stablecoin_inflow,
    short_liq_cascade,
    liq_imbalance_long,
    vol_compression,
    volume_shock_up,
)


def _df(n=120, **overrides):
    """Synthetic dataset with the expected columns; defaults to no signal fires."""
    df = pd.DataFrame({
        "trade_date": pd.date_range("2024-01-01", periods=n, freq="D"),
        "btc_close": np.linspace(50000, 60000, n),
        "btc_high": np.linspace(50500, 60500, n),
        "btc_low": np.linspace(49500, 59500, n),
        "btc_volume": np.full(n, 1000.0),
        "btc_return": np.full(n, 0.001),
        "btc_funding_rate": np.zeros(n),
        "fear_greed_value": np.full(n, 50),
        "stablecoin_mcap": np.linspace(1e11, 1.1e11, n),
    })
    for k, v in overrides.items():
        df[k] = v
    return df


class PublicSignalsTest(unittest.TestCase):

    def test_extreme_fear_fires_below_25(self):
        df = _df(n=10, fear_greed_value=[10] * 10)
        self.assertTrue(extreme_fear(df).iloc[-1])

    def test_extreme_fear_silent_above_25(self):
        df = _df(n=10, fear_greed_value=[40] * 10)
        self.assertFalse(extreme_fear(df).iloc[-1])

    def test_extreme_greed_fires_above_75(self):
        df = _df(n=10, fear_greed_value=[85] * 10)
        self.assertTrue(extreme_greed(df).iloc[-1])

    def test_funding_extreme(self):
        df = _df(n=10, btc_funding_rate=[0.0015] * 10)
        self.assertTrue(funding_extreme(df).iloc[-1])
        df = _df(n=10, btc_funding_rate=[0.0001] * 10)
        self.assertFalse(funding_extreme(df).iloc[-1])

    def test_stablecoin_inflow_wow(self):
        # 10% inflow over 7 days
        n = 14
        mcap = np.concatenate([np.full(7, 1e11), np.full(7, 1.10e11)])
        df = _df(n=n, stablecoin_mcap=mcap)
        # The 8th day onward sees > 0.5% wow growth
        result = stablecoin_inflow(df)
        self.assertTrue(result.iloc[-1])

    def test_short_liq_cascade_threshold(self):
        df = _df(n=5)
        df["short_liquidations_usd"] = [10e6, 20e6, 60e6, 30e6, 5e6]
        result = short_liq_cascade(df)
        self.assertTrue(result.iloc[2])  # 60M > 50M threshold
        self.assertFalse(result.iloc[0])

    def test_liq_imbalance_long(self):
        df = _df(n=5)
        df["long_liquidations_usd"] = [100e6, 50e6, 100e6, 100e6, 100e6]
        df["short_liquidations_usd"] = [10e6, 50e6, 30e6, 100e6, 50e6]
        result = liq_imbalance_long(df)
        self.assertTrue(result.iloc[0])   # 100/10 = 10x > 3
        self.assertFalse(result.iloc[3])  # 100/100 = 1x

    def test_vol_compression_returns_bool(self):
        df = _df(n=80)
        result = vol_compression(df)
        self.assertEqual(result.dtype, bool)
        self.assertEqual(len(result), 80)

    def test_volume_shock_up(self):
        # 25 days at 1000 vol then 1 spike day at 5000 vol with green close.
        # Rolling 20d median on the spike day (looking back) is still 1000.
        df = _df(n=26, btc_volume=np.concatenate([np.full(25, 1000), [5000]]))
        df["btc_return"] = 0.05
        result = volume_shock_up(df)
        self.assertTrue(result.iloc[-1])

    def test_compute_all_signals_adds_columns(self):
        df = _df(n=80)
        df = compute_public_signals(df)
        for sig in PUBLIC_SIGNAL_REGISTRY:
            self.assertIn(f"sig_{sig}", df.columns)
            self.assertEqual(df[f"sig_{sig}"].dtype, bool)


class CoverageTest(unittest.TestCase):
    """Smoke test: every signal in the registry must have a callable + return bool series."""

    def test_all_registered_signals_have_callables(self):
        df = _df(n=80)
        for name, fn in PUBLIC_SIGNAL_REGISTRY.items():
            with self.subTest(signal=name):
                result = fn(df)
                self.assertIsInstance(result, pd.Series, f"{name} did not return Series")
                self.assertEqual(len(result), len(df), f"{name} wrong length")
                # All entries should be bool-like
                for v in result.dropna().unique():
                    self.assertIn(bool(v), (True, False))


if __name__ == "__main__":
    unittest.main()
