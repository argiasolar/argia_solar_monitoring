"""
Growatt vendor client.

Two strategies under one roof:
  - OPEN API   (preferred): official JSON API at openapi.growatt.com
                requires GROWATT_API_TOKEN
  - WEB UI     (fallback):  scrapes server.growatt.com
                requires GROWATT_USERNAME + GROWATT_PASSWORD

The orchestrator doesn't know which path is active. Both implement
VendorClient.fetch_day_kwh / fetch_inverter_snapshots.

Decision rule for each call:
  1. If Open API token is set, try Open API first.
  2. On 401/403/quota-exceeded, fall back to web UI (if creds available).
  3. Web-only mode: skip Open API entirely.

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

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from argia.core.config import InverterConfig, PlantConfig
from argia.core.normalize import normalize_sn, pick, safe_float
from argia.core.time_utils import now_utc, parse_provider_datetime
from argia.vendors.base import InverterSnapshot, normalize_status

LOG = logging.getLogger("argia.vendors.growatt")

OPEN_API_BASE = "https://openapi.growatt.com"
WEB_UI_BASE = "https://server.growatt.com"
DEFAULT_TIMEOUT_SEC = 30

# Endpoints that v1 confirmed safe to call (NEVER call setMax / setTlx / etc.)
WEB_UI_UNSAFE_PREFIXES = ("/commonDeviceSetC/",)
WEB_UI_UNSAFE_KEYWORDS = (
    "setmax", "settlx", "setinverter",
    "delmax", "deltlx", "delinverter",
    "delete", "set", "save",
)


class GrowattAuthError(RuntimeError):
    pass


class GrowattAPIError(RuntimeError):
    pass


class GrowattClient:
    """
    Dual-strategy Growatt client. Pass at least one of:
      - api_token: Open API token (preferred)
      - web_username + web_password: web UI scraping creds (fallback)
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

        self._web_logged_in = False

    # ===== public VendorClient interface =====

    def login(self) -> None:
        """
        Lazy login. Open API doesn't need a session login — just a header on
        each call. Web UI does. We only authenticate the web session if it's
        actually going to be used.
        """
        # Nothing to do at construction; login_web() is called on demand.

    def fetch_day_kwh(self, plant: PlantConfig, date_iso: str) -> Optional[float]:
        """
        Total kWh produced by the plant on the local date ``date_iso``.
        Open API first, web UI fallback.
        """
        if self._api_token:
            try:
                value = self._fetch_day_kwh_open_api(plant, date_iso)
                if value is not None:
                    return value
                # Open API returned None (e.g. no data for that day).
                # Don't fall back — the data just doesn't exist.
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
        Live snapshot for the given inverters. Open API first, web UI fallback.
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
        Open API returns 'today_energy' for the current day only. For historical
        days we'd need /v1/plant/energy, but v1 only ever queries today, so we
        match that behavior and return None for non-today queries.
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

            snap = self._parse_open_api_inverter(raw, detail.get("data") or {}, plant.plant_key)
            if snap:
                snapshots.append(snap)

            # Be polite to Growatt; v1 does this too
            time.sleep(0.2)

        return snapshots

    @staticmethod
    def _parse_open_api_inverter(
        list_item: Dict[str, Any],
        detail_data: Dict[str, Any],
        plant_key: str,
    ) -> Optional[InverterSnapshot]:
        """Pure function — fully testable from JSON fixtures."""
        sn = normalize_sn(
            pick(list_item, ["sn", "device_sn", "deviceSn"])
        )
        if not sn:
            return None

        # Open API returns "status" 0/1/3 in list, more detail in data block
        status_raw = pick(detail_data, ["status", "device_status"]) or pick(
            list_item, ["status"]
        )

        # Power: Open API gives W in /data, kW in some fields
        power_raw = safe_float(
            pick(detail_data, ["pac", "ppv", "power"])
        )
        if power_raw is None:
            power_w = None
        elif abs(power_raw) <= 1000:
            # Heuristic: <=1000 likely kW
            power_w = power_raw * 1000.0
        else:
            power_w = power_raw

        etoday = safe_float(
            pick(detail_data, ["e_today", "eToday", "today_energy"])
        )

        ts_raw = pick(detail_data, ["last_update_time", "time", "data_log_time"])
        ts = parse_provider_datetime(ts_raw) or now_utc()

        return InverterSnapshot(
            plant_key=plant_key,
            inverter_sn=sn,
            timestamp_utc=ts,
            status=normalize_status(status_raw),
            power_w=power_w,
            etoday_kwh=etoday,
            raw_status=str(status_raw) if status_raw is not None else "",
        )

    # ===== Web UI implementation (fallback) =====

    def _ensure_web_logged_in(self) -> None:
        if self._web_logged_in:
            return
        self._web_login()

    def _web_login(self) -> None:
        """
        POST credentials to /login. Success means the response sets the
        ``assToken`` cookie. Tests mock this whole method via the session.
        """
        # Prime session with a GET so initial cookies are set
        self._session.get(f"{WEB_UI_BASE}/login", timeout=self._timeout)

        resp = self._session.post(
            f"{WEB_UI_BASE}/login",
            data={"account": self._web_user, "password": self._web_pass},
            timeout=self._timeout,
        )
        cookies = self._session.cookies.get_dict()
        if "assToken" not in cookies:
            raise GrowattAuthError(
                f"Web UI login failed (no assToken cookie). HTTP {resp.status_code}"
            )
        self._web_logged_in = True
        LOG.info("Growatt web UI login OK")

    def _web_get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> requests.Response:
        if any(path.startswith(p) for p in WEB_UI_UNSAFE_PREFIXES):
            raise ValueError(f"Refusing to call unsafe web UI path: {path}")
        if any(kw in path.lower() for kw in WEB_UI_UNSAFE_KEYWORDS):
            raise ValueError(f"Refusing to call unsafe web UI path: {path}")
        return self._session.get(
            f"{WEB_UI_BASE}{path}", params=params, timeout=self._timeout
        )

    def _web_post(
        self, path: str, data: Optional[Dict[str, Any]] = None
    ) -> requests.Response:
        if any(path.startswith(p) for p in WEB_UI_UNSAFE_PREFIXES):
            raise ValueError(f"Refusing to call unsafe web UI path: {path}")
        if any(kw in path.lower() for kw in WEB_UI_UNSAFE_KEYWORDS):
            raise ValueError(f"Refusing to call unsafe web UI path: {path}")
        return self._session.post(
            f"{WEB_UI_BASE}{path}",
            data=data or {},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=self._timeout,
        )

    def _fetch_day_kwh_web(
        self, plant: PlantConfig, date_iso: str
    ) -> Optional[float]:
        """
        Scrape the photovoltaic page for ``val_device_plantEToday``. v1 used
        this exact pattern in argia_growatt_monitoring.parse_plant_etoday_kwh.
        """
        self._ensure_web_logged_in()
        resp = self._web_get(
            "/device/photovoltaic", params={"plantId": plant.site_id}
        )
        if resp.status_code != 200:
            raise GrowattAPIError(
                f"photovoltaic page HTTP {resp.status_code} for plant {plant.site_id}"
            )
        return self._parse_plant_etoday_html(resp.text)

    @staticmethod
    def _parse_plant_etoday_html(html: str) -> Optional[float]:
        """Pure function — fully testable from a captured HTML fixture."""
        if not html:
            return None
        # Primary pattern: <span class="val_device_plantEToday">123.4</span>
        m = re.search(
            r'class\s*=\s*["\']val_device_plantEToday["\'][^>]*>\s*([0-9.]+)\s*<',
            html,
        )
        if m:
            return safe_float(m.group(1))
        # Fallback: any number near the plantEToday label
        m2 = re.search(r'plantEToday[^0-9]*([0-9]+(?:\.[0-9]+)?)', html)
        return safe_float(m2.group(1)) if m2 else None

    def _fetch_inverters_web(
        self,
        plant: PlantConfig,
        inverters: List[InverterConfig],
    ) -> List[InverterSnapshot]:
        """
        Scrape the inverter list endpoint. v1's argia_growatt_inverters.py
        does this with endpoint discovery + scoring; v2 uses a single known
        endpoint with payload variants. If Growatt changes the endpoint, the
        fix is one constant.
        """
        self._ensure_web_logged_in()
        wanted = {inv.inverter_sn for inv in inverters}

        # Warm plant context (some endpoints check this cookie)
        self._web_get("/device", params={"plantId": plant.site_id})
        self._session.cookies.set(
            "selectedPlantId", plant.site_id,
            domain="server.growatt.com", path="/",
        )

        items = self._web_fetch_device_list(plant.site_id)

        snapshots: List[InverterSnapshot] = []
        for raw in items:
            sn = normalize_sn(pick(raw, ["sn", "deviceSn", "invSn", "serialNum"]))
            if sn not in wanted:
                continue
            snap = self._parse_web_inverter(raw, plant.plant_key)
            if snap:
                snapshots.append(snap)
        return snapshots

    def _web_fetch_device_list(self, plant_id: str) -> List[Dict[str, Any]]:
        """
        Try known list endpoints with payload variants. Return first list
        that has at least one row with an SN-like field.
        """
        endpoints = [
            "/device/getMAXList",
            "/device/getMaxList",
            "/device/getInverterList",
            "/panel/getDeviceList",
        ]
        payload_variants = [
            {"plantId": plant_id, "currPage": "1", "pageSize": "50"},
            {"plantId": plant_id, "currPage": "1", "pageSize": "50", "ind": "1"},
        ]

        for endpoint in endpoints:
            for payload in payload_variants:
                try:
                    resp = self._web_post(endpoint, data=payload)
                except (requests.RequestException, ValueError):
                    continue
                if resp.status_code != 200:
                    continue
                items = self._extract_items_from_json(resp.text)
                if items:
                    return items
        return []

    @staticmethod
    def _extract_items_from_json(text: str) -> List[Dict[str, Any]]:
        """Pure function — fully testable."""
        import json as _json
        if not text:
            return []
        try:
            obj = _json.loads(text)
        except (ValueError, TypeError):
            return []
        if not isinstance(obj, dict):
            return []
        for key in ("datas", "data", "rows"):
            value = obj.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return []

    @staticmethod
    def _parse_web_inverter(
        raw: Dict[str, Any], plant_key: str
    ) -> Optional[InverterSnapshot]:
        """Pure function. Web UI returns slightly different field names."""
        sn = normalize_sn(pick(raw, ["sn", "deviceSn", "invSn", "serialNum"]))
        if not sn:
            return None

        status_raw = pick(raw, ["status", "deviceStatus", "invStatus", "workStatus"])

        # Web UI sometimes returns power as kW, sometimes already in W
        power_raw = safe_float(pick(raw, ["pac", "power", "actPower", "currentPower"]))
        if power_raw is None:
            power_w = None
        elif abs(power_raw) <= 1000:
            power_w = power_raw * 1000.0
        else:
            power_w = power_raw

        etoday = safe_float(
            pick(raw, ["eToday", "EToday", "todayEnergy", "generationToday"])
        )

        ts_raw = pick(raw, ["updateTime", "lastUpdateTime", "time"])
        ts = parse_provider_datetime(ts_raw) or now_utc()

        return InverterSnapshot(
            plant_key=plant_key,
            inverter_sn=sn,
            timestamp_utc=ts,
            status=normalize_status(status_raw),
            power_w=power_w,
            etoday_kwh=etoday,
            raw_status=str(status_raw) if status_raw is not None else "",
        )
