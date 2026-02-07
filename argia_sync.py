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
from argia_growatt_inverters import GrowattMonitoringClient, GrowattAuth, pick as growatt_pick

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
    dt_utc = datetime.fromisoformat(iso_utc)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S")


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
        return float(x)
    except Exception:
        return default


def qrange(tab: str, a1: str) -> str:
    return f"'{tab}'!{a1}"


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
        "UpdateTime",            # Mexico City time (always filled)
        "SiteId",
        "DeviceType",
        "DeviceSN",
        "Status",                # 1/3
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
# Config + SNAP (header-based)
# ----------------------------
def read_table(sheet_id: str, rng: str) -> List[List[Any]]:
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=rng,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    return resp.get("values", []) or []


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

    i_plant = idx("PLANT_KEY") or idx("PLANTKEY")
    i_site = idx("SITEID")
    i_brand = idx("BRAND")
    if i_plant is None or i_site is None or i_brand is None:
        raise RuntimeError(f"SNAP missing Plant_Key/SITEID/Brand columns. Header={header}")

    inv_cols = [i for i, h in enumerate(header) if ("INVERTER" in h) or ("IVERTER" in h)]

    out = []
    for r in rows:
        if len(r) <= max([i_plant, i_site, i_brand] + (inv_cols or [0])):
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
        sns = list(dict.fromkeys(sns))
        if plant and siteid and sns:
            out.append({"plant_key": plant, "siteid": siteid, "brand": brand, "sns": sns})
    return out


# ----------------------------
# Growatt inverter list: force the correct endpoint
# ----------------------------
def growatt_fetch_inverters(cli: GrowattMonitoringClient, plant_id: str, page_size: int = 50, max_pages: int = 5) -> List[Dict[str, Any]]:
    """
    Force inverter list endpoints. If one fails, try a small set of known safe endpoints.
    """
    candidates = [
        "/device/getInverterList",
        "/device/getInvList",
        "/device/getInverterlist",
        "/panel/getInverterList",
    ]

    payload_variants = [
        {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size)},
        {"currPage": "1", "pageSize": str(page_size)},  # plantId from cookie
    ]

    all_items: List[Dict[str, Any]] = []

    for ep in candidates:
        items_here: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            got_page: List[Dict[str, Any]] = []
            for base in payload_variants:
                payload = dict(base)
                payload["currPage"] = str(page)
                payload["pageSize"] = str(page_size)

                data = cli._call_json(plant_id, ep, payload)  # uses safe post/get in your class
                if not data:
                    continue
                items = cli._extract_items(data)
                if not items:
                    continue

                # Accept only items that actually have inverter SN fields
                if not any(growatt_pick(it, ["sn", "deviceSn", "invSn", "serialNum", "serialNo"]) for it in items):
                    continue

                got_page = items
                break

            if not got_page:
                break
            items_here.extend(got_page)
            if len(got_page) < page_size:
                break

        if items_here:
            LOG.info("✅ Growatt inverter endpoint %s items=%d plant=%s", ep, len(items_here), plant_id)
            all_items = items_here
            break

    return all_items


def growatt_status_to_1_3(val: Any) -> int:
    """
    Growatt: treat only explicit offline states as 3, everything else is 1.
    """
    if val is None:
        return 1
    s = str(val).strip().lower()
    if s in ("3", "offline", "off", "0", "disconnect", "disconnected"):
        return 3
    return 1


def growatt_extract_etoday(it: Dict[str, Any]) -> Optional[float]:
    v = growatt_pick(it, ["eToday", "EToday", "etoday", "todayEnergy", "today_energy", "generationToday"])
    return safe_float(v, None)


# ----------------------------
# Huawei (SN based)
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


def huawei_status_to_1_3(val: Any) -> int:
    """
    Huawei: be conservative — offline only if explicit 0/3/offline/disconnected.
    """
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
    extracted_mx = utc_iso_to_mx_str(extracted_at)

    meteo_cache: Dict[Tuple[str, str, int], Tuple[float, float]] = {}

    def get_meteo_for(plant_key: str, siteid: str) -> Tuple[float, float]:
        conf = plants.get(plant_key) or {}
        lat = conf.get("lat")
        lon = conf.get("lon")
        ws = conf.get("weather_sn") or ""
        addr = int(conf.get("addr") or 0)

        weather_plant_id = siteid if looks_like_growatt_siteid(siteid) else normalize_text(conf.get("growatt_siteid_for_weather") or "")
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

    # -------- Growatt ----------
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
            wanted_sns = [normalize_sn(sn) for sn in srow["sns"]]

            if not looks_like_growatt_siteid(siteid):
                continue

            irr, cloud_frac = get_meteo_for(plant_key, siteid)

            gcli.warm_plant_context(siteid)

            # 🔥 KEY CHANGE: force inverter list
            items = growatt_fetch_inverters(gcli, siteid, page_size=50, max_pages=5)

            fetched: Dict[str, Dict[str, Any]] = {}
            for it in items:
                sn0 = growatt_pick(it, ["sn", "deviceSn", "invSn", "serialNum", "serialNo"])
                sn = normalize_sn(sn0)
                if sn:
                    fetched[sn] = it

            if items and not any(sn in fetched for sn in wanted_sns):
                LOG.warning("Growatt plant %s: 0/%d SNAP SNs matched INVERTER list (check SNAP SNs).", siteid, len(wanted_sns))
                LOG.info("Growatt plant %s inverter sample keys: %s", siteid, list(items[0].keys())[:25])
                LOG.info("Growatt plant %s inverter sample SN fields: sn=%s deviceSn=%s invSn=%s serialNum=%s",
                         siteid,
                         items[0].get("sn"),
                         items[0].get("deviceSn"),
                         items[0].get("invSn"),
                         items[0].get("serialNum"))

            for sn in wanted_sns:
                it = fetched.get(sn)
                if it:
                    device_type = normalize_text(growatt_pick(it, ["deviceType", "deviceTypeNum", "type", "deviceTypeName"])) or "4"
                    status_raw = growatt_pick(it, ["status", "deviceStatus", "invStatus", "workStatus", "connStatus"])
                    etoday = growatt_extract_etoday(it)
                    rows_out.append([extracted_at, extracted_mx, siteid, device_type, sn, growatt_status_to_1_3(status_raw), etoday if etoday is not None else "", irr, cloud_frac])
                else:
                    # if not found we DON'T claim offline; we set status blank -> you can decide in Looker
                    LOG.warning("Growatt plant %s: SNAP inverter %s not found in INVERTER list.", siteid, sn)
                    rows_out.append([extracted_at, extracted_mx, siteid, "4", sn, "", "", irr, cloud_frac])

            time.sleep(0.5)
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

            irr, cloud_frac = get_meteo_for(plant_key, siteid)

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
                    rows_out.append([extracted_at, extracted_mx, siteid, str(devtype), sn, k["status_num"], k["e_today"] if k["e_today"] is not None else "", irr, cloud_frac])
                else:
                    LOG.warning("Huawei station %s: SNAP inverter %s not returned by getDevRealKpi.", siteid, sn)
                    rows_out.append([extracted_at, extracted_mx, siteid, str(devtype), sn, "", "", irr, cloud_frac])

            time.sleep(0.2)
    else:
        LOG.warning("Missing Huawei creds; skipping Huawei.")

    append_rows(sheet_id, unified_tab, rows_out)
    LOG.info("✅ Unified sync written rows=%d tab=%s", len(rows_out), unified_tab)


if __name__ == "__main__":
    main()
