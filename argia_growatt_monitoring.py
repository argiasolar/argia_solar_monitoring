import os
import json
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

LOG = logging.getLogger("argia.growatt.monitoring")


@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    """
    Monitoring client used ONLY by argia_probe.py / argia_snap.py
    It will NOT change your existing argia_growatt.py.

    Strategy:
    1) Attempt to import and reuse existing repo logic (if present).
    2) Otherwise fallback to a minimal requests-session login.
    """

    def __init__(self, auth: GrowattAuth, timeout_s: int = 30):
        self.auth = auth
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "argia-monitoring/1.0",
            "Accept": "application/json, text/plain, */*",
        })

        self._reuse_mode = None
        self._reuse_obj = None

    # -------------------------
    # Reuse existing argia_growatt if possible
    # -------------------------
    def try_reuse_existing_repo_client(self) -> bool:
        """
        If your repo already has robust Growatt login + calls, reuse them.
        We do NOT import argia.py, only attempt argia_growatt.py.

        If this fails, we fall back to our own login.
        """
        try:
            import argia_growatt  # type: ignore

            # Common patterns you might already have:
            # - argia_growatt.login_server(session, user, pass)
            # - argia_growatt.fetch_* functions
            # - a GrowattClient class

            if hasattr(argia_growatt, "login_server"):
                self._reuse_mode = "functions_login_server"
                self._reuse_obj = argia_growatt
                LOG.info("✅ Reusing existing argia_growatt.login_server()")
                return True

            if hasattr(argia_growatt, "GrowattClient"):
                self._reuse_mode = "class_GrowattClient"
                self._reuse_obj = argia_growatt.GrowattClient(self.auth.user, self.auth.password)  # type: ignore
                LOG.info("✅ Reusing existing argia_growatt.GrowattClient")
                return True

            LOG.warning("argia_growatt imported but no known reusable entrypoint found.")
            return False

        except Exception as e:
            LOG.warning("Could not reuse argia_growatt (%s). Falling back to monitoring client.", e)
            return False

    def login(self) -> None:
        if self._reuse_mode is None:
            self.try_reuse_existing_repo_client()

        if self._reuse_mode == "functions_login_server":
            # Reuse your repo’s login (best)
            self._reuse_obj.login_server(self.session, self.auth.user, self.auth.password)  # type: ignore
            return

        if self._reuse_mode == "class_GrowattClient":
            # Assume the class handles its own auth
            return

        # Fallback minimal login: (you already saw 'assToken cookie missing' before)
        self._fallback_login()

    def _fallback_login(self) -> None:
        """
        Minimal session login.
        NOTE: Growatt may change endpoints; this is why probe mode exists.
        """
        # These endpoints are intentionally configurable via env if needed later.
        base = os.getenv("GROWATT_BASE_URL", "https://server.growatt.com")
        login_url = os.getenv("GROWATT_LOGIN_URL", f"{base}/login")

        payload = {
            "account": self.auth.user,
            "password": self.auth.password,
        }

        LOG.info("🔐 Fallback login -> %s", login_url)
        r = self.session.post(login_url, data=payload, timeout=self.timeout_s, allow_redirects=True)

        LOG.info("Login HTTP %s, len=%s", r.status_code, len(r.text or ""))
        LOG.debug("Login response headers: %s", dict(r.headers))
        LOG.debug("Cookies after login: %s", self.session.cookies.get_dict())

        # Heuristic: your earlier error referenced assToken cookie
        cookies = self.session.cookies.get_dict()
        if "assToken" not in cookies and "JSESSIONID" not in cookies:
            raise RuntimeError(
                "Login likely failed: expected auth cookies not present. "
                "Run argia_probe.py to inspect latest login behavior."
            )

    # -------------------------
    # Generic request helpers
    # -------------------------
    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = self.session.get(url, params=params, timeout=self.timeout_s)
        LOG.info("GET %s -> %s", r.url, r.status_code)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_non_json": True, "text": r.text[:2000]}

    def _post_json(self, url: str, data: Optional[Dict[str, Any]] = None) -> Any:
        r = self.session.post(url, data=data, timeout=self.timeout_s)
        LOG.info("POST %s -> %s", r.url, r.status_code)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_non_json": True, "text": r.text[:2000]}

    # -------------------------
    # PROBE methods (discover fields/endpoints)
    # -------------------------
    def probe_endpoints(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """
        Attempts multiple known-ish endpoints and returns responses.
        This is intentionally verbose; it’s how we map what Growatt returns for your account.
        """
        base = os.getenv("GROWATT_BASE_URL", "https://server.growatt.com")

        candidates = [
            ("GET", f"{base}/indexbC/inv/getInvList", {}),
            ("GET", f"{base}/indexbC/plant/getPlantList", {}),
            ("GET", f"{base}/panel/plant/getPlantList", {}),
            ("GET", f"{base}/newPlantAPI.do", {"op": "getPlantList"}),
        ]

        out: List[Tuple[str, str, Dict[str, Any]]] = []
        for method, url, params in candidates:
            try:
                if method == "GET":
                    js = self._get_json(url, params=params)
                else:
                    js = self._post_json(url, data=params)
                out.append((method, url, {"ok": True, "params": params, "json": js}))
            except Exception as e:
                out.append((method, url, {"ok": False, "params": params, "error": str(e)}))
        return out

    # -------------------------
    # SNAPSHOT methods (30-min data)
    # -------------------------
    def list_inverters_for_plant(self, plant_id: str) -> List[Dict[str, Any]]:
        """
        Tries to return inverter/device list for a plant.
        In probe mode we’ll discover which endpoint works for your account.
        """
        if self._reuse_mode == "class_GrowattClient":
            # If your repo's GrowattClient supports something like this, add mapping later.
            raise NotImplementedError("Reuse mode class client not yet mapped to list_inverters_for_plant.")

        base = os.getenv("GROWATT_BASE_URL", "https://server.growatt.com")

        # Try multiple likely endpoints
        candidates = [
            (f"{base}/indexbC/inv/getInvList", {"plantId": plant_id}),
            (f"{base}/panel/inverter/getInverterList", {"plantId": plant_id}),
            (f"{base}/newInvAPI.do", {"op": "getInvList", "plantId": plant_id}),
        ]

        last_err = None
        for url, params in candidates:
            try:
                js = self._get_json(url, params=params)
                # Normalize list extraction:
                if isinstance(js, dict):
                    for k in ["data", "obj", "rows", "result", "list"]:
                        if k in js and isinstance(js[k], list):
                            return js[k]
                if isinstance(js, list):
                    return js
                # If dict but unknown structure, return as singleton
                return [{"_raw": js}]
            except Exception as e:
                last_err = e

        raise RuntimeError(f"Could not list inverters for plant {plant_id}: {last_err}")

    def get_inverter_realtime(self, inverter_sn: str) -> Dict[str, Any]:
        """
        Pull “current” / realtime data (often includes power, today energy, total energy).
        We’ll log raw response and normalize useful fields in argia_snap.py.
        """
        if self._reuse_mode == "class_GrowattClient":
            raise NotImplementedError("Reuse mode class client not yet mapped to get_inverter_realtime.")

        base = os.getenv("GROWATT_BASE_URL", "https://server.growatt.com")

        candidates = [
            (f"{base}/panel/inverter/getInverterData", {"sn": inverter_sn}),
            (f"{base}/indexbC/inv/getInvData", {"sn": inverter_sn}),
            (f"{base}/newInvAPI.do", {"op": "getInvData", "sn": inverter_sn}),
        ]

        last_err = None
        for url, params in candidates:
            try:
                js = self._get_json(url, params=params)
                if isinstance(js, dict):
                    return js
                return {"_raw": js}
            except Exception as e:
                last_err = e

        raise RuntimeError(f"Could not get realtime for inverter {inverter_sn}: {last_err}")
