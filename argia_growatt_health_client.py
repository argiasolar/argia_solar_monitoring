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

def discover_ajax_urls(html: str) -> List[str]:
    """
    Pull URLs from Growatt UI HTML. Often contains the real KPI endpoints.
    """
    urls: List[str] = []
    if not html:
        return urls
    # url: '/device/getInverterRealTimeData'
    for m in re.finditer(r"url\s*:\s*['\"](\/[^'\"]+)['\"]", html):
        urls.append(m.group(1))
    # $.post('/panel/getDeviceData', ...)
    for m in re.finditer(r"\$\.(?:post|get)\(\s*['\"](\/[^'\"]+)['\"]", html):
        urls.append(m.group(1))
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def is_readish_endpoint(u: str) -> bool:
    u2 = (u or "").lower()
    if not u2.startswith("/"):
        return False
    # avoid obvious dangerous endpoints
    bad = ("set", "save", "delete", "del", "add", "update")
    if any(tok in u2 for tok in bad):
        return False
    # prefer data/real/detail/list/info
    ok = ("data", "real", "detail", "kpi", "info", "status", "list")
    return any(tok in u2 for tok in ok)

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

        # Marker file so artifacts always exist
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
    def _post(self, path: str, data: dict) -> Tuple[int, str]:
        r = self.s.post(self.BASE + path, data=data, headers=self.AJAX_HEADERS, timeout=self.timeout)
        return r.status_code, (r.text or "")

    def _get(self, path: str, params: dict) -> Tuple[int, str]:
        r = self.s.get(self.BASE + path, params=params, headers=self.AJAX_HEADERS, timeout=self.timeout)
        return r.status_code, (r.text or "")

    # ---------------------------
    # login + context
    # ---------------------------
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
        self.s.get(self.BASE + "/index", timeout=self.timeout)

    def warm_plant_context(self, plant_id: str) -> None:
        # mimic browser navigation
        self.s.get(self.BASE + "/panel", timeout=self.timeout)
        self.s.get(self.BASE + f"/panel/getPlantInfo?plantId={plant_id}", timeout=self.timeout)
        self.s.get(self.BASE + f"/device/photovoltaic?plantId={plant_id}", timeout=self.timeout)

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
        status, txt = self._post(
            "/device/getMAXList",
            {"plantId": str(plant_id), "currPage": "1", "pageSize": "50"},
        )
        js = try_parse_json(txt) or {}
        items = js.get("datas") or js.get("data") or js.get("rows") or []

        sns_set = {normalize_sn(x) for x in sns if x}
        out: Dict[str, Dict[str, Any]] = {}
        for it in items:
            sn = normalize_sn(it.get("deviceSn") or it.get("sn") or it.get("invSn") or "")
            if sn and sn in sns_set:
                out[sn] = it

        LOG.info("Found %d devices in plant %s", len(out), plant_id)
        return out

    # ---------------------------
    # KPI fetch
    # ---------------------------
    def fetch_health_kpi_for_sn(self, plant_id: str, sn: str, device: Dict[str, Any]) -> Dict[str, Any]:
        plant_id = str(plant_id)
        sn = normalize_sn(sn)

        # Harvest possible identifiers from device row
        device_type = device.get("deviceType") or device.get("deviceTypeNum") or device.get("type")
        datalog_sn = device.get("datalogSn") or device.get("collectorSn") or device.get("dataloggerSn")
        device_id = device.get("deviceId") or device.get("id") or device.get("invId")

        # Pull HTML pages to discover the real endpoints (super important)
        html_parts: List[str] = []
        try:
            r = self.s.get(self.BASE + "/device/getMAXPage", params={"ttt": str(now_ms())}, timeout=self.timeout)
            html_parts.append(r.text or "")
        except Exception:
            pass
        try:
            r = self.s.get(self.BASE + "/device/getInverterPage", params={"plantId": plant_id}, timeout=self.timeout)
            html_parts.append(r.text or "")
        except Exception:
            pass

        discovered: List[str] = []
        for h in html_parts:
            discovered.extend(discover_ajax_urls(h))
        discovered = [u for u in discovered if is_readish_endpoint(u)]

        # Known common endpoints (plus discovered ones)
        endpoints = []
        for u in discovered:
            if u not in endpoints:
                endpoints.append(u)

        for u in [
            "/device/getInverterRealTimeData",
            "/device/getInvRealTimeData",
            "/device/getInverterDetailData2",
            "/device/getInverterDetailData",
            "/panel/getDeviceData",
            "/panel/getInverterData",
            "/panel/getDeviceDetail",
        ]:
            if u not in endpoints:
                endpoints.append(u)

        # Payload variants — Growatt differs by endpoint/model
        payloads: List[dict] = [
            {"plantId": plant_id, "deviceSn": sn, "deviceType": device_type, "datalogSn": datalog_sn},
            {"plantId": plant_id, "sn": sn, "deviceType": device_type, "datalogSn": datalog_sn},
            {"plantId": plant_id, "invSn": sn, "deviceType": device_type, "datalogSn": datalog_sn},
            {"plantId": plant_id, "serialNum": sn, "deviceType": device_type, "datalogSn": datalog_sn},
        ]
        if device_id:
            payloads.append({"plantId": plant_id, "deviceId": device_id, "deviceType": device_type, "datalogSn": datalog_sn})
            payloads.append({"deviceId": device_id})

        # Remove Nones/empties
        def clean(d: dict) -> dict:
            return {k: v for k, v in d.items() if v not in (None, "", "null")}

        payloads = [clean(p) for p in payloads]

        # Try endpoints
        for ep in endpoints[:80]:
            for p in payloads:
                try:
                    sc, txt = self._post(ep, p)
                    # Always save raw response for diagnosis
                    self._save_text(plant_id, sn, f"{ep}__POST__{sc}", (txt or "")[:20000])

                    js = try_parse_json(txt)
                    if isinstance(js, dict) and (js.get("data") is not None or js.get("datas") is not None):
                        out = js.get("data") if js.get("data") is not None else js.get("datas")
                        if isinstance(out, dict):
                            out["_endpoint"] = ep
                        self._save_json(plant_id, sn, f"HIT__{ep}__POST", js)
                        return out if isinstance(out, dict) else {"_endpoint": ep, "value": out}

                    # Try GET as fallback
                    sc2, txt2 = self._get(ep, p)
                    self._save_text(plant_id, sn, f"{ep}__GET__{sc2}", (txt2 or "")[:20000])

                    js2 = try_parse_json(txt2)
                    if isinstance(js2, dict) and (js2.get("data") is not None or js2.get("datas") is not None):
                        out2 = js2.get("data") if js2.get("data") is not None else js2.get("datas")
                        if isinstance(out2, dict):
                            out2["_endpoint"] = ep
                        self._save_json(plant_id, sn, f"HIT__{ep}__GET", js2)
                        return out2 if isinstance(out2, dict) else {"_endpoint": ep, "value": out2}

                except Exception as e:
                    self._save_text(plant_id, sn, f"{ep}__EXCEPTION", repr(e))

        # Summary for this inverter
        summary = {
            "plantId": plant_id,
            "sn": sn,
            "device_keys": list(device.keys())[:120],
            "deviceType": device_type,
            "datalogSn": datalog_sn,
            "deviceId": device_id,
            "tried_endpoints_count": len(endpoints),
            "tried_payloads_count": len(payloads),
            "cookies": self.s.cookies.get_dict(),
        }
        self._save_json(plant_id, sn, "SUMMARY", summary)

        return {}
