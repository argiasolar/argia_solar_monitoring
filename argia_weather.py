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
    Returns average hourly cloud cover (%) between 07:00 and 19:00 (inclusive).
    Output range: 0–100
    """
    start_hour = _env_int("CLOUDS_START_HOUR", 7)
    end_hour = _env_int("CLOUDS_END_HOUR", 19)

    def compute_from_json(js: Dict[str, Any]) -> Optional[float]:
        hourly = js.get("hourly") or {}
        times = hourly.get("time") or []
        clouds = hourly.get("cloudcover") or []

        if not isinstance(times, list) or not isinstance(clouds, list) or len(times) != len(clouds):
            return None

        vals: List[float] = []
        for t_str, c in zip(times, clouds):
            if not isinstance(t_str, str) or not t_str.startswith(date_iso):
                continue
            try:
                hour = int(t_str[11:13])
            except Exception:
                continue
            if start_hour <= hour <= end_hour:
                vals.append(_safe_float(c, 0.0))

        if not vals:
            return None

        return round(sum(vals) / len(vals), 2)

    # 1️⃣ Archive API (best for historical)
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
                v = compute_from_json(r.json())
                if v is not None:
                    _dbg(f"☁️ [OpenMeteo:archive] {date_iso} cloud_pct={v}")
                    return v
        except Exception as e:
            _dbg(f"☁️ [OpenMeteo:archive] error_
