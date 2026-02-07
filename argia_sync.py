#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA – Unified Sync (Growatt + Huawei + Meteo) 10-min
-----------------------------------------------------

Writes to Google Sheets tab UNIFIED_TAB (default: InverterUnified10m) with columns:
ExtractedAtUTC | UpdateTime | SiteId | DeviceType | DeviceSN | Status | EToday_kWh | Irradiance_kWh_m2 | Cloud_Coverage

Fix (Growatt):
- Probe multiple safe list endpoints but SELECT the BEST endpoint based on:
  - How many SNAP SNs are returned as inverters (deviceType==4), and/or
  - How many SNAP SNs have non-empty EToday,
  not merely "SN hits".
- When duplicate SN appears in multiple rows, pick the best row (prefer inverter/type=4 and/or higher eToday).

Notes:
- Cloud_Coverage written as 0..1 fraction (your sheet may format it as %).
- UpdateTime is Mexico City local time string.
"""

from __future__ import annotations

import os
import re
import json
import time
import math
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import argia_meteo as meteo

LOG = logging.getLogger("argia.sync")
GLOG = logging.getLogger("argia.growatt.inverters")
MX_TZ = ZoneInfo("America/Mexico_City")


# ----------------------------
# Logging
# ----------------------------
def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_utc_iso() -> str:
    return now_utc().isoformat()


def utc_to_mx_str(dt_utc: datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S")


def now_mx_str() -> str:
    return utc_to_mx_str(now_utc())


# ----------------------------
# Helpers
# ----------------------------
def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()


def normalize_sn(x: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(x)).upper()


def safe_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            s = x.strip()
            if s == "":
                return default
            s = s.replace(",", "")
            x = s
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def looks_like_growatt_siteid(s: str) -> bool:
    return bool(re.fullmatch(r"\d{6,12}", s or ""))


def looks_like_huawei_station(s: str) -> bool:
    return (s or "").startswith("NE=")


def qrange(tab: str, a1: str) -> str:
    return f"'{tab}'!{a1}"


def pick(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return None


def try_parse_json(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except Exception:
        return None


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_update_time_to_mx(val: Any) -> str:
    s = normalize_text(val)
    if not s:
        return now_mx_str()

    if re.fullmatch(r"\d{10,13}", s):
        try:
            n = int(s)
            if len(s) == 13:
                dt_utc = datetime.fromtimestamp(n / 1000.0, tz=timezone.utc)
            else:
                dt_utc = datetime.fromtimestamp(n, tz=timezone.utc)
            return utc_to_mx_str(dt_utc)
        except Exception:
            return now_mx_str()

    # already formatted; assume local-ish
    return s


# ----------------------------
# Google Sheets
# ----------------------------
def load_google_creds() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def sheets_service():
    return build("sheets", "v4", credentials=load_google_creds(), cache_discovery=False)


def ensure_sheet_exists(sheet_id: str, tab: str) -> None:
    svc = sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    titles = {s.get("properties", {}).get("title") for s in (meta.get("sheets") or [])}
    if tab in titles:
        return
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
    ).execute()
    LOG.info("Created missing tab '%s'", tab)


def ensure_header(sheet_id: str, tab: str) -> None:
    ensure_sheet_exists(sheet_id, tab)
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
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=qrange(tab, "A1:I1"),
    ).execute()
    existing = (resp.get("values") or [[]])[0]
    if not existing:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=qrange(tab, "A1"),
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()


def append_rows(sheet_id: str, tab: str, rows: List[List[Any]]) -> None:
    if not rows:
        return
    sheets_service().spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=qrange(tab, "A1"),
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def read_table(sheet_id: str, rng: str) -> List[List[Any]]:
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=rng,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    return resp.get("values", []) or []


# ----------------------------
# Config + SNAP
# ----------------------------
def read_config_plants(sheet_id: str, config_range: str) -> Dict[str, Dict[str, Any]]:
    values = read_table(sheet_id, config_range)
    if not values:
        return {}
    header = [normalize_text(h).upper() for h in values[0]]
    rows = values[1:]

    def idx(*names: str) -> Optional[int]:
        for n in names:
            n2 = n.upper()
            if n2 in header:
                return header.index(n2)
        return None

    i_plant = idx("PLANTKEY", "PLANT_KEY")
    i_site = idx("SITEID", "SITE_ID")
    i_lat = idx("LATITUDE", "LAT")
    i_lon = idx("LONGTITUDE", "LONGITUDE", "LON")
    i_ws = idx("WEATHERSTATION", "WEATHER STATION")
    i_addr = idx("ADDR", "ADDRESS")
    i_growatt_site = idx("GROWATT_SITEID", "GROWATT_SITE_ID", "GROWATT_SiteID")

    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if i_plant is None or len(r) <= i_plant:
            continue
        plant = normalize_text(r[i_plant])
        if not plant:
            continue

        siteid = normalize_text(r[i_site]) if i_site is not None and i_site < len(r) else ""
        lat = safe_float(r[i_lat], None) if i_lat is not None and i_lat < len(r) else None
        lon = safe_float(r[i_lon], None) if i_lon is not None and i_lon < len(r) else None
        ws = normalize_text(r[i_ws]) if i_ws is not None and i_ws < len(r) else ""
        addr_val = safe_float(r[i_addr], 0.0) if i_addr is not None and i_addr < len(r) else 0.0
        try:
            addr = int(addr_val or 0)
        except Exception:
            addr = 0
        growatt_site = normalize_text(r[i_growatt_site]) if i_growatt_site is not None and i_growatt_site < len(r) else ""

        out[plant] = {
            "siteid": siteid,
            "lat": lat,
            "lon": lon,
            "weather_sn": ws,
            "addr": addr,
            "growatt_siteid_for_weather": growatt_site,
        }
    return out


def read_snap(sheet_id: str, snap_range: str) -> List[Dict[str, Any]]:
    values = read_table(sheet_id, snap_range)
    if not values:
        return []

    header = [normalize_text(h).upper() for h in values[0]]
    rows = values[1:]

    def idx(name: str) -> Optional[int]:
        try:
            return header.index(name.upper())
        except ValueError:
            return None

    i_plant = idx("PLANT_KEY")
    if i_plant is None:
        i_plant = idx("PLANTKEY")

    i_site = idx("SITEID")
    i_brand = idx("BRAND")

    if i_plant is None or i_site is None or i_brand is None:
        raise RuntimeError(f"SNAP missing Plant_Key/SITEID/Brand columns. Header={header}")

    inv_cols = [i for i, h in enumerate(header) if ("INVERTER" in h) or ("IVERTER" in h)]

    out = []
    for r in rows:
        need_max = max([i_plant, i_site, i_brand] + (inv_cols or [0]))
        if len(r) <= need_max:
            continue

        plant = normalize_text(r[i_plant])
        siteid = normalize_text(r[i_site])
        brand = normalize_text(r[i_brand]).upper()

        sns: List[str] = []
        for j in inv_cols:
            if j < len(r):
                sn = normalize_text(r[j])
                if sn:
                    sns.append(sn)

        sns = [normalize_sn(x) for x in sns]
        sns = list(dict.fromkeys([s for s in sns if s]))

        if plant and siteid and sns:
            out.append({"plant_key": plant, "siteid": siteid, "brand": brand, "sns": sns})

    return out


# ----------------------------
# Meteo cache
# ----------------------------
METEO_CACHE: Dict[Tuple[str, str, int], Tuple[float, float]] = {}


def get_meteo_for(
    plants: Dict[str, Dict[str, Any]],
    plant_key: str,
    siteid: str,
    when_utc: datetime,
    interval_min: int,
) -> Tuple[float, float]:
    conf = plants.get(plant_key) or {}
    lat = conf.get("lat")
    lon = conf.get("lon")
    ws = conf.get("weather_sn") or ""
    addr = int(conf.get("addr") or 0)

    weather_plant_id = siteid if looks_like_growatt_siteid(siteid) else normalize_text(conf.get("growatt_siteid_for_weather") or "")
    key = (str(weather_plant_id), str(ws), int(addr))
    if key in METEO_CACHE:
        return METEO_CACHE[key]

    if not lat or not lon or not ws or not addr or not weather_plant_id or not looks_like_growatt_siteid(weather_plant_id):
        METEO_CACHE[key] = (0.0, 0.0)
        return METEO_CACHE[key]

    irr, cloud_frac = meteo.get_meteo_snapshot(
        plant_id_for_weather=str(weather_plant_id),
        lat=float(lat),
        lon=float(lon),
        weather_sn=str(ws),
        addr=int(addr),
        when_utc=when_utc,
        interval_minutes=interval_min,
    )
    METEO_CACHE[key] = (float(irr), float(cloud_frac))
    return METEO_CACHE[key]


# ----------------------------
# Growatt client (endpoint scoring + best-row selection)
# ----------------------------
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
        self.get("/device")
        self.get("/device/photovoltaic", params={"plantId": plant_id}, referer=self.BASE + "/device")
        self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")
        self.s.cookies.set("selPage", "%2Fpanel", domain="server.growatt.com", path="/")

    def get_max_page_html(self, plant_id: str) -> str:
        r = self.get("/device/getMAXPage", params={"ttt": str(now_ms())}, referer=self.BASE + "/index")
        return r.text or ""

    def get_inverter_page_html(self, plant_id: str) -> str:
        r = self.get("/device/getInverterPage", params={"plantId": str(plant_id)}, referer=self.BASE + "/device")
        return r.text or ""

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

    def _call_json(self, plant_id: str, endpoint: str, payload: dict) -> Optional[dict]:
        r = self.post(endpoint, data=payload, referer=self.BASE + "/index")
        data = try_parse_json(r.text or "")
        if data:
            return data
        r2 = self.get(endpoint, params=payload, referer=self.BASE + "/index")
        data2 = try_parse_json(r2.text or "")
        if data2:
            return data2
        return None

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
        return normalize_sn(pick(it, ["sn", "deviceSn", "invSn", "serialNum", "serialNo"]) or "")

    @staticmethod
    def _device_type_num(it: Dict[str, Any]) -> Optional[int]:
        v = pick(it, ["deviceType", "deviceTypeNum", "type"])
        n = safe_float(v, None)
        if n is None:
            return None
        try:
            return int(n)
        except Exception:
            return None

    def fetch_devices_best_for_snap(
        self,
        plant_id: str,
        wanted_sns: List[str],
        page_size: int,
        max_pages: int,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Probe safe list endpoints and select the BEST endpoint for *inverter* rows.
        Score priority:
          1) SNAP SN hits that have non-empty eToday
          2) SNAP SN hits that have deviceType==4 (inverter)
          3) SNAP SN hits at all
          4) total items
        """
        wanted = {normalize_sn(x) for x in (wanted_sns or []) if x}

        html_max = self.get_max_page_html(plant_id)
        html_inv = self.get_inverter_page_html(plant_id)

        urls = self.discover_ajax_urls(html_max) + self.discover_ajax_urls(html_inv)
        urls += [
            "/device/getMAXList",
            "/device/getMaxList",
            "/device/getInverterList",
            "/device/getInverterListData",
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
        GLOG.info("Found %d safe list candidates for plant %s", len(safe_candidates), plant_id)

        payload_variants = [
            {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size), "ind": "1"},
            {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size)},
            {"plantId": str(plant_id), "pageSize": str(page_size), "currPage": "1"},
            {"currPage": "1", "pageSize": str(page_size)},
        ]

        best_items: List[Dict[str, Any]] = []
        best_ep = ""
        best_score = (-1, -1, -1, -1)  # (hits_with_etoday, hits_type4, hits_any, total_items)

        for ep in safe_candidates:
            all_items: List[Dict[str, Any]] = []
            for page in range(1, max_pages + 1):
                page_items: List[Dict[str, Any]] = []
                for base in payload_variants:
                    payload = dict(base)
                    payload["currPage"] = str(page)
                    payload["pageSize"] = str(page_size)

                    data = self._call_json(plant_id, ep, payload)
                    if not data:
                        continue

                    items = self._extract_items(data)
                    if not items:
                        continue

                    page_items = items
                    break

                if not page_items:
                    break

                all_items.extend(page_items)
                if len(page_items) < page_size:
                    break

            if not all_items:
                continue

            # Score endpoint
            hits_any = 0
            hits_type4 = 0
            hits_etoday = 0

            for it in all_items:
                sn = self._sn_from_item(it)
                if not sn or (wanted and sn not in wanted):
                    continue
                hits_any += 1

                dtype = self._device_type_num(it)
                if dtype == 4:
                    hits_type4 += 1

                et = growatt_extract_etoday(it)
                if et is not None and et > 0:
                    hits_etoday += 1

            score = (hits_etoday, hits_type4, hits_any, len(all_items))

            if score > best_score:
                best_score = score
                best_items = all_items
                best_ep = ep

        if best_ep:
            GLOG.info(
                "✅ Growatt best endpoint for plant %s is %s score(etoday=%d,type4=%d,hits=%d,items=%d)",
                plant_id, best_ep, best_score[0], best_score[1], best_score[2], best_score[3]
            )
            return best_items, best_ep

        GLOG.warning("❌ No device list endpoint produced usable rows for plant %s", plant_id)
        return [], ""


def growatt_status_to_1_3(val: Any) -> int:
    if val is None:
        return 1
    s = str(val).strip().lower()
    if s in ("3", "offline", "off", "0", "disconnect", "disconnected"):
        return 3
    return 1


def growatt_extract_etoday(it: Dict[str, Any]) -> Optional[float]:
    v = pick(it, ["eToday", "EToday", "todayEnergy", "generationToday", "today_energy", "dayEnergy", "day_energy"])
    if v is not None:
        return safe_float(v, None)

    m = it.get("dataItemMap")
    if isinstance(m, dict):
        v2 = pick(m, ["eToday", "EToday", "todayEnergy", "generationToday", "day_cap", "daily_cap", "dayEnergy"])
        return safe_float(v2, None) if v2 is not None else None

    return None


def growatt_best_row(existing: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """
    If same SN appears multiple times (e.g., inverter vs ShineWiFi-X),
    keep the best row:
      1) prefer deviceType==4
      2) prefer higher eToday
    """
    ex_type = safe_float(pick(existing, ["deviceType", "deviceTypeNum", "type"]), None)
    ca_type = safe_float(pick(candidate, ["deviceType", "deviceTypeNum", "type"]), None)

    ex_is4 = int(ex_type) == 4 if ex_type is not None else False
    ca_is4 = int(ca_type) == 4 if ca_type is not None else False

    ex_et = growatt_extract_etoday(existing) or 0.0
    ca_et = growatt_extract_etoday(candidate) or 0.0

    if ca_is4 and not ex_is4:
        return candidate
    if ex_is4 and not ca_is4:
        return existing
    if ca_et > ex_et:
        return candidate
    return existing


# ----------------------------
# Huawei (stable SN-based)
# ----------------------------
class HuaweiThirdDataClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30):
        self.base = base_url.rstrip("/")
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

    def get_dev_real_kpi_by_sns(self, dev_type_id: int, sns: List[str]) -> List[Dict[str, Any]]:
        body = {"devTypeId": str(dev_type_id), "sns": ",".join(sns)}
        r = self.s.post(f"{self.base}/getDevRealKpi", json=body, timeout=self.timeout)
        js = r.json()
        if not js.get("success"):
            raise RuntimeError(f"getDevRealKpi failed: failCode={js.get('failCode')} message={js.get('message')}")
        data = js.get("data") or []
        return [d for d in data if isinstance(d, dict)]


def huawei_status_to_1_3(val: Any) -> int:
    if val is None:
        return 1
    s = str(val).strip().lower()
    if s in ("0", "3", "offline", "off", "disconnected", "disconnect"):
        return 3
    return 1


def huawei_parse_item(item: Dict[str, Any]) -> Dict[str, Any]:
    m = item.get("dataItemMap") or {}
    if not isinstance(m, dict):
        m = {}
    sn = normalize_sn(item.get("sn") or item.get("devSn") or item.get("deviceSn") or item.get("serialNum") or "")
    status = item.get("devStatus") or item.get("status") or item.get("workStatus")
    e_today = safe_float(m.get("day_cap") or m.get("daily_cap") or m.get("eToday") or m.get("todayEnergy"), None)
    return {"sn": sn, "status_num": huawei_status_to_1_3(status), "e_today": e_today}


def chunked(xs: List[str], n: int) -> List[List[str]]:
    return [xs[i:i + n] for i in range(0, len(xs), n)]


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    setup_logging()

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    unified_tab = os.getenv("UNIFIED_TAB", "InverterUnified10m").strip()
    config_range = os.getenv("CONFIG_RANGE", "Config_Plants!A1:Z1000").strip()
    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z1000").strip()
    interval_min = int(os.getenv("INTERVAL_MINUTES", "10").strip())

    ensure_header(sheet_id, unified_tab)

    plants = read_config_plants(sheet_id, config_range)
    snap_rows = read_snap(sheet_id, snap_range)
    LOG.info("SNAP rows=%d", len(snap_rows))
    if not snap_rows:
        return

    when = now_utc()
    extracted_at = now_utc_iso()

    rows_out: List[List[Any]] = []

    # -------- Growatt ----------
    g_user = (os.getenv("GROWATT_USERNAME") or os.getenv("GROWATT_USER") or "").strip()
    g_pass = (os.getenv("GROWATT_PASSWORD") or os.getenv("GROWATT_PASS") or "").strip()

    if g_user and g_pass:
        gcli = GrowattMonitoringClient(GrowattAuth(user=g_user, password=g_pass))
        gcli.login()

        page_size = int(os.getenv("PAGE_SIZE", "50").strip())
        max_pages = int(os.getenv("MAX_PAGES", "6").strip())

        for srow in snap_rows:
            if srow["brand"] != "GROWATT":
                continue

            siteid = srow["siteid"]
            plant_key = srow["plant_key"]
            wanted_sns = [normalize_sn(sn) for sn in srow["sns"]]

            if not looks_like_growatt_siteid(siteid):
                continue

            irr, cloud_frac = get_meteo_for(plants, plant_key, siteid, when, interval_min)

            gcli.warm_plant_context(siteid)
            items, endpoint = gcli.fetch_devices_best_for_snap(siteid, wanted_sns, page_size=page_size, max_pages=max_pages)

            # Build SN -> best item (prefer inverter rows)
            fetched: Dict[str, Dict[str, Any]] = {}
            for it in items:
                sn = gcli._sn_from_item(it)
                if not sn:
                    continue
                if sn not in fetched:
                    fetched[sn] = it
                else:
                    fetched[sn] = growatt_best_row(fetched[sn], it)

            # If still mismatch, show returned deviceType examples
            if items and wanted_sns and not any(sn in fetched for sn in wanted_sns):
                sample = []
                for it in items[:10]:
                    sn = gcli._sn_from_item(it)
                    dt_name = normalize_text(pick(it, ["deviceTypeName", "model"]))
                    dt_num = pick(it, ["deviceType", "deviceTypeNum", "type"])
                    sample.append(f"{sn}:{dt_num}/{dt_name}")
                GLOG.warning("Plant %s: SNAP SNs didn't match. Sample returned=%s", siteid, sample)

            for sn in wanted_sns:
                it = fetched.get(sn)
                if it:
                    device_type = normalize_text(pick(it, ["deviceType", "deviceTypeNum", "type", "deviceTypeName"])) or "GROWATT_INV"
                    status_raw = pick(it, ["status", "deviceStatus", "invStatus", "workStatus", "connStatus"])
                    etoday = growatt_extract_etoday(it)
                    upd = parse_update_time_to_mx(pick(it, ["updateTime", "lastUpdateTime", "time"]))
                    rows_out.append([
                        extracted_at,
                        upd,
                        siteid,
                        device_type,
                        sn,
                        growatt_status_to_1_3(status_raw),
                        etoday if etoday is not None else "",
                        irr,
                        cloud_frac,
                    ])
                else:
                    rows_out.append([
                        extracted_at,
                        now_mx_str(),
                        siteid,
                        "GROWATT_INV",
                        sn,
                        "",
                        "",
                        irr,
                        cloud_frac,
                    ])

            time.sleep(0.4)
    else:
        LOG.warning("Missing Growatt creds; skipping Growatt.")

    # -------- Huawei ----------
    h_user = os.getenv("HUAWEI_USERNAME", "").strip()
    h_pass = os.getenv("HUAWEI_PASSWORD", "").strip()

    if h_user and h_pass:
        base = (os.getenv("HUAWEI_BASE_URL") or "https://la5.fusionsolar.huawei.com/thirdData").rstrip("/")
        hcli = HuaweiThirdDataClient(base, h_user, h_pass, timeout=30)
        hcli.login()

        devtype = int(os.getenv("HUAWEI_INVERTER_DEVTYPE", "1").strip())

        for srow in snap_rows:
            if srow["brand"] != "HUAWEI":
                continue

            siteid = srow["siteid"]
            plant_key = srow["plant_key"]
            wanted_sns = [normalize_sn(sn) for sn in srow["sns"]]

            if not looks_like_huawei_station(siteid):
                continue

            irr, cloud_frac = get_meteo_for(plants, plant_key, siteid, when, interval_min)

            fetched: Dict[str, Dict[str, Any]] = {}
            for batch in chunked(wanted_sns, 50):
                items = hcli.get_dev_real_kpi_by_sns(devtype, batch)
                for it in items:
                    k = huawei_parse_item(it)
                    if k["sn"]:
                        fetched[k["sn"]] = k
                time.sleep(0.2)

            for sn in wanted_sns:
                k = fetched.get(sn)
                if k:
                    rows_out.append([
                        extracted_at,
                        now_mx_str(),
                        siteid,
                        str(devtype),
                        sn,
                        k["status_num"],
                        k["e_today"] if k["e_today"] is not None else "",
                        irr,
                        cloud_frac,
                    ])
                else:
                    rows_out.append([
                        extracted_at,
                        now_mx_str(),
                        siteid,
                        str(devtype),
                        sn,
                        "",
                        "",
                        irr,
                        cloud_frac,
                    ])

            time.sleep(0.2)
    else:
        LOG.warning("Missing Huawei creds; skipping Huawei.")

    append_rows(sheet_id, unified_tab, rows_out)
    LOG.info("✅ Unified sync written rows=%d tab=%s", len(rows_out), unified_tab)


if __name__ == "__main__":
    main()
