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
# Open-Meteo cloud cover (07-19)
# ============================

_OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
_OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

def _avg_cloudcover_7_19_from_open_meteo(lat: float, lon: float, date_iso: str) -> float:
    """
    Average hourly cloud cover (%) between 07:00 and 19:00 (inclusive) in local timezone ("auto")
    for given lat/lon and date.
    """
    start_hour = _env_int("CLOUDS_START_HOUR", 7)
    end_hour = _env_int("CLOUDS_END_HOUR", 19)

    def compute_from_json(js: Dict[str, Any]) -> Optional[float]:
        hourly = js.get("hourly") or {}
        times = hourly.get("time") or []
        clouds = hourly.get("cloudcover") or []
        if not isinstance(times, list) or not isinstance(clouds, list) or len(times) != len(clouds) or len(times) == 0:
            return None

        vals: List[float] = []
        for t_str, c in zip(times, clouds):
            # t_str expected: "YYYY-MM-DDTHH:MM"
            if not isinstance(t_str, str):
                continue
            if not t_str.startswith(date_iso):
                continue
            # parse hour
            try:
                hour = int(t_str[11:13])
            except Exception:
                continue
            if start_hour <= hour <= end_hour:
                vals.append(_safe_float(c, 0.0))

        if not vals:
            return None
        return round(sum(vals) / len(vals), 1)

    # 1) Try archive API (best for historical)
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloudcover",
        "timezone": "auto",
        "start_date": date_iso,
        "end_date": date_iso,
    }

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = requests.get(_OPEN_METEO_ARCHIVE, params=params, timeout=25)
            if r.status_code == 200:
                js = r.json()
                v = compute_from_json(js)
                if v is not None:
                    _dbg(f"☁️ [OpenMeteo:archive] {date_iso} clouds_7_19={v}")
                    return v
                _dbg(f"☁️ [OpenMeteo:archive] {date_iso} no hourly data in range 7-19")
                break
            _dbg(f"☁️ [OpenMeteo:archive] http={r.status_code} date={date_iso}")
        except Exception as e:
            last_err = e
            _dbg(f"☁️ [OpenMeteo:archive] attempt={attempt+1} error: {e}")
            time.sleep(2)

    # 2) Fallback: forecast API (works well for recent days)
    # forecast supports past_days; we can request a window that includes date_iso.
    # We'll request past_days=10 to cover "yesterday-ish" runs.
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
            js2 = r2.json()
            v2 = compute_from_json(js2)
            if v2 is not None:
                _dbg(f"☁️ [OpenMeteo:forecast] {date_iso} clouds_7_19={v2}")
                return v2
            _dbg(f"☁️ [OpenMeteo:forecast] {date_iso} no hourly data in range 7-19")
        else:
            _dbg(f"☁️ [OpenMeteo:forecast] http={r2.status_code} date={date_iso}")
    except Exception as e2:
        _dbg(f"☁️ [OpenMeteo:forecast] error: {e2}")
        if last_err:
            _dbg(f"☁️ [OpenMeteo] last archive error was: {last_err}")

    return 0.0


# ============================
# Growatt Web UI (Irradiance kWh/m²/day)
# ============================

class GrowattWebClient:
    """
    Minimal client for Growatt web endpoints used by ENV/weather station pages.
    """
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
        self.s.cookies.set("selectedPlantId", str(plant_id))

    def env_page_seed(self, plant_id: str) -> None:
        self._seed_plant(plant_id)
        r = self.s.get(f"{self.base}/device/getEnvPage", timeout=30)
        _dbg(f"🧭 [GrowattWeb] GET /device/getEnvPage plant={plant_id} -> {r.status_code}")

    def get_env_list(self, plant_id: str, curr_page: int = 1) -> Dict[str, Any]:
        self._seed_plant(plant_id)
        url = f"{self.base}/device/getEnvList"
        payload = {"plantId": str(plant_id), "currPage": str(curr_page), "alias": ""}
        r = self.s.post(url, data=payload, timeout=40)
        _dbg(f"📡 [GrowattWeb] getEnvList plant={plant_id} page={curr_page} -> {r.status_code}")
        r.raise_for_status()
        return r.json()

    def get_env_history(self, plant_id: str, datalog_sn: str, addr: int, day_iso: str, start: int) -> Dict[str, Any]:
        """
        Based on your working logs: uses startDate/endDate + start offset.
        """
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
        _dbg(f"📈 [GrowattWeb] getEnvHistory plant={plant_id} sn={datalog_sn} addr={addr} day={day_iso} start={start} -> {r.status_code}")
        r.raise_for_status()
        return r.json()


def _calendar_to_dt(cal: Dict[str, Any]) -> Optional[dt.datetime]:
    """
    Growatt calendar month is 0-based (0..11): confirmed by your raw JSON.
    Keys: year, month, dayOfMonth, hourOfDay, minute, second
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


def _pick_env_device(env_list: Dict[str, Any], prefer_sn: Optional[str], prefer_addr: Optional[int]) -> Optional[Tuple[str, int]]:
    datas = env_list.get("datas") or []
    devices: List[Tuple[str, int]] = []
    for d in datas:
        if not isinstance(d, dict):
            continue
        sn = d.get("datalogSn") or d.get("dataLogSn") or d.get("sn")
        addr = d.get("addr")
        if not sn or addr is None:
            continue
        try:
            devices.append((str(sn), int(addr)))
        except Exception:
            continue

    if not devices:
        return None

    if prefer_sn:
        for sn, a in devices:
            if sn == prefer_sn and (prefer_addr is None or a == prefer_addr):
                return sn, a

    return devices[0]


def _integrate_radiant_kwh_m2(rows: List[Dict[str, Any]], tag: str) -> float:
    """
    Trapezoidal integration:
      kWh/m² = Σ( (r0+r1)/2 * Δt[h] ) / 1000
    where r is radiant in W/m².
    """
    pts: List[Tuple[dt.datetime, float]] = []
    bad_cal = 0
    missing_rad = 0

    for r in rows:
        cal = r.get("calendar") or {}
        ts = _calendar_to_dt(cal) if isinstance(cal, dict) else None
        if not ts:
            bad_cal += 1
            continue
        if "radiant" not in r:
            missing_rad += 1
            continue
        rad = _safe_float(r.get("radiant"), 0.0)
        pts.append((ts, max(rad, 0.0)))

    if _dbg_on() and rows:
        sample = rows[0]
        rad_keys = [k for k in sample.keys() if "rad" in k.lower() or "irr" in k.lower()]
        _dbg(f"🔎 [GrowattWeb] {tag} sample_keys={list(sample.keys())[:14]}")
        _dbg(f"🔎 [GrowattWeb] {tag} sample_calendar={sample.get('calendar')}")
        _dbg(f"🔎 [GrowattWeb] {tag} radiant_like_keys={rad_keys} radiant={sample.get('radiant')} etodayRadiation={sample.get('etodayRadiation')}")
        _dbg(f"🧪 [GrowattWeb] {tag} rows={len(rows)} pts={len(pts)} bad_calendar={bad_cal} missing_radiant_key={missing_rad}")

    if len(pts) < 2:
        return 0.0

    pts.sort(key=lambda x: x[0])

    wh_m2 = 0.0
    max_gap_sec = _env_int("GROWATT_MAX_GAP_SEC", 7200)  # cap gaps to 2h by default

    for i in range(1, len(pts)):
        t0, r0 = pts[i - 1]
        t1, r1 = pts[i]
        dt_sec = (t1 - t0).total_seconds()
        if dt_sec <= 0:
            continue
        if dt_sec > max_gap_sec:
            dt_sec = max_gap_sec
        avg_w = 0.5 * (r0 + r1)
        wh_m2 += avg_w * (dt_sec / 3600.0)

    kwh_m2 = round(wh_m2 / 1000.0, 3)

    if _dbg_on():
        rads = [p[1] for p in pts]
        _dbg(
            f"🌞 [GrowattWeb] {tag} ts=[{pts[0][0].isoformat()}..{pts[-1][0].isoformat()}] "
            f"rad[min/mean/max]=[{min(rads):.1f}/{(sum(rads)/len(rads)):.1f}/{max(rads):.1f}] -> {kwh_m2} kWh/m²"
        )

    return kwh_m2


# ----------------------------
# Caches (per-run)
# ----------------------------
_GROWATT_CLIENT: Optional[GrowattWebClient] = None
_GROWATT_ENV_DEVICE_CACHE: Dict[str, Tuple[str, int]] = {}       # plant_id -> (sn, addr)
_GROWATT_IRR_CACHE: Dict[Tuple[str, str], float] = {}           # (plant_id, date_iso) -> kWh/m²


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


def _get_or_pick_env_device(cli: GrowattWebClient, plant_id: str, prefer_sn: Optional[str], prefer_addr: Optional[int]) -> Optional[Tuple[str, int]]:
    if plant_id in _GROWATT_ENV_DEVICE_CACHE:
        return _GROWATT_ENV_DEVICE_CACHE[plant_id]

    cli.env_page_seed(plant_id)
    env_list = cli.get_env_list(plant_id, 1)
    picked = _pick_env_device(env_list, prefer_sn, prefer_addr)
    if not picked:
        _dbg(f"⚠️ [GrowattWeb] No ENV devices in env_list for plant={plant_id}")
        return None

    _GROWATT_ENV_DEVICE_CACHE[plant_id] = picked
    return picked


def get_growatt_irradiance_kwh_m2(plant_id: str, date_iso: str, prefer_sn: Optional[str], prefer_addr: Optional[int]) -> float:
    """
    Returns daily irradiation in kWh/m² for given Growatt plant_id.
    Uses per-run caches.
    """
    key = (plant_id, date_iso)
    if key in _GROWATT_IRR_CACHE:
        return _GROWATT_IRR_CACHE[key]

    cli = _get_growatt_client()
    if cli is None:
        _GROWATT_IRR_CACHE[key] = 0.0
        return 0.0

    fallback_days = _env_int("GROWATT_FALLBACK_DAYS", 2)
    _dbg(f"🌦️ [ARGIA_WEATHER] Growatt irradiance start plant={plant_id} date={date_iso} fallback_days={fallback_days}")

    picked = _get_or_pick_env_device(cli, plant_id, prefer_sn, prefer_addr)
    if not picked:
        _GROWATT_IRR_CACHE[key] = 0.0
        return 0.0
    sn, addr = picked
    _dbg(f"✅ [GrowattWeb] plant={plant_id} chosen_env sn={sn} addr={addr}")

    d0 = dt.date.fromisoformat(date_iso)
    try_days = [(d0 - dt.timedelta(days=i)).isoformat() for i in range(0, fallback_days + 1)]
    _dbg(f"🗓️ [GrowattWeb] plant={plant_id} try_days={try_days}")

    for day in try_days:
        all_rows: List[Dict[str, Any]] = []
        start = 0
        pages = 0

        while True:
            js = cli.get_env_history(plant_id, sn, addr, day, start)
            pages += 1

            obj = js.get("obj") or {}
            rows = obj.get("datas") or []
            have_next = bool(obj.get("haveNext"))

            all_rows.extend(rows)

            _dbg(f"📈 [GrowattWeb] plant={plant_id} day={day} page={pages} start={start} rows={len(rows)} total={len(all_rows)} haveNext={have_next}")

            if not have_next or not rows:
                break

            start += len(rows)
            time.sleep(0.10)

        irr = _integrate_radiant_kwh_m2(all_rows, tag=f"plant={plant_id} day={day}")
        if irr > 0:
            _GROWATT_IRR_CACHE[(plant_id, day)] = irr
            _GROWATT_IRR_CACHE[key] = irr
            return irr

    _dbg(f"❌ [GrowattWeb] plant={plant_id} computed irr=0 for all try_days")
    _GROWATT_IRR_CACHE[key] = 0.0
    return 0.0


# ============================
# Public API used by argia.py
# ============================

def get_weather_for_date(p_key: str, date_iso: str, plants_config: dict) -> Tuple[float, float]:
    """
    Returns: (irradiance_kWh_m2, cloudcover_avg_7_19_pct)

    Irradiance rule:
      - if brand == GROWATT: use its own site_id as plant_id
      - if brand == HUAWEI: use fallback Growatt plant (default SMS 10069072 with DYD1EZR007 addr 32)
    Cloud cover rule:
      - Open-Meteo hourly cloudcover averaged 07:00–19:00 local time
    """
    conf = plants_config.get(p_key, {}) if isinstance(plants_config, dict) else {}

    lat = conf.get("lat")
    lon = conf.get("lon")
    if not lat or not lon:
        # no coords => no cloud cover; irradiance still possible, but config seems missing
        return 0.0, 0.0

    brand = str(conf.get("brand") or "").strip().upper()
    site_id = str(conf.get("site_id") or "").strip()

    fallback_plant = _env("GROWATT_WEATHER_FALLBACK_PLANT_ID", "10069072")

    irr_plant_id = site_id if (brand == "GROWATT" and site_id) else str(fallback_plant)

    # Prefer SN/ADDR only for the fallback plant (your SMS station)
    prefer_sn: Optional[str] = None
    prefer_addr: Optional[int] = None
    if irr_plant_id == str(fallback_plant):
        prefer_sn = _env("GROWATT_WEATHER_FALLBACK_DATALOG_SN", "DYD1EZR007")
        try:
            prefer_addr = int(_env("GROWATT_WEATHER_FALLBACK_ADDR", "32") or "32")
        except Exception:
            prefer_addr = None

    irr = 0.0
    if str(irr_plant_id).isdigit():
        irr = get_growatt_irradiance_kwh_m2(str(irr_plant_id), date_iso, prefer_sn, prefer_addr)
    else:
        _dbg(f"⚠️ [ARGIA_WEATHER] non-numeric irradiance plantId={irr_plant_id}")

    clouds = _avg_cloudcover_7_19_from_open_meteo(float(lat), float(lon), date_iso)

    _dbg(f"📌 [ARGIA_WEATHER] p_key={p_key} brand={brand} irr_source_plant={irr_plant_id} date={date_iso} -> irr={irr} clouds_7_19={clouds}")
    return irr, clouds
