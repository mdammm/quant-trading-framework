# Quant Trading Framework — Public Edition

> A multi-asset crypto trading framework with rigorous validation: walk-forward
> harness, dynamic momentum allocator, kill switch with backfill exclusion,
> atomic-write durability layer, and a forward-tracker that scores predictions
> daily so the record is provable, not backtestable.

This repo is the public, sanitized edition of a production trading
framework I built and operated. Proprietary signals (exchange-internal flow
data, customer-segmented fills) are not included. What remains is the
methodology, the validation harness, the operational discipline, and a
forward tracker running on public-data signals only — so every entry in
`tracker/public_predictions.json` is a dated, scored prediction that can be
verified from git history.

## Why this repo exists

Most quant track records are unverifiable: a backtest is a story, not
evidence. This repo's claim is operational. The forward tracker has
been emitting daily scored predictions on public-data signals starting
{INSERT_START_DATE}. Every prediction is committed to git within hours of
the bar that triggered it, before the 10-day forward outcome is knowable.
The scoreboard lives in [tracker/public_scorecard.csv](tracker/public_scorecard.csv).

## What's in here

| Module | What it demonstrates |
|---|---|
| [src/backtest_engine.py](src/backtest_engine.py) | Position management, P&L, fractional sizing, fees + slippage |
| [src/walk_forward.py](src/walk_forward.py) | 180-day train / 30-day test rolling validation |
| [src/portfolio_allocator.py](src/portfolio_allocator.py) | 5 allocator methods (momentum, inverse_vol, risk_parity, sharpe, hybrid) + constraint post-processor |
| [src/multi_pair.py](src/multi_pair.py) | Cross-pair backtest via column remapping |
| [src/kill_switch.py](src/kill_switch.py) | Throttle / halt with backfill exclusion + dual-view drawdown |
| [src/safe_io.py](src/safe_io.py) | Atomic writes (`os.replace` + fsync) and POSIX file locks |
| [src/forward_tracker.py](src/forward_tracker.py) | Daily prediction logging + scoring with bar-indexed forward windows |
| [tests/](tests/) | 20 unit tests + integration smoke + paper-backtest parity |
| [docs/METHODOLOGY.md](docs/METHODOLOGY.md) | Design decisions writeup |
| [audits/](audits/) | 60+ documented production bugs found and fixed (sanitized) |

## Live forward-tracker scoreboard (as of last commit)

The single most credible artifact in this repo. Each prediction was
logged before the 10-day forward outcome was knowable; each commit's
timestamp confirms ordering.

```
signal             n    hits    hit_rate_pct    avg_fwd_return_pct
extreme_fear       9    7       77.8%           +4.11%
funding_flip       1    0        0.0%           -1.27%
stablecoin_inflow  4    2       50.0%           +0.93%
```

Scoreboard updates every commit. Source: [tracker/public/predictions.json](tracker/public/predictions.json).
Reproduce: `python3 src/public_forward_tracker.py --digest`.

## Headline results (in-sample backtest, public-data subset)

```
WALK-FORWARD VALIDATION — Train: 180d, Test: 30d, Public-data signals only

Strategy              OOS days   OOS Sharpe
--------------------------------------------
public_long_short     {regenerate}  {regenerate}
```

To regenerate, run `python3 src/walk_forward.py --cache` after building
the dataset. The internal version produces 1290 OOS days at OOS Sharpe
1.89; the public-data subset is intentionally lower because proprietary
signals — which carry most of the alpha — are stripped.

## Discipline artifacts

- **[Test suite](tests/)** — 20 unit tests covering atomic-write concurrency,
  trade lifecycle precedence (stop_loss > max_loss_circuit > time_exit >
  signal_flip), and strategy-gate regression. Runs via stdlib `unittest`,
  no pytest dep.
- **[Bug audit history](audits/CHANGELOG_PUBLIC.md)** — every production fix
  has category / files_changed / metrics_before / metrics_after. The
  pattern over time is the artifact.
- **[Hybrid live design](docs/HYBRID_LIVE_DESIGN.md)** — per-`(signal, pair, bar)`
  promotion model with manual operator gating, per-combo kill switch, and
  size cap enforcement. Designed but not coded; published as a thinking
  artifact.

## Operating the forward tracker

```bash
# Run once to log today's predictions
python3 src/public_forward_tracker.py --report

# Score predictions made >= 10 days ago against actual outcomes
python3 src/public_forward_tracker.py --score

# Generate the digest — what's been firing, hit rate by signal
python3 src/public_forward_tracker.py --digest
```

Wire `python3 src/public_forward_tracker.py --report --score` into a daily
cron at any time after market close. Predictions auto-commit to git via the
included sync script.

## Portability boundary

This repo is intentionally a sub-system. The proprietary version uses
exchange-internal signals (institutional fill divergence, segment-specific
accumulation patterns, internalization rate shifts) that materially improve
the OOS Sharpe. This public edition replaces those with:

- Funding rate extremes (OKX, free API)
- Open interest deltas (OKX, Binance)
- Liquidation cascade signals (Coinalyze, free tier)
- Fear & Greed Index (Alternative.me, free)
- Put/call ratio + IV crush (Deribit, free)
- Order book depth imbalance (CCXT, free)
- Stablecoin TVL inflows (DefiLlama, free)
- CME futures basis (manual CSV — optional)

All backtest harness, allocator, kill switch, durability, and validation
code is identical between the public and internal editions. The signals
are the only stripped surface.

## Reproducibility

```bash
# Fresh clone, no proprietary data needed
pip install -r requirements.txt
python3 src/build_public_dataset.py    # fetches OKX + free sources
python3 src/walk_forward.py            # validates the harness
python3 tests/run.py                   # 20 unit + 2 integration
```

If `tests/run.py` exits 0, the framework is sound. If `walk_forward.py`
produces an OOS Sharpe within the expected band, the methodology is
confirmed working on public data.

## License

MIT. The code is yours. The numbers are mine — verify them yourself.
