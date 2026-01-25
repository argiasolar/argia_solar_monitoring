# argia_weather.py
# Weather provider for ARGIA PV monitoring:
# - Irradiance: Growatt Web UI endpoints (getEnvList + getEnvHistory)
# - Cloud cover: Open-Meteo archive API (as before)

from __future__ import annotations

import os
import time
import json
import requests
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------
# Open-Meteo (Cloud cover)
# -----------------------------

OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).strip().replace(",", "."))
    except Exception:
        return default

def _get_cloud_cover_open_meteo(lat: float, lon: float, date_iso: str) -> float:
    """
    Returns cloudcover_mean (%) for the date.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "cloudcover_mean",
        "timezone": "auto",
        "start_date": date_iso,
        "end_date": date_iso,
    }
    for attempt in range(3):
        try:
            r = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=20)
            if r.status_code == 200:
                js = r.json()
                cc = (js.get("daily", {}).get("cloudcover_mean") or [0])[0]
                return round(_safe_float(cc, 0.0), 1)
        except Exception:
            time.sleep(2)
    return 0.0


# -----------------------------
# Growatt Web UI (Irradiance)
# -----------------------------

GROWATT_WEB_BASE = (os.environ.get("GROWATT_WEB_BASE") or "https://server.growatt.com").rstrip("/")
GROWATT_USERNAME = os.environ.get("GROWATT_USERNAME")
GROWATT_PASSWORD = os.environ.get("GROWATT_PASSWORD")

# For Huawei plants: use a reference Growatt plant as weather source
GROWATT_WEATHER_FALLBACK_PLANT_ID = os.environ.get("GROWATT_WEATHER_FALLBACK_PLANT_ID") or "10069072"  # SMS default
# Optional preference (not required)
GROWATT_WEATHER_FALLBACK_DATALOG_SN = os.environ.get("GROWATT_WEATHER_FALLBACK_DATALOG_SN") or "DYD1EZR007"
GROWATT_WEATHER_FALLBACK_ADDR = os.environ.get("GROWATT_WEATHER_FALLBACK_ADDR") or "32"

# Cache (per process run)
# - env devices list per plant_id
# - computed irradiance per (plant_id, date_iso)
_ENV_DEVICES_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_IRRADIANCE_CACHE: Dict[Tuple[str, str], float] = {}

def _safe_json_loads(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    start_candidates = [text.find("{"), text.find("[")]
    start_candidates = [i for i in start_candidates if i >= 0]
    if not start_candidates:
        return None
    start = min(start_candidates)
    return json.loads(text[start:])

def _request_any(session: requests.Session, method: str, url: str, **kwargs) -> Tuple[int, Any, str]:
    resp = session.request(method, url, **kwargs)
    raw = resp.text or ""
    parsed = None
    try:
        parsed = resp.json()
    except Exception:
        try:
            parsed = _safe_json_loads(raw)
        except Exception:
            parsed = None
    return resp.status_code, parsed, raw

class GrowattWebClient:
    """
    Minimal Growatt Web UI client (same flow as growatt_weather_fetch.py, but slimmed down).
    """
    def __init__(self, base: str, username: str, password: str) -> None:
        self.base = base
        self.username = username
        self.password = password
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8,pl;q=0.7,cs;q=0.6",
                "Connection": "keep-alive",
            }
        )

    def login(self) -> bool:
        if not self.username or not self.password:
            return False

        login_url = f"{self.base}/login"
        st, _, _ = _request_any(self.s, "GET", login_url, timeout=30)
        if st != 200:
            return False

        payload = {"account": self.username, "password": self.password}
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/login",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        st, _, raw = _request_any(self.s, "POST", login_url, data=payload, headers=headers, timeout=30)

        cookies = self.s.cookies.get_dict()
        # Web UI auth cookie
        if "assToken" not in cookies:
            # sometimes login endpoint still returns 200 but no cookie
            return False
        return True

    def _seed_plant_context(self, plant_id: str) -> None:
        # Web UI relies on selectedPlantId cookie
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device/getEnvPage")

    def post_get_env_list(self, plant_id: str, curr_page: int = 1, alias: str = "") -> Any:
        self._seed_plant_context(plant_id)
        url = f"{self.base}/device/getEnvList"
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/device/getEnvPage",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        data = {
            "plantId": str(plant_id),
            "currPage": str(curr_page),
            "alias": alias,
        }
        st, parsed, raw = _request_any(self.s, "POST", url, headers=headers, data=data, timeout=45)
        if parsed is None:
            return {"_http": st, "_parse_error": True, "_raw_snippet": (raw or "")[:200]}
        if isinstance(parsed, dict):
            parsed["_http"] = st
        return parsed

    def post_get_env_history(
        self,
        plant_id: str,
        datalog_sn: str,
        addr: int,
        day_iso: str,
        start: int = 0,
    ) -> Tuple[int, Any]:
        self._seed_plant_context(plant_id)
        url = f"{self.base}/device/getEnvHistory"
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/device/getEnvPage",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        data = {
            "datalogSn": datalog_sn,
            "addr": str(addr),
            "startDate": day_iso,
            "endDate": day_iso,
            "start": str(start),
        }
        st, parsed, raw = _request_any(self.s, "POST", url, headers=headers, data=data, timeout=45)
        if parsed is None:
            return st, {"_parse_error": True, "_raw_snippet": (raw or "")[:200]}
        return st, parsed

def _extract_env_datas(resp: Any) -> List[Dict[str, Any]]:
    """
    Growatt env history expected:
      {"result":1,"obj":{"datas":[...], "haveNext": true/false, "start": 80, ...}}
    """
    if not isinstance(resp, dict):
        return []
    obj = resp.get("obj")
    if not isinstance(obj, dict):
        return []
    datas = obj.get("datas")
    if not isinstance(datas, list):
        return []
    return [x for x in datas if isinstance(x, dict)]

def _resp_have_next(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    obj = resp.get("obj")
    if not isinstance(obj, dict):
        return False
    return bool(obj.get("haveNext"))

def _resp_next_start(resp: Any, current_start: int, page_rows: int) -> int:
    if isinstance(resp, dict):
        obj = resp.get("obj")
        if isinstance(obj, dict):
            nxt = obj.get("start")
            try:
                if nxt is not None:
                    return int(nxt)
            except Exception:
                pass
    return current_start + max(page_rows, 0)

def _calendar_to_dt(cal: Any) -> Optional[datetime]:
    if not isinstance(cal, dict):
        return None

    def g(*keys: str) -> Optional[int]:
        for k in keys:
            if k in cal:
                try:
                    return int(cal[k])
                except Exception:
                    return None
        return None

    y = g("year")
    m = g("month")
    d = g("day", "dayOfMonth")
    hh = g("hour", "hourOfDay") or 0
    mm = g("minute") or 0
    ss = g("second") or 0
    if y and m and d:
        try:
            return datetime(y, m, d, hh, mm, ss)
        except Exception:
            return None
    return None

def _integrate_radiant_to_kwh_m2(points: List[Tuple[datetime, float]]) -> float:
    """
    Convert time series radiant(W/m2) into daily energy (kWh/m2):
      kWh/m2 = Σ( radiant(W/m2) * Δt[h] ) / 1000
    Uses left Riemann sum with basic guards on Δt.
    """
    if len(points) < 2:
        return 0.0

    points.sort(key=lambda x: x[0])
    total = 0.0

    for i in range(len(points) - 1):
        t0, r0 = points[i]
        t1, _ = points[i + 1]
        dt_sec = (t1 - t0).total_seconds()
        if dt_sec <= 0:
            continue

        # Guard: if gap too large, we skip (data holes)
        # Typical env sampling is minutes; > 3h likely a hole
        if dt_sec > 3 * 3600:
            continue

        dt_h = dt_sec / 3600.0
        if r0 < 0:
            continue

        total += (r0 * dt_h) / 1000.0

    return total

def _pick_device(devices: List[Dict[str, Any]], prefer_sn: str = "", prefer_addr: Optional[int] = None) -> Optional[Tuple[str, int]]:
    """
    Prefer a specific (sn, addr) if present; otherwise pick the first valid.
    """
    def _norm_sn(d: Dict[str, Any]) -> str:
        return str(d.get("datalogSn") or d.get("dataLogSn") or d.get("sn") or "").strip()

    # first pass: exact match
    if prefer_sn:
        for d in devices:
            sn = _norm_sn(d)
            addr = d.get("addr")
            if not sn or addr is None:
                continue
            try:
                a = int(addr)
            except Exception:
                continue
            if sn == prefer_sn and (prefer_addr is None or a == prefer_addr):
                return sn, a

    # second pass: any valid
    for d in devices:
        sn = _norm_sn(d)
        addr = d.get("addr")
        if not sn or addr is None:
            continue
        try:
            a = int(addr)
        except Exception:
            continue
        return sn, a

    return None

def _get_env_devices(cli: GrowattWebClient, plant_id: str) -> List[Dict[str, Any]]:
    """
    Slim pagination for getEnvList. Usually 1 page is enough, but we try few.
    """
    if plant_id in _ENV_DEVICES_CACHE:
        return _ENV_DEVICES_CACHE[plant_id]

    devices: List[Dict[str, Any]] = []
    seen = set()

    # try up to 5 pages (usually 1)
    for page in range(1, 6):
        resp = cli.post_get_env_list(plant_id=plant_id, curr_page=page, alias="")
        datas = []
        if isinstance(resp, dict):
            datas = resp.get("datas") if isinstance(resp.get("datas"), list) else []
        if not datas:
            break

        for d in datas:
            if not isinstance(d, dict):
                continue
            sn = d.get("datalogSn") or d.get("dataLogSn") or d.get("sn")
            addr = d.get("addr")
            if not sn or addr is None:
                continue
            try:
                a = int(addr)
            except Exception:
                continue
            key = (str(sn), a)
            if key in seen:
                continue
            seen.add(key)
            devices.append(d)

    _ENV_DEVICES_CACHE[plant_id] = devices
    return devices

def _get_irradiance_kwh_m2_from_growatt(plant_id: str, date_iso: str, prefer_sn: str = "", prefer_addr: Optional[int] = None) -> float:
    """
    Returns daily irradiance in kWh/m2 from Growatt ENV history.
    Uses caching per (plant_id, date_iso).
    """
    cache_key = (str(plant_id), str(date_iso))
    if cache_key in _IRRADIANCE_CACHE:
        return _IRRADIANCE_CACHE[cache_key]

    # Missing creds -> cannot fetch
    if not GROWATT_USERNAME or not GROWATT_PASSWORD:
        _IRRADIANCE_CACHE[cache_key] = 0.0
        return 0.0

    cli = GrowattWebClient(base=GROWATT_WEB_BASE, username=GROWATT_USERNAME, password=GROWATT_PASSWORD)
    if not cli.login():
        _IRRADIANCE_CACHE[cache_key] = 0.0
        return 0.0

    # find devices
    devices = _get_env_devices(cli, str(plant_id))
    chosen = _pick_device(devices, prefer_sn=prefer_sn, prefer_addr=prefer_addr)
    if not chosen:
        _IRRADIANCE_CACHE[cache_key] = 0.0
        return 0.0

    datalog_sn, addr = chosen

    # Fetch history (paginate via haveNext + start)
    all_rows: List[Dict[str, Any]] = []
    start = 0
    guard = 0

    while True:
        guard += 1
        if guard > 120:  # safety
            break

        st, resp = cli.post_get_env_history(str(plant_id), datalog_sn, int(addr), date_iso, start=start)
        if st != 200:
            break

        rows = _extract_env_datas(resp)
        if rows:
            all_rows.extend(rows)

        if not _resp_have_next(resp):
            break

        start = _resp_next_start(resp, current_start=start, page_rows=len(rows))
        if len(rows) == 0:
            break

        time.sleep(0.12)

    # Build points list (datetime, radiant)
    points: List[Tuple[datetime, float]] = []
    for r in all_rows:
        dt_obj = _calendar_to_dt(r.get("calendar"))
        if not dt_obj:
            continue
        rad = r.get("radiant")
        rad_f = _safe_float(rad, default=-1.0)
        if rad_f < 0:
            continue
        points.append((dt_obj, rad_f))

    irr = _integrate_radiant_to_kwh_m2(points)
    irr = round(float(irr), 3)

    _IRRADIANCE_CACHE[cache_key] = irr
    return irr


# -----------------------------
# Public API used by argia.py
# -----------------------------

def get_weather_for_date(p_key: str, date_iso: str, plants_config: dict) -> Tuple[float, float]:
    """
    Returns:
      (irradiance_kWh_m2, cloud_cover_pct)

    Logic:
      - cloud cover: Open-Meteo using plant lat/lon
      - irradiance:
          * if plant brand == GROWATT -> use its own site_id
          * if plant brand == HUAWEI  -> use reference Growatt plant id (SMS: 10069072 by default)
    """
    conf = plants_config.get(p_key, {}) if isinstance(plants_config, dict) else {}

    lat = _safe_float(conf.get("lat"), 0.0)
    lon = _safe_float(conf.get("lon"), 0.0)

    # Cloud cover (keep as before)
    clouds = 0.0
    if lat != 0.0 or lon != 0.0:
        clouds = _get_cloud_cover_open_meteo(lat, lon, date_iso)

    brand = str(conf.get("brand") or "").strip().upper()
    site_id = str(conf.get("site_id") or "").strip()

    # Choose plant_id for irradiance
    irr_plant_id = site_id if brand == "GROWATT" and site_id else GROWATT_WEATHER_FALLBACK_PLANT_ID

    # Prefer hint only for the fallback plant (your SMS plant)
    prefer_sn = ""
    prefer_addr: Optional[int] = None
    if str(irr_plant_id) == str(GROWATT_WEATHER_FALLBACK_PLANT_ID):
        prefer_sn = str(GROWATT_WEATHER_FALLBACK_DATALOG_SN or "").strip()
        try:
            prefer_addr = int(str(GROWATT_WEATHER_FALLBACK_ADDR))
        except Exception:
            prefer_addr = None

    irr = 0.0
    # Only numeric plant ids are valid for Growatt web context here
    if irr_plant_id.isdigit():
        irr = _get_irradiance_kwh_m2_from_growatt(
            plant_id=irr_plant_id,
            date_iso=date_iso,
            prefer_sn=prefer_sn,
            prefer_addr=prefer_addr,
        )

    return irr, clouds
