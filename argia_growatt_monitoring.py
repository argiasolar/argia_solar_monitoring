# argia_growatt_monitoring.py
#
# Monitoring-only Growatt ShineServer (server.growatt.com) client.
# VERIFIED against your working growatt_weather_fetch.py:
#   - GET /login
#   - POST /login with AJAX headers
#   - login success => assToken cookie present
#   - device endpoints live under /device/* (NOT /newPlantAPI.do etc for your account)
#
# This file is intentionally standalone and DOES NOT modify argia_growatt.py (OpenAPI token flow).

import os
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

LOG = logging.getLogger("argia.growatt.monitoring")


@dataclass
class GrowattAuth:
    username: str
    password: str


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


class GrowattMonitoringClient:
    """
    ShineServer web client (session/cookies) for monitoring / probing.

    Key points:
    - Uses the SAME login flow as growatt_weather_fetch.py and validates by assToken cookie.
    - Provides helpers for /device/* endpoints with correct headers and referers.
    - Provides plant context cookie seeding (selectedPlantId + selPage cookies), as used by the web UI.
    """

    def __init__(self, auth: GrowattAuth, timeout_s: int = 45):
        self.auth = auth
        self.timeout_s = timeout_s
        self.base = _env("GROWATT_BASE", "https://server.growatt.com").rstrip("/")

        self.s = requests.Session()

        # Match your working UA + Accept-Language hints
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        )
        self.s.headers.update(
            {
                "User-Agent": ua,
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8,pl;q=0.7,cs;q=0.6",
                "Connection": "keep-alive",
            }
        )

    # -------------------------
    # Login (Web UI flow)
    # -------------------------
    def login(self) -> None:
        login_url = f"{self.base}/login"

        # Step 1: GET /login
        r1 = self.s.get(login_url, timeout=self.timeout_s)
        LOG.info("GET /login -> %s", r1.status_code)
        if r1.status_code != 200:
            raise RuntimeError(f"GET /login failed: HTTP {r1.status_code}")

        # Step 2: POST /login (AJAX)
        payload = {"account": self.auth.username, "password": self.auth.password}
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/login",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        r2 = self.s.post(login_url, data=payload, headers=headers, timeout=self.timeout_s)
        LOG.info("POST /login -> %s (len=%s)", r2.status_code, len(r2.text or ""))

        cookies = self.s.cookies.get_dict()
        LOG.debug("Cookies after login: %s", cookies)

        # Your proven condition:
        if "assToken" not in cookies:
            snippet = (r2.text or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(
                f"Login failed: assToken cookie missing. HTTP={r2.status_code} body_snippet='{snippet}'"
            )

        LOG.info("✅ Login OK (assToken present). Cookies: %s", " | ".join(sorted(cookies.keys())))

    # -------------------------
    # Plant context (Web UI cookies)
    # -------------------------
    def seed_plant_context(self, plant_id: str) -> None:
        """
        Web UI relies on selectedPlantId + selPage* cookies for some /device/* calls.
        Your weather script uses:
          selectedPlantId
          selPage=/device
          selPageTwo=/device/photovoltaic
          selPageThree=/device/getEnvPage
        For monitoring, we keep a conservative default; probe scripts can override by setting the referer_path.
        """
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device")

    # -------------------------
    # Low-level HTTP helpers
    # -------------------------
    def _post_json(self, path: str, data: Dict[str, Any], referer_path: str = "/index") -> Any:
        """
        POST with the same header pattern as growatt_weather_fetch.py.
        """
        url = f"{self.base}{path}"
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}{referer_path}",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        r = self.s.post(url, headers=headers, data=data, timeout=self.timeout_s)
        LOG.info("POST %s -> %s", path, r.status_code)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_non_json": True, "text": (r.text or "")[:2000]}

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None, referer_path: str = "/index") -> Any:
        """
        GET that tries to parse JSON; falls back to trimmed HTML/text if not JSON.
        """
        url = f"{self.base}{path}"
        headers = {
            "Referer": f"{self.base}{referer_path}",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        r = self.s.get(url, headers=headers, params=params, timeout=self.timeout_s, allow_redirects=True)
        LOG.info("GET %s -> %s", path, r.status_code)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_non_json": True, "text": (r.text or "")[:2000]}

    # -------------------------
    # Convenience wrappers (known-good endpoints from your working script)
    # -------------------------
    def get_env_page_html(self, plant_id: str) -> str:
        """
        GET /device/getEnvPage (HTML) — used in your weather script for debugging.
        """
        self.seed_plant_context(plant_id)
        url = f"{self.base}/device/getEnvPage"
        headers = {
            "Referer": f"{self.base}/index",
            "Accept": "text/html, */*",
        }
        r = self.s.get(url, headers=headers, timeout=self.timeout_s)
        LOG.info("GET /device/getEnvPage -> %s (len=%s)", r.status_code, len(r.text or ""))
        r.raise_for_status()
        return r.text or ""

    def post_get_env_list(self, plant_id: str, curr_page: int = 1, alias: str = "") -> Any:
        """
        POST /device/getEnvList
          plantId=<PLANT_ID>
          currPage=<n>
          alias=<search>
        Matches your growatt_weather_fetch.py implementation.
        """
        self.seed_plant_context(plant_id)
        return self._post_json(
            "/device/getEnvList",
            {"plantId": str(plant_id), "currPage": str(curr_page), "alias": alias},
            referer_path="/device/getEnvPage",
        )

    def post_get_env_history(
        self, plant_id: str, datalog_sn: str, addr: int, start_date: str, end_date: str, start: int = 0
    ) -> Any:
        """
        POST /device/getEnvHistory
          datalogSn, addr, startDate, endDate, start
        Matches your growatt_weather_fetch.py implementation.
        """
        self.seed_plant_context(plant_id)
        return self._post_json(
            "/device/getEnvHistory",
            {
                "datalogSn": str(datalog_sn),
                "addr": str(addr),
                "startDate": str(start_date),
                "endDate": str(end_date),
                "start": str(start),
            },
            referer_path="/device/getEnvPage",
        )
