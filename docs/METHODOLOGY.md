# Methodology

How this framework was built and validated. Each section corresponds to a
real bug-and-fix or a real design decision. Where dates are referenced,
those are git-verifiable in the audit archive.

## 1. Walk-forward validation, with a real warmup bug

The framework uses a sliding 180-day train / 30-day test window. The naive
implementation is two lines:

```python
slice = df.iloc[i + train_days : i + train_days + test_days]
result = run_backtest(slice, strategy)
```

This is wrong. Rolling indicators (50-day MA, 14-day ATR, signal
windows up to 60 bars) produce NaN for the first `window_size` bars of any
slice. With test_days=30 and indicators using 60-bar windows, the *entire*
30-day test window has NaN signals. The strategy generates 0 trades. Every
walk-forward window reports 0 Sharpe.

This bug shipped silently for several weeks. It was caught because the
walk-forward results showed identical zeros across all strategies — a
clue that the harness itself, not the strategies, was returning nothing.

The fix:

```python
# Pass the full warmup+test window, but slice metrics to test-only after.
full_slice = df.iloc[i : i + train_days + test_days]
result = run_backtest(full_slice, strategy)

trades = result["trades"]
trades = trades[trades["entry_date"] >= test_window_start]
test_returns = result["returns"].iloc[test_start_local:]
```

The warmup window gives indicators their history; metrics are scored on the
test window only. After the fix, OOS Sharpe came in at 1.89 (vs 2.35 IS) —
a healthy 0.46 IS-to-OOS gap, neither too small (overfit signal) nor too
large (regime-dependent edge).

**Lesson**: in a walk-forward harness, the slice you compute on and the
slice you score on are different objects. Conflating them is silent.

## 2. Macro data lookahead — the +1 month shift

FRED publishes monthly economic data (CPI, Fed Funds, unemployment) dated
to the *reference month*, not the *release date*. The April CPI is dated
`2026-04-01` even though it isn't published until mid-May. A backtest that
joins FRED data on bar date will see future information.

The fix:

```python
regime[date_col] = regime[date_col] + pd.DateOffset(months=1)
df = pd.merge_asof(df, regime, on=date_col, direction="backward")
```

Shift FRED dates forward by one month before the merge. Then `merge_asof
direction="backward"` only joins to bars on or after the shifted date. A
CPI value dated 2026-04-01 (referencing April) gets shifted to 2026-05-01
and only joins bars from May onward. Conservative — sometimes you'd see
the data slightly earlier than the +1 month shift assumes — but never
later.

**Verification**: an empirical test in [tests/test_lookahead.py](../tests/test_lookahead.py)
samples 4 dates across 2024-2026 and asserts each bar's regime value
matches the FRED data from ≥1 month earlier.

## 3. Allocator constraints — the floor-cap interaction

The dynamic momentum allocator distributes capital across N pairs based on
their trailing-20d return. To prevent the allocator from concentrating
100% in one pair, it has a floor (minimum weight per pair) and a cap
(maximum). Reasonable values for 8 pairs: floor 0.12, cap 0.55.

When the framework expanded from 8 to 15 pairs, those values stopped
working. With floor=0.12 across 15 pairs:

```
15 * 0.12 = 1.80   (180% nominal weight)
```

The allocator's normalization step divides by the sum, so all 15 pairs end
up at exactly `0.12 / 1.80 = 0.0667` — uniform equal weight regardless of
momentum signal. The floor binds before the cap, the renormalization
flattens the signal, and the allocator silently degrades to equal-weight.

**Detection**: the dashboard's Allocation Weights tile showed all pairs at
6.67% on every refresh. A grid sweep confirmed the issue and produced new
values: floor 0.02, cap 0.30. With those constraints, the allocator
correctly concentrated 30% on the trending pair while keeping 2% on each
quiet pair.

**Lesson**: constraint values are not parameters; they are design
decisions tied to N. When N changes, the constraint surface needs
re-validation against a grid.

## 4. Kill switch — the backfill inflation problem

The kill switch evaluates 14-day rolling P&L. If P&L drops below -1.5%,
throttle to 0.5x sizing; below -3%, halt entirely.

After a bug-fix that retroactively backfilled some missing trade entries,
the kill switch reported 14d P&L of +5.89% — implying the system was
healthy. But live performance had been -1.1%. The 7-percentage-point gap
came from $<redacted> of backfilled trades that were retroactive
reconstructions, not real-time strategy decisions. They were inflating the
metric.

The fix splits "what the metric is measuring" cleanly:

```python
def _load_closed_trades(exclude_backfilled=True):
    trades = json.loads(path.read_text())
    if exclude_backfilled:
        trades = [t for t in trades if not t.get("backfill")]
    return trades
```

Plus a dual-view drawdown:

```python
pnl_14d_pct          # exits in window (realized)
pnl_14d_entries_pct  # entries in window (recent decisions only)
worst_view = min(pnl_14d_pct, pnl_14d_entries_pct)
```

Tripping on EITHER view increases sensitivity without false halts. A
recent loss that hasn't exited yet shows in `pnl_14d_entries_pct`; an old
loss that just exited shows in `pnl_14d_pct`. The kill switch fires on the
worse of the two.

**Lesson**: when a metric and the system that produces the metric drift
apart, the metric is wrong.

## 5. Atomic writes — the durability story

The framework's tracker state lives in JSON files: `paper_trades.json`,
`predictions.json`, `kill_switch_state.json`, `allocation_weights.json`,
`changelog.json`. Multiple processes write to these: a daily runner at
7am, an afternoon runner at 3pm, an hourly system check, plus the
dashboard process. The naive write pattern:

```python
path.write_text(json.dumps(data, indent=2))
```

This is not atomic. `write_text` truncates the file to zero, then writes
the new content. If the process crashes during the write, or two
processes write concurrently, the file is left empty or with mixed
content. The next reader sees `JSONDecodeError` — and may interpret it
as "no trades", silently losing state.

The fix:

```python
def atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, json.dumps(data).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)  # atomic on POSIX
```

Plus a context-manager file lock for read-modify-write loops:

```python
@contextmanager
def file_lock(path, timeout=30.0):
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        # Block-acquire with timeout
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(...)
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
```

A 20-thread concurrent-counter test in [tests/test_safe_io.py](../tests/test_safe_io.py)
confirms zero lost updates across 20 simultaneous load → mutate → save
cycles.

**Lesson**: paper trading tolerates corrupted state — you can rebuild
from logs. Live trading does not. Atomic writes are pre-deployment
infrastructure, not a future polish item.

## 6. Trade lifecycle — exit precedence

When multiple exit conditions trigger on the same bar, the order matters.
The framework enforces:

```
stop_loss > max_loss_circuit > time_exit/fir_exit > signal_flip
```

A stop-loss is the strongest signal (price has moved against you past a
predetermined threshold), so it executes first regardless of any
signal-direction flip on the same bar. The max-loss circuit is a softer
backstop for pairs without an ATR stop. Time-exit fires on the holding
window expiration. Signal-flip is the weakest — closing because a new
signal arrived in the opposite direction — and is gated by a hold-lock so
the framework can't whipsaw out of a position before its forward window
even completes.

The implementation is one long if-chain in `PaperTrader.execute_signal`,
each block ending in `break` after handling one trade. The key
correctness check: the schema validator allows `max_loss_circuit` as a
valid `exit_reason`. A schema enum that doesn't know about a real exit
type would emit warnings on every legitimate fire and be ignored, hiding
real schema violations.

**Verification**: [tests/test_trade_lifecycle.py](../tests/test_trade_lifecycle.py)
has 6 isolated tests, each forcing two exit conditions to compete on the
same call and asserting the precedence holds.

## 7. Per-combo live promotion

Standard tier systems gate by pair: BTC tier promoted from paper to micro
to small to full. This is too coarse. A pair's daily strategy might use
broken signals while its 4H strategy uses healthy ones — the tier system
has no language for that distinction.

The framework's hybrid live model gates by `(signal, pair, bar)` triple:

```python
LIVE_COMBOS = {
    ("routing_shift_signal", "BTC", "4H"): "live",
    ("routing_shift_signal", "ETH", "4H"): "live",
    # everything else: "paper" by default
}
LIVE_COMBO_SIZE_CAP_USD = {
    "live": 200.0,
    "shadow": 5.0,
    "paper": None,
}
```

Per-combo kill switches, per-combo size caps, per-combo promotion
criteria. Promotion requires:
1. ≥10 forward-tracked trades
2. ≥55% hit rate
3. 14-day combo P&L > -1.5% of allocated capital
4. Combo kill switch healthy

Demotion is automatic on any failure. Promotion is manual. The dashboard
surfaces "ready to promote" but never auto-promotes.

This is the design — the implementation is intentionally not in this
public repo because the live edition has additional platform-specific gates.
The design doc [HYBRID_LIVE_DESIGN.md](HYBRID_LIVE_DESIGN.md) is the
artifact.

**Lesson**: deployment readiness is not binary. The right granularity for
gating is the smallest unit you can independently validate.

## 8. The audit pattern itself

The biggest discipline artifact in this repo is the audit history. Every
fix has:
- Category (`fix`, `feature`, `optimization`, `audit`, `refactor`)
- Files changed
- Metrics before
- Metrics after
- Tags (linking to the audit finding ID)

166 entries over ~6 weeks of active development. The pattern: an audit
finds something, the next fix lands within 24 hours, the changelog entry
quantifies the impact. Examples from the public-sanitized archive:

- "Walk-forward harness: 0 trades / NaN bug" → fix → "1290 OOS days, OOS
  Sharpe 1.89"
- "Allocator floor 0.12 forces equal weight at 15 pairs" → grid sweep →
  "0.02 floor, 0.30 cap, Sharpe +0.130 vs prior"
- "Kill switch reports +5.89% vs actual -1.1%" → backfill exclusion →
  "metric matches reality, dual-view drawdown shipped"

What this demonstrates: every metric I report has a paper trail. If I
claim Sharpe 1.89 OOS, you can find the commit that produced it, the test
that validates it, and the audit entry that documents the methodology.
That's the actual artifact.
