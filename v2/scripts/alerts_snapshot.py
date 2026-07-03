#!/usr/bin/env python3
"""Argia_Mont — acute (per-snapshot) alert evaluation.

Runs frequently during daylight, right behind telemetry collection, so
conditions that are evidence from a SINGLE sample surface within one cycle
instead of tomorrow morning:

    inverter_fault       fault token in an inverter's latest sample
    inverter_temp_high   latest internal temperature >= 65/75 degC
    plant_offline        the WHOLE plant at 0 W mid-daylight
    data_stale           plant's newest sample older than 2 h of daylight

This tier only OPENS or TOUCHES alerts (``resolve_missing=False``): the
DAILY run is the single owner of resolution, arbitrating on full-day
aggregates. One-way acute + daily arbitration = flapping is structurally
impossible.

Outside daylight the script exits immediately (nothing acute is meaningful
at night, and it saves the sheet read).

USAGE
    PYTHONPATH=. python scripts/alerts_snapshot.py            # live
    PYTHONPATH=. python scripts/alerts_snapshot.py --dry-run  # print only

EXIT CODES
    0  ran cleanly (including the night no-op)
    3  config error
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from typing import List

from argia.alerts.engine import candidate_from_acute_breach, reconcile_alerts
from argia.analytics.acute import (
    DAYLIGHT_END_HOUR,
    DAYLIGHT_START_HOUR,
    evaluate_acute,
)
from argia.core.alerts_state import (
    ALERTS_HEADER,
    create_alerts_tab_if_missing,
    load_alerts_ledger,
    record_to_row,
)
from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import UTC, now_mx
from argia.kpi import read_day_bundle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argia.alerts_snapshot")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and print; write nothing")
    args = parser.parse_args(argv)

    mx = now_mx()
    if not (DAYLIGHT_START_HOUR <= mx.hour < DAYLIGHT_END_HOUR):
        log.info("outside daylight (%s MX) — acute checks are a no-op", mx)
        return 0

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 not set")
        return 3
    try:
        sheets = SheetsClient(sheet_id=sheet_id)
        portfolio = load_portfolio(sheets)
    except Exception as e:  # noqa: BLE001
        log.error("bootstrap failed: %s", e)
        return 3

    # Today's rows carry every "latest sample"; the evaluator freshness-
    # filters internally.
    bundle = read_day_bundle(sheets, mx.date().isoformat())
    samples = []
    for plant in portfolio.active_plants():
        for r in bundle.rows_for_plant(plant.plant_key):
            samples.append((r.timestamp_utc, plant.plant_key, r.inverter_sn,
                            r.power_w, r.temperature_c, r.status,
                            r.fault_code))
    log.info("acute evaluation at %s MX over %d sample(s)", mx, len(samples))

    now_utc = dt.datetime.now(UTC)
    breaches = evaluate_acute(
        samples, [p.plant_key for p in portfolio.active_plants()], now_utc)
    candidates = [candidate_from_acute_breach(b) for b in breaches]
    for c in candidates:
        log.info("ACUTE [%s] %s", c.severity, c.message)
    if not candidates:
        log.info("no acute conditions")

    create_alerts_tab_if_missing(sheets)
    ledger = load_alerts_ledger(sheets)
    # open/touch ONLY — daily owns resolution.
    result = reconcile_alerts(ledger, candidates, now_utc,
                              resolve_missing=False)
    log.info("Reconcile (acute, no-resolve): %s", result.summary())
    for r in result.opened:
        log.info("OPEN   %s  %s", r.alert_id, r.message)

    if args.dry_run:
        log.info("[DRY RUN] no rows written")
        return 0
    if result.opened or result.touched:
        block = [record_to_row(r) for r in result.records]
        end_col = chr(ord("A") + len(ALERTS_HEADER) - 1)
        sheets.write_values("Alerts", f"A2:{end_col}{len(block) + 1}", block)
        log.info("Wrote %d alert row(s) to Alerts", len(block))
    else:
        log.info("ledger unchanged — nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
