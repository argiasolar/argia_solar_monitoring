import os
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

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
    Monitoring-only client for ShineServer (server.growatt.com).

    VERIFIED: your working login is in growatt_weather_fetch.py:
      - GET /login
      - POST /login with AJAX headers
      - must have assToken cookie

    This class implements THAT exact login flow, not the broken "200 + JSESSIONID" flow.
    """

    def __init__(self, auth: GrowattAuth, timeout_s: int = 45):
        self.auth = auth
        self.timeout_s = timeout_s
        self.base = _env("GROWATT_BASE", "https://server.growatt.com").rstrip("/")

        self.s = requests.Session()

        # Match your working UA + language hints (important sometimes)
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
    # Login (web UI flow, assToken)
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

        if "assToken" not in cookies:
            snippet = (r2.text or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(
                f"Login failed: assToken cookie missing. HTTP={r2.status_code} body_snippet='{snippet}'"
            )

        LOG.info("✅ Login OK (assToken present).")

    # -------------------------
    # Plant context cookies (same idea as weather script)
    # -------------------------
    def seed_plant_context(self, plant_id: str) -> None:
        self.s.cookies.set("selectedPlantId", str(plant_id))
        # these "selPage*" cookies matter for some device endpoints
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device")

    # -------------------------
    # HTTP helpers
    # -------------------------
    def _post_json(self, path: str, data: Dict[str, Any], referer_path: str = "/index") -> Any:
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
        url = f"{self.base}{path}"
        headers = {"Referer": f"{self.base}{referer_path}", "Accept": "application/json, text/javascript, */*; q=0.01"}
        r = self.s.get(url, headers=headers, params=params, timeout=self.timeout_s, allow_redirects=True)
        LOG.info("GET %s -> %s", path, r.status_code)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_non_json": True, "text": (r.text or "")[:2000]}

    # -------------------------
    # Probe helpers
    # -------------------------
    def auth_check(self) -> Tuple[bool, Any]:
        """
        Try a known logged-in AJAX endpoint. If it returns login HTML -> not logged in.
        We reuse /device/getEnvPage logic style indirectly by calling a JSON-ish endpoint.
        """
        js = self._get_json("/newPlantAPI.do", params={"op": "getPlantList"}, referer_path="/index")
        if isinstance(js, dict) and js.get("_non_json"):
            t = js.get("text", "")
            if "errorNoLogin" in t or "Login Page" in t or "dumpLogin" in t:
                return False, js
        return True, js

    # -------------------------
    # Inverter realtime (best-effort, after login)
    # -------------------------
    def get_inverter_realtime(self, plant_id: str, inverter_sn: str) -> Dict[str, Any]:
        """
        We don't guess the wheel; we do best-effort endpoint tries AFTER proper assToken login.
        Some endpoints require selectedPlantId cookie, so we seed it.
        """
        self.seed_plant_context(plant_id)

        # Candidate endpoints (we'll lock the real one once probe shows which works)
        candidates: List[Tuple[str, Dict[str, Any]]] = [
            ("/panel/inverter/getInverterData", {"sn": inverter_sn}),
            ("/indexbC/inv/getInvData", {"sn": inverter_sn}),
            ("/newInvAPI.do", {"op": "getInvData", "sn": inverter_sn}),
        ]

        last: Optional[Exception] = None
        for path, params in candidates:
            try:
                js = self._get_json(path, params=params, referer_path="/index")
                if isinstance(js, dict) and js.get("_non_json"):
                    # likely HTML; try next
                    continue
                return js if isinstance(js, dict) else {"_raw": js}
            except Exception as e:
                last = e

        raise RuntimeError(f"All realtime endpoint tries failed for sn={inverter_sn}. last={last}")
