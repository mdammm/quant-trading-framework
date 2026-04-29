"""
FRED Data Integration — fetch macroeconomic data from the Federal Reserve
Economic Data API and compute regime indicators for signal filtering.

Key series:
  CPIAUCSL  — CPI (Consumer Price Index, monthly, seasonally adjusted)
  CPILFESL  — Core CPI (ex food & energy)
  FEDFUNDS  — Federal Funds Effective Rate (monthly)
  UNRATE    — Unemployment Rate (monthly)
  T10Y2Y    — 10Y-2Y Treasury Spread (daily, yield curve)
  VIXCLS    — VIX (daily, equity fear gauge)

Regime logic:
  FRIENDLY:  CPI trending down + Fed Funds stable/falling + unemployment stable
             → risk assets (BTC) tend to rally, signals more reliable
  HOSTILE:   CPI trending up + Fed Funds rising + unemployment spiking
             → risk-off, BTC signals less reliable (macro dominates)
  NEUTRAL:   mixed signals

Usage:
    from fred_data import load_fred_regime, add_fred_regime

    # Standalone check
    regime = load_fred_regime()
    print(regime)  # {'regime': 'FRIENDLY', 'cpi_trend': 'falling', ...}

    # Add to dataset
    df = add_fred_regime(df)  # adds 'macro_regime' column

Requires FRED_API_KEY in environment or .env file.
Get a free key: https://fred.stlouisfed.org/docs/api/api_key.html
"""

import os
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import config


# ---------------------------------------------------------------------------
# API configuration
# ---------------------------------------------------------------------------

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_CACHE_DIR = config.CACHE_DIR / "fred"

# Series IDs
SERIES = {
    "cpi": "CPIAUCSL",           # CPI All Urban Consumers (monthly)
    "core_cpi": "CPILFESL",      # Core CPI ex food & energy (monthly)
    "fed_funds": "FEDFUNDS",      # Fed Funds Rate (monthly)
    "unemployment": "UNRATE",     # Unemployment Rate (monthly)
    "t10y2y": "T10Y2Y",          # 10Y-2Y Treasury Spread (daily)
    "vix": "VIXCLS",             # VIX (daily)
}


def _get_api_key():
    """Get FRED API key from environment."""
    key = os.environ.get("FRED_API_KEY", "")
    if not key:
        # Try loading from .env files
        from dotenv import load_dotenv
        for env_path in [
            config.PROJECT_DIR / ".env",
            config.PROJECT_DIR.parent / "Client Strat" / ".env",
            Path.home() / "copper_to_snowflake" / ".env",
        ]:
            if env_path.exists():
                load_dotenv(env_path)
        key = os.environ.get("FRED_API_KEY", "")
    return key


def fetch_series(series_id, start_date="2020-01-01", end_date=None, cache_hours=24):
    """
    Fetch a FRED series. Caches locally to avoid hammering the API.
    Returns DataFrame with columns: date, value.
    """
    FRED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = FRED_CACHE_DIR / f"{series_id}.csv"

    # Check cache
    if cache_file.exists():
        import time
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_hours < cache_hours:
            df = pd.read_csv(cache_file, parse_dates=["date"])
            return df

    api_key = _get_api_key()
    if not api_key:
        print(f"[FRED] No API key found. Set FRED_API_KEY in your .env file.")
        print(f"[FRED] Get a free key: https://fred.stlouisfed.org/docs/api/api_key.html")
        # Return cached data even if stale
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=["date"])
        return pd.DataFrame(columns=["date", "value"])

    end_date = end_date or datetime.now().strftime("%Y-%m-%d")

    try:
        resp = requests.get(FRED_BASE_URL, params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start_date,
            "observation_end": end_date,
            "sort_order": "asc",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("observations", [])
    except Exception as e:
        print(f"[FRED] Error fetching {series_id}: {e}")
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=["date"])
        return pd.DataFrame(columns=["date", "value"])

    rows = []
    for obs in data:
        val = obs.get("value", ".")
        if val == ".":
            continue
        rows.append({
            "date": pd.Timestamp(obs["date"]),
            "value": float(val),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df.to_csv(cache_file, index=False)
        print(f"[FRED] {series_id}: {len(df)} observations fetched and cached")

    return df


def load_all_fred(start_date="2020-01-01"):
    """Fetch all FRED series and merge into one DataFrame."""
    result = pd.DataFrame()

    for name, series_id in SERIES.items():
        df = fetch_series(series_id, start_date=start_date)
        if df.empty:
            continue
        df = df.rename(columns={"value": name})
        if result.empty:
            result = df
        else:
            result = result.merge(df, on="date", how="outer")

    if not result.empty:
        result = result.sort_values("date").reset_index(drop=True)

    return result


def compute_macro_regime(fred_df=None, start_date="2020-01-01"):
    """
    Compute macro regime from FRED data.

    Returns DataFrame with columns:
      date, macro_regime ('FRIENDLY', 'HOSTILE', 'NEUTRAL'),
      cpi_trend, cpi_mom, fed_funds_trend, unemployment_trend

    Logic:
      CPI trend: 3-month change in YoY CPI
        falling → friendly, rising → hostile
      Fed Funds trend: 3-month change in rate
        falling/stable → friendly, rising → hostile
      Unemployment trend: 3-month change
        stable/falling → friendly, spiking (>0.5pp) → hostile

    Regime = majority vote of the 3 indicators.
    """
    if fred_df is None:
        fred_df = load_all_fred(start_date)

    if fred_df.empty:
        print("[FRED] No data available for regime computation")
        return pd.DataFrame()

    # Work with monthly data (CPI, fed_funds, unemployment are monthly)
    monthly = fred_df.dropna(subset=["cpi"]).copy() if "cpi" in fred_df.columns else fred_df.copy()

    if monthly.empty or "cpi" not in monthly.columns:
        return pd.DataFrame()

    # CPI year-over-year change
    monthly["cpi_yoy"] = monthly["cpi"].pct_change(12, fill_method=None) * 100  # 12 months

    # CPI 3-month momentum (is YoY CPI accelerating or decelerating?)
    monthly["cpi_mom_3m"] = monthly["cpi_yoy"].diff(3)

    # CPI trend: falling = friendly
    monthly["cpi_trend"] = "neutral"
    monthly.loc[monthly["cpi_mom_3m"] < -0.3, "cpi_trend"] = "falling"
    monthly.loc[monthly["cpi_mom_3m"] > 0.3, "cpi_trend"] = "rising"

    # Fed Funds 3-month change
    if "fed_funds" in monthly.columns:
        monthly["ff_chg_3m"] = monthly["fed_funds"].diff(3)
        monthly["fed_funds_trend"] = "stable"
        monthly.loc[monthly["ff_chg_3m"] < -0.25, "fed_funds_trend"] = "falling"
        monthly.loc[monthly["ff_chg_3m"] > 0.25, "fed_funds_trend"] = "rising"
    else:
        monthly["fed_funds_trend"] = "unknown"

    # Unemployment 3-month change
    if "unemployment" in monthly.columns:
        monthly["ur_chg_3m"] = monthly["unemployment"].diff(3)
        monthly["unemployment_trend"] = "stable"
        monthly.loc[monthly["ur_chg_3m"] < -0.3, "unemployment_trend"] = "improving"
        monthly.loc[monthly["ur_chg_3m"] > 0.5, "unemployment_trend"] = "spiking"
    else:
        monthly["unemployment_trend"] = "unknown"

    # Regime = majority vote
    def _regime(row):
        friendly = 0
        hostile = 0

        # CPI
        if row.get("cpi_trend") == "falling":
            friendly += 1
        elif row.get("cpi_trend") == "rising":
            hostile += 1

        # Fed funds
        if row.get("fed_funds_trend") in ("falling", "stable"):
            friendly += 1
        elif row.get("fed_funds_trend") == "rising":
            hostile += 1

        # Unemployment
        if row.get("unemployment_trend") in ("stable", "improving"):
            friendly += 1
        elif row.get("unemployment_trend") == "spiking":
            hostile += 1

        if friendly >= 2:
            return "FRIENDLY"
        elif hostile >= 2:
            return "HOSTILE"
        return "NEUTRAL"

    monthly["macro_regime"] = monthly.apply(_regime, axis=1)

    # Keep relevant columns
    regime_df = monthly[["date", "macro_regime", "cpi_trend", "cpi_yoy",
                          "cpi_mom_3m", "fed_funds_trend", "unemployment_trend"]].copy()

    return regime_df


def add_fred_regime(df, date_col="trade_date"):
    """
    Add macro_regime column to a daily/4H DataFrame.
    Forward-fills monthly regime data to match bar frequency.
    """
    regime = compute_macro_regime()
    if regime.empty:
        print("[FRED] No regime data — defaulting to NEUTRAL")
        df["macro_regime"] = "NEUTRAL"
        df["cpi_trend"] = "unknown"
        return df

    # Forward-fill monthly regime onto daily dates
    regime = regime.rename(columns={"date": date_col})
    regime[date_col] = pd.to_datetime(regime[date_col])

    # CRITICAL: Shift monthly regime dates forward by 1 month to prevent
    # lookahead bias. FRED dates monthly data to the 1st of the reference
    # month, but CPI/FOMC/unemployment data is not actually released until
    # 2-6 weeks later. Shifting by 1 month is conservative but safe.
    regime[date_col] = regime[date_col] + pd.DateOffset(months=1)

    # Merge with forward fill
    df = df.sort_values(date_col)
    df = pd.merge_asof(
        df, regime[[date_col, "macro_regime", "cpi_trend", "cpi_yoy",
                     "fed_funds_trend", "unemployment_trend"]],
        on=date_col, direction="backward"
    )

    # Fill any remaining NaN at start of dataset
    df["macro_regime"] = df["macro_regime"].fillna("NEUTRAL")
    df["cpi_trend"] = df["cpi_trend"].fillna("unknown")

    regime_counts = df["macro_regime"].value_counts()
    print(f"[FRED] Macro regime breakdown:")
    for regime_name, count in regime_counts.items():
        pct = count / len(df) * 100
        print(f"  {regime_name}: {count} days ({pct:.0f}%)")

    return df


def current_regime():
    """Get the current macro regime for display/reporting."""
    regime = compute_macro_regime()
    if regime.empty:
        return {"regime": "UNKNOWN", "reason": "No FRED data available"}

    latest = regime.iloc[-1]
    return {
        "regime": latest["macro_regime"],
        "date": str(latest["date"].date()),
        "cpi_yoy": f"{latest['cpi_yoy']:.1f}%",
        "cpi_trend": latest["cpi_trend"],
        "fed_funds_trend": latest["fed_funds_trend"],
        "unemployment_trend": latest["unemployment_trend"],
    }
