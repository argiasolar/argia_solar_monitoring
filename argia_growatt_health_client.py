#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, math, logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import requests

LOG = logging.getLogger("argia.growatt.health")

# ---------------------------------------------------------
# helpers
# ---------------------------------------------------------

def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()

def normalize_sn(x: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(x)).upper()

def safe_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None: return default
        if isinstance(x, str):
            x = x.replace(",", "").strip()
            if not x: return default
        v=float(x)
        if math.isnan(v) or math.isinf(v): return default
        return v
    except:
        return default

def try_parse_json(text:str)->Optional[dict]:
    try: return json.loads(text)
    except: return None

@dataclass
class GrowattAuth:
    user:str
    password:str

# ---------------------------------------------------------
# client
# ---------------------------------------------------------

class GrowattMonitoringClient:
    BASE="https://server.growatt.com"

    AJAX_HEADERS={
        "Accept":"application/json, text/javascript, */*; q=0.01",
        "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":"https://server.growatt.com",
        "Referer":"https://server.growatt.com/panel",
        "X-Requested-With":"XMLHttpRequest",
        "User-Agent":"Mozilla/5.0"
    }

    def __init__(self,auth:GrowattAuth,timeout:int=45):
        self.auth=auth
        self.timeout=timeout
        self.s=requests.Session()

    # ------------------------------------------------
    # login
    # ------------------------------------------------
    def login(self)->None:
        self.s.get(self.BASE+"/login",timeout=self.timeout)

        self.s.post(self.BASE+"/login",
            data={"account":self.auth.user,"password":self.auth.password},
            timeout=self.timeout)

        if "assToken" not in self.s.cookies.get_dict():
            raise RuntimeError("Growatt login failed")

        LOG.info("✅ Growatt login OK")

        # IMPORTANT: open index UI
        self.s.get(self.BASE+"/index",timeout=self.timeout)

    # ------------------------------------------------
    # activate plant session
    # ------------------------------------------------
    def warm_plant_context(self,plant_id:str)->None:
        self.s.get(self.BASE+"/panel",timeout=self.timeout)
        self.s.get(self.BASE+f"/panel/getPlantInfo?plantId={plant_id}",timeout=self.timeout)
        self.s.get(self.BASE+f"/device/photovoltaic?plantId={plant_id}",timeout=self.timeout)

        self.s.cookies.set("selectedPlantId",str(plant_id),domain="server.growatt.com",path="/")
        self.s.cookies.set("selPage","%2Fpanel",domain="server.growatt.com",path="/")

        LOG.info("Plant session activated %s",plant_id)

    # ------------------------------------------------
    # device list
    # ------------------------------------------------
    def fetch_devices_best_for_sns(self,plant_id:str,sns:List[str])->Dict[str,Dict[str,Any]]:
        r=self.s.post(self.BASE+"/device/getMAXList",
            data={"plantId":plant_id,"currPage":"1","pageSize":"50"},
            headers=self.AJAX_HEADERS,
            timeout=self.timeout)

        js=try_parse_json(r.text) or {}
        items=js.get("datas") or js.get("data") or js.get("rows") or []

        out={}
        for it in items:
            sn=normalize_sn(it.get("deviceSn") or it.get("sn") or "")
            if sn in sns:
                out[sn]=it

        LOG.info("Found %d devices in plant %s",len(out),plant_id)
        return out

    # ------------------------------------------------
    # realtime data (FINAL FIX)
    # ------------------------------------------------
    def fetch_health_kpi_for_sn(self,plant_id:str,sn:str,device:Dict[str,Any])->Dict[str,Any]:

        payload={
            "plantId":plant_id,
            "deviceSn":sn,
            "deviceType":device.get("deviceType"),
            "datalogSn":device.get("datalogSn")
        }

        endpoints=[
            "/device/getInverterRealTimeData",
            "/device/getInvRealTimeData",
            "/device/getInverterDetailData2"
        ]

        for ep in endpoints:
            r=self.s.post(self.BASE+ep,
                data=payload,
                headers=self.AJAX_HEADERS,
                timeout=self.timeout)

            js=try_parse_json(r.text)
            if js and ("data" in js or "datas" in js):
                LOG.info("KPI endpoint %s OK for %s",ep,sn)
                return js.get("data") or js.get("datas")

        LOG.warning("No KPI for %s",sn)
        return {}
