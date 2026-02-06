#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA - Growatt Inverters (Step 2)
- Reads PlantIDs (SITEIDs) from Google Sheet range SNAP!A1:Z (or configured)
- Logs into Growatt server.growatt.com
- For each plantId:
    - Tries multiple known endpoints to obtain inverter list JSON
    - Extracts: inverter SN, alias, status, today's energy (kWh)
- Writes rows to Google Sheet tab: InverterData

Required Secrets/Vars (GitHub Actions):
- GOOGLE_CREDENTIALS   (Service Account JSON string)  <-- you use this
- GOOGLE_SHEET_ID
- GROWATT_USER
- GROWATT_PASS

Optional:
- SNAP_RANGE           default: SNAP!A1:Z
- INVERTER_SHEET_NAME  default: InverterData
- DEBUG_OUT            default: out_inverters (creates files for troubleshooting)
- MAX_PAGES            default: 10
"""

import os
import re
import json
import time
import base64
import logging
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# -------------------------
# Logging
# -------------------------
LOG = logging.getLogger("argia.growatt.inverters")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


# -------------------------
# Helpers
# -------------------------
def utc_iso_now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def sanitize_filename(s: str) -> str:
    """
    GitHub artifacts and Windows filesystems reject characters like ? : < > | * "
    So we replace everything not safe with underscore.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


def ensure_dir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def first_present(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return None


def to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.strip()
            if v == "" or v.lower() == "null":
                return None
        return float(v)
    except Exception:
        return None


# -------------------------
# Google Sheets
# -------------------------
def load_google_creds() -> Credentials:
    """
    Tomasz: You use GOOGLE_CREDENTIALS (JSON string).
    Also supports GOOGLE_CREDENTIALS_B64 just in case.
    """
    if os.getenv("GOOGLE_CREDENTIALS"):
        raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS is set but is not valid JSON. "
                "It must be the full Service Account JSON content."
            ) from e
    elif os.getenv("GOOGLE_CREDENTIALS_B64"):
        b = base64.b64decode(os.getenv("GOOGLE_CREDENTIALS_B64", "").strip())
        info = json.loads(b.decode("utf-8"))
    else:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS (or GOOGLE_CREDENTIALS_B64)")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def sheets_service():
    creds = load_google_creds()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_snap_siteids(sheet_id: str, snap_range: str) -> List[str]:
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=snap_range).execute()
    values = resp.get("values", [])
    flat = []
    for row in values:
        for cell in row:
            if cell is None:
                continue
            s = str(cell).strip()
            if not s:
                continue
            # collect integers that look like plantIds (>= 6 digits usually)
            if re.fullmatch(r"\d{6,}", s):
                flat.append(s)
    # unique + stable order
    uniq = []
    seen = set()
    for x in flat:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def write_inverter_rows(sheet_id: str, tab_name: str, rows: List[List[Any]]) -> None:
    """
    Appends rows to InverterData.
    If tab doesn't exist, user must create it manually (simple and safer).
    """
    if not rows:
        LOG.info("No rows to write.")
        return

    svc = sheets_service()
    body = {"values": rows}
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()


# -------------------------
# Growatt client
# -------------------------
@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    BASE = "https://server.growatt.com"

    def __init__(self, auth: GrowattAuth, timeout: int = 30):
        self.auth = auth
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ArgiaGrowatt/1.0",
                "Accept": "*/*",
            }
        )

    def _req(self, method: str, path: str, **kwargs) -> requests.Response:
        url = self.BASE + path
        r = self.s.request(method, url, timeout=self.timeout, allow_redirects=True, **kwargs)
        return r

    def login(self) -> None:
        r1 = self._req("GET", "/login")
        LOG.info("GET /login -> %s", r1.status_code)

        payload = {"account": self.auth.user, "password": self.auth.password}
        r2 = self._req("POST", "/login", data=payload)
        LOG.info("POST /login -> %s (len=%s)", r2.status_code, len(r2.text or ""))

        cookies = self.s.cookies.get_dict()
        if "assToken" not in cookies:
            raise RuntimeError("Login failed: assToken cookie missing")

        LOG.info("✅ Login OK (assToken present). Cookies: %s", " | ".join(sorted(cookies.keys())))

        # Touch /index to fully establish session
        r3 = self._req("GET", "/index")
        LOG.info("GET /index -> %s (len=%s)", r3.status_code, len(r3.text or ""))

    def get_pv_page_html(self, plant_id: str) -> str:
        r = self._req("GET", "/device/photovoltaic", params={"plantId": plant_id})
        LOG.info("GET /device/photovoltaic?plantId=%s -> %s (len=%s)", plant_id, r.status_code, len(r.text or ""))
        return r.text or ""

    def _try_json(self, r: requests.Response) -> Tuple[bool, Dict[str, Any]]:
        """
        Growatt sometimes returns HTML "not logged in" even with 200.
        """
        txt = r.text or ""
        if "<html" in txt.lower() and "dumpLogin" in txt:
            return False, {"_non_json": True, "text": txt}
        try:
            return True, r.json()
        except Exception:
            return False, {"_non_json": True, "text": txt}

    def _post_list(self, endpoint: str, plant_id: str, page: int) -> Tuple[bool, Dict[str, Any]]:
        """
        Tries common paging parameter names.
        """
        data_variants = [
            {"plantId": plant_id, "currPage": page, "pageSize": 50},
            {"plantId": plant_id, "pageNum": page, "pageSize": 50},
            {"plantId": plant_id, "page": page, "pageSize": 50},
            {"plantId": plant_id, "currPage": page},
            {"plantId": plant_id, "pageNum": page},
            {"plantId": plant_id, "page": page},
        ]

        for data in data_variants:
            r = self._req("POST", endpoint, data=data)
            ok, j = self._try_json(r)
            if ok and isinstance(j, dict):
                return True, j
        return False, {"_non_json": True, "text": (r.text or "")[:500]}

    def iter_all_inverters(self, plant_id: str, max_pages: int = 10, debug_out: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Main discovery:
        - Try several known endpoints. One of them works in your account (we already saw it finds 12 inverters).
        - We keep it robust and log when something fails.
        """
        endpoints = [
            "/device/getInvList",
            "/device/getInverterList",
            "/device/getInverterListData",
            "/device/getPlantInvList",
            "/newInvAPI.do?op=getInvList",
        ]

        all_items: List[Dict[str, Any]] = []
        used_endpoint: Optional[str] = None

        for ep in endpoints:
            items: List[Dict[str, Any]] = []
            for page in range(1, max_pages + 1):
                # some endpoints are GET (newInvAPI.do) but POST often works too; we attempt POST first, then GET fallback
                ok, j = self._post_list(ep, plant_id, page)

                if not ok:
                    # try GET fallback
                    r = self._req("GET", ep, params={"plantId": plant_id, "currPage": page, "pageSize": 50})
                    ok2, j2 = self._try_json(r)
                    if ok2:
                        ok, j = True, j2

                if not ok:
                    # non-json -> likely wrong endpoint; stop paging this endpoint
                    break

                # save debug payload (safe filename)
                if debug_out:
                    ensure_dir(debug_out)
                    fn = sanitize_filename(f"{plant_id}__{ep}__p{page}.json")
                    with open(os.path.join(debug_out, fn), "w", encoding="utf-8") as f:
                        json.dump(j, f, ensure_ascii=False, indent=2)

                # Find list
                data_list = None
                for key in ["datas", "data", "rows", "list", "obj", "result"]:
                    if isinstance(j.get(key), list):
                        data_list = j.get(key)
                        break

                # Sometimes payload is { "datas": [...], "count": ..., "pages": ...}
                if data_list is None:
                    # Could be nested
                    for key in ["data", "obj", "result"]:
                        node = j.get(key)
                        if isinstance(node, dict):
                            for key2 in ["datas", "rows", "list"]:
                                if isinstance(node.get(key2), list):
                                    data_list = node.get(key2)
                                    break
                        if data_list is not None:
                            break

                if not data_list:
                    # no rows => done
                    break

                # accumulate
                for it in data_list:
                    if isinstance(it, dict):
                        items.append(it)

                # stop if last page
                pages = j.get("pages") or j.get("totalPage") or j.get("pageCount")
                if pages is not None:
                    try:
                        if int(pages) <= page:
                            break
                    except Exception:
                        pass

            if items:
                used_endpoint = ep
                all_items = items
                break

        if not all_items:
            raise RuntimeError(f"Could not fetch inverter list for plantId={plant_id} (all endpoints failed)")

        LOG.info("Inverter list endpoint used for plantId=%s: %s", plant_id, used_endpoint)
        return all_items


# -------------------------
# Parsing inverter fields
# -------------------------
def extract_inverter_fields(item: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[float]]:
    """
    Returns (sn, alias, status, today_kwh)

    We try MANY key variants because Growatt varies by device type and UI language.
    """
    sn = first_present(item, ["sn", "deviceSn", "invSn", "serialNum", "serialNo", "inverterSn"])
    alias = first_present(item, ["alias", "deviceName", "name", "invName", "inverterName"])

    # Status-like fields
    status_raw = first_present(item, ["deviceStatus", "invStatus", "status", "runStatus", "lost", "faultStatus"])
    status = None
    if status_raw is not None:
        status = str(status_raw)

    # Today energy (kWh) fields
    e_today_raw = first_present(
        item,
        [
            "etoday", "eToday", "EToday", "todayEnergy", "energyToday", "energy_today",
            "today_energy", "eTodayStr", "etoday_kwh", "e_today", "etodayEnergy"
        ],
    )
    today_kwh = to_float(e_today_raw)

    return sn, alias, status, today_kwh


# -------------------------
# Main
# -------------------------
def main() -> None:
    LOG.info("=== GROWATT INVERTERS START %s ===", utc_iso_now())

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z").strip()
    inv_tab = os.getenv("INVERTER_SHEET_NAME", "InverterData").strip()
    max_pages = int(os.getenv("MAX_PAGES", "10").strip() or "10")
    debug_out = os.getenv("DEBUG_OUT", "out_inverters").strip()

    user = os.getenv("GROWATT_USER", "").strip()
    pwd = os.getenv("GROWATT_PASS", "").strip()
    if not user or not pwd:
        raise RuntimeError("Missing GROWATT_USER or GROWATT_PASS")

    siteids = read_snap_siteids(sheet_id, snap_range)
    if not siteids:
        raise RuntimeError(f"No SITEIDs found in range {snap_range}")
    LOG.info("Loaded %s SITEIDs from SNAP: %s", len(siteids), ", ".join(siteids))

    cli = GrowattMonitoringClient(GrowattAuth(user=user, password=pwd))
    cli.login()

    today = dt.date.today().isoformat()
    out_rows: List[List[Any]] = []

    # Header suggestion (create this row once manually in sheet if you want):
    # Date | PlantId | InverterSN | InverterAlias | Status | Today_kWh
    for plant_id in siteids:
        LOG.info("==============================================")
        LOG.info("🏭 PlantId=%s", plant_id)

        # Touch PV page (helps session for some accounts)
        _ = cli.get_pv_page_html(plant_id)

        inv_items = cli.iter_all_inverters(plant_id, max_pages=max_pages, debug_out=debug_out)

        # We want unique by SN
        by_sn: Dict[str, Dict[str, Any]] = {}
        for it in inv_items:
            sn, _, _, _ = extract_inverter_fields(it)
            if sn:
                by_sn[sn] = it

        LOG.info("Found %s inverters for plantId=%s", len(by_sn) if by_sn else len(inv_items), plant_id)

        # Build rows
        for it in (by_sn.values() if by_sn else inv_items):
            sn, alias, status, today_kwh = extract_inverter_fields(it)

            # If energy/status missing, log a quick hint (but still write row)
            if today_kwh is None or status is None:
                # Show available keys to help mapping if needed
                keys_preview = ", ".join(sorted(list(it.keys()))[:40])
                LOG.info(
                    "Note: Missing fields for plantId=%s inverter=%s (status=%s today_kwh=%s). Keys(sample)=%s",
                    plant_id, sn or "?", status, today_kwh, keys_preview
                )

            out_rows.append([today, plant_id, sn or "", alias or "", status or "", today_kwh if today_kwh is not None else ""])

        # be nice to Growatt
        time.sleep(2)

    write_inverter_rows(sheet_id, inv_tab, out_rows)
    LOG.info("✅ Wrote %s rows to %s", len(out_rows), inv_tab)
    LOG.info("=== GROWATT INVERTERS END ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        LOG.exception("FAILED: %s", e)
        raise
