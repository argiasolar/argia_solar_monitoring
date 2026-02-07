#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA – Unified Inverter + Meteo Sync (10-min)
----------------------------------------------
Reads from Google Sheets:
- SNAP (Plant_Key, SITEID, INVERTER1..4, DATALOGGER, BRAND)
- Config_Plants (Plantkey, Brand, Latitude, Longtitude, WeatherStation, Addr, Growatt_SiteID, etc.)

Fetches:
- Growatt: inverter snapshot per plant (SNAP-aware endpoint selection so returned SNs match SNAP)
- Huawei: inverter real KPI by SNS (stable method) via FusionSolar thirdData getDevRealKpi
- Meteo:
    - Irradiance interval kWh/m² from Growatt ENV "radiant" using argia_meteo.get_meteo_snapshot()
    - Cloud cover fraction 0..1 via argia_meteo.get_cloud_fraction() (inside get_meteo_snapshot)

Writes to Google Sheets tab UNIFIED_TAB (default InverterUnified10m):
ExtractedAtUTC, UpdateTime, SiteId, DeviceType, DeviceSN, Status, EToday_kWh, Irradiance_kWh_m2, Cloud_Coverage

Notes:
- Cloud_Coverage is 0..1 fraction (not %).
- UpdateTime is Mexico City local time string (America/Mexico_City).
- Huawei is done by SNS because it worked reliably in your previous version.
- Irradiance is interval energy (kWh/m² per INTERVAL_MINUTES), not W/m².

Env required:
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS (service account JSON as TEXT)

Growatt (web):
- GROWATT_USERNAME / GROWATT_PASSWORD
- GROWATT_WEB_BASE (default https://server.growatt.com)

Huawei:
- HUAWEI_USERNAME / HUAWEI_PASSWORD
- HUAWEI_BASE_URL (default https://la5.fusionsolar.huawei.com/thirdData)
- HUAWEI_INVERTER_DEVTYPE (default "1")

Meteo (used by argia_meteo.py):
- GROWATT_USERNAME / GROWATT_PASSWORD / GROWATT_WEB_BASE
- OPENWEATHER_API_KEY (optional)
- OPEN_METEO_TIMEOUT / OPEN_METEO_RETRIES / OPEN_METEO_BACKOFF_SEC
- GROWATT_ENV_MAX_PAGES

Ranges/tabs:
- UNIFIED_TAB (default InverterUnified10m)
- SNAP_RANGE (default SNAP!A1:Z1000)
- CONFIG_RANGE (default Config_Plants!A1:Z1000)

Tuning:
- PAGE_SIZE (default 50)
- MAX_PAGES (default 6)
- INTERVAL_MINUTES (default 10)
- LOG_LEVEL (default INFO)
"""

from __future__ import annotations

import os
import re
import json
import time
import math
import logging
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

import argia_meteo as meteo  # uses Growatt ENV + OpenMeteo/OpenWeather internally


# ============================
# Logging / TZ
# ============================

LOG = logging.getLogger("argia.sync")


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def tz_mx() -> dt.tzinfo:
    if ZoneInfo:
        try:
            return ZoneInfo("America/Mexico_City")
        except Exception:
            pass
    return dt.timezone(dt.timedelta(hours=-6))


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def now_mx() -> dt.datetime:
    return now_utc().astimezone(tz_mx())


def fmt_mx(ts: dt.datetime) -> str:
    return ts.astimezone(tz_mx()).strftime("%Y-%m-%d %H:%M:%S")


# ============================
# Helpers
# ============================

def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def env_int(name: str, default: int) -> int:
    try:
        return int(str(env(name, str(default))).strip())
    except Exception:
        return default


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        s = str(x).strip()
        if s == "":
            return default
        s = s.replace(",", ".")
        v = float(s)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def normalize_sn(x: Any) -> str:
    s = "" if x is None else str(x).strip()
    s = re.sub(r"\s+", "", s).upper()
    return s


def looks_like_growatt_siteid(s: str) -> bool:
    return bool(re.fullmatch(r"\d{6,12}", s or ""))


def looks_like_huawei_station(s: str) -> bool:
    return (s or "").startswith("NE=")


def parse_vendor_time_to_mx(s: Any) -> Optional[str]:
    """
    Best-effort parser for vendor update time to MX string.
    Accepts:
    - "YYYY-MM-DD HH:MM:SS"
    - "YYYY/MM/DD HH:MM:SS"
    - epoch seconds / ms
    Otherwise returns None.
    """
    if s is None:
        return None
    st = str(s).strip()
    if not st:
        return None

    # epoch?
    if re.fullmatch(r"\d{10,13}", st):
        try:
            n = int(st)
            if len(st) == 13:
                ts = dt.datetime.fromtimestamp(n / 1000.0, tz=dt.timezone.utc)
            else:
                ts = dt.datetime.fromtimestamp(n, tz=dt.timezone.utc)
            return fmt_mx(ts)
        except Exception:
            return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            naive = dt.datetime.strptime(st, fmt)
            local = naive.replace(tzinfo=tz_mx())
            return fmt_mx(local)
        except Exception:
            continue

    return None


# ============================
# Google Sheets
# ============================

def load_google_creds() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS secret (service account JSON as text).")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def sheets_service():
    return build("sheets", "v4", credentials=load_google_creds(), cache_discovery=False)


def sheet_create_if_missing(sheet_id: str, tab_name: str) -> None:
    svc = sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets = meta.get("sheets", []) or []
    for sh in sheets:
        props = sh.get("properties") or {}
        if str(props.get("title", "")).strip() == tab_name:
            return

    LOG.info("Tab '%s' missing -> creating it", tab_name)
    req = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=req).execute()


def values_get(sheet_id: str, rng: str) -> List[List[Any]]:
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=rng,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    return resp.get("values", []) or []


def values_append(sheet_id: str, tab: str, rows: List[List[Any]]) -> None:
    if not rows:
        return
    svc = sheets_service()
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def ensure_header(sheet_id: str, tab: str) -> None:
    header = [
        "ExtractedAtUTC",
        "UpdateTime",
        "SiteId",
        "DeviceType",
        "DeviceSN",
        "Status",
        "EToday_kWh",
        "Irradiance_kWh_m2",
        "Cloud_Coverage",
    ]
    sheet_create_if_missing(sheet_id, tab)

    svc = sheets_service()
    rng = f"{tab}!A1:I1"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    existing = (resp.get("values") or [[]])[0] if resp else []
    if not existing or len(existing) == 0:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
        LOG.info("Ensured header on tab '%s'", tab)


# ============================
# SNAP + Config parsing
# ============================

def header_index_map(header_row: List[Any]) -> Dict[str, int]:
    m: Dict[str, int] = {}
    for i, h in enumerate(header_row or []):
        k = str(h).strip().lower()
        if k:
            m[k] = i
    return m


def read_snap(sheet_id: str, snap_range: str) -> List[Dict[str, Any]]:
    values = values_get(sheet_id, snap_range)
    if not values:
        return []

    header = values[0]
    idx = header_index_map(header)

    def find_col(*names: str) -> Optional[int]:
        for n in names:
            if n.lower() in idx:
                return idx[n.lower()]
        return None

    i_pk = find_col("plant_key", "plantkey", "plant key")
    i_site = find_col("siteid", "site_id", "site id")
    i_brand = find_col("brand")
    i_logger = idx.get("datalogger")

    if i_pk is None or i_site is None or i_brand is None:
        raise RuntimeError(f"SNAP missing Plant_Key/SITEID/BRAND columns. Header={header}")

    # inverter columns
    inverter_cols: List[int] = []
    for cand in ("inverter1", "inverter2", "inverter3", "inverter4", "iverter2"):  # tolerate old typo
        if cand in idx:
            inverter_cols.append(idx[cand])
    for k, i in idx.items():
        if k.startswith("inverter") and i not in inverter_cols:
            inverter_cols.append(i)
    inverter_cols = sorted(list(dict.fromkeys(inverter_cols)))

    out: List[Dict[str, Any]] = []
    for r in values[1:]:
        plant_key = str(r[i_pk]).strip() if i_pk < len(r) else ""
        siteid = str(r[i_site]).strip() if i_site < len(r) else ""
        brand = str(r[i_brand]).strip().upper() if i_brand < len(r) else ""
        if not plant_key or not siteid or not brand:
            continue

        sns: List[str] = []
        for ci in inverter_cols:
            if ci < len(r):
                sn = normalize_sn(r[ci])
                if sn:
                    sns.append(sn)
        sns = sorted(list(dict.fromkeys(sns)))

        datalogger = str(r[i_logger]).strip() if (i_logger is not None and i_logger < len(r)) else ""

        out.append({
            "plant_key": plant_key,
            "siteid": siteid,
            "brand": brand,
            "sns": sns,
            "datalogger": datalogger,
        })

    return out


def read_config_plants(sheet_id: str, config_range: str) -> Dict[str, Dict[str, Any]]:
    values = values_get(sheet_id, config_range)
    if not values:
        return {}

    header = values[0]
    idx = header_index_map(header)

    def find_col(*names: str) -> Optional[int]:
        for n in names:
            if n.lower() in idx:
                return idx[n.lower()]
        return None

    i_pk = find_col("plantkey", "plant_key", "plant key")
    i_lat = find_col("latitude", "lat")
    i_lon = find_col("longtitude", "longitude", "lon")
    i_siteid = find_col("siteid", "site_id", "site id")
    i_ws = find_col("weatherstation", "weather_station", "weather station")
    i_addr = find_col("addr", "address")
    i_growatt_siteid = find_col("growatt_siteid", "growatt siteid", "growatt_site_id")

    out: Dict[str, Dict[str, Any]] = {}

    for r in values[1:]:
        pk = str(r[i_pk]).strip() if (i_pk is not None and i_pk < len(r)) else ""
        if not pk:
            continue

        lat = safe_float(r[i_lat], 0.0) if (i_lat is not None and i_lat < len(r)) else 0.0
        lon = safe_float(r[i_lon], 0.0) if (i_lon is not None and i_lon < len(r)) else 0.0
        siteid = str(r[i_siteid]).strip() if (i_siteid is not None and i_siteid < len(r)) else ""
        ws = str(r[i_ws]).strip() if (i_ws is not None and i_ws < len(r)) else ""
        addr = int(safe_float(r[i_addr], 0.0)) if (i_addr is not None and i_addr < len(r)) else 0
        growatt_siteid = str(r[i_growatt_siteid]).strip() if (i_growatt_siteid is not None and i_growatt_siteid < len(r)) else ""

        out[pk] = {
            "plant_key": pk,
            "lat": lat,
            "lon": lon,
            "siteid": siteid,
            "weather_station": ws,
            "addr": addr,
            "growatt_siteid": growatt_siteid,
        }
        if siteid:
            out[f"SITE::{siteid}"] = out[pk]

    return out


# ============================
# Meteo cache (interval kWh/m² + cloud 0..1)
# ============================

_METEO_CACHE: Dict[Tuple[str, str, int, int], Tuple[float, float]] = {}


def meteo_for_site(
    plant_key: str,
    siteid: str,
    plant_conf: Dict[str, Any],
    when_utc: dt.datetime,
    interval_minutes: int,
) -> Tuple[float, float]:
    """
    Returns (irradiance_kwh_m2_interval, cloud_fraction_0_1)

    Weather plant id:
    - Growatt plants: siteid is numeric => use siteid
    - Huawei plants: siteid is NE=... => use Config_Plants.Growatt_SiteID (numeric) to query Growatt ENV
    """
    lat = safe_float(plant_conf.get("lat"), 0.0)
    lon = safe_float(plant_conf.get("lon"), 0.0)
    ws = str(plant_conf.get("weather_station") or "").strip()
    addr = int(safe_float(plant_conf.get("addr"), 0.0))
    growatt_site = str(plant_conf.get("growatt_siteid") or "").strip()

    weather_plant_id = siteid if looks_like_growatt_siteid(siteid) else growatt_site
    cache_key = (str(weather_plant_id), str(ws), int(addr), int(interval_minutes))
    if cache_key in _METEO_CACHE:
        return _METEO_CACHE[cache_key]

    # If not enough info => zeros
    if not weather_plant_id or not looks_like_growatt_siteid(weather_plant_id) or not ws or not addr or not lat or not lon:
        _METEO_CACHE[cache_key] = (0.0, 0.0)
        return _METEO_CACHE[cache_key]

    irr, cloud = meteo.get_meteo_snapshot(
        plant_id_for_weather=str(weather_plant_id),
        lat=float(lat),
        lon=float(lon),
        weather_sn=str(ws),
        addr=int(addr),
        when_utc=when_utc,
        interval_minutes=int(interval_minutes),
    )
    # argia_meteo already returns kWh/m2 for interval and cloud fraction
    _METEO_CACHE[cache_key] = (float(irr), float(cloud))
    return _METEO_CACHE[cache_key]


# ============================
# Growatt inverter fetching (SNAP-aware)
# ============================

@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    BASE = "https://server.growatt.com"

    UNSAFE_PREFIXES = ("/commonDeviceSetC/",)
    UNSAFE_CONTAINS = ("setmax", "settlx", "setinverter", "delmax", "deltlx", "delinverter", "delete", "set", "save")

    def __init__(self, auth: GrowattAuth, timeout: int = 45):
        self.auth = auth
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (ARGIA Growatt Bot)", "Accept": "*/*"})

    def get(self, path: str, params: Optional[dict] = None, referer: Optional[str] = None) -> requests.Response:
        url = self.BASE + path
        headers = {}
        if referer:
            headers["Referer"] = referer
        return self.s.get(url, params=params, headers=headers, timeout=self.timeout, allow_redirects=True)

    def post(self, path: str, data: Optional[dict] = None, referer: Optional[str] = None) -> requests.Response:
        url = self.BASE + path
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if referer:
            headers["Referer"] = referer
        return self.s.post(url, data=data or {}, headers=headers, timeout=self.timeout, allow_redirects=True)

    def login(self) -> None:
        r1 = self.get("/login")
        LOG.info("GET /login -> %s", r1.status_code)

        payload = {"account": self.auth.user, "password": self.auth.password}
        r2 = self.post("/login", data=payload, referer=self.BASE + "/login")
        LOG.info("POST /login -> %s (len=%s)", r2.status_code, len(r2.text or ""))

        cookies = self.s.cookies.get_dict()
        if "assToken" not in cookies:
            raise RuntimeError("Login failed: assToken cookie missing")

        LOG.info("✅ Login OK (assToken present). Cookies: %s", " | ".join(sorted(list(cookies.keys()))))

    def warm_plant_context(self, plant_id: str) -> None:
        # Warm a bit + set cookies like UI does
        self.get("/device")
        self.get("/device/photovoltaic", params={"plantId": plant_id}, referer=self.BASE + "/device")
        self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")
        self.s.cookies.set("selPage", "%2Fpanel", domain="server.growatt.com", path="/")

    @staticmethod
    def discover_ajax_urls(html: str) -> List[str]:
        urls: List[str] = []
        for m in re.finditer(r"url\s*:\s*['\"](\/[^'\"]+)['\"]", html):
            urls.append(m.group(1))
        for m in re.finditer(r"\$\.(?:post|get)\(\s*['\"](\/[^'\"]+)['\"]", html):
            urls.append(m.group(1))
        seen = set()
        out = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _is_safe_endpoint(self, endpoint: str) -> bool:
        ep = endpoint.lower()
        if any(ep.startswith(p) for p in self.UNSAFE_PREFIXES):
            return False
        if any(bad in ep for bad in self.UNSAFE_CONTAINS):
            return False
        if "list" not in ep:
            return False
        return True

    @staticmethod
    def _extract_items(data: dict) -> List[dict]:
        items = data.get("datas")
        if items is None:
            items = data.get("data")
        if items is None:
            items = data.get("rows")
        if items is None:
            items = []
        if not isinstance(items, list):
            return []
        return [x for x in items if isinstance(x, dict)]

    @staticmethod
    def _sn_from_item(it: Dict[str, Any]) -> str:
        for k in ("sn", "deviceSn", "invSn", "serialNum", "serialNo"):
            if k in it and it[k] not in (None, "", "null"):
                return normalize_sn(it[k])
        return ""

    def _call_json(self, endpoint: str, payload: dict) -> Optional[dict]:
        r = self.post(endpoint, data=payload, referer=self.BASE + "/index")
        try:
            return r.json()
        except Exception:
            pass
        r2 = self.get(endpoint, params=payload, referer=self.BASE + "/index")
        try:
            return r2.json()
        except Exception:
            return None

    def fetch_devices_matching_snap(self, plant_id: str, snap_sns: List[str], page_size: int, max_pages: int) -> List[Dict[str, Any]]:
        """
        SNAP-aware endpoint probing: accept endpoint only if it yields SNs that match SNAP.
        """
        wanted = {normalize_sn(x) for x in (snap_sns or []) if x}

        html_max = self.get("/device/getMAXPage", params={"ttt": str(int(time.time() * 1000))}, referer=self.BASE + "/index").text or ""
        html_inv = self.get("/device/getInverterPage", params={"plantId": str(plant_id)}, referer=self.BASE + "/device").text or ""

        urls = self.discover_ajax_urls(html_max) + self.discover_ajax_urls(html_inv)
        urls += [
            "/device/getMAXList",
            "/device/getMaxList",
            "/device/getInverterList",
            "/device/getInverterListData",
            "/device/getDeviceList",
            "/device/getPlantDeviceList",
            "/panel/getDeviceList",
            "/panel/getPlantDeviceList",
            "/device/getDatalogList",
        ]

        seen = set()
        candidates = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                candidates.append(u)
        safe_candidates = [u for u in candidates if self._is_safe_endpoint(u)]
        logging.getLogger("argia.growatt.inverters").info(
            "Found %d safe list candidates for plant %s", len(safe_candidates), plant_id
        )

        payload_variants = [
            {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size), "ind": "1"},
            {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size)},
            {"plantId": str(plant_id), "pageSize": str(page_size), "currPage": "1"},
            {"currPage": "1", "pageSize": str(page_size)},
        ]

        best_items: List[Dict[str, Any]] = []
        best_hits = -1
        best_ep = None

        for ep in safe_candidates:
            all_items: List[Dict[str, Any]] = []
            for page in range(1, max_pages + 1):
                page_items: List[Dict[str, Any]] = []
                for base in payload_variants:
                    payload = dict(base)
                    payload["currPage"] = str(page)
                    payload["pageSize"] = str(page_size)
                    data = self._call_json(ep, payload)
                    if not data:
                        continue
                    items = self._extract_items(data)
                    if items:
                        page_items = items
                        break

                if not page_items:
                    break

                all_items.extend(page_items)
                if len(page_items) < page_size:
                    break

            returned_sns = [self._sn_from_item(it) for it in all_items]
            returned_sns = [sn for sn in returned_sns if sn]
            hits = sum(1 for sn in returned_sns if sn in wanted) if wanted else (1 if returned_sns else 0)

            if hits > best_hits:
                best_hits = hits
                best_items = all_items
                best_ep = ep

            if hits >= 1:
                logging.getLogger("argia.growatt.inverters").info(
                    "✅ Using endpoint %s (items=%d hits=%d) for plant %s", ep, len(all_items), hits, plant_id
                )
                return all_items

        logging.getLogger("argia.growatt.inverters").warning(
            "❌ No endpoint matched SNAP SNs for plant %s. Best=%s hits=%s items=%s",
            plant_id,
            best_ep,
            best_hits,
            len(best_items),
        )
        return best_items


def growatt_status_1_or_3(item: Dict[str, Any]) -> int:
    if "lost" in item and safe_float(item.get("lost"), 0) >= 1:
        return 3

    for k in ("status", "deviceStatus", "invStatus", "workStatus", "connStatus"):
        if k in item and item[k] not in (None, "", "null"):
            try:
                v = int(safe_float(item[k], 0))
                if v in (1, 3):
                    return v
                if v == 0:
                    return 3
                return 1
            except Exception:
                s = str(item[k]).strip().lower()
                if s in ("online", "normal", "connected", "run", "running"):
                    return 1
                if s in ("offline", "disconnected", "lost", "fault"):
                    return 3

    pac = safe_float(item.get("pac") or item.get("power") or item.get("actPower") or 0, 0.0)
    if pac > 1:
        return 1
    return 1


def growatt_extract_etoday(item: Dict[str, Any]) -> float:
    for k in ("eToday", "EToday", "todayEnergy", "generationToday"):
        if k in item:
            v = safe_float(item.get(k), 0.0)
            if v > 0:
                return v
    if isinstance(item.get("dataItemMap"), dict):
        m = item["dataItemMap"]
        for kk in ("eToday", "day_cap", "daily_cap"):
            if kk in m:
                v = safe_float(m.get(kk), 0.0)
                if v > 0:
                    return v
    return 0.0


def growatt_extract_updatetime_mx(item: Dict[str, Any]) -> str:
    for k in ("updateTime", "lastUpdateTime", "time"):
        parsed = parse_vendor_time_to_mx(item.get(k))
        if parsed:
            return parsed
    return fmt_mx(now_mx())


def growatt_device_type(item: Dict[str, Any]) -> str:
    for k in ("deviceType", "deviceTypeNum", "type", "deviceTypeName", "model"):
        if k in item and item[k] not in (None, "", "null"):
            return str(item[k]).strip()
    return "GROWATT_INV"


# ============================
# Huawei (stable: getDevRealKpi by SNS)
# ============================

class HuaweiClient:
    def __init__(self, base: str, username: str, password: str, timeout: int = 30):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def login(self) -> None:
        r = self.s.post(
            f"{self.base}/login",
            json={"userName": self.username, "systemCode": self.password},
            timeout=self.timeout,
        )
        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        if not token:
            raise RuntimeError("Huawei login failed: missing XSRF-TOKEN")
        self.s.headers.update({"XSRF-TOKEN": token})
        LOG.info("✅ Huawei login OK")

    def get_dev_real_kpi_by_sns(self, dev_type_id: str, sns: List[str]) -> List[Dict[str, Any]]:
        sns_clean = [normalize_sn(x) for x in (sns or []) if normalize_sn(x)]
        if not sns_clean:
            return []
        body = {"devTypeId": str(dev_type_id), "sns": ",".join(sns_clean)}
        r = self.s.post(f"{self.base}/getDevRealKpi", json=body, timeout=60)
        try:
            js = r.json()
        except Exception:
            raise RuntimeError(f"getDevRealKpi non-JSON response: http={r.status_code} body={r.text[:300]}")
        if not js.get("success"):
            raise RuntimeError(
                f"getDevRealKpi failed: http={r.status_code} failCode={js.get('failCode')} message={js.get('message')}"
            )
        data = js.get("data") or []
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]


def huawei_find_sn_field(dev: Dict[str, Any]) -> str:
    for k in ("sn", "devSn", "deviceSn", "esnCode", "serialNum", "serialNo"):
        if k in dev and dev[k] not in (None, "", "null"):
            sn = normalize_sn(dev[k])
            if sn:
                return sn
    return ""


def huawei_status_1_or_3(kpi: Dict[str, Any]) -> int:
    for k in ("devStatus", "status", "onlineStatus", "runningStatus", "workStatus"):
        if k in kpi and kpi[k] not in (None, "", "null"):
            try:
                v = int(safe_float(kpi[k], 0))
                if v in (1, 3):
                    return v
                if v == 0:
                    return 3
                return 1
            except Exception:
                s = str(kpi[k]).strip().lower()
                if s in ("online", "normal", "running"):
                    return 1
                if s in ("offline", "lost", "fault"):
                    return 3
    return 1


def huawei_extract_etoday(kpi: Dict[str, Any]) -> float:
    m = kpi.get("dataItemMap") or {}
    if isinstance(m, dict):
        for kk in ("day_cap", "daily_cap", "day_power", "eToday", "todayEnergy"):
            if kk in m:
                v = safe_float(m.get(kk), 0.0)
                if v > 0:
                    return v
    for kk in ("day_cap", "daily_cap", "day_power", "eToday", "todayEnergy"):
        if kk in kpi:
            v = safe_float(kpi.get(kk), 0.0)
            if v > 0:
                return v
    return 0.0


def huawei_extract_updatetime_mx(kpi: Dict[str, Any]) -> str:
    for k in ("collectTime", "updateTime", "time"):
        parsed = parse_vendor_time_to_mx(kpi.get(k))
        if parsed:
            return parsed
    return fmt_mx(now_mx())


def huawei_device_type(kpi: Dict[str, Any], dev_type_id: str) -> str:
    # keep something meaningful; if Huawei returns type name/id use it, else dev_type_id
    for k in ("devTypeId", "deviceType", "typeId", "devTypeName"):
        if k in kpi and kpi[k] not in (None, "", "null"):
            return str(kpi[k]).strip()
    return str(dev_type_id)


def chunked(xs: List[str], n: int) -> List[List[str]]:
    return [xs[i:i + n] for i in range(0, len(xs), n)]


# ============================
# Main orchestration
# ============================

def main() -> None:
    setup_logging()

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    unified_tab = env("UNIFIED_TAB", "InverterUnified10m") or "InverterUnified10m"
    snap_range = env("SNAP_RANGE", "SNAP!A1:Z1000") or "SNAP!A1:Z1000"
    config_range = env("CONFIG_RANGE", "Config_Plants!A1:Z1000") or "Config_Plants!A1:Z1000"

    interval_min = env_int("INTERVAL_MINUTES", 10)
    when = now_utc()

    ensure_header(sheet_id, unified_tab)

    plants_cfg = read_config_plants(sheet_id, config_range)
    snap_rows = read_snap(sheet_id, snap_range)
    LOG.info("SNAP rows=%d", len(snap_rows))
    if not snap_rows:
        LOG.warning("No SNAP rows found, nothing to do.")
        return

    extracted_at = when.isoformat()

    growatt_snap = [r for r in snap_rows if r["brand"] == "GROWATT"]
    huawei_snap = [r for r in snap_rows if r["brand"] == "HUAWEI"]

    out_rows: List[List[Any]] = []

    # --- Growatt ---
    g_user = env("GROWATT_USERNAME") or env("GROWATT_USER") or ""
    g_pass = env("GROWATT_PASSWORD") or env("GROWATT_PASS") or ""

    if growatt_snap and g_user and g_pass:
        gcli = GrowattMonitoringClient(GrowattAuth(g_user, g_pass))
        gcli.login()

        page_size = env_int("PAGE_SIZE", 50)
        max_pages = env_int("MAX_PAGES", 6)

        for srow in growatt_snap:
            siteid = srow["siteid"]
            plant_key = srow["plant_key"]
            wanted_sns = [normalize_sn(x) for x in (srow["sns"] or []) if normalize_sn(x)]

            if not looks_like_growatt_siteid(siteid):
                LOG.warning("Growatt row has non-numeric SITEID=%s plant=%s -> skipping inverter fetch", siteid, plant_key)
                continue

            conf = plants_cfg.get(f"SITE::{siteid}") or plants_cfg.get(plant_key) or {}
            irr, cloud = meteo_for_site(plant_key, siteid, conf, when_utc=when, interval_minutes=interval_min)

            gcli.warm_plant_context(siteid)
            items = gcli.fetch_devices_matching_snap(siteid, wanted_sns, page_size=page_size, max_pages=max_pages)

            fetched: Dict[str, Dict[str, Any]] = {}
            for it in items:
                sn = GrowattMonitoringClient._sn_from_item(it)
                if sn:
                    fetched[sn] = it

            if wanted_sns:
                hits = sum(1 for sn in wanted_sns if sn in fetched)
                if hits == 0:
                    LOG.warning("Growatt plant %s: 0/%d SNAP SNs matched device list (check SNAP SNs).", siteid, len(wanted_sns))
                    if items:
                        LOG.info("Growatt plant %s sample keys: %s", siteid, list(items[0].keys())[:30])

            for sn in wanted_sns:
                it = fetched.get(sn, {})
                status = growatt_status_1_or_3(it) if it else 3
                etoday = growatt_extract_etoday(it) if it else 0.0
                upd = growatt_extract_updatetime_mx(it) if it else fmt_mx(now_mx())
                dtype = growatt_device_type(it) if it else "GROWATT_INV"

                out_rows.append([
                    extracted_at,
                    upd,
                    siteid,
                    dtype,
                    sn,
                    status,
                    round(float(etoday), 3),
                    round(float(irr), 5),
                    round(float(cloud), 4),
                ])

            time.sleep(0.25)
    else:
        if growatt_snap:
            LOG.warning("Missing Growatt creds; skipping Growatt.")
        else:
            LOG.info("No Growatt rows in SNAP.")

    # --- Huawei (SNS-based) ---
    h_user = env("HUAWEI_USERNAME") or ""
    h_pass = env("HUAWEI_PASSWORD") or ""
    h_base = (env("HUAWEI_BASE_URL", "https://la5.fusionsolar.huawei.com/thirdData") or "").rstrip("/")
    h_devtype = env("HUAWEI_INVERTER_DEVTYPE", "1") or "1"

    if huawei_snap and h_user and h_pass:
        hcli = HuaweiClient(h_base, h_user, h_pass)
        hcli.login()

        LOG.info("Huawei stations in SNAP: %s", ", ".join(sorted({r["siteid"] for r in huawei_snap})))

        for srow in huawei_snap:
            siteid = srow["siteid"]
            plant_key = srow["plant_key"]
            wanted_sns = [normalize_sn(x) for x in (srow["sns"] or []) if normalize_sn(x)]

            if not looks_like_huawei_station(siteid):
                LOG.warning("Huawei row has SITEID not starting with NE=: %s plant=%s -> skipping", siteid, plant_key)
                continue

            conf = plants_cfg.get(f"SITE::{siteid}") or plants_cfg.get(plant_key) or {}
            irr, cloud = meteo_for_site(plant_key, siteid, conf, when_utc=when, interval_minutes=interval_min)

            fetched: Dict[str, Dict[str, Any]] = {}
            # Huawei: commonly safe to do ~50 SNS per call (your previous version used 50)
            for batch in chunked(wanted_sns, 50):
                items = hcli.get_dev_real_kpi_by_sns(h_devtype, batch)
                for it in items:
                    sn = huawei_find_sn_field(it)
                    if sn:
                        fetched[sn] = it
                time.sleep(0.2)

            for sn in wanted_sns:
                kpi = fetched.get(sn, {})
                if not kpi:
                    LOG.warning("Huawei station %s: SNAP inverter %s not returned by getDevRealKpi.", siteid, sn)

                status = huawei_status_1_or_3(kpi) if kpi else 1
                etoday = huawei_extract_etoday(kpi) if kpi else 0.0
                upd = huawei_extract_updatetime_mx(kpi) if kpi else fmt_mx(now_mx())
                dtype = huawei_device_type(kpi, h_devtype) if kpi else str(h_devtype)

                out_rows.append([
                    extracted_at,
                    upd,
                    siteid,
                    dtype,
                    sn,
                    status,
                    round(float(etoday), 3),
                    round(float(irr), 5),
                    round(float(cloud), 4),
                ])

            time.sleep(0.2)
    else:
        if huawei_snap:
            LOG.warning("Missing Huawei creds; skipping Huawei.")
        else:
            LOG.info("No Huawei rows in SNAP.")

    # Write
    values_append(sheet_id, unified_tab, out_rows)
    LOG.info("✅ Unified sync written rows=%d tab=%s", len(out_rows), unified_tab)


if __name__ == "__main__":
    main()
