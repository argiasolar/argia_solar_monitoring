from __future__ import annotations

import os
import time
import math
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

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


# ============================
# Open-Meteo: nearest hourly cloud cover
# ============================
_OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"


def _open_meteo_cloudcover_nearest(lat: float, lon: float, when_utc: dt.datetime) -> float:
    """
    Returns cloud cover (%) near the given timestamp (converted by Open-Meteo timezone=auto).
    We query hourly cloudcover and pick the closest hour.

    On failure returns 0.
    """
    timeout = _env_int("OPEN_METEO_TIMEOUT", 10)
    retries = _env_int("OPEN_METEO_RETRIES", 3)
    backoff = _env_float("OPEN_METEO_BACKOFF_SEC", 1.2)

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloudcover",
        "timezone": "auto",
        "past_days": 2,
        "forecast_days": 2,
    }

    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(_OPEN_METEO_FORECAST, params=params, timeout=timeout)
            if r.status_code != 200:
                _dbg(f"☁️ [OpenMeteo] http={r.status_code} attempt={attempt}")
                time.sleep(backoff)
                continue

            js = r.json()
            hourly = js.get("hourly") or {}
            times = hourly.get("time") or []
            clouds = hourly.get("cloudcover") or []

            if not isinstance(times, list) or not isinstance(clouds, list) or len(times) != len(clouds) or not times:
                _dbg("☁️ [OpenMeteo] bad hourly payload")
                return 0.0

            # Pick closest hour (Open-Meteo returns local timestamps in "time")
            # We'll compare by parsing to datetime (no tz) and compare to local projection of when_utc is tricky.
            # Instead we choose the last timestamp <= now_local-ish by string order and accept it.
            # Better: pick last time entry and just find nearest by absolute difference to 'now' as naive.

            # Convert when_utc to naive local? Not available. So we choose "nearest" by comparing to last entry
            # within the same date as when_utc in UTC is not stable. We'll do: choose last item of list as fallback,
            # but also compute absolute difference vs parsed times assuming they are local and when_utc is also treated naive.
            target = when_utc.replace(tzinfo=None)

            best_i = 0
            best_dt = None
            best_abs = None

            for i, tstr in enumerate(times):
                if not isinstance(tstr, str) or len(tstr) < 13:
                    continue
                try:
                    # "YYYY-MM-DDTHH:MM"
                    dti = dt.datetime.fromisoformat(tstr)
                except Exception:
                    continue
                diff = abs((dti - target).total_seconds())
                if best_abs is None or diff < best_abs:
                    best_abs = diff
                    best_dt = dti
                    best_i = i

            cc = _safe_float(clouds[best_i], 0.0)
            _dbg(f"☁️ [OpenMeteo] cloud={cc:.1f}% near={best_dt} (idx={best_i})")
            return float(cc)

        except Exception as e:
            last_err = e
            _dbg(f"☁️ [OpenMeteo] attempt={attempt} error: {e}")
            time.sleep(backoff * attempt)

    _dbg(f"☁️ [OpenMeteo] error: {last_err}")
    return 0.0


# ============================
# Growatt Web UI: ENV history radiant (W/m²)
# ============================
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
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144 Safari/537.36"
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

        cookies = self.s.cookies.get_dict()
        ok = "assToken" in cookies
        _dbg(
            f"🔐 [GrowattWeb] POST /login -> {r2.status_code} | assToken={ok} | cookies={','.join(sorted(cookies.keys()))}"
        )
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


# per-run cache
_GROWATT_CLIENT: Optional[GrowattWebClient] = None


def _get_growatt_client() -> Optional[GrowattWebClient]:
    global _GROWATT_CLIENT
    if _GROWATT_CLIENT is not None:
        return _GROWATT_CLIENT

    base = _env("GROWATT_WEB_BASE", "https://server.growatt.com")
    user = _env("GROWATT_USERNAME")
    pwd = _env("GROWATT_PASSWORD")
    if not user or not pwd:
        _dbg("❌ [GrowattWeb] Missing GROWATT_USERNAME/GROWATT_PASSWORD")
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
    """
    Pull ENV history for today's date and return a radiant W/m² estimate for the last interval.

    Strategy:
    - Download env history pages (today)
    - Convert calendar->datetime
    - Keep points within last `interval_minutes*2` window (buffer)
    - Compute average radiant of last two points (or last point)
    """
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

    # Build points
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

    # Filter to recent window in local timestamps returned by Growatt (naive)
    # We'll treat when_utc naive for approximate filtering.
    target = when_utc.replace(tzinfo=None)
    window_sec = max(900, interval_minutes * 60 * 2)  # at least 15 min buffer
    recent = [(t, r) for (t, r) in pts if abs((t - target).total_seconds()) <= window_sec]

    use = recent if len(recent) >= 1 else pts[-2:]
    if len(use) == 1:
        return float(use[-1][1])

    # Average of last two points
    return float(0.5 * (use[-1][1] + use[-2][1]))


def _radiant_to_interval_kwh_m2(radiant_wm2: float, interval_minutes: int) -> float:
    """
    Convert radiant W/m² (assumed approx constant during interval) to kWh/m² for that interval:
      Wh/m² = W/m² * hours
      kWh/m² = Wh/m² / 1000
    """
    hours = float(interval_minutes) / 60.0
    return round((max(radiant_wm2, 0.0) * hours) / 1000.0, 5)


# ============================
# Public API
# ============================
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
      (Irradiance_kWh_m2_for_interval, Cloud_Coverage_percent)

    - Irradiance: based on Growatt ENV 'radiant' W/m², converted to kWh/m² for the interval
    - Clouds: Open-Meteo hourly cloud cover nearest to timestamp
    """
    rad = _fetch_recent_radiant_wm2(
        plant_id=str(plant_id_for_weather),
        weather_sn=str(weather_sn),
        addr=int(addr),
        when_utc=when_utc,
        interval_minutes=int(interval_minutes),
    )
    irr = _radiant_to_interval_kwh_m2(rad, int(interval_minutes))

    # small informative log line (you already saw this)
    print(f"🌞 [Meteo] plant={plant_id_for_weather} sn={weather_sn} addr={addr} day={when_utc.date().isoformat()} radiant={rad:.1f} W/m²", flush=True)

    clouds = _open_meteo_cloudcover_nearest(float(lat), float(lon), when_utc)
    return irr, clouds
