#!/usr/bin/env python3
"""
argia_probe.py

Goal:
- Login to Growatt server.growatt.com
- Load Growatt plant IDs from Google Sheet tab "SNAP"
- For each plantId:
    - GET /device/getEnvPage
    - POST /device/getEnvList (page 1)
    - GET /device/photovoltaic?plantId=<id>
- Save everything into ./out so GitHub Actions can upload it.

Requires envs:
  GROWATT_USERNAME
  GROWATT_PASSWORD
  GOOGLE_SHEET_ID
  GOOGLE_CREDENTIALS

Optional:
  GROWATT_BASE (default https://server.growatt.com)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("argia.probe")


# ----------------------------
# helpers
# ----------------------------

def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def safe_filename(s: str) -> str:
    """
    GitHub artifact + Windows-safe filename.
    Replaces:  " : < > | * ?  and control chars
    """
    s = s.strip()
    s = re.sub(r'[\x00-\x1f]', "_", s)
    s = re.sub(r'[<>:"|?*]+', "_", s)
    s = re.sub(r"\s+", "_", s)
    return s[:180] if len(s) > 180 else s


def request_any(session: requests.Session, method: str, url: str, **kwargs) -> Tuple[int, Dict[str, str], Any, str]:
    resp = session.request(method, url, **kwargs)
    text = resp.text or ""
    parsed = None
    try:
        parsed = resp.json()
    except Exception:
        parsed = None
    return resp.status_code, dict(resp.headers), parsed, text


def extract_paths_from_html(html: str, prefix: str = "/device") -> List[str]:
    if not html:
        return []
    # finds strings like /device/whatever
    found = set(re.findall(r"(/device[^\s\"'<>]+)", html))
    out = []
    for p in found:
        if p.startswith(prefix):
            out.append(p)
    return sorted(out)


def extract_ajax_endpoints(html: str) -> List[str]:
    """
    Very lightweight: find '/device/xxxx' inside JS strings
    """
    if not html:
        return []
    found = set(re.findall(r"(/device/[A-Za-z0-9_]+)", html))
    return sorted(found)


# ----------------------------
# Google Sheets
# ----------------------------

def get_sheets_service():
    sheet_id = env("GOOGLE_SHEET_ID")
    creds_json = env("GOOGLE_CREDENTIALS")
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS")

    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds)
    return service


def load_siteids_from_snap() -> List[str]:
    """
    Reads SNAP!A1:Z and extracts numeric plant IDs.
    We keep it dumb-simple: look for 7+ digit numbers anywhere.
    """
    service = get_sheets_service()
    sheet_id = env("GOOGLE_SHEET_ID")
    res = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="SNAP!A1:Z",
    ).execute()

    values = res.get("values", [])
    blob = "\n".join(["\t".join(row) for row in values if isinstance(row, list)])

    # plantIds look like 9275498 etc
    ids = sorted(set(re.findall(r"\b\d{7,12}\b", blob)))
    return ids


# ----------------------------
# Growatt Monitoring Client
# ----------------------------

@dataclass
class GrowattAuth:
    user: str
    password: str
    base: str = "https://server.growatt.com"


class GrowattMonitoringClient:
    def __init__(self, auth: GrowattAuth, timeout: int = 45):
        self.auth = auth
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
                "Connection": "keep-alive",
            }
        )

    def login(self) -> None:
        base = self.auth.base.rstrip("/")
        st, _, _, _ = request_any(self.s, "GET", f"{base}/login", timeout=self.timeout)
        logging.getLogger("argia.growatt.monitoring").info("GET /login -> %s", st)
        if st != 200:
            raise RuntimeError(f"GET /login failed HTTP {st}")

        payload = {"account": self.auth.user, "password": self.auth.password}
        headers = {
            "Origin": base,
            "Referer": f"{base}/login",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        st2, _, _, body = request_any(self.s, "POST", f"{base}/login", data=payload, headers=headers, timeout=self.timeout)
        logging.getLogger("argia.growatt.monitoring").info("POST /login -> %s (len=%s)", st2, len(body or ""))

        cookies = self.s.cookies.get_dict()
        if "assToken" not in cookies:
            raise RuntimeError("Login failed: assToken cookie missing")

        logging.getLogger("argia.growatt.monitoring").info(
            "✅ Login OK (assToken present). Cookies: %s",
            " | ".join(sorted(cookies.keys()))
        )

    def seed_plant_context(self, plant_id: str) -> None:
        # UI uses cookies for selected plant navigation; this helps endpoints behave
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device/getEnvPage")

    def get_device_home(self) -> str:
        base = self.auth.base.rstrip("/")
        st, _, _, text = request_any(self.s, "GET", f"{base}/device", timeout=self.timeout)
        logging.getLogger("argia.growatt.monitoring").info("GET /device -> %s (len=%s)", st, len(text or ""))
        return text or ""

    def get_env_page(self, plant_id: str) -> str:
        self.seed_plant_context(plant_id)
        base = self.auth.base.rstrip("/")
        st, _, _, text = request_any(self.s, "GET", f"{base}/device/getEnvPage", timeout=self.timeout)
        logging.getLogger("argia.growatt.monitoring").info("GET /device/getEnvPage -> %s (len=%s)", st, len(text or ""))
        return text or ""

    def post_env_list(self, plant_id: str, curr_page: int = 1, alias: str = "") -> Any:
        self.seed_plant_context(plant_id)
        base = self.auth.base.rstrip("/")
        headers = {
            "Origin": base,
            "Referer": f"{base}/device/getEnvPage",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        data = {"plantId": str(plant_id), "currPage": str(curr_page), "alias": alias}
        st, _, parsed, raw = request_any(self.s, "POST", f"{base}/device/getEnvList", headers=headers, data=data, timeout=self.timeout)
        logging.getLogger("argia.growatt.monitoring").info("POST /device/getEnvList -> %s", st)
        return parsed if parsed is not None else {"_non_json": True, "http": st, "text": raw[:500]}

    def get_pv_page(self, plant_id: str) -> str:
        """
        IMPORTANT: the probe logs showed that /device/photovoltaic works ONLY when plantId query param is present.
        """
        self.seed_plant_context(plant_id)
        base = self.auth.base.rstrip("/")
        url = f"{base}/device/photovoltaic?plantId={plant_id}"
        st, _, _, text = request_any(self.s, "GET", url, timeout=self.timeout)
        logging.getLogger("argia.growatt.monitoring").info("GET /device/photovoltaic -> %s (len=%s)", st, len(text or ""))
        return text or ""


# ----------------------------
# main
# ----------------------------

def main() -> None:
    log.info("=== PROBE START %s ===", __import__("datetime").datetime.utcnow().isoformat() + "Z")

    out_dir = "out"
    ensure_dir(out_dir)

    # load plantIds from SNAP
    siteids = load_siteids_from_snap()
    if not siteids:
        raise RuntimeError("No SITEIDs found in SNAP")
    log.info("Loaded %d SITEIDs from SNAP: %s", len(siteids), ", ".join(siteids[:20]))

    # growatt login
    username = env("GROWATT_USERNAME")
    password = env("GROWATT_PASSWORD")
    if not username or not password:
        raise RuntimeError("Missing GROWATT_USERNAME / GROWATT_PASSWORD")

    base = env("GROWATT_BASE", "https://server.growatt.com") or "https://server.growatt.com"

    cli = GrowattMonitoringClient(GrowattAuth(user=username, password=password, base=base))
    cli.login()

    # Save device home page (useful to discover URLs)
    device_home = cli.get_device_home()
    write_text(os.path.join(out_dir, "device__home.html"), device_home)

    # show discovered paths for debugging (in logs + file)
    data_urls = re.findall(r'data-url="([^"]+)"', device_home or "")
    paths = extract_paths_from_html(device_home)
    write_json(
        os.path.join(out_dir, "device__discovery.json"),
        {"data_urls": data_urls[:50], "paths": paths[:200]},
    )

    for plant_id in siteids:
        log.info("==============================================")
        log.info("🏭 PlantId=%s", plant_id)

        # ENV page
        env_html = cli.get_env_page(plant_id)
        write_text(os.path.join(out_dir, f"{safe_filename(plant_id)}__envpage.html"), env_html)

        endpoints = extract_ajax_endpoints(env_html)
        log.info("Env page endpoints: %s", ", ".join(endpoints) if endpoints else "(none)")

        # ENV list page 1
        env_list = cli.post_env_list(plant_id, curr_page=1, alias="")
        write_json(os.path.join(out_dir, f"{safe_filename(plant_id)}__env_list__p1.json"), env_list)

        # PV page (HTML)
        pv_html = cli.get_pv_page(plant_id)
        write_text(os.path.join(out_dir, f"{safe_filename(plant_id)}__pvpage.html"), pv_html)

    log.info("=== PROBE END ===")


if __name__ == "__main__":
    main()
