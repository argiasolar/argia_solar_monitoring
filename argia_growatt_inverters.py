#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA - Growatt Inverters (per-inverter daily snapshot)

Goal:
- Keep your existing plant daily script untouched.
- This script ONLY writes per-inverter rows into Google Sheet tab: "InverterData"

What it records per row:
- ExtractedAt (timestamp when script ran)
- Date (local date for Mexico City timezone)
- PlantId
- InverterSN
- InverterAlias
- Status
- Today_kWh

Auth:
- Growatt: GROWATT_USER / GROWATT_PASS
- Google Sheets: GOOGLE_CREDENTIALS (service account JSON TEXT), GOOGLE_SHEET_ID

How it finds inverter list:
- login
- for each plantId:
    - GET /device?plantId=...
    - GET /device/getInverterPage?plantId=...
    - POST /device/getInverterList (most common) with paging
Fallback endpoints are attempted if needed.
"""

import os
import re
import json
import time
import base64
import logging
import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------
# Logging
# ---------------------------

LOG = logging.getLogger("argia.growatt.inverters")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# ---------------------------
# Constants
# ---------------------------

BASE = "https://server.growatt.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) ARGIA/1.0"

DEFAULT_SNAP_RANGE = os.getenv("SNAP_RANGE", "SNAP!A1:Z")
DEFAULT_INVERTER_SHEET = os.getenv("INVERTER_SHEET", "InverterData")

# Mexico City timezone: UTC-6 (your logs show timezone "-6" on devices)
MX_TZ = datetime.timezone(datetime.timedelta(hours=-6))


# ---------------------------
# Helpers
# ---------------------------

def now_mx() -> datetime.datetime:
    return datetime.datetime.now(tz=MX_TZ)

def iso_ts(dt: datetime.datetime) -> str:
    return dt.isoformat(timespec="seconds")

def safe_filename(s: str) -> str:
    """
    GitHub upload-artifact forbids:  " : < > | * ? \r \n
    Replace '?' and other bad chars so artifacts never fail.
    """
    return re.sub(r'[\"\:<>|\*\?\r\n]+', "_", s)

def pick(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return None

def to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "":
            return None
        return float(s)
    except Exception:
        return None

def parse_json_or_text(resp: requests.Response) -> Dict[str, Any]:
    """
    Growatt sometimes returns HTML login page with 200.
    We detect JSON by attempting parsing.
    """
    text = resp.text or ""
    try:
        return resp.json()
    except Exception:
        return {"_non_json": True, "text": text}


# ---------------------------
# Google Sheets
# ---------------------------

def load_google_creds():
    raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS (service account JSON text).")

    # Some people store as base64 – if it looks like base64, decode.
    if raw.startswith("{"):
        data = json.loads(raw)
    else:
        try:
            decoded = base64.b64decode(raw).decode("utf-8")
            data = json.loads(decoded)
        except Exception:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS must be service account JSON text OR base64(JSON)."
            )

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = service_account.Credentials.from_service_account_info(data, scopes=scopes)
    return creds

def sheets_service():
    creds = load_google_creds()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def read_snap_siteids(sheet_id: str, snap_range: str) -> List[str]:
    svc = sheets_service()
    res = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=snap_range).execute()
    values = res.get("values", []) or []

    siteids = set()
    for row in values:
        for cell in row:
            s = str(cell).strip()
            if s.isdigit() and len(s) >= 6:
                siteids.add(s)

    # Stable order
    out = sorted(siteids)
    LOG.info("Loaded %d SITEIDs from SNAP: %s", len(out), ", ".join(out))
    return out

def ensure_sheet_header(sheet_id: str, tab_name: str, header: List[str]) -> None:
    svc = sheets_service()
    # Read first row
    res = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"{tab_name}!A1:Z1").execute()
    vals = res.get("values", [])
    if vals and vals[0] == header:
        return
    # Write header
    body = {"values": [header]}
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="RAW",
        body=body,
    ).execute()
    LOG.info("Ensured header on tab '%s'", tab_name)

def append_rows(sheet_id: str, tab_name: str, rows: List[List[Any]]) -> None:
    if not rows:
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


# ---------------------------
# Growatt client
# ---------------------------

@dataclass
class GrowattAuth:
    user: str
    password: str

class GrowattMonitoringClient:
    def __init__(self, auth: GrowattAuth, timeout: int = 30):
        self.auth = auth
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.timeout = timeout

    def _get(self, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else BASE + path
        resp = self.s.get(url, timeout=self.timeout, allow_redirects=True, **kwargs)
        return resp

    def _post(self, path: str, data: Dict[str, Any], **kwargs) -> requests.Response:
        url = path if path.startswith("http") else BASE + path
        headers = kwargs.pop("headers", {})
        headers.setdefault("X-Requested-With", "XMLHttpRequest")
        resp = self.s.post(url, data=data, headers=headers, timeout=self.timeout, allow_redirects=True, **kwargs)
        return resp

    def login(self) -> None:
        r1 = self._get("/login")
        LOG.info("GET /login -> %s", r1.status_code)

        payload = {"account": self.auth.user, "password": self.auth.password}
        r2 = self._post("/login", payload)
        LOG.info("POST /login -> %s (len=%s)", r2.status_code, len(r2.text or ""))

        cookies = self.s.cookies.get_dict()
        if "assToken" not in cookies:
            raise RuntimeError("Login failed: assToken cookie missing")
        LOG.info("✅ Login OK (assToken present). Cookies: %s", " | ".join(sorted(cookies.keys())))

    def set_plant_context(self, plant_id: str) -> None:
        """
        Growatt often needs plant context in session to make list endpoints return data.
        """
        r = self._get(f"/device?plantId={plant_id}")
        LOG.debug("GET /device?plantId=%s -> %s (len=%s)", plant_id, r.status_code, len(r.text or ""))

    def get_inverter_page_html(self, plant_id: str) -> str:
        r = self._get(f"/device/getInverterPage?plantId={plant_id}")
        if r.status_code != 200:
            raise RuntimeError(f"GET /device/getInverterPage failed: {r.status_code}")
        return r.text or ""

    def discover_endpoints_from_html(self, html: str) -> List[str]:
        # find /device/xxx endpoints inside html/js
        eps = sorted(set(re.findall(r"(/device/[A-Za-z0-9_]+)", html)))
        return eps

    def try_inverter_list_once(self, endpoint: str, page: int, page_size: int) -> Dict[str, Any]:
        """
        Different pages use different param names. We try the most common ones.
        """
        payload_variants = [
            {"currPage": page, "pageSize": page_size},
            {"currentPage": page, "pageSize": page_size},
            {"page": page, "pageSize": page_size},
            {"currPage": page, "pageSize": page_size, "toList": "true"},
        ]

        last = None
        for p in payload_variants:
            r = self._post(endpoint, p)
            data = parse_json_or_text(r)
            last = data
            # If JSON with datas list returned, accept
            if not data.get("_non_json") and isinstance(data.get("datas"), list):
                return data
        return last or {"_non_json": True, "text": ""}

    def iter_all_inverters(self, plant_id: str, max_pages: int = 10, page_size: int = 20) -> List[Dict[str, Any]]:
        """
        Main method:
        - set plant context
        - load inverter page
        - discover endpoint candidates
        - call list endpoint with paging
        """
        self.set_plant_context(plant_id)

        inv_html = self.get_inverter_page_html(plant_id)
        eps = self.discover_endpoints_from_html(inv_html)

        # Candidate endpoints in typical Growatt device UI
        candidates = []
        for e in eps:
            if "Inverter" in e or "inverter" in e:
                candidates.append(e)
        # add known common endpoint even if not in HTML
        candidates += ["/device/getInverterList", "/device/getInvList"]
        candidates = [c for i, c in enumerate(candidates) if c not in candidates[:i]]

        # We want the list endpoint (returns JSON with datas)
        list_candidates = [c for c in candidates if "List" in c or "list" in c]
        if not list_candidates:
            list_candidates = candidates

        all_items: List[Dict[str, Any]] = []
        used_endpoint = None

        for endpoint in list_candidates:
            # Try first page to see if it returns datas
            data = self.try_inverter_list_once(endpoint, page=1, page_size=page_size)
            if data.get("_non_json"):
                continue
            if not isinstance(data.get("datas"), list):
                continue

            used_endpoint = endpoint
            datas = data.get("datas") or []
            all_items.extend(datas)

            # Paging
            pages = data.get("pages")
            if isinstance(pages, str) and pages.isdigit():
                pages = int(pages)
            if isinstance(pages, (int, float)) and pages > 1:
                pages_int = int(pages)
            else:
                pages_int = 1

            for p in range(2, min(pages_int, max_pages) + 1):
                d2 = self.try_inverter_list_once(endpoint, page=p, page_size=page_size)
                if d2.get("_non_json"):
                    break
                ds2 = d2.get("datas") or []
                if not ds2:
                    break
                all_items.extend(ds2)

            break

        if used_endpoint is None:
            # Debug: show first 200 chars of HTML returned (likely login page)
            raise RuntimeError(f"Could not find JSON inverter list endpoint for plantId={plant_id}")

        # De-dup by SN if present
        seen = set()
        out = []
        for it in all_items:
            sn = str(pick(it, ["sn", "deviceSn", "invSn", "inverterSn", "serialNum"]) or "").strip()
            key = sn or json.dumps(it, sort_keys=True)[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(it)

        return out


# ---------------------------
# Parsing inverter fields
# ---------------------------

def normalize_status(raw: Any) -> str:
    """
    Growatt uses numeric codes sometimes. We keep raw but map common ones.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    mapping = {
        "1": "Online",
        "0": "Offline",
        "2": "Alarm",
        "3": "Fault",
        "4": "Unknown",
    }
    return mapping.get(s, s)

def extract_inverter_row(plant_id: str, item: Dict[str, Any], extracted_at: datetime.datetime) -> List[Any]:
    extracted_at_s = iso_ts(extracted_at)
    date_s = extracted_at.date().isoformat()

    sn = pick(item, ["sn", "deviceSn", "invSn", "inverterSn", "serialNum", "invsn"])
    alias = pick(item, ["alias", "deviceAilas", "invAlias", "inverterAlias", "name", "invName"])

    # These key names vary; try many
    status = pick(item, ["deviceStatus", "status", "invStatus", "inverterStatus", "lost", "onlineStatus"])
    status_s = normalize_status(status)

    # Today energy - common keys
    # Sometimes it comes as "eToday" or "etoday" or "etodayEnergy" etc.
    today_kwh = pick(item, ["eToday", "etoday", "e_today", "todayEnergy", "etodayEnergy", "eTodayKwh", "eToday_kWh", "energyToday"])
    today_kwh_f = to_float(today_kwh)

    return [
        extracted_at_s,
        date_s,
        str(plant_id),
        "" if sn is None else str(sn),
        "" if alias is None else str(alias),
        status_s,
        "" if today_kwh_f is None else today_kwh_f,
    ]


# ---------------------------
# Main
# ---------------------------

def main():
    try:
        LOG.info("This script expects GOOGLE_CREDENTIALS (not GOOGLE_SA_JSON/GOOGLE_SA_B64)")

        growatt_user = os.getenv("GROWATT_USER", "").strip()
        growatt_pass = os.getenv("GROWATT_PASS", "").strip()
        sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()

        if not growatt_user or not growatt_pass:
            raise RuntimeError("Missing GROWATT_USER or GROWATT_PASS")
        if not sheet_id:
            raise RuntimeError("Missing GOOGLE_SHEET_ID")
        # Will raise if missing
        _ = load_google_creds()

        snap_range = os.getenv("SNAP_RANGE", DEFAULT_SNAP_RANGE)
        inverter_tab = os.getenv("INVERTER_SHEET", DEFAULT_INVERTER_SHEET)

        # 1) Read plant IDs (SITEIDs) from SNAP
        siteids = read_snap_siteids(sheet_id, snap_range)
        if not siteids:
            raise RuntimeError("No SITEIDs found in SNAP range")

        # 2) Login to Growatt
        cli = GrowattMonitoringClient(GrowattAuth(user=growatt_user, password=growatt_pass))
        cli.login()

        # 3) Ensure sheet header
        header = ["ExtractedAt", "Date", "PlantId", "InverterSN", "InverterAlias", "Status", "Today_kWh"]
        ensure_sheet_header(sheet_id, inverter_tab, header)

        # 4) Collect rows
        extracted_at = now_mx()
        out_rows: List[List[Any]] = []

        for plant_id in siteids:
            LOG.info("==============================================")
            LOG.info("🏭 PlantId=%s", plant_id)

            inv_items = cli.iter_all_inverters(plant_id, max_pages=10, page_size=20)
            LOG.info("Found %d inverters for plantId=%s", len(inv_items), plant_id)

            for it in inv_items:
                out_rows.append(extract_inverter_row(plant_id, it, extracted_at))

            # be nice to server
            time.sleep(1)

        # 5) Write rows
        append_rows(sheet_id, inverter_tab, out_rows)
        LOG.info("✅ Wrote %d rows to %s", len(out_rows), inverter_tab)

    except Exception as e:
        LOG.exception("FAILED: %s", str(e))
        raise


if __name__ == "__main__":
    main()
