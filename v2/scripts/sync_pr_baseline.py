"""One-time IMPORT of plant.pr_baseline from the Plants sheet tab.

History: written 2026-09-02 when the sheet was still the authority for
the clean-state PR. Later the same day the /setup/finance PR editor
shipped (v168) and the DATABASE became the authority — admin edits land
in PG with an audit row, and the sheet column is legacy. Running this
with --apply now would OVERWRITE audited admin edits with stale sheet
values, so: use it only as an import tool on a fresh database, and
check the finance_audit trail for 'pr_baseline' actions first.

Dry-run by default. Usage: sync_pr_baseline.py [--apply]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.store import pg_mirror

LOG = logging.getLogger("argia.sync_pr_baseline")


def diff_rows(sheet_vals, pg_vals):
    """[(key, sheet, pg)] where they differ (>0.001) — pure."""
    out = []
    for k in sorted(set(sheet_vals) | set(pg_vals)):
        s, g = sheet_vals.get(k), pg_vals.get(k)
        if s is None:
            continue                    # sheet silent -> leave PG alone
        if g is None or abs(s - g) > 0.001:
            out.append((k, s, g))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="pr_baseline sheet->PG sync")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    from argia.store.pgq import psql_exec, psql_rows

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 not set")
        return 1
    portfolio = load_portfolio(SheetsClient(sheet_id=sheet_id))
    sheet_vals = {p.plant_key: p.pr_baseline
                  for p in portfolio.plants.values()
                  if p.pr_baseline is not None}
    pg_vals = {}
    for r in psql_rows("SELECT plant_key, pr_baseline FROM plant;"):
        try:
            pg_vals[r[0]] = float(r[1]) if len(r) > 1 and r[1] else None
        except (ValueError, TypeError):
            pg_vals[r[0]] = None

    LOG.info("sheet defines pr_baseline for %d plant(s); PG has %d",
             len(sheet_vals), len(pg_vals))
    diffs = diff_rows(sheet_vals, pg_vals)
    if not diffs:
        LOG.info("PG already matches the sheet — nothing to change")
        return 0
    for k, s, g in diffs:
        LOG.info("  %-6s sheet=%.3f pg=%s", k, s,
                 f"{g:.3f}" if g is not None else "NULL")
    if not args.apply:
        LOG.info("dry-run — nothing written")
        return 0
    for k, s, _ in diffs:
        psql_exec("UPDATE plant SET pr_baseline = %.4f"
                  " WHERE plant_key = '%s';" % (s, k))
    got = {r[0]: r[1] for r in psql_rows(
        "SELECT plant_key, pr_baseline FROM plant;")}
    bad = [k for k, s, _ in diffs
           if abs(float(got.get(k, 0) or 0) - s) > 0.001]
    if bad:
        LOG.error("verification FAILED for %s", bad)
        return 1
    LOG.info("synced %d plant(s); PG now matches the sheet", len(diffs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
