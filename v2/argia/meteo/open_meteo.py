"""
Open-Meteo cloud cover client.

Free, no-auth weather API. We use it to get average cloud cover during
daylight hours (07-19 local) for each plant location.

v1 had this logic inline in argia_weather.py with module-level globals
and string-formatted error logging mixed with the parsing. v2 splits:
  - ``compute_avg_cloudcover()`` is pure: takes JSON, returns float.
  - ``CloudCoverClient`` is the HTTP-aware wrapper.

Tests can drive ``compute_avg_cloudcover()`` from fixtures with no network.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from argia.core.normalize import safe_float

LOG = logging.getLogger("argia.meteo.open_meteo")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_DAYLIGHT_START_HOUR = 7
DEFAULT_DAYLIGHT_END_HOUR = 19  # inclusive
DEFAULT_TIMEOUT_SEC = 25
DEFAULT_RETRIES = 2


def compute_avg_cloudcover(
    response_json: Dict[str, Any],
    date_iso: str,
    start_hour: int = DEFAULT_DAYLIGHT_START_HOUR,
    end_hour: int = DEFAULT_DAYLIGHT_END_HOUR,
) -> Optional[float]:
    """
    Pure function. Average hourly cloud cover (%) between ``start_hour`` and
    ``end_hour`` (inclusive) on ``date_iso``.

    Returns None if no hourly data falls in the window.

    Open-Meteo response shape:
      {
        "hourly": {
          "time": ["2026-04-15T00:00", "2026-04-15T01:00", ...],
          "cloudcover": [12, 15, 18, ...]
        }
      }
    """
    hourly = (response_json or {}).get("hourly") or {}
    times = hourly.get("time") or []
    clouds = hourly.get("cloudcover") or []

    if not isinstance(times, list) or not isinstance(clouds, list):
        return None
    if len(times) != len(clouds) or len(times) == 0:
        return None

    selected: List[float] = []
    for t_str, value in zip(times, clouds):
        if not isinstance(t_str, str) or not t_str.startswith(date_iso):
            continue
        # Time format is "YYYY-MM-DDTHH:MM"
        try:
            hour = int(t_str[11:13])
        except (ValueError, IndexError):
            continue
        if start_hour <= hour <= end_hour:
            v = safe_float(value)
            if v is not None:
                selected.append(v)

    if not selected:
        return None

    return round(sum(selected) / len(selected), 1)


class CloudCoverClient:
    """
    Open-Meteo HTTP client. Tries archive API first (better historical data),
    falls back to forecast API (covers recent days well).
    """

    def __init__(
        self,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        retries: int = DEFAULT_RETRIES,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._timeout = timeout_sec
        self._retries = max(0, retries)
        self._session = session or requests.Session()

    def fetch_avg_cloudcover_pct(
        self,
        lat: float,
        lon: float,
        date_iso: str,
        start_hour: int = DEFAULT_DAYLIGHT_START_HOUR,
        end_hour: int = DEFAULT_DAYLIGHT_END_HOUR,
    ) -> Optional[float]:
        """
        Returns average daylight-hours cloud cover as a percentage (0-100).
        Returns None on any error or missing data.
        """
        # Archive API
        archive_params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "cloudcover",
            "timezone": "auto",
            "start_date": date_iso,
            "end_date": date_iso,
        }
        result = self._try_request(ARCHIVE_URL, archive_params)
        if result is not None:
            value = compute_avg_cloudcover(result, date_iso, start_hour, end_hour)
            if value is not None:
                return value

        # Forecast API fallback (covers recent days + a window of past)
        forecast_params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "cloudcover",
            "timezone": "auto",
            "past_days": 10,
            "forecast_days": 2,
        }
        result = self._try_request(FORECAST_URL, forecast_params)
        if result is not None:
            return compute_avg_cloudcover(result, date_iso, start_hour, end_hour)

        return None

    def _try_request(self, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """One request with bounded retries. Tests mock this method."""
        attempts = 0
        while attempts <= self._retries:
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout)
                if resp.status_code == 200:
                    return resp.json()
                LOG.debug("Open-Meteo %s returned HTTP %s", url, resp.status_code)
            except (requests.RequestException, ValueError) as e:
                LOG.debug("Open-Meteo %s error: %s", url, e)
            attempts += 1
        return None
