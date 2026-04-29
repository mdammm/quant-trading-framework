# Hybrid Live Deployment — Design Doc

Date: 2026-04-28
Status: design only, no code shipped

## The decision

Promote individual `(signal, pair, bar)` combos to live trading independently
instead of a single `EXECUTION_MODE = paper | live` flag for the whole system.
This lets the proven pieces (`routing_shift_signal` longs on 4H, 8 pairs)
deploy real capital while everything unverified stays in paper.

This is not a bigger system; it's a tighter perimeter on the existing system.

## Why a flag-per-combo, not a flag-per-tier

The existing tier system (`paper -> shadow -> micro -> small -> full`) gates by
pair only, on a global multiplier. It assumes "if BTC is ready, BTC's whole
strategy is ready." That fails because:
- BTC daily strategy uses dead prop signals (broken)
- BTC 4H strategy uses routing_shift_signal (working)
- Both share the BTC tier — promoting BTC promotes both books simultaneously

A combo-level flag lets BTC 4H go live while BTC daily stays paper.

## Combo identity

Each combo is the triple `(signal_name, pair, bar)`:
- `signal_name`: `routing_shift_signal`, `compression_breakout`, `microflow_signal_A`,
  `prop_fill_div_pct`, etc.
- `pair`: `BTC`, `ETH`, `SOL`, ...
- `bar`: `daily` or `4H`

For confluence trades (multiple signals fire on the same bar), the combo is
identified by the strongest signal in the firing set, by historical hit rate.

## Config schema

```python
# config.py

# Map (signal, pair, bar) -> live_state
# live_state in: "paper" | "shadow" | "live"
LIVE_COMBOS = {
    # 4H combos already validated forward (62% WR, +$<redacted>, n=8)
    ("routing_shift_signal", "BTC", "4H"): "live",
    ("routing_shift_signal", "ETH", "4H"): "live",
    ("routing_shift_signal", "SOL", "4H"): "live",
    ("routing_shift_signal", "BAT", "4H"): "live",
    ("routing_shift_signal", "BCH", "4H"): "live",
    ("routing_shift_signal", "ETC", "4H"): "live",
    ("routing_shift_signal", "LTC", "4H"): "live",
    ("routing_shift_signal", "XRP", "4H"): "live",

    # Default for everything else: paper.
}
LIVE_COMBO_DEFAULT = "paper"

# Per-combo size caps (USD per order). None = use ALLOCATOR weight.
LIVE_COMBO_SIZE_CAP_USD = {
    "live": 200.0,    # max $200 per order on any live combo
    "shadow": 5.0,    # SHADOW_USD_PER_ORDER already exists for this
    "paper": None,    # paper uses normal allocator weight on 100k pool
}
```

## Wiring

### `MultiPairPaperTrader.execute_signal` decision tree (execution.py)

Replace the single `self.mode == "paper"` check with:

```python
def _resolve_combo_state(self, signal_name, pair, bar):
    # signal_name may be a comma-list ("contrarian_signal_A, contrarian_signal_B")
    # — pick the first listed signal as canonical for combo lookup
    primary_sig = signal_name.split(",")[0].strip() if signal_name else None
    return config.LIVE_COMBOS.get(
        (primary_sig, pair, bar), config.LIVE_COMBO_DEFAULT
    )

def execute_signal(self, ...):
    state = self._resolve_combo_state(signal_name, pair, bar)
    cap_usd = config.LIVE_COMBO_SIZE_CAP_USD.get(state)

    # Existing paper path becomes the default.
    # New: if state == "live", route to venue with cap_usd ceiling.
    # New: if state == "shadow", route to venue with $5 cap (existing path).

    qty = self._size_qty(position_size, price, cap_usd)
    ...
```

### Per-combo kill switch (kill_switch.py)

Today the kill switch evaluates one global state. Combo-level evaluation:

```python
def evaluate_combo(signal, pair, bar):
    """Return per-combo state: healthy | throttled | halted."""
    trades = _load_closed_trades_combo(signal, pair, bar)
    # Same triggers as global: WR, drawdown, divergence — but scoped
    # to this combo's trade history only.
```

Persistence: `tracker/kill_switch_state_per_combo.json`, keyed on
`f"{signal}_{pair}_{bar}"`.

When ANY combo trips to `halted`, that combo's state in `LIVE_COMBOS` is
auto-flipped back to `paper`. A halted combo NEVER auto-promotes — manual
review required.

### Promotion criteria

A combo can promote `paper -> shadow -> live` when all of:
1. **Forward sample**: >= 10 closed live-track trades on this combo
2. **Hit rate**: >= 55% across those 10 trades
3. **Drawdown**: 14d combo P&L > -1.5% of allocated capital
4. **Health**: combo kill switch is `healthy` (not throttled)
5. **Parallel signal validation**: if combo uses a parallel-tracked signal
   (e.g. `prop_fill_div_pct`), that signal must have >= 30 historical
   forward-tracker observations with >= 60% hit rate

Promotion is manual, not automatic. The system surfaces "ready to promote"
on the dashboard; the operator clicks promote.

### Demotion criteria

A combo demotes `live -> paper` when ANY of:
1. Combo kill switch trips `halted` (auto)
2. WR drops below 40% on n>=10 (auto)
3. Operator-initiated (always allowed)

Demotion bypasses the safety period — instant.

### Per-combo size cap enforcement

`execution.py:execute_signal` computes qty from position_size and price.
Add a clamp:

```python
notional = qty * price
if cap_usd is not None and notional > cap_usd:
    qty = cap_usd / price
    log_clamped = True
```

This sits ABOVE existing per-pair allocator weight — a $200 cap means even
if BTC has 30% allocation on a $<redacted> pool ($<redacted>), the actual order is $200.

## Dashboard surface

New tab section: "Combo Live Status"

```
Combo                                 State      WR    n   14d P&L   Action
routing_shift_signal  BTC  4H        live       66%   1   +$<redacted>   [demote]
routing_shift_signal  ETH  4H        live       100%  1   +$<redacted>   [demote]
routing_shift_signal  SOL  4H        live       100%  1   +$956     [demote]
routing_shift_signal  BTC  daily     paper      —     0   —         [...]
prop_fill_div_pct      BTC  daily     paper      —     0   —         observe (no action)
contrarian_signal_A         BTC  daily     paper      —     0   —         [...]
```

Operator can click `promote` once criteria pass, `demote` to roll back. All
state changes audit-logged to `tracker/combo_state_history.json`.

## Files that change

| File | Change | Approx LOC |
|---|---|---|
| `config.py` | Add `LIVE_COMBOS`, `LIVE_COMBO_SIZE_CAP_USD`, defaults | ~20 |
| `execution.py` | `_resolve_combo_state`, size cap clamp, route to venue per state | ~40 |
| `execution_venue.py` | Already abstracts NullVenue / SFOXVenue / KrakenVenue — no change beyond verifying it's wired | ~5 |
| `kill_switch.py` | New `evaluate_combo` + per-combo state file | ~80 |
| `dashboard_pages/paper_trading.py` | Combo Live Status table + promote/demote actions | ~60 |
| `system_check.py` | New rule: detect any live combo whose kill switch flipped halted | ~15 |
| Tests: `scripts/test_hybrid_live.py` | Combo resolution + cap clamp + promotion flow | ~120 |
| Migration: `scripts/migrate_to_combos.py` | One-shot to back-fill combo identity onto historical trades | ~50 |

Total: ~390 LOC, plus tests. Real engineering — 2-3 days of focused work.

## Rollout sequence

**Day 0: foundation (no behavior change)**
- Add `LIVE_COMBOS` config (all entries default to `paper`)
- Wire `_resolve_combo_state` (returns `paper` for everything initially)
- Verify smoke + parity unchanged
- Commit

**Day 1: first live combo**
- Set `("routing_shift_signal", "BTC", "4H"): "live"`
- Verify kraken_futures venue config is correct
- Verify exposure_cap.py respects the $200 cap
- Run on a 1-day window with the cap forced low ($1) — confirm fills happen
- Restore $200 cap; let one entry fire on next signal

**Day 2-7: add the other 4H combos**
- One pair at a time. Watch for unexpected behavior. Demote anything that
  trips a kill switch.

**Day 7-14: validate forward**
- Combos accumulate live trades. Dashboard shows combo-level WR.
- Daily combos remain paper.

**Day 14+: promote daily combos**
- If `routing_shift_signal` daily on BTC/ETH/SOL has 10+ paper trades at
  >=55% WR, promote them to live.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Bug in combo routing sends a "paper" trade to live venue | Default state is `paper`; a misconfigured combo defaults to safest (paper). Test in `scripts/test_hybrid_live.py` |
| Kill switch trips per-combo correctly but operator doesn't notice | system_check.py adds a rule that fails on any halted live combo; alert into tracker/alerts.log |
| Combo definition collides (signal + pair + bar but two strategies) | Combo identity uses primary signal + pair + bar; one strategy per combo, enforced at config validation |
| Live combo flips back to paper, leaves an open live position orphaned | Demotion preserves open positions in their current state; only blocks NEW entries. Open position closes on its normal exit logic |
| Concurrent combo state writes from operator + auto-demote | safe_io file_lock around combo state file (already shipped) |

## Open questions

1. Confluence trades (multiple signals on same bar): which combo do they belong to?
   → Use first listed signal alphabetically OR by hit rate (need to decide)
2. What's the cap escalation path?
   → Suggest: $200 -> $<redacted> -> $<redacted> -> $<redacted> after each 30-day clean window
3. Should kill switch trip reset on combo state change?
   → No — kill switch history tracks the live combo identity; demote-then-repromote should re-acquire trust on n=10 fresh trades

## What this design does NOT do

- Does NOT change strategy code (signal sets stay the same)
- Does NOT change the existing tier system (still gates the multiplier)
- Does NOT touch backtest harness or walk-forward
- Does NOT add new signals (just routes existing ones differently)
- Does NOT promote anything live without explicit operator action
