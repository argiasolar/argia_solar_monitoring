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
        if not token: 
            print("❌ [Huawei] Login failed - no token")
            return results
        sess.headers.update({"XSRF-TOKEN": token})
        
        # 2. DISCOVERY - Sprawdzamy co widzi API
        print("🔍 [Huawei Discovery] Fetching available stations list...")
        list_r = sess.post(f"{DEFAULT_BASE}/getStationList", json={}, timeout=25)
        stations_data = list_r.json().get("data", [])
        
        if not stations_data:
            print("   ⚠️ [Huawei Discovery] API returned ZERO stations. Check account permissions!")
        else:
            print(f"   ✅ [Huawei Discovery] Found {len(stations_data)} stations:")
            for s in stations_data:
                # To są kluczowe informacje, których potrzebujemy
                print(f"      👉 Name: {s.get('stationName')} | CODE: {s.get('stationCode')}")

        # 3. Próba pobrania danych dla Twoich obecnych kodów
        station_codes = ",".join(plants_to_fetch.keys())
        rr = sess.post(f"{DEFAULT_BASE}/getStationRealKpi", json={"stationCodes": station_codes}, timeout=25)
        
        js = rr.json()
        if js.get("success"):
            data_list = js.get("data") or []
            for item in data_list:
                s_code = item.get("stationCode")
                p_key = plants_to_fetch.get(s_code)
                if p_key:
                    val = (item.get("dataItemMap") or {}).get("day_cap", 0.0)
                    results[p_key] = round(float(val), 2)
                    print(f"   📊 [Huawei] {p_key}: {results[p_key]} kWh")

    except Exception as e:
        print(f"❌ [Huawei] Error: {e}")
    
    return results
