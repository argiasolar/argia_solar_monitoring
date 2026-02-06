# argia_growatt_monitoring.py
# ------------------------------------------------------------
# Growatt WEB monitoring client (NOT OpenAPI)
# Used for probing + inverter-level monitoring
# ------------------------------------------------------------

import os
import time
import logging
import requests
from typing import Dict, List, Optional, Tuple

BASE_URL = "https://server.growatt.com"

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
log = logging.getLogger("argia.growatt.monitoring")
log.setLevel(logging.INFO)


# ------------------------------------------------------------
# Auth helper (cookie-based)
# ------------------------------------------------------------
class GrowattAuth:
    def __init__(self):
        self.username = os.environ.get("GROWATT_USERNAME")
        self.password = os.environ.get("GROWATT_PASSWORD")

        if not self.username or not self.password:
            raise RuntimeError("Missing GROWATT_USERNAME or GROWATT_PASSWORD")

        self.session = requests.Session()

    def login(self) -> requests.Session:
        # Initial GET to seed cookies
        r = self.session.get(f"{BASE_URL}/login", timeout=20)
        log.info(f"GET /login -> {r.status_code}")

        payload = {
            "account": self.username,
            "password": self.password,
        }

        r = self.session.post(f"{BASE_URL}/login", data=payload, timeout=20)
        log.info(f"POST /login -> {r.status_code} (len={len(r.text)})")

        cookies = self.session.cookies.get_dict()
        if "assToken" not in cookies:
            raise RuntimeError("Growatt login failed: assToken missing")

        log.info(
            "✅ Login OK (assToken present). Cookies: "
            + " | ".join(cookies.keys())
        )
        return self.session


# ------------------------------------------------------------
# Monitoring client
# ------------------------------------------------------------
class GrowattMonitoringClient:
    def __init__(self, session: requests.Session, debug: bool = True):
        self.session = session
        self.debug = debug

    # ----------------------------
    # Low-level request wrapper
    # ----------------------------
    def _req(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        timeout: int = 20,
    ) -> Tuple[int, dict, dict, str]:
        url = BASE_URL + path
        r = self.session.request(
            method,
            url,
            params=params,
            data=data,
            timeout=timeout,
        )
        return r.status_code, r.headers, r.cookies.get_dict(), r.text

    # ----------------------------
    # Plant context priming
    # REQUIRED or Growatt returns 500
    # ----------------------------
    def _seed_plant_context(self, plant_id: str):
        self._req("GET", "/device", params={"plantId": str(plant_id)})
        time.sleep(0.3)

    # ============================================================
    # Methods REQUIRED by argia_probe.py
    # ============================================================

    def get_device_page_html(self, plant_id: Optional[str] = None) -> str:
        params = {"plantId": str(plant_id)} if plant_id else None
        st, _, _, html = self._req("GET", "/device", params=params, timeout=30)
        if self.debug:
            log.info(f"GET /device -> {st} (len={len(html)})")
        return html or ""

    def get_env_page_html(self, plant_id: str) -> str:
        self._seed_plant_context(plant_id)
        st, _, _, html = self._req("GET", "/device/getEnvPage", timeout=30)
        if self.debug:
            log.info(f"GET /device/getEnvPage -> {st} (len={len(html)})")
        return html or ""

    def get_env_list(self, plant_id: str) -> dict:
        self._seed_plant_context(plant_id)
        st, _, _, txt = self._req("POST", "/device/getEnvList", timeout=30)
        if self.debug:
            log.info(f"POST /device/getEnvList -> {st}")
        return self._safe_json(txt)

    def get_pv_page_html(self, plant_id: str) -> str:
        self._seed_plant_context(plant_id)
        st, _, _, html = self._req(
            "GET",
            "/device/photovoltaic",
            params={"plantId": str(plant_id)},
            timeout=30,
        )
        if self.debug:
            log.info(f"GET /device/photovoltaic -> {st} (len={len(html)})")
        return html or ""

    # ============================================================
    # Helper
    # ============================================================
    @staticmethod
    def _safe_json(txt: str) -> dict:
        try:
            import json
            return json.loads(txt)
        except Exception:
            return {}


# ------------------------------------------------------------
# What this file ENABLES next (important)
# ------------------------------------------------------------
"""
✔ CURRENTLY WORKING:
- Web login via assToken
- Plant context switching
- Access to:
    /device
    /device/getEnvPage
    /device/getEnvList
    /device/photovoltaic?plantId=...

✔ THIS IS EXACTLY WHAT YOU NEED TO:
1) Read inverter list (serial numbers, status)
2) Discover AJAX endpoints used for:
   - per-inverter power
   - per-inverter energy
   - historical curves (30-min)
3) Implement:
   - Daily kWh per plant  (sum of inverters)
   - Daily kWh per inverter
   - 30-min monitoring loop
   - downtime alerts
"""
