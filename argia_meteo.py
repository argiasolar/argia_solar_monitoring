from __future__ import annotations

import os
import time
import math
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import requests


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(_env(name, str(default))).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(_env(name, str(default))).strip())
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


# Reuse session
_SESS = requests.Session()
_SESS.headers.update({"User-Agent": "ARGIA Meteo Bot", "Accept": "application/json"})


# ----------------------------
# Cloud cover: Open-Meteo (primary) -> 0..1
# ----------------------------
_OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"


def _open_meteo_hourly_cloud_series(lat: float, lon: float) -> Optional[Tuple[List[str], List[float], int]]:
    """
    Returns (times_local_iso, cloud_pct, utc_offset_seconds)
    """
    timeout = _env_int("OPEN_METEO_TIMEOUT", 10)
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloudcover",
        "timezone": "auto",
        "past_days": 2,
        "forecast_days": 2,
    }
    r = _SESS.get(_OPEN_METEO_FORECAST, params=params, timeout=timeout)
    if r.status_code != 200:
        _dbg(f"☁️ [OpenMeteo] http={r.status_code}")
        return None
    js = r.json()
    hourly = js.get("hourly") or {}
    times = hourly.get("time") or []
    clouds = hourly.get("cloudcover") or []
    if not isinstance(times, list) or not isinstance(clouds, list) or len(times) != len(clouds) or not times:
        return None
    offset_sec = int(js.get("utc_offset_seconds") or 0)
    cloud_pct = [_safe_float(c, 0.0) for c in clouds]
    return times, cloud_pct, offset_sec


def _open_meteo_cloud_fraction_nearest(lat: float, lon: float, when_utc: dt.datetime) -> Optional[float]:
    retries = _env_int("OPEN_METEO_RETRIES", 3)
    backoff = _env_float("OPEN_METEO_BACKOFF_SEC", 1.2)

    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            series = _open_meteo_hourly_cloud_series(lat, lon)
            if not series:
                time.sleep(backoff * attempt)
                continue

            times, cloud_pct, offset_sec = series

            # Convert target UTC -> local naive using Open-Meteo offset
            target_local = (when_utc + dt.timedelta(seconds=offset_sec)).replace(tzinfo=None)

            best_i = 0
            best_abs = None
            best_dt = None

            for i, tstr in enumerate(times):
                if not isinstance(tstr, str):
                    continue
                try:
                    dti_local = dt.datetime.fromisoformat(tstr)
                except Exception:
                    continue
                diff = abs((dti_local - target_local).total_seconds())
                if best_abs is None or diff < best_abs:
                    best_abs = diff
                    best_i = i
                    best_dt = dti_local

            cc_pct = float(cloud_pct[best_i] if best_i < len(cloud_pct) else 0.0)
            cc_frac = max(0.0, min(1.0, cc_pct / 100.0))

            _dbg(f"☁️ [OpenMeteo] cloud={cc_pct:.1f}% (frac={cc_frac:.3f}) near={best_dt} offset_sec={offset_sec}")

            # Optional debug window: print surrounding hours
            w = _env_int("METEO_CLOUD_DEBUG_WINDOW_HOURS", 0)
            if _dbg_on() and w > 0 and best_dt is not None:
                print(f"☁️ [OpenMeteo] debug window ±{w}h around {best_dt}", flush=True)
                for j in range(max(0, best_i - w), min(len(times), best_i + w + 1)):
                    try:
                        d = dt.datetime.fromisoformat(times[j])
                    except Exception:
                        continue
                    print(f"   - {d} : {cloud_pct[j]:.1f}%", flush=True)

            return float(round(cc_frac, 4))

        except Exception as e:
            last_err = e
            _dbg(f"☁️ [OpenMeteo] attempt={attempt} error: {e}")
            time.sleep(backoff * attempt)

    _dbg(f"☁️ [OpenMeteo] error: {last_err}")
    return None


# ----------------------------
# Cloud cover: OpenWeather fallback -> 0..1
# ----------------------------
def _openweather_cloud_fraction_nearest(lat: float, lon: float, when_utc: dt.datetime) -> Optional[float]:
    key = _env("OPENWEATHER_API_KEY")
    if not key:
        return None

    timeout = _env_int("OPENWEATHER_TIMEOUT", 10)
    url = "https://api.openweathermap.org/data/2.5/onecall"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": key,
        "exclude": "minutely,daily,alerts",
        "units": "metric",
    }

    try:
        r = _SESS.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            _dbg(f"☁️ [OpenWeather] http={r.status_code}")
            return None
        js = r.json()
        hourly = js.get("hourly") or []
        if not isinstance(hourly, list) or not hourly:
            return None

        target_ts = int(when_utc.timestamp())
        best_cc = None
        best_abs = None
        for h in hourly:
            if not isinstance(h, dict):
                continue
            ts = h.get("dt")
            cc = h.get("clouds")
            if ts is None or cc is None:
                continue
            diff = abs(int(ts) - target_ts)
            if best_abs is None or diff < best_abs:
                best_abs = diff
                best_cc = float(cc)

        if best_cc is None:
            return None

        cc_frac = max(0.0, min(1.0, best_cc / 100.0))
        _dbg(f"☁️ [OpenWeather] cloud={best_cc:.1f}% (frac={cc_frac:.3f})")
        return float(round(cc_frac, 4))

    except Exception as e:
        _dbg(f"☁️ [OpenWeather] error: {e}")
        return None


def get_cloud_fraction(lat: float, lon: float, when_utc: dt.datetime) -> float:
    """
    Returns cloud cover fraction 0..1.
    Strategy:
      - OpenMeteo primary
      - OpenWeather if available
      - If both present and differ a lot, use average and log it (prevents false 1.0 spikes)
    """
    om = _open_meteo_cloud_fraction_nearest(lat, lon, when_utc)
    ow = _openweather_cloud_fraction_nearest(lat, lon, when_utc)

    if om is None and ow is None:
        return 0.0
    if om is None:
        return float(ow or 0.0)
    if ow is None:
        return float(om)

    if abs(om - ow) >= 0.45:
        print(f"☁️ [CloudBlend] OpenMeteo={om:.3f} OpenWeather={ow:.3f} -> avg", flush=True)
        return float(round((om + ow) / 2.0, 4))

    return float(om)


# ----------------------------
# Growatt ENV radiant -> interval irradiance (kWh/m² for interval)
# ----------------------------
class GrowattWebClient:
    def __init__(self, base: str, username: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144 Safari/144"
                ),
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Connection": "keep-alive",
            }
        )

    def login(self) -> None:
        r1 = self.s.get(f"{self.base}/login", timeout=30)
        _dbg(f"🔐 [GrowattWeb] GET /login -> {r1.status_code}")
        payload = {"account": self.username, "password": self.password}
        r2 = self.s.post(f"{self.base}/login", data=payload, timeout=30)
        ok = "assToken" in self.s.cookies.get_dict()
        _dbg(f"🔐 [GrowattWeb] POST /login -> {r2.status_code} | assToken={ok}")
        if not ok:
            raise RuntimeError("Growatt login failed (no assToken cookie).")

    def _seed_plant(self, plant_id: str) -> None:
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
    try:
        y = int(cal["year"])
        m0 = int(cal["month"])  # 0-based
        d = int(cal.get("dayOfMonth") or cal.get("day"))
        hh = int(cal.get("hourOfDay", 0))
        mm = int(cal.get("minute", 0))
        ss = int(cal.get("second", 0))
        return dt.datetime(y, m0 + 1, d, hh, mm, ss)
    except Exception:
        return None


_GROWATT_CLIENT: Optional[GrowattWebClient] = None


def _get_growatt_client() -> Optional[GrowattWebClient]:
    global _GROWATT_CLIENT
    if _GROWATT_CLIENT is not None:
        return _GROWATT_CLIENT

    base = _env("GROWATT_WEB_BASE", "https://server.growatt.com")
    user = _env("GROWATT_USERNAME")
    pwd = _env("GROWATT_PASSWORD")
    if not user or not pwd:
        return None

    cli = GrowattWebClient(base, user, pwd)
    cli.login()
    _GROWATT_CLIENT = cli
    return cli


def _fetch_recent_radiant_wm2(
    plant_id: str,
    weather_sn: str,
    addr: int,
    when_utc: dt.datetime,
    interval_minutes: int,
) -> float:
    cli = _get_growatt_client()
    if cli is None:
        return 0.0

    cli.env_page_seed(plant_id)
    day_iso = when_utc.date().isoformat()

    all_rows: List[Dict[str, Any]] = []
    start = 0
    pages = 0
    max_pages = _env_int("GROWATT_ENV_MAX_PAGES", 6)
    page_sleep = _env_float("GROWATT_ENV_PAGE_SLEEP", 0.10)

    while pages < max_pages:
        js = cli.get_env_history(plant_id, weather_sn, addr, day_iso, start)
        pages += 1
        obj = js.get("obj") or {}
        rows = obj.get("datas") or []
        have_next = bool(obj.get("haveNext"))
        if not rows:
            break
        all_rows.extend(rows)
        if not have_next:
            break
        start += len(rows)
        time.sleep(page_sleep)

    pts: List[Tuple[dt.datetime, float]] = []
    for r in all_rows:
        cal = r.get("calendar") or {}
        ts = _calendar_to_dt(cal) if isinstance(cal, dict) else None
        if not ts:
            continue
        rad = _safe_float(r.get("radiant"), 0.0)
        pts.append((ts, max(rad, 0.0)))

    if not pts:
        return 0.0

    pts.sort(key=lambda x: x[0])

    # Growatt timestamps here are local-naive; compare to MX-local naive derived from UTC-6.
    # (Growatt server is usually aligned to plant local time; your plants are Mexico.)
    target = (when_utc + dt.timedelta(hours=-6)).replace(tzinfo=None)
    window_sec = max(900, interval_minutes * 60 * 2)
    recent = [(t, r) for (t, r) in pts if abs((t - target).total_seconds()) <= window_sec]
    use = recent if recent else pts[-2:]
    if len(use) == 1:
        return float(use[-1][1])
    return float(0.5 * (use[-1][1] + use[-2][1]))


def _radiant_to_interval_kwh_m2(radiant_wm2: float, interval_minutes: int) -> float:
    hours = float(interval_minutes) / 60.0
    return round((max(radiant_wm2, 0.0) * hours) / 1000.0, 5)


def get_meteo_snapshot(
    plant_id_for_weather: str,
    lat: float,
    lon: float,
    weather_sn: str,
    addr: int,
    when_utc: dt.datetime,
    interval_minutes: int = 10,
) -> Tuple[float, float]:
    rad = _fetch_recent_radiant_wm2(
        plant_id=str(plant_id_for_weather),
        weather_sn=str(weather_sn),
        addr=int(addr),
        when_utc=when_utc,
        interval_minutes=int(interval_minutes),
    )
    irr = _radiant_to_interval_kwh_m2(rad, int(interval_minutes))
    print(
        f"🌞 [Meteo] plant={plant_id_for_weather} sn={weather_sn} addr={addr} day={when_utc.date().isoformat()} radiant={rad:.1f} W/m²",
        flush=True,
    )

    cloud_frac = get_cloud_fraction(float(lat), float(lon), when_utc)
    return irr, float(cloud_frac)
