#!/usr/bin/env python3
"""Argia_Mont — End-of-day KPI archival.

Runs ONCE PER DAY, after the day's telemetry has fully landed. Typically
scheduled in cron around 01:30 MX (when even slow vendors have flushed).

Steps:
1. Load yesterday's Telemetry_Argia rows (MX local date)
2. For each active plant: compute energy, irradiance, PR, capacity factor
3. Upsert one row per (plant, yesterday) into KPI_Daily
4. Optionally prune rows older than 14 days

USAGE
    PYTHONPATH=. python scripts/kpi_eod.py
    PYTHONPATH=. python scripts/kpi_eod.py --date 2026-05-13
    PYTHONPATH=. python scripts/kpi_eod.py --dry-run
    PYTHONPATH=. python scripts/kpi_eod.py --prune
    PYTHONPATH=. python scripts/kpi_eod.py --prune-apply   # ACTUALLY DELETE

EXIT CODES
    0  ran cleanly, KPIs upserted
    1  partial — some plants had no data
    2  nothing written (no data anywhere)
    3  config error
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import logging
import os
import sys
from typing import Dict, List, Tuple

from argia.vendors import growatt_token
from argia.kpi.design import design_kwh_for_day, load_design_monthly
from argia.archive.kpi_daily import (
    AVAILABILITY_COL_NAME,
    CLOUD_COVERAGE_COL_NAME,
    SPECIFIC_YIELD_COL_NAME,
    EXPECTED_KWH_COL_NAME,
    PRODUCTION_PCT_COL_NAME,
    SOILING_LOSS_COL_NAME,
    STATUS_NOTE_COL_NAME,
    compute_availability,
    compute_expected_kwh,
    compute_production_pct,
    gated_production_pct,
    production_statement,
    compute_soiling_loss_pct,
    compute_specific_yield,
    HOT_WINDOW_DAYS,
    classify_coverage,
    create_kpi_daily_tab_if_missing,
    mean_cloud_cover,
    perf_to_row,
    prune_old_rows,
    stamp_column,
    stamp_data_class,
    upsert_kpi_rows,
)
from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx
from argia.kpi import (
    compute_plant_energy,
    compute_plant_pr,
    read_day_bundle,
)
from argia.kpi.irradiance import daily_irradiance_for_plant
from argia.kpi.performance import (
    GAMMA_PMAX_DEFAULT,
    irradiance_weighted_module_temp,
)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _yesterday_mx_iso() -> str:
    return (now_mx().date() - dt.timedelta(days=1)).isoformat()


LOG = logging.getLogger("argia.kpi_eod")


def try_dense_irradiance(web, plant, date_iso):
    """Best-effort dense ShineMaster history -> IrradianceDay, or None.

    Never allowed to break the KPI run: any fetch/parse problem logs a
    warning and returns None so the snapshot/cloud hybrid stands."""
    from argia.kpi.irradiance import integrate_history_points
    from argia.meteo.growatt_env import fetch_env_day_auto
    try:
        points, sn, addr = fetch_env_day_auto(
            web, plant.weather_plant_id, plant.datalogger_sn,
            plant.datalogger_addr, date_iso)
        LOG.info("[%s] dense fetch via device (%s, addr=%s): %d points",
                 plant.plant_key, sn, addr, len(points))
        result = integrate_history_points(points)
        if result.kwh_m2 is None:
            LOG.warning("[%s] dense irradiance unusable (%d samples) — "
                        "falling back", plant.plant_key,
                        result.samples_used)
            return None
        return result
    except Exception as e:  # noqa: BLE001 — best-effort by contract
        LOG.warning("[%s] dense irradiance fetch failed (%s) — falling "
                    "back", plant.plant_key, e)
        return None


def build_dense_web_client():
    """GrowattWebClient from env creds, or None (feature silently off)."""
    from argia.vendors.growatt_web import GrowattWebClient
    user = os.environ.get("GROWATT_USERNAME", "").strip()
    pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not (user and pwd):
        LOG.info("dense irradiance requested but GROWATT_USERNAME/"
                 "GROWATT_PASSWORD not set — skipping")
        return None
    client = GrowattWebClient(username=user, password=pwd)
    client.login()
    return client


from argia.core.job_log import instrument


@instrument("kpi_eod")
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--dense-irradiance", action="store_true",
        help="fetch dense ShineMaster history for plants with a "
             "datalogger_sn (best-effort; any problem falls back to the "
             "snapshot/cloud hybrid)")
    parser.add_argument(
        "--date", default=None,
        help="Local date YYYY-MM-DD (default: yesterday MX)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and log results, do not write to KPI_Daily",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help=f"Find rows older than {HOT_WINDOW_DAYS} days but DO NOT delete (preview)",
    )
    parser.add_argument(
        "--prune-apply", action="store_true",
        help=f"Actually delete rows older than {HOT_WINDOW_DAYS} days. DESTRUCTIVE.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    log = logging.getLogger("argia.kpi_eod")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 not set")
        return 3

    try:
        sheets = SheetsClient(sheet_id=sheet_id)
    except Exception as e:
        log.error("SheetsClient failed: %s", e)
        return 3

    # Bootstrap KPI_Daily if needed
    try:
        created = create_kpi_daily_tab_if_missing(sheets)
        if created:
            log.info("Created KPI_Daily tab")
    except Exception as e:
        log.warning("Could not bootstrap KPI_Daily: %s", e)

    try:
        portfolio = load_portfolio(sheets)
    except Exception as e:
        log.error("load_portfolio failed: %s", e)
        return 3

    date_iso = args.date or _yesterday_mx_iso()
    dense_web = build_dense_web_client() if args.dense_irradiance else None
    log.info("Computing EOD KPIs for date %s", date_iso)
    bundle = read_day_bundle(sheets, date_iso)

    new_rows: List = []
    coverage: Dict[Tuple[str, str], str] = {}
    cloud_stamps: Dict[Tuple[str, str], float] = {}
    expected_stamps: Dict[Tuple[str, str], float] = {}
    design_stamps: dict = {}
    design = load_design_monthly(sheets)
    avail_stamps: Dict[Tuple[str, str], float] = {}
    sy_stamps: Dict[Tuple[str, str], float] = {}
    prod_stamps: Dict[Tuple[str, str], float] = {}
    soil_stamps: Dict[Tuple[str, str], float] = {}
    note_stamps: Dict[Tuple[str, str], str] = {}
    plants_with_data = 0
    plants_without = 0
    for plant in portfolio.active_plants():
        rows = bundle.rows_for_plant(plant.plant_key)
        if not rows:
            log.info("[%s] no telemetry for %s — skipping",
                     plant.plant_key, date_iso)
            plants_without += 1
            continue
        plants_with_data += 1

        # rows are time-sorted ascending, so the last one is the day's newest
        # sample. Coverage keys off it: EToday is cumulative, so a late sample
        # means the daily MAX is trustworthy.
        coverage[(date_iso, plant.plant_key)] = classify_coverage(
            rows[-1].timestamp_utc
        )

        # Daylight-mean cloud cover for the day (same 0-1 scale as v1's
        # Cloud_Coverage). None (no usable samples) -> no stamp for this plant.
        cc = mean_cloud_cover(
            [(r.timestamp_utc, r.cloud_cover_pct) for r in rows]
        )
        if cc is not None:
            cloud_stamps[(date_iso, plant.plant_key)] = cc

        energy_by_inv = compute_plant_energy(rows)
        # 2026-07-07: if the Growatt web block left the day without
        # telemetry, use the token-API energy telemetry cached intraday
        # (plant-level; coverage/data_class stays honest about detail).
        energy_by_inv, token_used = growatt_token.apply_energy_fallback(
            energy_by_inv, date_iso, plant.plant_key)
        if token_used:
            log.info("[%s] energy via Growatt token API fallback "
                     "(web telemetry was blocked)", plant.plant_key)
        irr = daily_irradiance_for_plant(rows, lat=plant.lat, date_iso=date_iso)
        if dense_web is not None and plant.datalogger_sn:
            dense = try_dense_irradiance(dense_web, plant, date_iso)
            if dense is not None:
                log.info("[%s] irradiance %.3f (%s, %d samples) replaces "
                         "%.3f (%s, %d samples)",
                         plant.plant_key, dense.kwh_m2, dense.source.value,
                         dense.samples_used,
                         irr.kwh_m2 or 0.0, irr.source.value,
                         irr.samples_used)
                irr = dense
        module_temp = irradiance_weighted_module_temp(
            (r.module_temp_c, r.irradiance_wm2) for r in rows
        )
        gamma = (
            plant.gamma_pmax if plant.gamma_pmax is not None else GAMMA_PMAX_DEFAULT
        )
        perf = compute_plant_pr(
            plant_key=plant.plant_key, date_iso=date_iso,
            kwp_dc=plant.kwp_dc, kwp_ac=plant.kwp_ac,
            energy_per_inverter=energy_by_inv,
            irradiance=irr,
            inverter_count_expected=len(portfolio.inverters_for(plant.plant_key)),
            module_temp_c=module_temp,
            gamma_pmax=gamma,
        )
        if token_used:
            perf = dataclasses.replace(
                perf, status_note=(perf.status_note + " | energy via "
                                   "Growatt token API").strip(" |"))
        new_rows.append(perf_to_row(perf))

        # Expected energy for the day (v1 Theoretical_kWh semantics).
        exp = compute_expected_kwh(
            plant.kwp_dc, irr.kwh_m2, plant.expected_factor
        )
        if exp is not None:
            expected_stamps[(date_iso, plant.plant_key)] = exp

        # Contract design baseline (static — works on blocked-sun days).
        dk = design_kwh_for_day(design, plant.plant_key, date_iso)
        if dk is not None:
            design_stamps[(date_iso, plant.plant_key)] = dk

        # Availability vs CONFIGURED inverters (uptime, not performance).
        av = compute_availability(
            [(r.timestamp_utc, r.inverter_sn, r.status) for r in rows],
            [inv.inverter_sn for inv in portfolio.inverters_for(plant.plant_key)],
        )
        if av is not None:
            avail_stamps[(date_iso, plant.plant_key)] = av

        # Specific yield (kWh/kWp) — feeds the plant-vs-twin indicator.
        sy = compute_specific_yield(perf.energy_kwh, plant.kwp_dc)
        if sy is not None:
            sy_stamps[(date_iso, plant.plant_key)] = sy

        # production_pct and soiling_loss_pct only make sense on FULL days:
        # a partial day undercounts both energy and PR, and a stamped lie
        # is worse than a blank (data_class explains the blank).
        if coverage.get((date_iso, plant.plant_key)) == "full":
            pp = gated_production_pct(perf.energy_kwh, exp, perf.pr)
            if pp is not None:   # "" is a valid stamp: clears inflated cells
                prod_stamps[(date_iso, plant.plant_key)] = pp
            sl = compute_soiling_loss_pct(perf.pr, plant.pr_baseline)
            if sl is not None:
                soil_stamps[(date_iso, plant.plant_key)] = sl
        else:
            pp, sl = None, None

        # Plain-language day statement (uses the values just computed;
        # partial days get their own honest sentence).
        note = production_statement(
            pp, perf.pr, av, sl,
            coverage.get((date_iso, plant.plant_key)))
        if note is not None:
            note_stamps[(date_iso, plant.plant_key)] = note
        log.info(
            "[%s] energy=%s kWh  PR=%s (%s)  PR_STC=%s  Tmod=%s  CF=%s (%s)",
            plant.plant_key,
            f"{perf.energy_kwh:.1f}" if perf.energy_kwh else "--",
            f"{perf.pr:.3f}" if perf.pr else "--",
            perf.pr_confidence.value,
            f"{perf.pr_stc:.3f}" if perf.pr_stc else "--",
            f"{perf.module_temp_c:.1f}" if perf.module_temp_c is not None else "--",
            f"{perf.capacity_factor:.3f}" if perf.capacity_factor else "--",
            perf.capacity_factor_confidence.value,
        )

    # Upsert
    if new_rows:
        stats = upsert_kpi_rows(sheets, new_rows, dry_run=args.dry_run)
        log.info("KPI_Daily upsert: %s", stats)
    else:
        log.warning("No KPI rows to write")

    # Stamp coverage (data_class) so the reconcile can distinguish an
    # undercounted day (last sample too early) from a real disagreement.
    if coverage:
        log.info("Coverage: %s",
                 {pk: cls for (_, pk), cls in coverage.items()})
        stamped = stamp_data_class(sheets, coverage, dry_run=args.dry_run)
        log.info("Stamped %d data_class cell(s)%s",
                 stamped, " (dry-run)" if args.dry_run else "")

    # Stamp daylight-mean cloud cover (feeds DailyData_v2's Cloud_Coverage).
    if cloud_stamps:
        log.info("Cloud cover: %s",
                 {pk: v for (_, pk), v in cloud_stamps.items()})
        stamped = stamp_column(sheets, CLOUD_COVERAGE_COL_NAME, cloud_stamps,
                               dry_run=args.dry_run)
        log.info("Stamped %d cloud_coverage_pct cell(s)%s",
                 stamped, " (dry-run)" if args.dry_run else "")

    # Stamp expected_kwh (kwp x irradiance x expected_factor).
    if expected_stamps:
        log.info("Expected kWh: %s",
                 {pk: v for (_, pk), v in expected_stamps.items()})
        stamped = stamp_column(sheets, EXPECTED_KWH_COL_NAME, expected_stamps,
                               dry_run=args.dry_run)
        log.info("Stamped %d expected_kwh cell(s)%s",
                 stamped, " (dry-run)" if args.dry_run else "")

    # Stamp design_kwh (contract baseline, month/days — static).
    if design_stamps:
        log.info("Design kWh/day: %s",
                 {pk: v for (_, pk), v in design_stamps.items()})
        stamped = stamp_column(sheets, "design_kwh", design_stamps,
                               dry_run=args.dry_run)
        log.info("Stamped %d design_kwh cell(s)%s",
                 stamped, " (dry-run)" if args.dry_run else "")

    # Stamp specific yield (kWh/kWp).
    if sy_stamps:
        log.info("Specific yield: %s",
                 {pk: v for (_, pk), v in sy_stamps.items()})
        stamped = stamp_column(sheets, SPECIFIC_YIELD_COL_NAME, sy_stamps,
                               dry_run=args.dry_run)
        log.info("Stamped %d specific_yield cell(s)%s",
                 stamped, " (dry-run)" if args.dry_run else "")

    # Stamp production_pct (real vs expected) — full days only.
    if prod_stamps:
        log.info("Production pct: %s",
                 {pk: v for (_, pk), v in prod_stamps.items()})
        stamped = stamp_column(sheets, PRODUCTION_PCT_COL_NAME, prod_stamps,
                               dry_run=args.dry_run)
        log.info("Stamped %d production_pct cell(s)%s",
                 stamped, " (dry-run)" if args.dry_run else "")

    # Stamp soiling_loss_pct (PR drift vs clean baseline) — full days only.
    if soil_stamps:
        log.info("Soiling loss pct: %s",
                 {pk: v for (_, pk), v in soil_stamps.items()})
        stamped = stamp_column(sheets, SOILING_LOSS_COL_NAME, soil_stamps,
                               dry_run=args.dry_run)
        log.info("Stamped %d soiling_loss_pct cell(s)%s",
                 stamped, " (dry-run)" if args.dry_run else "")

    # Stamp the plain-language day statement.
    if note_stamps:
        stamped = stamp_column(sheets, STATUS_NOTE_COL_NAME, note_stamps,
                               dry_run=args.dry_run)
        log.info("Stamped %d status_note cell(s)%s",
                 stamped, " (dry-run)" if args.dry_run else "")

    # Stamp availability (fraction of daylight slots online, vs config).
    if avail_stamps:
        log.info("Availability: %s",
                 {pk: v for (_, pk), v in avail_stamps.items()})
        stamped = stamp_column(sheets, AVAILABILITY_COL_NAME, avail_stamps,
                               dry_run=args.dry_run)
        log.info("Stamped %d availability cell(s)%s",
                 stamped, " (dry-run)" if args.dry_run else "")

    # Prune (optional)
    if args.prune or args.prune_apply:
        today_iso = now_mx().date().isoformat()
        result = prune_old_rows(
            sheets, today_iso=today_iso,
            window_days=HOT_WINDOW_DAYS,
            apply=args.prune_apply,
        )
        log.info("Prune: %s", result)

    if plants_with_data == 0:
        return 2
    if plants_without > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
