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
    Average hourly cloud cover (%) between 07:00 and 19:00 (inclusive)
    RETURNS: 0–100  (UNCHANGED)
    """
    start_hour = _env_int("CLOUDS_START_HOUR", 7)
    end_hour = _env_int("CLOUDS_END_HOUR", 19)

    def compute_from_json(js: Dict[str, Any]) -> Optional[float]:
        hourly = js.get("hourly") or {}
        times = hourly.get("time") or []
        clouds = hourly.get("cloudcover") or []

        if not isinstance(times, list) or not isinstance(clouds, list):
            return None
        if len(times) != len(clouds):
            return None

        vals: List[float] = []
        for t_str, c in zip(times, clouds):
            if not isinstance(t_str, str):
                continue
            if not t_str.startswith(date_iso):
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
            v2 = compute_from_json(r2.json())
            if v2 is not None:
                return v2
    except Exception:
        pass

    return 0.0


# ============================
# Growatt Web UI (ENV / IRRADIANCE)
# ============================

class GrowattWebClient:
    def __init__(self, base: str, username: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Connection": "keep-alive",
        })

    def login(self) -> None:
        self.s.get(f"{self.base}/login", timeout=30)
        r = self.s.post(
            f"{self.base}/login",
            data={"account": self.username, "password": self.password},
            timeout=30,
        )
        if "assToken" not in self.s.cookies.get_dict():
            raise RuntimeError("Growatt login failed (no assToken cookie).")

    def _seed_plant(self, plant_id: str) -> None:
        self.s.cookies.set("selectedPlantId", str(plant_id))

    def env_page_seed(self, plant_id: str) -> None:
        self._seed_plant(plant_id)
        self.s.get(f"{self.base}/device/getEnvPage", timeout=30)

    def get_env_list(self, plant_id: str, curr_page: int = 1) -> Dict[str, Any]:
        self._seed_plant(plant_id)
        r = self.s.post(
            f"{self.base}/device/getEnvList",
            data={"plantId": str(plant_id), "currPage": str(curr_page), "alias": ""},
            timeout=40,
        )
        r.raise_for_status()
        return r.json()

    def get_env_history(self, plant_id: str, datalog_sn: str, addr: int, day_iso: str, start: int) -> Dict[str, Any]:
        self._seed_plant(plant_id)
        r = self.s.post(
            f"{self.base}/device/getEnvHistory",
            data={
                "datalogSn": datalog_sn,
                "addr": str(addr),
                "startDate": day_iso,
                "endDate": day_iso,
                "start": str(start),
            },
            timeout=45,
        )
        r.raise_for_status()
        return r.json()


def _calendar_to_dt(cal: Dict[str, Any]) -> Optional[dt.datetime]:
    try:
        return dt.datetime(
            int(cal["year"]),
            int(cal["month"]) + 1,
            int(cal["dayOfMonth"]),
            int(cal.get("hourOfDay", 0)),
            int(cal.get("minute", 0)),
            int(cal.get("second", 0)),
        )
    except Exception:
        return None


def _integrate_radiant_kwh_m2(rows: List[Dict[str, Any]]) -> float:
    pts: List[Tuple[dt.datetime, float]] = []
    for r in rows:
        ts = _calendar_to_dt(r.get("calendar", {}))
        if not ts:
            continue
        rad = _safe_float(r.get("radiant"), 0.0)
        pts.append((ts, max(rad, 0.0)))

    if len(pts) < 2:
        return 0.0

    pts.sort(key=lambda x: x[0])
    wh_m2 = 0.0

    for i in range(1, len(pts)):
        t0, r0 = pts[i - 1]
        t1, r1 = pts[i]
        dt_sec = (t1 - t0).total_seconds()
        if dt_sec <= 0:
            continue
        wh_m2 += ((r0 + r1) / 2) * (dt_sec / 3600)

    return round(wh_m2 / 1000, 3)


# ============================
# PUBLIC API (USED BY argia.py)
# ============================

def get_weather_for_date(p_key: str, date_iso: str, plants_config: dict) -> Tuple[float, float]:
    conf = plants_config.get(p_key, {})
    lat = conf.get("lat")
    lon = conf.get("lon")

    if not lat or not lon:
        return 0.0, 0.0

    # ----- IRRADIANCE (UNCHANGED) -----
    irr = 0.0  # ← exactly as before in your file

    # ----- CLOUD COVER (ONLY FIX) -----
    clouds_pct = _avg_cloudcover_7_19_from_open_meteo(float(lat), float(lon), date_iso)
    clouds = round(clouds_pct / 100.0, 4)  # ← THE ONLY CHANGE

    return irr, clouds
