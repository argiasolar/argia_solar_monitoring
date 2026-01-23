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
        time.sleep(2)
        r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=25)
        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        if not token: return results
        sess.headers.update({"XSRF-TOKEN": token})
        
        for s_code, p_key in plants_to_fetch.items():
            try:
                time.sleep(3) # Przerwa dla Huawei
                print(f"   🚀 Fetching Huawei: {p_key}...")
                payload = {"stationCodes": s_code}
                rr = sess.post(f"{DEFAULT_BASE}/getStationRealKpi", json=payload, timeout=25)
                
                if rr.status_code == 200:
                    js = rr.json()
                    if isinstance(js, dict) and js.get("data"):
                        m = js["data"][0].get("dataItemMap") or {}
                        results[p_key] = round(float(m.get("day_cap") or 0.0), 2)
                        print(f"   📊 [Huawei] {p_key}: {results[p_key]} kWh")
            except Exception as e:
                print(f"   ⚠️ [Huawei] {p_key} skipped: {e}")
    except Exception as e:
        print(f"❌ [Huawei] Global Error: {e}")
    return results
