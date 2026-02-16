#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import math
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
            x = x.replace(",", "").strip()
            if x == "":
                return default
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


# ---------------------------------------------------------
# Client
# ---------------------------------------------------------

class GrowattMonitoringClient:
    BASE = "https://server.growatt.com"

    def __init__(self, auth: GrowattAuth, timeout: int = 45):
        self.auth = auth
        self.timeout = timeout

        # folder where workflow will upload artifacts
        self.debug_dir = "out_health"
        os.makedirs(self.debug_dir, exist_ok=True)

        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest"
        })

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

        # Important — initialize UI session
        self.s.get(self.BASE + "/index", timeout=self.timeout)

    # ------------------------------------------------
    # SESSION ACTIVATION (critical)
    # ------------------------------------------------
    def warm_plant_context(self, plant_id: str) -> None:
        self.s.get(self.BASE + "/panel", timeout=self.timeout)
        self.s.get(self.BASE + f"/panel/getPlantInfo?plantId={plant_id}", timeout=self.timeout)
        self.s.get(self.BASE + f"/device/photovoltaic?plantId={plant_id}", timeout=self.timeout)

        self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")

        LOG.info("Plant session activated %s", plant_id)

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
    # REALTIME DATA (with RAW JSON dump)
    # ------------------------------------------------
    def fetch_health_kpi_for_sn(self, plant_id: str, sn: str, device: Dict[str, Any]) -> Dict[str, Any]:

        payload = {
            "plantId": plant_id,
            "deviceSn": sn,
            "deviceType": device.get("deviceType"),
            "datalogSn": device.get("datalogSn"),
        }

        endpoints = [
            "/device/getInverterRealTimeData",
            "/device/getInvRealTimeData",
            "/device/getInverterDetailData2",
            "/device/getInverterDetailData",
        ]

        for ep in endpoints:
            try:
                r = self.s.post(self.BASE + ep, data=payload, timeout=self.timeout)

                js = try_parse_json(r.text)
                if not js:
                    continue

                # 🔥 SAVE RAW JSON FOR ANALYSIS
                try:
                    fname = f"{plant_id}__kpi_raw__{sn}__{ep.replace('/','_')}.json"
                    path = os.path.join(self.debug_dir, fname)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(js, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

                if js and ("data" in js or "datas" in js):
                    return js.get("data") or js.get("datas")

            except Exception:
                continue

        return {}
