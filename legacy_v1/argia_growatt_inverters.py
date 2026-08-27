#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA – Growatt Inverter Snapshot (30-min)
-----------------------------------------
This version avoids dead endpoints and avoids dangerous endpoints.

How it works:
1) Login
2) For each plantId:
   - Warm context (/device + /device/photovoltaic?plantId=...)
   - Load HTML pages that contain the real AJAX endpoints:
       * /device/getMAXPage?ttt=...
       * /device/getInverterPage?plantId=...
   - Discover candidate URLs in the HTML
   - Probe ONLY SAFE list endpoints until we get device rows (SN, status, power, eToday, eMonth, eTotal)
3) Append time-series rows to Google Sheets "InverterData"

Env required:
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS   (service-account JSON as TEXT)
- GROWATT_USER
- GROWATT_PASS

Optional:
- SNAP_RANGE      default "SNAP!A1:Z"
- INVERTER_TAB    default "InverterData"
- PAGE_SIZE       default 50
- MAX_PAGES       default 5
- DEBUG_OUT_DIR   default "out"
- LOG_LEVEL       default "INFO"
"""

import os
import re
import json
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Iterable

import requests

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


LOG = logging.getLogger("argia.growatt.inverters")


# ----------------------------
# Logging
# ----------------------------
def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


# ----------------------------
# Helpers
# ----------------------------
INVALID_FS_CHARS = r'["<>:|*?\r\n]'


def safe_filename(name: str) -> str:
    name = re.sub(INVALID_FS_CHARS, "_", name)
    name = name.replace("/", "_").strip("_")
    return name


def ensure_dir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_ms() -> int:
    return int(time.time() * 1000)


def try_parse_json(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except Exception:
        return None


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


def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()


# ----------------------------
# Google Sheets
# ----------------------------
def load_google_creds() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS secret (service account JSON as text).")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def sheets_service():
    return build("sheets", "v4", credentials=load_google_creds(), cache_discovery=False)


def read_snap_siteids(sheet_id: str, snap_range: str) -> List[str]:
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=snap_range,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()

    values = resp.get("values", []) or []
    siteids: List[str] = []
    for row in values:
        for cell in row:
            s = str(cell).strip()
            if re.fullmatch(r"\d{6,12}", s):
                siteids.append(s)

    return sorted(list(dict.fromkeys(siteids)))


def ensure_header(sheet_id: str, tab: str) -> None:
    """
    Your sheet already has 11 columns. We keep that same schema.
    We only write header if the sheet is empty.
    """
    header = [
        "ExtractedAtUTC",
        "PlantId",
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
# Growatt client
# ----------------------------
@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    BASE = "https://server.growatt.com"

    # Hard safety blacklist: NEVER call these from automation
    UNSAFE_PREFIXES = (
        "/commonDeviceSetC/",
    )
    UNSAFE_CONTAINS = (
        "setmax", "settlx", "setinverter",
        "delmax", "deltlx", "delinverter",
        "delete", "set", "save",
    )

    def __init__(self, auth: GrowattAuth, timeout: int = 45, debug_out_dir: str = "out"):
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

    def warm_plant_context(self, plant_id: str) -> None:
        self.get("/device")
        r_pv = self.get("/device/photovoltaic", params={"plantId": plant_id}, referer=self.BASE + "/device")
        if r_pv.status_code == 200 and r_pv.text:
            self._save_debug(plant_id, "pvpage", r_pv.text, "html")

        # These cookies matter for some list endpoints
        self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")
        self.s.cookies.set("selPage", "%2Fpanel", domain="server.growatt.com", path="/")

    def get_max_page_html(self, plant_id: str) -> str:
        r = self.get("/device/getMAXPage", params={"ttt": str(now_ms())}, referer=self.BASE + "/index")
        self._save_debug(plant_id, "max_page", r.text or "", "html")
        return r.text or ""

    def get_inverter_page_html(self, plant_id: str) -> str:
        r = self.get("/device/getInverterPage", params={"plantId": str(plant_id)}, referer=self.BASE + "/device")
        self._save_debug(plant_id, "inv_page", r.text or "", "html")
        return r.text or ""

    @staticmethod
    def discover_ajax_urls(html: str) -> List[str]:
        urls: List[str] = []

        # url:'/...'
        for m in re.finditer(r"url\s*:\s*['\"](\/[^'\"]+)['\"]", html):
            urls.append(m.group(1))

        # $.post('/...') / $.get('/...')
        for m in re.finditer(r"\$\.(?:post|get)\(\s*['\"](\/[^'\"]+)['\"]", html):
            urls.append(m.group(1))

        # Dedup keep order
        seen = set()
        out = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _is_safe_endpoint(self, endpoint: str) -> bool:
        ep = endpoint.lower()
        if any(ep.startswith(p) for p in self.UNSAFE_PREFIXES):
            return False
        if any(bad in ep for bad in self.UNSAFE_CONTAINS):
            return False
        # we only want list-ish endpoints
        if "list" not in ep:
            return False
        return True

    def _call_json(self, plant_id: str, endpoint: str, payload: dict) -> Optional[dict]:
        """
        Try POST then GET; return JSON dict if possible.
        """
        r = self.post(endpoint, data=payload, referer=self.BASE + "/index")
        txt = r.text or ""
        data = try_parse_json(txt)
        if data:
            return data

        # fallback to GET
        r2 = self.get(endpoint, params=payload, referer=self.BASE + "/index")
        txt2 = r2.text or ""
        data2 = try_parse_json(txt2)
        if data2:
            return data2

        # debug non-json
        self._save_debug(plant_id, f"nonjson_{safe_filename(endpoint)}", txt2[:20000], "txt")
        return None

    @staticmethod
    def _extract_items(data: dict) -> List[dict]:
        items = data.get("datas")
        if items is None:
            items = data.get("data")
        if items is None:
            items = data.get("rows")
        if items is None:
            items = []
        if not isinstance(items, list):
            return []
        return [x for x in items if isinstance(x, dict)]

    def fetch_devices_for_plant(self, plant_id: str, page_size: int, max_pages: int) -> List[Dict[str, Any]]:
        """
        Discover + probe safe list endpoints from MAX page and inverter page.
        """
        html_max = self.get_max_page_html(plant_id)
        html_inv = self.get_inverter_page_html(plant_id)

        urls = self.discover_ajax_urls(html_max) + self.discover_ajax_urls(html_inv)

        # Always include these fallbacks (safe)
        urls += [
            "/device/getMAXList",
            "/device/getMaxList",
            "/device/getDatalogList",
            "/panel/getDeviceList",
            "/panel/getPlantDeviceList",
        ]

        # Dedup
        seen = set()
        candidates = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                candidates.append(u)

        safe_candidates = [u for u in candidates if self._is_safe_endpoint(u)]
        LOG.info("Found %d safe list candidates for plant %s", len(safe_candidates), plant_id)

        payload_variants = [
            {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size), "ind": "1"},
            {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size)},
            {"plantId": str(plant_id), "pageSize": str(page_size), "currPage": "1"},
            {"currPage": "1", "pageSize": str(page_size)},  # some endpoints infer plantId from cookie
        ]

        for ep in safe_candidates:
            all_items: List[Dict[str, Any]] = []
            for page in range(1, max_pages + 1):
                page_items: List[Dict[str, Any]] = []
                for base in payload_variants:
                    payload = dict(base)
                    payload["currPage"] = str(page)
                    payload["pageSize"] = str(page_size)

                    data = self._call_json(plant_id, ep, payload)
                    if not data:
                        continue

                    items = self._extract_items(data)
                    if not items:
                        continue

                    page_items = items
                    break

                if not page_items:
                    if page == 1:
                        break
                    else:
                        break

                all_items.extend(page_items)
                if len(page_items) < page_size:
                    break

            # accept if it has at least one device with an SN-like field
            if any(pick(it, ["sn", "deviceSn", "invSn", "serialNum"]) for it in all_items):
                LOG.info("✅ Using endpoint %s (items=%d) for plant %s", ep, len(all_items), plant_id)
                self._save_debug(plant_id, "chosen_endpoint", f"{ep}\n", "txt")
                return all_items

        LOG.warning("❌ No device list endpoint produced SNs for plant %s", plant_id)
        return []


def build_rows(extracted_at: str, plant_id: str, items: List[Dict[str, Any]]) -> List[List[Any]]:
    """
    Output matches your existing 11 columns:
    ExtractedAtUTC, PlantId, DeviceType, DeviceSN, Status, UpdateTime,
    RatedPower_W, CurrentPower_W, EToday_kWh, EMonth_kWh, ETotal_kWh
    """
    rows: List[List[Any]] = []
    for it in items:
        sn = pick(it, ["sn", "deviceSn", "invSn", "serialNum", "serialNo"]) or ""
        if not sn:
            continue

        device_type = pick(it, ["deviceType", "deviceTypeNum", "type", "deviceTypeName"]) or ""
        status = pick(it, ["status", "deviceStatus", "invStatus", "workStatus", "connStatus"]) or ""
        update_time = pick(it, ["updateTime", "lastUpdateTime", "time"]) or ""

        rated = pick(it, ["ratedPower", "ratedPowerW", "capacity"])
        pac = pick(it, ["pac", "power", "actPower", "pNow", "currentPower"])

        etoday = pick(it, ["eToday", "EToday", "todayEnergy", "generationToday"])
        emonth = pick(it, ["eMonth", "EMonth", "monthEnergy", "monthlyEnergy", "generationMonth"])
        etotal = pick(it, ["eTotal", "ETotal", "totalEnergy", "generationTotal"])

        rows.append([
            extracted_at,
            str(plant_id),
            normalize_text(device_type),
            normalize_text(sn),
            normalize_text(status),
            normalize_text(update_time),
            normalize_float(rated) if rated is not None else "",
            normalize_float(pac) if pac is not None else "",
            normalize_float(etoday) if etoday is not None else "",
            normalize_float(emonth) if emonth is not None else "",
            normalize_float(etotal) if etotal is not None else "",
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

    username = os.getenv("GROWATT_USER", "").strip()
    password = os.getenv("GROWATT_PASS", "").strip()
    if not username or not password:
        raise RuntimeError("Missing GROWATT_USER or GROWATT_PASS")

    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z").strip()
    tab = os.getenv("INVERTER_TAB", "InverterData").strip()
    page_size = int(os.getenv("PAGE_SIZE", "50").strip())
    max_pages = int(os.getenv("MAX_PAGES", "5").strip())
    out_dir = os.getenv("DEBUG_OUT_DIR", "out").strip()

    ensure_header(sheet_id, tab)

    siteids = read_snap_siteids(sheet_id, snap_range)
    LOG.info("Loaded %s plants", len(siteids))

    cli = GrowattMonitoringClient(GrowattAuth(user=username, password=password), debug_out_dir=out_dir)
    cli.login()

    extracted_at = now_utc_iso()
    all_rows: List[List[Any]] = []

    for plant_id in siteids:
        LOG.info("==============================================")
        LOG.info("🏭 PlantId=%s", plant_id)

        cli.warm_plant_context(plant_id)
        items = cli.fetch_devices_for_plant(plant_id, page_size=page_size, max_pages=max_pages)

        rows = build_rows(extracted_at, plant_id, items)
        LOG.info("→ devices=%d, rows=%d", len(items), len(rows))

        all_rows.extend(rows)
        time.sleep(1)

    append_rows(sheet_id, tab, all_rows)
    LOG.info("✅ Written %d rows", len(all_rows))


if __name__ == "__main__":
    main()
