#!/usr/bin/env python3
"""Read-only probe of the SolarEdge Monitoring API v2 (v330).

    solaredge_v2_probe.py --site 4362085 --dry-run      # list the calls, spend nothing
    solaredge_v2_probe.py --site 4362085                # ~10 credits

Why: the v2 response shapes must come from the real API, not from community
code. Each answer is saved as raw JSON in --out (customer data: server only,
default /root/argia_onboard/se_v2_probe, mode 0700) and summarised here as
HTTP status + structure (keys and list lengths, never values), so the
parsers and their fixtures can be written from what SolarEdge really sends.

Needs SOLAREDGE_V2_API_KEY (and SOLAREDGE_V2_ACCOUNT_KEY if the Developer
Platform gave one) in the environment. Every call passes the credit budget.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argia.vendors import solaredge_v2 as V2  # noqa: E402

OUT_DEFAULT = "/root/argia_onboard/se_v2_probe"


def calls(site: str, day: dt.date) -> List[Tuple[str, str, dict]]:
    """(name, path, params) - the endpoints the monitoring would use. PURE."""
    d0 = dt.datetime(day.year, day.month, day.day).strftime("%Y-%m-%dT00:00:00Z")
    d1 = (dt.datetime(day.year, day.month, day.day) + dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    m0 = day.replace(day=1).strftime("%Y-%m-%dT00:00:00Z")
    return [
        ("sites", "sites", {}),
        ("site", f"sites/{site}", {}),
        ("devices_inverters", f"sites/{site}/devices", {"types": "INVERTER"}),
        ("overview", f"sites/{site}/overview", {}),
        ("power_day", f"sites/{site}/power", {"from": d0, "to": d1}),
        ("energy_day", f"sites/{site}/energy", {"from": m0, "to": d1, "resolution": "DAY"}),
        ("inverters_telemetry", f"sites/{site}/inverters/telemetry", {"from": d0, "to": d1}),
        ("alerts", f"sites/{site}/alerts", {}),
    ]


def shape(obj: Any, depth: int = 0) -> str:
    """Structure without values: keys, list lengths, scalar types. PURE."""
    if depth > 5:
        return "..."
    if isinstance(obj, dict):
        return "{" + ", ".join(f"{k}: {shape(v, depth + 1)}" for k, v in list(obj.items())[:25]) + "}"
    if isinstance(obj, list):
        return f"[{len(obj)} x {shape(obj[0], depth + 1)}]" if obj else "[]"
    return type(obj).__name__


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SolarEdge v2 read-only probe")
    ap.add_argument("--site", required=True)
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    day = dt.date.fromisoformat(a.day) if a.day else dt.date.today() - dt.timedelta(days=1)
    plan = calls(a.site, day)
    if a.dry_run:
        for name, path, params in plan:
            print(f"would call {name}: GET /v2/{path} {params}")
        print(f"{len(plan)} call(s) = {len(plan)} credit(s)")
        return 0
    client = V2.SolarEdgeV2Client.from_env()
    os.makedirs(a.out, mode=0o700, exist_ok=True)
    bad = 0
    for name, path, params in plan:
        try:
            body = client.get(path, params)
            with open(os.path.join(a.out, f"{name}.json"), "w", encoding="utf-8") as fh:
                json.dump(body, fh, indent=1)
            print(f"OK   {name}: {shape(body)[:600]}")
        except V2.SolarEdgeV2BudgetExceeded as e:
            print(f"STOP {e}")
            return 2
        except V2.SolarEdgeV2Error as e:
            bad += 1
            print(f"FAIL {name}: {str(e)[:300]}")
    print(f"credits left this month: {V2.CreditBudget().remaining()}")
    return 1 if bad == len(plan) else 0


if __name__ == "__main__":
    sys.exit(main())
