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
        r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=25)
        r.raise_for_status()
        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        if not token: return results
        sess.headers.update({"XSRF-TOKEN": token})
        
        for s_code, p_key in plants_to_fetch.items():
            try:
                # Dodajemy mały odstęp, żeby Huawei nas nie blokował
                time.sleep(2) 
                payload = {"stationCodes": s_code}
                rr = sess.post(f"{DEFAULT_BASE}/getStationRealKpi", json=payload, timeout=25)
                
                # Sprawdzamy, czy odpowiedź to faktycznie JSON
                if rr.status_code == 200:
                    js = rr.json()
                    if isinstance(js, dict) and js.get("success"):
                        data = js.get("data") or []
                        if data:
                            m = data[0].get("dataItemMap") or {}
                            val = m.get("day_cap") or 0.0
                            results[p_key] = round(float(val), 2)
                            print(f"   📊 [Huawei] {p_key}: {results[p_key]} kWh")
                    else:
                        print(f"   ⚠️ [Huawei] API returned error for {p_key}: {js}")
                else:
                    print(f"   ⚠️ [Huawei] HTTP {rr.status_code} for {p_key}")
            
            except Exception as e:
                print(f"   ⚠️ [Huawei] Error processing station {p_key}: {e}")
                
    except Exception as e:
        print(f"❌ [Huawei] Login/Session Critical Error: {e}")
    
    return results
