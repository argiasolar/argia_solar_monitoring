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
    # Powrót do ustawienia 00:00:00 dnia raportowanego
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
        # Odczekaj chwilę, żeby uniknąć błędu 407 (Access Frequency)
        time.sleep(2)
        
        print(f"🚀 [Huawei] Logging in to Daily Report Mode...")
        r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=25)
        r.raise_for_status()

        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        if not token: return results
        sess.headers.update({"XSRF-TOKEN": token})

        collect_ms = _collect_time_ms(date_iso)
        
        # Iterujemy pojedynczo, bo przy grupowym zapytaniu serwer LA5 czasem gubi dane
        for s_code, p_key in plants_to_fetch.items():
            payload = {"stationCodes": s_code, "collectTime": collect_ms}
            rr = sess.post(f"{DEFAULT_BASE}/getKpiStationDay", json=payload, timeout=25)
            
            if rr.status_code == 407:
                print("⚠️ [Huawei] Rate limit hit. Cooling down...")
                time.sleep(5)
                continue

            js = rr.json()
            data_list = js.get("data") or []
            
            if data_list and len(data_list) > 0:
                m = data_list[0].get("dataItemMap") or {}
                # inverterYield to standard dla Daily KPI
                val = _safe_float(m.get("inverterYield") or m.get("PVYield") or m.get("day_cap"))
                results[p_key] = round(val, 2)
                print(f"   📊 [Huawei] {p_key}: {results[p_key]} kWh")
            else:
                print(f"   ⚠️ [Huawei] No data for {p_key} on {date_iso}")

    except Exception as e:
        print(f"   ❌ [Huawei] Error: {e}")

    return results
