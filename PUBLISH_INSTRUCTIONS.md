# How to publish this as a public GitHub repo

This directory is the public, employer-facing edition of the trading
framework. Every file here has been sanitized — customer names, account
IDs, internal table refs, and proprietary signal names are redacted or
replaced with public-data analogs.

## Pre-publish checklist (run before pushing)

```bash
# 1. Re-sanitize the changelog so CHANGELOG_PUBLIC.md is current
python3 ../scripts/sanitize_changelog.py

# 2. Re-run the portability audit and confirm src/ files are PORTABLE
#    (the report is written to ../docs/ in the internal repo, not public)
python3 ../scripts/portability_audit.py

# 3. Run the public test suite
python3 tests/run.py
# Expected: 18 tests, all passing

# 4. Run the public forward tracker end-to-end on the public dataset
python3 src/build_public_dataset.py --lookback 365
python3 src/public_forward_tracker.py --report --score --digest
# Expected: predictions logged, scoreboard populated

# 5. Re-run the comprehensive scrub (idempotent — safe to re-run)
python3 ../scripts/scrub_public_dir.py
# Expected: minimal output. Zero modifications means no leaks remain.
```

## Publishing steps

```bash
# 1. Create a new empty repo on GitHub (e.g. signals-research,
#    quant-trading-framework, etc.). Don't initialize with README.

# 2. From this directory, init a fresh git repo and push:
cd piper_public
git init
git add .
git commit -m "Initial public release of quant trading framework

Multi-asset crypto trading framework with rigorous validation:
walk-forward harness, dynamic momentum allocator, kill switch with
backfill exclusion, atomic-write durability layer, and a forward
tracker that scores predictions daily.

Public-data signals only. Proprietary inputs (exchange-internal flow
data, customer-segmented fills) stripped per /docs/METHODOLOGY.md.
"

git remote add origin git@github.com:YOUR_USERNAME/REPO_NAME.git
git branch -M main
git push -u origin main
```

## Daily forward-tracker maintenance (the proof-building step)

The forward tracker only matters if it runs DAILY and commits to the
public repo regularly. Set up a cron entry so this is automatic:

```bash
# Edit your crontab (or use macOS launchd)
crontab -e

# Add: every day at 5pm ET, refresh data, log + score, push to git
0 17 * * * cd /path/to/REPO_NAME && \
    python3 src/build_public_dataset.py --lookback 60 && \
    python3 src/public_forward_tracker.py --report --score && \
    git add tracker/public/predictions.json && \
    git commit -m "auto: forward-tracker $(date -u +%Y-%m-%d)" --no-verify && \
    git push
```

After 60 days of this running, the repo's git log shows ~60 commits each
landing one prediction set. Each prediction's `logged_at` timestamp is in
the JSON; each commit's timestamp is in git. Anyone can verify the
predictions were logged BEFORE the outcomes were known.

## What to mention in a job application or LinkedIn

> "Built and operate a quant trading framework with rigorous forward
> validation. The public edition at github.com/YOUR_USERNAME/REPO is the
> sanitized subset — same backtest harness, allocator, kill switch, test
> suite as the production version, with proprietary signal sources
> stripped. The /tracker/public/predictions.json file is a forward-only
> log: each entry's logged_at timestamp predates the 10-day forward
> outcome it scores against, and is git-verifiable. /audits/ contains the
> redacted history of 169 production fixes. /docs/METHODOLOGY.md walks
> through 8 design decisions and the bugs that produced them."

The artifact is the discipline. A 1.5 Sharpe with a forward log + audit
history + test suite + methodology doc beats a 3.0 Sharpe with no
provenance.

## What to NOT do

- Do NOT push the `cache/` directory if it contains anything from the
  internal data builder. Only the public dataset (~12 columns, no
  segments) is safe.
- Do NOT mention employer name in commit messages or README.
- Do NOT cross-link to internal tools (the internal Piper repo, Snowflake
  tables, etc.).
- Do NOT include real production P&L. Only the public-tracker outcomes —
  derived from public price data — should ever be in the public repo.
