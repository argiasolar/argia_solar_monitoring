# argia_huawei.py
import os, requests, time
from typing import Dict

DEFAULT_BASE = (os.environ.get("HUAWEI_BASE_URL") or "https://la5.fusionsolar.huawei.com/thirdData").rstrip("/")

def fetch_huawei_day_kwh(date_iso: str, plants_to_fetch: Dict[str, str], plants_config: dict) -> Dict[str, float]:
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    user, password = os.environ.get("HUAWEI_USERNAME"), os.environ.get("HUAWEI_PASSWORD")
    if not user or not password: return results
    
    sess = requests.Session()
    sess.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    
    try:
        # 1. Logowanie
        r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=25)
        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        if not token: return results
        sess.headers.update({"XSRF-TOKEN": token})
        
        # 2. Zapytanie o dane Real-Time (one zawierają dzisiejszą sumę)
        station_codes = ",".join(plants_to_fetch.keys())
        print(f"   🚀 Fetching RealKpi for: {station_codes}")
        
        rr = sess.post(f"{DEFAULT_BASE}/getStationRealKpi", json={"stationCodes": station_codes}, timeout=25)
        js = rr.json()
        
        if js.get("success"):
            data_list = js.get("data") or []
            for item in data_list:
                s_code = item.get("stationCode")
                p_key = plants_to_fetch.get(s_code)
                if p_key:
                    # W RealKpi 'dataItemMap' zawiera licznik dzienny
                    m = item.get("dataItemMap") or {}
                    # 'day_cap' to standard, ale LA5 czasem używa 'daily_cap' lub 'day_power'
                    val = m.get("day_cap") or m.get("daily_cap") or m.get("day_power") or 0.0
                    results[p_key] = round(float(val), 2)
                    print(f"   📊 [Huawei:Real] {p_key}: {results[p_key]} kWh")
        else:
            print(f"   ⚠️ [Huawei] RealKpi failed: {js.get('message')}")

    except Exception as e:
        print(f"❌ [Huawei] Error: {e}")
    
    return results
