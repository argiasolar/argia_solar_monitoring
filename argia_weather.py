# argia_weather.py
from __future__ import annotations

import os
import time
import math
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import requests


# ============================
# ENV / Debug helpers
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
    Returns average hourly cloud cover (%) between 07:00 and 19:00 local time.
    Output range: 0–100
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

    # 1️⃣ Archive API (historical)
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloudcover",
        "timezone": "auto",
        "start_date": date_iso,
        "end_date": date_iso,
    }

    for attempt in range(3):
        try:
            r = requests.get(_OPEN_METEO_ARCHIVE, params=params, timeout=25)
            if r.status_code == 200:
                v = compute(r.json())
                if v is not None:
                    _dbg(f"☁️ [OpenMeteo:archive] {date_iso} cloud_pct={v}")
                    return v
        except Exception as e:
            _dbg(f"☁️ [OpenMeteo:archive] error: {e}")
            time.sleep(2)

    # 2️⃣ Forecast fallback
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
                _dbg(f"☁️ [OpenMeteo:forecast] {date_iso} cloud_pct={v2}")
                return v2
    except Exception as e2:
        _dbg(f"☁️ [OpenMeteo:forecast] error: {e2}")

    return 0.0


# ============================
# Growatt Web (Irradiance)
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
            data={"plantId": plant_id, "currPage": curr_page, "alias": ""},
            timeout=40,
        )
        r.raise_for_status()
        return r.json()

    def get_env_history(
        self,
        plant_id: str,
        datalog_sn: str,
        addr: int,
        day_iso: str,
        start: int,
    ) -> Dict[str, Any]:
        self._seed_plant(plant_id)
        r = self.s.post(
            f"{self.base}/device/getEnvHistory",
            data={
                "datalogSn": datalog_sn,
                "addr": addr,
                "startDate": day_iso,
                "endDate": day_iso,
                "start": start,
            },
            timeout=45,
        )
        r.raise_for_status()
        return r.json()


# ============================
# Public API used by argia.py
# ============================

def get_weather_for_date(
    p_key: str,
    date_iso: str,
    plants_config: dict,
) -> Tuple[float, float]:
    """
    Returns:
      irradiance_kWh_m2
      cloud_coverage (0–1)
    """
    conf = plants_config.get(p_key, {})
    lat = conf.get("lat")
    lon = conf.get("lon")

    if not lat or not lon:
        return 0.0, 0.0

    # 🌞 Irradiance (unchanged logic)
    brand = str(conf.get("brand", "")).upper()
    site_id = str(conf.get("site_id", ""))
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

    # ☁️ Cloud cover FIX → fraction
    cloud_pct = _avg_cloudcover_7_19_from_open_meteo(float(lat), float(lon), date_iso)
    cloud_fraction = round(cloud_pct / 100.0, 4)

    _dbg(
        f"📌 [ARGIA_WEATHER] {p_key} "
        f"irr={irr} cloud_pct={cloud_pct} cloud_frac={cloud_fraction}"
    )

    return irr, cloud_fraction
