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
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    token = os.environ.get("GROWATT_API_TOKEN")

    if not token:
        print("❌ [Growatt] Missing GROWATT_API_TOKEN")
        return results

    headers = {"token": token, "Accept": "application/json"}

    for plant_id, p_key in plants_to_fetch.items():
        try:
            # Kluczowa zmiana: plant/data zamiast plant/list lub plant/energy
            url = f"{GROWATT_OPENAPI_BASE}v1/plant/data"
            r = requests.get(url, headers=headers, params={"plant_id": plant_id}, timeout=25)
            r.raise_for_status()
            js = r.json()

            d = js.get("data", {})
            # Próbujemy wyciągnąć energię z wczoraj (last_day_energy) 
            # lub dzisiejszą, jeśli skrypt idzie tuż po północy
            val = _safe_float(d.get("last_day_energy") or d.get("today_energy"))
            
            results[p_key] = val
            print(f"   📊 [Growatt:Data] {p_key} ({plant_id}): {results[p_key]} kWh")

        except Exception as e:
            print(f"   ⚠️ [Growatt] Error for {p_key}: {e}")

    return results
