#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA - Growatt Inverter Snapshot (Step 2) - FIXED v2 (panel context)
--------------------------------------------------------------------
Fixes included:
- After login, open /index to match browser flow.
- For each plant:
  - Enter panel context (/panel) so Growatt sets/uses selectedPlantId state.
  - Force cookie selectedPlantId + selPage to mimic browser session.
- Use strong referer tied to /index and inverter page (?plantId=...).
- Keep debug artifact outputs.

Secrets/Env expected:
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS          (service account JSON content as TEXT, not base64)
- GROWATT_USER
- GROWATT_PASS

Optional env:
- SNAP_RANGE                  default: "SNAP!A1:Z"
- INVERTER_TAB                default: "InverterData"
- MAX_PAGES                   default: 10
- PAGE_SIZE                   default: 20
- DEBUG_OUT_DIR               default: "out"
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


def try_parse_json(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except Exception:
        return None


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

        # Match browser flow: hit /index once after login
        r_idx = self.get("/index", referer=self.BASE + "/login")
        LOG.info("GET /index -> %s (len=%s)", r_idx.status_code, len(r_idx.text or ""))

    def _set_cookie(self, key: str, value: str) -> None:
        # Ensure cookie is available for server.growatt.com
        self.s.cookies.set(key, value, domain="server.growatt.com", path="/")

    def enter_panel_context(self, plant_id: str) -> None:
        """
        Critical for your account:
        Browser sets selectedPlantId + selPage=/panel and then XHRs start returning data.
        """
        # Force cookies like browser (safe; if Growatt overwrites them, that's fine)
        self._set_cookie("selectedPlantId", str(plant_id))
        self._set_cookie("selPage", "/panel")

        # Open /panel to initialize context
        r_panel = self.get("/panel", params={"plantId": plant_id}, referer=self.BASE + "/index")
        LOG.info("GET /panel?plantId=%s -> %s (len=%s)", plant_id, r_panel.status_code, len(r_panel.text or ""))
        if r_panel.status_code == 200 and r_panel.text:
            self._save_debug(plant_id, "panel", r_panel.text, "html")

    def warm_plant_context(self, plant_id: str) -> None:
        # Keep your original warm-up (doesn't hurt)
        r_dev = self.get("/device", referer=self.BASE + "/index")
        LOG.info("GET /device -> %s (len=%s)", r_dev.status_code, len(r_dev.text or ""))

        r_pv = self.get("/device/photovoltaic", params={"plantId": plant_id}, referer=self.BASE + "/device")
        LOG.info(
            "GET /device/photovoltaic?plantId=%s -> %s (len=%s)",
            plant_id,
            r_pv.status_code,
            len(r_pv.text or ""),
        )
        if r_pv.status_code == 200 and r_pv.text:
            self._save_debug(plant_id, "pvpage", r_pv.text, "html")

    def get_inverter_page_html(self, plant_id: str) -> str:
        r = self.get("/device/getInverterPage", params={"plantId": plant_id}, referer=self.BASE + "/device")
        LOG.info("GET /device/getInverterPage?plantId=%s -> %s", plant_id, r.status_code)
        if r.status_code != 200:
            raise RuntimeError(f"GET /device/getInverterPage?plantId={plant_id} -> {r.status_code}")
        self._save_debug(plant_id, "inverter_page", r.text or "", "html")
        return r.text or ""

    @staticmethod
    def discover_inv_endpoints(html: str) -> Dict[str, str]:
        found: Dict[str, str] = {}
        m = re.search(r"(\/newInvAPI\.do\?op=getInvList)", html)
        if m:
            found["list"] = m.group(1)
        m = re.search(r"(\/device\/getInverterList)", html)
        if m and "list" not in found:
            found["list"] = m.group(1)
        m = re.search(r"(\/newInvAPI\.do\?op=getInvData)", html)
        if m:
            found["detail"] = m.group(1)
        return found

    def _call_list_endpoint(self, endpoint: str, plant_id: str, page: int, page_size: int, referer: str) -> dict:
        payload = {"plantId": str(plant_id), "currPage": str(page), "pageSize": str(page_size)}
        r = self.post(endpoint, data=payload, referer=referer)

        if r.status_code in (404, 405) or (r.text and r.text.lstrip().startswith("<!DOCTYPE")):
            r = self.get(endpoint, params=payload, referer=referer)

        txt = r.text or ""
        self._save_debug(plant_id, f"inv_list__{safe_filename(endpoint)}__p{page}", txt, "json")

        data = try_parse_json(txt)
        if not data:
            raise RuntimeError(f"Non-JSON inverter list for plantId={plant_id} endpoint={endpoint}: {txt[:200]}")
        return data

    def iter_all_inverters(self, plant_id: str, max_pages: int = 10, page_size: int = 20) -> List[Dict[str, Any]]:
        # Discover endpoints from page (still useful)
        html = self.get_inverter_page_html(plant_id)
        eps = self.discover_inv_endpoints(html)

        inverter_page_url = self.BASE + f"/device/getInverterPage?plantId={plant_id}"

        candidates: List[str] = []
        if eps.get("list"):
            candidates.append(eps["list"])

        candidates += [
            "/newInvAPI.do?op=getInvList",
            "/newInvApi.do?op=getInvList",
            "/device/getInverterList",
        ]

        last_err: Optional[Exception] = None

        # IMPORTANT: use /index as referer for “panel-like” XHRs
        # and inverter page URL for inverter-specific calls.
        # We’ll try both referers automatically.
        referers_to_try = [self.BASE + "/index", inverter_page_url]

        for endpoint in candidates:
            for ref in referers_to_try:
                try:
                    all_items: List[Dict[str, Any]] = []
                    for page in range(1, max_pages + 1):
                        data = self._call_list_endpoint(endpoint, plant_id, page, page_size, referer=ref)

                        items = data.get("datas") or data.get("data") or data.get("rows") or []
                        if not isinstance(items, list):
                            items = []

                        LOG.info(
                            "List endpoint=%s referer=%s plantId=%s -> count=%s pages=%s items_len=%s",
                            endpoint,
                            "/index" if ref.endswith("/index") else "invpage",
                            plant_id,
                            data.get("count"),
                            data.get("pages"),
                            len(items),
                        )

                        all_items.extend([it for it in items if isinstance(it, dict)])

                        pages = data.get("pages")
                        if isinstance(pages, int) and page >= pages:
                            break
                        if not pages and len(items) < page_size:
                            break

                    return all_items

                except Exception as e:
                    last_err = e
                    continue

        raise RuntimeError(f"Could not find JSON inverter list endpoint for plantId={plant_id}") from last_err


def pick(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return None


def normalize_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        if isinstance(x, str):
            x = x.strip().replace(",", "")
        return float(x)
    except Exception:
        return None


def build_rows_for_sheet(extracted_at: str, plant_id: str, inv_items: List[Dict[str, Any]]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for inv in inv_items:
        sn = pick(inv, ["sn", "invSn", "deviceSn", "inverterSn", "serialNum", "serialNo"])
        alias = pick(inv, ["alias", "invAlias", "deviceAilas", "name", "deviceName"])
        status = pick(inv, ["deviceStatus", "status", "invStatus", "workStatus", "statusText"])
        etoday = pick(inv, ["eToday", "etoday", "eTodayEnergy", "todayEnergy", "EToday", "etodayEnergy"])
        power = pick(inv, ["power", "pac", "powerNow", "pNow", "actPower", "p"])

        rows.append(
            [
                extracted_at,
                str(plant_id),
                sn or "",
                alias or "",
                str(status) if status is not None else "",
                normalize_float(etoday) if normalize_float(etoday) is not None else "",
                normalize_float(power) if normalize_float(power) is not None else "",
            ]
        )
    return rows


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
    max_pages = int(os.getenv("MAX_PAGES", "10").strip())
    page_size = int(os.getenv("PAGE_SIZE", "20").strip())
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

        # NEW: panel context first (this matches your browser cookies / referer flow)
        cli.enter_panel_context(plant_id)

        # keep original warm-up too
        cli.warm_plant_context(plant_id)

        inv_items = cli.iter_all_inverters(plant_id, max_pages=max_pages, page_size=page_size)
        LOG.info("Found %s inverters for plantId=%s", len(inv_items), plant_id)

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
