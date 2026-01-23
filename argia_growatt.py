# argia_growatt.py
from __future__ import annotations
import os
import requests
from typing import Dict

# OpenAPI (token)
GROWATT_OPENAPI_BASE = (os.environ.get("GROWATT_OPENAPI_BASE") or "https://openapi.growatt.com/").rstrip("/") + "/"

def _safe_float(x) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return 0.0

def fetch_growatt_day_kwh(date_iso: str, plants_to_fetch: Dict[str, str], plants_config: Dict[str, dict]) -> Dict[str, float]:
    """
    Pobiera dane historyczne dla konkretnego dnia korzystając z raportu miesięcznego.
    date_iso format: YYYY-MM-DD
    """
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    token = os.environ.get("GROWATT_API_TOKEN")

    if not token:
        print("❌ [Growatt] Missing GROWATT_API_TOKEN")
        return results

    # Rok i miesiąc potrzebne do endpointu miesięcznego
    year_month = date_iso[:7] # YYYY-MM

    for plant_id, p_key in plants_to_fetch.items():
        print(f"🚀 [Growatt] Fetching history for {p_key} ({plant_id}) via Monthly Report...")
        
        url = f"{GROWATT_OPENAPI_BASE}v1/plant/energy"
        params = {
            "plant_id": plant_id,
            "time_unit": "month",
            "date": year_month
        }
        headers = {"token": token, "Accept": "application/json"}

        try:
            r = requests.get(url, params=params, headers=headers, timeout=25)
            r.raise_for_status()
            js = r.json()

            # Szukamy listy dni w odpowiedzi
            data = js.get("data", {})
            # Czasami data jest listą, czasami słownikiem z kluczem 'energys' lub 'data'
            days_list = []
            if isinstance(data, list):
                days_list = data
            elif isinstance(data, dict):
                days_list = data.get("energys") or data.get("data") or []

            # Szukamy konkretnego dnia w liście
            found_val = 0.0
            for entry in days_list:
                # Growatt zwraca daty różnie, np. "2026-01-22" lub "2026-01-22 00:00:00"
                entry_date = str(entry.get("date", ""))
                if entry_date.startswith(date_iso):
                    found_val = _safe_float(entry.get("energy"))
                    break
            
            results[p_key] = found_val
            print(f"   📊 [Growatt] {p_key}: {results[p_key]} kWh")

        except Exception as e:
            print(f"   ⚠️ [Growatt] Error for {p_key}: {e}")

    return results
