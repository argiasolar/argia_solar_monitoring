# argia_growatt_monitoring.py
# ------------------------------------------------------------
# Growatt WEB monitoring client (NOT OpenAPI)
# - Compatible with existing argia_probe.py signature expectations
# - Login via /login -> assToken cookie
# - Plant context seeding required for plant-scoped pages
# ------------------------------------------------------------

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional, Tuple, Union, Dict, Any

import requests

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
log = logging.getLogger("argia.growatt.monitoring")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)
log.setLevel(logging.INFO)


# ------------------------------------------------------------
# Auth (probe-compatible)
# ------------------------------------------------------------
@dataclass
class GrowattAuth:
    """
    Probe expects:
      GrowattAuth(username=..., password=..., base=...)

    We also allow env fallback:
      GrowattAuth()
    """
    username: Optional[str] = None
    password: Optional[str] = None
    base: str = "https://server.growatt.com"
    timeout: int = 30
    debug: bool = False

    def __post_init__(self):
        if not self.username:
            self.username = os.environ.get("GROWATT_USERNAME")
        if not self.password:
            self.password = os.environ.get("GROWATT_PASSWORD")
        if not self.username or not self.password:
            raise RuntimeError("Missing Growatt credentials (username/password or env GROWATT_USERNAME/GROWATT_PASSWORD).")

        self.base = (self.base or "https://server.growatt.com").rstrip("/")
        self.session = requests.Session()

        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        )
        self.session.headers.update(
            {
                "User-Agent": ua,
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8,pl;q=0.7,cs;q=0.6",
                "Connection": "keep-alive",
            }
        )

    def login(self) -> requests.Session:
        # Seed cookies
        r = self.session.get(f"{self.base}/login", timeout=self.timeout)
        log.info(f"GET /login -> {r.status_code}")

        payload = {"account": self.username, "password": self.password}
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/login",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        r = self.session.post(f"{self.base}/login", data=payload, headers=headers, timeout=self.timeout)
        log.info(f"POST /login -> {r.status_code} (len={len(r.text or '')})")

        cookies = self.session.cookies.get_dict()
        if self.debug:
            log.info(f"Cookies after login: {cookies}")

        if "assToken" not in cookies:
            snippet = (r.text or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(f"Growatt login failed: assToken missing. HTTP={r.status_code} body_snippet='{snippet}'")

        log.info("✅ Login OK (assToken present). Cookies: " + " | ".join(sorted(cookies.keys())))
        return self.session


# ------------------------------------------------------------
# Client
# ------------------------------------------------------------
class GrowattMonitoringClient:
    """
    Accepts:
      GrowattMonitoringClient(GrowattAuth(...))
    OR
      GrowattMonitoringClient(requests.Session())
    """
    def __init__(self, auth_or_session: Union[GrowattAuth, requests.Session], base: Optional[str] = None, debug: bool = False):
        self.debug = debug

        if isinstance(auth_or_session, GrowattAuth):
            self.auth = auth_or_session
            self.base = (base or self.auth.base or "https://server.growatt.com").rstrip("/")
            self.session = self.auth.login()
        else:
            self.auth = None
            self.session = auth_or_session
            self.base = (base or "https://server.growatt.com").rstrip("/")

    # ----------------------------
    # Request helper
    # ----------------------------
    def _req(self, method: str, path: str, *, params=None, data=None, headers=None, timeout: int = 30) -> Tuple[int, Dict[str, str], str]:
        url = self.base + path
        r = self.session.request(method, url, params=params, data=data, headers=headers, timeout=timeout)
        return r.status_code, dict(r.headers), (r.text or "")

    # ----------------------------
    # Plant context seeding
    # Growatt returns "not login" / 500 unless plantId context is set
    # ----------------------------
    def seed_plant_context(self, plant_id: str) -> None:
        # Web UI behavior: plantId is applied through navigation
        # We do GET /device?plantId=... to establish context
        self._req("GET", "/device", params={"plantId": str(plant_id)}, timeout=30)
        time.sleep(0.15)

    # ============================================================
    # Methods that probe uses / expects
    # ============================================================

    def get_device_page_html(self) -> str:
        st, _, html = self._req("GET", "/device", timeout=30)
        if self.debug:
            log.info(f"GET /device -> {st} (len={len(html)})")
        return html

    def get_env_page_html(self, plant_id: str) -> str:
        self.seed_plant_context(plant_id)
        st, _, html = self._req("GET", "/device/getEnvPage", timeout=30)
        log.info(f"GET /device/getEnvPage -> {st} (len={len(html)})")
        return html

    def post_get_env_list(self, plant_id: str, curr_page: int = 1, alias: str = "") -> Any:
        self.seed_plant_context(plant_id)

        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/device/getEnvPage",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        data = {"plantId": str(plant_id), "currPage": str(curr_page), "alias": alias}
        st, _, txt = self._req("POST", "/device/getEnvList", data=data, headers=headers, timeout=45)
        log.info(f"POST /device/getEnvList -> {st}")
        return self._safe_json(txt)

    def get_pv_page_html(self, plant_id: str) -> str:
        # IMPORTANT: must seed context first, then call photovoltaic with plantId param
        self.seed_plant_context(plant_id)
        st, _, html = self._req("GET", "/device/photovoltaic", params={"plantId": str(plant_id)}, timeout=30)
        log.info(f"GET /device/photovoltaic -> {st} (len={len(html)})")
        return html

    # ------------------------------------------------------------
    # JSON helper
    # ------------------------------------------------------------
    @staticmethod
    def _safe_json(txt: str) -> Any:
        try:
            import json
            return json.loads(txt)
        except Exception:
            return {"_non_json": True, "text": txt[:2000] if txt else ""}


"""
Next step (after probe is green):
- Extract inverter list (SNs) from plant scope
- Pull per-inverter daily energy and/or power curve
- Aggregate per-plant from inverter totals (sanity check vs plant totals)
"""
