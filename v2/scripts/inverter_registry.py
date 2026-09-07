#!/usr/bin/env python3
"""Inverter registry vs the monitoring table (v229).

    inverter_registry.py            # check: table vs data/inverter_registry.json (exit 2 on differences)
    inverter_registry.py --apply    # make labels match and add missing inverters (idempotent), then re-check
    inverter_registry.py --live     # also ask SolarEdge (/equipment/{site}/list, 1 call per site) whether the
                                    # vendor's serials and names still match the registry
    inverter_registry.py --sql      # print the statements --apply would run, change nothing

drift_check runs the check every morning; a difference reaches the
administrator as a status-quo finding on the daily digest.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple

from argia.core import inverter_registry as R


def table_rows(rows) -> List[R.TableRow]:
    return [(r[0], r[1], r[2], r[3] in ("t", "true", "True", True))
            for r in rows("SET statement_timeout='10s'; SELECT plant_key, inverter_sn, coalesce(inverter_label,''), active::text"
                          " FROM inverter ORDER BY 1, 2;") if len(r) >= 4]


def solaredge_lists(rows) -> List[Tuple[str, List[Tuple[str, str]], str]]:
    """[(plant_key, [(sn, name)], error)] for every active SolarEdge plant."""
    from argia.vendors.solaredge import SolarEdgeClient
    out = []
    for pk, site, secret in rows("SET statement_timeout='5s'; SELECT plant_key, site_id, coalesce(secret_api_name,'')"
                                 " FROM plant WHERE brand = 'SOLAREDGE' AND active ORDER BY 1;"):
        key = os.environ.get(secret, "").strip() if secret else ""
        if not key:
            out.append((pk, [], f"no API key in the environment ({secret or 'secret_api_name empty'})"))
            continue
        try:
            resp = SolarEdgeClient(api_key=key)._get_json(f"/equipment/{site}/list", {})
            items = [(str(i.get("serialNumber") or ""), str(i.get("name") or ""))
                     for i in ((resp.get("reporters") or {}).get("list") or []) if isinstance(i, dict)]
            out.append((pk, items, ""))
        except Exception as e:  # noqa: BLE001
            out.append((pk, [], f"{type(e).__name__}: {e}"))
    return out


def live_findings(reg: dict, rows) -> List[str]:
    out: List[str] = []
    for pk, items, err in solaredge_lists(rows):
        if err:
            out.append(f"{pk}: vendor list unavailable — {err}")
        else:
            out += R.vendor_diff(reg, pk, items)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="inverter registry vs the monitoring table")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--live", action="store_true", help="also compare with the SolarEdge equipment lists")
    ap.add_argument("--sql", action="store_true", help="print the fix statements only")
    a = ap.parse_args(argv)
    from argia.store.pgq import psql_exec, psql_rows
    reg = R.load()
    bad = R.validate(reg)
    if bad:
        print("registry file invalid:")
        for b in bad:
            print("  - " + b)
        return 3
    rows = table_rows(psql_rows)
    if a.sql:
        for s in R.apply_sql(reg, rows):
            print(s)
        return 0
    if a.apply:
        stmts = R.apply_sql(reg, rows)
        for s in stmts:
            print("apply: " + s)
            if not s.startswith("--"):
                psql_exec(s)
        rows = table_rows(psql_rows)
    findings = R.compare(reg, rows)
    if a.live:
        findings += live_findings(reg, psql_rows)
    print(f"inverter_registry (verified {reg.get('verified')}): {len(rows)} table rows, {len(findings)} finding(s)")
    for f in findings:
        print("  - " + f)
    return 2 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
