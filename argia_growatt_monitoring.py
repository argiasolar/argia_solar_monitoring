# argia_growatt_monitoring.py
#
# ShineServer (server.growatt.com) monitoring client.
# LOGIN FLOW (matches browser + your working growatt_weather_fetch.py):
#   GET  /login
#   POST /login  (ajax) -> assToken cookie
#   GET  /index  (IMPORTANT: initializes session state for other modules)
#
# ENV module works already.
# PV module (/device/photovoltaic) needs the extra /index initialization.

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
    def __init__(self, auth: GrowattAuth, timeout_s: int = 45):
        self.auth = auth
        self.timeout_s = timeout_s
        self.base = _env("GROWATT_BASE", "https://server.growatt.com").rstrip("/")

        self.s = requests.Session()

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

        self._index_initialized = False

    # -------------------------
    # Login (Web UI flow)
    # -------------------------
    def login(self) -> None:
        login_url = f"{self.base}/login"

        r1 = self.s.get(login_url, timeout=self.timeout_s)
        LOG.info("GET /login -> %s", r1.status_code)
        if r1.status_code != 200:
            raise RuntimeError(f"GET /login failed: HTTP {r1.status_code}")

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

        if "assToken" not in cookies:
            snippet = (r2.text or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(
                f"Login failed: assToken cookie missing. HTTP={r2.status_code} body_snippet='{snippet}'"
            )

        LOG.info("✅ Login OK (assToken present). Cookies: %s", " | ".join(sorted(cookies.keys())))

        # IMPORTANT: Initialize index/dashboard session state.
        self.init_index()

    def init_index(self) -> None:
        if self._index_initialized:
            return
        # browser always hits /index right after login
        r = self.s.get(
            f"{self.base}/index",
            headers={"Referer": f"{self.base}/login", "Accept": "text/html, */*"},
            timeout=self.timeout_s,
            allow_redirects=True,
        )
        LOG.info("GET /index -> %s (len=%s)", r.status_code, len(r.text or ""))
        # Even if it returns 200 with HTML, we treat this as initialization step.
        self._index_initialized = True

    # -------------------------
    # Context cookies (Growatt UI is cookie-stateful)
    # -------------------------
    def seed_env_context(self, plant_id: str) -> None:
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device/getEnvPage")

    def seed_pv_context(self, plant_id: str) -> None:
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device/photovoltaic")

    # compatibility alias
    def seed_plant_context(self, plant_id: str) -> None:
        self.seed_env_context(plant_id)

    # -------------------------
    # Low-level HTTP helpers
    # -------------------------
    def _post_json(self, path: str, data: Dict[str, Any], referer_path: str = "/index") -> Any:
        self.init_index()

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

    def _get_text(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        referer_path: str = "/index",
    ) -> str:
        self.init_index()

        url = f"{self.base}{path}"
        headers = {"Referer": f"{self.base}{referer_path}", "Accept": "text/html, */*"}
        r = self.s.get(url, headers=headers, params=params, timeout=self.timeout_s, allow_redirects=True)
        LOG.info("GET %s -> %s (len=%s)", path, r.status_code, len(r.text or ""))
        return r.text or ""

    # -------------------------
    # Known-good ENV wrappers
    # -------------------------
    def get_env_page_html(self, plant_id: str) -> str:
        self.seed_env_context(plant_id)
        return self._get_text("/device/getEnvPage", referer_path="/device")

    def post_get_env_list(self, plant_id: str, curr_page: int = 1, alias: str = "") -> Any:
        self.seed_env_context(plant_id)
        return self._post_json(
            "/device/getEnvList",
            {"plantId": str(plant_id), "currPage": str(curr_page), "alias": alias},
            referer_path="/device/getEnvPage",
        )

    def post_get_env_history(
        self,
        plant_id: str,
        datalog_sn: str,
        addr: int,
        start_date: str,
        end_date: str,
        start: int = 0,
    ) -> Any:
        self.seed_env_context(plant_id)
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

    # -------------------------
    # PV page (diagnostics)
    # -------------------------
    def get_device_page_html(self) -> str:
        return self._get_text("/device", referer_path="/index")

    def get_pv_page_html(self, plant_id: str) -> str:
        self.seed_pv_context(plant_id)
        # Must refer from /device
        return self._get_text("/device/photovoltaic", referer_path="/device")
