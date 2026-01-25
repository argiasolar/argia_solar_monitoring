# argia_weather.py
import os
import time
import json
import math
import requests
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------
# Helpers / logging
# -----------------------------
def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(_env(name, str(default))).strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name, None)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _calendar_to_dt(cal: Dict[str, Any]) -> Optional[dt.datetime]:
    """
    Growatt uses month: 0..11 and keys: year, month, dayOfMonth, hourOfDay/minute/second
    """
    try:
        y = int(cal.get("year"))
        m0 = int(cal.get("month"))
        d = int(cal.get("dayOfMonth"))
        hh = int(cal.get("hourOfDay", cal.get("hour", 0)))
        mm = int(cal.get("minute", 0))
        ss = int(cal.get("second", 0))
        # month is 0-based
        return dt.datetime(y, m0 + 1, d, hh, mm, ss)
    except Exception:
        return None


# -----------------------------
# Growatt Web client (same flow as Web UI)
# -----------------------------
class GrowattWebClient:
    def __init__(self, base: str, username: str, password: str, debug: bool = False) -> None:
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.debug = debug
        self.sess = requests.Session()
        self.sess.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (argia_solar_monitoring)",
                "Accept": "application/json, text/plain, */*",
            }
        )
        self.ass_token: Optional[str] = None

    def _dbg(self, msg: str) -> None:
        if self.debug:
            _log(msg)

    def login(self) -> None:
        # 1) open login page (sets cookies)
        url_get = f"{self.base}/login"
        r1 = self.sess.get(url_get, timeout=30)
        self._dbg(f"🔐 [GrowattWeb] GET /login -> HTTP {r1.status_code}")

        # 2) post login (Web UI flow)
        url_post = f"{self.base}/login"
        payload = {"account": self.username, "password": self.password}
        r2 = self.sess.post(url_post, data=payload, timeout=30)
        ok = (r2.status_code == 200) and ("assToken" in self.sess.cookies.get_dict())
        self.ass_token = self.sess.cookies.get_dict().get("assToken")
        self._dbg(
            f"🔐 [GrowattWeb] POST /login -> HTTP {r2.status_code} | assToken={bool(self.ass_token)} | cookies={','.join(self.sess.cookies.get_dict().keys())}"
        )
        if not ok:
            raise RuntimeError("Growatt web login failed (no assToken cookie).")

    def get_env_list(self, plant_id: str, page: int = 1) -> Dict[str, Any]:
        url = f"{self.base}/device/getEnvList"
        payload = {"plantId": plant_id, "currPage": page}
        r = self.sess.post(url, data=payload, timeout=30)
        self._dbg(f"📡 [GrowattWeb] getEnvList plant={plant_id} page={page} -> http={r.status_code}")
        r.raise_for_status()
        return r.json()

    def get_env_history(self, plant_id: str, datalog_sn: str, addr: int, day_iso: str, start: int = 0) -> Dict[str, Any]:
        """
        Web UI endpoint:
          POST /device/getEnvHistory
        Important:
          - start is offset (0,80,160,...) for pagination
        """
        url = f"{self.base}/device/getEnvHistory"
        payload = {
            "plantId": plant_id,
            "datalogSn": datalog_sn,
            "addr": str(addr),
            "date": day_iso,
            "start": str(start),
        }
        r = self.sess.post(url, data=payload, timeout=45)
        self._dbg(
            f"📈 [GrowattWeb] getEnvHistory plant={plant_id} sn={datalog_sn} addr={addr} day={day_iso} start={start} -> HTTP={r.status_code}"
        )
        r.raise_for_status()
        return r.json()


def _pick_env_device(env_list_json: Dict[str, Any], preferred_sn: Optional[str] = None, preferred_addr: Optional[str] = None) -> Optional[Tuple[str, int]]:
    """
    env_list_json expected keys: obj.datas[*].datalogSn + addr
    but sometimes returned as: datalogSn or dataLogSn depending on endpoint/version.
    We'll handle both.
    """
    obj = env_list_json.get("obj") or {}
    datas = obj.get("datas") or []

    devices: List[Tuple[str, int]] = []
    for d in datas:
        sn = d.get("datalogSn") or d.get("dataLogSn") or d.get("sn") or d.get("datalog_sn")
        addr = d.get("addr")
        if sn is None or addr is None:
            continue
        try:
            devices.append((str(sn), int(addr)))
        except Exception:
            continue

    if not devices:
        return None

    if preferred_sn:
        for sn, a in devices:
            if sn == preferred_sn:
                if preferred_addr is None or int(preferred_addr) == a:
                    return sn, a

    # fallback: first device
    return devices[0]


# -----------------------------
# Irradiance calculation
# -----------------------------
def _compute_daily_kwh_m2_from_radiant(rows: List[Dict[str, Any]], debug: bool = False, tag: str = "") -> float:
    """
    rows: list of measurement dicts from getEnvHistory 'datas'
    We integrate radiant (W/m²) over time -> Wh/m² -> kWh/m²
    Uses trapezoidal integration on sorted timestamps.
    """
    pts: List[Tuple[dt.datetime, float]] = []
    for r in rows:
        cal = r.get("calendar") or {}
        ts = _calendar_to_dt(cal)
        if ts is None:
            continue
        rad = _safe_float(r.get("radiant"), 0.0)  # W/m²
        # guard for NaN
        if math.isnan(rad) or math.isinf(rad):
            continue
        pts.append((ts, max(rad, 0.0)))

    if not pts:
        if debug:
            _log(f"🌞 [GrowattWeb] {tag} no valid radiant points -> kWh/m2=0.0")
        return 0.0

    pts.sort(key=lambda x: x[0])

    # trapezoid
    wh_m2 = 0.0
    for i in range(1, len(pts)):
        t0, r0 = pts[i - 1]
        t1, r1 = pts[i]
        dt_s = (t1 - t0).total_seconds()
        if dt_s <= 0:
            continue
        # protect against insane gaps (e.g., >2h) – still integrate but cap to avoid crazy spikes
        if dt_s > 2 * 3600:
            dt_s = 2 * 3600
        avg_w = 0.5 * (r0 + r1)
        wh_m2 += avg_w * (dt_s / 3600.0)  # W * h = Wh

    kwh_m2 = wh_m2 / 1000.0

    if debug:
        rads = [p[1] for p in pts]
        _log(
            f"🌞 [GrowattWeb] {tag} points={len(pts)} "
            f"ts=[{pts[0][0].isoformat()}..{pts[-1][0].isoformat()}] "
            f"rad[min/mean/max]=[{min(rads):.1f}/{(sum(rads)/len(rads)):.1f}/{max(rads):.1f}] W/m2 "
            f"-> {kwh_m2:.3f} kWh/m2"
        )

    return round(kwh_m2, 3)


def get_growatt_irradiance_kwh_m2(
    plant_id: str,
    day_iso: str,
    preferred_sn: Optional[str] = None,
    preferred_addr: Optional[int] = None,
    fallback_days: int = 2,
    debug: bool = False,
) -> float:
    """
    Returns daily irradiation in kWh/m².
    Tries day_iso, then day-1, ... day-fallback_days (helps for "today not closed yet").
    """
    base = _env("GROWATT_BASE", "https://server.growatt.com")
    user = _env("GROWATT_USERNAME")
    pwd = _env("GROWATT_PASSWORD")
    if not user or not pwd:
        raise RuntimeError("Missing GROWATT_USERNAME / GROWATT_PASSWORD env vars.")

    cli = GrowattWebClient(base=base, username=user, password=pwd, debug=debug)
    cli.login()

    # env list (device selection)
    env_list = cli.get_env_list(plant_id, page=1)
    picked = _pick_env_device(env_list, preferred_sn=preferred_sn, preferred_addr=str(preferred_addr) if preferred_addr is not None else None)
    if not picked:
        if debug:
            _log(f"❌ [GrowattWeb] No ENV devices for plant={plant_id}")
        return 0.0

    sn, addr = picked
    if debug:
        _log(f"✅ [GrowattWeb] Chosen ENV device plant={plant_id}: sn={sn} addr={addr}")

    # try dates
    d0 = dt.date.fromisoformat(day_iso)
    try_dates = [(d0 - dt.timedelta(days=i)).isoformat() for i in range(0, max(fallback_days, 0) + 1)]
    if debug:
        _log(f"🗓️  [GrowattWeb] Dates to try for irradiance plant={plant_id}: {try_dates}")

    for d in try_dates:
        all_rows: List[Dict[str, Any]] = []
        start = 0
        pages = 0
        total_rows = 0

        while True:
            js = cli.get_env_history(plant_id, sn, addr, d, start=start)
            pages += 1

            # Typical structure: {"result":1,"obj":{"datas":[...], "haveNext": true/false, "total": 254, ...}}
            obj = js.get("obj") or {}
            rows = obj.get("datas") or []
            have_next = bool(obj.get("haveNext"))

            all_rows.extend(rows)
            total_rows += len(rows)

            if debug:
                _log(
                    f"📈 [GrowattWeb] page={pages} plant={plant_id} day={d} start={start} "
                    f"rows_page={len(rows)} total_acc={total_rows} haveNext={have_next}"
                )

            if not have_next or len(rows) == 0:
                break

            # In Growatt UI this is usually 80
            start += len(rows)
            time.sleep(0.10)

        if not all_rows:
            if debug:
                _log(f"⚠️  [GrowattWeb] No rows for plant={plant_id} day={d}")
            continue

        kwh_m2 = _compute_daily_kwh_m2_from_radiant(all_rows, debug=debug, tag=f"plant={plant_id} day={d}")
        # if still 0, we still accept (could be night-only data). but try previous day if today.
        if kwh_m2 > 0:
            return kwh_m2

        if debug:
            # show one sample row with radiant presence
            sample = all_rows[0]
            _log(f"🧪 [GrowattWeb] sample row keys={list(sample.keys())[:12]} radiant={sample.get('radiant')} etodayRadiation={sample.get('etodayRadiation')}")

    if debug:
        _log(f"❌ [GrowattWeb] No usable irradiance computed for plant={plant_id} try_dates={try_dates}")
    return 0.0


# -----------------------------
# Cloud cover via Open-Meteo (as before)
# -----------------------------
def _open_meteo_cloudcover_mean(lat: float, lon: float, date_iso: str, debug: bool = False) -> float:
    url = "https://archive-api.open-meteo.com/v1/archive"
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
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 200:
                js = r.json()
                cc = (js.get("daily", {}).get("cloudcover_mean") or [0])[0]
                return round(float(cc), 1)
            if debug:
                _log(f"🌥️  [OpenMeteo] http={r.status_code} body={r.text[:120]}")
        except Exception as e:
            if debug:
                _log(f"🌥️  [OpenMeteo] error attempt={attempt+1}: {e}")
            time.sleep(3)
    return 0.0


# -----------------------------
# Main function used by argia.py
# -----------------------------
def get_weather_for_date(p_key: str, date_iso: str, plants_config: dict) -> Tuple[float, float]:
    """
    Returns: (irradiance_kwh_m2, cloudcover_mean_pct)
    - irradiance: from Growatt ENV history radiant integration (kWh/m²)
    - clouds: Open-Meteo (mean %)
    """
    conf = plants_config.get(p_key, {})
    lat, lon = conf.get("lat"), conf.get("lon")
    if not lat or not lon:
        return 0.0, 0.0

    debug = _env_bool("ARGIA_DEBUG_GROWATT_WEATHER", False)

    brand = str(conf.get("brand", "")).upper()
    site_id = str(conf.get("site_id", "")).strip()

    # For Huawei plants: use special Growatt plant as irradiance source if provided
    # You said: Huawei -> irradiance from SMS plant 10069072 with ENV DYD1EZR007 addr 32
    irr_source_plant = conf.get("irr_source_plant")  # optional in config sheet
    if irr_source_plant:
        irr_plant_id = str(irr_source_plant).strip()
    else:
        # if this plant is Growatt -> use its own site_id
        # if Huawei -> default to SMS 10069072 (your requirement)
        irr_plant_id = site_id if brand == "GROWATT" else "10069072"

    # optional hints (if you later add them to Config sheet)
    preferred_sn = conf.get("weather_station_sn")  # e.g. DYD1EZR007
    preferred_addr = conf.get("weather_station_addr")  # e.g. 32
    try:
        preferred_addr_int = int(preferred_addr) if preferred_addr not in (None, "") else None
    except Exception:
        preferred_addr_int = None

    irr = 0.0
    try:
        irr = get_growatt_irradiance_kwh_m2(
            plant_id=irr_plant_id,
            day_iso=date_iso,
            preferred_sn=str(preferred_sn).strip() if preferred_sn else None,
            preferred_addr=preferred_addr_int,
            fallback_days=_env_int("GROWATT_FALLBACK_DAYS", 2),
            debug=debug,
        )
    except Exception as e:
        if debug:
            _log(f"❌ [GrowattWeb] irradiance fetch failed for plant={irr_plant_id}: {e}")
        irr = 0.0

    clouds = _open_meteo_cloudcover_mean(float(lat), float(lon), date_iso, debug=debug)

    if debug:
        _log(f"📌 [Weather] p_key={p_key} brand={brand} irr_source_plant={irr_plant_id} date={date_iso} -> irr={irr} kWh/m2 clouds={clouds}")

    return irr, clouds
