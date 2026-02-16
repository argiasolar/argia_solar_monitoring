#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, math, logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

LOG = logging.getLogger("argia.growatt.health")

INVALID_FS_CHARS = r'["<>:|*?\r\n]'


def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()


def normalize_sn(x: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(x)).upper()


def safe_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            s = x.strip().replace(",", "")
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


@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    BASE = "https://server.growatt.com"

    def __init__(self, auth: GrowattAuth, timeout: int = 45):
        self.auth = auth
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (ARGIA Growatt Health Bot)"})

    # ------------------------------------------------
    # LOGIN
    # ------------------------------------------------
    def login(self) -> None:
        self.s.get(self.BASE + "/login", timeout=self.timeout)
        r = self.s.post(
            self.BASE + "/login",
            data={"account": self.auth.user, "password": self.auth.password},
            timeout=self.timeout,
        )
        if "assToken" not in self.s.cookies.get_dict():
            raise RuntimeError("Growatt login failed")
        LOG.info("✅ Growatt login OK")

    def warm_plant_context(self, plant_id: str) -> None:
        self.s.get(self.BASE + "/device")
        self.s.get(self.BASE + f"/device/photovoltaic?plantId={plant_id}")

    # ------------------------------------------------
    # DEVICE LIST
    # ------------------------------------------------
    def fetch_devices_best_for_sns(self, plant_id: str, sns: List[str]) -> Dict[str, Dict[str, Any]]:
        r = self.s.post(
            self.BASE + "/device/getMAXList",
            data={"plantId": plant_id, "currPage": "1", "pageSize": "50"},
            timeout=self.timeout,
        )
        js = try_parse_json(r.text) or {}
        items = js.get("datas") or js.get("data") or js.get("rows") or []

        out = {}
        for it in items:
            sn = normalize_sn(it.get("deviceSn") or it.get("sn") or "")
            if sn in sns:
                out[sn] = it

        LOG.info("Found %d devices in plant %s", len(out), plant_id)
        return out

    # ------------------------------------------------
    # 🔥 REALTIME KPI (FIXED — includes deviceType)
    # ------------------------------------------------
    def fetch_health_kpi_for_sn(self, plant_id: str, sn: str, device: Dict[str, Any]) -> Dict[str, Any]:
        sn = normalize_sn(sn)

        device_type = device.get("deviceType") or device.get("type")
        datalog_sn = device.get("datalogSn") or device.get("collectorSn")

        payload = {
            "plantId": str(plant_id),
            "deviceSn": sn,
            "deviceType": device_type,
        }

        if datalog_sn:
            payload["datalogSn"] = datalog_sn

        endpoints = [
            "/device/getInverterRealTimeData",
            "/device/getInverterDetailData",
            "/device/getInverterDetailData2",
            "/panel/getDeviceData",
        ]

        for ep in endpoints:
            try:
                r = self.s.post(self.BASE + ep, data=payload, timeout=self.timeout)
                js = try_parse_json(r.text)
                if not js:
                    continue

                flat = {}

                def walk(obj):
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            key = re.sub(r"[^a-zA-Z0-9]", "", k).lower()
                            flat[key] = v
                            walk(v)
                    elif isinstance(obj, list):
                        for i in obj:
                            walk(i)

                walk(js)

                if any("pv" in k or "str" in k for k in flat.keys()):
                    flat["_endpoint"] = ep
                    return flat

            except Exception:
                pass

        return {}
