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


def _safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^\w\-\.]+", "_", s)
    return s.strip("_")[:180]


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

        self.debug_dir = "out_health"
        os.makedirs(self.debug_dir, exist_ok=True)

        # Marker file so artifact upload ALWAYS finds something
        try:
            with open(os.path.join(self.debug_dir, f"RUN_MARKER_{int(time.time())}.txt"), "w", encoding="utf-8") as f:
                f.write("run started\n")
        except Exception:
            pass

        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
        })

    def _save_text(self, plant_id: str, sn: str, endpoint: str, text: str) -> None:
        fn = f"{plant_id}__{sn}__{_safe_filename(endpoint)}.txt"
        path = os.path.join(self.debug_dir, fn)
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            f.write(text or "")

    def _save_json(self, plant_id: str, sn: str, endpoint: str, obj: Any) -> None:
        fn = f"{plant_id}__{sn}__{_safe_filename(endpoint)}.json"
        path = os.path.join(self.debug_dir, fn)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

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
            raise RuntimeError("Growatt login failed (assToken missing)")

        LOG.info("✅ Growatt login OK")

        # Initialize UI session
        self.s.get(self.BASE + "/index", timeout=self.timeout)

    # ------------------------------------------------
    # SESSION ACTIVATION (critical for KPI endpoints)
    # ------------------------------------------------
    def warm_plant_context(self, plant_id: str) -> None:
        # Open panel UI, select plant, open device page (browser behavior)
        self.s.get(self.BASE + "/panel", timeout=self.timeout)
        self.s.get(self.BASE + f"/panel/getPlantInfo?plantId={plant_id}", timeout=self.timeout)
        self.s.get(self.BASE + f"/device/photovoltaic?plantId={plant_id}", timeout=self.timeout)

        # Some endpoints rely on this cookie
        try:
            self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")
            self.s.cookies.set("selPage", "%2Fpanel", domain="server.growatt.com", path="/")
        except Exception:
            pass

        LOG.info("Plant session activated %s", plant_id)

    # ------------------------------------------------
    # DEVICE LIST (Status + deviceType + datalogSn)
    # ------------------------------------------------
    def fetch_devices_best_for_sns(self, plant_id: str, sns: List[str]) -> Dict[str, Dict[str, Any]]:
        r = self.s.post(
            self.BASE + "/device/getMAXList",
            data={"plantId": str(plant_id), "currPage": "1", "pageSize": "50"},
            timeout=self.timeout,
            headers={"Referer": self.BASE + "/device"},
        )
        js = try_parse_json(r.text) or {}
        items = js.get("datas") or js.get("data") or js.get("rows") or []

        sns_set = {normalize_sn(x) for x in sns if x}
        out: Dict[str, Dict[str, Any]] = {}
        for it in items:
            sn = normalize_sn(it.get("deviceSn") or it.get("sn") or "")
            if sn in sns_set:
                out[sn] = it

        LOG.info("Found %d devices in plant %s", len(out), plant_id)
        return out

    # ------------------------------------------------
    # REALTIME DATA (ALWAYS writes raw response files)
    # ------------------------------------------------
    def fetch_health_kpi_for_sn(self, plant_id: str, sn: str, device: Dict[str, Any]) -> Dict[str, Any]:
        plant_id = str(plant_id)
        sn = normalize_sn(sn)

        payload = {
            "plantId": plant_id,
            "deviceSn": sn,
            "deviceType": device.get("deviceType") or device.get("type") or device.get("deviceTypeNum"),
            "datalogSn": device.get("datalogSn") or device.get("collectorSn") or device.get("dataloggerSn"),
        }

        endpoints = [
            "/device/getInverterRealTimeData",
            "/device/getInvRealTimeData",
            "/device/getInverterDetailData2",
            "/device/getInverterDetailData",
            "/panel/getDeviceData",
            "/panel/getInverterData",
        ]

        last_json: Optional[dict] = None

        for ep in endpoints:
            try:
                r = self.s.post(
                    self.BASE + ep,
                    data=payload,
                    timeout=self.timeout,
                    headers={"Referer": self.BASE + "/panel"},
                )

                txt = r.text or ""
                # ALWAYS save raw response (even if it's HTML)
                self._save_text(plant_id, sn, ep, txt[:20000])

                js = try_parse_json(txt)
                if js:
                    last_json = js
                    # also save JSON
                    self._save_json(plant_id, sn, ep, js)

                    # Return the typical container if present
                    if isinstance(js, dict):
                        if "data" in js and js["data"] is not None:
                            out = js["data"]
                            if isinstance(out, dict):
                                out["_endpoint"] = ep
                            return out if isinstance(out, dict) else {"_endpoint": ep, "value": out}
                        if "datas" in js and js["datas"] is not None:
                            out = js["datas"]
                            if isinstance(out, dict):
                                out["_endpoint"] = ep
                            return out if isinstance(out, dict) else {"_endpoint": ep, "value": out}

            except Exception as e:
                # Save exception info
                self._save_text(plant_id, sn, ep + "__EXCEPTION", repr(e))
                continue

        # No structured data. Save a summary file too.
        summary = {
            "plantId": plant_id,
            "sn": sn,
            "payload": payload,
            "cookies": self.s.cookies.get_dict(),
            "last_json_present": bool(last_json),
            "tried_endpoints": endpoints,
        }
        self._save_json(plant_id, sn, "SUMMARY", summary)

        return {}
