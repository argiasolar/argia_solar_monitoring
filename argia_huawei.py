# argia_huawei.py
from __future__ import annotations
import os
import datetime as dt
from typing import Dict
import requests

DEFAULT_BASE = (os.environ.get("HUAWEI_BASE_URL") or "https://la5.fusionsolar.huawei.com/thirdData").rstrip("/")

def _safe_float(x) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return 0.0

def _collect_time_ms(date_iso: str) -> int:
    """
    Wczorajsze rozwiązanie: Huawei LA5 indeksuje dane 'dzienne' 
    pod znacznikiem godziny 23:00 dnia poprzedniego.
    """
    target_date = dt.date.fromisoformat(date_iso)
    yesterday = target_date - dt.timedelta(days=1)
    # Celujemy w 23:00 dnia poprzedniego
    dt_obj = dt.datetime(yesterday.year, yesterday.month, yesterday.day, 23, 0, 0)
    return int(dt_obj.timestamp() * 1000)

def fetch_huawei_day_kwh(date_iso: str, plants_to_fetch: Dict[str, str], plants_config: Dict[str, dict]) -> Dict[str, float]:
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    user = os.environ.get("HUAWEI_USERNAME")
    password = os.environ.get("HUAWEI_PASSWORD")

    if not user or not password:
        print("❌ [Huawei] Missing HUAWEI_USERNAME / HUAWEI_PASSWORD")
        return results

    sess = requests.Session()
    sess.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    try:
        print(f"🚀 [Huawei] Logging in to {DEFAULT_BASE}...")
        r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=25)
        r.raise_for_status()

        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        if not token:
            print("❌ [Huawei] Login failed: No XSRF-TOKEN")
            return results
        
        sess.headers.update({"XSRF-TOKEN": token})
        collect_ms = _collect_time_ms(date_iso)
        
        station_codes_str = ",".join(plants_to_fetch.keys())
        payload = {"stationCodes": station_codes_str, "collectTime": collect_ms}
        
        rr = sess.post(f"{DEFAULT_BASE}/getKpiStationDay", json=payload, timeout=25)
        rr.raise_for_status()
        js = rr.json()

        data_list = js.get("data") or []
        
        for item in data_list:
            s_code = item.get("stationCode")
            p_key = plants_to_fetch.get(s_code)
            if not p_key: continue

            m = item.get("dataItemMap") or {}
            val = 0.0
            # inverterYield to najpewniejszy klucz dla produkcji dziennej
            candidates = ["inverterYield", "PVYield", "day_cap"]
            for c in candidates:
                if c in m and m[c] is not None:
                    val = _safe_float(m[c])
                    if val > 0: break

            results[p_key] = round(val, 2)
            print(f"   📊 [Huawei] {p_key} ({s_code}): {results[p_key]} kWh")

    except Exception as e:
        print(f"   ❌ [Huawei] Error: {e}")

    return results
