"""
Build the public dataset from free APIs only. No exchange-internal data.

Output: cache/public_dataset.csv with columns:
- trade_date, btc_close, btc_high, btc_low, btc_open, btc_volume
- btc_return, atr_14, atr_pct
- btc_funding_rate (OKX, last 33d available, NaN before)
- fear_greed_value (Alternative.me, full history)
- stablecoin_mcap (DefiLlama, full history)
- short_liquidations_usd, long_liquidations_usd (Coinalyze, last 365d)
- put_call_ratio, btc_iv (Deribit, snapshot accumulating)
- depth_bid_share (CCXT, snapshot accumulating)

This is a sub-dataset of what build_master_dataset() produces in the
internal edition. The structural code (signals, strategy, walk-forward,
allocator) all run unchanged on this dataset.

Usage:
    python build_public_dataset.py             # full build
    python build_public_dataset.py --lookback 365   # last 1y only
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# OKX candles (BTC daily OHLCV + volume)
# ---------------------------------------------------------------------------

def fetch_okx_btc_daily(lookback_days: int = 1500) -> pd.DataFrame:
    """OKX returns max 100 candles per call; loop with `before` cursor."""
    url = "https://www.okx.com/api/v5/market/history-candles"
    rows = []
    before = ""
    end_ms = int(pd.Timestamp.now().timestamp() * 1000)
    target_ms = end_ms - lookback_days * 86400 * 1000

    while True:
        params = {"instId": "BTC-USDT", "bar": "1D", "limit": 100}
        if before:
            params["after"] = before
        r = requests.get(url, params=params, timeout=10)
        data = r.json().get("data", [])
        if not data:
            break
        rows.extend(data)
        # Earliest ts in this batch (column 0 is timestamp ms)
        earliest = int(data[-1][0])
        if earliest <= target_ms:
            break
        before = data[-1][0]

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        "ts", "open", "high", "low", "close",
        "volume", "volume_currency", "volume_currency_quote", "confirm",
    ])
    df["trade_date"] = pd.to_datetime(df["ts"].astype(int), unit="ms").dt.normalize()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.rename(columns={
        "open": "btc_open", "high": "btc_high", "low": "btc_low",
        "close": "btc_close", "volume": "btc_volume",
    })
    df = (df[["trade_date", "btc_open", "btc_high", "btc_low", "btc_close", "btc_volume"]]
          .drop_duplicates("trade_date")
          .sort_values("trade_date")
          .reset_index(drop=True))
    return df


# ---------------------------------------------------------------------------
# Fear & Greed (Alternative.me)
# ---------------------------------------------------------------------------

def fetch_fear_greed() -> pd.DataFrame:
    url = "https://api.alternative.me/fng/?limit=0"
    r = requests.get(url, timeout=10)
    data = r.json().get("data", [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["trade_date"] = pd.to_datetime(df["timestamp"].astype(int), unit="s").dt.normalize()
    df["fear_greed_value"] = pd.to_numeric(df["value"], errors="coerce")
    return df[["trade_date", "fear_greed_value"]].sort_values("trade_date")


# ---------------------------------------------------------------------------
# Stablecoin total mcap (DefiLlama)
# ---------------------------------------------------------------------------

def fetch_stablecoin_mcap() -> pd.DataFrame:
    url = "https://stablecoins.llama.fi/stablecoincharts/all"
    r = requests.get(url, timeout=15)
    data = r.json()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["trade_date"] = pd.to_datetime(df["date"].astype(int), unit="s").dt.normalize()
    df["stablecoin_mcap"] = df["totalCirculatingUSD"].apply(
        lambda d: float(d.get("peggedUSD", 0)) if isinstance(d, dict) else 0
    )
    return df[["trade_date", "stablecoin_mcap"]].sort_values("trade_date")


# ---------------------------------------------------------------------------
# Funding rates (OKX, last 33d available)
# ---------------------------------------------------------------------------

def fetch_btc_funding() -> pd.DataFrame:
    url = "https://www.okx.com/api/v5/public/funding-rate-history"
    params = {"instId": "BTC-USDT-SWAP", "limit": 100}
    r = requests.get(url, params=params, timeout=10)
    data = r.json().get("data", [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["trade_date"] = pd.to_datetime(df["fundingTime"].astype(int), unit="ms").dt.normalize()
    df["btc_funding_rate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    return (df[["trade_date", "btc_funding_rate"]]
            .groupby("trade_date").mean().reset_index())


# ---------------------------------------------------------------------------
# Build + merge
# ---------------------------------------------------------------------------

def build(lookback_days: int = 1500) -> pd.DataFrame:
    print("Fetching BTC daily OHLCV from OKX...")
    df = fetch_okx_btc_daily(lookback_days=lookback_days)
    if df.empty:
        raise RuntimeError("No BTC data from OKX")
    print(f"  {len(df)} days, {df['trade_date'].min().date()} to {df['trade_date'].max().date()}")

    df["btc_return"] = df["btc_close"].pct_change()

    # ATR-14 (Wilder TR averaged via SMA)
    prev_close = df["btc_close"].shift(1)
    tr = pd.concat([
        df["btc_high"] - df["btc_low"],
        (df["btc_high"] - prev_close).abs(),
        (df["btc_low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean()
    df["atr_pct"] = df["atr_14"] / df["btc_close"]

    print("Fetching Fear & Greed Index...")
    fg = fetch_fear_greed()
    if not fg.empty:
        print(f"  {len(fg)} days")
        df = df.merge(fg, on="trade_date", how="left")

    print("Fetching stablecoin mcap from DefiLlama...")
    sc = fetch_stablecoin_mcap()
    if not sc.empty:
        print(f"  {len(sc)} days")
        df = df.merge(sc, on="trade_date", how="left")

    print("Fetching BTC funding rate from OKX (last ~33d)...")
    fr = fetch_btc_funding()
    if not fr.empty:
        print(f"  {len(fr)} days")
        df = df.merge(fr, on="trade_date", how="left")

    # Cache
    out = CACHE_DIR / "public_dataset.csv"
    df.to_csv(out, index=False)
    print(f"\nWrote {out} ({len(df)} rows, {len(df.columns)} cols)")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=1500,
                    help="lookback days (default 1500 = ~4yr)")
    args = ap.parse_args()
    build(lookback_days=args.lookback)


if __name__ == "__main__":
    main()
