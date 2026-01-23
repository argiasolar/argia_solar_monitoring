# argia_weather.py
from __future__ import annotations

import requests
from functools import lru_cache
from typing import Tuple

MJ_TO_KWH = 1.0 / 3.6


@lru_cache(maxsize=2048)
def get_daily_irradiance_and_clouds(
    date_iso: str,
    latitude: float,
    longitude: float,
    tz: str = "America/Mexico_City",
) -> Tuple[float, float]:
    """
    Returns:
      irradiance_kwh_m2, cloud_cover_mean_percent

    Source: Open-Meteo Historical Weather API (daily shortwave_radiation_sum + cloud_cover_mean)
    """
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": date_iso,
        "end_date": date_iso,
        "daily": "shortwave_radiation_sum,cloud_cover_mean",
        "timezone": tz,
    }
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    js = r.json()

    daily = js.get("daily", {}) if isinstance(js, dict) else {}
    sw = (daily.get("shortwave_radiation_sum") or [0])[0]  # MJ/m2
    cc = (daily.get("cloud_cover_mean") or [0])[0]         # %
    irr_kwh_m2 = round(float(sw) * MJ_TO_KWH, 3)

    return irr_kwh_m2, float(cc)
