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
        r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=20)
        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        if not token:
            print("❌ [Huawei] Failed to get XSRF-TOKEN")
            return results
        sess.headers.update({"XSRF-TOKEN": token})
        
        # 2. Pobieranie danych Real-Time (najpewniejsze o 21:00)
        # Próbujemy pobrać każdą stację osobno, aby uniknąć błędów grupowych
        for s_code, p_key in plants_to_fetch.items():
            payload = {"stationCodes": s_code}
            rr = sess.post(f"{DEFAULT_BASE}/getStationRealKpi", json=payload, timeout=20)
            
            if rr.status_code == 200:
                js = rr.json()
                data = js.get("data") or []
                if data:
                    m = data[0].get("dataItemMap") or {}
                    # Sprawdzamy wszystkie możliwe nazwy pól dla 'produkcji dzisiejszej'
                    val = m.get("day_cap") or m.get("daily_cap") or m.get("day_power") or 0.0
                    results[p_key] = round(float(val), 2)
                    print(f"   📊 [Huawei] {p_key}: {results[p_key]} kWh")
                else:
                    print(f"   ⚠️ [Huawei] No data in response for {p_key} ({s_code})")
            else:
                print(f"   ❌ [Huawei] API Error {rr.status_code} for {p_key}")
            
            time.sleep(1) # Grzecznościowy odstęp

    except Exception as e:
        print(f"❌ Huawei Critical Error: {e}")
    
    return results
