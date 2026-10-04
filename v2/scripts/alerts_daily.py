#!/usr/bin/env python3
"""Argia_Mont - daily alert evaluation (plan #5).

Runs AFTER kpi_eod (which stamps energy / specific_yield / expected_kwh /
data_class). Evaluates yesterday's full-day aggregates through the three
performance detectors, reconciles against the Alerts ledger, and persists
open/touch/resolve transitions as rows in the Alerts tab.

Layers evaluated:
  1. inverter_relative  - inverter daily energy vs plant-peer MEDIAN
  2. plant_twin_yield   - specific yield vs regional twin (SLP pair, MEX pair)
  3. energy_daily_pct   - plant energy vs expected_kwh

Data-quality gate: layers 2 and 3 only run for plants whose KPI_Daily
data_class is "full". An undercounted partial day must not fire plant
alerts. Layer 1 runs regardless - peers share the same window.

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
import sys
from typing import Dict, List, Optional, Tuple

from argia.alerts.digest import resolve_digest_rows
from argia.alerts.engine import (
    Candidate,
    candidate_from_expected_breach,
    candidate_from_fault_breach,
    candidate_from_stale_breach,
    candidate_from_string_breach,
    candidate_from_relative_breach,
    candidate_from_twin_breach,
    apply_maintenance_suppression,
    reconcile_alerts,
)
from argia.maintenance.events import (
    load_maintenance_events, plant_maintenance_on_date,
)
from argia.analytics.inverter_health import (
    InverterReading,
    evaluate_inverter_relative,
)
from argia.analytics import evidence as EV
from argia.analytics.acute import TEMP_HIGH_C, TEMP_WARN_C
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
    date_key,
)
from argia.core.alerts_state import (
    create_alerts_tab_if_missing,
    load_alerts_ledger,
    write_ledger,
)
from argia.core.config import load_portfolio
from argia.core.normalize import normalize_text, safe_float
from argia.core.sheets import SheetsClient, open_sheets
from argia.core.time_utils import UTC, now_mx
from argia.kpi import compute_plant_energy, read_day_bundle
from argia.core.job_log import instrument
from argia.alerts import grading
from argia.telemetry.fresh import drop_repeats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argia.alerts_daily")


def _read_kpi_day(sheets: SheetsClient, date_iso: str) -> Dict[str, Dict]:
    """KPI_Daily rows for one day: plant_key -> {energy, sy, expected, data_class}."""
    out: Dict[str, Dict] = {}
    from argia.kpi.pg_kpi_source import kpi_grid
    data = kpi_grid(sheets, "A1:ZZ")          # v190: sheet or PG
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


def split_string_samples(rows, date_iso: str, base_start: str, active_keys):
    """Pure: PG wide rows -> (day_samples, baseline_samples) shaped for
    ``evaluate_string_new_bits``. ``rows`` are
    (ts_utc, plant_key, inverter_sn, {str_break, str_unmatch, str_unblance});
    a row lands in the day list when its MX day is ``date_iso`` and in
    the baseline when it is within [base_start, date_iso)."""
    from argia.core.time_utils import utc_to_mx
    day_s, base_s = [], []
    for ts, pk, sn, flags in rows:
        if pk not in active_keys or ts is None:
            continue
        mx_day = utc_to_mx(ts).date().isoformat()
        entry = (ts, pk, str(sn), dict(flags))
        if mx_day == date_iso:
            day_s.append(entry)
        elif base_start <= mx_day < date_iso:
            base_s.append(entry)
    return day_s, base_s


def _read_string_samples(portfolio, date_iso: str):
    """Read str_break/str_unmatch/str_unblance from ``telemetry_detail``
    (v207 - the per-plant sheet tabs are gone) for the day and its
    trailing baseline. A PG read failure degrades to no string samples,
    but LOUDLY (WARNING): silence here is exactly what v199–v206 got
    wrong."""
    import datetime as _dt
    from argia.store import pg_detail
    y, m, d = (int(x) for x in date_iso.split("-"))
    day0 = _dt.date(y, m, d)
    base_start = (day0 - _dt.timedelta(days=STRING_BASELINE_DAYS)).isoformat()
    try:
        rows = pg_detail.read_string_flags(base_start, date_iso)
    except Exception as e:  # noqa: BLE001
        log.warning("string flags: telemetry_detail unreadable (%s) - "
                    "string rule skipped for %s", e, date_iso)
        return [], []
    active = {p.plant_key for p in portfolio.active_plants()}
    day_s, base_s = split_string_samples(rows, date_iso, base_start, active)
    log.info("string flags: %d day / %d baseline samples from telemetry_detail",
             len(day_s), len(base_s))
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
    string_rows: Optional[Dict] = None,
    relative_skip=frozenset(),
) -> List[Candidate]:
    """Run the detector layers; map breaches to engine candidates.
    ``relative_skip`` (v305): (plant, sn) whose day counter is partial
    (``stopped_early``) - not judged against their peers."""
    cands: List[Candidate] = []

    for b in evaluate_inverter_relative([r for r in per_inverter_kwh
                                         if (r.plant_key, str(r.inverter_sn).strip()) not in relative_skip]):
        cands.append(candidate_from_relative_breach(b))

    for b in evaluate_inverter_faults(fault_samples or []):
        cands.append(candidate_from_fault_breach(b))

    for b in evaluate_string_new_bits(string_day or [], string_baseline or []):
        cands.append(string_candidate_with_evidence(b, per_inverter_kwh, string_rows or {}))

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


def without_frozen(bundle):
    """v303: the day's rows without frozen-logger repeats (the rule of
    the live pages, argia.telemetry.fresh). A logger that went quiet at
    16:35 while the vendor cloud kept answering with its last values is
    a hole in the data - data_stale and the silent-inverter rule must see
    the hole, not a fresh-looking repeat."""
    from argia.kpi import DayBundle
    rows = drop_repeats(list(bundle.rows), lambda r: (r.plant_key, str(r.inverter_sn).strip()),
                        lambda r: (r.timestamp_utc, r.power_w, r.etoday_kwh))
    if len(rows) == len(bundle.rows):
        return bundle
    log.info("frozen readings: %d repeated row(s) skipped for %s", len(bundle.rows) - len(rows), bundle.date_iso)
    return DayBundle(date_iso=bundle.date_iso, rows=tuple(rows))


TEMP_CLEAR_C = 60.0
"""v202 hysteresis: an OPEN temperature alert stays open (WARNING) while
the day-peak is still above this, and resolves only after a full day
below it. MEX1 (peaks 57-71 degC) used to open, resolve two days later
and re-open - one mail per cycle - for what is one condition."""


def _read_thermal_evidence(date_iso: str) -> Dict[Tuple[str, str], EV.ThermalDay]:
    """thermal_daily rows of the day (argia-thermal ran at 01:10 MX):
    the measured derating evidence behind a day-peak temperature. Empty
    on any failure - the rule then stays at WARNING (v220)."""
    from argia.store.pgq import psql_rows
    out: Dict[Tuple[str, str], EV.ThermalDay] = {}
    try:
        rows = psql_rows("SELECT plant_key, inverter_sn, coalesce(derating_minutes,0), coalesce(lost_kwh,0),"
                         " energy_kwh, coalesce(vendor_derating_minutes,0)"
                         f" FROM thermal_daily WHERE prod_date = DATE '{date_iso}';")
    except Exception as e:  # noqa: BLE001
        log.warning("thermal evidence unreadable (%s) - temperature alerts stay WARNING", e)
        return out
    for r in rows:
        if len(r) >= 5 and r[0] and r[1]:
            try:
                out[(r[0], r[1].strip())] = EV.ThermalDay(int(float(r[2])), float(r[3]),
                                                          float(r[4]) if r[4] else None,
                                                          int(float(r[5])) if len(r) > 5 and r[5] else 0)
            except ValueError:
                continue
    return out


def _read_string_evidence(date_iso: str) -> Dict[Tuple[str, str], dict]:
    """string_daily (kind='string') for the day and its trailing baseline:
    per inverter {"today": [(channel, share)], "base": {channel: [share…]}}
    - each string against its own history (v220)."""
    from argia.store.pgq import psql_rows
    out: Dict[Tuple[str, str], dict] = {}
    try:
        rows = psql_rows("SELECT plant_key, inverter_sn, prod_date::text, channel, share FROM string_daily"
                         f" WHERE prod_date BETWEEN DATE '{date_iso}' - {STRING_BASELINE_DAYS}"
                         f" AND DATE '{date_iso}' AND kind = 'string';")
    except Exception as e:  # noqa: BLE001
        log.warning("string evidence unreadable (%s) - string flags without current data", e)
        return out
    for r in rows:
        if len(r) >= 5 and r[0] and r[1]:
            try:
                share = float(r[4]) if r[4] not in (None, "") else None
            except ValueError:
                continue
            ent = out.setdefault((r[0], r[1].strip()), {"today": [], "base": {}})
            if r[2] == date_iso:
                ent["today"].append((r[3], share))
            else:
                ent["base"].setdefault(r[3], []).append(share)
    return out


def string_candidate_with_evidence(b, readings, string_rows) -> Candidate:
    """v220: a new string-diagnostic bit is a WARNING only when the day's
    data shows a loss (a string far below its siblings, or the inverter
    below its plant peers); otherwise INFO - kept in the ledger and on
    the portal, never mailed. The message carries the numbers."""
    ratio = EV.peer_ratio(readings, b.plant_key, b.inverter_sn)
    ent = string_rows.get((b.plant_key, b.inverter_sn)) or {"today": [], "base": {}}
    weak, judged = EV.weak_strings(ent["today"], ent["base"])
    sev, evidence = EV.string_severity(ratio, weak, judged)
    c = candidate_from_string_breach(b)
    msg = c.message.rsplit(" [", 1)[0] + f" - {evidence} [{sev}]"
    return Candidate(alert_key=c.alert_key, plant_key=c.plant_key, inverter_sn=c.inverter_sn,
                     metric=c.metric, severity=sev, value=None if ratio is None else round(ratio, 3),
                     threshold=None, message=msg)


def daily_temp_candidates(bundle, portfolio,
                          open_keys=frozenset(), evidence=None) -> List[Candidate]:
    """Daily owner of inverter_temp_high: fires on the day's MAX temperature,
    so an acute-opened alert resolves once a full day stays below the
    CLEAR level (60 degC) - below WARN alone is not enough for a key in
    ``open_keys`` (the ledger's OPEN temperature alerts)."""
    from argia.core.alerts_state import make_inverter_alert_key
    out: List[Candidate] = []
    for plant in portfolio.active_plants():
        peak: Dict[str, float] = {}
        for r in bundle.rows_for_plant(plant.plant_key):
            if r.temperature_c is not None:
                sn = str(r.inverter_sn).strip()
                peak[sn] = max(peak.get(sn, -999.0), float(r.temperature_c))
        for sn, t in sorted(peak.items()):
            key = make_inverter_alert_key(plant.plant_key, sn,
                                          "inverter_temp_high")
            if t < TEMP_WARN_C:
                if key in open_keys and t >= TEMP_CLEAR_C:
                    out.append(Candidate(
                        alert_key=key, plant_key=plant.plant_key,
                        inverter_sn=sn, metric="inverter_temp_high",
                        severity="WARNING", value=round(t, 1),
                        threshold=TEMP_WARN_C,
                        message=(f"{plant.plant_key} {sn}: day-peak temperature "
                                 f"{t:.1f} degC - still above the clear level "
                                 f"{TEMP_CLEAR_C:.0f} [WARNING]"),
                    ))
                continue
            # v220: CRITICAL only with the nightly thermal evidence of a loss
            sev, ev_txt = EV.thermal_day_severity(t, TEMP_HIGH_C, (evidence or {}).get((plant.plant_key, sn)))
            crit = sev == "CRITICAL"
            out.append(Candidate(
                alert_key=key,
                plant_key=plant.plant_key, inverter_sn=sn,
                metric="inverter_temp_high",
                severity=sev,
                value=round(t, 1), threshold=TEMP_HIGH_C if crit else TEMP_WARN_C,
                message=(f"{plant.plant_key} {sn}: day-peak temperature "
                         f"{t:.1f} degC - {ev_txt} [{sev}]"),
            ))
    return out


def silent_severity(kind: str, detected: str) -> str:
    """v305 (Tomasz: mail where we are losing money, flag the rest): only
    "the unit was OFF" - its counter grew clearly less than its siblings'
    across the gap - is a measured loss (CRITICAL). "Kept producing" (the
    counter climbed), "no counter to confirm" and "siblings too low to
    compare" are missing data: INFO, on the portal; grading.escalate_blind
    raises them after BLIND_DAYS days. Pure."""
    return detected if kind == "off" else "INFO"


STOPPED_EARLY_MIN = 45
"""v305: an inverter whose last reading of the day is this much earlier
than the plant's last production lost its connection (its day is cut
short) - its day counter is partial, so comparing it with the peers
would book a loss that is only missing data (TAM1 JNMAE5X00K, 30 Sep:
"26% of peers" while it went silent at 11:31 and its peers reported to
19:12)."""


def stopped_early(bundle, portfolio, min_gap: int = STOPPED_EARLY_MIN) -> Dict[Tuple[str, str], Tuple[dt.datetime, dt.datetime]]:
    """{(plant, sn): (its last reading, the plant's last production)} for
    inverters whose last reading of the day is ``min_gap`` minutes or more
    before the last moment the plant produced (any inverter above 0 W).
    Rows without power are not readings; an inverter that powers down at
    sunset with its datalogger is NOT cut short - only before the end of
    production counts. Pure."""
    out = {}
    for plant in portfolio.active_plants():
        last: Dict[str, dt.datetime] = {}
        prod_end = None
        for r in bundle.rows_for_plant(plant.plant_key):
            if r.power_w is None:
                continue
            sn = str(r.inverter_sn).strip()
            if sn not in last or r.timestamp_utc > last[sn]:
                last[sn] = r.timestamp_utc
            if r.power_w > 0 and (prod_end is None or r.timestamp_utc > prod_end):
                prod_end = r.timestamp_utc
        if prod_end is None or len(last) < 2:
            continue
        for sn, t in last.items():
            if (prod_end - t) >= dt.timedelta(minutes=min_gap):
                out[(plant.plant_key, sn)] = (t, prod_end)
    return out


CUT_OFF_MIN = 60
CUT_OFF_POWER_SHARE = 0.05
"""v305: a plant whose readings END while it is still producing (at least
5 % of its kWp) an hour or more before the fleet's production ends lost
its connection (or the whole site went dark with its logger): its day
counter is partial, so its day energy is unconfirmed - SAG 20, 26 and 29
Sep were mailed as CRITICAL "produced 264 kWh vs 2,251 expected (12%)"
while the next night's lifetime counter proved a normal day."""


def cut_off_plants(bundle, portfolio, min_gap: int = CUT_OFF_MIN,
                   share: float = CUT_OFF_POWER_SHARE) -> Dict[str, Tuple[dt.datetime, float]]:
    """{plant: (last reading, kW at that moment)} for plants whose data
    stops while producing, ``min_gap`` minutes or more before the median
    end of production across the fleet. Pure."""
    last: Dict[str, dt.datetime] = {}
    prod_end: Dict[str, dt.datetime] = {}
    inv_last: Dict[str, Dict[str, Tuple[dt.datetime, float]]] = {}
    kwp = {p.plant_key: float(getattr(p, "kwp_dc", 0) or 0) for p in portfolio.active_plants()}
    for pk in kwp:
        for r in bundle.rows_for_plant(pk):
            if r.power_w is None:
                continue
            sn = str(r.inverter_sn).strip()
            if pk not in last or r.timestamp_utc > last[pk]:
                last[pk] = r.timestamp_utc
            if r.power_w > 0 and (pk not in prod_end or r.timestamp_utc > prod_end[pk]):
                prod_end[pk] = r.timestamp_utc
            seen = inv_last.setdefault(pk, {}).get(sn)
            if seen is None or r.timestamp_utc > seen[0]:
                inv_last[pk][sn] = (r.timestamp_utc, float(r.power_w))
    if len(prod_end) < 2:
        return {}
    fleet_end = sorted(prod_end.values())[len(prod_end) // 2]
    out = {}
    for pk, t in last.items():
        if (fleet_end - t) < dt.timedelta(minutes=min_gap) or not kwp.get(pk):
            continue
        # each inverter's last reading, if it is within 10 min of the plant's last
        kw = sum(p for ts, p in inv_last.get(pk, {}).values() if t - ts <= dt.timedelta(minutes=10)) / 1000.0
        if kw >= share * kwp[pk]:
            out[pk] = (t, kw)
    return out


def unconfirmed_energy(cands: List[Candidate], cut_off: Dict[str, Tuple[dt.datetime, float]]) -> List[Candidate]:
    """Plant-day energy verdicts of a cut-off plant are a flag (INFO), not a
    loss: the counter decides once it reports again (loss_daily's catch-up).
    Pure."""
    import dataclasses
    from argia.core.time_utils import MX_TZ
    out = []
    for c in cands:
        if c.metric in ("energy_daily_pct", "plant_twin_yield") and c.plant_key in cut_off:
            t, kw = cut_off[c.plant_key]
            c = dataclasses.replace(c, severity="INFO", message=(
                f"{c.message} - its readings stopped at {t.astimezone(MX_TZ):%H:%M} MX while producing"
                f" {kw:.0f} kW (lost connection?): energy unconfirmed until the counter reports [INFO]"))
        out.append(c)
    return out


def daily_silent_candidates(bundle, portfolio, date_iso: str) -> List[Candidate]:
    """Daily owner of inverter_silent (v203): every daylight gap of one
    inverter while its siblings produced, classified through the vendor
    counter - comms-only (WARNING) or the unit was OFF (CRITICAL). v223:
    fleet-wide blanks (the collector) are excluded first."""
    from argia.analytics.silent import collector_windows, evaluate_silent_gaps
    from argia.core.alerts_state import make_inverter_alert_key
    from argia.core.time_utils import MX_TZ
    y, m, d = (int(x) for x in date_iso.split("-"))
    day_end = dt.datetime(y, m, d, 20, 0, tzinfo=MX_TZ).astimezone(UTC)
    windows = collector_windows({p.plant_key: [r.timestamp_utc for r in bundle.rows_for_plant(p.plant_key)]
                                 for p in portfolio.active_plants()})
    for a, b in windows:
        log.info("collector blank %s-%s MX (fleet-wide) - inverter gaps inside it are not alerts",
                 a.astimezone(MX_TZ).strftime("%H:%M"), b.astimezone(MX_TZ).strftime("%H:%M"))
    out: List[Candidate] = []
    for plant in portfolio.active_plants():
        invs = portfolio.inverters_for(plant.plant_key)
        rated = {i.inverter_sn: float(i.rated_kw or 0) for i in invs}
        rows = [(r.timestamp_utc, str(r.inverter_sn).strip(), r.etoday_kwh, r.power_w)
                for r in bundle.rows_for_plant(plant.plant_key)]
        for b in evaluate_silent_gaps(plant.plant_key, rows, rated, day_end,
                                      configured=[i.inverter_sn for i in invs], collector=windows):
            out.append(Candidate(
                alert_key=make_inverter_alert_key(plant.plant_key, b.inverter_sn,
                                                  "inverter_silent"),
                plant_key=plant.plant_key, inverter_sn=b.inverter_sn,
                metric="inverter_silent", severity=silent_severity(b.kind, b.severity.value),
                value=b.gap_min, threshold=None, message=b.message))
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


def _ticket_briefs():
    """v226: {alert_key: TicketBrief} of OPEN maintenance tickets; {} when
    the module or the tables are not there (the mail then carries the
    warning itself)."""
    try:
        from argia.maintenance import tickets as TK
        return TK.load_open_briefs()
    except Exception as e:  # noqa: BLE001
        log.warning("ticket briefs unavailable (%s)", e)
        return {}


def _open_tickets():
    """v227: the OPEN tickets themselves (asset matching, verification);
    [] when the tables are not there."""
    try:
        from argia.maintenance import tickets as TK
        from argia.store.pgq import psql_csv
        return [TK.ticket_from_row(r) for r in TK.rows_from_csv(psql_csv(
            TK.SELECT_TICKETS + " WHERE status IN ('NEW','IN_PROGRESS','WAITING','VERIFICATION');"))]
    except Exception as e:  # noqa: BLE001
        log.warning("open tickets unavailable (%s)", e)
        return []


def _ticket_notify(t, what, detail=""):
    """Tell the ticket's participants what the data decided (fail-soft)."""
    try:
        from argia.alerts import naming
        from argia.maintenance import notify as NOTIFY
        n = naming.load_names()
        email_of, name_of = NOTIFY.account_lookups()
        NOTIFY.send(t, "monitoring", what, detail, email_of, name_of, n.plant(t.plant_key),
                    n.inverter(t.plant_key, t.inverter_sn) if t.inverter_sn else "",
                    watchers=NOTIFY.status_watchers(t.plant_key))      # v303: a status change
    except Exception as e:  # noqa: BLE001
        log.warning("ticket notification failed: %s", e)


def _attach_to_tickets(records, open_tickets, dry_run: bool = False) -> int:
    """v226/v227: an alert opened or touched today that belongs to an open
    ticket - by alert_key, or by the ASSET (same plant + inverter, or the
    plant itself for a plant-level alert) - is an occurrence on that
    ticket: counted in ticket_alert (which links it from now on) and
    written to the timeline as an 'alert' event (actor 'monitoring'). So
    once a ticket exists for an inverter, its warnings are never repeated
    in the mail - the ticket's progress is."""
    if not open_tickets:
        return 0
    from argia.maintenance import tickets as TK
    from argia.store.pgq import psql_exec
    by_key = {k: t for t in open_tickets for k in t.alert_keys}
    n = 0
    for r in records:
        t = by_key.get(r.alert_key) or TK.matching_ticket(open_tickets, r.plant_key, r.inverter_sn or "")
        if t is None:
            continue
        log.info("TICKET   %s  %s -> %s", r.alert_id, r.alert_key, t.number)
        if dry_run:
            n += 1
            continue
        try:
            psql_exec(TK.link_alert_sql(t.id, r.alert_key)
                      + TK.event_sql(t.id, "monitoring", "alert", r.message[:400], {"alert_id": r.alert_id}))
            if r.alert_key not in t.alert_keys:
                t.alert_keys.append(r.alert_key)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("could not attach %s to %s: %s", r.alert_id, t.number, e)
    return n


def _verify_tickets(open_tickets, ledger_records, touched_records, now_utc, dry_run: bool = False) -> int:
    """v227: the last step before closing comes from the data. For every
    ticket in VERIFICATION with linked alerts: an alert seen again today
    sends it back to In progress; none open and none seen for
    tickets.VERIFY_QUIET_DAYS marks it Resolved. Both are timeline events
    by 'monitoring' and a mail to the participants."""
    from argia.maintenance import tickets as TK
    from argia.store.pgq import psql_csv, psql_exec
    cands = [t for t in open_tickets if t.status == "VERIFICATION" and t.alert_keys]
    if not cands:
        return 0
    open_keys = {r.alert_key for r in ledger_records if r.is_open()}
    touched = {r.alert_key for r in touched_records}
    last_seen = {}
    try:
        for r in TK.rows_from_csv(psql_csv("SELECT alert_key, max(last_seen)::text AS seen FROM ticket_alert GROUP BY 1;")):
            ts = TK.parse_ts(r.get("seen") or "")
            if ts is not None:
                last_seen[r["alert_key"]] = ts
    except Exception as e:  # noqa: BLE001
        log.warning("ticket_alert last_seen unavailable (%s)", e)
    n = 0
    for t in cands:
        to, reason = TK.verify_decision(t, open_keys, touched, last_seen, now_utc)
        log.info("VERIFY   %s  %s", t.number, reason or "nothing to decide")
        if to is None or dry_run:
            continue
        try:
            psql_exec(TK.status_sql(t.id, to) + TK.event_sql(t.id, "monitoring", "status", reason, {"from": t.status, "to": to}))
            old = t.status
            t.status = to
            _ticket_notify(t, f"Status: {TK.STATUS_LABEL.get(old, old)} → {TK.STATUS_LABEL.get(to, to)} (by the data)", reason)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("could not update %s: %s", t.number, e)
    return n


@instrument("alerts_daily")
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--date", default=None,
                        help="ISO date to evaluate (default: yesterday MX)")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and print; write nothing")
    args = parser.parse_args(argv)

    date_iso = args.date or (now_mx().date() - dt.timedelta(days=1)).isoformat()

    try:
        sheets = open_sheets()          # v199: NullSheets once retired
        portfolio = load_portfolio(sheets)
    except Exception as e:  # noqa: BLE001
        log.error("bootstrap failed: %s", e)
        return 3

    # --- inverter daily energies from telemetry ---
    bundle = without_frozen(read_day_bundle(sheets, date_iso))
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
        # None when the day had too little data for that inverter - skip
        # those rather than feeding the detector a fake 0 (an inverter
        # with NO data is a data-quality problem, not "producing zero").
        for sn, eday in compute_plant_energy(rows).items():
            if eday.energy_kwh is None:
                log.info("[%s] %s: no computable energy for %s - skipped",
                         plant.plant_key, sn, date_iso)
                continue
            readings.append(InverterReading(
                plant_key=plant.plant_key, inverter_sn=sn,
                value=eday.energy_kwh, rated_kw=rated.get(sn),
            ))
    if not readings:
        log.warning("no telemetry for %s - nothing evaluated", date_iso)
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

    # --- string flags: telemetry_detail, day vs trailing baseline (v207) ---
    string_day, string_baseline = _read_string_samples(portfolio, date_iso)

    # --- plant-level aggregates from KPI_Daily (stamped by kpi_eod) ---
    kpi = _read_kpi_day(sheets, date_iso)
    log.info("Evaluating %s: %d inverter readings, %d KPI plant rows",
             date_iso, len(readings), len(kpi))

    # the ledger is loaded before the candidates: the temperature rule
    # needs to know which alerts are OPEN (hysteresis, v202)
    create_alerts_tab_if_missing(sheets)
    ledger = load_alerts_ledger(sheets)
    open_temp_keys = frozenset(
        r.alert_key for r in ledger.records
        if r.metric == "inverter_temp_high" and (r.is_open() or r.is_silenced()))
    early = stopped_early(bundle, portfolio)
    for (pk, sn), (t, peers_t) in sorted(early.items()):
        log.info("[%s] %s stopped reporting at %s UTC, the plant produced until %s - day counter partial,"
                 " not compared with its peers (missing data, not a loss)", pk, sn,
                 t.strftime("%H:%M"), peers_t.strftime("%H:%M"))
    candidates = build_candidates(
        readings, kpi, fault_samples, string_day, string_baseline, stale,
        temp_candidates=daily_temp_candidates(bundle, portfolio, open_temp_keys,
                                              evidence=_read_thermal_evidence(date_iso)),
        offline_candidates=(daily_offline_candidates(readings)
                            + daily_silent_candidates(bundle, portfolio, date_iso)),
        string_rows=_read_string_evidence(date_iso),
        relative_skip=frozenset(early),
    )
    # v303: one severity policy for both tiers (argia.alerts.grading) -
    # a flag without a measured loss is INFO: portal only, never mailed
    candidates = grading.grade_all(candidates, tier="daily")
    cut_off = cut_off_plants(bundle, portfolio)
    for pk, (t, kw) in sorted(cut_off.items()):
        log.info("[%s] readings stopped at %s UTC while producing %.0f kW - day energy unconfirmed (flag, not a loss)",
                 pk, t.strftime("%H:%M"), kw)
    candidates = unconfirmed_energy(candidates, cut_off)
    # v305 last resort: blind for BLIND_DAYS days in a row -> one WARNING
    open_since = {}
    for r in ledger.records:
        if r.is_open() and r.metric in grading.POTENTIAL:
            try:
                t0 = dt.datetime.fromisoformat(r.opened_utc)
                open_since[r.alert_key] = t0 if t0.tzinfo else t0.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                pass
    candidates = grading.escalate_blind(candidates, open_since, dt.datetime.now(UTC))
    for c in candidates:
        log.info("CANDIDATE [%s] %s", c.severity, c.message)
    if not candidates:
        log.info("no breaches today")

    # v92: suppress plant-level "down / underproducing" candidates for
    # plants in a logged maintenance window - the plant does not open (or
    # re-open) a critical; the daily report shows a maintenance badge
    # instead. Approval-independent: a logged window (draft or approved)
    # means the operator already knows.
    events = load_maintenance_events(sheets)
    maint = plant_maintenance_on_date(events, date_iso)
    if maint:
        candidates, suppressed = apply_maintenance_suppression(
            candidates, maint)
        for c in suppressed:
            log.info("SUPPRESS(maint) [%s] %s %s", c.severity,
                     c.plant_key, c.metric)
        if suppressed:
            log.info("Suppressed %d candidate(s) for %d plant(s) under "
                     "maintenance: %s", len(suppressed), len(maint),
                     ", ".join(sorted(maint)))

    # --- reconcile against ledger ---
    result = reconcile_alerts(ledger, candidates, dt.datetime.now(UTC))
    log.info("Reconcile: %s", result.summary())
    for r in result.opened:
        log.info("OPEN     %s  %s", r.alert_id, r.message)
    for r in result.touched:
        log.info("TOUCH    %s  %s", r.alert_id, r.alert_key)
    for r in result.resolved:
        log.info("RESOLVE  %s  %s", r.alert_id, r.alert_key)

    # --- the daily digest pseudo-alert is retired (v223) ---------------
    # "Silence means all clear" now lives inside the morning mail as the
    # "still open" section; any digest row left open is closed here.
    now_utc = dt.datetime.now(UTC)
    digest = resolve_digest_rows(result.records, now_utc)
    for line in digest.log_lines():
        log.info("%s", line)

    # v196/v223: the ONE morning mail - new WARNING/CRITICAL alerts grouped
    # plant -> issue -> inverters, the still-open reminder, explanations
    # once per issue type. Mailed records come back with 'email' in
    # channels_sent.
    from argia.alerts.ledger_mail import mail_new_alerts
    open_tickets = _open_tickets()
    _attach_to_tickets(result.opened + result.touched, open_tickets, dry_run=args.dry_run)
    _verify_tickets(open_tickets, result.records, result.opened + result.touched, now_utc, dry_run=args.dry_run)
    tickets = _ticket_briefs()            # after the attach: newly linked keys count as in hand
    records = mail_new_alerts(result.records, dry_run=args.dry_run, morning=True,
                              when_mx=now_mx().strftime("%Y-%m-%d %H:%M"), now_utc=now_utc, tickets=tickets)
    mailed = records != list(result.records)

    if args.dry_run:
        log.info("[DRY RUN] no rows written")
        return 0

    # --- persist: rewrite the data region in ledger order ---
    # Rows only ever update in place or append (history never shrinks), so a
    # single block write of all records is idempotent and race-free for a
    # once-a-day job.
    if result.opened or result.touched or result.resolved or digest.changed \
            or mailed:
        n = write_ledger(sheets, records)
        log.info("Wrote %d alert row(s) to the Alerts ledger", n)
    else:
        log.info("ledger unchanged - nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
