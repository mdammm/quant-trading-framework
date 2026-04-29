"""
Risk Manager — institutional-grade position limits, circuit breakers, and exposure tracking.

Three components:
  1. CircuitBreaker: halts trading when losses exceed thresholds
  2. PositionSizer: enforces position limits and vol-adaptive sizing
  3. ExposureTracker: tracks open positions and prevents over-concentration

Usage:
    from risk_manager import RiskManager, RiskConfig

    # In backtest
    rm = RiskManager(RiskConfig())
    positions = rm.apply_limits(raw_positions, df)

    # Check status
    rm.status()          # current state
    rm.is_halted()       # circuit breaker check
    rm.reset()           # manual reset after halt
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import config


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RiskConfig:
    """All risk parameters with conservative defaults.

    Calibrated to vol_sized strategy profile (Sharpe 1.60, MaxDD -18.4%):
    - 20% drawdown cap allows the system to ride through normal corrections
    - 5% daily loss is 2x the typical daily swing
    - 20 bar cooldown = 20 trading days (1 month)
    """
    # Position limits
    max_position_pct: float = 0.10       # max 10% of capital per position
    max_total_exposure_pct: float = 0.30  # max 30% total exposure
    max_concurrent_positions: int = 3     # max 3 positions at once

    # Circuit breakers
    max_daily_loss_pct: float = 0.05     # halt if down 5% in a day
    max_drawdown_pct: float = 0.20       # halt if down 20% from peak
    cooldown_bars: int = 20              # stay out 20 bars after halt
    drawdown_auto_reset: bool = True     # auto-reset after cooldown (vs manual)

    # Position sizing
    use_atr_sizing: bool = True          # inverse ATR sizing
    atr_lookback: int = 60              # rolling median window for ATR
    min_size: float = 0.3               # minimum position size (30%)
    max_size: float = 1.0               # maximum position size (100%)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """
    Monitors P&L and halts trading when losses exceed thresholds.

    Two triggers:
      1. Daily loss > max_daily_loss_pct → halt for cooldown_bars
      2. Drawdown from peak > max_drawdown_pct → halt until manual reset

    State is tracked per-bar in vectorized mode (backtest) or per-cycle in live mode.
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        self._halted = False
        self._halt_reason = ""
        self._halt_bar = -1
        self._equity_peak = 0.0
        self._daily_start_equity = 0.0
        self._halt_log = []

    def check(self, bar_idx: int, equity: float, daily_start_equity: float = None) -> bool:
        """
        Check if circuit breaker should fire.
        Returns True if trading should be HALTED.
        """
        if daily_start_equity is not None:
            self._daily_start_equity = daily_start_equity

        # Track equity peak
        if equity > self._equity_peak:
            self._equity_peak = equity

        # Already halted — check cooldown
        if self._halted:
            bars_since_halt = bar_idx - self._halt_bar
            if self._halt_reason == "max_drawdown" and not self.config.drawdown_auto_reset:
                return True  # manual reset required
            elif bars_since_halt >= self.config.cooldown_bars:
                self._halted = False
                self._halt_reason = ""
                self._equity_peak = equity  # reset peak after cooldown to prevent re-trigger
                return False
            return True

        # Check daily loss
        if self._daily_start_equity > 0:
            daily_loss = (equity / self._daily_start_equity) - 1
            if daily_loss < -self.config.max_daily_loss_pct:
                self._halt("max_daily_loss", bar_idx, equity,
                           f"Daily loss {daily_loss:.2%} exceeds -{self.config.max_daily_loss_pct:.1%}")
                return True

        # Check drawdown from peak
        if self._equity_peak > 0:
            drawdown = (equity / self._equity_peak) - 1
            if drawdown < -self.config.max_drawdown_pct:
                self._halt("max_drawdown", bar_idx, equity,
                           f"Drawdown {drawdown:.2%} exceeds -{self.config.max_drawdown_pct:.1%}")
                return True

        return False

    def _halt(self, reason: str, bar_idx: int, equity: float, message: str):
        self._halted = True
        self._halt_reason = reason
        self._halt_bar = bar_idx
        self._halt_log.append({
            "bar": bar_idx,
            "reason": reason,
            "equity": round(equity, 2),
            "message": message,
            "timestamp": datetime.now().isoformat(),
        })

    def is_halted(self) -> bool:
        return self._halted

    def halt_reason(self) -> str:
        return self._halt_reason

    def reset(self):
        """Manual reset after max_drawdown halt."""
        self._halted = False
        self._halt_reason = ""

    @property
    def halt_log(self) -> list:
        return self._halt_log


# ---------------------------------------------------------------------------
# Position Sizer
# ---------------------------------------------------------------------------

class PositionSizer:
    """
    Enforces position size limits and applies vol-adaptive sizing.

    Extracted from VolSizedStrategy for reuse across all strategies.
    Applies three layers:
      1. ATR-based sizing (reduce in high vol)
      2. Capital-based cap (max 10% per position)
      3. Drawdown brake (reduce as drawdown deepens)
    """

    def __init__(self, config: RiskConfig):
        self.config = config

    def size(self, raw_position: float, atr_pct: float = None,
             atr_median: float = None, current_drawdown: float = 0.0) -> float:
        """
        Compute final position size from raw signal strength.

        Args:
            raw_position: strategy output (0.0 to 1.0, or -1.0 to 0.0)
            atr_pct: current ATR as % of price
            atr_median: rolling median ATR for normalization
            current_drawdown: current drawdown from peak (negative number)

        Returns:
            Adjusted position size (capped, vol-scaled, drawdown-braked)
        """
        if raw_position == 0:
            return 0.0

        direction = np.sign(raw_position)
        size = abs(raw_position)

        # Layer 1: ATR-based sizing (inverse volatility)
        if self.config.use_atr_sizing and atr_pct is not None and atr_median is not None:
            if atr_pct > 0:
                vol_scale = atr_median / atr_pct
                size *= np.clip(vol_scale, self.config.min_size, self.config.max_size)

        # Layer 2: Capital-based cap
        size = min(size, self.config.max_position_pct / 0.10)  # normalize to 10% = 1.0

        # Layer 3: Drawdown-responsive deleveraging (stepped schedule)
        # At deeper drawdowns, progressively reduce position size
        dd_abs = abs(current_drawdown)
        dd_scale = 1.0
        for dd_thresh, dd_size in config.RISK_DD_SCHEDULE:
            if dd_abs >= dd_thresh:
                dd_scale = dd_size
        size *= dd_scale

        return direction * np.clip(abs(size), 0.0, self.config.max_size)

    def size_series(self, positions: pd.Series, df: pd.DataFrame,
                    equity: pd.Series = None) -> pd.Series:
        """
        Apply sizing to an entire position series (for backtesting).
        """
        result = positions.copy()

        # ATR columns
        atr_pct = df.get("atr_pct", pd.Series(np.nan, index=df.index))
        atr_med = atr_pct.rolling(self.config.atr_lookback).median()

        # Drawdown from equity
        if equity is not None:
            peak = equity.cummax()
            drawdown = (equity - peak) / peak
        else:
            drawdown = pd.Series(0.0, index=df.index)

        for i in range(len(result)):
            if result.iloc[i] != 0:
                result.iloc[i] = self.size(
                    result.iloc[i],
                    atr_pct.iloc[i] if not np.isnan(atr_pct.iloc[i]) else None,
                    atr_med.iloc[i] if not np.isnan(atr_med.iloc[i]) else None,
                    drawdown.iloc[i] if equity is not None else 0.0,
                )

        return result


# ---------------------------------------------------------------------------
# Exposure Tracker
# ---------------------------------------------------------------------------

class ExposureTracker:
    """
    Tracks open positions and enforces exposure limits.

    In vectorized backtest mode, this operates on the position series.
    In live mode, it tracks actual positions from the execution engine.
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        self._positions = {}  # signal_name -> position_size

    def can_open(self, signal_name: str, position_size: float) -> bool:
        """Check if opening this position would violate limits."""
        # Max concurrent positions
        active = sum(1 for v in self._positions.values() if v != 0)
        if active >= self.config.max_concurrent_positions and signal_name not in self._positions:
            return False

        # Max total exposure
        current_exposure = sum(abs(v) for v in self._positions.values())
        new_exposure = current_exposure + abs(position_size)
        if new_exposure > self.config.max_total_exposure_pct:
            return False

        return True

    def update(self, signal_name: str, position_size: float):
        """Update tracked position."""
        if position_size == 0:
            self._positions.pop(signal_name, None)
        else:
            self._positions[signal_name] = position_size

    def total_exposure(self) -> float:
        return sum(abs(v) for v in self._positions.values())

    def active_count(self) -> int:
        return sum(1 for v in self._positions.values() if v != 0)

    def status(self) -> dict:
        return {
            "positions": dict(self._positions),
            "total_exposure": self.total_exposure(),
            "active_count": self.active_count(),
            "max_allowed": self.config.max_concurrent_positions,
        }


# ---------------------------------------------------------------------------
# Risk Manager (unified interface)
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Unified risk management interface.

    Combines circuit breaker, position sizer, and exposure tracker
    into a single object that can be passed to the backtest engine.
    """

    def __init__(self, config: RiskConfig = None):
        self.config = config or RiskConfig()
        self.circuit_breaker = CircuitBreaker(self.config)
        self.position_sizer = PositionSizer(self.config)
        self.exposure_tracker = ExposureTracker(self.config)
        self._actions_log = []

    def apply_limits(self, positions: pd.Series, df: pd.DataFrame,
                     initial_capital: float = None) -> pd.Series:
        """
        Apply all risk limits to a position series (for backtesting).

        This is the main integration point with backtest_engine.run_backtest().
        Called after strategy generates positions but before P&L is computed.
        """
        initial_capital = initial_capital or config.INITIAL_CAPITAL
        result = positions.copy().astype(float)

        # Compute a preliminary equity curve for circuit breaker checks
        fwd_return = df["btc_return"].shift(-1).fillna(0)
        equity = initial_capital
        equity_peak = initial_capital
        daily_start = initial_capital
        last_date = None

        # Precompute ATR median once (was O(n^2) when inside loop)
        _atr_med_series = df["atr_pct"].rolling(self.config.atr_lookback).median() if "atr_pct" in df.columns else None

        for i in range(len(result)):
            # Track equity
            if i > 0:
                ret = result.iloc[i - 1] * fwd_return.iloc[i - 1]
                equity *= (1 + ret)

            # New day check (for daily loss tracking)
            current_date = df["trade_date"].iloc[i].date() if hasattr(df["trade_date"].iloc[i], 'date') else None
            if current_date != last_date:
                daily_start = equity
                last_date = current_date

            # Circuit breaker check
            if self.circuit_breaker.check(i, equity, daily_start):
                result.iloc[i] = 0
                continue

            # Position sizing
            if result.iloc[i] != 0:
                atr_pct = df["atr_pct"].iloc[i] if "atr_pct" in df.columns else None
                atr_med = _atr_med_series.iloc[i] if "atr_pct" in df.columns else None
                dd = (equity / equity_peak - 1) if equity_peak > 0 else 0

                result.iloc[i] = self.position_sizer.size(
                    result.iloc[i],
                    atr_pct if atr_pct is not None and not np.isnan(atr_pct) else None,
                    atr_med if atr_med is not None and not np.isnan(atr_med) else None,
                    dd,
                )

            # Update equity peak
            if equity > equity_peak:
                equity_peak = equity

        return result

    def is_halted(self) -> bool:
        return self.circuit_breaker.is_halted()

    def reset(self):
        """Reset circuit breaker (manual action after max drawdown halt)."""
        self.circuit_breaker.reset()

    def status(self) -> dict:
        return {
            "halted": self.is_halted(),
            "halt_reason": self.circuit_breaker.halt_reason(),
            "halt_count": len(self.circuit_breaker.halt_log),
            "exposure": self.exposure_tracker.status(),
            "config": asdict(self.config),
        }

    def save_log(self, path: str = None):
        """Save halt log and status to JSON."""
        path = path or str(config.PROJECT_DIR / "tracker" / "risk_log.json")
        log = {
            "status": self.status(),
            "halt_events": self.circuit_breaker.halt_log,
            "saved_at": datetime.now().isoformat(),
        }
        Path(path).parent.mkdir(exist_ok=True)
        Path(path).write_text(json.dumps(log, indent=2, default=str))
