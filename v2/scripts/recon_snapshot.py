"""Nightly vendor-counter snapshot + daily reconciliation (pio06 only).

Runs at 23:50 MX (generation window is 05:00-21:00, so the day's
counters are final). For every active plant it captures the vendor's own
daily / monthly / lifetime energy counters into
``vendor_counter_snapshot`` — the immutable audit trail the monthly
billing control rides on — then (re)computes ``reconciliation_daily``
for the last N days so late KPI rows and healed telemetry are picked up.

Requires ARGIA_PG_MIRROR=1 (the server env); exits 0 quietly elsewhere.
SolarEdge cost: 3 API requests per site per night — far inside quota.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

from argia.core.config import PlantConfig, load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import MX_TZ
from argia.recon import backfill as B
from argia.recon import counters as C
from argia.recon import engine as E
from argia.recon import perf as P
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.recon_snapshot")

# Expected 5-min ticks per MX production day, per vendor (the systemd
# timers): Growatt/Huawei */5 05:00-21:00 = 193; SolarEdge 3/h 06-20 = 45.
EXPECTED_TICKS = {"GROWATT": 193, "HUAWEI": 193, "SOLAREDGE": 45, "SMA": 193}

MX_DATE_SQL = "(ts_utc AT TIME ZONE 'America/Mexico_City')::date"


def _today_mx() -> dt.date:
    return dt.datetime.now(MX_TZ).date()


def now_mx() -> dt.datetime:
    return dt.datetime.now(MX_TZ)


RECON_TODAY_FROM_HOUR = 22
"""Reconciling TODAY before the day is over produces garbage rows
(midday vendor counter vs midday interval sum — the 2026-08-26 false
FAIL). The nightly timer fires 23:50 MX; a manual daytime run starts
from yesterday instead."""


def recon_dates(base: dt.date, days_back: int, hour_mx: int,
                today: dt.date = None) -> List[str]:
    """The dates one run reconciles, newest first. ``base`` (and only
    ``base``) is skipped when it IS today and the MX clock says the day
    is still in progress — historical dates are always fair game."""
    today = today or _today_mx()
    out = []
    for back in range(days_back):
        d = base - dt.timedelta(days=back)
        if d == today and hour_mx < RECON_TODAY_FROM_HOUR:
            continue
        out.append(d.isoformat())
    return out


# ---------------------------------------------------------------------------
# Counter capture, one function per vendor. Each degrades to note-only —
# a vendor outage must never sink the whole snapshot run.
# ---------------------------------------------------------------------------
def snap_growatt(plants: List[PlantConfig], snap_date: str
                 ) -> List[C.CounterSnapshot]:
    out: List[C.CounterSnapshot] = []
    if not plants:
        return out
    user = os.environ.get("GROWATT_USERNAME", "").strip()
    pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not user or not pwd:
        LOG.warning("growatt: no credentials — skipped")
        return out
    from argia.vendors.growatt_web import GrowattWebClient
    client = GrowattWebClient(username=user, password=pwd)
    try:
        client.login()
    except Exception as e:  # noqa: BLE001
        LOG.error("growatt login failed: %s", e)
        return [C.CounterSnapshot(p.plant_key, "GROWATT", snap_date,
                                  None, None, None, f"login failed: {e}")
                for p in plants]
    for p in plants:
        pid = (p.site_id or p.weather_plant_id or "").strip()
        if not pid:
            out.append(C.CounterSnapshot(p.plant_key, "GROWATT", snap_date,
                                         None, None, None, "no plant id"))
            continue
        try:
            day, month, life = C.growatt_counters(
                client.get_max_total_data(pid))
            out.append(C.CounterSnapshot(p.plant_key, "GROWATT", snap_date,
                                         day, month, life))
        except Exception as e:  # noqa: BLE001
            LOG.error("growatt %s: %s", p.plant_key, e)
            out.append(C.CounterSnapshot(p.plant_key, "GROWATT", snap_date,
                                         None, None, None, str(e)[:200]))
    return out


def snap_huawei(plants: List[PlantConfig], snap_date: str
                ) -> List[C.CounterSnapshot]:
    out: List[C.CounterSnapshot] = []
    if not plants:
        return out
    user = os.environ.get("HUAWEI_USERNAME", "").strip()
    pwd = os.environ.get("HUAWEI_PASSWORD", "").strip()
    if not user or not pwd:
        LOG.warning("huawei: no credentials — skipped")
        return out
    from argia.vendors.huawei import HuaweiClient
    client = HuaweiClient(username=user, password=pwd)
    try:
        client.login()
        codes = ",".join(p.site_id for p in plants if p.site_id)
        result = client._post_json("/getStationRealKpi",
                                   {"stationCodes": codes})
        by_code = C.huawei_station_counters(result)
    except Exception as e:  # noqa: BLE001
        LOG.error("huawei snapshot failed: %s", e)
        return [C.CounterSnapshot(p.plant_key, "HUAWEI", snap_date,
                                  None, None, None, f"fetch failed: {e}")
                for p in plants]
    for p in plants:
        day, month, life = by_code.get(str(p.site_id), (None, None, None))
        note = "" if str(p.site_id) in by_code else "station not in response"
        out.append(C.CounterSnapshot(p.plant_key, "HUAWEI", snap_date,
                                     day, month, life, note))
    return out


def snap_solaredge(plants: List[PlantConfig], snap_date: str
                   ) -> List[C.CounterSnapshot]:
    out: List[C.CounterSnapshot] = []
    from argia.vendors.solaredge import SolarEdgeClient
    first_of_month = snap_date[:8] + "01"
    for p in plants:
        key = os.environ.get(p.secret_api_name or "", "").strip()
        if not key or not p.site_id:
            out.append(C.CounterSnapshot(p.plant_key, "SOLAREDGE", snap_date,
                                         None, None, None,
                                         "no api key or site id"))
            continue
        client = SolarEdgeClient(api_key=key)
        day = month = life = None
        notes = []
        try:
            day = C.solaredge_energy_kwh(client._get_json(
                f"/site/{p.site_id}/energy",
                {"timeUnit": "DAY", "startDate": snap_date,
                 "endDate": snap_date}))
            month = C.solaredge_energy_kwh(client._get_json(
                f"/site/{p.site_id}/energy",
                {"timeUnit": "MONTH", "startDate": first_of_month,
                 "endDate": snap_date}))
            life = C.solaredge_lifetime_kwh(client._get_json(
                f"/site/{p.site_id}/overview", {}))
        except Exception as e:  # noqa: BLE001
            LOG.error("solaredge %s: %s", p.plant_key, e)
            notes.append(str(e)[:200])
        out.append(C.CounterSnapshot(p.plant_key, "SOLAREDGE", snap_date,
                                     day, month, life, "; ".join(notes)))
    return out


# ---------------------------------------------------------------------------
# Daily reconciliation from PG.
# ---------------------------------------------------------------------------
def _f(s: str) -> Optional[float]:
    try:
        return float(s) if s != "" else None
    except ValueError:
        return None


def interval_by_plant(date_iso: str) -> Dict[str, Tuple[float, int]]:
    """{plant: (Σ max(etoday) over inverters, distinct 5-min ticks)}."""
    rows = psql_rows(
        "SELECT t.plant_key, sum(t.m), max(t.ticks) FROM ("
        " SELECT plant_key, inverter_sn, max(etoday_kwh) AS m,"
        "  count(DISTINCT date_trunc('minute',"
        "   ts_utc AT TIME ZONE 'America/Mexico_City')) AS ticks"
        f" FROM telemetry WHERE {MX_DATE_SQL} = DATE '{date_iso}'"
        " GROUP BY 1, 2) t GROUP BY 1;")
    out: Dict[str, Tuple[float, int]] = {}
    for r in rows:
        if len(r) >= 3 and r[0]:
            out[r[0]] = (_f(r[1]) or 0.0, int(_f(r[2]) or 0))
    return out


def stored_daily(date_iso: str) -> Dict[str, Tuple[Optional[float],
                                                   Optional[float]]]:
    """{plant: (vendor_daily_kwh from snapshot, kpi energy_kwh)}."""
    snaps = {r[0]: _f(r[1]) for r in psql_rows(
        "SELECT plant_key, daily_kwh FROM vendor_counter_snapshot "
        f"WHERE snap_date = DATE '{date_iso}';") if len(r) >= 2}
    kpi = {r[0]: _f(r[1]) for r in psql_rows(
        "SELECT plant_key, energy_kwh FROM daily_production "
        f"WHERE prod_date = DATE '{date_iso}';") if len(r) >= 2}
    return {pk: (snaps.get(pk), kpi.get(pk))
            for pk in set(snaps) | set(kpi)}


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _num(v: Optional[float]) -> str:
    return "NULL" if v is None else f"{v:.3f}"


def inverter_coverage(date_iso: str) -> Dict[str, Tuple[int, int]]:
    """{plant: (reporting, configured)} for one MX date — configured
    ACTIVE inverters vs those with any usable sample. Feeds the
    completeness scaling (a comms-dead inverter guarantees an interval
    undercount that perfect ticks can't see)."""
    out: Dict[str, Tuple[int, int]] = {}
    for r in psql_rows(
            "SELECT i.plant_key, count(*),"
            " count(*) FILTER (WHERE EXISTS (SELECT 1 FROM telemetry t"
            "  WHERE t.inverter_sn = i.inverter_sn"
            f"  AND {MX_DATE_SQL} = DATE '{date_iso}'"
            "  AND (t.etoday_kwh IS NOT NULL OR t.power_w IS NOT NULL)))"
            " FROM inverter i WHERE i.active GROUP BY 1;"):
        if len(r) >= 3:
            try:
                out[r[0]] = (int(r[2]), int(r[1]))
            except ValueError:
                continue
    return out


def reconcile_day(date_iso: str, brand_by_plant: Dict[str, str],
                  dry_run: bool) -> int:
    """(Re)compute reconciliation_daily for one date. Returns row count."""
    interval = interval_by_plant(date_iso)
    stored = stored_daily(date_iso)
    coverage = inverter_coverage(date_iso)
    plants = sorted(set(interval) | set(stored) | set(brand_by_plant))
    values = []
    for pk in plants:
        ikwh, ticks = interval.get(pk, (None, 0))
        vendor_daily, kpi = stored.get(pk, (None, None))
        expected = EXPECTED_TICKS.get(brand_by_plant.get(pk, ""), 193)
        completeness = round(100.0 * ticks / expected, 2) if expected else None
        if completeness is not None:
            completeness = min(completeness, 100.0)
        rep, conf = coverage.get(pk, (None, None))
        completeness = E.effective_completeness(completeness, rep, conf)
        if (rep is not None and conf and rep < conf):
            LOG.info("recon %s %s: %d/%d configured inverters reported"
                     " — completeness scaled to %s", date_iso, pk,
                     rep, conf, completeness)
        r = E.daily_recon(ikwh, vendor_daily, kpi, completeness)
        LOG.info("recon %s %s: %s (%s)", date_iso, pk, r.status, r.note)
        # Self-heal: a KPI day missing or below the vendor counter is
        # filled/raised (never lowered) so a later sheet re-sync cannot
        # re-introduce an undercount. Provenance lands in status_note.
        if not dry_run and vendor_daily is not None:
            cls, _delta = B.classify_day(kpi, vendor_daily)
            if cls in (B.CLASS_MISSING, B.CLASS_UNDER):
                psql_exec(B.build_fix_sql(pk, date_iso, vendor_daily, kpi))
                LOG.info("recon %s %s: daily_production %s -> corrected "
                         "to vendor %.1f kWh", date_iso, pk, cls,
                         vendor_daily)
        values.append(
            f"({_txt(pk)}, DATE '{date_iso}', {_num(r.interval_kwh)},"
            f" {_num(r.vendor_daily_kwh)}, {_num(r.kpi_kwh)},"
            f" {_num(r.completeness_pct)}, {_num(r.variance_pct)},"
            f" {_txt(r.status)}, {_txt(r.note)})")
    if not values or dry_run:
        return 0
    psql_exec(
        "INSERT INTO reconciliation_daily (plant_key, prod_date,"
        " interval_kwh, vendor_daily_kwh, kpi_kwh, completeness_pct,"
        " variance_pct, status, note) VALUES\n" + ",\n".join(values) +
        "\nON CONFLICT (plant_key, prod_date) DO UPDATE SET"
        " interval_kwh=EXCLUDED.interval_kwh,"
        " vendor_daily_kwh=EXCLUDED.vendor_daily_kwh,"
        " kpi_kwh=EXCLUDED.kpi_kwh,"
        " completeness_pct=EXCLUDED.completeness_pct,"
        " variance_pct=EXCLUDED.variance_pct, status=EXCLUDED.status,"
        " note=EXCLUDED.note, checked_at=now();")
    return len(values)


def stamp_pr_stc(gamma_by_plant: Dict[str, Optional[float]],
                 window_days: int = 35, dry_run: bool = False) -> int:
    """Stamp daily_production.pr_stc (AGS-701 R2) for every plant-day in
    the window that has BOTH a KPI PR and measured irradiance-weighted
    module temperature. Idempotent; days without measurements stay NULL
    — a correction is computed from data or not at all."""
    temps = psql_rows(
        f"SELECT {MX_DATE_SQL}::text, plant_key,"
        " round((sum(irradiance_wm2 * module_temp_c)"
        "  / nullif(sum(irradiance_wm2), 0))::numeric, 2)"
        " FROM telemetry"
        " WHERE irradiance_wm2 > 50 AND module_temp_c IS NOT NULL"
        f" AND ts_utc > now() - interval '{window_days} days'"
        " GROUP BY 1, 2;")
    prs = {(r[1], r[0]): _f(r[2]) for r in psql_rows(
        "SELECT prod_date::text, plant_key, pr FROM daily_production"
        f" WHERE prod_date > current_date - {window_days};")
        if len(r) >= 3}
    values = []
    for r in temps:
        if len(r) < 3:
            continue
        d, pk, t_eff = r[0], r[1], _f(r[2])
        v = P.pr_stc(prs.get((pk, d)), t_eff, gamma_by_plant.get(pk))
        if v is not None:
            values.append(f"({_txt(pk)}, DATE '{d}', {v})")
    if not values or dry_run:
        LOG.info("PR_STC: %d plant-day(s) computable (dry_run=%s)",
                 len(values), dry_run)
        return 0
    psql_exec(
        "UPDATE daily_production dp SET pr_stc = v.val"
        " FROM (VALUES " + ", ".join(values) +
        ") AS v(pk, d, val)"
        " WHERE dp.plant_key = v.pk AND dp.prod_date = v.d"
        " AND (dp.pr_stc IS DISTINCT FROM v.val);")
    LOG.info("PR_STC stamped for %d plant-day(s)", len(values))
    return len(values)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="nightly counter snapshot "
                                     "+ daily reconciliation")
    parser.add_argument("--date", default=None,
                        help="snapshot date ISO (default: today MX)")
    parser.add_argument("--days-back", type=int, default=3,
                        help="re-reconcile this many trailing days")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-snapshot", action="store_true",
                        help="only recompute reconciliation_daily")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 not set")
        return 1
    portfolio = load_portfolio(SheetsClient(sheet_id=sheet_id))
    active = portfolio.active_plants()
    brand_by_plant = {p.plant_key: p.brand.upper() for p in active}
    snap_date = args.date or _today_mx().isoformat()

    if not args.skip_snapshot:
        snaps = []
        snaps += snap_growatt(
            [p for p in active if p.brand.upper() == "GROWATT"], snap_date)
        snaps += snap_huawei(
            [p for p in active if p.brand.upper() == "HUAWEI"], snap_date)
        snaps += snap_solaredge(
            [p for p in active if p.brand.upper() == "SOLAREDGE"], snap_date)
        for s in snaps:
            LOG.info("snapshot %s %s: day=%s month=%s lifetime=%s %s",
                     s.plant_key, s.vendor, s.daily_kwh, s.monthly_kwh,
                     s.lifetime_kwh, s.note)
        sql = C.build_snapshot_upsert_sql(snaps)
        if sql and not args.dry_run:
            psql_exec(sql)
            LOG.info("stored %d counter snapshots for %s", len(snaps),
                     snap_date)

    base = dt.date.fromisoformat(snap_date)
    total = 0
    for d in recon_dates(base, args.days_back, now_mx().hour):
        total += reconcile_day(d, brand_by_plant, args.dry_run)

    # Re-derive pr on the days the self-heal corrected: the stamped PR
    # still reflected the undercounted interval energy (2026-08-27
    # finding). Implausible results (>1.05: broken irradiance) go NULL.
    if not args.dry_run:
        try:
            psql_exec(B.build_pr_resync_sql())
        except RuntimeError as e:
            LOG.warning("pr resync failed (recon unaffected): %s", e)

    # AGS-701 R2: weather-normalized PR_STC wherever module temperature
    # was measured (whole telemetry window — heals late KPI arrivals)
    gamma_by_plant = {p.plant_key: p.gamma_pmax for p in active}
    try:
        stamp_pr_stc(gamma_by_plant, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        LOG.warning("PR_STC stamping failed (recon unaffected): %s", e)

    LOG.info("DONE: reconciliation rows upserted=%d dry_run=%s",
             total, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
