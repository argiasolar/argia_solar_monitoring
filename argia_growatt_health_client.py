#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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

    BASE = "https://server.growatt.com"

    def __init__(self, auth: GrowattAuth, timeout: int = 45, debug_out_dir: str = "out_health"):
        self.auth = auth
        self.timeout = timeout
        self.debug_out_dir = debug_out_dir
        self.s = requests.Session()

        self.s.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.8,es;q=0.7",
            "Connection": "keep-alive",
        })

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.BASE + path

    def _dump_debug(self, name: str, content: str) -> None:
        try:
            fn = safe_filename(name)
            with open(f"{self.debug_out_dir}/{fn}", "w", encoding="utf-8") as f:
                f.write(content if content else "")
        except Exception:
            pass

    def get(self, path: str, params: Optional[dict] = None, referer: Optional[str] = None) -> requests.Response:
        headers = {}
        if referer:
            headers["Referer"] = referer
        return self.s.get(self._url(path), params=params, headers=headers, timeout=self.timeout, allow_redirects=True)

    def post(self, path: str, data: Optional[dict] = None, referer: Optional[str] = None, ajax: bool = True) -> requests.Response:
        headers = {}
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        return self.s.post(self._url(path), data=data or {}, headers=headers, timeout=self.timeout, allow_redirects=True)

    # ---------------- LOGIN ----------------

    def login(self) -> None:
        r1 = self.get("/login")
        LOG.info("GET /login -> %s", r1.status_code)

        payload = {"account": self.auth.user, "password": self.auth.password}
        r2 = self.post("/login", data=payload, referer=self._url("/login"), ajax=False)
        LOG.info("POST /login -> %s", r2.status_code)

        cookies = {c.name: c.value for c in self.s.cookies}
        if "JSESSIONID" not in cookies and "assToken" not in cookies:
            self._dump_debug("LOGIN_FAIL.html", r2.text)
            raise RuntimeError("Growatt login failed")

        LOG.info("✅ Growatt login OK")

    # ---------------- SESSION ----------------

    def activate_plant_session(self, plant_id: str) -> None:
        pid = str(plant_id)
        self.get("/device/getEnvPage", params={"plantId": pid})
        self.get("/device/getInverterPage", params={"plantId": pid})
        self.get("/device/getMAXPage", params={"ttt": str(int(time.time()*1000))})
        LOG.info("Plant session activated %s", pid)

    # ---------------- FIXED INVERTER LIST ----------------

    def list_inverters(self, plant_id: str, inv_sn_filter: str = "", curr_page: int = 1) -> List[Dict[str, Any]]:
        """
        POST /device/getInverterList2
        IMPORTANT: MUST BE POST (Growatt rejects GET with 405)
        """

        pid = str(plant_id)

        r = self.post(
            "/device/getInverterList2",
            data={
                "plantId": pid,
                "invSn": inv_sn_filter,
                "currPage": str(curr_page),
            },
            referer=self._url("/device/getInverterPage?plantId=" + pid),
            ajax=True,
        )

        txt = r.text or ""

        if r.status_code != 200:
            self._dump_debug(f"{pid}__getInverterList2__POST__{r.status_code}.txt", txt[:200000])
            return []

        try:
            js = r.json()
        except Exception:
            self._dump_debug(f"{pid}__getInverterList2__NOT_JSON.txt", txt[:200000])
            return []

        datas = js.get("datas") or js.get("data") or []
        if not isinstance(datas, list):
            return []

        LOG.info("Found %s devices in plant %s", len(datas), pid)
        return datas

    # ---------------- DEVICE INFO ----------------

    def get_device_info(self, plant_id: str, device_type_name: str, sn: str) -> Optional[Dict[str, Any]]:
        pid = str(plant_id)
        snn = normalize_sn(sn)

        r = self.post(
            "/panel/getDeviceInfo",
            data={"plantId": pid, "deviceTypeName": device_type_name or "tlx", "sn": snn},
            referer=self._url("/device/getInverterPage?plantId=" + pid),
            ajax=True,
        )

        txt = r.text or ""

        if r.status_code != 200:
            self._dump_debug(f"{pid}__{snn}__getDeviceInfo__{r.status_code}.txt", txt[:200000])
            return None

        try:
            js = r.json()
        except Exception:
            self._dump_debug(f"{pid}__{snn}__getDeviceInfo__NOT_JSON.txt", txt[:200000])
            return None

        obj = js.get("obj") or js.get("data") or js.get("datas")
        if isinstance(obj, dict):
            return obj

        self._dump_debug(f"{pid}__{snn}__getDeviceInfo__EMPTY.json", json.dumps(js, indent=2))
        return None
