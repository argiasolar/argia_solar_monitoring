#!/usr/bin/env python3
"""
ARGIA - Growatt Inverter Snapshot -> Google Sheets (InverterData)

What it does:
- Logs in to https://server.growatt.com (web session)
- Loads plantIds (SITEIDs) from SNAP sheet in your Google Spreadsheet
- For each plantId:
    - GET /device/photovoltaic?plantId=...   (keeps session/context)
    - Fetch inverter list via /newInvAPI.do?op=getInvList&plantId=...
- Appends a snapshot row per inverter into sheet tab: InverterData

ENV REQUIRED:
- GROWATT_USER
- GROWATT_PASS
- GOOGLE_SHEET_ID
- GOOGLE_SA_JSON   (preferred)  OR GOOGLE_SA_B64  (base64 of SA json)

ENV OPTIONAL:
- SNAP_RANGE        default: "SNAP!A1:Z"
- INVERTER_SHEET    default: "InverterData"
- TIMEZONE_OFFSET   default: "-06:00"  (Mexico City; only used for display timestamp)
- GROWATT_BASE_URL  default: "https://server.growatt.com"
- DEBUG             default: "0"
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

# Google Sheets
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# ----------------------------
# Logging
# ----------------------------

def setup_logging() -> None:
    level = logging.DEBUG if os.getenv("DEBUG", "0").strip() == "1" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


log = logging.getLogger("argia.growatt.inverters")


# ----------------------------
# Helpers
# ----------------------------

def must_getenv(name: str) -> str:
    v = os.getenv(name)
    if not v or not v.strip():
        raise RuntimeError(f"Missing {name}")
    return v.strip()


def now_local_iso(tz_offset: str = "-06:00") -> str:
    # tz_offset like "-06:00"
    m = re.match(r"^([+-])(\d{2}):(\d{2})$", tz_offset.strip())
    if not m:
        # fallback UTC
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    delta = timedelta(hours=hh, minutes=mm)
    if sign == "-":
        delta = -delta
    tz = timezone(delta)
    return datetime.now(tz).replace(microsecond=0).isoformat()


def safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "" or s.lower() in {"null", "none", "nan"}:
            return None
        return float(s)
    except Exception:
        return None


def safe_str(x: Any) -> str:
    if x is None:
        return ""
    s = str(x)
    if s.lower() == "null":
        return ""
    return s


def first_key(d: Dict[str, Any], keys: List[str]) -> Any:
    for k in keys:
        if k in d:
            return d.get(k)
    return None


# ----------------------------
# Google Sheets
# ----------------------------

def load_google_creds() -> Credentials:
    """
    Prefer GOOGLE_SA_JSON. If not present, use GOOGLE_SA_B64.
    """
    sa_json = os.getenv("GOOGLE_SA_JSON", "").strip()
    sa_b64 = os.getenv("GOOGLE_SA_B64", "").strip()

    if sa_json:
        info = json.loads(sa_json)
    elif sa_b64:
        raw = base64.b64decode(sa_b64.encode("utf-8")).decode("utf-8")
        info = json.loads(raw)
    else:
        raise RuntimeError("Missing GOOGLE_SA_JSON or GOOGLE_SA_B64")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def sheets_service() -> Any:
    creds = load_google_creds()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_snap_siteids(sheet_id: str, snap_range: str) -> List[str]:
    """
    Reads SNAP!A1:Z and returns unique SITEIDs.

    We look for SITEID values anywhere in the table:
    - If there is a header row containing "SITEID" we take that column.
    - Otherwise: we take any cell that looks like a numeric plantId (7-10 digits).
    """
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=snap_range).execute()
    values = resp.get("values", []) or []
    if not values:
        return []

    # Find header col index for SITEID if present
    header = [c.strip().upper() for c in values[0]]
    site_col = None
    for i, h in enumerate(header):
        if h in {"SITEID", "SITE_ID", "PLANTID", "PLANT_ID"}:
            site_col = i
            break

    siteids: List[str] = []
    plant_re = re.compile(r"^\d{7,10}$")

    for r_idx, row in enumerate(values[1:] if site_col is not None else values):
        if site_col is not None:
            if site_col < len(row):
                v = (row[site_col] or "").strip()
                if plant_re.match(v):
                    siteids.append(v)
        else:
            for cell in row:
                v = (cell or "").strip()
                if plant_re.match(v):
                    siteids.append(v)

    # unique while keeping order
    seen = set()
    out = []
    for x in siteids:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def ensure_sheet_has_header(sheet_id: str, tab_name: str, header: List[str]) -> None:
    """
    If A1 is empty, write header row into A1:...
    """
    svc = sheets_service()
    rng = f"{tab_name}!A1:Z1"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    values = resp.get("values", []) or []
    if values and values[0] and any(str(x).strip() for x in values[0]):
        return

    body = {"values": [header]}
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="RAW",
        body=body
    ).execute()
    log.info("Header written to %s!A1", tab_name)


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
        body=body
    ).execute()
    log.info("Appended %d rows -> %s", len(rows), tab_name)


# ----------------------------
# Growatt Web Client
# ----------------------------

@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattWeb:
    """
    Minimal, robust Growatt web-session client.
    """
    def __init__(self, auth: GrowattAuth, base_url: str = "https://server.growatt.com", timeout: int = 30) -> None:
        self.auth = auth
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (ARGIA Monitoring Bot)",
            "Accept": "*/*",
            "Connection": "keep-alive",
        })
        self._logged_in = False

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def login(self) -> None:
        # 1) GET /login
        r1 = self.s.get(self._url("/login"), timeout=self.timeout)
        log.info("GET /login -> %s", r1.status_code)
        r1.raise_for_status()

        # 2) POST /login
        payload = {
            "account": self.auth.user,
            "password": self.auth.password,
        }
        r2 = self.s.post(self._url("/login"), data=payload, timeout=self.timeout)
        log.info("POST /login -> %s (len=%s)", r2.status_code, len(r2.text or ""))
        r2.raise_for_status()

        ck = self.s.cookies.get_dict()
        if "assToken" not in ck:
            raise RuntimeError("Login failed: assToken cookie missing")
        self._logged_in = True
        log.info("✅ Login OK (assToken present). Cookies: %s", " | ".join(sorted(ck.keys())))

    def warmup_device_context(self, plant_id: str) -> None:
        """
        Important: call device pages with plantId to "set context" for subsequent API calls.
        """
        # /device?plantId=...
        r1 = self.s.get(self._url("/device"), params={"plantId": plant_id}, timeout=self.timeout)
        log.debug("GET /device?plantId=%s -> %s", plant_id, r1.status_code)
        r1.raise_for_status()

        # /device/photovoltaic?plantId=...
        r2 = self.s.get(self._url("/device/photovoltaic"), params={"plantId": plant_id}, timeout=self.timeout)
        log.info("GET /device/photovoltaic?plantId=%s -> %s (len=%s)", plant_id, r2.status_code, len(r2.text or ""))
        r2.raise_for_status()

    def _get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Growatt sometimes returns HTML (login redirect) when auth is not accepted.
        This method:
          - tries GET
          - if not JSON, tries POST
          - if still not JSON, returns {"_non_json": True, "text": "..."}
        """
        # GET
        r = self.s.get(self._url(path), params=params, timeout=self.timeout)
        ct = (r.headers.get("Content-Type") or "").lower()
        if "application/json" in ct:
            return r.json()
        try:
            return r.json()
        except Exception:
            pass

        txt = r.text or ""
        if "<html" in txt.lower():
            # try POST
            r2 = self.s.post(self._url(path), data=params, timeout=self.timeout)
            ct2 = (r2.headers.get("Content-Type") or "").lower()
            if "application/json" in ct2:
                return r2.json()
            try:
                return r2.json()
            except Exception:
                return {"_non_json": True, "text": (r2.text or "")[:2000]}
        return {"_non_json": True, "text": txt[:2000]}

    def get_inverter_list(self, plant_id: str, page: int = 1) -> Dict[str, Any]:
        """
        Endpoint discovered from Growatt UI:
          /newInvAPI.do?op=getInvList

        We include plantId + currPage/pageSize typical patterns.
        """
        params_variants = [
            {"op": "getInvList", "plantId": plant_id, "currPage": page},
            {"op": "getInvList", "plantId": plant_id, "page": page},
            {"op": "getInvList", "plantId": plant_id, "currentPage": page},
            {"op": "getInvList", "plantId": plant_id, "currPage": page, "pageSize": 50},
        ]
        last = None
        for params in params_variants:
            data = self._get_json("/newInvAPI.do", params=params)
            last = data
            if not data.get("_non_json"):
                return data
        return last or {"_non_json": True, "text": "No response"}

    def iter_all_inverters(self, plant_id: str, max_pages: int = 10) -> List[Dict[str, Any]]:
        all_items: List[Dict[str, Any]] = []

        for p in range(1, max_pages + 1):
            data = self.get_inverter_list(plant_id, page=p)
            if data.get("_non_json"):
                raise RuntimeError(f"Growatt returned non-JSON for inverter list plantId={plant_id}: {data.get('text','')[:200]}")

            # Common shapes:
            # { currPage, pages, pageSize, count, datas:[...] }
            datas = data.get("datas") or data.get("data") or data.get("rows") or []
            if not isinstance(datas, list):
                datas = []

            all_items.extend([x for x in datas if isinstance(x, dict)])

            pages = data.get("pages")
            try:
                pages_i = int(pages) if pages is not None else p
            except Exception:
                pages_i = p

            if p >= pages_i or not datas:
                break

        return all_items


# ----------------------------
# Main
# ----------------------------

def main() -> None:
    setup_logging()

    sheet_id = must_getenv("GOOGLE_SHEET_ID")
    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z").strip()
    tab_name = os.getenv("INVERTER_SHEET", "InverterData").strip()
    tz_offset = os.getenv("TIMEZONE_OFFSET", "-06:00").strip()

    user = must_getenv("GROWATT_USER")
    pwd = must_getenv("GROWATT_PASS")
    base_url = os.getenv("GROWATT_BASE_URL", "https://server.growatt.com").strip()

    # 1) Read plant IDs from SNAP
    siteids = read_snap_siteids(sheet_id, snap_range)
    if not siteids:
        raise RuntimeError(f"No SITEIDs found in {snap_range}. Check SNAP sheet format.")
    log.info("Loaded %d SITEIDs from SNAP: %s", len(siteids), ", ".join(siteids))

    # 2) Ensure header exists in InverterData
    header = [
        "Timestamp",
        "PlantId",
        "PlantName",
        "InverterSN",
        "InverterAlias",
        "Status",
        "Pac_W",
        "Today_kWh",
        "Total_kWh",
        "RawJSON",
    ]
    ensure_sheet_has_header(sheet_id, tab_name, header)

    # 3) Login
    cli = GrowattWeb(GrowattAuth(user=user, password=pwd), base_url=base_url)
    cli.login()

    # 4) For each plant -> pull inverter list -> append rows
    ts = now_local_iso(tz_offset)
    out_rows: List[List[Any]] = []

    for plant_id in siteids:
        log.info("==============================================")
        log.info("🏭 PlantId=%s", plant_id)

        # Keep Growatt context for the plant
        cli.warmup_device_context(plant_id)

        inv_items = cli.iter_all_inverters(plant_id, max_pages=10)
        log.info("Found %d inverters for plantId=%s", len(inv_items), plant_id)

        for inv in inv_items:
            # Try to capture common fields from different Growatt responses
            inv_sn = safe_str(first_key(inv, ["sn", "invSn", "deviceSn", "serialNum", "inverterSn"]))
            inv_alias = safe_str(first_key(inv, ["alias", "name", "deviceName", "invName"]))
            plant_name = safe_str(first_key(inv, ["plantName", "plant_name"]))
            status = safe_str(first_key(inv, ["status", "deviceStatus", "invStatus", "lost"]))  # may be numeric/boolean/string
            pac = safe_float(first_key(inv, ["pac", "power", "powerW", "outputPower", "acPower"]))
            etoday = safe_float(first_key(inv, ["etoday", "eToday", "todayEnergy", "etodayEnergy", "e_day"]))
            etotal = safe_float(first_key(inv, ["etotal", "eTotal", "totalEnergy", "etotalEnergy", "e_total"]))

            # Some responses embed numbers as strings with units; try a quick unit strip
            def strip_units(v: Any) -> Optional[float]:
                if v is None:
                    return None
                s = str(v).strip()
                s = s.replace("kWh", "").replace("KWH", "").replace("W", "").strip()
                return safe_float(s)

            if etoday is None:
                etoday = strip_units(first_key(inv, ["etoday", "todayEnergy"]))
            if etotal is None:
                etotal = strip_units(first_key(inv, ["etotal", "totalEnergy"]))
            if pac is None:
                pac = strip_units(first_key(inv, ["pac", "power", "acPower"]))

            raw_compact = json.dumps(inv, ensure_ascii=False, separators=(",", ":"))[:30000]

            out_rows.append([
                ts,
                plant_id,
                plant_name,
                inv_sn,
                inv_alias,
                status,
                pac if pac is not None else "",
                etoday if etoday is not None else "",
                etotal if etotal is not None else "",
                raw_compact,
            ])

    append_rows(sheet_id, tab_name, out_rows)
    log.info("✅ Done. Wrote %d inverter rows into %s.", len(out_rows), tab_name)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error("FAILED: %s", e)
        raise
