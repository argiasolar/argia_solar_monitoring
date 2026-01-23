# argia_huawei.py
import os, requests, time
from typing import Dict

DEFAULT_BASE = (os.environ.get("HUAWEI_BASE_URL") or "https://la5.fusionsolar.huawei.com/thirdData").rstrip("/")

def fetch_huawei_day_kwh(date_iso: str, plants_to_fetch: Dict[str, str], plants_config: dict) -> Dict[str, float]:
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    user, password = os.environ.get("HUAWEI_USERNAME"), os.environ.get("HUAWEI_PASSWORD")
    if not user or not password: return results
    
    sess = requests.Session()
    try:
        r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=20)
        token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
        sess.headers.update({"XSRF-TOKEN": token, "Content-Type": "application/json"})
        
        # O 21:00 RealKpi ma finalne dane
        payload = {"stationCodes": ",".join(plants_to_fetch.keys())}
        rr = sess.post(f"{DEFAULT_BASE}/getStationRealKpi", json=payload, timeout=20)
        data = rr.json().get("data") or []
        for item in data:
            p_key = plants_to_fetch.get(item.get("stationCode"))
            if p_key:
                val = item.get("dataItemMap", {}).get("day_cap") or 0.0
                results[p_key] = round(float(val), 2)
    except Exception as e:
        print(f"❌ Huawei Error: {e}")
    return results
