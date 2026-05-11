"""
Growatt env-station irradiance integration.

Growatt env stations report instantaneous irradiance in W/m² at irregular
intervals. To compute daily irradiation in kWh/m² we trapezoidal-integrate
the readings over the day:

    kWh/m² = Σ ((r0 + r1) / 2 * Δt[h]) / 1000

where r is W/m² and Δt is seconds between readings, capped to avoid
extrapolating across unreasonable gaps (e.g. station offline overnight).

This module is structured in two layers:
  - ``integrate_radiance_to_kwh_m2()`` is pure: takes a list of
    (timestamp, W/m²) tuples, returns kWh/m². Fully unit-testable.
  - ``GrowattIrradianceClient`` is the HTTP-aware wrapper that talks to
    server.growatt.com/device/getEnvHistory.

v1 had this logic inline in argia_weather.py with global mutable state
(``_GROWATT_CLIENT``, ``_GROWATT_ENV_DEVICE_CACHE``, ``_GROWATT_IRR_CACHE``).
v2 makes the cache an instance attribute so tests can construct a fresh
client per test.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

from argia.core.normalize import normalize_text, pick, safe_float
from argia.core.time_utils import parse_growatt_calendar

LOG = logging.getLogger("argia.meteo.growatt_irradiance")

WEB_BASE = "https://server.growatt.com"
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_GAP_SEC = 7200  # 2 hours — cap gaps to avoid wild extrapolation
DEFAULT_MAX_PAGES = 6
DEFAULT_PAGE_SLEEP_SEC = 0.10


# ============================================================
# Pure: trapezoidal integration
# ============================================================


def integrate_radiance_to_kwh_m2(
    points: List[Tuple[dt.datetime, float]],
    max_gap_sec: int = DEFAULT_MAX_GAP_SEC,
) -> float:
    """
    Trapezoidal integration of W/m² readings into kWh/m².

    points: list of (timestamp, W/m²) tuples. Order doesn't matter — they
            will be sorted internally. Negative readings are clamped to 0.

    Returns kWh/m² over the time span of the points. If fewer than 2 points,
    returns 0.0.

    Gaps longer than ``max_gap_sec`` are capped (treated as ``max_gap_sec``)
    to prevent a sensor outage from inflating the integral.

    Examples:
        >>> from datetime import datetime, timezone
        >>> # 1000 W/m² constant for 1 hour = 1.0 kWh/m²
        >>> t0 = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        >>> t1 = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
        >>> integrate_radiance_to_kwh_m2([(t0, 1000.0), (t1, 1000.0)])
        1.0
    """
    if len(points) < 2:
        return 0.0

    sorted_points = sorted(points, key=lambda p: p[0])

    wh_per_m2 = 0.0
    for i in range(1, len(sorted_points)):
        t0, r0 = sorted_points[i - 1]
        t1, r1 = sorted_points[i]
        delta_sec = (t1 - t0).total_seconds()
        if delta_sec <= 0:
            continue
        if delta_sec > max_gap_sec:
            delta_sec = max_gap_sec
        # Clamp negatives
        avg_w_m2 = 0.5 * max(r0, 0.0) + 0.5 * max(r1, 0.0)
        wh_per_m2 += avg_w_m2 * (delta_sec / 3600.0)

    return round(wh_per_m2 / 1000.0, 4)


def extract_radiance_points(
    rows: List[Dict[str, Any]]
) -> List[Tuple[dt.datetime, float]]:
    """
    Pure function. Convert raw Growatt env-history rows into
    (timestamp, W/m²) tuples, dropping rows with unparseable timestamps
    or missing radiance.

    Each row is expected to have ``calendar`` (Java-style 0-based month)
    and ``radiant`` (W/m²) fields.
    """
    out: List[Tuple[dt.datetime, float]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cal = row.get("calendar")
        if not isinstance(cal, dict):
            continue
        ts = parse_growatt_calendar(cal)
        if ts is None:
            continue
        rad = safe_float(row.get("radiant"))
        if rad is None:
            continue
        out.append((ts, max(rad, 0.0)))
    return out


def find_latest_radiance_wm2(
    rows: List[Dict[str, Any]]
) -> Optional[float]:
    """
    Pure function. Return the most recent radiance reading (W/m²) in the
    given rows, or None if no valid reading is found.

    Used for the 10-min snapshot path where we want a current value rather
    than an integrated daily total.
    """
    points = extract_radiance_points(rows)
    if not points:
        return None
    return max(points, key=lambda p: p[0])[1]


def interval_kwh_m2_from_wm2(radiance_wm2: float, interval_min: int) -> float:
    """
    Convert an instantaneous W/m² reading into the kWh/m² produced over
    ``interval_min`` minutes (assuming the reading is constant over the
    interval — only sensible for short intervals like 10 minutes).

    Examples:
        >>> interval_kwh_m2_from_wm2(1000.0, 60)
        1.0
        >>> interval_kwh_m2_from_wm2(500.0, 10)
        0.083333
        >>> interval_kwh_m2_from_wm2(-50.0, 10)
        0.0
    """
    if radiance_wm2 <= 0 or interval_min <= 0:
        return 0.0
    return round(radiance_wm2 * interval_min / 60_000.0, 6)


# ============================================================
# Stateful HTTP client
# ============================================================


@dataclass
class GrowattWebSession:
    """Minimal stateful credentials container — session lives on the client."""

    username: str
    password: str
    base_url: str = WEB_BASE
    timeout_sec: int = DEFAULT_TIMEOUT_SEC


class GrowattIrradianceClient:
    """
    Talks to server.growatt.com env-station endpoints.

    Per-instance caches avoid the global-state pitfalls of v1:
      - ``_env_device_cache``: plant_id → (datalog_sn, addr) of chosen env device
      - ``_daily_irradiance_cache``: (plant_id, date_iso) → kWh/m²
    """

    def __init__(
        self,
        session_creds: GrowattWebSession,
        http_session: Optional[requests.Session] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        page_sleep_sec: float = DEFAULT_PAGE_SLEEP_SEC,
        max_gap_sec: int = DEFAULT_MAX_GAP_SEC,
    ) -> None:
        self._creds = session_creds
        self._http = http_session or requests.Session()
        self._http.headers.update({"User-Agent": "Mozilla/5.0 (Argia_Mont/2.0)"})
        self._max_pages = max_pages
        self._page_sleep = page_sleep_sec
        self._max_gap_sec = max_gap_sec

        self._logged_in = False
        self._env_device_cache: Dict[str, Tuple[str, int]] = {}
        self._daily_irradiance_cache: Dict[Tuple[str, str], float] = {}

    # ----- low-level transport (mocked in tests) -----

    def _login(self) -> None:
        if self._logged_in:
            return
        # Prime cookies
        self._http.get(
            f"{self._creds.base_url}/login", timeout=self._creds.timeout_sec
        )
        resp = self._http.post(
            f"{self._creds.base_url}/login",
            data={"account": self._creds.username, "password": self._creds.password},
            timeout=self._creds.timeout_sec,
        )
        cookies = self._http.cookies.get_dict()
        if "assToken" not in cookies:
            raise RuntimeError(
                f"Growatt env-client login failed (HTTP {resp.status_code}, no assToken)"
            )
        self._logged_in = True

    def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Single POST. Tests mock THIS method."""
        self._login()
        resp = self._http.post(
            f"{self._creds.base_url}{path}",
            data=data,
            timeout=self._creds.timeout_sec,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Growatt {path} HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise RuntimeError(f"Growatt {path} returned non-JSON: {e}") from e

    # ----- env device discovery -----

    def get_env_device(
        self,
        plant_id: str,
        prefer_sn: Optional[str] = None,
        prefer_addr: Optional[int] = None,
    ) -> Optional[Tuple[str, int]]:
        """
        Pick an env device for the plant. Caches the result.

        If ``prefer_sn`` is given, only return that SN if found. Otherwise
        return the first device in the list.
        """
        if plant_id in self._env_device_cache:
            return self._env_device_cache[plant_id]

        result = self._post(
            "/device/getEnvList",
            {"plantId": str(plant_id), "currPage": "1", "alias": ""},
        )
        devices = self._parse_env_list(result, prefer_sn, prefer_addr)
        if not devices:
            return None

        chosen = devices[0]
        self._env_device_cache[plant_id] = chosen
        return chosen

    @staticmethod
    def _parse_env_list(
        response: Dict[str, Any],
        prefer_sn: Optional[str],
        prefer_addr: Optional[int],
    ) -> List[Tuple[str, int]]:
        """Pure helper — extract (sn, addr) pairs and apply preference."""
        datas = (response or {}).get("datas") or []
        candidates: List[Tuple[str, int]] = []
        for d in datas:
            if not isinstance(d, dict):
                continue
            sn = normalize_text(pick(d, ["datalogSn", "dataLogSn", "sn"]))
            addr_val = safe_float(d.get("addr"))
            if not sn or addr_val is None:
                continue
            candidates.append((sn, int(addr_val)))

        if prefer_sn:
            for sn, addr in candidates:
                if sn == prefer_sn and (prefer_addr is None or addr == prefer_addr):
                    return [(sn, addr)] + [
                        c for c in candidates if c != (sn, addr)
                    ]
        return candidates

    # ----- env history pagination -----

    def fetch_env_history_rows(
        self,
        plant_id: str,
        datalog_sn: str,
        addr: int,
        date_iso: str,
    ) -> List[Dict[str, Any]]:
        """
        Page through getEnvHistory for the given day, return all rows.
        """
        all_rows: List[Dict[str, Any]] = []
        start = 0
        for _ in range(self._max_pages):
            resp = self._post(
                "/device/getEnvHistory",
                {
                    "datalogSn": datalog_sn,
                    "addr": str(addr),
                    "startDate": date_iso,
                    "endDate": date_iso,
                    "start": str(start),
                },
            )
            obj = resp.get("obj") or {}
            rows = obj.get("datas") or []
            if not isinstance(rows, list) or not rows:
                break
            all_rows.extend(rows)
            if not obj.get("haveNext"):
                break
            start += len(rows)
            if self._page_sleep:
                time.sleep(self._page_sleep)
        return all_rows

    # ----- public API -----

    def fetch_daily_irradiance_kwh_m2(
        self,
        plant_id: str,
        date_iso: str,
        prefer_sn: Optional[str] = None,
        prefer_addr: Optional[int] = None,
    ) -> Optional[float]:
        """
        Returns the day's irradiation in kWh/m² for the plant's env station.
        Cached per (plant_id, date_iso).
        """
        cache_key = (plant_id, date_iso)
        if cache_key in self._daily_irradiance_cache:
            return self._daily_irradiance_cache[cache_key]

        device = self.get_env_device(plant_id, prefer_sn, prefer_addr)
        if device is None:
            self._daily_irradiance_cache[cache_key] = 0.0
            return 0.0

        sn, addr = device
        rows = self.fetch_env_history_rows(plant_id, sn, addr, date_iso)
        if not rows:
            self._daily_irradiance_cache[cache_key] = 0.0
            return 0.0

        points = extract_radiance_points(rows)
        kwh_m2 = integrate_radiance_to_kwh_m2(points, max_gap_sec=self._max_gap_sec)
        self._daily_irradiance_cache[cache_key] = kwh_m2
        return kwh_m2

    def fetch_current_irradiance_wm2(
        self,
        plant_id: str,
        date_iso: str,
        prefer_sn: Optional[str] = None,
        prefer_addr: Optional[int] = None,
    ) -> Optional[float]:
        """
        Returns the latest available W/m² reading on ``date_iso`` for the
        plant's env station. Used by the 10-min snapshot.

        Not cached — always returns fresh value (the 10-min cron expects
        latest data each call).
        """
        device = self.get_env_device(plant_id, prefer_sn, prefer_addr)
        if device is None:
            return None
        sn, addr = device
        rows = self.fetch_env_history_rows(plant_id, sn, addr, date_iso)
        return find_latest_radiance_wm2(rows)
