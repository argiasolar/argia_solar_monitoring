#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA – Huawei Inverter Snapshot (10-min)

Reads Huawei plants & inverter SNs from SNAP sheet, then queries FusionSolar thirdData:
  1) POST /thirdData/login
  2) POST /thirdData/getDevList   (stationCodes -> devices, incl. devId/devTypeId/sn)
  3) POST /thirdData/getDevRealKpi (devTypeId + devIds -> realtime KPI for inverters)

Writes rows into a Google Sheets tab (default: HuaweiInverterData) with schema:
ExtractedAtUTC, SiteId, DeviceType, DeviceSN, Status, UpdateTime,
RatedPower_W, CurrentPower_W, EToday_kWh, EMonth_kWh, ETotal_kWh

ENV required:
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS   (service-account JSON as TEXT)

- HUAWEI_USERNAME
- HUAWEI_PASSWORD

Optional:
- HUAWEI_BASE_URL      default "https://la5.fusionsolar.huawei.com/thirdData"
- SNAP_RANGE           default "SNAP!A1:Z"
- HUAWEI_INVERTER_TAB  default "HuaweiInverterData"
- LOG_LEVEL            default "INFO"
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


LOG = logging.getLogger("argia.huawei.inverters")


# ----------------------------
# Logging
# ----------------------------
def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


# ----------------------------
# Helpers
# ----------------------------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()


def safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        if isinstance(x, str):
            x = x.strip().replace(",", "")
        return float(x)
    except Exception:
        return None


def to_text_station_code(x: Any) -> str:
    return normalize_text(x)


def is_huawei_station_code(siteid: str) -> bool:
    # Your SNAP uses "NE=35314736"
    return siteid.startswith("NE=") or bool(re.fullmatch(r"[A-Za-z]{2}=\d+", siteid))


def is_sn_like(x: str) -> bool:
    # Huawei inverter SNs in your SNAP look like: ES2470051825 / GR2499018270
    # We'll accept alnum 8..20
    return bool(re.fullmatch(r"[A-Za-z0-9]{8,24}", x))


# ----------------------------
# Google Sheets
# ----------------------------
def load_google_creds() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS (service account JSON as text).")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def sheets_service():
    return build("sheets", "v4", credentials=load_google_creds(), cache_discovery=False)


def read_snap_huawei(sheet_id: str, snap_range: str) -> Dict[str, Dict[str, Any]]:
    """
    Returns dict keyed by stationCode (SiteId), containing:
      { plant_key: str, inverter_sns: [..] }
    Reads SNAP header row to locate columns by name.
    """
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=snap_range,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()

    values = resp.get("values", []) or []
    if not values:
        return {}

    header = [normalize_text(h).upper() for h in values[0]]
    rows = values[1:]

    def idx(name: str) -> Optional[int]:
        try:
            return header.index(name.upper())
        except ValueError:
            return None

    i_plant = idx("PLANT_KEY")
    i_site = idx("SITEID") or idx("SITEID ")  # defensive
    # Inverter columns may have typos: IVERTER2 etc.
    inv_cols = []
    for key in ("INVERTER1", "INVERTER2", "IVERTER2", "INVERTER3", "IVERTER3", "INVERTER4", "IVERTER4"):
        j = idx(key)
        if j is not None and j not in inv_cols:
            inv_cols.append(j)

    if i_plant is None or i_site is None or not inv_cols:
        raise RuntimeError(f"SNAP header missing required columns. Found header={header}")

    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if len(r) <= max([i_plant, i_site] + inv_cols):
            continue

        plant_key = normalize_text(r[i_plant])
        siteid = to_text_station_code(r[i_site])
        if not plant_key or not siteid or not is_huawei_station_code(siteid):
            continue

        sns: List[str] = []
        for j in inv_cols:
            sn = normalize_text(r[j]) if j < len(r) else ""
            if sn and is_sn_like(sn):
                sns.append(sn)

        if not sns:
            continue

        out[siteid] = {"plant_key": plant_key, "inverter_sns": sorted(list(dict.fromkeys(sns)))}

    return out


def ensure_header(sheet_id: str, tab: str) -> None:
    header = [
        "ExtractedAtUTC",
        "SiteId",
        "DeviceType",
        "DeviceSN",
        "Status",
        "UpdateTime",
        "RatedPower_W",
        "CurrentPower_W",
        "EToday_kWh",
        "EMonth_kWh",
        "ETotal_kWh",
    ]
    svc = sheets_service()
    rng = f"{tab}!A1:K1"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    existing = (resp.get("values") or [[]])[0] if resp else []
    existing = existing or []

    if len(existing) == 0:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
        LOG.info("Ensured header on tab '%s'", tab)


def append_rows(sheet_id: str, tab: str, rows: List[List[Any]]) -> None:
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


# ----------------------------
# Huawei client (thirdData)
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

    def get_dev_list(self, station_codes: List[str]) -> List[Dict[str, Any]]:
        body = {"stationCodes": ",".join(station_codes)}
        r = self.s.post(f"{self.base}/getDevList", json=body, timeout=self.timeout)
        js = r.json()
        if not js.get("success"):
            raise RuntimeError(f"getDevList failed: failCode={js.get('failCode')} message={js.get('message')}")
        data = js.get("data") or []
        return [d for d in data if isinstance(d, dict)]

    def get_dev_real_kpi(self, dev_type_id: int, dev_ids: List[str]) -> List[Dict[str, Any]]:
        # devIds often accepted as comma-separated string
        body = {"devTypeId": str(dev_type_id), "devIds": ",".join(dev_ids)}
        r = self.s.post(f"{self.base}/getDevRealKpi", json=body, timeout=self.timeout)
        js = r.json()
        if not js.get("success"):
            raise RuntimeError(f"getDevRealKpi failed: failCode={js.get('failCode')} message={js.get('message')}")
        data = js.get("data") or []
        return [d for d in data if isinstance(d, dict)]


def pick(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return None


def chunked(xs: List[str], n: int) -> List[List[str]]:
    return [xs[i : i + n] for i in range(0, len(xs), n)]


# ----------------------------
# Mapping + row building
# ----------------------------
def build_dev_maps(
    snap: Dict[str, Dict[str, Any]],
    dev_list: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[int, List[str]]]:
    """
    Returns:
      wanted_by_devid: devId -> {stationCode, sn, devTypeId, ratedPowerW?}
      dev_ids_by_type: devTypeId -> [devId...]
    """
    wanted_sns = set()
    for station, meta in snap.items():
        for sn in meta["inverter_sns"]:
            wanted_sns.add(sn)

    wanted_by_devid: Dict[str, Dict[str, Any]] = {}
    dev_ids_by_type: Dict[int, List[str]] = {}

    for d in dev_list:
        sn = normalize_text(pick(d, ["sn", "devSn", "deviceSn", "serialNum", "esn"]))
        if not sn or sn not in wanted_sns:
            continue

        dev_id = normalize_text(pick(d, ["id", "devId", "deviceId"]))
        if not dev_id:
            continue

        dev_type_id = pick(d, ["devTypeId", "typeId"])
        try:
            dev_type_id = int(dev_type_id)
        except Exception:
            continue

        station_code = normalize_text(pick(d, ["stationCode", "plantCode"]))  # best-effort
        rated = pick(d, ["ratedPower", "ratedCapacity", "capacity", "ratedPowerW"])

        wanted_by_devid[dev_id] = {
            "stationCode": station_code,
            "sn": sn,
            "devTypeId": dev_type_id,
            "ratedPowerW": safe_float(rated),
        }
        dev_ids_by_type.setdefault(dev_type_id, []).append(dev_id)

    # Dedup lists
    for t in list(dev_ids_by_type.keys()):
        dev_ids_by_type[t] = sorted(list(dict.fromkeys(dev_ids_by_type[t])))

    return wanted_by_devid, dev_ids_by_type


def parse_kpi_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes a single KPI response item into:
      devId, status, updateTime, powerW, eToday, eMonth, eTotal
    For inverters, dataItemMap commonly contains keys like:
      active_power, day_cap, month_cap, total_cap
    """
    dev_id = normalize_text(pick(item, ["devId", "id", "deviceId"]))
    status = normalize_text(pick(item, ["devStatus", "status", "workStatus"]))
    update_time = normalize_text(pick(item, ["collectTime", "updateTime", "time"]))

    m = item.get("dataItemMap") or item.get("data") or {}
    if not isinstance(m, dict):
        m = {}

    power = safe_float(pick(m, ["active_power", "activePower", "pac", "power"]))
    # Some deployments return kW; best-effort conversion if value looks like kW
    power_w: Optional[float] = None
    if power is not None:
        # if small number (<= 1000) it's likely kW; if huge already W
        power_w = power * 1000.0 if power <= 1000 else power

    e_today = safe_float(pick(m, ["day_cap", "daily_cap", "eToday", "todayEnergy"]))
    e_month = safe_float(pick(m, ["month_cap", "eMonth", "monthEnergy"]))
    e_total = safe_float(pick(m, ["total_cap", "eTotal", "totalEnergy"]))

    return {
        "devId": dev_id,
        "status": status,
        "updateTime": update_time,
        "powerW": power_w,
        "eToday": e_today,
        "eMonth": e_month,
        "eTotal": e_total,
    }


def build_rows(extracted_at: str, wanted_by_devid: Dict[str, Dict[str, Any]], kpi_items: List[Dict[str, Any]]) -> List[List[Any]]:
    rows: List[List[Any]] = []

    for item in kpi_items:
        k = parse_kpi_item(item)
        dev_id = k.get("devId") or ""
        if not dev_id or dev_id not in wanted_by_devid:
            continue

        meta = wanted_by_devid[dev_id]
        station = normalize_text(meta.get("stationCode"))
        sn = normalize_text(meta.get("sn"))
        dev_type = normalize_text(meta.get("devTypeId"))

        rated = meta.get("ratedPowerW")
        power_w = k.get("powerW")

        rows.append([
            extracted_at,
            station,
            dev_type,
            sn,
            normalize_text(k.get("status")),
            normalize_text(k.get("updateTime")),
            rated if rated is not None else "",
            power_w if power_w is not None else "",
            k.get("eToday") if k.get("eToday") is not None else "",
            k.get("eMonth") if k.get("eMonth") is not None else "",
            k.get("eTotal") if k.get("eTotal") is not None else "",
        ])

    return rows


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    setup_logging()

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z").strip()
    tab = os.getenv("HUAWEI_INVERTER_TAB", "HuaweiInverterData").strip()

    user = os.getenv("HUAWEI_USERNAME", "").strip()
    pwd = os.getenv("HUAWEI_PASSWORD", "").strip()
    if not user or not pwd:
        raise RuntimeError("Missing HUAWEI_USERNAME / HUAWEI_PASSWORD")

    base = (os.getenv("HUAWEI_BASE_URL") or "https://la5.fusionsolar.huawei.com/thirdData").rstrip("/")

    ensure_header(sheet_id, tab)

    snap = read_snap_huawei(sheet_id, snap_range)
    if not snap:
        LOG.warning("No Huawei plants found in SNAP range=%s", snap_range)
        return

    stations = sorted(list(snap.keys()))
    LOG.info("Huawei stations in SNAP: %s", ", ".join(stations))

    cli = HuaweiThirdDataClient(base, user, pwd, timeout=30)
    cli.login()
    LOG.info("✅ Huawei login OK")

    # getDevList max is commonly 100 stations per call; we'll chunk defensively
    dev_list: List[Dict[str, Any]] = []
    for group in chunked(stations, 100):
        LOG.info("Fetching getDevList for %d station(s)", len(group))
        dev_list.extend(cli.get_dev_list(group))
        time.sleep(0.3)

    LOG.info("Devices in devList: %d", len(dev_list))

    wanted_by_devid, dev_ids_by_type = build_dev_maps(snap, dev_list)
    LOG.info("Matched Huawei inverter devIds from SNAP SNs: %d", len(wanted_by_devid))

    if not wanted_by_devid:
        LOG.warning("No Huawei inverter devices matched SNAP SNs. Check SN formats / devList fields.")
        return

    extracted_at = now_utc_iso()
    all_rows: List[List[Any]] = []

    # getDevRealKpi supports up to 100 devices of the same type per call
    for dev_type_id, dev_ids in dev_ids_by_type.items():
        if not dev_ids:
            continue
        for batch in chunked(dev_ids, 100):
            LOG.info("Fetching getDevRealKpi devTypeId=%s devs=%d", dev_type_id, len(batch))
            items = cli.get_dev_real_kpi(dev_type_id, batch)
            rows = build_rows(extracted_at, wanted_by_devid, items)
            all_rows.extend(rows)
            time.sleep(0.3)

    append_rows(sheet_id, tab, all_rows)
    LOG.info("✅ Written %d rows to %s", len(all_rows), tab)


if __name__ == "__main__":
    main()
