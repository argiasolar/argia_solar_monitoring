#!/usr/bin/env python3
"""One-off (v189.1): restore env fields in PostgreSQL that the mirror erased.

The PG mirror overwrote irradiance / cloud / ambient / module_temp with
NULL on every SolarEdge refetch (see pg_mirror.py, v189.1). The sheet kept
them (v89 rule). For every row still in Telemetry_Argia, copy the env
fields into PG *only where PG is NULL and the sheet has a value* —
COALESCE semantics, so it is idempotent and can never clobber a value.

    PYTHONPATH=. python scripts/telemetry_env_backfill_pg.py            # dry run
    PYTHONPATH=. python scripts/telemetry_env_backfill_pg.py --apply
"""
from __future__ import annotations

import os
import sys

from argia.core.sheets import SheetsClient
from argia.kpi.reader import parse_rows
from argia.store.pgq import psql_exec, psql_rows

ENV_FIELDS = ("irradiance_wm2", "irradiance_kwh_m2_5m", "cloud_cover_pct",
              "ambient_temp_c", "module_temp_c")


def build_updates(rows) -> list:
    """SQL UPDATEs for rows that carry at least one env value. Pure."""
    out = []
    for r in rows:
        sets = []
        for f in ENV_FIELDS:
            v = getattr(r, f)
            if v is not None:
                sets.append(f"{f}=COALESCE({f}, {float(v)!r})")
        if not sets:
            continue
        out.append(
            "UPDATE telemetry SET " + ", ".join(sets)
            + f" WHERE plant_key='{r.plant_key}' AND inverter_sn='{r.inverter_sn}'"
            + f" AND ts_utc='{r.timestamp_utc.isoformat()}'::timestamptz"
            + " AND (" + " OR ".join(f"{f} IS NULL" for f in ENV_FIELDS) + ");")
    return out


def main(argv=None) -> int:
    apply = "--apply" in (argv or sys.argv[1:])
    sid = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    grid = SheetsClient(sid).read_range("Telemetry_Argia", "A1:P")
    rows = parse_rows(grid)
    ups = build_updates(rows)
    before = psql_rows("SELECT count(*) FROM telemetry WHERE cloud_cover_pct IS NULL;")[0][0]
    print(f"sheet rows: {len(rows)}  candidate updates: {len(ups)}  "
          f"PG rows with NULL cloud_cover before: {before}")
    if not apply:
        print("DRY RUN"); return 0
    for i in range(0, len(ups), 500):
        psql_exec("\n".join(ups[i:i + 500]))
    after = psql_rows("SELECT count(*) FROM telemetry WHERE cloud_cover_pct IS NULL;")[0][0]
    print(f"PG rows with NULL cloud_cover after: {after}  (restored {int(before)-int(after)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
