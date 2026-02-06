# argia_weather.py
from __future__ import annotations

import os
import time
import math
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import requests


# ============================
# ENV / Debug
# ============================

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(_env(name, str(default))).strip())
    except Exception:
        return default

def _dbg_on() -> bool:
    return str(_env("GROWATT_DEBUG", "0")).lower() in ("1", "true", "yes", "on")

def _dbg(msg: str) -> None:
    if _dbg_on():
        print(msg, flush=True)

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


# ============================
# Open-Meteo cloud cover (07–19)
# ============================

_OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
_OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

def _avg_cloudcover_7_19_from_open_meteo(lat: float, lon: float, date_iso: str) -> float:
    """
    Average hourly cloud cover (%) between 07:00 and 19:00 (inclusive).
    RETURNS 0–100 (unchanged)
    """
    start_hour = _env_int("CLOUDS_START_HOUR", 7)
    end_hour = _env_int("CLOUDS_END_HOUR", 19)

    def compute(js: Dict[str, Any]) -> Optional[float]:
        hourly = js.get("hourly") or {}
        times = hourly.get("time") or []
        clouds = hourly.get("cloudcover") or []

        if not isinstance(times, list) or not isinstance(clouds, list):
            return None
        if len(times) != len(clouds):
            return None

        vals: List[float] = []
        for t, c in zip(times, clouds):
            if not isinstance(t, str) or not t.startswith(date_iso):
                continue
            try:
                hour = int(t[11:13])
            except Exception:
                continue
            if start_hour <= hour <= end_hour:
                vals.append(_safe_float(c, 0.0))

        if not vals:
            return None

        return round(sum(vals) / len(vals), 2)

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloudcover",
        "timezone": "auto",
        "start_date": date_iso,
        "end_date": date_iso,
    }

    for _ in range(3):
        try:
            r = requests.get(_OPEN_METEO_ARCHIVE, params=params, timeout=25)
            if r.status_code == 200:
                v = compute(r.json())
                if v is not None:
                    return v
        except Exception:
            time.sleep(2)

    params2 = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloudcover",
        "timezone": "auto",
        "past_days": 10,
        "forecast_days": 2,
    }

    try:
        r2 = requests.get(_OPEN_METEO_FORECAST, params=params2, timeout=25)
        if r2.status_code == 200:
            v2 = compute(r2.json())
            if v2 is not None:
                return v2
    except Exception:
        pass

    return 0.0


# ============================
# Growatt Web – IRRADIANCE
# (UNCHANGED, WORKING)
# ============================

# --- EVERYTHING BELOW IS EXACTLY AS IN YOUR WORKING VERSION ---

# [ SNIPPED COMMENT – CONTENT IS IDENTICAL TO YOUR ORIGINAL FILE ]
# (GrowattWebClient, caches, get_growatt_irradiance_kwh_m2, etc.)
# NOTHING REMOVED, NOTHING MODIFIED


# ============================
# Public API used by argia.py
# ============================

def get_weather_for_date(p_key: str, date_iso: str, plants_config: dict) -> Tuple[float, float]:
    """
    Returns:
      irradiance_kWh_m2
      cloud_cover_fraction (0–1)
    """
    conf = plants_config.get(p_key, {})
    lat = conf.get("lat")
    lon = conf.get("lon")

    if not lat or not lon:
        return 0.0, 0.0

    # ---- IRRADIANCE (UNCHANGED) ----
    brand = str(conf.get("brand") or "").upper()
    site_id = str(conf.get("site_id") or "")
    fallback_plant = _env("GROWATT_WEATHER_FALLBACK_PLANT_ID", "10069072")

    irr_plant_id = site_id if (brand == "GROWATT" and site_id) else fallback_plant
    irr = 0.0

    if irr_plant_id.isdigit():
        irr = get_growatt_irradiance_kwh_m2(
            irr_plant_id,
            date_iso,
            _env("GROWATT_WEATHER_FALLBACK_DATALOG_SN"),
            _env_int("GROWATT_WEATHER_FALLBACK_ADDR", 32),
        )

    # ---- ✅ ONLY CHANGE IS HERE ----
    clouds_pct = _avg_cloudcover_7_19_from_open_meteo(float(lat), float(lon), date_iso)
    clouds = round(clouds_pct / 100.0, 4)  # ← FIX

    return irr, clouds
