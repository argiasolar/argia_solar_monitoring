# argia_huawei.py
from __future__ import annotations
import os
import datetime as dt
import requests
import time
from typing import Dict

DEFAULT_BASE = (os.environ.get("HUAWEI_BASE_URL") or "https://la5.fusionsolar.huawei.com/thirdData").rstrip("/")

def _safe_float(x) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return 0.0

def _collect_time_ms(date_iso: str) -> int:
    d = dt.date.fromisoformat(date_iso)
    dt_obj = dt.datetime(d.year, d.month, d.day, 0, 0, 0)
    return int(dt_obj.timestamp() * 1000)

def fetch_huawei_day_kwh(date_iso: str, plants_to_fetch: Dict[str, str], plants_config: Dict[str, dict]) -> Dict[str, float]:
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    user = os.environ.get("HUAWEI_USERNAME")
    password = os.environ.get("HUAWEI_PASSWORD")

    if not user or not password: return results
    sess = requests.Session()
    sess.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    try:
        time.sleep(1)
        r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=25)
        r.raise_for_status()
        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        if not token: return results
        sess.headers.update({"XSRF-TOKEN": token})

        collect_ms = _collect_time_ms(date_iso)
        
        for s_code, p_key in plants_to_fetch.items():
            # Próba 1: Raport Dzienny
            payload = {"stationCodes": s_code, "collectTime": collect_ms}
            rr = sess.post(f"{DEFAULT_BASE}/getKpiStationDay", json=payload, timeout=25)
            js = rr.json()
            data_list = js.get("data") or []
            
            val = 0.0
            if data_list:
                m = data_list[0].get("dataItemMap") or {}
                val = _safe_float(m.get("inverterYield") or m.get("PVYield") or m.get("day_cap"))
            
            # Próba 2: Fallback do Real-Time (jeśli nadal 0)
            if val <= 0:
                payload_rt = {"stationCodes": s_code}
                rrr = sess.post(f"{DEFAULT_BASE}/getStationRealKpi", json=payload_rt, timeout=25)
                js_rt = rrr.json()
                rt_list = js_rt.get("data") or []
                if rt_list:
                    m_rt = rt_list[0].get("dataItemMap") or {}
                    val = _safe_float(m_rt.get("day_cap"))

            results[p_key] = round(val, 2)
            print(f"   📊 [Huawei] {p_key}: {results[p_key]} kWh")
            time.sleep(1)

    except Exception as e:
        print(f"   ❌ [Huawei] Error: {e}")

    return results
