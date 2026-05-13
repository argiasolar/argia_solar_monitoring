"""
Growatt vendor client.

Two strategies under one roof:
  - OPEN API   (preferred): official JSON API at openapi.growatt.com
                requires GROWATT_API_TOKEN
  - WEB UI     (fallback):  authenticated JSON API at server.growatt.com
                requires GROWATT_USERNAME + GROWATT_PASSWORD

The orchestrator doesn't know which path is active. Both implement
VendorClient.fetch_day_kwh / fetch_inverter_snapshots.

Decision rule for each call:
  1. If Open API token is set, try Open API first.
  2. On 401/403/auth failure, fall back to web UI (if creds available).
  3. Web-only mode: skip Open API entirely.

Stage 2 change (2026-05-13): the web UI path no longer scrapes HTML or
probes multiple unknown endpoints. It uses ``argia.vendors.growatt_web``
which talks to the documented JSON endpoints we captured in Stage 0 and
parsed in Stage 1. Same public API, much sturdier internals.

This consolidates 5 v1 files into one:
  argia_growatt.py
  argia_growatt_monitoring.py
  argia_growatt_health_client.py
  argia_growatt_inverters.py
  argia_snap.py / argia_sync.py (Growatt parts)

NOTE: irradiance/env-station data is intentionally NOT here. That's a
separate concern handled by argia.meteo.growatt_irradiance (stage 4).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from argia.core.config import InverterConfig, PlantConfig
from argia.core.normalize import normalize_sn, pick, safe_float
from argia.core.time_utils import MX_TZ, now_utc
from argia.vendors.base import InverterSnapshot, normalize_status
from argia.vendors.growatt_web import (
    GrowattWebClient,
    GrowattAuthError as WebAuthError,
    GrowattAPIError as WebAPIError,
)
from argia.vendors.growatt_web_parser import (
    build_inverter_snapshot,
    extract_latest_row,
    parse_max_history,
    parse_max_total_data,
)

LOG = logging.getLogger("argia.vendors.growatt")

OPEN_API_BASE = "https://openapi.growatt.com"
DEFAULT_TIMEOUT_SEC = 30
PER_INVERTER_DELAY_SEC = 0.2  # be polite to Growatt's servers


class GrowattAuthError(RuntimeError):
    """Auth failure on either Open API or web UI."""


class GrowattAPIError(RuntimeError):
    """Non-auth API failure on either Open API or web UI."""


class GrowattClient:
    """
    Dual-strategy Growatt client. Pass at least one of:
      - api_token: Open API token (preferred)
      - web_username + web_password: web UI JSON-API creds (fallback)
    """

    brand = "GROWATT"

    def __init__(
        self,
        *,
        api_token: Optional[str] = None,
        web_username: Optional[str] = None,
        web_password: Optional[str] = None,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not api_token and not (web_username and web_password):
            raise ValueError(
                "GrowattClient needs api_token OR (web_username + web_password)"
            )

        self._api_token = api_token
        self._web_user = web_username
        self._web_pass = web_password
        self._timeout = timeout_sec
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0 (Argia_Mont/2.0)"})

        # Stage 2: lazy-init the web client so web-only constructions work and
        # API-token-only constructions never even build the web client.
        self._web_client: Optional[GrowattWebClient] = None

    # ===== public VendorClient interface =====

    def login(self) -> None:
        """
        Lazy login. Open API doesn't need a session login — just a header on
        each call. Web UI does, but the web client handles its own login
        the first time it's used.
        """
        # Nothing eager to do. _get_web_client() lazy-logs-in on demand.

    def fetch_day_kwh(self, plant: PlantConfig, date_iso: str) -> Optional[float]:
        """
        Total kWh produced by the plant on the local date ``date_iso``.
        Open API first, web UI fallback.

        Returns None on any failure (orchestrator treats None as
        per-plant error and writes PARTIAL status — see test_orchestrator
        regression for the MEX2/None contract).
        """
        if self._api_token:
            try:
                value = self._fetch_day_kwh_open_api(plant, date_iso)
                if value is not None:
                    return value
                # Open API returned None (no data for that day). Don't fall
                # back — the data just doesn't exist.
                return None
            except GrowattAuthError:
                LOG.warning(
                    "Open API auth failed for %s; falling back to web UI",
                    plant.plant_key,
                )
            except GrowattAPIError as e:
                LOG.warning(
                    "Open API error for %s (%s); falling back to web UI",
                    plant.plant_key, e,
                )

        if self._web_user and self._web_pass:
            return self._fetch_day_kwh_web(plant, date_iso)

        return None

    def fetch_inverter_snapshots(
        self,
        plant: PlantConfig,
        inverters: List[InverterConfig],
    ) -> List[InverterSnapshot]:
        """
        Live snapshot for the given inverters.
        Open API first, web UI fallback.

        Returns [] on any failure (orchestrator treats empty list as a
        missed snapshot and logs PARTIAL).
        """
        if not inverters:
            return []

        if self._api_token:
            try:
                snaps = self._fetch_inverters_open_api(plant, inverters)
                if snaps:
                    return snaps
            except GrowattAuthError:
                LOG.warning(
                    "Open API auth failed for %s inverters; falling back",
                    plant.plant_key,
                )
            except GrowattAPIError as e:
                LOG.warning(
                    "Open API inverter error for %s (%s); falling back",
                    plant.plant_key, e,
                )

        if self._web_user and self._web_pass:
            return self._fetch_inverters_web(plant, inverters)

        return []

    # ===== Open API implementation =====
    # Docs: https://www.showdoc.com.cn/262556420217021/1494064780850116
    # Endpoints used:
    #   GET  /v1/plant/data           -- plant totals (today_energy)
    #   GET  /v1/device/inverter/all  -- list of inverters in a plant
    #   GET  /v1/device/inverter/data -- per-inverter status + day energy

    def _open_api_get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Single low-level GET. Tests mock THIS method."""
        url = f"{OPEN_API_BASE}{path}"
        headers = {"token": self._api_token, "Accept": "application/json"}
        resp = self._session.get(
            url, params=params, headers=headers, timeout=self._timeout
        )
        if resp.status_code in (401, 403):
            raise GrowattAuthError(
                f"Open API {path} returned HTTP {resp.status_code} — token rejected"
            )
        if resp.status_code != 200:
            raise GrowattAPIError(
                f"Open API {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise GrowattAPIError(f"Open API {path} returned invalid JSON: {e}") from e

    def _fetch_day_kwh_open_api(
        self, plant: PlantConfig, date_iso: str
    ) -> Optional[float]:
        """
        Open API returns 'today_energy' for the current day only. For
        historical days we'd need /v1/plant/energy, but v1 only ever queries
        today, so we match that behavior and return None for non-today
        queries.
        """
        result = self._open_api_get("/v1/plant/data", {"plant_id": plant.site_id})

        # Growatt returns {"data": {...}} on success, {"error_code": N} on error
        if "error_code" in result and result.get("error_code"):
            raise GrowattAPIError(
                f"plant/data error_code={result.get('error_code')}: "
                f"{result.get('error_msg')}"
            )

        data = result.get("data") or {}
        if not isinstance(data, dict):
            return None

        return safe_float(pick(data, ["today_energy", "todayEnergy", "day_energy"]))

    def _fetch_inverters_open_api(
        self,
        plant: PlantConfig,
        inverters: List[InverterConfig],
    ) -> List[InverterSnapshot]:
        """
        Walk /v1/device/inverter/all to get the SN list, then call
        /v1/device/inverter/data for each SN we care about.
        """
        wanted = {inv.inverter_sn for inv in inverters}

        # Step 1: list inverters in the plant
        list_resp = self._open_api_get(
            "/v1/device/inverter/all", {"plant_id": plant.site_id}
        )
        if list_resp.get("error_code"):
            raise GrowattAPIError(
                f"inverter/all error_code={list_resp.get('error_code')}"
            )

        inverter_list = (list_resp.get("data") or {}).get("inverters") or []
        if not isinstance(inverter_list, list):
            inverter_list = []

        # Step 2: per-inverter detail for the SNs we want
        snapshots: List[InverterSnapshot] = []
        for raw in inverter_list:
            if not isinstance(raw, dict):
                continue
            sn = normalize_sn(pick(raw, ["sn", "device_sn", "deviceSn"]))
            if sn not in wanted:
                continue

            try:
                detail = self._open_api_get(
                    "/v1/device/inverter/data", {"device_sn": sn}
                )
            except GrowattAPIError as e:
                LOG.warning("inverter/data failed for %s: %s", sn, e)
                continue

            snap = self._parse_open_api_inverter(
                raw, detail.get("data") or {}, plant.plant_key
            )
            if snap:
                snapshots.append(snap)

            # Be polite to Growatt; v1 does this too
            time.sleep(PER_INVERTER_DELAY_SEC)

        return snapshots

    @staticmethod
    def _parse_open_api_inverter(
        list_item: Dict[str, Any],
        detail_data: Dict[str, Any],
        plant_key: str,
    ) -> Optional[InverterSnapshot]:
        """Pure function — fully testable from JSON fixtures."""
        sn = normalize_sn(pick(list_item, ["sn", "device_sn", "deviceSn"]))
        if not sn:
            return None

        status_raw = pick(
            list_item, ["status", "device_status", "deviceStatus"]
        ) or pick(detail_data, ["status", "device_status"])

        # Detail data is more reliable for energy values; list for status
        power = safe_float(pick(detail_data, ["pac", "current_power", "currentPower"]))
        if power is not None and abs(power) <= 1000:
            # API sometimes returns kW
            power_w = power * 1000.0
        else:
            power_w = power

        etoday = safe_float(
            pick(detail_data, ["e_today", "eToday", "today_energy", "todayEnergy"])
        )

        # Use a recent UTC timestamp; per-inverter timestamps are unreliable
        # in Open API responses
        ts_utc = now_utc()

        return InverterSnapshot(
            plant_key=plant_key,
            inverter_sn=sn,
            timestamp_utc=ts_utc,
            status=normalize_status(status_raw),
            power_w=power_w,
            etoday_kwh=etoday,
            raw_status=str(status_raw) if status_raw is not None else "",
        )

    # ===== Web UI implementation (Stage 2: uses GrowattWebClient) =====

    def _get_web_client(self) -> GrowattWebClient:
        """Lazy-init the web client. Login is idempotent inside it."""
        if self._web_client is None:
            self._web_client = GrowattWebClient(
                username=self._web_user,
                password=self._web_pass,
                session=self._session,
                timeout_sec=self._timeout,
            )
        # GrowattWebClient.login() is idempotent (no-op after first success)
        self._web_client.login()
        return self._web_client

    def _fetch_day_kwh_web(
        self, plant: PlantConfig, date_iso: str
    ) -> Optional[float]:
        """
        Plant-level eToday via getMAXTotalData.

        Stage-2 change: was HTML regex against /device/photovoltaic; now
        single JSON call returning typed dataclass. The parser handles
        Growatt's quirk of returning string-coerced floats.

        Note: getMAXTotalData has no date parameter — it's always "today" in
        plant local time. For non-today dates we return None, matching the
        Open API behavior. If you ever need historical days, the right
        endpoint is getMAXDayChart (returns 288 5-min slots that can be
        summed) — wire that in then.
        """
        # Compare against MX-local "today" (Growatt server is on plant TZ)
        today_local = dt.datetime.now(MX_TZ).strftime("%Y-%m-%d")
        if date_iso != today_local:
            LOG.debug(
                "Growatt web path only returns today's energy; "
                "asked for %s but local today is %s — skipping",
                date_iso, today_local,
            )
            return None

        try:
            web = self._get_web_client()
            envelope = web.get_max_total_data(plant.site_id)
        except WebAuthError as e:
            LOG.warning("Growatt web auth failed for %s: %s", plant.plant_key, e)
            return None
        except WebAPIError as e:
            LOG.warning("Growatt web API error for %s: %s", plant.plant_key, e)
            return None

        total = parse_max_total_data(envelope)
        if total is None:
            return None
        return total.e_today_kwh

    def _fetch_inverters_web(
        self,
        plant: PlantConfig,
        inverters: List[InverterConfig],
    ) -> List[InverterSnapshot]:
        """
        Per-inverter snapshots via getMAXHistory.

        Stage-2 change: was multi-endpoint device-list scraping (4 URLs × 2
        payloads, hoping one worked); now one canonical endpoint per SN,
        documented and tested. getMAXHistory returns ~150 5-min rows for
        today; we pick the latest and build a snapshot.

        Cost note: this is N HTTP calls for N inverters with a small delay
        between them. For TAIGENE (4 inverters) that's ~1.2s. The old
        device-list approach was 1-8 calls (variants) and returned only one
        SN per response anyway due to a Growatt-side bug.
        """
        try:
            web = self._get_web_client()
        except WebAuthError as e:
            LOG.warning(
                "Growatt web auth failed for %s inverters: %s", plant.plant_key, e
            )
            return []

        today_local = dt.datetime.now(MX_TZ).strftime("%Y-%m-%d")

        snapshots: List[InverterSnapshot] = []
        for i, inv in enumerate(inverters):
            sn = normalize_sn(inv.inverter_sn)
            if not sn:
                continue

            try:
                envelope = web.get_max_history(sn, today_local, start=0)
            except WebAuthError as e:
                LOG.warning(
                    "Growatt web auth lost mid-loop for %s/%s: %s",
                    plant.plant_key, sn, e,
                )
                # If auth fails mid-loop, no point continuing — every call
                # after this would fail the same way.
                return snapshots
            except WebAPIError as e:
                LOG.warning(
                    "Growatt web getMAXHistory failed for %s/%s: %s",
                    plant.plant_key, sn, e,
                )
                continue

            rows = parse_max_history(envelope)
            latest = extract_latest_row(rows)
            if latest is None:
                LOG.debug(
                    "No history rows yet for %s/%s on %s",
                    plant.plant_key, sn, today_local,
                )
                continue

            snap = build_inverter_snapshot(
                latest, plant_key=plant.plant_key, inverter_sn=sn
            )
            snapshots.append(snap)

            # Be polite (last iteration doesn't need a delay)
            if i < len(inverters) - 1:
                time.sleep(PER_INVERTER_DELAY_SEC)

        return snapshots
