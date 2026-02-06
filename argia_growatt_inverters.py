#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA - Growatt Inverter Snapshot (Step 2) - WORKING (Index HTML scrape)
------------------------------------------------------------------------
Why this works:
- For many Growatt accounts, /device/getInverterList returns empty (legacy endpoint).
- The actual inverter list + KPIs (SN, status, current power, generation today)
  are present on the server-rendered /index page once selectedPlantId is set.
- This script:
  1) logs in
  2) for each plantId sets selectedPlantId cookie
  3) GET /index
  4) scrapes inverter blocks from HTML
  5) appends rows to Google Sheet tab "InverterData"

Secrets/Env expected:
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS          (service account JSON content as TEXT, not base64)
- GROWATT_USER
- GROWATT_PASS

Optional env:
- SNAP_RANGE                  default: "SNAP!A1:Z"
- INVERTER_TAB                default: "InverterData"
- DEBUG_OUT_DIR               default: "out"  (writes debug html)
- LOG_LEVEL                   default: INFO
"""

import os
import re
import json
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

# Google Sheets
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


LOG = logging.getLogger("argia.growatt.inverters")
INVALID_FS_CHARS = r'["<>:|*?\r\n]'


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def safe_filename(name: str) -> str:
    name = re.sub(INVALID_FS_CHARS, "_", name)
    name = name.replace("/", "_").strip("_")
    return name


def ensure_dir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------
# Google Sheets
# ----------------------------
def load_google_creds() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS secret (service account JSON as text).")
    try:
        info = json.loads(raw)
    except Exception as e:
        raise RuntimeError("GOOGLE_CREDENTIALS is not valid JSON text.") from e

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def sheets_service():
    return build("sheets", "v4", credentials=load_google_creds(), cache_discovery=False)


def read_snap_siteids(sheet_id: str, snap_range: str) -> List[str]:
    svc = sheets_service()
    resp = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=snap_range, valueRenderOption="UNFORMATTED_VALUE")
        .execute()
    )
    values = resp.get("values", []) or []
    siteids: List[str] = []
    for row in values:
        for cell in row:
            s = str(cell).strip()
            if re.fullmatch(r"\d{6,12}", s):
                siteids.append(s)
    return sorted(list(dict.fromkeys(siteids)))


def ensure_header(sheet_id: str, tab: str, header: List[str]) -> None:
    svc = sheets_service()
    rng = f"{tab}!A1:Z1"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    existing = (resp.get("values") or [[]])[0] if resp else []
    existing = existing or []

    if existing == header:
        return

    if len(existing) == 0:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
        LOG.info("Ensured header on tab '%s'", tab)
        return

    LOG.warning("Tab '%s' already has a header, leaving it as-is.", tab)


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
# Growatt client
# ----------------------------
@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    BASE = "https://server.growatt.com"

    def __init__(self, auth: GrowattAuth, timeout: int = 30, debug_out_dir: str = "out"):
        self.auth = auth
        self.timeout = timeout
        self.s = requests.Session()
        self.debug_out_dir = debug_out_dir
        ensure_dir(self.debug_out_dir)

        self.s.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (ARGIA Growatt Bot)",
                "Accept": "*/*",
            }
        )

    def _save_debug(self, plant_id: str, label: str, content: str, ext: str) -> None:
        fn = safe_filename(f"{plant_id}__{label}.{ext}")
        path = os.path.join(self.debug_out_dir, fn)
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            f.write(content)

    def _set_cookie(self, key: str, value: str) -> None:
        # ensure cookie is scoped correctly
        self.s.cookies.set(key, value, domain="server.growatt.com", path="/")

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
            LOG.error("Cookies after login: %s", cookies)
            raise RuntimeError("Login failed: assToken cookie missing")

        LOG.info("✅ Login OK (assToken present). Cookies: %s", " | ".join(sorted(list(cookies.keys()))))

        # enter /index once
        r_idx = self.get("/index", referer=self.BASE + "/login")
        LOG.info("GET /index -> %s (len=%s)", r_idx.status_code, len(r_idx.text or ""))

    def load_index_for_plant(self, plant_id: str) -> str:
        """
        Sets selectedPlantId and loads /index HTML.
        This page (for your account) contains inverter cards with:
          Device Serial Number, Connection Status, Update Time,
          Current Power(W), Generation Today(kWh), etc.
        """
        self._set_cookie("selectedPlantId", str(plant_id))
        self._set_cookie("selPage", "/index")

        r = self.get("/index", referer=self.BASE + "/index")
        LOG.info("GET /index (selectedPlantId=%s) -> %s (len=%s)", plant_id, r.status_code, len(r.text or ""))

        if r.status_code != 200:
            raise RuntimeError(f"GET /index for plantId={plant_id} -> {r.status_code}")

        html = r.text or ""
        self._save_debug(plant_id, "index", html, "html")
        return html


# ----------------------------
# HTML parsing (regex-based)
# ----------------------------
def _to_float(x: Optional[str]) -> Optional[float]:
    if not x:
        return None
    try:
        return float(x.strip().replace(",", ""))
    except Exception:
        return None


def parse_inverters_from_index_html(html: str) -> List[Dict[str, Any]]:
    """
    Parses inverter blocks like you pasted:

    Device Serial Number：JNM7DY306G
    Connection Status：Connected
    Update Time：2026-02-06 14:15:00
    Current Power(W)：49533.9
    Generation Today(kWh)：228.2
    ...

    Returns list of dicts with keys: sn, status, power_w, etoday_kwh
    """
    # Make punctuation variations more tolerant (Growatt uses full-width colon ：)
    # We match minimally between fields, DOTALL for multi-line.
    pattern = re.compile(
        r"Device Serial Number[：:]\s*(?P<sn>[A-Za-z0-9_\-]+).*?"
        r"Connection Status[：:]\s*(?P<status>[^ \r\n\t<]+).*?"
        r"Update Time[：:]\s*(?P<update>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*?"
        r"Current Power\(W\)[：:]\s*(?P<power>[\d.,]+).*?"
        r"Generation Today\(kWh\)[：:]\s*(?P<today>[\d.,]+)",
        re.DOTALL | re.IGNORECASE,
    )

    items: List[Dict[str, Any]] = []
    for m in pattern.finditer(html):
        sn = m.group("sn").strip()
        status = m.group("status").strip()
        power_w = _to_float(m.group("power"))
        etoday_kwh = _to_float(m.group("today"))
        update_time = m.group("update").strip()

        items.append(
            {
                "sn": sn,
                "status": status,
                "power_w": power_w,
                "etoday_kwh": etoday_kwh,
                "update_time": update_time,
            }
        )

    return items


def build_rows_for_sheet(extracted_at: str, plant_id: str, inv_items: List[Dict[str, Any]]) -> List[List[Any]]:
    """
    Keeps your original 7-column schema:
      ExtractedAtUTC, PlantId, InverterSN, InverterAlias, Status, EToday_kWh, Power_W
    Alias is not reliably present in index HTML, so we leave it blank.
    """
    rows: List[List[Any]] = []
    for inv in inv_items:
        rows.append(
            [
                extracted_at,
                str(plant_id),
                inv.get("sn") or "",
                "",  # alias not available from index scrape
                inv.get("status") or "",
                inv.get("etoday_kwh") if inv.get("etoday_kwh") is not None else "",
                inv.get("power_w") if inv.get("power_w") is not None else "",
            ]
        )
    return rows


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    setup_logging()

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    username = os.getenv("GROWATT_USER", "").strip()
    password = os.getenv("GROWATT_PASS", "").strip()
    if not username or not password:
        raise RuntimeError("Missing GROWATT_USER or GROWATT_PASS")

    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z").strip()
    tab = os.getenv("INVERTER_TAB", "InverterData").strip()
    out_dir = os.getenv("DEBUG_OUT_DIR", "out").strip()

    LOG.info("This script expects GOOGLE_CREDENTIALS (not GOOGLE_SA_JSON/GOOGLE_SA_B64)")

    siteids = read_snap_siteids(sheet_id, snap_range)
    LOG.info("Loaded %s SITEIDs from SNAP: %s", len(siteids), ", ".join(siteids))

    header = ["ExtractedAtUTC", "PlantId", "InverterSN", "InverterAlias", "Status", "EToday_kWh", "Power_W"]
    ensure_header(sheet_id, tab, header)

    cli = GrowattMonitoringClient(GrowattAuth(user=username, password=password), debug_out_dir=out_dir)
    cli.login()

    extracted_at = now_utc_iso()
    all_rows: List[List[Any]] = []

    for plant_id in siteids:
        LOG.info("==============================================")
        LOG.info("🏭 PlantId=%s", plant_id)

        html = cli.load_index_for_plant(plant_id)
        inv_items = parse_inverters_from_index_html(html)

        LOG.info("Parsed %s inverter blocks from /index for plantId=%s", len(inv_items), plant_id)
        if len(inv_items) == 0:
            # helpful debug: show a tiny hint in logs
            LOG.warning("No inverters parsed for plantId=%s. Check out/%s__index.html", plant_id, plant_id)

        all_rows.extend(build_rows_for_sheet(extracted_at, plant_id, inv_items))
        time.sleep(1)

    append_rows(sheet_id, tab, all_rows)
    LOG.info("✅ Wrote %s rows to %s", len(all_rows), tab)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        LOG.exception("FAILED: %s", e)
        raise
