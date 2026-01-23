# argia_huawei.py
import os, requests, time, datetime as dt
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
        
        # 2. Czas
        d = dt.date.fromisoformat(date_iso)
        collect_time = int(dt.datetime(d.year, d.month, d.day).timestamp() * 1000)

        # 3. Pobieranie raportu
        station_codes = ",".join(plants_to_fetch.keys())
        payload = {"stationCodes": station_codes, "collectTime": collect_time}
        
        print(f"🔍 [Huawei Debug] Requesting daily report for codes: {station_codes}")
        rr = sess.post(f"{DEFAULT_BASE}/getKpiStationDay", json=payload, timeout=25)
        js = rr.json()
        
        if js.get("success"):
            data_list = js.get("data") or []
            print(f"🔍 [Huawei Debug] Received {len(data_list)} data points.")
            
            for i, item in enumerate(data_list):
                s_code = item.get("stationCode")
                p_key = plants_to_fetch.get(s_code)
                m = item.get("dataItemMap") or {}
                
                # WYŚWIETLAMY WSZYSTKO DLA PIERWSZEGO REKORDU KAŻDEJ STACJI
                if i < 2 or s_code: 
                    print(f"   --- Data Map for {p_key} ({s_code}) ---")
                    print(f"   {m}")
                
                # Próbujemy zsumować najbardziej prawdopodobne klucze
                # product_power często oznacza całkowitą energię wyprodukowaną w danym dniu
                val = m.get("product_power") or m.get("inverter_yield") or m.get("daily_energy") or 0.0
                if p_key:
                    results[p_key] += round(float(val), 2)
            
            for p_key, total in results.items():
                print(f"   📊 [Huawei:Result] {p_key} Total Sum: {total} kWh")
        else:
            print(f"   ⚠️ [Huawei] API Fail: {js.get('message')}")

    except Exception as e:
        print(f"❌ [Huawei] Error: {e}")
    
    return results
