#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import math
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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

def _safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^\w\-\.]+", "_", s)
    return s.strip("_")[:180]

def now_ms() -> int:
    return int(time.time() * 1000)


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

    def __init__(self, auth: GrowattAuth, timeout: int = 45, debug_dir: str = "out_health"):
        self.auth = auth
        self.timeout = timeout
        self.debug_dir = debug_dir
        os.makedirs(self.debug_dir, exist_ok=True)

        # Marker so artifact always uploads
        try:
            with open(os.path.join(self.debug_dir, f"RUN_MARKER_{int(time.time())}.txt"), "w", encoding="utf-8") as f:
                f.write("run started\n")
        except Exception:
            pass

        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0"})

    # ---------------------------
    # debug save
    # ---------------------------
    def _save_text(self, plant_id: str, sn: str, label: str, text: str) -> None:
        fn = f"{plant_id}__{sn}__{_safe_filename(label)}.txt"
        path = os.path.join(self.debug_dir, fn)
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            f.write(text or "")

    def _save_json(self, plant_id: str, sn: str, label: str, obj: Any) -> None:
        fn = f"{plant_id}__{sn}__{_safe_filename(label)}.json"
        path = os.path.join(self.debug_dir, fn)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    # ---------------------------
    # low-level http
    # ---------------------------
    def _post(self, path: str, data: dict, referer: Optional[str] = None) -> Tuple[int, str]:
        headers = dict(self.AJAX_HEADERS)
        if referer:
            headers["Referer"] = referer
        r = self.s.post(self.BASE + path, data=data, headers=headers, timeout=self.timeout)
        return r.status_code, (r.text or "")

    def _get_plain(self, path: str, params: Optional[dict] = None) -> Tuple[int, str]:
        # plain GET used for UI navigation (HTML pages)
        r = self.s.get(self.BASE + path, params=params or {}, timeout=self.timeout)
        return r.status_code, (r.text or "")

    # ---------------------------
    # login + context
    # ---------------------------
    def login(self) -> None:
        self._get_plain("/login")
        sc, txt = self._post("/login", {"account": self.auth.user, "password": self.auth.password}, referer="https://server.growatt.com/login")
        # cookies after login
        if "assToken" not in self.s.cookies.get_dict():
            # save response for debugging
            self._save_text("LOGIN", "LOGIN", "login_failed_response", (txt or "")[:20000])
            raise RuntimeError("Growatt login failed (assToken missing)")
        LOG.info("✅ Growatt login OK")
        self._get_plain("/index")

    def warm_plant_context(self, plant_id: str) -> None:
        # mimic browser navigation
        self._get_plain("/panel")
        self._get_plain("/panel/getPlantInfo", params={"plantId": str(plant_id)})
        self._get_plain("/device/photovoltaic", params={"plantId": str(plant_id)})

        # cookies used by Growatt UI
        try:
            self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")
            self.s.cookies.set("selPage", "%2Fpanel", domain="server.growatt.com", path="/")
        except Exception:
            pass

        LOG.info("Plant session activated %s", plant_id)

    # ---------------------------
    # device list
    # ---------------------------
    def fetch_devices_best_for_sns(self, plant_id: str, sns: List[str]) -> Dict[str, Dict[str, Any]]:
        sns_set = {normalize_sn(x) for x in sns if x}

        sc, txt = self._post(
            "/device/getMAXList",
            {"plantId": str(plant_id), "currPage": "1", "pageSize": "50"},
            referer="https://server.growatt.com/device",
        )
        js = try_parse_json(txt) or {}
        items = js.get("datas") or js.get("data") or js.get("rows") or []

        out: Dict[str, Dict[str, Any]] = {}
        for it in items:
            sn = normalize_sn(it.get("deviceSn") or it.get("sn") or it.get("invSn") or "")
            if sn and sn in sns_set:
                out[sn] = it

        LOG.info("Found %d devices in plant %s", len(out), plant_id)
        return out

    # ---------------------------
    # KPI fetch (CRITICAL: open inverter detail page first)
    # ---------------------------
    def fetch_health_kpi_for_sn(self, plant_id: str, sn: str, device: Dict[str, Any]) -> Dict[str, Any]:
        plant_id = str(plant_id)
        sn = normalize_sn(sn)

        device_type = device.get("deviceType") or device.get("deviceTypeNum") or device.get("type")
        datalog_sn = device.get("datalogSn") or device.get("collectorSn") or device.get("dataloggerSn")
        device_id = device.get("deviceId") or device.get("id") or device.get("invId")

        # ----------------------------------------------------
        # STEP 1 — open inverter pages (THIS UNLOCKS SESSION)
        # ----------------------------------------------------
        try:
            self._get_plain("/device/inverter", params={"plantId": plant_id})
            # different Growatt builds use different URLs; try both
            self._get_plain("/device/inverterDetail", params={"sn": sn, "plantId": plant_id})
            self._get_plain("/device/getInverterPage", params={"plantId": plant_id})
            LOG.info("Inverter page opened %s", sn)
        except Exception:
            pass

        # ----------------------------------------------------
        # STEP 2 — POST ONLY (Growatt returns 405 on GET)
        # ----------------------------------------------------
        payload_variants: List[dict] = [
            {"plantId": plant_id, "deviceSn": sn, "deviceType": device_type, "datalogSn": datalog_sn},
            {"plantId": plant_id, "sn": sn, "deviceType": device_type, "datalogSn": datalog_sn},
            {"plantId": plant_id, "invSn": sn, "deviceType": device_type, "datalogSn": datalog_sn},
            {"plantId": plant_id, "serialNum": sn, "deviceType": device_type, "datalogSn": datalog_sn},
        ]
        if device_id:
            payload_variants.append({"plantId": plant_id, "deviceId": device_id, "deviceType": device_type, "datalogSn": datalog_sn})

        # clean empties
        def clean(d: dict) -> dict:
            return {k: v for k, v in d.items() if v not in (None, "", "null")}

        payload_variants = [clean(p) for p in payload_variants]

        endpoints = [
            "/device/getInverterRealTimeData",
            "/device/getInvRealTimeData",
            "/device/getInverterDetailData2",
            "/device/getInverterDetailData",
        ]

        for ep in endpoints:
            for p in payload_variants:
                try:
                    sc, txt = self._post(ep, p, referer="https://server.growatt.com/device")
                    # always save raw response
                    self._save_text(plant_id, sn, f"{ep}__POST__{sc}", (txt or "")[:20000])

                    js = try_parse_json(txt)
                    if js:
                        self._save_json(plant_id, sn, f"RAW__{ep}__POST__{sc}", js)

                    if isinstance(js, dict) and (js.get("data") is not None or js.get("datas") is not None):
                        out = js.get("data") if js.get("data") is not None else js.get("datas")
                        if isinstance(out, dict):
                            out["_endpoint"] = ep
                        LOG.info("✅ KPI OK %s via %s", sn, ep)
                        return out if isinstance(out, dict) else {"_endpoint": ep, "value": out}

                except Exception as e:
                    self._save_text(plant_id, sn, f"{ep}__EXCEPTION", repr(e))

        # summary for this inverter
        summary = {
            "plantId": plant_id,
            "sn": sn,
            "deviceType": device_type,
            "datalogSn": datalog_sn,
            "deviceId": device_id,
            "cookies": self.s.cookies.get_dict(),
            "tried_endpoints": endpoints,
            "payload_variants": payload_variants,
        }
        self._save_json(plant_id, sn, "SUMMARY", summary)

        LOG.warning("No KPI for %s", sn)
        return {}
