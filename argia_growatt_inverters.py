#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA - Growatt Inverter / MAX Snapshot (30-min intervals)
---------------------------------------------------------
Goal:
- For each Growatt PlantId (SITEID), fetch device list (especially MAX devices like JNM...).
- Append rows into Google Sheet tab: "InverterData"
- Include extraction timestamp (UTC) so you can build time-series graphs.

Why this version works better:
- Your plants show deviceTypeName=max and serials like JNM..., and /device/getInverterList returns empty.
- Growatt pages often load device lists via dynamic AJAX endpoints.
- This script:
    1) warms plant context
    2) loads /device/getMAXPage
    3) discovers AJAX URLs from that HTML
    4) probes likely "list" endpoints with multiple payload variants until it gets results

Secrets/Env expected:
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS          (service account JSON content as TEXT, not base64)
- GROWATT_USER
- GROWATT_PASS

Optional env:
- SNAP_RANGE                  default: "SNAP!A1:Z"
- INVERTER_TAB                default: "InverterData"
- MAX_PAGES                   default: 5
- PAGE_SIZE                   default: 50
- DEBUG_OUT_DIR               default: "out" (writes debug html/json responses)
- PLANT_DEVICE_TYPES          default: "max,inv" (comma list of device pages to probe)
"""

import os
import re
import json
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Iterable

import requests

# Google Sheets
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# ----------------------------
# Logging
# ----------------------------
LOG = logging.getLogger("argia.growatt.inverters")


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


# ----------------------------
# Helpers
# ----------------------------
INVALID_FS_CHARS = r'["<>:|*?\r\n]'


def safe_filename(name: str) -> str:
    name = re.sub(INVALID_FS_CHARS, "_", name)
    name = name.replace("/", "_")
    name = name.strip("_")
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


def strip_html_title(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:200]


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
    creds = load_google_creds()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


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

    siteids = sorted(list(dict.fromkeys(siteids)))
    return siteids


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
    """
    Works with https://server.growatt.com (web monitoring endpoints).
    """

    BASE = "https://server.growatt.com"

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
        r_dev = self.get("/device")
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

    def get_device_page(self, plant_id: str, kind: str) -> str:
        """
        Loads a device page HTML so we can discover the AJAX endpoints used to fetch lists/details.
        kind:
          - 'max' -> /device/getMAXPage
          - 'inv' -> /device/getInverterPage
          - 'datalog' -> /device/getDatalogPage  (optional)
        """
        kind = (kind or "").lower().strip()

        if kind == "max":
            path = "/device/getMAXPage"
            params = {"ttt": str(now_ms())}
        elif kind == "inv":
            path = "/device/getInverterPage"
            params = {"plantId": str(plant_id)}
        elif kind == "datalog":
            path = "/device/getDatalogPage"
            params = {"plantId": str(plant_id)}
        else:
            raise ValueError(f"Unknown page kind: {kind}")

        r = self.get(path, params=params, referer=self.BASE + "/index")
        LOG.info("GET %s -> %s (len=%s)", path, r.status_code, len(r.text or ""))
        if r.status_code != 200:
            raise RuntimeError(f"GET {path} failed: {r.status_code}")

        html = r.text or ""
        self._save_debug(plant_id, f"{kind}_page", html, "html")
        return html

    @staticmethod
    def discover_ajax_urls(html: str) -> List[str]:
        """
        Pulls AJAX URLs from a Growatt HTML page by scanning for patterns like:
          url:'/device/getXxxList'
          url : "/panel/getYyy"
        """
        urls: List[str] = []

        # url:'/xxx'
        for m in re.finditer(r"url\s*:\s*['\"](\/[^'\"]+)['\"]", html):
            urls.append(m.group(1))

        # $.post('/xxx' ...) or $.get('/xxx' ...)
        for m in re.finditer(r"\$\.(?:post|get)\(\s*['\"](\/[^'\"]+)['\"]", html):
            urls.append(m.group(1))

        # Dedup, keep order
        seen = set()
        out = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _save_json_debug(self, plant_id: str, label: str, endpoint: str, payload: dict, txt: str) -> None:
        safe_ep = safe_filename(endpoint.replace("/", "_"))
        safe_lbl = safe_filename(label)
        fn = f"{plant_id}__{safe_lbl}__{safe_ep}.json"
        path = os.path.join(self.debug_out_dir, fn)
        obj = {
            "endpoint": endpoint,
            "payload": payload,
            "raw": txt[:20000],  # cap
        }
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def _call_json(self, plant_id: str, endpoint: str, payload: dict) -> Optional[dict]:
        """
        Try POST then GET. Return dict if JSON, else None.
        """
        r = self.post(endpoint, data=payload, referer=self.BASE + "/index")
        txt = r.text or ""

        # If HTML comes back (like a login page), try GET
        if r.status_code in (404, 405) or (txt.lstrip().startswith("<!DOCTYPE") or "<html" in txt.lower()):
            r = self.get(endpoint, params=payload, referer=self.BASE + "/index")
            txt = r.text or ""

        self._save_debug(plant_id, f"ajax_{safe_filename(endpoint)}", txt, "txt")
        data = try_parse_json(txt)
        if not data:
            self._save_json_debug(plant_id, "nonjson", endpoint, payload, txt)
            return None
        return data

    @staticmethod
    def _extract_items(data: dict) -> List[dict]:
        """
        Growatt uses different envelopes:
          - datas: [...]
          - data: [...]
          - rows: [...]
          - obj: [...]
        """
        items = data.get("datas")
        if items is None:
            items = data.get("data")
        if items is None:
            items = data.get("rows")
        if items is None:
            items = data.get("obj")
        if items is None:
            items = []

        if isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list):
            return []
        return [x for x in items if isinstance(x, dict)]

    def probe_device_list(self, plant_id: str, html: str, kind: str, max_pages: int, page_size: int) -> List[Dict[str, Any]]:
        """
        From the page HTML, discover list endpoints, then probe them using several payload variants.
        Returns list of device dicts.
        """
        discovered = self.discover_ajax_urls(html)

        # Prefer endpoints that look like list getters
        likely = [u for u in discovered if any(t in u.lower() for t in ["list", "get", "device"])]

        # Add known fallbacks (some accounts have these)
        fallbacks = [
            "/device/getMAXList",
            "/device/getMaxList",
            "/device/getInverterList",
            "/newInvAPI.do?op=getInvList",
            "/panel/getDeviceList",
            "/panel/getPlantDeviceList",
        ]

        candidates = []
        for u in likely + fallbacks:
            if u not in candidates:
                candidates.append(u)

        # payload variants to try (Growatt is inconsistent)
        base_variants = [
            {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size)},
            {"plantId": str(plant_id), "currPage": 1, "pageSize": page_size},
            {"plantId": str(plant_id), "pageSize": str(page_size), "currPage": "1", "ind": "1"},
            {"plantId": str(plant_id), "pageSize": str(page_size), "currPage": "1", "deviceTypeName": kind},
            {"plantId": str(plant_id), "deviceTypeName": kind, "currPage": "1", "pageSize": str(page_size)},
            {"plantId": str(plant_id), "deviceTypeName": kind},
            {"plantId": str(plant_id)},
            {"currPage": "1", "pageSize": str(page_size)},  # some endpoints infer plantId from cookie
        ]

        # For inverter list endpoints, Growatt sometimes expects "op" query
        # but we already include /newInvAPI.do?op=getInvList as a candidate.
        results: List[Dict[str, Any]] = []

        last_reason = ""
        for ep in candidates:
            ep_lower = ep.lower()

            # Skip clearly unrelated endpoints
            if any(bad in ep_lower for bad in ["alertplantevent", "remindme", "nolongerremind", "getchuanghuodevicelist"]):
                continue

            LOG.info("🔎 Probing endpoint: %s", ep)

            # paginate if the endpoint supports it
            all_items: List[Dict[str, Any]] = []

            for page in range(1, max_pages + 1):
                page_items: List[Dict[str, Any]] = []

                for base in base_variants:
                    payload = dict(base)
                    payload["currPage"] = payload.get("currPage", str(page))
                    payload["pageSize"] = payload.get("pageSize", str(page_size))
                    # ensure correct page number
                    payload["currPage"] = str(page)

                    data = self._call_json(plant_id, ep, payload)
                    if not data:
                        continue

                    items = self._extract_items(data)

                    # If empty, keep trying other payload shapes
                    if not items:
                        continue

                    # Heuristic filter:
                    # - MAX devices often have SN like JNM...
                    # - Inverters often have other serial formats
                    if kind == "max":
                        items2 = []
                        for it in items:
                            sn = (it.get("sn") or it.get("deviceSn") or it.get("invSn") or it.get("serialNum") or "")
                            if isinstance(sn, str) and (sn.startswith("JNM") or "max" in str(it.get("deviceTypeName", "")).lower()):
                                items2.append(it)
                        page_items = items2 if items2 else items
                    else:
                        page_items = items

                    # success for this page/payload
                    break

                if page_items:
                    all_items.extend(page_items)
                else:
                    # If no items on page 1, this endpoint likely isn't it.
                    if page == 1:
                        break

                # If fewer than page_size, stop.
                if len(page_items) < page_size:
                    break

            if all_items:
                LOG.info("✅ Endpoint %s produced %d items", ep, len(all_items))
                results = all_items
                break
            else:
                last_reason = f"endpoint={ep} returned no items"

        if not results:
            LOG.warning("No device items found for plantId=%s (%s). Last: %s", plant_id, kind, last_reason)

        return results


# ----------------------------
# Extract fields robustly
# ----------------------------
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
    if x is None:
        return ""
    return str(x).strip()


def build_rows_for_sheet(extracted_at: str, plant_id: str, dev_items: List[Dict[str, Any]], device_type: str) -> List[List[Any]]:
    """
    Converts device dicts into rows for Google Sheet.

    We keep the header stable and just fill what we can:
      - ExtractedAtUTC
      - PlantId
      - DeviceType
      - DeviceSN
      - Status / Connection
      - UpdateTime
      - RatedPower_W
      - CurrentPower_W
      - GenerationToday_kWh
      - Month_kWh
      - Total_kWh
      - RawStatusCode (if exists)
    """
    rows: List[List[Any]] = []

    for d in dev_items:
        sn = pick(d, ["sn", "deviceSn", "invSn", "serialNum", "serialNo"])
        device_type_name = pick(d, ["deviceTypeName", "typeName", "deviceType"]) or device_type

        # status fields vary
        status = pick(d, ["status", "deviceStatus", "invStatus", "workStatus", "statusText", "connStatus", "connectionStatus"])
        status_code = pick(d, ["statusNum", "statusCode", "deviceStatusNum", "invStatusNum"])

        # power / energy fields vary
        rated_power = pick(d, ["ratedPower", "ratedPowerW", "ratedPower(W)", "ratedPower_w", "capacity", "power"])
        current_power = pick(d, ["currentPower", "pac", "invPac", "powerNow", "pNow", "actPower", "p", "power"])
        etoday = pick(d, ["eToday", "etoday", "todayEnergy", "todayE", "generationToday", "eacToday", "invEacToday"])
        emonth = pick(d, ["eMonth", "emonth", "monthEnergy", "monthlyEnergy", "monthE", "generationMonth"])
        etotal = pick(d, ["eTotal", "etotal", "totalEnergy", "totalE", "generationTotal", "eacTotal", "invEacTotal"])

        update_time = pick(d, ["updateTime", "lastUpdateTime", "time", "update_time"])

        rows.append([
            extracted_at,
            str(plant_id),
            normalize_text(device_type_name),
            normalize_text(sn),
            normalize_text(status),
            normalize_text(update_time),
            normalize_float(rated_power) if rated_power is not None else "",
            normalize_float(current_power) if current_power is not None else "",
            normalize_float(etoday) if etoday is not None else "",
            normalize_float(emonth) if emonth is not None else "",
            normalize_float(etotal) if etotal is not None else "",
            normalize_text(status_code),
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
    max_pages = int(os.getenv("MAX_PAGES", "5").strip())
    page_size = int(os.getenv("PAGE_SIZE", "50").strip())
    out_dir = os.getenv("DEBUG_OUT_DIR", "out").strip()

    types_raw = os.getenv("PLANT_DEVICE_TYPES", "max,inv").strip()
    plant_device_types = [t.strip().lower() for t in types_raw.split(",") if t.strip()]

    LOG.info("This script expects GOOGLE_CREDENTIALS (not GOOGLE_SA_JSON/GOOGLE_SA_B64)")

    siteids = read_snap_siteids(sheet_id, snap_range)
    LOG.info("Loaded %s SITEIDs from SNAP: %s", len(siteids), ", ".join(siteids))

    header = [
        "ExtractedAtUTC",
        "PlantId",
        "DeviceType",
        "DeviceSN",
        "Status",
        "UpdateTime",
        "RatedPower_W",
        "CurrentPower_W",
        "GenerationToday_kWh",
        "Month_kWh",
        "Total_kWh",
        "RawStatusCode",
    ]
    ensure_header(sheet_id, tab, header)

    cli = GrowattMonitoringClient(GrowattAuth(user=username, password=password), debug_out_dir=out_dir)
    cli.login()

    extracted_at = now_utc_iso()
    all_rows: List[List[Any]] = []

    for plant_id in siteids:
        LOG.info("==============================================")
        LOG.info("🏭 PlantId=%s", plant_id)

        cli.warm_plant_context(plant_id)

        for kind in plant_device_types:
            try:
                html = cli.get_device_page(plant_id, kind)
                items = cli.probe_device_list(
                    plant_id=plant_id,
                    html=html,
                    kind=kind,
                    max_pages=max_pages,
                    page_size=page_size,
                )
                LOG.info("Found %s '%s' devices for plantId=%s", len(items), kind, plant_id)

                rows = build_rows_for_sheet(extracted_at, plant_id, items, device_type=kind)
                all_rows.extend(rows)

                time.sleep(0.8)
            except Exception as e:
                LOG.exception("Failed probing kind=%s for plantId=%s: %s", kind, plant_id, e)
                # continue other types/plants
                time.sleep(1.0)
                continue

        time.sleep(0.8)

    append_rows(sheet_id, tab, all_rows)
    LOG.info("✅ Wrote %s rows to %s", len(all_rows), tab)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        LOG.exception("FAILED: %s", e)
        raise
