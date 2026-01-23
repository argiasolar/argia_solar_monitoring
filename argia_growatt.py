# argia_growatt.py
from __future__ import annotations
import os
import requests
from typing import Dict

GROWATT_OPENAPI_BASE = (os.environ.get("GROWATT_OPENAPI_BASE") or "https://openapi.growatt.com/").rstrip("/") + "/"

def _safe_float(x) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return 0.0

def fetch_growatt_day_kwh(date_iso: str, plants_to_fetch: Dict[str, str], plants_config: Dict[str, dict]) -> Dict[str, float]:
    """
    Wczorajsze rozwiązanie: Pobieramy listę elektrowni. 
    Pole 'today_energy' w tym endpoincie jest najbardziej niezawodne.
    """
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    token = os.environ.get("GROWATT_API_TOKEN")

    if not token:
        print("❌ [Growatt] Missing GROWATT_API_TOKEN")
        return results

    url = f"{GROWATT_OPENAPI_BASE}v1/plant/list"
    headers = {"token": token, "Accept": "application/json"}

    try:
        print(f"🚀 [Growatt] Fetching plant list for today's energy...")
        r = requests.get(url, headers=headers, params={"page": 1, "perpage": 50}, timeout=25)
        r.raise_for_status()
        js = r.json()

        data_payload = js.get("data", {})
        plants_data = data_payload.get("plants", []) if isinstance(data_payload, dict) else []

        for p in plants_data:
            pid = str(p.get("plant_id"))
            if pid in plants_to_fetch:
                p_key = plants_to_fetch[pid]
                # 'today_energy' zawiera produkcję z ostatniego cyklu
                val = _safe_float(p.get("today_energy"))
                results[p_key] = val
                print(f"   📊 [Growatt] {p_key} ({pid}): {results[p_key]} kWh")

    except Exception as e:
        print(f"   ❌ [Growatt] Error: {e}")

    return results
