#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import argia_meteo as meteo
from argia_growatt_inverters import GrowattMonitoringClient, GrowattAuth

LOG = logging.getLogger("argia.sync")
MX_TZ = ZoneInfo("America/Mexico_City")


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_utc_iso() -> str:
    return now_utc().isoformat()


def utc_iso_to_mx_str(iso_utc: str) -> str:
    try:
        dt_utc = datetime.fromisoformat(iso_utc)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now(MX_TZ).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()


def normalize_sn(x: Any) -> str:
    # case-insensitive + remove spaces
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
        return float(x)
    except Exception:
        return default


def qrange(tab: str, a1: str) -> str:
    return f"'{tab}'!{a1}"


def pick(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return None


def chunked(xs: List[str], n: int) -> List[List[str]]:
    return [xs[i:i + n] for i in range(0, len(xs), n)]


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
        "UpdateTime",            # Mexico City time
        "SiteId",
        "DeviceType",
        "DeviceSN",
        "Status",
        "EToday_kWh",
        "Irradiance_kWh_m2",
        "Cloud_Coverage",        # 0..1
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
# Config_Plants (header-based)
# ----------------------------
def read_config_plants(sheet_id: str, config_range: str) -> Dict[str, Dict[str, Any]]:
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
    i_ws = idx("WEATHERSTATION", "WEATHER STATION")
    i_addr = idx("ADDR", "ADDRESS")
    i_growatt_site = idx("GROWATT_SITEID", "GROWATT_SITE_ID", "GROWATT_SiteID")

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
# SNAP (header-based)
# ----------------------------
def read_snap(sheet_id: str, snap_range: str) -> List[Dict[str, Any]]:
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
        need = max([i_plant, i_site, i_brand] + (inv_cols or [0]))
        if len(r) <= need:
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

        sns = list(dict.fromkeys([s for s in sns if s]))

        if not plant or not siteid or not sns:
            continue

        out.append({"plant_key": plant, "brand": brand, "siteid": siteid, "sns": sns})

    return out


# ----------------------------
# Huawei thirdData (by SN)
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

    sn = normalize_sn(pick(item, ["sn", "devSn", "deviceSn", "serialNum", "esn"]))
    status = normalize_text(pick(item, ["devStatus", "status", "workStatus"]))
    e_today = safe_float(pick(m, ["day_cap", "daily_cap", "eToday", "todayEnergy", "etoday"]), None)

    st = 1
    if status:
        s = status.lower()
        if s in ("3", "offline", "off", "0", "disconnected"):
            st = 3

    return {"sn": sn, "status_num": st, "e_today": e_today}


def growatt_status_to_1_3(val: Any) -> int:
    if val is None:
        return 1
    s = str(val).strip().lower()
    if s in ("3", "offline", "off", "0", "disconnect", "disconnected"):
        return 3
    return 1


def growatt_extract_etoday(it: Dict[str, Any]) -> Optional[float]:
    """
    Growatt device list keys vary across plants. Try many candidates.
    """
    candidates = [
        "eToday", "EToday", "etoday",
        "todayEnergy", "TodayEnergy", "today_energy",
        "generationToday", "genToday",
        "e_today", "E_TODAY",
    ]
    v = pick(it, candidates)
    return safe_float(v, None)


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
    extracted_mx = utc_iso_to_mx_str(extracted_at)

    meteo_cache: Dict[Tuple[str, str, int], Tuple[float, float]] = {}

    def get_meteo_for(plant_key: str, siteid: str) -> Tuple[float, float]:
        conf = plants.get(plant_key) or {}
        lat = conf.get("lat")
        lon = conf.get("lon")
        ws = conf.get("weather_sn") or ""
        addr = int(conf.get("addr") or 0)

        if looks_like_growatt_siteid(siteid):
            weather_plant_id = siteid
        else:
            weather_plant_id = normalize_text(conf.get("growatt_siteid_for_weather") or "")

        key = (str(weather_plant_id), str(ws), int(addr))
        if key in meteo_cache:
            return meteo_cache[key]

        if not lat or not lon or not ws or not addr or not weather_plant_id:
            meteo_cache[key] = (0.0, 0.0)
            return (0.0, 0.0)

        irr, cloud_frac = meteo.get_meteo_snapshot(
            plant_id_for_weather=str(weather_plant_id),
            lat=float(lat),
            lon=float(lon),
            weather_sn=str(ws),
            addr=int(addr),
            when_utc=when,
            interval_minutes=interval_min,
        )
        meteo_cache[key] = (irr, cloud_frac)
        return irr, cloud_frac

    rows_out: List[List[Any]] = []

    # ----------------------------
    # Growatt
    # ----------------------------
    g_user = os.getenv("GROWATT_USERNAME", "").strip()
    g_pass = os.getenv("GROWATT_PASSWORD", "").strip()
    if g_user and g_pass:
        gcli = GrowattMonitoringClient(GrowattAuth(user=g_user, password=g_pass))
        gcli.login()

        for srow in snap_rows:
            if srow["brand"] != "GROWATT":
                continue

            siteid = srow["siteid"]
            plant_key = srow["plant_key"]
            wanted_sns_raw = [sn for sn in srow["sns"] if sn]
            wanted_sns = [normalize_sn(sn) for sn in wanted_sns_raw]

            if not looks_like_growatt_siteid(siteid):
                continue

            irr, cloud_frac = get_meteo_for(plant_key, siteid)

            gcli.warm_plant_context(siteid)
            items = gcli.fetch_devices_for_plant(siteid, page_size=50, max_pages=5)

            # Build fetched map using normalized SN
            fetched: Dict[str, Dict[str, Any]] = {}
            for it in items:
                sn0 = pick(it, ["sn", "deviceSn", "invSn", "serialNum", "serialNo"])
                sn = normalize_sn(sn0)
                if sn:
                    fetched[sn] = it

            # Debug: if we have no match on this plant, show sample keys once
            if items and not any(sn in fetched for sn in wanted_sns):
                LOG.warning("Growatt plant %s: 0/%d SNAP SNs matched device list (check SN format).", siteid, len(wanted_sns))
                sample = items[0]
                LOG.info("Growatt plant %s sample keys: %s", siteid, list(sample.keys())[:25])

            for sn in wanted_sns:
                it = fetched.get(sn)
                update_time_mx = extracted_mx

                if it:
                    device_type = normalize_text(pick(it, ["deviceType", "deviceTypeNum", "type", "deviceTypeName"])) or "4"
                    status_raw = pick(it, ["status", "deviceStatus", "invStatus", "workStatus", "connStatus"])
                    etoday = growatt_extract_etoday(it)

                    # If still None, log once (helps find correct key)
                    if etoday is None:
                        LOG.warning("Growatt plant %s inverter %s: EToday missing. Candidate fields seen: %s",
                                    siteid, sn, {k: it.get(k) for k in it.keys() if "today" in k.lower() or "e" == k.lower()})

                    rows_out.append([
                        extracted_at,
                        update_time_mx,
                        siteid,
                        device_type,
                        sn,
                        growatt_status_to_1_3(status_raw),
                        etoday if etoday is not None else "",
                        irr,
                        cloud_frac,
                    ])
                else:
                    # Important: don't pretend it's 0 generation; keep blank + mark offline
                    LOG.warning("Growatt plant %s: SNAP inverter %s not found in device list.", siteid, sn)
                    rows_out.append([
                        extracted_at,
                        update_time_mx,
                        siteid,
                        "4",
                        sn,
                        3,
                        "",
                        irr,
                        cloud_frac,
                    ])

            time.sleep(0.6)
    else:
        LOG.warning("Missing Growatt creds; skipping Growatt.")

    # ----------------------------
    # Huawei
    # ----------------------------
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
            wanted_sns_raw = [sn for sn in srow["sns"] if sn]
            wanted_sns = [normalize_sn(sn) for sn in wanted_sns_raw]

            if not looks_like_huawei_station(siteid):
                continue

            irr, cloud_frac = get_meteo_for(plant_key, siteid)

            fetched: Dict[str, Dict[str, Any]] = {}
            for batch in chunked(wanted_sns, 50):
                items = hcli.get_dev_real_kpi_by_sns(devtype, batch)
                for it in items:
                    k = huawei_parse_item(it)
                    if k["sn"]:
                        fetched[k["sn"]] = k
                time.sleep(0.25)

            for sn in wanted_sns:
                update_time_mx = extracted_mx
                k = fetched.get(sn)
                if k:
                    rows_out.append([
                        extracted_at,
                        update_time_mx,
                        siteid,
                        str(devtype) if devtype else "1",
                        sn,
                        k["status_num"],
                        k["e_today"] if k["e_today"] is not None else "",
                        irr,
                        cloud_frac,
                    ])
                else:
                    LOG.warning("Huawei station %s: SNAP inverter %s not returned by getDevRealKpi.", siteid, sn)
                    rows_out.append([
                        extracted_at,
                        update_time_mx,
                        siteid,
                        str(devtype) if devtype else "1",
                        sn,
                        3,
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
