# argia_growatt.py
import os, requests, time
from typing import Dict

GROWATT_OPENAPI_BASE = (os.environ.get("GROWATT_OPENAPI_BASE") or "https://openapi.growatt.com/").rstrip("/") + "/"

def fetch_growatt_day_kwh(date_iso: str, plants_to_fetch: Dict[str, str], plants_config: dict) -> Dict[str, float]:
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    token = os.environ.get("GROWATT_API_TOKEN")
    if not token: return results
    
    headers = {"token": token, "Accept": "application/json"}
    for plant_id, p_key in plants_to_fetch.items():
        try:
            time.sleep(2) # Przerwa dla Growatt
            print(f"   🚀 Fetching Growatt: {p_key}...")
            r = requests.get(f"{GROWATT_OPENAPI_BASE}v1/plant/data", headers=headers, params={"plant_id": plant_id}, timeout=25)
            if r.status_code == 200:
                js = r.json()
                if isinstance(js, dict) and js.get("data"):
                    val = js["data"].get("today_energy") or 0.0
                    results[p_key] = float(val)
                    print(f"   📊 [Growatt] {p_key}: {results[p_key]} kWh")
        except Exception as e:
            print(f"   ⚠️ [Growatt] {p_key} error: {e}")
    return results
