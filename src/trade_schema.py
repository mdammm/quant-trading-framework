"""
Canonical schema for paper-trade records + validator.

Created 2026-04-19 after a field-name mismatch between daily trades
(`signal` string) and 4H backfill trades (`signals` list) caused the
Execution Quality dashboard to aggregate half the trades as "unknown".

Canonical format (all trades, daily + 4H):
    {
        "trade_id":        int or None,
        "entry_date":      ISO timestamp string (required)
        "entry_price":     float > 0 (required)
        "exit_date":       ISO string or None
        "exit_price":      float or None
        "direction":       "long" | "short" (required)
        "quantity":        signed float (required)   # +qty for long, -qty for short
        "signal":          comma-separated string    # CANONICAL (not `signals` list)
        "status":          "open" | "closed" (required)
        "pnl_usd":         float or None             # None if open
        "pnl_pct":         float or None             # P&L as % of pair_capital
        "fees_usd":        float or None
        "exit_reason":     "time_exit" | "stop_loss" | "signal_flip" | "fir_exit" |
                           "backfill_open_mtm" | "rotate_same_dir" | None
        "position_size_pct": float (default 100.0)
        "stop_loss_price": float or None
        "target_price":    float or None
        "backfill":        bool (default False)
        "backfill_reason": string or None
        "paper":           bool (default True)
        "source":          "daily" | "4H" (inferred from filename if missing)
    }

Validator behaviors:
  - strict=True   → raises ValidationError on any issue (for new writes)
  - strict=False  → auto-migrates legacy fields in place (for backward compat):
      * `signals` (list) → `signal` (comma-string)
      * `signal` missing → "" (empty)
      * missing status → inferred from exit_date
      * missing numeric fields → None

Usage:
    from trade_schema import validate_trade, migrate_trade, normalize_trades_file
    validate_trade(t, strict=True)   # raises on issue
    migrate_trade(t)                 # in-place legacy fixup, returns migrated dict
    normalize_trades_file(path)      # rewrites a JSON file with migrated trades
"""
import json
from pathlib import Path
from typing import Dict, List, Any, Optional


class TradeSchemaError(ValueError):
    """Raised when a trade record fails strict validation."""


REQUIRED_FIELDS = {"entry_date", "entry_price", "direction", "quantity", "status"}
VALID_DIRECTIONS = {"long", "short"}
VALID_STATUSES = {"open", "closed"}
VALID_EXIT_REASONS = {
    "time_exit", "stop_loss", "signal_flip", "fir_exit",
    "max_loss_circuit",  # added 2026-04-28: A12 per-trade max loss backstop
    "backfill_open_mtm", "rotate_same_dir", "manual", "open_mtm",
    None,
}


def _signals_to_string(sigs) -> str:
    """Convert signals list/string to canonical comma-string."""
    if sigs is None:
        return ""
    if isinstance(sigs, list):
        return ", ".join(str(s).strip() for s in sigs if str(s).strip())
    return str(sigs).strip()


def migrate_trade(t: Dict[str, Any]) -> Dict[str, Any]:
    """
    In-place migration of legacy trade records to canonical schema.
    Returns the migrated dict. Non-destructive for already-canonical trades.

    Specifically:
      - `signals` (list) → `signal` (comma-string)
      - missing `status` inferred from `exit_date` presence
      - ensures `direction` is lowercase
      - ensures `backfill` is bool (default False)
    """
    # 1. signals → signal canonicalization
    if "signals" in t:
        existing = t.get("signal") or ""
        fromlist = _signals_to_string(t.get("signals"))
        # If both present, prefer the canonical `signal` field if non-empty;
        # otherwise use the list field value
        if not existing and fromlist:
            t["signal"] = fromlist
        elif not existing:
            t["signal"] = ""
        # Remove the list field to prevent future divergence
        t.pop("signals", None)
    if "signal" not in t:
        t["signal"] = ""

    # 2. status inference
    if "status" not in t:
        t["status"] = "closed" if t.get("exit_date") else "open"

    # 3. direction lowercase
    if "direction" in t:
        t["direction"] = str(t["direction"]).lower()

    # 4. backfill flag
    t["backfill"] = bool(t.get("backfill", False))

    # 5. paper flag
    if "paper" not in t:
        t["paper"] = True

    return t


def validate_trade(t: Dict[str, Any], strict: bool = True) -> List[str]:
    """
    Validate a trade record against canonical schema.

    strict=True:  raises TradeSchemaError on first issue
    strict=False: returns list of warning strings (empty = clean)

    Does NOT mutate the record — use migrate_trade() for that.
    """
    issues = []

    for field in REQUIRED_FIELDS:
        if field not in t or t[field] in (None, ""):
            issues.append(f"missing required field: {field}")

    # Direction
    d = t.get("direction")
    if d is not None and d not in VALID_DIRECTIONS:
        issues.append(f"direction must be long|short, got {d!r}")

    # Status
    s = t.get("status")
    if s is not None and s not in VALID_STATUSES:
        issues.append(f"status must be open|closed, got {s!r}")

    # Quantity sign matches direction
    q = t.get("quantity")
    if q is not None and d in VALID_DIRECTIONS:
        if d == "long" and q < 0:
            issues.append(f"long trade has negative quantity: {q}")
        if d == "short" and q > 0:
            issues.append(f"short trade has positive quantity: {q}")

    # Closed trades must have exit fields
    if s == "closed":
        if t.get("exit_date") is None:
            issues.append("closed trade missing exit_date")
        if t.get("exit_price") in (None, 0):
            issues.append("closed trade missing exit_price")

    # Exit reason valid
    er = t.get("exit_reason")
    if er is not None and er not in VALID_EXIT_REASONS:
        # Allow free-form reason strings as a warning only (e.g., "fir_exit (10d, avg of 12 bars)")
        if not any(er.startswith(prefix) for prefix in VALID_EXIT_REASONS if prefix):
            issues.append(f"exit_reason unusual: {er!r}")

    # Field name lint: legacy `signals` should have been migrated
    if "signals" in t:
        issues.append("legacy `signals` field present — migrate to `signal`")

    # Numeric sanity
    for num_field in ("entry_price", "exit_price", "pnl_usd", "pnl_pct", "fees_usd"):
        v = t.get(num_field)
        if v is not None and not isinstance(v, (int, float)):
            issues.append(f"{num_field} must be numeric, got {type(v).__name__}: {v!r}")

    if strict and issues:
        raise TradeSchemaError(f"Invalid trade record ({len(issues)} issue(s)): "
                               + "; ".join(issues))
    return issues


def normalize_trades_file(path: Path, dry_run: bool = False) -> Dict[str, Any]:
    """
    Migrate all trades in a paper_trades_*.json file to canonical schema.
    Returns {migrated: N, issues: [...]} summary.

    Safe to run multiple times (idempotent).
    """
    try:
        trades = json.loads(path.read_text())
    except Exception as e:
        return {"error": f"unparseable: {e}", "migrated": 0, "issues": []}

    if not isinstance(trades, list):
        return {"error": "not a list", "migrated": 0, "issues": []}

    migrated_count = 0
    all_issues = []
    for t in trades:
        before = dict(t)
        migrate_trade(t)
        if t != before:
            migrated_count += 1
        issues = validate_trade(t, strict=False)
        if issues:
            all_issues.append({"entry_date": t.get("entry_date"), "issues": issues})

    if not dry_run and migrated_count > 0:
        path.write_text(json.dumps(trades, indent=2))

    return {
        "path": str(path),
        "total_trades": len(trades),
        "migrated": migrated_count,
        "issues": all_issues,
    }


def normalize_all_paper_trades(dry_run: bool = False) -> List[Dict]:
    """Normalize all paper_trades_*.json files in tracker/ directory."""
    from pair_registry import paper_trade_files
    ROOT = Path(__file__).resolve().parent
    results = []
    for _, fname in paper_trade_files(source="both"):
        path = ROOT / "tracker" / fname
        if path.exists():
            result = normalize_trades_file(path, dry_run=dry_run)
            results.append(result)
    return results


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    print(f"Normalizing all paper_trades files{' (dry-run)' if dry else ''}...")
    results = normalize_all_paper_trades(dry_run=dry)
    total_migrated = sum(r.get("migrated", 0) for r in results)
    total_issues = sum(len(r.get("issues", [])) for r in results)
    total_trades = sum(r.get("total_trades", 0) for r in results)
    print(f"\n{len(results)} files checked, {total_trades} trades, "
          f"{total_migrated} migrated, {total_issues} issues")
    if total_issues:
        for r in results:
            if r.get("issues"):
                print(f"\n  {r['path']}:")
                for i in r["issues"]:
                    print(f"    {i['entry_date'][:10]}: {i['issues']}")
