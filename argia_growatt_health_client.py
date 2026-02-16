#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import math
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

LOG = logging.getLogger("argia.growatt.health")

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()

def normalize_sn(x: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(x)).upper()

def safe_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            s = x.replace(",", "").strip()
            if s == "":
                return default
            x = s
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default

def try_parse_json(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except Exception:
        return None


# ---------------------------------------------------------
# Auth
# ---------------------------------------------------------

@dataclass
class GrowattAuth:
    user: str
    password: str


# ---------------------------------------------------------
# Client
# ---------------------------------------------------------

class GrowattMonitoringClient:
    BASE = "https://server.growatt.com"

    AJAX_HEADERS = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://server.growatt.com",
        "Referer": "https://server.growatt.com/panel",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0",
    }

    def __init__(self, auth: GrowattAuth, timeout: int = 45):
        self.auth = auth
        self.timeout = timeout

        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0"})

    # ------------------------------------------------
    # LOGIN
    # ------------------------------------------------
    def login(self) -> None:
        self.s.get(self.BASE + "/login", timeout=self.timeout)

        self.s.post(
            self.BASE + "/login",
            data={"account": self.auth.user, "password": self.auth.password},
            timeout=self.timeout,
        )

        if "assToken" not in self.s.cookies.get_dict():
            raise RuntimeError("Growatt login failed")

        LOG.info("✅ Growatt login OK")

        # initialize UI session
        self.s.get(self.BASE + "/index", timeout=self.timeout)

    # ------------------------------------------------
    # PLANT CONTEXT
    # ------------------------------------------------
    def warm_plant_context(self, plant_id: str) -> None:
        self.s.get(self.BASE + "/panel", timeout=self.timeout)
        self.s.get(self.BASE + f"/panel/getPlantInfo?plantId={plant_id}", timeout=self.timeout)
        self.s.get(self.BASE + f"/device/photovoltaic?plantId={plant_id}", timeout=self.timeout)

        # required cookies
        self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")
        self.s.cookies.set("selPage", "%2Fpanel", domain="server.growatt.com", path="/")

        LOG.info("Plant session activated %s", plant_id)

    # ------------------------------------------------
    # DEVICE LIST
    # ------------------------------------------------
    def fetch_devices_best_for_sns(self, plant_id: str, sns: List[str]) -> Dict[str, Dict[str, Any]]:
        r = self.s.post(
            self.BASE + "/device/getMAXList",
            data={"plantId": plant_id, "currPage": "1", "pageSize": "50"},
            headers=self.AJAX_HEADERS,
            timeout=self.timeout
        )

        js = try_parse_json(r.text) or {}
        items = js.get("datas") or js.get("data") or js.get("rows") or []

        out = {}
        sns_set = {normalize_sn(x) for x in sns}

        for it in items:
            sn = normalize_sn(it.get("deviceSn") or it.get("sn"))
            if sn in sns_set:
                out[sn] = it

        LOG.info("Found %d devices in plant %s", len(out), plant_id)
        return out

    # ------------------------------------------------
    # REALTIME KPI (FINAL WORKING FLOW)
    # ------------------------------------------------
    def fetch_health_kpi_for_sn(self, plant_id: str, sn: str, device: Dict[str, Any]) -> Dict[str, Any]:
        plant_id = str(plant_id)
        sn = normalize_sn(sn)

        device_type = device.get("deviceType")
        datalog_sn = device.get("datalogSn")

        # STEP 1 — select inverter
        try:
            self.s.post(
                self.BASE + "/device/setDeviceSn",
                data={"sn": sn, "plantId": plant_id},
                headers=self.AJAX_HEADERS,
                timeout=self.timeout
            )
            LOG.info("Session device set %s", sn)
        except Exception:
            pass

        # STEP 2 — activate inverter protocol (CRITICAL)
        try:
            self.s.post(
                self.BASE + "/device/setDeviceType",
                data={"deviceType": device_type, "sn": sn},
                headers=self.AJAX_HEADERS,
                timeout=self.timeout
            )
            LOG.info("Device type activated %s type=%s", sn, device_type)
        except Exception:
            pass

        # STEP 3 — realtime request
        payload = {
            "plantId": plant_id,
            "deviceSn": sn,
            "deviceType": device_type,
            "datalogSn": datalog_sn
        }

        endpoints = [
            "/device/getInverterRealTimeData",
            "/device/getInvRealTimeData",
            "/device/getInverterDetailData"
        ]

        for ep in endpoints:
            try:
                r = self.s.post(self.BASE + ep, data=payload, headers=self.AJAX_HEADERS, timeout=self.timeout)
                js = try_parse_json(r.text)

                if js and ("data" in js or "datas" in js):
                    LOG.info("✅ KPI OK %s via %s", sn, ep)
                    return js.get("data") or js.get("datas")

            except Exception:
                continue

        LOG.warning("No KPI for %s", sn)
        return {}
