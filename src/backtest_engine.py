"""
Backtesting Engine — vectorized backtest using pandas.
No event loop; all operations are column-wise for speed and clarity.
"""

import numpy as np
import pandas as pd

import config


def _apply_hold_period(positions, hold_days, hold_days_short=None):
    """
    Once a position is entered, hold it for hold_days regardless of new signals.
    This reduces turnover and makes strategies more realistic.

    If hold_days_short is provided, short positions (-1) use that hold period
    instead of hold_days. This supports the 10d long / 5d short split.
    """
    result = positions.copy()
    hold_until = -1
    active_hold = hold_days  # track which hold period is active

    for i in range(len(result)):
        if i <= hold_until:
            # Still holding — keep the position that was entered
            result.iloc[i] = result.iloc[hold_until - active_hold + 1]
        elif result.iloc[i] != 0:
            # New position entered — pick hold period based on direction
            if hold_days_short is not None and result.iloc[i] < 0:
                active_hold = hold_days_short
            else:
                active_hold = hold_days
            hold_until = i + active_hold - 1

    return result


def _apply_exits(positions, df, stop_atr_mult=2.0, tp_atr_mult=3.0):
    """
    Apply ATR-based stop-loss and take-profit exits.

    Once a position is entered, track entry price. Exit if:
      - Price moves against by stop_atr_mult * ATR (stop-loss)
      - Price moves in favor by tp_atr_mult * ATR (take-profit)

    Returns modified positions series. Backward compatible: returns
    unchanged positions if both multipliers are None.
    """
    if stop_atr_mult is None and tp_atr_mult is None:
        return positions

    result = positions.copy()
    has_atr = "atr_14" in df.columns

    entry_price = None
    entry_atr = None
    entry_direction = 0

    for i in range(len(result)):
        pos = result.iloc[i]
        price = df["btc_close"].iloc[i]
        atr = df["atr_14"].iloc[i] if has_atr else np.nan

        # Skip bars with NaN price — can happen from outer merge gaps.
        # Don't enter or evaluate exits on missing data.
        if np.isnan(price):
            result.iloc[i] = 0
            continue

        if entry_price is None and pos != 0:
            # New position — record entry
            entry_price = price
            entry_atr = atr if not np.isnan(atr) else price * 0.03
            entry_direction = pos

        elif entry_price is not None:
            # Check exits before anything else
            pnl = (price - entry_price) * entry_direction

            # Stop-loss
            if stop_atr_mult is not None and pnl < -(stop_atr_mult * entry_atr):
                result.iloc[i] = 0
                entry_price = None
                entry_direction = 0
                continue

            # Take-profit
            if tp_atr_mult is not None and pnl > (tp_atr_mult * entry_atr):
                result.iloc[i] = 0
                entry_price = None
                entry_direction = 0
                continue

            # Signal changed — reset entry tracking
            if pos != entry_direction:
                if pos != 0:
                    entry_price = price
                    entry_atr = atr if not np.isnan(atr) else price * 0.03
                    entry_direction = pos
                else:
                    entry_price = None
                    entry_direction = 0

    return result


def _extract_trades(df, positions, fwd_return, fee_bps, slippage_bps):
    """
    Extract individual trades from position series.
    A trade starts when position changes from 0 to +1/-1,
    and ends when position returns to 0 or reverses.
    """
    trades = []
    entry_idx = None
    entry_direction = 0
    cum_return = 0.0
    # Round-trip cost: entry + exit
    round_trip_cost = 2 * (fee_bps + slippage_bps) / 10000

    for i in range(len(positions)):
        pos = positions.iloc[i]

        if entry_idx is None and pos != 0:
            # Enter trade
            entry_idx = i
            entry_direction = pos
            cum_return = 0.0

        elif entry_idx is not None:
            # Accumulate return using fwd_return at the HELD bar
            # fwd_return[i] = return from close_i to close_{i+1}
            # On bar i, we held through bar i-1, so we earned fwd_return[i-1]
            ret = fwd_return.iloc[i - 1] if i > 0 else 0
            if not np.isnan(ret):
                cum_return += entry_direction * ret

            if pos != entry_direction:
                # Exit trade (position changed or went flat)
                trades.append({
                    "entry_date": df["trade_date"].iloc[entry_idx],
                    "exit_date": df["trade_date"].iloc[i],
                    "direction": "long" if entry_direction > 0 else "short",
                    "hold_days": i - entry_idx,
                    "gross_return": cum_return,
                    "net_return": cum_return - round_trip_cost,
                })

                if pos != 0:
                    # Immediately enter new position
                    entry_idx = i
                    entry_direction = pos
                    cum_return = 0.0
                else:
                    entry_idx = None
                    entry_direction = 0

    # Close any open trade at end of data
    if entry_idx is not None:
        trades.append({
            "entry_date": df["trade_date"].iloc[entry_idx],
            "exit_date": df["trade_date"].iloc[-1],
            "direction": "long" if entry_direction > 0 else "short",
            "hold_days": len(positions) - entry_idx,
            "gross_return": cum_return,
            "net_return": cum_return - round_trip_cost,
        })

    return pd.DataFrame(trades) if trades else pd.DataFrame(
        columns=["entry_date", "exit_date", "direction", "hold_days", "gross_return", "net_return"]
    )


def run_backtest(df, strategy, fee_bps=None, slippage_bps=None,
                 initial_capital=None, hold_days=1, hold_days_short=None,
                 execution_lag=1,
                 stop_atr_mult=None, tp_atr_mult=None, risk_manager=None):
    """
    Run a vectorized backtest with explicit execution lag to prevent lookahead bias.

    Timeline (with execution_lag=1, the default):
        Day T close: signal computed from day T data (and prior)
        Day T+1 open: trade executed (position enters)
        Day T+1 close → Day T+2 close: position earns return

    This means:
        - Signal on day T → position active on day T+1 → earns return from T+1 to T+2
        - The signal NEVER uses data it couldn't have at market close on day T

    Parameters:
        df: master dataset with signals computed (must have trade_date, btc_return)
        strategy: object with generate_positions(df) → Series of +1/0/-1
        fee_bps: round-trip trading fee in basis points
        slippage_bps: estimated slippage per trade
        initial_capital: starting USD capital
        hold_days: days to hold each position (1 = daily rebalance)
        execution_lag: days between signal and execution (default 1 = next day)

    Returns dict:
        equity_curve: pd.Series (daily portfolio value)
        positions: pd.Series (daily position: +1/0/-1)
        returns: pd.Series (daily strategy returns)
        trades: pd.DataFrame (individual trade log)
        metrics: dict (computed by analysis.compute_metrics)
    """
    fee_bps = fee_bps if fee_bps is not None else config.TRADING_FEE_BPS
    slippage_bps = slippage_bps if slippage_bps is not None else config.SLIPPAGE_BPS
    initial_capital = initial_capital or config.INITIAL_CAPITAL

    # Generate raw positions from strategy (based on day-T data)
    raw_positions = strategy.generate_positions(df)

    # CRITICAL: Apply execution lag — shift positions forward so signal on T
    # becomes active position on T+execution_lag. This prevents lookahead bias.
    # Use float to support fractional position sizing (e.g., 0.33 for 1/3 signal)
    positions = raw_positions.shift(execution_lag).fillna(0).astype(float)

    # Apply hold period if > 1 day
    if hold_days > 1:
        positions = _apply_hold_period(positions, hold_days, hold_days_short)

    # Apply stop-loss / take-profit exits (if configured)
    _stop = stop_atr_mult if stop_atr_mult is not None else getattr(strategy, 'stop_atr_mult', None)
    _tp = tp_atr_mult if tp_atr_mult is not None else getattr(strategy, 'tp_atr_mult', None)
    if _stop is not None or _tp is not None:
        positions = _apply_exits(positions, df, _stop, _tp)

    # Apply risk manager limits (circuit breaker, position sizing, exposure)
    if risk_manager is not None:
        positions = risk_manager.apply_limits(positions, df, initial_capital)

    # Forward return: what you earn by holding from today's close to tomorrow's close
    fwd_return = df["btc_return"].shift(-1).fillna(0)

    # Drawdown-responsive deleveraging: scale NEW entries based on equity DD
    # Only applied at entry time (not during holds) to avoid churning costs
    dd_schedule = config.RISK_DD_SCHEDULE
    if dd_schedule:
        cost_rate = (fee_bps + slippage_bps) / 10000
        equity_val = initial_capital
        peak = initial_capital
        scaled_pos = positions.copy()
        active_scale = 1.0  # scale locked at entry, held until exit

        for i in range(len(positions)):
            if i > 0:
                pos_chg = abs(scaled_pos.iloc[i] - scaled_pos.iloc[i - 1])
                ret = scaled_pos.iloc[i - 1] * fwd_return.iloc[i - 1] - pos_chg * cost_rate
                equity_val *= (1 + ret)
            peak = max(peak, equity_val)
            dd_abs = (peak - equity_val) / peak if peak > 0 else 0

            # Compute DD scale
            dd_scale = 1.0
            for dd_thresh, dd_size in dd_schedule:
                if dd_abs >= dd_thresh:
                    dd_scale = dd_size

            # Only apply scale on new entries (position goes from 0 to non-zero)
            if positions.iloc[i] != 0:
                was_flat = (i == 0) or (positions.iloc[i - 1] == 0)
                if was_flat:
                    active_scale = dd_scale  # lock scale at entry
                scaled_pos.iloc[i] = positions.iloc[i] * active_scale

        positions = scaled_pos

    # Trade cost: incurred on every position change
    pos_change = positions.diff().abs().fillna(0)
    trade_cost = pos_change * (fee_bps + slippage_bps) / 10000

    # Diligence #2 (2026-04-20): short-side borrow / perp-funding drag.
    # Previously shorts earned full -1 × fwd_return with NO cost — overstated
    # short P&L. Shorts pay either spot-borrow fees (3-10bps/day typical) or
    # perp funding (positive in normal regime). Conservative default 3bps/day;
    # configurable via config.SHORT_BORROW_BPS_PER_DAY.
    short_borrow_bps = getattr(config, "SHORT_BORROW_BPS_PER_DAY", 3.0)
    short_borrow_daily = short_borrow_bps / 10000
    short_drag = positions.where(positions < 0, 0).abs() * short_borrow_daily

    # Strategy return = position × forward return − trade cost − short borrow
    strategy_return = (positions * fwd_return) - trade_cost - short_drag

    # Equity curve
    equity = initial_capital * (1 + strategy_return).cumprod()

    # Extract individual trades
    trades = _extract_trades(df, positions, fwd_return, fee_bps, slippage_bps)

    return {
        "equity_curve": equity,
        "positions": positions,
        "returns": strategy_return,
        "trades": trades,
        "strategy_name": strategy.name,
    }
