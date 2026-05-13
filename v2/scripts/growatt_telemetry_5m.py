#!/usr/bin/env python3
"""Argia_Mont — 5-minute Growatt telemetry.

Fetches the latest MAXHistory row for every active Growatt inverter, joins per
plant weather (irradiance + cloud cover), and upserts wide rows into:

  - ``Telemetry_<KEY>`` per plant
  - ``Telemetry_Argia`` aggregated

Both tabs auto-create + auto-write header on first run.

USAGE
    python scripts/growatt_telemetry_5m.py
    python scripts/growatt_telemetry_5m.py --dry-run
    python scripts/growatt_telemetry_5m.py --plant-key GTO1
    python scripts/growatt_telemetry_5m.py --plant-key GTO1 --dry-run

EXIT CODES
    0  all Growatt plants succeeded (or no Growatt plants in portfolio)
    1  partial — some plants/inverters failed, others succeeded
    2  total failure — no inverter row written
    3  config error (sheet, credentials, portfolio)

ENV VARS REQUIRED
    GOOGLE_SHEET_ID_V2     v2 sheet
    GOOGLE_CREDENTIALS     service account JSON
    GROWATT_USERNAME       web UI username (used for both inverter + env data)
    GROWATT_PASSWORD       web UI password

NOT IN SCOPE (deferred to later stages)
    * Cron schedule (this is manual-trigger only for now)
    * End-of-day archive + clear (Stage 5)
    * ambient_temp_c column (currently always blank — Stage 3.x)
    * Other Growatt inverter types beyond MAX (parser is MAX-only)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import List, Optional

from argia.core.config import (
    InverterConfig,
    PlantConfig,
    Portfolio,
    load_portfolio,
)
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx
from argia.meteo.growatt_irradiance import (
    GrowattIrradianceClient,
    GrowattWebSession,
    interval_kwh_m2_from_wm2,
)
from argia.meteo.open_meteo import CloudCoverClient
from argia.telemetry.growatt_row import (
    WeatherSnapshot,
    build_argia_row,
    build_plant_row,
)
from argia.telemetry.schema import (
    ARGIA_SCHEMA,
    ARGIA_TAB_NAME,
    PLANT_SCHEMA,
    plant_tab_name,
)
from argia.telemetry.sheets_writer import (
    ensure_telemetry_tab,
    write_telemetry_rows,
)
from argia.vendors.growatt_web import GrowattWebClient
from argia.vendors.growatt_web_parser import (
    extract_latest_row,
    parse_max_history,
)


# Per-inverter delay to avoid hammering Growatt's web UI.
# Matches the existing PER_INVERTER_DELAY_SEC in growatt.py.
PER_INVERTER_DELAY_SEC = 0.2


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _today_iso_mx() -> str:
    """Today in MX local time. Growatt's getMAXHistory needs the local date."""
    return now_mx().date().isoformat()


def _fetch_weather_for_plant(
    plant: PlantConfig,
    date_iso: str,
    irradiance_client: Optional[GrowattIrradianceClient],
    cloud_client: Optional[CloudCoverClient],
    log: logging.Logger,
) -> WeatherSnapshot:
    """Build a WeatherSnapshot for one plant.

    Each fetch is wrapped in a try so weather failures don't cascade into the
    inverter data path. Missing values become None → empty cells in the row.
    """
    irradiance_wm2: Optional[float] = None
    irradiance_kwh_m2_5m: Optional[float] = None
    cloud_pct: Optional[float] = None

    # Irradiance from Growatt ENV station
    if (
        irradiance_client is not None
        and plant.weather_plant_id
        and plant.datalogger_sn
    ):
        try:
            irradiance_wm2 = irradiance_client.fetch_current_irradiance_wm2(
                plant_id=plant.weather_plant_id,
                date_iso=date_iso,
                prefer_sn=plant.datalogger_sn,
                prefer_addr=plant.datalogger_addr,
            )
            if irradiance_wm2 is not None:
                irradiance_kwh_m2_5m = interval_kwh_m2_from_wm2(
                    irradiance_wm2, interval_min=5
                )
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] irradiance fetch failed: %s", plant.plant_key, e)

    # Cloud cover from Open-Meteo (daily average — fine for 5-min joining)
    if cloud_client is not None and plant.lat is not None and plant.lon is not None:
        try:
            cloud_pct = cloud_client.fetch_avg_cloudcover_pct(
                plant.lat, plant.lon, date_iso
            )
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] cloud cover fetch failed: %s", plant.plant_key, e)

    return WeatherSnapshot(
        irradiance_wm2=irradiance_wm2,
        irradiance_kwh_m2_5m=irradiance_kwh_m2_5m,
        cloud_cover_pct=cloud_pct,
        ambient_temp_c=None,  # Stage 3.x — needs new GrowattIrradianceClient method
    )


def _process_plant(
    plant: PlantConfig,
    inverters: List[InverterConfig],
    date_iso: str,
    sheets: SheetsClient,
    web_client: GrowattWebClient,
    weather: WeatherSnapshot,
    dry_run: bool,
    log: logging.Logger,
) -> tuple:
    """Fetch + write one plant's rows. Returns (rows_processed, errors_count).

    All argia rows are also returned so the caller can batch-write them after
    every plant is processed.
    """
    plant_rows = []
    argia_rows = []
    errors = 0

    for inv in inverters:
        try:
            envelope = web_client.get_max_history(inv.inverter_sn, date_iso)
            history = parse_max_history(envelope)
            latest = extract_latest_row(history)
            if latest is None:
                log.warning(
                    "[%s/%s] no history rows for %s — skipping",
                    plant.plant_key, inv.inverter_sn, date_iso,
                )
                continue

            plant_rows.append(build_plant_row(
                latest, inv.inverter_sn, inv.inverter_label, weather,
            ))
            argia_rows.append(build_argia_row(
                latest, plant.plant_key, inv.inverter_sn, inv.inverter_label, weather,
            ))

            time.sleep(PER_INVERTER_DELAY_SEC)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "[%s/%s] fetch/parse failed: %s",
                plant.plant_key, inv.inverter_sn, e,
            )
            errors += 1

    # Write per-plant tab
    if plant_rows:
        tab = plant_tab_name(plant.plant_key)
        try:
            ensure_telemetry_tab(sheets, tab, PLANT_SCHEMA)
            stats = write_telemetry_rows(
                sheets, tab, PLANT_SCHEMA, plant_rows, dry_run=dry_run,
            )
            log.info(
                "[%s] %s: %s",
                plant.plant_key, tab,
                "DRY RUN " + str(stats) if dry_run else stats,
            )
        except Exception as e:  # noqa: BLE001
            log.error("[%s] sheet write failed for %s: %s", plant.plant_key, tab, e)
            errors += 1

    return argia_rows, errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch but do not write to the sheet",
    )
    parser.add_argument(
        "--plant-key",
        default=None,
        help="Run only this one plant (e.g. GTO1). Default: all Growatt plants.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    log = logging.getLogger("argia.growatt_telemetry_5m")

    # --- Sheet client ---
    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 is not set — cannot continue")
        return 3
    try:
        sheets = SheetsClient(sheet_id=sheet_id)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to construct SheetsClient: %s", e)
        return 3

    # --- Portfolio ---
    try:
        portfolio = load_portfolio(sheets)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to load portfolio: %s", e)
        return 3

    growatt_plants = portfolio.plants_by_brand("GROWATT")
    if args.plant_key:
        growatt_plants = [p for p in growatt_plants if p.plant_key == args.plant_key]
        if not growatt_plants:
            log.error(
                "No active Growatt plant with plant_key=%r — check Plants tab",
                args.plant_key,
            )
            return 3

    if not growatt_plants:
        log.info("No active Growatt plants in portfolio — nothing to do.")
        return 0

    log.info(
        "Processing %d Growatt plant(s): %s",
        len(growatt_plants),
        [p.plant_key for p in growatt_plants],
    )

    # --- Growatt clients (one set, reused across plants) ---
    g_user = os.environ.get("GROWATT_USERNAME", "").strip()
    g_pass = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not g_user or not g_pass:
        log.error("GROWATT_USERNAME or GROWATT_PASSWORD is not set — cannot continue")
        return 3

    web_client = GrowattWebClient(username=g_user, password=g_pass)

    irradiance_client: Optional[GrowattIrradianceClient]
    try:
        irradiance_client = GrowattIrradianceClient(
            GrowattWebSession(username=g_user, password=g_pass)
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "Could not build GrowattIrradianceClient: %s — weather will be partial",
            e,
        )
        irradiance_client = None

    cloud_client: Optional[CloudCoverClient]
    try:
        cloud_client = CloudCoverClient()
    except Exception as e:  # noqa: BLE001
        log.warning(
            "Could not build CloudCoverClient: %s — cloud_cover_pct will be blank",
            e,
        )
        cloud_client = None

    # --- Process each plant ---
    date_iso = _today_iso_mx()
    all_argia_rows: List[list] = []
    plants_processed = 0
    plants_skipped = 0
    total_errors = 0

    for plant in growatt_plants:
        inverters = portfolio.inverters_for(plant.plant_key)
        if not inverters:
            log.info(
                "[%s] no active inverters in Inverters tab — skipping",
                plant.plant_key,
            )
            plants_skipped += 1
            continue

        log.info(
            "[%s] %d active inverter(s): %s",
            plant.plant_key,
            len(inverters),
            [i.inverter_sn for i in inverters],
        )

        weather = _fetch_weather_for_plant(
            plant, date_iso, irradiance_client, cloud_client, log,
        )

        try:
            argia_rows, errs = _process_plant(
                plant, inverters, date_iso, sheets, web_client,
                weather, args.dry_run, log,
            )
            all_argia_rows.extend(argia_rows)
            total_errors += errs
            plants_processed += 1
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] plant processing crashed: %s", plant.plant_key, e)
            total_errors += 1
            plants_skipped += 1

    # --- Write the aggregated Argia tab ---
    if all_argia_rows:
        try:
            ensure_telemetry_tab(sheets, ARGIA_TAB_NAME, ARGIA_SCHEMA)
            stats = write_telemetry_rows(
                sheets, ARGIA_TAB_NAME, ARGIA_SCHEMA, all_argia_rows,
                dry_run=args.dry_run,
            )
            log.info(
                "[ARGIA] %s: %s",
                ARGIA_TAB_NAME,
                "DRY RUN " + str(stats) if args.dry_run else stats,
            )
        except Exception as e:  # noqa: BLE001
            log.error("Argia tab write failed: %s", e)
            total_errors += 1

    # --- Summary + exit ---
    log.info(
        "DONE: plants_processed=%d plants_skipped=%d rows_collected=%d errors=%d "
        "dry_run=%s",
        plants_processed,
        plants_skipped,
        len(all_argia_rows),
        total_errors,
        args.dry_run,
    )

    if total_errors == 0 and len(all_argia_rows) > 0:
        return 0
    if len(all_argia_rows) == 0:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
