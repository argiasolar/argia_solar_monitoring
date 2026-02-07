from __future__ import annotations

import os
import time
import math
import datetime as dt
from typing import Any, Dict, Optional, Tuple, List

import requests


# ============================
# ENV helpers
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
    return str(_env("ARGIA_METEO_DEBUG", "0")).lower() in ("1", "true", "yes", "on")

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
# Open-Meteo cloud cover
# ============================

_OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

def _cloudcover_pct_nearest_hour(lat: float, lon: float, when_utc: dt.datetime) -> float:
    """
    Returns cloud cover (%) near the given time.
    Uses Open-Meteo hourly cloudcover with timezone=auto.
    We pick the closest hour in the returned series for today's window.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloudcover",
        "timezone": "auto",
        "past_days": 1,
        "forecast_days": 2,
    }

    try:
        r = requests.get(_OPEN_METEO_FORECAST, params=params, timeout=25)
        if r.status_code != 200:
            _dbg(f"☁️ [OpenMeteo] http={r.status_code}")
            return 0.0

        js = r.json()
        hourly = js.get("hourly") or {}
        times = hourly.get("time") or []
        clouds = hourly.get("cloudcover") or []
        if not isinstance(times, list) or not isinstance(clouds, list) or len(times) != len(clouds) or not times:
            return 0.0

        # We don't know the plant timezone exactly (Open-Meteo returns local time strings),
        # so we pick the closest by matching date+hour. Good enough for cloudcover.
        target_ymd = when_utc.date().isoformat()
        target_hour = when_utc.hour  # UTC hour, but series is local. Still okay as approx.
        # Better: just pick the latest entry for today if mismatch. We'll try best-effort.

        best_idx = None
        best_score = 10**9
        for i, t_str in enumerate(times):
            if not isinstance(t_str, str) or len(t_str) < 13:
                continue
            ymd = t_str[:10]
            try:
                hr = int(t_str[11:13])
            except Exception:
                continue
            score = abs(hr - target_hour) + (0 if ymd == target_ymd else 24)
            if score < best_score:
                best_score = score
                best_idx = i

        if best_idx is None:
            return 0.0

        return round(_safe_float(clouds[best_idx], 0.0), 1)

    except Exception as e:
        _dbg(f"☁️ [OpenMeteo] error: {e}")
        return 0.0


# ============================
# Growatt Web ENV radiant (W/m²)
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
        r1 = self.s.get(f"{self.base}/login", timeout=30)
        _dbg(f"🔐 [GrowattWeb] GET /login -> {r1.status_code}")

        payload = {"account": self.username, "password": self.password}
        r2 = self.s.post(f"{self.base}/login", data=payload, timeout=30)

        cookies = self.s.cookies.get_dict()
        ok = "assToken" in cookies
        _dbg(f"🔐 [GrowattWeb] POST /login -> {r2.status_code} | assToken={ok} | cookies={','.join(sorted(cookies.keys()))}")
        if not ok:
            raise RuntimeError("Growatt login failed (no assToken cookie).")

    def _seed_plant(self, plant_id: str) -> None:
        # This cookie is important for some endpoints
        self.s.cookies.set("selectedPlantId", str(plant_id))

    def env_page_seed(self, plant_id: str) -> None:
        self._seed_plant(plant_id)
        r = self.s.get(f"{self.base}/device/getEnvPage", timeout=30)
        _dbg(f"🧭 [GrowattWeb] GET /device/getEnvPage plant={plant_id} -> {r.status_code}")

    def get_env_history(self, plant_id: str, datalog_sn: str, addr: int, day_iso: str, start: int) -> Dict[str, Any]:
        self._seed_plant(plant_id)
        url = f"{self.base}/device/getEnvHistory"
        payload = {
            "datalogSn": datalog_sn,
            "addr": str(addr),
            "startDate": day_iso,
            "endDate": day_iso,
            "start": str(start),
        }
        r = self.s.post(url, data=payload, timeout=45)
        r.raise_for_status()
        return r.json()


def _calendar_to_dt(cal: Dict[str, Any]) -> Optional[dt.datetime]:
    """
    Growatt calendar month is 0-based (0..11).
    """
    try:
        y = int(cal["year"])
        m0 = int(cal["month"])
        d = int(cal.get("dayOfMonth") or cal.get("day"))
        hh = int(cal.get("hourOfDay", 0))
        mm = int(cal.get("minute", 0))
        ss = int(cal.get("second", 0))
        return dt.datetime(y, m0 + 1, d, hh, mm, ss)
    except Exception:
        return None


# ----------------------------
# Per-run caches
# ----------------------------
_CLIENT: Optional[GrowattWebClient] = None
_RAD_CACHE: Dict[Tuple[str, str, str, int], float] = {}  # (plant_id, day_iso, sn, addr) -> radiant W/m2
_CLOUD_CACHE: Dict[Tuple[float, float, str], float] = {} # (lat, lon, ymdhh) -> cloud pct


def _get_client() -> Optional[GrowattWebClient]:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    base = _env("GROWATT_WEB_BASE", "https://server.growatt.com")
    user = _env("GROWATT_USERNAME")
    pwd = _env("GROWATT_PASSWORD")
    if not user or not pwd:
        _dbg("❌ Missing GROWATT_USERNAME/GROWATT_PASSWORD")
        return None

    cli = GrowattWebClient(base, user, pwd)
    cli.login()
    _CLIENT = cli
    return cli


def get_radiant_w_m2_snapshot(plant_id: str, datalog_sn: str, addr: int, when_utc: dt.datetime) -> float:
    """
    Pulls ENV history for given plant/day and returns a 'recent' radiant (W/m²).
    We keep requests moderate by paging only a bit and selecting the latest timestamp.
    """
    cli = _get_client()
    if cli is None:
        return 0.0

    day_iso = when_utc.date().isoformat()
    cache_key = (str(plant_id), day_iso, str(datalog_sn), int(addr))
    if cache_key in _RAD_CACHE:
        return _RAD_CACHE[cache_key]

    try:
        cli.env_page_seed(str(plant_id))

        all_rows: List[Dict[str, Any]] = []
        start = 0
        pages = 0
        max_pages = _env_int("ARGIA_METEO_MAX_PAGES", 4)  # keep it light

        while pages < max_pages:
            js = cli.get_env_history(str(plant_id), str(datalog_sn), int(addr), day_iso, start)
            obj = js.get("obj") or {}
            rows = obj.get("datas") or []
            have_next = bool(obj.get("haveNext"))

            if not isinstance(rows, list) or not rows:
                break

            all_rows.extend([r for r in rows if isinstance(r, dict)])
            pages += 1

            if not have_next:
                break

            start += len(rows)
            time.sleep(0.08)

        # Choose latest radiant
        best_ts = None
        best_rad = 0.0
        for r in all_rows:
            cal = r.get("calendar") or {}
            ts = _calendar_to_dt(cal) if isinstance(cal, dict) else None
            if not ts:
                continue
            rad = _safe_float(r.get("radiant"), 0.0)
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_rad = max(rad, 0.0)

        _RAD_CACHE[cache_key] = best_rad
        _dbg(f"🌞 [Meteo] plant={plant_id} sn={datalog_sn} addr={addr} day={day_iso} radiant={best_rad} W/m²")
        return best_rad

    except Exception as e:
        _dbg(f"🌞 [Meteo] error plant={plant_id}: {e}")
        return 0.0


def get_meteo_snapshot(
    plant_id_for_weather: str,
    lat: float,
    lon: float,
    weather_sn: str,
    addr: int,
    when_utc: dt.datetime,
    interval_minutes: int = 10,
) -> Tuple[float, float]:
    """
    Returns:
      (irradiance_kwh_m2_interval, cloud_cover_pct)

    irradiance_kwh_m2_interval is computed from latest radiant W/m²:
      radiant(W/m²) * (interval_min/60) / 1000
    """
    radiant = 0.0
    if str(plant_id_for_weather).isdigit():
        radiant = get_radiant_w_m2_snapshot(str(plant_id_for_weather), weather_sn, addr, when_utc)

    irr = round((radiant * (interval_minutes / 60.0)) / 1000.0, 6)

    ymdhh = when_utc.strftime("%Y%m%d%H")
    ckey = (float(lat), float(lon), ymdhh)
    if ckey in _CLOUD_CACHE:
        clouds_pct = _CLOUD_CACHE[ckey]
    else:
        clouds_pct = _cloudcover_pct_nearest_hour(float(lat), float(lon), when_utc)
        _CLOUD_CACHE[ckey] = clouds_pct

    return irr, clouds_pct
