#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA – Growatt Health Client
-----------------------------
Focused client for health monitoring (KPI/string values) via:
  - /device/getInverterList2   (list inverters per plant)
  - /panel/getDeviceInfo       (detailed device info / KPI-like fields)

Notes:
- We intentionally do NOT call any "set/config" endpoints.
- We mimic AJAX headers for /panel endpoints (Growatt sometimes returns "not login" otherwise).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

LOG = logging.getLogger("argia.growatt.health")


INVALID_FS_CHARS = r'["<>:|*?\r\n]'


def safe_filename(name: str) -> str:
    name = re.sub(INVALID_FS_CHARS, "_", name)
    name = name.replace("/", "_").strip("_")
    return name


def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()


def normalize_sn(x: Any) -> str:
    return normalize_text(x).replace(" ", "").upper()


def safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        if isinstance(x, str):
            x = x.strip().replace(",", "")
        return float(x)
    except Exception:
        return None


@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    """
    Minimal Growatt web client (server.growatt.com).
    """

    BASE = "https://server.growatt.com"

    # Safety blacklist (never call config endpoints)
    UNSAFE_PREFIXES = ("/commonDeviceSetC/",)
    UNSAFE_CONTAINS = (
        "setmax", "settlx", "setinverter",
        "delmax", "deltlx", "delinverter",
        "delete", "set", "save",
    )

    def __init__(self, auth: GrowattAuth, timeout: int = 45, debug_out_dir: str = "out_health"):
        self.auth = auth
        self.timeout = timeout
        self.debug_out_dir = debug_out_dir
        self.s = requests.Session()

        # Global headers that help prevent "not login" / blocked ajax
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/121.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.8,es;q=0.7",
            "Connection": "keep-alive",
        })

    # ------------
    # Low-level HTTP
    # ------------
    def _is_unsafe(self, path: str) -> bool:
        p = (path or "").lower()
        if any(p.startswith(x) for x in self.UNSAFE_PREFIXES):
            return True
        if any(x in p for x in self.UNSAFE_CONTAINS):
            return True
        return False

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.BASE + path

    def _dump_debug(self, name: str, content: str) -> None:
        # The caller should ensure directory exists; we keep this simple.
        try:
            fn = safe_filename(name)
            with open(f"{self.debug_out_dir}/{fn}", "w", encoding="utf-8") as f:
                f.write(content if content is not None else "")
        except Exception:
            # never crash on debug dumps
            pass

    def get(self, path: str, params: Optional[dict] = None, referer: Optional[str] = None) -> requests.Response:
        if self._is_unsafe(path):
            raise RuntimeError(f"Blocked unsafe endpoint: {path}")

        headers = {}
        if referer:
            headers["Referer"] = referer

        r = self.s.get(self._url(path), params=params, headers=headers, timeout=self.timeout, allow_redirects=True)
        return r

    def post(
        self,
        path: str,
        data: Optional[dict] = None,
        referer: Optional[str] = None,
        ajax: bool = True,
    ) -> requests.Response:
        if self._is_unsafe(path):
            raise RuntimeError(f"Blocked unsafe endpoint: {path}")

        headers = {}
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        r = self.s.post(self._url(path), data=data or {}, headers=headers, timeout=self.timeout, allow_redirects=True)
        return r

    # ------------
    # Auth + session warm-up
    # ------------
    def login(self) -> None:
        # Warm login page (sets cookies)
        r1 = self.get("/login")
        LOG.info("GET /login -> %s", r1.status_code)

        payload = {
            "account": self.auth.user,
            "password": self.auth.password,
        }
        r2 = self.post("/login", data=payload, referer=self._url("/login"), ajax=False)
        LOG.info("POST /login -> %s (len=%s)", r2.status_code, len(r2.text or ""))

        # Growatt sets assToken cookie on success (common)
        cookies = {c.name: c.value for c in self.s.cookies}
        if "assToken" not in cookies and "JSESSIONID" not in cookies:
            # Dump response for diagnosis
            self._dump_debug("LOGIN_RESPONSE.html", r2.text or "")
            raise RuntimeError("Growatt login failed: missing session cookies (assToken/JSESSIONID).")

        LOG.info("✅ Growatt login OK")

    def activate_plant_session(self, plant_id: str) -> None:
        """
        Loads plant pages that typically establish server-side session context.
        """
        pid = str(plant_id).strip()
        # These GETs are safe and help with later AJAX calls.
        self.get("/device/getEnvPage", params={"plantId": pid}, referer=self._url("/device"))
        self.get("/device/getInverterPage", params={"plantId": pid}, referer=self._url("/device"))
        self.get("/device/getMAXPage", params={"ttt": str(int(time.time() * 1000))}, referer=self._url("/device"))
        LOG.info("Plant session activated %s", pid)

    # ------------
    # Data endpoints
    # ------------
    def list_inverters(self, plant_id: str, inv_sn_filter: str = "", curr_page: int = 1) -> List[Dict[str, Any]]:
        """
        Returns list of inverter devices for plant. Uses the same endpoint as the UI HTML:
          /device/getInverterList2  (GET, params plantId, invSn, currPage)

        Response commonly: { datas:[...], count, currPage, pageSize, ... }
        """
        pid = str(plant_id).strip()
        params = {"plantId": pid, "invSn": inv_sn_filter, "currPage": int(curr_page)}
        r = self.get("/device/getInverterList2", params=params, referer=self._url("/device/getInverterPage?plantId=" + pid))

        # Some Growatt returns JSON with correct header, some returns text; handle both.
        try:
            data = r.json()
        except Exception:
            self._dump_debug(f"{pid}__getInverterList2__{r.status_code}.txt", (r.text or "")[:200000])
            return []

        datas = data.get("datas") or data.get("data") or []
        if not isinstance(datas, list):
            datas = []
        return datas

    def get_device_info(self, plant_id: str, device_type_name: str, sn: str) -> Optional[Dict[str, Any]]:
        """
        Mimics UI tooltip AJAX:
          POST /panel/getDeviceInfo  data: {plantId, deviceTypeName, sn}

        Returns "obj" dict (or None).
        """
        pid = str(plant_id).strip()
        snn = normalize_sn(sn)
        dtype = (device_type_name or "tlx").strip()

        r = self.post(
            "/panel/getDeviceInfo",
            data={"plantId": pid, "deviceTypeName": dtype, "sn": snn},
            referer=self._url("/device/getInverterPage?plantId=" + pid),
            ajax=True,
        )

        # Growatt sometimes answers 200 with HTML "not login" or similar. Detect and dump.
        txt = r.text or ""
        if r.status_code != 200:
            self._dump_debug(f"{pid}__{snn}__panel_getDeviceInfo__POST__{r.status_code}.txt", txt[:200000])
            return None

        # If it isn't JSON, dump and bail
        try:
            js = r.json()
        except Exception:
            self._dump_debug(f"{pid}__{snn}__panel_getDeviceInfo__NOT_JSON.txt", txt[:200000])
            return None

        obj = js.get("obj") or js.get("data") or js.get("datas")
        if isinstance(obj, dict):
            return obj

        # Some installations may wrap differently
        if isinstance(js, dict) and any(k in js for k in ("result", "msg")):
            self._dump_debug(f"{pid}__{snn}__panel_getDeviceInfo__WRAP.json", json.dumps(js, ensure_ascii=False, indent=2)[:200000])

        return None
