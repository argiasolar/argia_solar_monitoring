#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA Unified 10-min Sync
------------------------
Writes one unified table containing:
ExtractedAtUTC, UpdateTime, SiteId, DeviceType, DeviceSN, Status, EToday_kWh, Irradiance_kWh_m2, Cloud_Coverage

Sources:
- Growatt inverters: Growatt web (safe list endpoints, via argia_growatt_inverters.py client)
- Huawei inverters: FusionSolar thirdData getDevRealKpi by SNS
- Meteo: Growatt ENV radiant -> 10-min kWh/m² interval + Open-Meteo cloud cover (%)

ENV required:
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS (service account json as TEXT)

- GROWATT_USERNAME / GROWATT_PASSWORD
- HUAWEI_USERNAME / HUAWEI_PASSWORD

Optional:
- UNIFIED_TAB (default "InverterUnified10m")
- CONFIG_RANGE (default "Config_Plants!A1:Z1000")
- SNAP_RANGE (default "SNAP!A1:Z1000")
- HUAWEI_BASE_URL (default LA5 thirdData)
- LOG_LEVEL (default INFO)
- INTERVAL_MINUTES (default 10)
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import argia_meteo as meteo

# Import Growatt safe client from your working script
from argia_growatt_inverters import GrowattMonitoringClient, GrowattAuth  # noqa


LOG = logging.getLogger("argia.sync")


# ----------------------------
# Logging
# ----------------------------
def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


# ----------------------------
# Helpers
# ----------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)

def now_utc_iso() -> str:
    return now_utc().isoformat()

def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()

def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, str):
            x = x.strip().replace(",", "")
        return float(x)
    except Exception:
        return None

def qrange(tab: str, a1: str) -> str:
    return f"'{tab}'!{a1}"

def pick(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return None

def chunked(xs: List[str], n: int) -> List[List[str]]:
    return [xs[i:i+n] for i in range(0, len(xs), n)]

def looks_like_growatt_siteid(s: str) -> bool:
    return bool(re.fullmatch(r"\d{6,12}", s or ""))

def looks_like_huawei_station(s: str) -> bool:
    return (s or "").startswith("NE=")


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


# ----------------------------
# Read Config_Plants (header-based)
# ----------------------------
def read_config_plants(sheet_id: str, config_range: str) -> Dict[str, Dict[str, Any]]:
    """
    Returns dict keyed by PlantKey with fields:
      brand, siteid, lat, lon, weather_sn, addr, growatt_siteid_for_weather
    Supports your described columns:
      SiteId (may be in N in your sheet),
      WeatherStation (O),
      Addr (P),
      plus Growatt_SiteID column.
    We'll search by header names, not fixed indexes.
    """
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=config_range,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    values = resp.get("values", []) or []
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
    i_brand = idx("BRAND")
    i_site = idx("SITEID", "SITE ID", "SITE_ID")
    i_lat = idx("LATITUDE", "LAT")
    i_lon = idx("LONGTITUDE", "LONGITUDE", "LON")
    i_ws  = idx("WEATHERSTATION", "WEATHER STATION")
    i_addr = idx("ADDR", "ADDRESS")
    i_growatt_site = idx("GROWATT_SITEID", "GROWATT_SITE_ID", "GROWATT_SITeID", "GROWATT_SiteID")

    if i_plant is None:
        raise RuntimeError(f"Config_Plants missing PlantKey column. Header={header}")

    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if len(r) <= i_plant:
            continue
        plant = normalize_text(r[i_plant])
        if not plant:
            continue

        brand = normalize_text(r[i_brand]) if i_brand is not None and i_brand < len(r) else ""
        siteid = normalize_text(r[i_site]) if i_site is not None and i_site < len(r) else ""
        lat = safe_float(r[i_lat]) if i_lat is not None and i_lat < len(r) else None
        lon = safe_float(r[i_lon]) if i_lon is not None and i_lon < len(r) else None
        ws = normalize_text(r[i_ws]) if i_ws is not None and i_ws < len(r) else ""
        addr = int(safe_float(r[i_addr], 0) if i_addr is not None and i_addr < len(r) else 0) if True else 0
        growatt_site = normalize_text(r[i_growatt_site]) if i_growatt_site is not None and i_growatt_site < len(r) else ""

        out[plant] = {
            "brand": brand.upper(),
            "siteid": siteid,
            "lat": lat,
            "lon": lon,
            "weather_sn": ws,
            "addr": addr,
            "growatt_siteid_for_weather": growatt_site,
        }
    return out


# ----------------------------
# Read SNAP (header-based)
# ----------------------------
def read_snap(sheet_id: str, snap_range: str) -> List[Dict[str, Any]]:
    """
    Returns list of rows:
      {plant_key, brand, siteid, inverter_sns[]}
    """
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=snap_range,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    values = resp.get("values", []) or []
    if not values:
        return []

    header = [normalize_text(h).upper() for h in values[0]]
    rows = values[1:]

    def idx(name: str) -> Optional[int]:
        try:
            return header.index(name.upper())
        except ValueError:
            return None

    i_plant = idx("PLANT_KEY") or idx("PLANTKEY")
    i_site = idx("SITEID")
    i_brand = idx("BRAND")

    if i_plant is None or i_site is None or i_brand is None:
        raise RuntimeError(f"SNAP missing Plant_Key/SITEID/Brand columns. Header={header}")

    inv_cols = [i for i, h in enumerate(header) if "INVERTER" in h]  # includes typos too

    out = []
    for r in rows:
        if len(r) <= max([i_plant, i_site, i_brand] + (inv_cols or [0])):
            continue
        plant = normalize_text(r[i_plant])
        siteid = normalize_text(r[i_site])
        brand = normalize_text(r[i_brand]).upper()

        sns = []
        for j in inv_cols:
            if j < len(r):
                sn = normalize_text(r[j])
                if sn and re.fullmatch(r"[A-Za-z0-9]{8,32}", sn):
                    sns.append(sn)

        sns = sorted(list(dict.fromkeys(sns)))
        if not plant or not siteid or not sns:
            continue

        out.append({
            "plant_key": plant,
            "brand": brand,
            "siteid": siteid,
            "sns": sns,
        })

    return out


# ----------------------------
# Huawei client (thirdData by SNS)
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

    def get_dev_real_kpi_by_sns(self, dev_type_id: int, sns: List[str]) -> List[Dict[str, Any]]:
        body = {"devTypeId": str(dev_type_id), "sns": ",".join(sns)}
        r = self.s.post(f"{self.base}/getDevRealKpi", json=body, timeout=self.timeout)
        js = r.json()
        if not js.get("success"):
            raise RuntimeError(f"getDevRealKpi failed: failCode={js.get('failCode')} message={js.get('message')}")
        data = js.get("data") or []
        return [d for d in data if isinstance(d, dict)]


def huawei_parse_item(item: Dict[str, Any]) -> Dict[str, Any]:
    m = item.get("dataItemMap") or {}
    if not isinstance(m, dict):
        m = {}

    sn = normalize_text(pick(item, ["sn", "devSn", "deviceSn", "serialNum", "esn"]))
    status = normalize_text(pick(item, ["devStatus", "status", "workStatus"]))
    update_time = normalize_text(pick(item, ["collectTime", "updateTime", "time", "dataTime"]))

    power = safe_float(pick(m, ["active_power", "activePower", "pac", "power"]))
    power_w = None
    if power is not None:
        power_w = power * 1000.0 if power <= 1000 else power

    e_today = safe_float(pick(m, ["day_cap", "daily_cap", "eToday", "todayEnergy"]))

    # Status mapping:
    # If empty -> assume online (1) unless power/e_today missing; keep conservative:
    st = 1
    if status:
        s = status.lower()
        if s in ("3", "offline", "off", "0", "disconnected"):
            st = 3

    return {
        "sn": sn,
        "status_num": st,
        "update_time": update_time,
        "e_today": e_today,
    }


# ----------------------------
# Growatt parsing helper
# ----------------------------
def growatt_status_to_1_3(val: Any) -> int:
    if val is None:
        return 1
    s = str(val).strip().lower()
    if s in ("3", "offline", "off", "0", "disconnect", "disconnected"):
        return 3
    return 1


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

    if not snap_rows:
        LOG.warning("No SNAP rows found.")
        return

    # Prepare meteo cache per site/plant
    when = now_utc()
    meteo_by_plant: Dict[str, Tuple[float, float]] = {}

    def get_meteo_for_plant(plant_key: str, siteid: str) -> Tuple[float, float]:
        if plant_key in meteo_by_plant:
            return meteo_by_plant[plant_key]

        conf = plants.get(plant_key) or {}
        lat = conf.get("lat")
        lon = conf.get("lon")
        ws = conf.get("weather_sn") or ""
        addr = int(conf.get("addr") or 0)

        # determine which growatt plant id to use for weather
        # - for growatt plants: use numeric siteid if numeric
        # - for huawei plants: use growatt_siteid_for_weather (Config_Plants)
        weather_plant_id = ""
        if looks_like_growatt_siteid(siteid):
            weather_plant_id = siteid
        else:
            weather_plant_id = normalize_text(conf.get("growatt_siteid_for_weather") or "")

        if not lat or not lon or not ws or not addr or not weather_plant_id:
            meteo_by_plant[plant_key] = (0.0, 0.0)
            return (0.0, 0.0)

        irr, clouds = meteo.get_meteo_snapshot(
            plant_id_for_weather=str(weather_plant_id),
            lat=float(lat),
            lon=float(lon),
            weather_sn=str(ws),
            addr=int(addr),
            when_utc=when,
            interval_minutes=interval_min,
        )
        meteo_by_plant[plant_key] = (irr, clouds)
        return irr, clouds

    extracted_at = now_utc_iso()
    rows_out: List[List[Any]] = []

    # ----------------------------
    # Growatt inverters
    # ----------------------------
    growatt_user = os.getenv("GROWATT_USERNAME", "").strip()
    growatt_pass = os.getenv("GROWATT_PASSWORD", "").strip()
    if not growatt_user or not growatt_pass:
        LOG.warning("Missing Growatt web creds; Growatt inverter rows will be skipped.")
    else:
        growatt_cli = GrowattMonitoringClient(GrowattAuth(user=growatt_user, password=growatt_pass))
        growatt_cli.login()

        for srow in snap_rows:
            if srow["brand"] != "GROWATT":
                continue
            siteid = srow["siteid"]
            plant_key = srow["plant_key"]
            if not looks_like_growatt_siteid(siteid):
                continue

            irr, clouds = get_meteo_for_plant(plant_key, siteid)

            growatt_cli.warm_plant_context(siteid)
            items = growatt_cli.fetch_devices_for_plant(siteid, page_size=50, max_pages=3)

            wanted_sns = set(srow["sns"])
            for it in items:
                sn = normalize_text(pick(it, ["sn", "deviceSn", "invSn", "serialNum", "serialNo"]))
                if not sn or (wanted_sns and sn not in wanted_sns):
                    continue

                device_type = normalize_text(pick(it, ["deviceType", "deviceTypeNum", "type", "deviceTypeName"]))
                status_raw = pick(it, ["status", "deviceStatus", "invStatus", "workStatus", "connStatus"])
                update_time = normalize_text(pick(it, ["updateTime", "lastUpdateTime", "time"]))

                etoday = safe_float(pick(it, ["eToday", "EToday", "todayEnergy", "generationToday"]))

                rows_out.append([
                    extracted_at,
                    update_time,
                    siteid,
                    device_type,
                    sn,
                    growatt_status_to_1_3(status_raw),
                    etoday if etoday is not None else "",
                    irr,
                    clouds,  # %
                ])

            time.sleep(0.6)

    # ----------------------------
    # Huawei inverters
    # ----------------------------
    h_user = os.getenv("HUAWEI_USERNAME", "").strip()
    h_pass = os.getenv("HUAWEI_PASSWORD", "").strip()
    if not h_user or not h_pass:
        LOG.warning("Missing Huawei creds; Huawei inverter rows will be skipped.")
    else:
        base = (os.getenv("HUAWEI_BASE_URL") or "https://la5.fusionsolar.huawei.com/thirdData").rstrip("/")
        hcli = HuaweiThirdDataClient(base, h_user, h_pass, timeout=30)
        hcli.login()

        devtype = int(os.getenv("HUAWEI_INVERTER_DEVTYPE", "1").strip())

        for srow in snap_rows:
            if srow["brand"] != "HUAWEI":
                continue
            siteid = srow["siteid"]
            plant_key = srow["plant_key"]
            if not looks_like_huawei_station(siteid):
                continue

            irr, clouds = get_meteo_for_plant(plant_key, siteid)

            sns = srow["sns"]
            for batch in chunked(sns, 50):
                items = hcli.get_dev_real_kpi_by_sns(devtype, batch)

                for it in items:
                    k = huawei_parse_item(it)
                    sn = k["sn"]
                    if not sn:
                        continue

                    update_time = k["update_time"] or ""  # may be empty; still ok
                    rows_out.append([
                        extracted_at,
                        update_time,
                        siteid,
                        str(devtype),
                        sn,
                        k["status_num"],
                        k["e_today"] if k["e_today"] is not None else "",
                        irr,
                        clouds,
                    ])

                time.sleep(0.35)

    # Write everything in one append
    append_rows(sheet_id, unified_tab, rows_out)
    LOG.info("✅ Unified sync written rows=%d tab=%s", len(rows_out), unified_tab)


if __name__ == "__main__":
    main()
