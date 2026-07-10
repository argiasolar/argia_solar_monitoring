#!/usr/bin/env python3
"""Argia_Mont — unified 5-minute telemetry across all vendors.

Stage 6: SMA pipeline added (sandbox-ready).

For each active plant in the portfolio:
1. Pick the right vendor handler.
2. Fetch the latest snapshot for every active inverter.
3. Join plant-level weather (irradiance + cloud cover).
4. Build wide rows (one per inverter) and upsert to ``Telemetry_<KEY>``.
5. Collect narrow common rows; after all plants, upsert to ``Telemetry_Argia``.

Vendors:
- GROWATT: rich web-UI scraping, ~150 fields per inverter
- HUAWEI: REST API (getDevRealKpi), ~100 fields per inverter
- SOLAREDGE: REST API (/equipment/.../data), ~30 fields per inverter (Stage 5.1)
- SMA: OAuth2 + backchannel + Monitoring API, ~10-15 fields (Stage 6)

**Rate limit notes:**
- SolarEdge: 300 calls/day per site/api_key. Will exhaust mid-morning at 5-min cadence.
- SMA sandbox: documents say some endpoints are unavailable; 404s logged, run continues.

USAGE
    python scripts/telemetry_5m.py
    python scripts/telemetry_5m.py --dry-run
    python scripts/telemetry_5m.py --plant-key SMA_SANDBOX
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
    load_portfolio,
)
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx, now_utc
from argia.orchestrator import RunResult, TAB_SYNC, new_run_id
from argia.meteo.growatt_irradiance import (
    GrowattIrradianceClient,
    GrowattWebSession,
    find_latest_env_temps,
    find_latest_radiance_wm2,
    interval_kwh_m2_from_wm2,
)
from argia.meteo.open_meteo import CloudCoverClient
from argia.vendors import growatt_token
from argia.telemetry import growatt_row, huawei_row, sma_row, solaredge_row
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
from argia.vendors.sma import (
    SMAAPIError,
    SMAAuthError,
    SMAClient,
    SMAConsentError,
)
from argia.vendors.sma_telemetry import (
    SMATelemetryRow,
    fetch_inverter_telemetry as fetch_sma_telemetry,
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
    ambient_temp_c: Optional[float] = None
    module_temp_c: Optional[float] = None
    cloud_pct: Optional[float] = None

    if (
        irradiance_client is not None
        and plant.weather_plant_id
        and plant.datalogger_sn
    ):
        try:
            # One env-history fetch feeds irradiance AND temperatures — the
            # readings ride the same ShineMaster record, so we don't pay for
            # the getEnvHistory call twice.
            device = irradiance_client.get_env_device(
                plant.weather_plant_id,
                prefer_sn=plant.datalogger_sn,
                prefer_addr=plant.datalogger_addr,
            )
            if device is not None:
                sn, addr = device
                rows = irradiance_client.fetch_env_history_rows(
                    plant.weather_plant_id, sn, addr, date_iso
                )
                irradiance_wm2 = find_latest_radiance_wm2(rows)
                ambient_temp_c, module_temp_c = find_latest_env_temps(rows)
                if irradiance_wm2 is not None:
                    irradiance_kwh_m2_5m = interval_kwh_m2_from_wm2(
                        irradiance_wm2, interval_min=5
                    )
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] weather fetch failed: %s", plant.plant_key, e)

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
        ambient_temp_c=ambient_temp_c,
        module_temp_c=module_temp_c,
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
    token_client: "growatt_token.GrowattTokenClient | None" = None,
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

    # Degraded-mode fallback (2026-07-07): the web session is the only
    # carrier of per-inverter data; when it is blocked (LoginBackoff /
    # auth refusal) every inverter fails and plant_rows is empty. Then —
    # and only then — fetch plant-level today_energy via the OpenAPI
    # token (v1's proven route) and cache it for tomorrow's kpi-eod.
    # Errors stand as counted: the run stays PARTIAL, honestly.
    if not plant_rows and errors and token_client is not None:
        kwh = token_client.plant_today_energy(plant.weather_plant_id)
        if kwh is not None and kwh > 0:
            growatt_token.cache_energy(date_iso, plant.plant_key, kwh)
            log.info("[%s] degraded mode: today_energy=%.1f kWh via "
                     "Growatt token API (web session blocked); cached "
                     "for kpi-eod", plant.plant_key, kwh)

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
# SolarEdge: process one plant (Stage 5)
# ============================================================


KNOWN_BRANDS = ("GROWATT", "HUAWEI", "SOLAREDGE", "SMA")


def brand_enabled(brand: str, only: str | None, skip: str | None) -> bool:
    """--brand X runs only X; --skip-brand Y runs everything but Y.
    Both None = everything (unchanged default)."""
    if only is not None:
        return brand == only
    if skip is not None:
        return brand != skip
    return True


class _SolarEdgeQuotaExhausted(Exception):
    """This SITE's daily API quota (300 req/day/site) is spent. v80:
    per-site budgets — the orchestrator skips THIS plant only and
    continues with the next SolarEdge plant, whose budget is separate.
    Quota exhaustion near end of day is an expected condition, not an
    outage: data resumes after site-local midnight and the evening gap
    is reconciled by the next morning's full-day fetch."""


class _SolarEdgeAuthFailed(Exception):
    """API key rejected — no point trying other calls with it; the
    orchestrator skips ONLY plants sharing this key's secret name."""


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
        log.error("[%s] SolarEdge auth failed (key rejected): %s",
                  plant.plant_key, e)
        raise _SolarEdgeAuthFailed() from e
    except SolarEdgeAPIError as e:
        msg = str(e).lower()
        if "rate-limited" in msg or "429" in msg:
            log.warning(
                "[%s] site %s: daily SolarEdge API quota reached "
                "(~300 req/day/site) — expected near end of day at the "
                "20-min cadence, NOT an outage. Collection resumes "
                "after site-local midnight; tomorrow's full-day fetch "
                "backfills today's tail for KPI purposes.",
                plant.plant_key, plant.site_id,
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
# SMA: process one plant (NEW in Stage 6)
# ============================================================


class _SMAAuthFailed(Exception):
    """Token/consent failed for the whole SMA pipeline — skip remaining SMA plants."""


def _process_sma_plant(
    plant: PlantConfig,
    inverters: List[InverterConfig],
    sheets: SheetsClient,
    sma_client: SMAClient,
    weather: WeatherSnapshot,
    dry_run: bool,
    log: logging.Logger,
) -> tuple:
    common_rows: List[list] = []
    plant_rows: List[list] = []
    errors = 0

    try:
        telemetry: List[SMATelemetryRow] = fetch_sma_telemetry(
            sma_client, plant, inverters,
        )
    except SMAAuthError as e:
        log.error("[%s] SMA auth failed: %s — skipping remaining SMA plants",
                  plant.plant_key, e)
        raise _SMAAuthFailed() from e
    except SMAConsentError as e:
        log.error("[%s] SMA consent failed: %s — skipping remaining SMA plants",
                  plant.plant_key, e)
        raise _SMAAuthFailed() from e
    except SMAAPIError as e:
        msg = str(e).lower()
        if "rate-limited" in msg or "429" in msg:
            log.warning(
                "[%s] SMA rate-limited — skipping remaining SMA inverters",
                plant.plant_key,
            )
            return [], 1
        log.warning("[%s] SMA fetch failed: %s", plant.plant_key, e)
        return [], 1
    except Exception as e:  # noqa: BLE001
        log.exception("[%s] SMA fetch crashed: %s", plant.plant_key, e)
        return [], 1

    label_by_sn = {inv.inverter_sn: inv.inverter_label for inv in inverters}

    for tel in telemetry:
        label = label_by_sn.get(tel.inverter_sn, tel.inverter_sn)
        try:
            plant_rows.append(sma_row.build_plant_row(tel, label, weather))
            common_rows.append(sma_row.build_common_row(tel, label, weather))
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
    token_client = growatt_token.GrowattTokenClient.from_env()
    # Run-level session revalidation (2026-07-08): a restored session is
    # probed before trust; expired -> fresh login here, ONCE per run.
    # Failures are non-fatal — per-inverter fetches surface them and the
    # token fallback covers energy.
    try:
        web_client.ensure_session()
    except Exception as e:  # noqa: BLE001
        log.warning("Growatt session ensure failed: %s", e)
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
                weather, dry_run, log, token_client=token_client,
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

    dead_keys: set = set()
    for plant in plants:
        secret_name = plant.secret_api_name
        if secret_name in dead_keys:
            skipped += 1
            continue
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
            # per-SITE budget: only this plant pauses; the next SE
            # plant has its own quota. Not counted as an error — the
            # condition is expected and self-heals at midnight.
            skipped += 1
            continue
        except _SolarEdgeAuthFailed:
            bad_key = plant.secret_api_name
            same_key = [p.plant_key for p in plants
                        if p.secret_api_name == bad_key]
            log.error("skipping plant(s) sharing rejected key %r: %s",
                      bad_key, same_key)
            dead_keys.add(bad_key)
            total_errors += 1
            skipped += 1
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] plant crashed: %s", plant.plant_key, e)
            total_errors += 1
            skipped += 1

    return all_common, processed, skipped, total_errors


def _run_sma(portfolio, sheets, date_iso, only_plant,
             irradiance_client, cloud_client, dry_run, log) -> tuple:
    """SMA pipeline.

    Reads SMA_CLIENT_ID, SMA_CLIENT_SECRET, SMA_LOGIN_HINT from env.
    SMA_ENVIRONMENT defaults to 'sandbox' if unset.

    Currently uses ONE SMAClient for all SMA plants (shared OAuth). In the
    future, if Argia onboards multiple customer SMA accounts, the
    secret_*_name columns let each plant point to its own credentials.
    """
    plants = portfolio.plants_by_brand("SMA")
    if only_plant:
        plants = [p for p in plants if p.plant_key == only_plant]
    if not plants:
        return [], 0, 0, 0

    client_id = os.environ.get("SMA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SMA_CLIENT_SECRET", "").strip()
    login_hint = os.environ.get(
        "SMA_LOGIN_HINT", "apiTestUser@apiSandbox.com",
    ).strip()
    environment = os.environ.get("SMA_ENVIRONMENT", "sandbox").strip()

    if not (client_id and client_secret):
        log.error("SMA_CLIENT_ID/SMA_CLIENT_SECRET not set — skipping %d SMA plant(s)",
                  len(plants))
        return [], 0, len(plants), 1

    try:
        sma_client = SMAClient(
            client_id=client_id,
            client_secret=client_secret,
            login_hint=login_hint,
            environment=environment,
        )
        sma_client.login()
    except (SMAAuthError, SMAConsentError) as e:
        log.error("SMA login failed: %s — skipping all SMA plants", e)
        return [], 0, len(plants), 1
    except SMAAPIError as e:
        log.error("SMA login API error: %s — skipping all SMA plants", e)
        return [], 0, len(plants), 1
    except Exception as e:  # noqa: BLE001
        log.exception("SMA client setup crashed: %s", e)
        return [], 0, len(plants), 1

    log.info("Processing %d SMA plant(s) [env=%s]: %s",
             len(plants), environment, [p.plant_key for p in plants])

    all_common: List[list] = []
    processed = 0
    skipped = 0
    total_errors = 0

    for plant in plants:
        inverters = portfolio.inverters_for(plant.plant_key)
        if not inverters:
            log.info("[%s] no active inverters — skipping (run "
                     "sma_discover_plants.py and add SNs to Inverters tab)",
                     plant.plant_key)
            skipped += 1
            continue
        log.info("[%s] %d active inverter(s): %s",
                 plant.plant_key, len(inverters), [i.inverter_sn for i in inverters])

        weather = _fetch_weather_for_plant(plant, date_iso, irradiance_client, cloud_client, log)
        try:
            common, errs = _process_sma_plant(
                plant, inverters, sheets, sma_client, weather, dry_run, log,
            )
            all_common.extend(common)
            total_errors += errs
            processed += 1
        except _SMAAuthFailed:
            skipped += len(plants) - processed - 1
            break
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] plant crashed: %s", plant.plant_key, e)
            total_errors += 1
            skipped += 1

    return all_common, processed, skipped, total_errors


# ============================================================
# SyncRuns logging (NEW)
# ============================================================


SYNC_RUNS_HEADER = [
    "run_id",
    "started_at_utc",
    "finished_at_utc",
    "script",
    "status",
    "plants_processed",
    "rows_written",
    "errors_json",
]


def _finalize_and_log_run(
    result: RunResult,
    sheets: SheetsClient,
    total_processed: int,
    total_skipped: int,
    total_errors: int,
    rows_collected: int,
    dry_run: bool,
    log: logging.Logger,
) -> None:
    """Stamp final accounting on ``result``, then append a row to ``SyncRuns``.

    Never raises. If the write itself fails (network blip, sheet permissions),
    we log the error and return — telemetry data is already written by this
    point, and one missed SyncRuns row must not turn a green run red.

    Status is derived by ``RunResult.finalize()``:
      - OK       if no errors recorded
      - PARTIAL  if some plants processed AND some errors
      - FAILED   if no plants processed AND errors present
    """
    result.plants_processed = total_processed
    result.plants_skipped = total_skipped
    result.rows_written = rows_collected
    if total_errors > 0:
        # The detailed per-plant errors are already in the log; capture a
        # one-line summary here so an operator can spot a bad run at a glance.
        result.errors.append(
            f"{total_errors} non-fatal error(s) during run — see log for details"
        )
    result.finalize()

    try:
        sheets.ensure_tab(TAB_SYNC)
        sheets.ensure_header(TAB_SYNC, SYNC_RUNS_HEADER)
    except Exception as e:  # noqa: BLE001
        log.error("[SyncRuns] could not ensure tab/header: %s", e)
        return

    row = result.to_sheet_row()
    if dry_run:
        log.info("[SyncRuns] DRY RUN — would log: %s", row)
        return

    try:
        sheets.append_rows(TAB_SYNC, [row])
        log.info(
            "[SyncRuns] logged run %s status=%s processed=%d skipped=%d "
            "rows=%d errors=%d",
            result.run_id, result.status,
            result.plants_processed, result.plants_skipped,
            result.rows_written, len(result.errors),
        )
    except Exception as e:  # noqa: BLE001
        log.error("[SyncRuns] append failed (telemetry data unaffected): %s", e)


# ============================================================
# Main
# ============================================================


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--plant-key", default=None)
    parser.add_argument("--brand", default=None,
                        choices=KNOWN_BRANDS,
                        type=lambda v: v.upper(),
                        help="run ONLY this vendor pipeline (e.g. the "
                             "dedicated SolarEdge cron at its "
                             "quota-safe cadence)")
    parser.add_argument("--skip-brand", default=None,
                        choices=KNOWN_BRANDS,
                        type=lambda v: v.upper(),
                        help="run every pipeline EXCEPT this vendor "
                             "(the main 5-min cron skips SOLAREDGE, "
                             "whose ~300 req/day/site quota needs the "
                             "slower dedicated cron)")
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

    # SyncRuns: start the run-accounting record. We log the row at the end of
    # main() regardless of outcome (OK/PARTIAL/FAILED) so cron failures become
    # visible in the sheet instead of just dying silently.
    run_result = RunResult(
        run_id=new_run_id(),
        started_at_utc=now_utc(),
        script="telemetry_5m",
    )
    log.info("Starting run %s", run_result.run_id)

    g_user = os.environ.get("GROWATT_USERNAME", "").strip()
    g_pass = os.environ.get("GROWATT_PASSWORD", "").strip()
    irradiance_client: Optional[GrowattIrradianceClient] = None
    if g_user and g_pass:
        try:
            irradiance_client = GrowattIrradianceClient(
                GrowattWebSession(username=g_user, password=g_pass)
            )
            try:
                irradiance_client.ensure_session()
            except Exception as e:  # noqa: BLE001
                log.warning("Growatt env session ensure failed: %s", e)
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
        common, processed, skipped, errs = (
            _run_growatt(
                portfolio, sheets, date_iso, args.plant_key,
                irradiance_client, cloud_client, args.dry_run, log,
            )
            if brand_enabled("GROWATT", args.brand, args.skip_brand)
            else ([], 0, 0, 0))
        all_common.extend(common)
        total_processed += processed
        total_skipped += skipped
        total_errors += errs
    except Exception as e:  # noqa: BLE001
        log.exception("Growatt pipeline crashed: %s", e)
        total_errors += 1

    # ----- Huawei -----
    try:
        common, processed, skipped, errs = (
            _run_huawei(
                portfolio, sheets, date_iso, args.plant_key,
                irradiance_client, cloud_client, args.dry_run, log,
            )
            if brand_enabled("HUAWEI", args.brand, args.skip_brand)
            else ([], 0, 0, 0))
        all_common.extend(common)
        total_processed += processed
        total_skipped += skipped
        total_errors += errs
    except Exception as e:  # noqa: BLE001
        log.exception("Huawei pipeline crashed: %s", e)
        total_errors += 1

    # ----- SolarEdge -----
    try:
        common, processed, skipped, errs = (
            _run_solaredge(
                portfolio, sheets, date_iso, args.plant_key,
                irradiance_client, cloud_client, args.dry_run, log,
            )
            if brand_enabled("SOLAREDGE", args.brand, args.skip_brand)
            else ([], 0, 0, 0))
        all_common.extend(common)
        total_processed += processed
        total_skipped += skipped
        total_errors += errs
    except Exception as e:  # noqa: BLE001
        log.exception("SolarEdge pipeline crashed: %s", e)
        total_errors += 1

    # ----- SMA (NEW in Stage 6) -----
    try:
        common, processed, skipped, errs = (
            _run_sma(
                portfolio, sheets, date_iso, args.plant_key,
                irradiance_client, cloud_client, args.dry_run, log,
            )
            if brand_enabled("SMA", args.brand, args.skip_brand)
            else ([], 0, 0, 0))
        all_common.extend(common)
        total_processed += processed
        total_skipped += skipped
        total_errors += errs
    except Exception as e:  # noqa: BLE001
        log.exception("SMA pipeline crashed: %s", e)
        total_errors += 1

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

    # SyncRuns: log this run regardless of outcome. Must happen AFTER all
    # writes, so the row reflects the actual end-state and rows_written count.
    _finalize_and_log_run(
        result=run_result,
        sheets=sheets,
        total_processed=total_processed,
        total_skipped=total_skipped,
        total_errors=total_errors,
        rows_collected=len(all_common),
        dry_run=args.dry_run,
        log=log,
    )

    if total_errors == 0 and len(all_common) > 0:
        return 0
    if len(all_common) == 0:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
