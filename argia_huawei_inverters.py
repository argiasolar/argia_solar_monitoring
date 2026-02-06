#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA – Huawei Inverter Snapshot (10-min)

Reads Huawei plants + inverter SNs from SNAP, filters by SNAP.Brand == "HUAWEI",
then queries FusionSolar thirdData:

  1) POST /thirdData/login
  2) POST /thirdData/getDevRealKpi using SNS (not devIds)
     - This avoids dependence on getDevList SN field naming differences.

Writes rows into Google Sheets tab (default: HuaweiInverterData) with schema:
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


def chunked(xs: List[str], n: int) -> List[List[str]]:
    return [xs[i : i + n] for i in range(0, len(xs), n)]


def qrange(tab: str, a1: str) -> str:
    # Always quote tab name to avoid parse issues
    return f"'{tab}'!{a1}"


def is_sn_like(x: str) -> bool:
    # Huawei inverter SNs in your SNAP look like: ES2470051825 / GR2499018270
    return bool(re.fullmatch(r"[A-Za-z0-9]{8,32}", x or ""))


def is_huawei_station_code(siteid: str) -> bool:
    s = normalize_text(siteid)
    return s.startswith("NE=") or bool(re.fullmatch(r"[A-Za-z]{2}=\d+", s))


def pick(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return None


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


def ensure_sheet_exists(sheet_id: str, tab: str) -> None:
    svc = sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets = meta.get("sheets", []) or []
    titles = {s.get("properties", {}).get("title") for s in sheets}
    if tab in titles:
        return

    req = {"requests": [{"addSheet": {"properties": {"title": tab}}}]}
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=req).execute()
    LOG.info("Created missing sheet tab '%s'", tab)


def ensure_header(sheet_id: str, tab: str) -> None:
    """
    Ensures tab exists and header row A1:K1 is present.
    """
    ensure_sheet_exists(sheet_id, tab)

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
    rng = qrange(tab, "A1:K1")
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    existing = (resp.get("values") or [[]])[0] if resp else []
    existing = existing or []

    if len(existing) == 0:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=qrange(tab, "A1"),
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
        range=qrange(tab, "A1"),
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def read_snap_huawei(sheet_id: str, snap_range: str) -> Dict[str, Dict[str, Any]]:
    """
    Reads SNAP and returns dict keyed by stationCode (SiteId NE=...):
      {
        "NE=35314736": {"plant_key": "MEX1", "inverter_sns": ["ES..", "GR..", ...]},
        ...
      }

    Requires SNAP columns:
    - Plant_Key
    - SITEID
    - Brand  (must be "HUAWEI" to include)
    - INVERTER1..4 (supports common typos like IVERTER2)
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
    i_site = idx("SITEID")
    i_brand = idx("BRAND")

    inv_cols: List[int] = []
    for key in ("INVERTER1", "INVERTER2", "IVERTER2", "INVERTER3", "IVERTER3", "INVERTER4", "IVERTER4"):
        j = idx(key)
        if j is not None and j not in inv_cols:
            inv_cols.append(j)

    if i_plant is None or i_site is None or i_brand is None:
        raise RuntimeError(f"SNAP header missing Plant_Key / SITEID / Brand. Found header={header}")
    if not inv_cols:
        raise RuntimeError(f"SNAP header missing inverter columns. Found header={header}")

    out: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        if len(r) <= max([i_plant, i_site, i_brand] + inv_cols):
            continue

        plant_key = normalize_text(r[i_plant])
        siteid = normalize_text(r[i_site])
        brand = normalize_text(r[i_brand]).upper()

        if not plant_key or not siteid:
            continue
        if brand != "HUAWEI":
            continue
        if not is_huawei_station_code(siteid):
            # defensive: only keep NE=... for Huawei
            continue

        sns: List[str] = []
        for j in inv_cols:
            sn = normalize_text(r[j]) if j < len(r) else ""
            if sn and is_sn_like(sn):
                sns.append(sn)

        sns = sorted(list(dict.fromkeys(sns)))
        if not sns:
            continue

        out[siteid] = {"plant_key": plant_key, "inverter_sns": sns}

    return out


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

    def get_dev_real_kpi_by_sns(self, dev_type_id: int, sns: List[str]) -> List[Dict[str, Any]]:
        """
        Query KPI using SNS list (robust vs devId mapping differences).
        """
        body = {"devTypeId": str(dev_type_id), "sns": ",".join(sns)}
        r = self.s.post(f"{self.base}/getDevRealKpi", json=body, timeout=self.timeout)
        js = r.json()
        if not js.get("success"):
            raise RuntimeError(f"getDevRealKpi failed: failCode={js.get('failCode')} message={js.get('message')}")
        data = js.get("data") or []
        return [d for d in data if isinstance(d, dict)]


# ----------------------------
# KPI parsing
# ----------------------------
def parse_kpi_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes a single KPI response item.

    Common structure:
      {
        "sn": "...",
        "devStatus": ...,
        "collectTime": ...,
        "dataItemMap": {
            "active_power": ... (often kW)
            "day_cap": ... (kWh)
            "month_cap": ... (kWh)
            "total_cap": ... (kWh)
        }
      }
    """
    sn = normalize_text(pick(item, ["sn", "devSn", "deviceSn", "serialNum", "esn"]))

    status = normalize_text(pick(item, ["devStatus", "status", "workStatus"]))
    update_time = normalize_text(pick(item, ["collectTime", "updateTime", "time"]))

    m = item.get("dataItemMap") or item.get("data") or {}
    if not isinstance(m, dict):
        m = {}

    power = safe_float(pick(m, ["active_power", "activePower", "pac", "power"]))
    # best-effort: if <= 1000 assume kW, else already W
    power_w: Optional[float] = None
    if power is not None:
        power_w = power * 1000.0 if power <= 1000 else power

    e_today = safe_float(pick(m, ["day_cap", "daily_cap", "eToday", "todayEnergy"]))
    e_month = safe_float(pick(m, ["month_cap", "eMonth", "monthEnergy"]))
    e_total = safe_float(pick(m, ["total_cap", "eTotal", "totalEnergy"]))

    return {
        "sn": sn,
        "status": status,
        "updateTime": update_time,
        "powerW": power_w,
        "eToday": e_today,
        "eMonth": e_month,
        "eTotal": e_total,
    }


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
        LOG.warning("No Huawei plants found in SNAP (Brand=HUAWEI) range=%s", snap_range)
        return

    stations = sorted(list(snap.keys()))
    LOG.info("Huawei stations in SNAP: %s", ", ".join(stations))

    cli = HuaweiThirdDataClient(base, user, pwd, timeout=30)
    cli.login()
    LOG.info("✅ Huawei login OK")

    extracted_at = now_utc_iso()
    all_rows: List[List[Any]] = []

    DEVTYPE_INVERTER = int(os.getenv("HUAWEI_INVERTER_DEVTYPE", "1").strip())  # usually 1 for inverters

    for station_code, meta in snap.items():
        sns = meta.get("inverter_sns") or []
        if not sns:
            continue

        # Keep calls small; Huawei rate limits can be strict
        for batch in chunked(sns, 50):
            LOG.info("Fetching getDevRealKpi station=%s sns=%d", station_code, len(batch))
            items = cli.get_dev_real_kpi_by_sns(DEVTYPE_INVERTER, batch)

            for it in items:
                k = parse_kpi_item(it)
                sn = k["sn"]
                if not sn:
                    continue

                all_rows.append([
                    extracted_at,
                    station_code,               # SiteId (NE=...)
                    str(DEVTYPE_INVERTER),      # DeviceType
                    sn,                         # DeviceSN
                    k["status"],
                    k["updateTime"],
                    "",                         # RatedPower_W (unknown unless you add getDevList mapping later)
                    k["powerW"] if k["powerW"] is not None else "",
                    k["eToday"] if k["eToday"] is not None else "",
                    k["eMonth"] if k["eMonth"] is not None else "",
                    k["eTotal"] if k["eTotal"] is not None else "",
                ])

            time.sleep(0.25)

    append_rows(sheet_id, tab, all_rows)
    LOG.info("✅ Written %d rows to %s", len(all_rows), tab)


if __name__ == "__main__":
    main()
