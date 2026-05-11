"""
SolarEdge Monitoring API client.

Cleanest of the four vendors — official REST API, API-key auth, JSON
everywhere. No scraping, no session cookies, no AJAX endpoint discovery.

Endpoints used:
  GET /site/{siteId}/energy
       Query: timeUnit=DAY, startDate, endDate
       Returns: site-level kWh per day (technically Wh, we convert)

  GET /equipment/{siteId}/{sn}/data
       Query: startTime, endTime (format "YYYY-MM-DD HH:MM:SS")
       Returns: per-inverter telemetries (power, lifetime energy, status)

Honest limitations documented in the module:
  - SolarEdge returns Wh, not kWh. Conversion is silent.
  - Timestamps are site-local, no TZ offset. We assume MX_TZ.
  - Per-inverter ETodayis derived from lifetime energy diff, slight
    inaccuracy at start of day.
  - SolarEdge rate-limits API keys (300/day account-level). Heavy callers
    must cache.

Docs: https://knowledge-center.solaredge.com/sites/kc/files/se_monitoring_api.pdf
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

import requests

from argia.core.config import InverterConfig, PlantConfig
from argia.core.normalize import normalize_sn, pick, safe_float
from argia.core.time_utils import MX_TZ, UTC, now_utc, parse_provider_datetime
from argia.vendors.base import InverterSnapshot

LOG = logging.getLogger("argia.vendors.solaredge")

DEFAULT_BASE_URL = "https://monitoringapi.solaredge.com"
DEFAULT_TIMEOUT_SEC = 30


class SolarEdgeAuthError(RuntimeError):
    pass


class SolarEdgeAPIError(RuntimeError):
    pass


# Inverter mode strings we treat as offline.
# Source: SolarEdge Monitoring API documentation, "inverterMode" field.
OFFLINE_INVERTER_MODES = frozenset(
    {"OFF", "FAULT", "STANDBY", "SHUTTING_DOWN", "NIGHT", "SLEEPING"}
)


class SolarEdgeClient:
    """
    SolarEdge Monitoring API client.

    Authentication is per-request via the ``api_key`` query parameter.
    There is no session login; the client is essentially a thin wrapper
    around requests with retry-friendly error classes.
    """

    brand = "SOLAREDGE"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        session: Optional[requests.Session] = None,
        site_timezone: str = "America/Mexico_City",
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._session = session or requests.Session()
        self._site_tz = MX_TZ if site_timezone == "America/Mexico_City" else MX_TZ
        # ^ TODO: when we expand outside MX, plumb timezone through PlantConfig

    # ===== public VendorClient interface =====

    def login(self) -> None:
        """No-op: SolarEdge auth is per-request via api_key query param."""

    def fetch_day_kwh(
        self, plant: PlantConfig, date_iso: str
    ) -> Optional[float]:
        """
        Site-level kWh for the given local date.
        Returns None on missing data, raises on auth/server errors.
        """
        result = self._get_json(
            f"/site/{plant.site_id}/energy",
            {
                "timeUnit": "DAY",
                "startDate": date_iso,
                "endDate": date_iso,
            },
        )
        return self._parse_day_kwh(result, date_iso)

    def fetch_inverter_snapshots(
        self,
        plant: PlantConfig,
        inverters: List[InverterConfig],
    ) -> List[InverterSnapshot]:
        """
        Latest telemetry per inverter. Power in W, status normalized to 1/3,
        ``etoday_kwh`` derived from totalEnergy diff over today.

        One HTTP call per inverter — be mindful of rate limits.
        """
        if not inverters:
            return []

        snapshots: List[InverterSnapshot] = []
        # Today's window in site-local time, then ISO string for the API
        now_site = dt.datetime.now(self._site_tz)
        start_of_day = now_site.replace(hour=0, minute=0, second=0, microsecond=0)

        start_str = start_of_day.strftime("%Y-%m-%d %H:%M:%S")
        end_str = now_site.strftime("%Y-%m-%d %H:%M:%S")

        for inv in inverters:
            try:
                result = self._get_json(
                    f"/equipment/{plant.site_id}/{inv.inverter_sn}/data",
                    {"startTime": start_str, "endTime": end_str},
                )
            except SolarEdgeAPIError as e:
                LOG.warning(
                    "SolarEdge inverter %s/%s failed: %s",
                    plant.site_id, inv.inverter_sn, e,
                )
                continue

            snap = self._parse_inverter_data(result, plant.plant_key, inv.inverter_sn)
            if snap is not None:
                snapshots.append(snap)
        return snapshots

    # ===== HTTP transport (mocked in tests) =====

    def _get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Single low-level GET. Tests mock THIS method to avoid network.
        Raises:
          SolarEdgeAuthError on 401/403 (bad api_key, account suspended)
          SolarEdgeAPIError on other non-200 or invalid JSON
        """
        merged = {**params, "api_key": self._api_key}
        url = f"{self._base}{path}"
        resp = self._session.get(url, params=merged, timeout=self._timeout)
        if resp.status_code in (401, 403):
            raise SolarEdgeAuthError(
                f"SolarEdge {path} returned HTTP {resp.status_code} — "
                f"api_key rejected"
            )
        if resp.status_code == 429:
            raise SolarEdgeAPIError(
                f"SolarEdge {path} rate-limited (HTTP 429). "
                f"Account exceeded daily quota."
            )
        if resp.status_code != 200:
            raise SolarEdgeAPIError(
                f"SolarEdge {path} returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise SolarEdgeAPIError(
                f"SolarEdge {path} returned invalid JSON: {e}"
            ) from e

    # ===== parsers (pure, fully testable) =====

    @staticmethod
    def _parse_day_kwh(
        response: Dict[str, Any], date_iso: str
    ) -> Optional[float]:
        """
        Pure function. Extract day's kWh from /site/.../energy response.
        Converts Wh to kWh (SolarEdge returns Wh).

        Response shape:
          {
            "energy": {
              "timeUnit": "DAY",
              "unit": "Wh",
              "values": [
                {"date": "2026-04-15 00:00:00", "value": 1245500.0}
              ]
            }
          }

        Returns None when:
          - response missing 'energy' key
          - no values match the requested date
          - value is null (sensor outage)
        """
        energy = (response or {}).get("energy") or {}
        unit = str(energy.get("unit", "Wh")).strip().lower()
        values = energy.get("values") or []
        if not isinstance(values, list):
            return None

        # SolarEdge returns one entry per day in DAY mode; filter by date prefix
        for entry in values:
            if not isinstance(entry, dict):
                continue
            entry_date = str(entry.get("date", "")).strip()
            if not entry_date.startswith(date_iso):
                continue
            raw = entry.get("value")
            if raw is None:
                return None
            wh_value = safe_float(raw)
            if wh_value is None:
                return None
            # Normalize to kWh
            if unit in ("kwh",):
                return round(wh_value, 3)
            return round(wh_value / 1000.0, 3)

        return None

    def _parse_inverter_data(
        self,
        response: Dict[str, Any],
        plant_key: str,
        sn: str,
    ) -> Optional[InverterSnapshot]:
        """
        Parse /equipment/.../data response into an InverterSnapshot.

        Response shape:
          {
            "data": {
              "count": N,
              "telemetries": [
                {"date": "...", "totalActivePower": ..., "totalEnergy": ...,
                 "inverterMode": "MPPT", "temperature": ...},
                ...
              ]
            }
          }
        """
        data = (response or {}).get("data") or {}
        telemetries = data.get("telemetries") or []
        if not isinstance(telemetries, list) or not telemetries:
            return None

        # Sort defensively — API usually returns chronological but verify
        def _entry_ts(entry: Dict[str, Any]) -> str:
            return str(entry.get("date", ""))

        sorted_telemetries = sorted(telemetries, key=_entry_ts)
        latest = sorted_telemetries[-1]
        if not isinstance(latest, dict):
            return None

        # Power: SolarEdge gives W directly under totalActivePower
        power_w = safe_float(
            pick(latest, ["totalActivePower", "power"])
        )

        # Status: derive from inverterMode string
        mode_raw = pick(latest, ["inverterMode", "mode"])
        status = self._inverter_mode_to_status(mode_raw)

        # ETodayfrom totalEnergy diff: latest minus first today
        first = sorted_telemetries[0]
        latest_total = safe_float(latest.get("totalEnergy"))
        first_total = (
            safe_float(first.get("totalEnergy")) if isinstance(first, dict) else None
        )
        if latest_total is not None and first_total is not None:
            etoday_wh = max(0.0, latest_total - first_total)
            etoday_kwh: Optional[float] = round(etoday_wh / 1000.0, 3)
        else:
            etoday_kwh = None

        # Timestamp: site-local (naive) → MX_TZ → UTC
        ts_raw = pick(latest, ["date", "time"])
        ts_utc = self._parse_site_local_to_utc(ts_raw)

        return InverterSnapshot(
            plant_key=plant_key,
            inverter_sn=normalize_sn(sn),
            timestamp_utc=ts_utc,
            status=status,
            power_w=power_w,
            etoday_kwh=etoday_kwh,
            raw_status=str(mode_raw) if mode_raw is not None else "",
        )

    @staticmethod
    def _inverter_mode_to_status(mode_raw: Any) -> int:
        """
        SolarEdge ``inverterMode`` string → 1 (online) or 3 (offline).

        MPPT, THROTTLED, IDLE → online (1)
        OFF, FAULT, STANDBY, SHUTTING_DOWN, NIGHT, SLEEPING → offline (3)
        Unknown → online (1) by default; alarms surface elsewhere.
        """
        if mode_raw is None:
            return 1
        mode = str(mode_raw).strip().upper()
        if mode in OFFLINE_INVERTER_MODES:
            return 3
        return 1

    def _parse_site_local_to_utc(self, value: Any) -> dt.datetime:
        """
        Parse a SolarEdge timestamp string ('YYYY-MM-DD HH:MM:SS', site local,
        no TZ) and return a UTC-aware datetime.
        """
        if value is None:
            return now_utc()

        s = str(value).strip()
        if not s:
            return now_utc()

        # Try common formats; treat as site-local
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                naive = dt.datetime.strptime(s, fmt)
                return naive.replace(tzinfo=self._site_tz).astimezone(UTC)
            except ValueError:
                continue

        # Last-resort: parse_provider_datetime treats as UTC
        parsed = parse_provider_datetime(value)
        return parsed if parsed else now_utc()
