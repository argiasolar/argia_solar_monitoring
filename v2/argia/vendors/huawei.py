"""
Huawei FusionSolar (thirdData) vendor client.

Endpoints used:
  POST /thirdData/login
       body: {"userName": ..., "systemCode": ...}
       returns XSRF-TOKEN in cookies/headers

  POST /thirdData/getStationRealKpi
       body: {"stationCodes": "NE=1,NE=2"}
       returns dataItemMap.day_cap (kWh today)

  POST /thirdData/getDevRealKpi
       body: {"devTypeId": "1", "sns": "ES...,GR..."}
       returns per-inverter status, day_cap, active_power

Compared to v1:
- Single client class (v1 had two, in argia_huawei.py and argia_huawei_inverters.py).
- All HTTP I/O isolated behind ``_post_json`` so tests mock just that one method.
- Returns ``InverterSnapshot`` dataclass instead of writing rows to a sheet directly.
- Errors raised, not silently swallowed. The orchestrator decides what to do.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

import requests

from argia.core.config import InverterConfig, PlantConfig
from argia.core.normalize import chunked, normalize_sn, pick, safe_float
from argia.core.time_utils import now_utc, parse_provider_datetime
from argia.vendors.base import InverterSnapshot, normalize_status

LOG = logging.getLogger("argia.vendors.huawei")

DEFAULT_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"
DEFAULT_TIMEOUT_SEC = 30
INVERTER_DEV_TYPE_ID = 1
SN_BATCH_SIZE = 50  # API limit


class HuaweiAuthError(RuntimeError):
    pass


class HuaweiAPIError(RuntimeError):
    pass


class HuaweiClient:
    """
    Huawei FusionSolar thirdData API client.

    Stateful in the sense that ``login()`` populates an XSRF token in the
    session. Subsequent calls reuse it. Call ``login()`` again if you get
    a 401/403.
    """

    brand = "HUAWEI"

    def __init__(
        self,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not username or not password:
            raise ValueError("username and password are required")
        self._username = username
        self._password = password
        self._base = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._session = session or requests.Session()
        self._session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )
        self._logged_in = False

    # ----------------------- transport -----------------------

    def _post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Single low-level POST. Tests mock THIS method to avoid network.
        Returns parsed JSON. Raises HuaweiAPIError on non-200 or invalid JSON.
        """
        url = f"{self._base}{path}"
        resp = self._session.post(url, json=body, timeout=self._timeout)
        if resp.status_code != 200:
            raise HuaweiAPIError(
                f"Huawei {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise HuaweiAPIError(f"Huawei {path} returned invalid JSON: {e}") from e

    # ----------------------- auth -----------------------

    def login(self) -> None:
        """Acquire XSRF token. Idempotent; safe to retry."""
        url = f"{self._base}/login"
        resp = self._session.post(
            url,
            json={"userName": self._username, "systemCode": self._password},
            timeout=self._timeout,
        )
        token = resp.headers.get("XSRF-TOKEN") or resp.cookies.get("XSRF-TOKEN")
        if not token:
            raise HuaweiAuthError(
                f"Huawei login failed: no XSRF-TOKEN in response "
                f"(HTTP {resp.status_code})"
            )
        self._session.headers["XSRF-TOKEN"] = token
        self._logged_in = True
        LOG.info("Huawei login OK")

    def _ensure_logged_in(self) -> None:
        if not self._logged_in:
            self.login()

    # ----------------------- daily kWh -----------------------

    def fetch_day_kwh(self, plant: PlantConfig, date_iso: str) -> Optional[float]:
        """
        Returns plant's today-so-far energy in kWh.

        IMPORTANT: getStationRealKpi gives "today" for the station's local
        timezone. We do not currently support querying a specific past date
        for Huawei; date_iso is accepted for interface consistency but only
        "today" makes sense.

        If you need a historical day, use getKpiStationDay (not implemented
        here yet — out of scope for v2 stage 2).
        """
        self._ensure_logged_in()
        result = self._post_json(
            "/getStationRealKpi",
            {"stationCodes": plant.site_id},
        )

        if not result.get("success"):
            LOG.warning(
                "Huawei getStationRealKpi failed for %s: failCode=%s msg=%s",
                plant.plant_key,
                result.get("failCode"),
                result.get("message"),
            )
            return None

        for item in result.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("stationCode")) != plant.site_id:
                continue
            data_map = item.get("dataItemMap") or {}
            if not isinstance(data_map, dict):
                continue
            value = pick(data_map, ["day_cap", "daily_cap", "day_power"])
            return safe_float(value)
        return None

    # ----------------------- inverter snapshots -----------------------

    def fetch_inverter_snapshots(
        self,
        plant: PlantConfig,
        inverters: List[InverterConfig],
    ) -> List[InverterSnapshot]:
        """
        Returns one InverterSnapshot per inverter found by the API.
        Inverters not returned by Huawei are omitted from the result.
        """
        if not inverters:
            return []
        self._ensure_logged_in()

        sns = [inv.inverter_sn for inv in inverters]
        out: List[InverterSnapshot] = []

        for batch in chunked(sns, SN_BATCH_SIZE):
            result = self._post_json(
                "/getDevRealKpi",
                {"devTypeId": str(INVERTER_DEV_TYPE_ID), "sns": ",".join(batch)},
            )
            if not result.get("success"):
                raise HuaweiAPIError(
                    f"getDevRealKpi failed: failCode={result.get('failCode')} "
                    f"msg={result.get('message')}"
                )
            for item in result.get("data", []) or []:
                snap = self._parse_kpi_item(item, plant.plant_key)
                if snap is not None:
                    out.append(snap)
        return out

    @staticmethod
    def _parse_kpi_item(
        item: Dict[str, Any],
        plant_key: str,
    ) -> Optional[InverterSnapshot]:
        """
        Parse one item from getDevRealKpi.data into an InverterSnapshot.
        Pure function — fully testable from a JSON fixture.
        """
        if not isinstance(item, dict):
            return None

        sn = normalize_sn(
            pick(item, ["sn", "devSn", "deviceSn", "serialNum", "esn"])
        )
        if not sn:
            return None

        data_map = item.get("dataItemMap") or {}
        if not isinstance(data_map, dict):
            data_map = {}

        # Power: Huawei usually returns kW; convert to W defensively
        power_raw = safe_float(
            pick(data_map, ["active_power", "activePower", "pac", "power"])
        )
        if power_raw is None:
            power_w = None
        else:
            # Heuristic: values <= 1000 are likely kW (a 1MW string inverter
            # is the upper bound), > 1000 already in W
            power_w = power_raw * 1000.0 if abs(power_raw) <= 1000 else power_raw

        etoday = safe_float(
            pick(data_map, ["day_cap", "daily_cap", "eToday", "todayEnergy"])
        )

        status_raw = pick(item, ["devStatus", "status", "workStatus"])
        ts_raw = pick(item, ["collectTime", "updateTime", "time"])
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
