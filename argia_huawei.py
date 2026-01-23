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
        
        # 2. Przygotowanie czasu (Huawei wymaga timestampu w ms dla 00:00:00 danego dnia)
        d = dt.date.fromisoformat(date_iso)
        collect_time = int(dt.datetime(d.year, d.month, d.day).timestamp() * 1000)

        # 3. Pobieranie raportu dziennego (bardziej niezawodny dla LA5)
        print(f"   🚀 Fetching Huawei Daily Report for {date_iso}...")
        station_codes = ",".join(plants_to_fetch.keys())
        
        payload = {
            "stationCodes": station_codes,
            "collectTime": collect_time
        }
        
        rr = sess.post(f"{DEFAULT_BASE}/getKpiStationDay", json=payload, timeout=25)
        js = rr.json()
        
        if js.get("success"):
            data_list = js.get("data") or []
            for item in data_list:
                s_code = item.get("stationCode")
                p_key = plants_to_fetch.get(s_code)
                if p_key:
                    m = item.get("dataItemMap") or {}
                    # W raporcie dziennym pole to 'inverter_yield' lub 'daily_energy'
                    val = m.get("inverter_yield") or m.get("daily_energy") or 0.0
                    results[p_key] = round(float(val), 2)
                    print(f"   📊 [Huawei:Daily] {p_key}: {results[p_key]} kWh")
        else:
            print(f"   ⚠️ [Huawei] Report failed: {js.get('message')}")

    except Exception as e:
        print(f"❌ [Huawei] Global Error: {e}")
    
    return results
