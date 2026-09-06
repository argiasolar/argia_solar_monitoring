"""Write the portfolio (plant + inverter records) as JSON next to the
nightly backups (v214). The Pi pulls it with the dumps, so its outage
watch (pi/report_watch/ppa_watch.py) knows the PPA plants without a
database or a sheet — the workbook is retired.

    python3 scripts/portfolio_export.py --out /root/argia_backups/portfolio_latest.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from argia.core.config_pg import inverters_records, plants_records
from argia.core.sheets import open_sheets


def export(plants_raw, inverters_raw) -> dict:
    """The JSON shape ppa_watch reads: exactly the two record lists."""
    return {"generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "plants": list(plants_raw), "inverters": list(inverters_raw)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    sheets = open_sheets()
    doc = export(plants_records(sheets, "A1:AZ"), inverters_records(sheets, "A1:Z"))
    if not doc["plants"]:
        print("portfolio_export: no plants — nothing written", file=sys.stderr)
        return 1
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, default=str)
    os.replace(tmp, args.out)
    print(f"portfolio_export: {len(doc['plants'])} plants, {len(doc['inverters'])} inverters -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
