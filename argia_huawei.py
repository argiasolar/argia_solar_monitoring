# argia_huawei.py
from __future__ import annotations
import os
import requests
from typing import Dict

DEFAULT_BASE = (os.environ.get("HUAWEI_BASE_URL") or "https://la5.fusionsolar.huawei.com/thirdData").rstrip("/")

def _safe_float(x) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return 0.0

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
        print(f"🚀 [Huawei] Logging in (Real-Time Mode)...")
        r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=25)
        r.raise_for_status()

        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        if not token: return results
        
        sess.headers.update({"XSRF-TOKEN": token})
        
        # Kluczowa zmiana: getStationRealKpi zamiast raportu dziennego
        station_codes_str = ",".join(plants_to_fetch.keys())
        payload = {"stationCodes": station_codes_str}
        
        rr = sess.post(f"{DEFAULT_BASE}/getStationRealKpi", json=payload, timeout=25)
        rr.raise_for_status()
        js = rr.json()

        data_list = js.get("data") or []
        for item in data_list:
            s_code = item.get("stationCode")
            p_key = plants_to_fetch.get(s_code)
            if not p_key: continue

            m = item.get("dataItemMap") or {}
            # 'day_cap' to licznik energii wyprodukowanej dzisiaj do tej pory
            val = _safe_float(m.get("day_cap"))

            results[p_key] = round(val, 2)
            print(f"   📊 [Huawei:RealTime] {p_key} ({s_code}): {results[p_key]} kWh")

    except Exception as e:
        print(f"   ❌ [Huawei] Error: {e}")

    return results
