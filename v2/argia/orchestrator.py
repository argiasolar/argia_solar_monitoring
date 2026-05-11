"""
Orchestrator: shared logic between argia_mont_daily.py and argia_mont_10min.py.

The two entry-point scripts are intentionally thin. Anything they have in
common — opening the sheet, loading the portfolio, iterating plants, error
isolation — lives here so it's tested once and used twice.

DESIGN PRINCIPLES
-----------------
1. **Per-plant isolation**: a failure on one plant must never abort the rest
   of the run. The orchestrator catches and logs everything per plant.
2. **Dry-run is free**: ``dry_run=True`` does every API call but skips the
   final sheet write. Used for testing in CI.
3. **Single plant override**: ``only_plant`` runs just one plant, useful for
   debugging a flaky integration.
4. **Side effects are explicit**: writes only happen in the run_* functions
   when dry_run is False. Nothing else writes to the sheet.
"""

from __future__ import annotations

import datetime as dt
import logging
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from argia.core.config import Portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx, now_utc
from argia.meteo.growatt_irradiance import GrowattIrradianceClient
from argia.meteo.open_meteo import CloudCoverClient
from argia.vendors.base import VendorClient
from argia.vendors.factory import build_clients_for_active_plants

LOG = logging.getLogger("argia.orchestrator")

# Tab names
TAB_DAILY = "DailyProduction"
TAB_SNAP = "InverterSnapshot10m"
TAB_HEALTH = "HealthLog"
TAB_SYNC = "SyncRuns"


# ============================================================
# Per-run accounting
# ============================================================


@dataclass
class RunResult:
    """Result accounting for one orchestrator run, written to SyncRuns."""

    run_id: str
    started_at_utc: dt.datetime
    finished_at_utc: Optional[dt.datetime] = None
    script: str = ""
    status: str = "RUNNING"  # RUNNING | OK | PARTIAL | FAILED
    plants_processed: int = 0
    plants_skipped: int = 0
    rows_written: int = 0
    errors: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def add_error(self, plant_key: str, err: Exception) -> None:
        self.errors.append(f"{plant_key}: {type(err).__name__}: {err}")

    def finalize(self) -> None:
        self.finished_at_utc = now_utc()
        if not self.errors:
            self.status = "OK"
        elif self.plants_processed > 0:
            self.status = "PARTIAL"
        else:
            self.status = "FAILED"

    def to_sheet_row(self) -> List:
        """SyncRuns row in canonical column order."""
        return [
            self.run_id,
            self.started_at_utc.isoformat() if self.started_at_utc else "",
            self.finished_at_utc.isoformat() if self.finished_at_utc else "",
            self.script,
            self.status,
            self.plants_processed,
            self.rows_written,
            " | ".join(self.errors) if self.errors else "",
        ]


def new_run_id() -> str:
    """Short unique id for a run. Easy to grep in logs."""
    return f"{int(time.time())}-{uuid.uuid4().hex[:6]}-{socket.gethostname()[:10]}"


# ============================================================
# Daily aggregate
# ============================================================


def run_daily(
    sheets: SheetsClient,
    portfolio: Portfolio,
    date_iso: str,
    dry_run: bool = False,
    only_plant: Optional[str] = None,
    client_factory: Callable[[Dict], Dict[str, VendorClient]] = build_clients_for_active_plants,
) -> RunResult:
    """
    Daily aggregate run: one row per active plant in DailyProduction.

    Computes (real_kwh, irradiance, cloud, expected, pr) for the date.
    Uses idempotent upsert on (date, plant_key) so re-running is safe.
    """
    result = RunResult(
        run_id=new_run_id(),
        started_at_utc=now_utc(),
        script="argia_mont_daily",
    )

    LOG.info(
        "Starting daily run %s for date %s (dry_run=%s, only_plant=%s)",
        result.run_id, date_iso, dry_run, only_plant,
    )

    plants = portfolio.plants
    if only_plant:
        plants = {only_plant: plants[only_plant]} if only_plant in plants else {}

    clients = client_factory(plants)
    if not clients:
        LOG.warning("No active plants with valid credentials — nothing to do")
        result.finalize()
        return result

    cloud_client = CloudCoverClient()
    irradiance_client: Optional[GrowattIrradianceClient] = None

    rows_to_write: List[List] = []

    for plant_key, plant in plants.items():
        if plant_key not in clients:
            continue  # already logged by factory

        try:
            client = clients[plant_key]
            client.login()

            real_kwh = client.fetch_day_kwh(plant, date_iso)
            LOG.info("[%s] real_kwh=%s", plant_key, real_kwh)

            # Cloud cover (Open-Meteo)
            cloud_pct: Optional[float] = None
            if plant.lat is not None and plant.lon is not None:
                try:
                    cloud_pct = cloud_client.fetch_avg_cloudcover_pct(
                        plant.lat, plant.lon, date_iso
                    )
                except Exception as e:  # noqa: BLE001
                    LOG.warning("[%s] cloud cover fetch failed: %s", plant_key, e)

            # Irradiance (Growatt ShineMaster — shared across vendors)
            irradiance_kwh_m2: Optional[float] = None
            if plant.weather_plant_id and plant.datalogger_sn:
                if irradiance_client is None:
                    # Lazy init — only need it if at least one plant has weather config
                    irradiance_client = _build_irradiance_client(clients)
                if irradiance_client is not None:
                    try:
                        irradiance_kwh_m2 = irradiance_client.fetch_daily_irradiance_kwh_m2(
                            plant_id=plant.weather_plant_id,
                            date_iso=date_iso,
                            prefer_sn=plant.datalogger_sn,
                            prefer_addr=plant.datalogger_addr,
                        )
                    except Exception as e:  # noqa: BLE001
                        LOG.warning("[%s] irradiance fetch failed: %s", plant_key, e)

            # Expected kWh = kWp_DC * irradiance * expected_factor (rough model)
            expected_kwh: Optional[float] = None
            if irradiance_kwh_m2 is not None and plant.kwp_dc:
                expected_kwh = plant.kwp_dc * irradiance_kwh_m2 * plant.expected_factor

            # Performance Ratio = real / expected
            pr_pct: Optional[float] = None
            if expected_kwh and expected_kwh > 0 and real_kwh is not None:
                pr_pct = round(100.0 * real_kwh / expected_kwh, 2)

            rows_to_write.append([
                date_iso,
                plant_key,
                plant.brand,
                round(real_kwh, 3) if real_kwh is not None else "",
                round(irradiance_kwh_m2, 4) if irradiance_kwh_m2 is not None else "",
                round(cloud_pct, 2) if cloud_pct is not None else "",
                round(expected_kwh, 3) if expected_kwh is not None else "",
                pr_pct if pr_pct is not None else "",
                f"v2/{plant.brand.lower()}",
                now_utc().isoformat(),
            ])
            result.plants_processed += 1

        except Exception as e:  # noqa: BLE001 - per-plant isolation
            LOG.exception("[%s] daily run failed", plant_key)
            result.add_error(plant_key, e)
            result.plants_skipped += 1

    # Write to sheet (idempotent on date + plant_key)
    if rows_to_write and not dry_run:
        try:
            stats = sheets.upsert_rows(
                tab=TAB_DAILY,
                rows=rows_to_write,
                natural_key_columns=[0, 1],  # date, plant_key
            )
            result.rows_written = stats.get("inserted", 0) + stats.get("updated", 0)
            LOG.info("DailyProduction upsert: %s", stats)
        except Exception as e:  # noqa: BLE001
            LOG.exception("Failed to upsert daily rows")
            result.add_error("__sheets__", e)
    elif rows_to_write and dry_run:
        LOG.info("[DRY RUN] would write %d rows to %s", len(rows_to_write), TAB_DAILY)
        for r in rows_to_write:
            LOG.info("[DRY RUN]   %s", r)

    result.finalize()

    # Write SyncRuns row (also skipped in dry_run)
    if not dry_run:
        try:
            sheets.append_rows(TAB_SYNC, [result.to_sheet_row()])
        except Exception as e:  # noqa: BLE001
            LOG.warning("Failed to write SyncRuns row: %s", e)

    return result


# ============================================================
# 10-minute snapshot
# ============================================================


def run_snapshot10m(
    sheets: SheetsClient,
    portfolio: Portfolio,
    dry_run: bool = False,
    only_plant: Optional[str] = None,
    client_factory: Callable[[Dict], Dict[str, VendorClient]] = build_clients_for_active_plants,
) -> RunResult:
    """
    10-min snapshot: one row per active inverter in InverterSnapshot10m.

    Appends — never updates. The natural key would be (timestamp, sn) but
    since we never re-run for the same exact timestamp, append is safe.
    """
    result = RunResult(
        run_id=new_run_id(),
        started_at_utc=now_utc(),
        script="argia_mont_10min",
    )

    LOG.info(
        "Starting 10-min run %s (dry_run=%s, only_plant=%s)",
        result.run_id, dry_run, only_plant,
    )

    plants = portfolio.plants
    if only_plant:
        plants = {only_plant: plants[only_plant]} if only_plant in plants else {}

    clients = client_factory(plants)
    if not clients:
        LOG.warning("No active plants with valid credentials — nothing to do")
        result.finalize()
        return result

    cloud_client = CloudCoverClient()
    irradiance_client: Optional[GrowattIrradianceClient] = None
    snapshot_mx = now_mx()
    date_iso = snapshot_mx.date().isoformat()

    rows_to_write: List[List] = []

    for plant_key, plant in plants.items():
        if plant_key not in clients:
            continue

        try:
            client = clients[plant_key]
            client.login()

            inverters = portfolio.inverters_for(plant_key)
            if not inverters:
                LOG.info("[%s] no active inverters configured — skipping", plant_key)
                continue

            snapshots = client.fetch_inverter_snapshots(plant, inverters)

            # Cloud cover (for the day — same value across the day's snapshots)
            cloud_frac: Optional[float] = None
            if plant.lat is not None and plant.lon is not None:
                try:
                    pct = cloud_client.fetch_avg_cloudcover_pct(
                        plant.lat, plant.lon, date_iso
                    )
                    if pct is not None:
                        cloud_frac = round(pct / 100.0, 4)
                except Exception as e:  # noqa: BLE001
                    LOG.warning("[%s] cloud cover fetch failed: %s", plant_key, e)

            # Current irradiance in kWh/m² over the 10-min interval
            irr_kwh_m2_10min: Optional[float] = None
            if plant.weather_plant_id and plant.datalogger_sn:
                if irradiance_client is None:
                    irradiance_client = _build_irradiance_client(clients)
                if irradiance_client is not None:
                    try:
                        from argia.meteo.growatt_irradiance import interval_kwh_m2_from_wm2

                        wm2 = irradiance_client.fetch_current_irradiance_wm2(
                            plant_id=plant.weather_plant_id,
                            date_iso=date_iso,
                            prefer_sn=plant.datalogger_sn,
                            prefer_addr=plant.datalogger_addr,
                        )
                        if wm2 is not None:
                            irr_kwh_m2_10min = interval_kwh_m2_from_wm2(wm2, interval_min=10)
                    except Exception as e:  # noqa: BLE001
                        LOG.warning("[%s] irradiance snapshot failed: %s", plant_key, e)

            for snap in snapshots:
                rows_to_write.append([
                    snapshot_mx.strftime("%Y-%m-%d %H:%M:%S"),
                    snap.timestamp_utc.isoformat() if snap.timestamp_utc else "",
                    plant_key,
                    snap.inverter_sn,
                    snap.status,
                    snap.power_w if snap.power_w is not None else "",
                    snap.etoday_kwh if snap.etoday_kwh is not None else "",
                    irr_kwh_m2_10min if irr_kwh_m2_10min is not None else "",
                    cloud_frac if cloud_frac is not None else "",
                    f"v2/{plant.brand.lower()}",
                ])

            result.plants_processed += 1

        except Exception as e:  # noqa: BLE001
            LOG.exception("[%s] snapshot run failed", plant_key)
            result.add_error(plant_key, e)
            result.plants_skipped += 1

    if rows_to_write and not dry_run:
        try:
            written = sheets.append_rows(TAB_SNAP, rows_to_write)
            result.rows_written = written
            LOG.info("Appended %d rows to %s", written, TAB_SNAP)
        except Exception as e:  # noqa: BLE001
            LOG.exception("Failed to append snapshot rows")
            result.add_error("__sheets__", e)
    elif rows_to_write and dry_run:
        LOG.info("[DRY RUN] would append %d rows to %s", len(rows_to_write), TAB_SNAP)
        for r in rows_to_write[:5]:
            LOG.info("[DRY RUN]   %s", r)
        if len(rows_to_write) > 5:
            LOG.info("[DRY RUN]   ... and %d more", len(rows_to_write) - 5)

    result.finalize()

    if not dry_run:
        try:
            sheets.append_rows(TAB_SYNC, [result.to_sheet_row()])
        except Exception as e:  # noqa: BLE001
            LOG.warning("Failed to write SyncRuns row: %s", e)

    return result


# ============================================================
# Helpers
# ============================================================


def _build_irradiance_client(
    clients: Dict[str, VendorClient],
) -> Optional[GrowattIrradianceClient]:
    """
    Find the first Growatt client in the active set and re-use its session
    for ShineMaster irradiance queries. Returns None if no Growatt web
    credentials are available (the irradiance endpoints use the web UI,
    not the Open API).
    """
    import os
    from argia.meteo.growatt_irradiance import GrowattWebSession
    from argia.vendors.growatt import GrowattClient

    # Prefer pulling web creds from the first active GrowattClient that has them
    for c in clients.values():
        if isinstance(c, GrowattClient) and c._web_user and c._web_pass:
            creds = GrowattWebSession(username=c._web_user, password=c._web_pass)
            return GrowattIrradianceClient(session_creds=creds, http_session=c._session)

    # Fallback: read web creds directly from env vars (the irradiance endpoints
    # require the web UI, which the API-token-only Growatt clients don't use)
    user = os.environ.get("GROWATT_USERNAME", "").strip()
    pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
    if user and pwd:
        creds = GrowattWebSession(username=user, password=pwd)
        return GrowattIrradianceClient(session_creds=creds)

    LOG.info(
        "No Growatt web credentials available — irradiance queries will be skipped. "
        "Set GROWATT_USERNAME + GROWATT_PASSWORD to enable."
    )
    return None
