#!/usr/bin/env python3
"""Argia_Mont — acute (per-snapshot) alert evaluation.

Runs frequently during daylight, right behind telemetry collection, so
conditions that are evidence from a SINGLE sample surface within one cycle
instead of tomorrow morning:

    inverter_fault       >=2 fault samples in the last 35 min (look-back)
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

from argia.alerts.engine import (
    candidate_from_acute_breach, apply_maintenance_suppression,
    reconcile_alerts,
)
from argia.maintenance.events import (
    load_maintenance_events, plant_maintenance_on_date,
)
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
from argia.core.normalize import normalize_text, safe_float
from argia.core.time_utils import UTC, now_mx
from argia.core.job_log import instrument

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argia.alerts_snapshot")

TAIL_ROWS = 600
"""How many trailing telemetry rows to read. The tab is append-ordered, so
the tail IS the newest data. 600 rows spans ~2 days at today's GitHub
cadence and ~4-5 h at the Pi's future 10-min cadence — far more than the
45-min freshness window and the 2 h acute-gap check need. Reading the tail
instead of the whole tab (13k+ rows, growing daily) cuts the per-run
payload ~20x, and the ratio improves as the tab grows."""

TELEMETRY_TAB = "Telemetry_Argia"


def _read_recent_samples(sheets: SheetsClient, tail_rows: int = TAIL_ROWS):
    """Read only the tail of Telemetry_Argia and parse the acute fields.

    Two cheap reads: column A for the used-row count, then the last
    ``tail_rows`` data rows. Fields resolved BY HEADER NAME, so column
    reordering can't silently break parsing.
    Returns (samples, tail_span_hours): samples shaped for
    ``evaluate_acute``; tail_span_hours = coverage of the tail, used to
    report plants entirely absent from it.
    """
    header = [normalize_text(h) for h in
              (sheets.read_range(TELEMETRY_TAB, "A1:ZZ1") or [[]])[0]]
    need = ("timestamp_utc", "plant_key", "inverter_sn", "power_w",
            "temperature_c", "status", "fault_code")
    if not all(n in header for n in need):
        raise RuntimeError(f"{TELEMETRY_TAB} missing columns for acute parse")
    idx = {n: header.index(n) for n in need}
    end_col_i = max(idx.values())
    end_col = chr(ord("A") + end_col_i) if end_col_i < 26 else "A" + chr(ord("A") + end_col_i - 26)

    n_rows = len(sheets.read_range(TELEMETRY_TAB, "A:A"))
    start = max(2, n_rows - tail_rows + 1)
    data = sheets.read_range(TELEMETRY_TAB, f"A{start}:{end_col}{n_rows}")

    samples = []
    for row in data:
        def cell(name):
            i = idx[name]
            return row[i] if i < len(row) else None
        try:
            ts = dt.datetime.fromisoformat(
                str(cell("timestamp_utc")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except ValueError:
            continue
        st = cell("status")
        samples.append((
            ts, str(cell("plant_key") or ""), str(cell("inverter_sn") or ""),
            safe_float(cell("power_w")), safe_float(cell("temperature_c")),
            int(st) if isinstance(st, (int, float)) else None,
            cell("fault_code"),
        ))
    span_h = 0.0
    if samples:
        stamps = [s[0] for s in samples]
        span_h = (max(stamps) - min(stamps)).total_seconds() / 3600.0
    log.info("tail read: rows %d-%d of %d, %d parsed, span %.1f h",
             start, n_rows, n_rows, len(samples), span_h)
    return samples, span_h


@instrument("alerts_snapshot")
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

    # Only the TAIL of telemetry — the acute tier needs the latest samples,
    # not the day. The evaluator freshness-filters internally.
    samples, span_h = _read_recent_samples(sheets)
    log.info("acute evaluation at %s MX over %d tail sample(s)", mx, len(samples))

    now_utc = dt.datetime.now(UTC)
    breaches = evaluate_acute(
        samples, [p.plant_key for p in portfolio.active_plants()], now_utc,
        absent_gap_hours=span_h if span_h >= 2.0 else None)
    candidates = [candidate_from_acute_breach(b) for b in breaches]
    for c in candidates:
        log.info("ACUTE [%s] %s", c.severity, c.message)
    if not candidates:
        log.info("no acute conditions")

    # v92: same maintenance suppression as the daily tier — a plant in a
    # logged window won't open an acute plant_offline/data_stale (it is
    # intentionally down). Hardware faults still fire.
    events = load_maintenance_events(sheets)
    maint = plant_maintenance_on_date(events, mx.date().isoformat(), now=mx)
    if maint:
        candidates, suppressed = apply_maintenance_suppression(
            candidates, maint)
        for c in suppressed:
            log.info("SUPPRESS(maint) [%s] %s %s", c.severity,
                     c.plant_key, c.metric)

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
