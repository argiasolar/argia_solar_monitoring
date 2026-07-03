#!/usr/bin/env python3
"""Argia_Mont — daily alert evaluation (plan #5).

Runs AFTER kpi_eod (which stamps energy / specific_yield / expected_kwh /
data_class). Evaluates yesterday's full-day aggregates through the three
performance detectors, reconciles against the Alerts ledger, and persists
open/touch/resolve transitions as rows in the Alerts tab.

Layers evaluated:
  1. inverter_relative  — inverter daily energy vs plant-peer MEDIAN
  2. plant_twin_yield   — specific yield vs regional twin (SLP pair, MEX pair)
  3. energy_daily_pct   — plant energy vs expected_kwh

Data-quality gate: layers 2 and 3 only run for plants whose KPI_Daily
data_class is "full". An undercounted partial day must not fire plant
alerts. Layer 1 runs regardless — peers share the same window.

USAGE
    PYTHONPATH=. python scripts/alerts_daily.py                # yesterday
    PYTHONPATH=. python scripts/alerts_daily.py --date 2026-07-02
    PYTHONPATH=. python scripts/alerts_daily.py --dry-run      # print only

EXIT CODES
    0  ran cleanly (alerts may or may not have fired)
    2  no telemetry for the day (nothing evaluated)
    3  config error
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional

from argia.alerts.engine import (
    Candidate,
    candidate_from_expected_breach,
    candidate_from_fault_breach,
    candidate_from_stale_breach,
    candidate_from_string_breach,
    candidate_from_relative_breach,
    candidate_from_twin_breach,
    reconcile_alerts,
)
from argia.analytics.inverter_health import (
    InverterReading,
    evaluate_inverter_relative,
)
from argia.analytics.acute import TEMP_CRIT_C, TEMP_WARN_C
from argia.analytics.data_health import evaluate_data_stale
from argia.analytics.vendor_flags import (
    STRING_BASELINE_DAYS,
    evaluate_inverter_faults,
    evaluate_string_new_bits,
)
from argia.analytics.perf_indicators import (
    evaluate_energy_vs_expected,
    evaluate_plant_twins,
)
from argia.archive.kpi_daily import (
    DATA_CLASS_FULL,
    KPI_DAILY_TAB,
    date_key,
)
from argia.core.alerts_state import (
    ALERTS_HEADER,
    create_alerts_tab_if_missing,
    load_alerts_ledger,
    record_to_row,
)
from argia.core.config import load_portfolio
from argia.core.normalize import normalize_text, safe_float
from argia.core.sheets import SheetsClient
from argia.core.time_utils import UTC, now_mx
from argia.kpi import compute_plant_energy, read_day_bundle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argia.alerts_daily")


def _read_kpi_day(sheets: SheetsClient, date_iso: str) -> Dict[str, Dict]:
    """KPI_Daily rows for one day: plant_key -> {energy, sy, expected, data_class}."""
    out: Dict[str, Dict] = {}
    data = sheets.read_range(KPI_DAILY_TAB, "A1:ZZ")
    if not data:
        return out
    header = [normalize_text(h) for h in data[0]]
    idx = {name: header.index(name) for name in
           ("date_iso", "plant_key", "energy_kwh", "specific_yield",
            "expected_kwh", "data_class") if name in header}
    for row in data[1:]:
        try:
            if date_key(row[idx["date_iso"]]) != date_iso:
                continue
            pk = normalize_text(row[idx["plant_key"]]).upper()
        except (KeyError, IndexError):
            continue
        def cell(name):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else None
        out[pk] = {
            "energy": safe_float(cell("energy_kwh")),
            "sy": safe_float(cell("specific_yield")),
            "expected": safe_float(cell("expected_kwh")),
            "data_class": normalize_text(cell("data_class")).lower(),
        }
    return out


def _read_string_samples(sheets, portfolio, date_iso: str):
    """Read str_break/str_unmatch/str_unblance from the deep per-plant tabs.

    Returns (day_samples, baseline_samples) shaped for
    ``evaluate_string_new_bits``. Plants without a deep tab, or whose tab
    lacks the string columns (non-Growatt schemas leave them blank), simply
    contribute nothing — string alerting degrades gracefully per plant.
    """
    import datetime as _dt
    y, m, d = (int(x) for x in date_iso.split("-"))
    day0 = _dt.date(y, m, d)
    base_start = (day0 - _dt.timedelta(days=STRING_BASELINE_DAYS)).isoformat()
    cols = ("str_break", "str_unmatch", "str_unblance")
    day_s, base_s = [], []
    for plant in portfolio.active_plants():
        tab = f"Telemetry_{plant.plant_key}"
        try:
            data = sheets.read_range(tab, "A1:ZZ")
        except Exception as e:  # noqa: BLE001
            log.info("string flags: no deep tab for %s (%s) — skipped",
                     plant.plant_key, e)
            continue
        if not data:
            continue
        header = [normalize_text(h) for h in data[0]]
        if not all(c in header for c in cols) or "timestamp_utc" not in header:
            continue
        ic = {c: header.index(c) for c in cols}
        iu = header.index("timestamp_utc")
        isn = header.index("inverter_sn")
        imx = header.index("timestamp_mx") if "timestamp_mx" in header else None
        for row in data[1:]:
            if iu >= len(row) or isn >= len(row):
                continue
            mx_day = str(row[imx])[:10] if imx is not None else ""
            if not (base_start <= mx_day <= date_iso):
                continue
            try:
                ts = _dt.datetime.fromisoformat(
                    str(row[iu]).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                continue
            vals = {c: (row[ic[c]] if ic[c] < len(row) else None) for c in cols}
            entry = (ts, plant.plant_key, str(row[isn]), vals)
            if mx_day == date_iso:
                day_s.append(entry)
            else:
                base_s.append(entry)
    return day_s, base_s


def build_candidates(
    per_inverter_kwh: List[InverterReading],
    kpi_by_plant: Dict[str, Dict],
    fault_samples: Optional[List] = None,
    string_day: Optional[List] = None,
    string_baseline: Optional[List] = None,
    stale_breaches: Optional[List] = None,
    temp_candidates: Optional[List[Candidate]] = None,
    offline_candidates: Optional[List[Candidate]] = None,
) -> List[Candidate]:
    """Run the detector layers; map breaches to engine candidates."""
    cands: List[Candidate] = []

    for b in evaluate_inverter_relative(per_inverter_kwh):
        cands.append(candidate_from_relative_breach(b))

    for b in evaluate_inverter_faults(fault_samples or []):
        cands.append(candidate_from_fault_breach(b))

    for b in evaluate_string_new_bits(string_day or [], string_baseline or []):
        cands.append(candidate_from_string_breach(b))

    for b in (stale_breaches or []):
        cands.append(candidate_from_stale_breach(b))

    cands.extend(temp_candidates or [])
    cands.extend(offline_candidates or [])

    full = {pk: v for pk, v in kpi_by_plant.items()
            if v.get("data_class") == DATA_CLASS_FULL}
    skipped = sorted(set(kpi_by_plant) - set(full))
    if skipped:
        log.info("data_class gate: plant-level layers skip %s", skipped)

    sy = {pk: v["sy"] for pk, v in full.items()}
    for b in evaluate_plant_twins(sy):
        cands.append(candidate_from_twin_breach(b))

    energy = {pk: v["energy"] for pk, v in full.items()}
    expected = {pk: v["expected"] for pk, v in full.items()}
    for b in evaluate_energy_vs_expected(energy, expected):
        cands.append(candidate_from_expected_breach(b))

    return cands


def daily_temp_candidates(bundle, portfolio) -> List[Candidate]:
    """Daily owner of inverter_temp_high: fires on the day's MAX temperature,
    so an acute-opened alert resolves once a full day stays below WARN."""
    from argia.core.alerts_state import make_inverter_alert_key
    out: List[Candidate] = []
    for plant in portfolio.active_plants():
        peak: Dict[str, float] = {}
        for r in bundle.rows_for_plant(plant.plant_key):
            if r.temperature_c is not None:
                sn = str(r.inverter_sn).strip()
                peak[sn] = max(peak.get(sn, -999.0), float(r.temperature_c))
        for sn, t in sorted(peak.items()):
            if t < TEMP_WARN_C:
                continue
            crit = t >= TEMP_CRIT_C
            out.append(Candidate(
                alert_key=make_inverter_alert_key(plant.plant_key, sn,
                                                  "inverter_temp_high"),
                plant_key=plant.plant_key, inverter_sn=sn,
                metric="inverter_temp_high",
                severity="CRITICAL" if crit else "WARNING",
                value=round(t, 1), threshold=TEMP_CRIT_C if crit else TEMP_WARN_C,
                message=(f"{plant.plant_key} {sn}: day-peak temperature "
                         f"{t:.1f} degC [{'CRITICAL' if crit else 'WARNING'}]"),
            ))
    return out


def daily_offline_candidates(readings: List[InverterReading]) -> List[Candidate]:
    """Daily owner of plant_offline: a plant that HAD telemetry but produced
    0 kWh across all inverters for the whole day."""
    from argia.core.alerts_state import make_plant_alert_key
    from collections import defaultdict
    tot: Dict[str, float] = defaultdict(float)
    n: Dict[str, int] = defaultdict(int)
    for r in readings:
        tot[r.plant_key] += r.value
        n[r.plant_key] += 1
    out: List[Candidate] = []
    for pk in sorted(tot):
        if n[pk] and tot[pk] <= 0:
            out.append(Candidate(
                alert_key=make_plant_alert_key(pk, "plant_offline"),
                plant_key=pk, inverter_sn="", metric="plant_offline",
                severity="CRITICAL", value=0.0, threshold=None,
                message=f"{pk}: 0 kWh across all inverters for the day [CRITICAL]",
            ))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--date", default=None,
                        help="ISO date to evaluate (default: yesterday MX)")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and print; write nothing")
    args = parser.parse_args(argv)

    date_iso = args.date or (now_mx().date() - dt.timedelta(days=1)).isoformat()

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

    # --- inverter daily energies from telemetry ---
    bundle = read_day_bundle(sheets, date_iso)
    readings: List[InverterReading] = []
    rated = {i.inverter_sn: i.rated_kw
             for p in portfolio.active_plants()
             for i in portfolio.inverters_for(p.plant_key)}
    n_rows = 0
    for plant in portfolio.active_plants():
        rows = bundle.rows_for_plant(plant.plant_key)
        if not rows:
            continue
        n_rows += len(rows)
        # compute_plant_energy returns sn -> EnergyDay (an object); the
        # detector wants the day's kWh as a plain float. energy_kwh is
        # None when the day had too little data for that inverter — skip
        # those rather than feeding the detector a fake 0 (an inverter
        # with NO data is a data-quality problem, not "producing zero").
        for sn, eday in compute_plant_energy(rows).items():
            if eday.energy_kwh is None:
                log.info("[%s] %s: no computable energy for %s — skipped",
                         plant.plant_key, sn, date_iso)
                continue
            readings.append(InverterReading(
                plant_key=plant.plant_key, inverter_sn=sn,
                value=eday.energy_kwh, rated_kw=rated.get(sn),
            ))
    if not readings:
        log.warning("no telemetry for %s — nothing evaluated", date_iso)
        return 2

    # --- vendor fault codes: same bundle rows, zero extra reads ---
    fault_samples = []
    for plant in portfolio.active_plants():
        for r in bundle.rows_for_plant(plant.plant_key):
            fault_samples.append(
                (r.timestamp_utc, plant.plant_key, r.inverter_sn, r.fault_code)
            )

    # --- data staleness: bundle timestamps per plant (zero rows = breach) ---
    ts_by_plant = {p.plant_key: [r.timestamp_utc
                                 for r in bundle.rows_for_plant(p.plant_key)]
                   for p in portfolio.active_plants()}
    stale = evaluate_data_stale(
        ts_by_plant, [p.plant_key for p in portfolio.active_plants()], date_iso)

    # --- string flags: deep per-plant tabs, day vs trailing baseline ---
    string_day, string_baseline = _read_string_samples(
        sheets, portfolio, date_iso)

    # --- plant-level aggregates from KPI_Daily (stamped by kpi_eod) ---
    kpi = _read_kpi_day(sheets, date_iso)
    log.info("Evaluating %s: %d inverter readings, %d KPI plant rows",
             date_iso, len(readings), len(kpi))

    candidates = build_candidates(
        readings, kpi, fault_samples, string_day, string_baseline, stale,
        temp_candidates=daily_temp_candidates(bundle, portfolio),
        offline_candidates=daily_offline_candidates(readings),
    )
    for c in candidates:
        log.info("CANDIDATE [%s] %s", c.severity, c.message)
    if not candidates:
        log.info("no breaches today")

    # --- reconcile against ledger ---
    create_alerts_tab_if_missing(sheets)
    ledger = load_alerts_ledger(sheets)
    result = reconcile_alerts(ledger, candidates, dt.datetime.now(UTC))
    log.info("Reconcile: %s", result.summary())
    for r in result.opened:
        log.info("OPEN     %s  %s", r.alert_id, r.message)
    for r in result.touched:
        log.info("TOUCH    %s  %s", r.alert_id, r.alert_key)
    for r in result.resolved:
        log.info("RESOLVE  %s  %s", r.alert_id, r.alert_key)

    if args.dry_run:
        log.info("[DRY RUN] no rows written")
        return 0

    # --- persist: rewrite the data region in ledger order ---
    # Rows only ever update in place or append (history never shrinks), so a
    # single block write of all records is idempotent and race-free for a
    # once-a-day job.
    if result.opened or result.touched or result.resolved:
        block = [record_to_row(r) for r in result.records]
        end_col = chr(ord("A") + len(ALERTS_HEADER) - 1)   # N for 14 cols
        sheets.write_values(
            "Alerts", f"A2:{end_col}{len(block) + 1}", block,
        )
        log.info("Wrote %d alert row(s) to Alerts", len(block))
    else:
        log.info("ledger unchanged — nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
