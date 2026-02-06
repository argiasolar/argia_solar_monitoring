#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA - Growatt Inverters (Step 2: InverterData)

Goal:
- For each Growatt plantId in Google Sheet "SNAP" tab:
  - Discover inverter list endpoints dynamically from /device/getInverterPage
  - Fetch inverter list (SN + alias if available)
  - Fetch daily kWh per inverter (E-Today / daily energy)
  - Write rows to Google Sheet tab "InverterData"

This script expects GOOGLE_CREDENTIALS (Service Account JSON) in GitHub Secrets.
DO NOT use GOOGLE_SA_JSON / GOOGLE_SA_B64.

Required GitHub Secrets:
- GOOGLE_CREDENTIALS      (full service account json text)
- GOOGLE_SHEET_ID         (your spreadsheet id)
- GROWATT_USER
- GROWATT_PASS

Optional env:
- SNAP_RANGE              default "SNAP!A1:Z"
- INVERTER_TAB             default "InverterData"
- TIMEZONE_OFFSET_HOURS    default "-6" (only for logging; Growatt uses plant TZ)
- DEBUG_OUT                default "" (set to "out" to write debug html/json files)
"""

from __future__ import annotations

import os
import re
import json
import time
import math
import logging
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from google.oauth2 import service_account
from googleapiclient.discovery import build


# -----------------------------
# Logging
# -----------------------------
LOG = logging.getLogger("argia.growatt.inverters")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)


# -----------------------------
# Google Sheets helpers
# -----------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def load_google_creds():
    """
    Uses GOOGLE_CREDENTIALS containing FULL service account JSON (string).
    """
    raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS (service account json text)")

    try:
        info = json.loads(raw)
    except Exception as e:
        raise RuntimeError("GOOGLE_CREDENTIALS is not valid JSON") from e

    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def sheets_service():
    creds = load_google_creds()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_snap_siteids(sheet_id: str, snap_range: str) -> List[str]:
    """
    Reads plantIds/siteIds from SNAP sheet.
    We accept values anywhere in the range that look like a number >= 6 digits.
    """
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=snap_range).execute()
    values = resp.get("values", [])

    siteids: List[str] = []
    for row in values:
        for cell in row:
            s = str(cell).strip()
            if re.fullmatch(r"\d{6,}", s):
                siteids.append(s)

    # unique + stable order
    seen = set()
    out = []
    for x in siteids:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def ensure_tab_exists(sheet_id: str, tab_name: str):
    """
    If tab doesn't exist, create it.
    """
    svc = sheets_service()
    ss = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tabs = [s["properties"]["title"] for s in ss.get("sheets", [])]
    if tab_name in tabs:
        return

    body = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=body).execute()


def write_inverter_rows(
    sheet_id: str,
    tab_name: str,
    header: List[str],
    rows: List[List[Any]],
):
    """
    Overwrites the entire tab with header + rows.
    """
    svc = sheets_service()
    ensure_tab_exists(sheet_id, tab_name)

    values = [header] + rows
    rng = f"{tab_name}!A1"
    body = {"values": values}
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=rng,
        valueInputOption="RAW",
        body=body,
    ).execute()


# -----------------------------
# Growatt Monitoring client
# -----------------------------
@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    """
    Low-level client that mimics browser flow enough to keep session.

    Key fixes:
    - Always set plant context using /device?plantId=...
    - Always open /device/getInverterPage?plantId=... and parse AJAX endpoints
    - Always call JSON endpoints with XHR headers + Referer
    """

    BASE = "https://server.growatt.com"

    def __init__(self, auth: GrowattAuth, timeout: int = 30):
        self.auth = auth
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ArgiaGrowatt/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self._last_referer: Optional[str] = None

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.BASE + path

    def get(self, path: str, **kwargs) -> requests.Response:
        r = self.s.get(self._url(path), timeout=self.timeout, allow_redirects=True, **kwargs)
        return r

    def post(self, path: str, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
        hdrs = {}
        if headers:
            hdrs.update(headers)
        r = self.s.post(
            self._url(path),
            data=data,
            timeout=self.timeout,
            allow_redirects=True,
            headers=hdrs
        )
        return r

    def login(self):
        r1 = self.get("/login")
        LOG.info("GET /login -> %s", r1.status_code)

        payload = {"account": self.auth.user, "password": self.auth.password}
        r2 = self.post("/login", data=payload)
        LOG.info("POST /login -> %s (len=%s)", r2.status_code, len(r2.text))

        if "assToken" not in self.s.cookies.get_dict():
            raise RuntimeError("Login failed: assToken cookie missing")

        LOG.info("✅ Login OK (assToken present). Cookies: %s", " | ".join(sorted(self.s.cookies.get_dict().keys())))

        # warm up index/device
        self.get("/index")
        self.get("/device")

    def set_plant_context(self, plant_id: str):
        # This matters: Growatt UI often relies on plant context.
        self.get(f"/device?plantId={plant_id}")
        # Optional but helps: open PV page too (Growatt sometimes sets extra state)
        self.get(f"/device/photovoltaic?plantId={plant_id}")

    @staticmethod
    def _looks_like_login_html(text: str) -> bool:
        t = text.lower()
        return ("data-name=\"dumplogin\"" in t) or ("errornologin" in t) or ("login page" in t)

    @staticmethod
    def _extract_ajax_paths(html: str) -> List[str]:
        """
        Extract candidate endpoints from html.
        We prefer anything with Inv + List, but keep a broad net.
        """
        candidates = set()

        # /device/xxxx or /newInvAPI.do?... etc.
        for m in re.findall(r'/(?:device|newInvAPI\.do|newPlantAPI\.do)[^"\'\s<>]+', html):
            # strip trailing junk
            m = m.strip().split("\\")[0]
            candidates.add(m)

        # also grab things inside JS "url:" patterns
        for m in re.findall(r'url\s*:\s*[\'"]([^\'"]+)[\'"]', html, flags=re.I):
            if m.startswith("/"):
                candidates.add(m)

        # rank: inv/list first
        def score(p: str) -> int:
            s = 0
            low = p.lower()
            if "inv" in low:
                s += 10
            if "list" in low:
                s += 10
            if "get" in low:
                s += 2
            if "api" in low:
                s += 1
            return -s

        return sorted(candidates, key=score)

    def get_inverter_page_html(self, plant_id: str) -> str:
        """
        Open inverter page (this is where Growatt hides the AJAX URLs).
        """
        r = self.get(f"/device/getInverterPage?plantId={plant_id}")
        if r.status_code != 200:
            raise RuntimeError(f"Failed to open inverter page for plantId={plant_id}: {r.status_code}")
        if self._looks_like_login_html(r.text):
            raise RuntimeError(f"Inverter page returned login HTML for plantId={plant_id}")
        self._last_referer = self._url(f"/device/getInverterPage?plantId={plant_id}")
        return r.text

    def _xhr_headers(self) -> Dict[str, str]:
        hdrs = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        if self._last_referer:
            hdrs["Referer"] = self._last_referer
        return hdrs

    def try_fetch_inverter_list(self, plant_id: str, page: int, page_size: int) -> Optional[Dict[str, Any]]:
        """
        Try several candidate endpoints + payload shapes until we get JSON.
        Returns parsed json or None.
        """
        inv_html = self.get_inverter_page_html(plant_id)
        endpoints = self._extract_ajax_paths(inv_html)

        # Candidate endpoints to try FIRST (common patterns)
        preferred = []
        for p in endpoints:
            low = p.lower()
            if ("inv" in low and "list" in low) or ("newinvapi.do" in low and "getinvlist" in low):
                preferred.append(p)

        # Add some known fallbacks if not present
        fallbacks = [
            "/newInvAPI.do?op=getInvList",
            "/device/getInvList",
            "/device/getInverterList",
            "/device/getInverterListData",
        ]

        tried = []
        for p in preferred + fallbacks + endpoints:
            if p in tried:
                continue
            tried.append(p)

            # Build payload variants Growatt might expect
            payload_variants = [
                {"plantId": plant_id, "currPage": page, "pageSize": page_size},
                {"plantId": plant_id, "page": page, "pageSize": page_size},
                {"plantId": plant_id, "currPage": page},
                {"plantId": plant_id},
            ]

            for data in payload_variants:
                try:
                    r = self.post(p, data=data, headers=self._xhr_headers())
                    txt = r.text or ""
                    if r.status_code != 200:
                        continue
                    if self._looks_like_login_html(txt):
                        continue

                    # must be JSON
                    try:
                        js = r.json()
                    except Exception:
                        continue

                    # sanity: must contain list-ish keys
                    if isinstance(js, dict) and any(k in js for k in ["datas", "data", "rows", "obj", "result"]):
                        return js
                except Exception:
                    continue

        return None

    @staticmethod
    def _normalize_inverter_items(js: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Convert various Growatt JSON shapes into a list of inverter dicts.
        """
        # Most common: {"datas":[...], "count":..., "pages":...}
        if "datas" in js and isinstance(js["datas"], list):
            return js["datas"]

        # Sometimes: {"data":{"datas":[...]}}
        if "data" in js and isinstance(js["data"], dict) and "datas" in js["data"]:
            if isinstance(js["data"]["datas"], list):
                return js["data"]["datas"]

        # Sometimes: {"rows":[...]}
        if "rows" in js and isinstance(js["rows"], list):
            return js["rows"]

        # Sometimes nested
        for k in ["obj", "result"]:
            if k in js and isinstance(js[k], dict):
                inner = js[k]
                if "datas" in inner and isinstance(inner["datas"], list):
                    return inner["datas"]
                if "rows" in inner and isinstance(inner["rows"], list):
                    return inner["rows"]

        return []

    @staticmethod
    def _extract_sn(item: Dict[str, Any]) -> Optional[str]:
        for key in ["sn", "serialNum", "deviceSn", "invSn", "inverterSn"]:
            v = item.get(key)
            if v and str(v).strip().lower() != "null":
                s = str(v).strip()
                if s:
                    return s
        return None

    @staticmethod
    def _extract_alias(item: Dict[str, Any]) -> str:
        for key in ["alias", "name", "deviceName", "invName"]:
            v = item.get(key)
            if v and str(v).strip().lower() != "null":
                return str(v).strip()
        return ""

    def list_all_inverters(self, plant_id: str, max_pages: int = 20, page_size: int = 50) -> List[Dict[str, Any]]:
        """
        Attempts paginated listing. Stops when no new items.
        """
        all_items: List[Dict[str, Any]] = []
        seen = set()

        for page in range(1, max_pages + 1):
            js = self.try_fetch_inverter_list(plant_id, page=page, page_size=page_size)
            if not js:
                raise RuntimeError(f"Could not fetch inverter list JSON for plantId={plant_id} (still getting login/non-json)")

            items = self._normalize_inverter_items(js)
            if not items:
                break

            added = 0
            for it in items:
                sn = self._extract_sn(it)
                if not sn:
                    continue
                if sn in seen:
                    continue
                seen.add(sn)
                all_items.append(it)
                added += 1

            # if a page adds nothing new, stop
            if added == 0:
                break

            # if response has pages and we're at the end, stop
            pages = js.get("pages")
            try:
                pages_i = int(pages) if pages is not None else None
            except Exception:
                pages_i = None
            if pages_i and page >= pages_i:
                break

        return all_items

    def fetch_daily_kwh_for_inverter(self, plant_id: str, sn: str, date_iso: str) -> Optional[float]:
        """
        Growatt has multiple daily endpoints depending on account.
        We try a few patterns and parse JSON if possible.

        date_iso: YYYY-MM-DD
        """
        # keep plant context
        self.set_plant_context(plant_id)
        # refresh referer to inverter page for XHR calls
        self.get_inverter_page_html(plant_id)

        candidates = [
            # common "panel" style
            ("/panel/inverter/getInverterData", {"sn": sn}),
            # older style
            ("/indexbC/inv/getInvData", {"sn": sn}),
            # sometimes there is a daily report api (varies)
            ("/newInvAPI.do?op=getInvData", {"sn": sn}),
            ("/newInvAPI.do?op=getInvDayData", {"sn": sn, "date": date_iso}),
            ("/newInvAPI.do?op=getInvEday", {"sn": sn, "date": date_iso}),
        ]

        for path, params in candidates:
            try:
                # some are GET
                url = path + "?" + "&".join(f"{k}={v}" for k, v in params.items())
                r = self.get(url, headers=self._xhr_headers())
                if r.status_code != 200:
                    continue
                if self._looks_like_login_html(r.text or ""):
                    continue
                try:
                    js = r.json()
                except Exception:
                    continue

                # Heuristics for daily energy field
                # Look for etoday / eToday / todayEnergy / e_today etc.
                flat = json.dumps(js).lower()
                for k in ["etoday", "e_today", "todayenergy", "today_energy", "eday", "e_day", "energy_today"]:
                    if k in flat:
                        pass

                # attempt extraction:
                val = None

                def dig(obj):
                    nonlocal val
                    if val is not None:
                        return
                    if isinstance(obj, dict):
                        for kk, vv in obj.items():
                            low = str(kk).lower()
                            if low in ["etoday", "e_today", "todayenergy", "today_energy", "eday", "e_day", "energy_today"]:
                                try:
                                    val = float(str(vv).strip())
                                    return
                                except Exception:
                                    pass
                            dig(vv)
                    elif isinstance(obj, list):
                        for x in obj:
                            dig(x)

                dig(js)

                if val is not None and (not math.isnan(val)):
                    return float(val)
            except Exception:
                continue

        return None


# -----------------------------
# Main
# -----------------------------
def main():
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z").strip()
    inverter_tab = os.getenv("INVERTER_TAB", "InverterData").strip()

    user = os.getenv("GROWATT_USER", "").strip()
    pw = os.getenv("GROWATT_PASS", "").strip()
    if not user or not pw:
        raise RuntimeError("Missing GROWATT_USER or GROWATT_PASS")

    # Date to fetch = yesterday in Mexico time (default -6) OR just "today" if you prefer.
    # For now: use TODAY in UTC-6 as "date".
    tz_hours = int(os.getenv("TIMEZONE_OFFSET_HOURS", "-6"))
    now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    local = now_utc.astimezone(dt.timezone(dt.timedelta(hours=tz_hours)))
    date_iso = local.date().isoformat()

    LOG.info("This script expects GOOGLE_CREDENTIALS (not GOOGLE_SA_JSON/GOOGLE_SA_B64)")
    siteids = read_snap_siteids(sheet_id, snap_range)
    LOG.info("Loaded %s SITEIDs from SNAP: %s", len(siteids), ", ".join(siteids))

    cli = GrowattMonitoringClient(GrowattAuth(user=user, password=pw))
    cli.login()

    rows: List[List[Any]] = []
    header = ["Date", "PlantId", "InverterSN", "InverterAlias", "Daily_kWh"]

    for plant_id in siteids:
        LOG.info("==============================================")
        LOG.info("🏭 PlantId=%s", plant_id)

        # critical: plant context first
        cli.set_plant_context(plant_id)

        inv_items = cli.list_all_inverters(plant_id, max_pages=10, page_size=50)
        LOG.info("Found %s inverters for plantId=%s", len(inv_items), plant_id)

        if not inv_items:
            continue

        for it in inv_items:
            sn = cli._extract_sn(it)
            if not sn:
                continue
            alias = cli._extract_alias(it)

            kwh = cli.fetch_daily_kwh_for_inverter(plant_id, sn, date_iso=date_iso)
            if kwh is None:
                # keep row but empty kWh (so you see missing endpoints)
                rows.append([date_iso, plant_id, sn, alias, ""])
            else:
                rows.append([date_iso, plant_id, sn, alias, round(kwh, 3)])

            time.sleep(0.2)

        time.sleep(0.5)

    write_inverter_rows(sheet_id, inverter_tab, header, rows)
    LOG.info("✅ Wrote %s rows to %s", len(rows), inverter_tab)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        LOG.error("FAILED: %s", e, exc_info=True)
        raise
