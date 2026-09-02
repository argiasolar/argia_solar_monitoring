"""Correct MEX1 (SAG) installed capacity: 586.71 -> 597.78 kWp.

Solar director's August 2026 close: three installed-capacity figures
circulated for SAG — 597.78 kWp (Helioscope design), 598.7 (his June
report) and 586.7 (our billing/monitoring config). Only 597.78
reconciles with the as-built array: 972 x LONGi LR7-72HTH-615M =
972 x 0.615 = 597.78 kWp exactly, and his string detection confirms
54 strings x 18 modules. He asked for the other figures to be
"corrected at source"; Tomasz authorized it 2026-09-02.

Updates BOTH copies of the config — the PG ``plant`` table (report
pages) and the Plants sheet tab kwp_dc cell (the daily KPI pipeline
reads the sheet) — and leaves a finance_audit row. Effect to expect:
PR figures drop ~1.9% from the correction date forward (same energy,
honest denominator); history keeps the PR stamped at compute time.

Dry-run by default. Usage: fix_mex1_kwp.py [--apply]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from argia.core.sheets import SheetsClient
from argia.store import pg_mirror

LOG = logging.getLogger("argia.fix_mex1_kwp")

PLANT = "MEX1"
OLD_KWP = 586.71
NEW_KWP = 597.78          # 972 modules x 615 W, exact


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MEX1 kwp correction")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    from argia.store.pgq import psql_exec, psql_rows

    cur = float(psql_rows("SELECT kwp_dc FROM plant WHERE plant_key"
                          f" = '{PLANT}';")[0][0])
    LOG.info("PG %s kwp_dc: %.2f (expect %.2f before fix)", PLANT, cur,
             OLD_KWP)
    if abs(cur - NEW_KWP) < 0.01:
        LOG.info("PG already corrected")
    elif abs(cur - OLD_KWP) > 0.5:
        LOG.error("PG value %.2f is neither the old nor the new figure"
                  " — refusing to touch it", cur)
        return 1

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    sheets = SheetsClient(sheet_id=sheet_id) if sheet_id else None
    row_ix = col_ix = None
    if sheets:
        grid = sheets.read_range("Plants", "A1:AZ200")
        header = [str(h).strip() for h in (grid[0] if grid else [])]
        try:
            key_c = header.index("plant_key")
            col_ix = header.index("kwp_dc")
        except ValueError:
            LOG.error("Plants tab: plant_key/kwp_dc column not found")
            return 1
        for i, row in enumerate(grid[1:], start=2):
            if len(row) > key_c and str(row[key_c]).strip() == PLANT:
                row_ix = i
                sheet_val = row[col_ix] if len(row) > col_ix else None
                LOG.info("sheet Plants!%s%d currently: %s",
                         chr(65 + col_ix) if col_ix < 26 else "?",
                         i, sheet_val)
                break
        if row_ix is None:
            LOG.error("MEX1 not found in Plants tab")
            return 1

    if not args.apply:
        LOG.info("dry-run — would set PG and Plants!row %s kwp_dc to %.2f",
                 row_ix, NEW_KWP)
        return 0

    psql_exec("UPDATE plant SET kwp_dc = %.2f WHERE plant_key = '%s';"
              % (NEW_KWP, PLANT))
    psql_exec("INSERT INTO finance_audit (username, plant_key, loan_id,"
              " action, detail) VALUES ('tomasz','%s','','kwp_fix',"
              "'MEX1 kwp_dc %.2f -> %.2f (972 x 615W as-built; solar"
              " director Aug close reconciliation)');"
              % (PLANT, OLD_KWP, NEW_KWP))
    if sheets and row_ix is not None:
        sheets.write_cell("Plants", row_ix, col_ix + 1, NEW_KWP)
    got = float(psql_rows("SELECT kwp_dc FROM plant WHERE plant_key"
                          f" = '{PLANT}';")[0][0])
    ok = abs(got - NEW_KWP) < 0.01
    LOG.info("VERIFY PG kwp_dc = %.2f -> %s", got, "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
