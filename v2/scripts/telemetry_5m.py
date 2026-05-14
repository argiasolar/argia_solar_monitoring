#!/usr/bin/env python3
"""Argia_Mont — unified 5-minute telemetry across all vendors.

Stage 5: SolarEdge pipeline added alongside Growatt and Huawei.

For each active plant in the portfolio:
1. Pick the right vendor handler.
2. Fetch the latest snapshot for every active inverter.
3. Join plant-level weather (irradiance + cloud cover).
4. Build wide rows (one per inverter) and upsert to ``Telemetry_<KEY>``.
5. Collect narrow common rows; after all plants, upsert to ``Telemetry_Argia``.

Vendors:
- GROWATT: rich web-UI scraping, ~150 fields per inverter
- HUAWEI: REST API (getDevRealKpi), ~100 fields per inverter
- SOLAREDGE: REST API (/equipment/.../data), ~9 fields per inverter
- SMA: not yet built (Stage 6)

**SolarEdge rate limit warning:** SolarEdge enforces 300 calls/day per site/api_key.
At 5-min cadence with multiple inverters per site, this quota will be exhausted
around mid-morning. The script catches HTTP 429 and skips remaining SolarEdge
plants for that run — other vendors continue. Quota resets at midnight UTC.

USAGE
    python scripts/telemetry_5m.py
    python scripts/telemetry_5m.py --dry-run
    python scripts/telemetry_5m.py --plant-key GTO1
    python scripts/telemetry_5m.py --plant-key QRO1 --dry-run --log-level DEBUG

EXIT CODES
    0  all processed plants succeeded
    1  partial — some failed, some succeeded
    2  total failure — no rows written
    3  config error
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
from argia.telemetry import growatt_row, huawei_row, solaredge_row
from argia.telemetry.growatt_row import WeatherSnapshot
from argia.telemetry.schema import (
    ARGIA_SCHEMA,
    ARGIA_TAB_NAME,
    PLANT_SCHEMA,
    plant_tab_name,
)
from argia.telemetry.sheets_writer import (
    SchemaMismatchError,
    ensure_telemetry_tab,
    write_telemetry_rows,
)
from argia.vendors.growatt_web import GrowattWebClient
from argia.vendors.growatt_web_parser import (
    extract_latest_row,
    parse_max_history,
)
from argia.vendors.huawei import HuaweiAPIError, HuaweiAuthError, HuaweiClient
from argia.vendors.huawei_telemetry import (
    HuaweiTelemetryRow,
    fetch_inverter_telemetry as fetch_huawei_telemetry,
)
from argia.vendors.solaredge import (
    SolarEdgeAPIError,
    SolarEdgeAuthError,
    SolarEdgeClient,
)
from argia.vendors.solaredge_telemetry import (
    SolarEdgeTelemetryRow,
    fetch_inverter_telemetry as fetch_solaredge_telemetry,
)


PER_GROWATT_INVERTER_DELAY_SEC = 0.2


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _today_iso_mx() -> str:
    return now_mx().date().isoformat()


# ============================================================
# Weather (shared across vendors)
# ============================================================


def _fetch_weather_for_plant(
    plant: PlantConfig,
    date_iso: str,
    irradiance_client: Optional[GrowattIrradianceClient],
    cloud_client: Optional[CloudCoverClient],
    log: logging.Logger,
) -> WeatherSnapshot:
    irradiance_wm2: Optional[float] = None
    irradiance_kwh_m2_5m: Optional[float] = None
    cloud_pct: Optional[float] = None

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
        ambient_temp_c=None,
    )


# ============================================================
# Growatt: process one plant (unchanged from Stage 4.2)
# ============================================================


def _process_growatt_plant(
    plant: PlantConfig,
    inverters: List[InverterConfig],
    date_iso: str,
    sheets: SheetsClient,
    web_client: GrowattWebClient,
    weather: WeatherSnapshot,
    dry_run: bool,
    log: logging.Logger,
) -> tuple:
    plant_rows: List[list] = []
    common_rows: List[list] = []
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

            plant_rows.append(growatt_row.build_plant_row(
                latest, inv.inverter_sn, inv.inverter_label, weather,
            ))
            common_rows.append(growatt_row.build_common_row(
                latest, plant.plant_key, inv.inverter_sn, inv.inverter_label, weather,
            ))
            time.sleep(PER_GROWATT_INVERTER_DELAY_SEC)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "[%s/%s] fetch/parse failed: %s",
                plant.plant_key, inv.inverter_sn, e,
            )
            errors += 1

    if plant_rows:
        tab = plant_tab_name(plant.plant_key)
        try:
            ensure_telemetry_tab(sheets, tab, PLANT_SCHEMA)
            stats = write_telemetry_rows(
                sheets, tab, PLANT_SCHEMA, plant_rows, dry_run=dry_run,
            )
            log.info("[%s] %s: %s", plant.plant_key, tab,
                     "DRY RUN " + str(stats) if dry_run else stats)
        except SchemaMismatchError as e:
            log.error("[%s] %s", plant.plant_key, e)
            errors += 1
        except Exception as e:  # noqa: BLE001
            log.error("[%s] sheet write failed: %s", plant.plant_key, e)
            errors += 1

    return common_rows, errors


# ============================================================
# Huawei: process one plant (unchanged from Stage 4.2)
# ============================================================


def _process_huawei_plant(
    plant: PlantConfig,
    inverters: List[InverterConfig],
    sheets: SheetsClient,
    huawei_client: HuaweiClient,
    weather: WeatherSnapshot,
    dry_run: bool,
    log: logging.Logger,
) -> tuple:
    common_rows: List[list] = []
    plant_rows: List[list] = []
    errors = 0

    try:
        telemetry: List[HuaweiTelemetryRow] = fetch_huawei_telemetry(
            huawei_client, plant, inverters,
        )
    except (HuaweiAuthError, HuaweiAPIError) as e:
        log.warning("[%s] Huawei fetch failed: %s", plant.plant_key, e)
        return [], 1
    except Exception as e:  # noqa: BLE001
        log.exception("[%s] Huawei fetch crashed: %s", plant.plant_key, e)
        return [], 1

    label_by_sn = {inv.inverter_sn: inv.inverter_label for inv in inverters}

    for tel in telemetry:
        label = label_by_sn.get(tel.inverter_sn, tel.inverter_sn)
        try:
            plant_rows.append(huawei_row.build_plant_row(tel, label, weather))
            common_rows.append(huawei_row.build_common_row(tel, label, weather))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s/%s] row build failed: %s",
                        plant.plant_key, tel.inverter_sn, e)
            errors += 1

    returned_sns = {t.inverter_sn for t in telemetry}
    for inv in inverters:
        if inv.inverter_sn not in returned_sns:
            log.warning("[%s/%s] Huawei API did not return data for this SN",
                        plant.plant_key, inv.inverter_sn)

    if plant_rows:
        tab = plant_tab_name(plant.plant_key)
        try:
            ensure_telemetry_tab(sheets, tab, PLANT_SCHEMA)
            stats = write_telemetry_rows(
                sheets, tab, PLANT_SCHEMA, plant_rows, dry_run=dry_run,
            )
            log.info("[%s] %s: %s", plant.plant_key, tab,
                     "DRY RUN " + str(stats) if dry_run else stats)
        except SchemaMismatchError as e:
            log.error("[%s] %s", plant.plant_key, e)
            errors += 1
        except Exception as e:  # noqa: BLE001
            log.error("[%s] sheet write failed: %s", plant.plant_key, e)
            errors += 1

    return common_rows, errors


# ============================================================
# SolarEdge: process one plant (NEW in Stage 5)
# ============================================================


class _SolarEdgeQuotaExhausted(Exception):
    """Signal that the SolarEdge quota has hit; caller should skip remaining plants."""


def _process_solaredge_plant(
    plant: PlantConfig,
    inverters: List[InverterConfig],
    sheets: SheetsClient,
    se_client: SolarEdgeClient,
    weather: WeatherSnapshot,
    dry_run: bool,
    log: logging.Logger,
) -> tuple:
    common_rows: List[list] = []
    plant_rows: List[list] = []
    errors = 0

    try:
        telemetry: List[SolarEdgeTelemetryRow] = fetch_solaredge_telemetry(
            se_client, plant, inverters,
        )
    except SolarEdgeAuthError as e:
        log.error("[%s] SolarEdge auth failed: %s — skipping remaining SE plants",
                  plant.plant_key, e)
        raise _SolarEdgeQuotaExhausted() from e
    except SolarEdgeAPIError as e:
        msg = str(e).lower()
        if "rate-limited" in msg or "429" in msg:
            log.warning(
                "[%s] SolarEdge rate-limited — skipping this and remaining SE plants",
                plant.plant_key,
            )
            raise _SolarEdgeQuotaExhausted() from e
        log.warning("[%s] SolarEdge fetch failed: %s", plant.plant_key, e)
        return [], 1
    except Exception as e:  # noqa: BLE001
        log.exception("[%s] SolarEdge fetch crashed: %s", plant.plant_key, e)
        return [], 1

    label_by_sn = {inv.inverter_sn: inv.inverter_label for inv in inverters}

    for tel in telemetry:
        label = label_by_sn.get(tel.inverter_sn, tel.inverter_sn)
        try:
            plant_rows.append(solaredge_row.build_plant_row(tel, label, weather))
            common_rows.append(solaredge_row.build_common_row(tel, label, weather))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s/%s] row build failed: %s",
                        plant.plant_key, tel.inverter_sn, e)
            errors += 1

    returned_sns = {t.inverter_sn for t in telemetry}
    for inv in inverters:
        if inv.inverter_sn not in returned_sns:
            log.warning("[%s/%s] no telemetry returned (offline or no data)",
                        plant.plant_key, inv.inverter_sn)

    if plant_rows:
        tab = plant_tab_name(plant.plant_key)
        try:
            ensure_telemetry_tab(sheets, tab, PLANT_SCHEMA)
            stats = write_telemetry_rows(
                sheets, tab, PLANT_SCHEMA, plant_rows, dry_run=dry_run,
            )
            log.info("[%s] %s: %s", plant.plant_key, tab,
                     "DRY RUN " + str(stats) if dry_run else stats)
        except SchemaMismatchError as e:
            log.error("[%s] %s", plant.plant_key, e)
            errors += 1
        except Exception as e:  # noqa: BLE001
            log.error("[%s] sheet write failed: %s", plant.plant_key, e)
            errors += 1

    return common_rows, errors


# ============================================================
# Vendor pipelines
# ============================================================


def _run_growatt(portfolio, sheets, date_iso, only_plant,
                 irradiance_client, cloud_client, dry_run, log) -> tuple:
    plants = portfolio.plants_by_brand("GROWATT")
    if only_plant:
        plants = [p for p in plants if p.plant_key == only_plant]
    if not plants:
        return [], 0, 0, 0

    g_user = os.environ.get("GROWATT_USERNAME", "").strip()
    g_pass = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not g_user or not g_pass:
        log.error("GROWATT_USERNAME/PASSWORD not set — skipping %d Growatt plant(s)",
                  len(plants))
        return [], 0, len(plants), 1

    web_client = GrowattWebClient(username=g_user, password=g_pass)
    log.info("Processing %d Growatt plant(s): %s",
             len(plants), [p.plant_key for p in plants])

    all_common: List[list] = []
    processed = 0
    skipped = 0
    total_errors = 0

    for plant in plants:
        inverters = portfolio.inverters_for(plant.plant_key)
        if not inverters:
            log.info("[%s] no active inverters — skipping", plant.plant_key)
            skipped += 1
            continue
        log.info("[%s] %d active inverter(s): %s",
                 plant.plant_key, len(inverters), [i.inverter_sn for i in inverters])

        weather = _fetch_weather_for_plant(plant, date_iso, irradiance_client, cloud_client, log)
        try:
            common, errs = _process_growatt_plant(
                plant, inverters, date_iso, sheets, web_client,
                weather, dry_run, log,
            )
            all_common.extend(common)
            total_errors += errs
            processed += 1
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] plant crashed: %s", plant.plant_key, e)
            total_errors += 1
            skipped += 1

    return all_common, processed, skipped, total_errors


def _run_huawei(portfolio, sheets, date_iso, only_plant,
                irradiance_client, cloud_client, dry_run, log) -> tuple:
    plants = portfolio.plants_by_brand("HUAWEI")
    if only_plant:
        plants = [p for p in plants if p.plant_key == only_plant]
    if not plants:
        return [], 0, 0, 0

    h_user = os.environ.get("HUAWEI_USERNAME", "").strip()
    h_pass = os.environ.get("HUAWEI_PASSWORD", "").strip()
    if not h_user or not h_pass:
        log.error("HUAWEI_USERNAME/PASSWORD not set — skipping %d Huawei plant(s)",
                  len(plants))
        return [], 0, len(plants), 1

    try:
        huawei_client = HuaweiClient(username=h_user, password=h_pass)
        huawei_client.login()
    except Exception as e:  # noqa: BLE001
        log.error("Huawei login failed: %s — skipping all Huawei plants", e)
        return [], 0, len(plants), 1

    log.info("Processing %d Huawei plant(s): %s",
             len(plants), [p.plant_key for p in plants])

    all_common: List[list] = []
    processed = 0
    skipped = 0
    total_errors = 0

    for plant in plants:
        inverters = portfolio.inverters_for(plant.plant_key)
        if not inverters:
            log.info("[%s] no active inverters — skipping", plant.plant_key)
            skipped += 1
            continue
        log.info("[%s] %d active inverter(s): %s",
                 plant.plant_key, len(inverters), [i.inverter_sn for i in inverters])

        weather = _fetch_weather_for_plant(plant, date_iso, irradiance_client, cloud_client, log)
        try:
            common, errs = _process_huawei_plant(
                plant, inverters, sheets, huawei_client, weather, dry_run, log,
            )
            all_common.extend(common)
            total_errors += errs
            processed += 1
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] plant crashed: %s", plant.plant_key, e)
            total_errors += 1
            skipped += 1

    return all_common, processed, skipped, total_errors


def _run_solaredge(portfolio, sheets, date_iso, only_plant,
                   irradiance_client, cloud_client, dry_run, log) -> tuple:
    """SolarEdge pipeline. Note: per-plant api_keys from secret_api_name column."""
    plants = portfolio.plants_by_brand("SOLAREDGE")
    if only_plant:
        plants = [p for p in plants if p.plant_key == only_plant]
    if not plants:
        return [], 0, 0, 0

    log.info("Processing %d SolarEdge plant(s): %s",
             len(plants), [p.plant_key for p in plants])

    all_common: List[list] = []
    processed = 0
    skipped = 0
    total_errors = 0

    for plant in plants:
        # Each plant has its own api_key (named in secret_api_name column)
        secret_name = plant.secret_api_name
        api_key = os.environ.get(secret_name, "").strip() if secret_name else ""
        if not api_key:
            log.warning("[%s] env var %s is not set — skipping",
                        plant.plant_key, secret_name)
            skipped += 1
            continue

        try:
            se_client = SolarEdgeClient(api_key=api_key)
        except ValueError as e:
            log.error("[%s] could not build SolarEdgeClient: %s",
                      plant.plant_key, e)
            total_errors += 1
            skipped += 1
            continue

        inverters = portfolio.inverters_for(plant.plant_key)
        if not inverters:
            log.info("[%s] no active inverters — skipping (run "
                     "solaredge_discover_inverters.py and add SNs to Inverters tab)",
                     plant.plant_key)
            skipped += 1
            continue
        log.info("[%s] %d active inverter(s): %s",
                 plant.plant_key, len(inverters), [i.inverter_sn for i in inverters])

        weather = _fetch_weather_for_plant(plant, date_iso, irradiance_client, cloud_client, log)
        try:
            common, errs = _process_solaredge_plant(
                plant, inverters, sheets, se_client, weather, dry_run, log,
            )
            all_common.extend(common)
            total_errors += errs
            processed += 1
        except _SolarEdgeQuotaExhausted:
            # Bail out of remaining SolarEdge plants — quota done for the day
            skipped += len(plants) - processed - 1
            break
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] plant crashed: %s", plant.plant_key, e)
            total_errors += 1
            skipped += 1

    return all_common, processed, skipped, total_errors


# ============================================================
# Main
# ============================================================


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--plant-key", default=None)
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    log = logging.getLogger("argia.telemetry_5m")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 is not set")
        return 3
    try:
        sheets = SheetsClient(sheet_id=sheet_id)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to construct SheetsClient: %s", e)
        return 3

    try:
        portfolio = load_portfolio(sheets)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to load portfolio: %s", e)
        return 3

    g_user = os.environ.get("GROWATT_USERNAME", "").strip()
    g_pass = os.environ.get("GROWATT_PASSWORD", "").strip()
    irradiance_client: Optional[GrowattIrradianceClient] = None
    if g_user and g_pass:
        try:
            irradiance_client = GrowattIrradianceClient(
                GrowattWebSession(username=g_user, password=g_pass)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Could not build GrowattIrradianceClient: %s", e)

    try:
        cloud_client: Optional[CloudCoverClient] = CloudCoverClient()
    except Exception as e:  # noqa: BLE001
        log.warning("Could not build CloudCoverClient: %s", e)
        cloud_client = None

    date_iso = _today_iso_mx()
    all_common: List[list] = []
    total_processed = 0
    total_skipped = 0
    total_errors = 0

    # ----- Growatt -----
    try:
        common, processed, skipped, errs = _run_growatt(
            portfolio, sheets, date_iso, args.plant_key,
            irradiance_client, cloud_client, args.dry_run, log,
        )
        all_common.extend(common)
        total_processed += processed
        total_skipped += skipped
        total_errors += errs
    except Exception as e:  # noqa: BLE001
        log.exception("Growatt pipeline crashed: %s", e)
        total_errors += 1

    # ----- Huawei -----
    try:
        common, processed, skipped, errs = _run_huawei(
            portfolio, sheets, date_iso, args.plant_key,
            irradiance_client, cloud_client, args.dry_run, log,
        )
        all_common.extend(common)
        total_processed += processed
        total_skipped += skipped
        total_errors += errs
    except Exception as e:  # noqa: BLE001
        log.exception("Huawei pipeline crashed: %s", e)
        total_errors += 1

    # ----- SolarEdge (NEW in Stage 5) -----
    try:
        common, processed, skipped, errs = _run_solaredge(
            portfolio, sheets, date_iso, args.plant_key,
            irradiance_client, cloud_client, args.dry_run, log,
        )
        all_common.extend(common)
        total_processed += processed
        total_skipped += skipped
        total_errors += errs
    except Exception as e:  # noqa: BLE001
        log.exception("SolarEdge pipeline crashed: %s", e)
        total_errors += 1

    # ----- SMA (Stage 6) -----
    sma_plants = portfolio.plants_by_brand("SMA")
    if args.plant_key:
        sma_plants = [p for p in sma_plants if p.plant_key == args.plant_key]
    if sma_plants:
        log.info("SMA telemetry pipeline not yet built — skipping %d plant(s): %s",
                 len(sma_plants), [p.plant_key for p in sma_plants])
        total_skipped += len(sma_plants)

    # ----- Write the aggregated Argia tab in one batch -----
    if all_common:
        try:
            ensure_telemetry_tab(sheets, ARGIA_TAB_NAME, ARGIA_SCHEMA)
            stats = write_telemetry_rows(
                sheets, ARGIA_TAB_NAME, ARGIA_SCHEMA, all_common,
                dry_run=args.dry_run,
            )
            log.info("[ARGIA] %s: %s", ARGIA_TAB_NAME,
                     "DRY RUN " + str(stats) if args.dry_run else stats)
        except SchemaMismatchError as e:
            log.error("Argia tab schema mismatch: %s", e)
            total_errors += 1
        except Exception as e:  # noqa: BLE001
            log.error("Argia tab write failed: %s", e)
            total_errors += 1

    log.info(
        "DONE: plants_processed=%d plants_skipped=%d rows_collected=%d errors=%d "
        "dry_run=%s",
        total_processed, total_skipped, len(all_common), total_errors, args.dry_run,
    )

    if total_errors == 0 and len(all_common) > 0:
        return 0
    if len(all_common) == 0:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
