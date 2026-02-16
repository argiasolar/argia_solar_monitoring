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


def now_ms() -> int:
    return int(time.time() * 1000)


def norm_key(k: str) -> str:
    k = (k or "").strip().lower()
    k = re.sub(r"\(.*?\)", "", k)
    k = re.sub(r"[^a-z0-9]+", "", k)
    return k


@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    BASE = "https://server.growatt.com"

    def __init__(self, auth: GrowattAuth, timeout: int = 45, debug_dir: str = "out_health"):
        self.auth = auth
        self.timeout = timeout
        self.debug_dir = debug_dir
        os.makedirs(self.debug_dir, exist_ok=True)

        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (ARGIA Growatt Health Bot)", "Accept": "*/*"})

    def _safe_filename(self, name: str) -> str:
        name = re.sub(INVALID_FS_CHARS, "_", name)
        name = name.replace("/", "_").strip("_")
        return name

    def _save(self, plant_id: str, label: str, content: str, ext: str) -> None:
        fn = self._safe_filename(f"{plant_id}__{label}.{ext}")
        path = os.path.join(self.debug_dir, fn)
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            f.write(content or "")

    # ------------------------------------------------
    # LOGIN
    # ------------------------------------------------
    def login(self) -> None:
        r1 = self.s.get(self.BASE + "/login", timeout=self.timeout)
        LOG.info("GET /login -> %s", r1.status_code)

        r2 = self.s.post(
            self.BASE + "/login",
            data={"account": self.auth.user, "password": self.auth.password},
            timeout=self.timeout,
        )
        LOG.info("POST /login -> %s (len=%s)", r2.status_code, len(r2.text or ""))

        if "assToken" not in self.s.cookies.get_dict():
            raise RuntimeError("Growatt login failed: assToken missing")

        LOG.info("✅ Growatt login OK")

    def warm_plant_context(self, plant_id: str) -> None:
        self.s.get(self.BASE + "/device", timeout=self.timeout)
        pv = self.s.get(self.BASE + "/device/photovoltaic", params={"plantId": str(plant_id)}, timeout=self.timeout)
        # Optional: keep one html snapshot
        if pv.status_code == 200:
            self._save(str(plant_id), "pvpage", pv.text[:20000], "html")

        # cookies that Growatt UI uses
        try:
            self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")
            self.s.cookies.set("selPage", "%2Fpanel", domain="server.growatt.com", path="/")
        except Exception:
            pass

    # ------------------------------------------------
    # DEVICE LIST (Status + deviceType + datalogSn)
    # ------------------------------------------------
    def fetch_devices_best_for_sns(self, plant_id: str, sns: List[str]) -> Dict[str, Dict[str, Any]]:
        sns_set = {normalize_sn(x) for x in sns if x}

        r = self.s.post(
            self.BASE + "/device/getMAXList",
            data={"plantId": str(plant_id), "currPage": "1", "pageSize": "50"},
            timeout=self.timeout,
        )
        js = try_parse_json(r.text) or {}
        items = js.get("datas") or js.get("data") or js.get("rows") or []

        out: Dict[str, Dict[str, Any]] = {}
        for it in items:
            sn = normalize_sn(it.get("deviceSn") or it.get("sn") or it.get("invSn") or "")
            if sn and sn in sns_set:
                out[sn] = it

        LOG.info("Found %d devices in plant %s", len(out), plant_id)
        return out

    # ------------------------------------------------
    # KPI / String data (deviceType + datalogSn required)
    # ------------------------------------------------
    def fetch_health_kpi_for_sn(self, plant_id: str, sn: str, device: Dict[str, Any]) -> Dict[str, Any]:
        plant_id = str(plant_id)
        sn = normalize_sn(sn)

        device_type = device.get("deviceType") or device.get("deviceTypeNum") or device.get("type")
        datalog_sn = device.get("datalogSn") or device.get("dataloggerSn") or device.get("collectorSn")

        # These are the params Growatt UI commonly sends
        base_payload = {
            "plantId": plant_id,
            "deviceSn": sn,
            "deviceType": device_type,
        }
        if datalog_sn:
            base_payload["datalogSn"] = datalog_sn

        # Some endpoints expect different param names too
        payloads = [
            dict(base_payload),
            {"plantId": plant_id, "sn": sn, "deviceType": device_type, **({"datalogSn": datalog_sn} if datalog_sn else {})},
            {"plantId": plant_id, "invSn": sn, "deviceType": device_type, **({"datalogSn": datalog_sn} if datalog_sn else {})},
            {"plantId": plant_id, "serialNum": sn, "deviceType": device_type, **({"datalogSn": datalog_sn} if datalog_sn else {})},
        ]

        endpoints = [
            "/device/getInverterRealTimeData",
            "/device/getInvRealTimeData",
            "/device/getInverterDetailData2",
            "/device/getInverterDetailData",
            "/panel/getDeviceData",
            "/panel/getInverterData",
        ]

        # canonical keys we want to detect
        wanted_norm = set()
        for k in ["Vpv1", "Ipv1", "VacRS", "VacST", "VacTR", "PacR", "PacS", "PacT", "Pac"]:
            wanted_norm.add(norm_key(k))
        for i in range(1, 17):
            wanted_norm.add(norm_key(f"Vstr{i}"))
            wanted_norm.add(norm_key(f"Istr{i}"))

        def flatten_any(obj: Any, flat: Dict[str, Any]) -> None:
            if isinstance(obj, dict):
                # name/value pattern
                if ("name" in obj or "key" in obj) and any(k in obj for k in ("value", "val", "v", "data")):
                    kk = obj.get("name") or obj.get("key")
                    vv = obj.get("value", obj.get("val", obj.get("v", obj.get("data"))))
                    if isinstance(kk, str):
                        flat[norm_key(kk)] = vv
                for k, v in obj.items():
                    if isinstance(k, str):
                        flat[norm_key(k)] = v
                    if isinstance(v, (dict, list)):
                        flatten_any(v, flat)
            elif isinstance(obj, list):
                for it in obj:
                    flatten_any(it, flat)

        last_debug = {"endpoint": "", "payload": None, "text": ""}

        for ep in endpoints:
            for payload in payloads:
                try:
                    r = self.s.post(self.BASE + ep, data=payload, timeout=self.timeout)
                    txt = r.text or ""
                    last_debug = {"endpoint": ep, "payload": payload, "text": txt[:20000]}

                    js = try_parse_json(txt)
                    if not js:
                        continue

                    flat: Dict[str, Any] = {}
                    flatten_any(js, flat)

                    # do we have ANY of the expected keys?
                    if any(k in flat for k in wanted_norm):
                        # save raw json for inspection
                        self._save(plant_id, f"kpi_hit__{sn}__{self._safe_filename(ep)}", json.dumps(js, ensure_ascii=False)[:20000], "json")
                        flat["_endpoint"] = ep
                        return flat

                except Exception:
                    continue

        # miss → save *one* file with last response text + payload we used
        miss_obj = {
            "plantId": plant_id,
            "sn": sn,
            "deviceType": device_type,
            "datalogSn": datalog_sn,
            "lastEndpoint": last_debug["endpoint"],
            "lastPayload": last_debug["payload"],
            "lastResponseText": last_debug["text"],
            "triedEndpoints": endpoints,
        }
        self._save(plant_id, f"kpi_miss__{sn}", json.dumps(miss_obj, ensure_ascii=False, indent=2)[:20000], "json")
        return {}
