# argia_growatt.py
from __future__ import annotations

import os
import time
import requests
from typing import Dict, Optional

import growattServer


# OpenAPI (token) – domyślny base
GROWATT_OPENAPI_BASE = (os.environ.get("GROWATT_OPENAPI_BASE") or "https://openapi.growatt.com/").rstrip("/") + "/"
# Legacy (username/password) – często 403 w CI
GROWATT_LEGACY_BASE = (os.environ.get("GROWATT_LEGACY_BASE") or "https://server.growatt.com/").rstrip("/") + "/"


def _safe_float(x) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return 0.0


def _option_a_openapi_token_day_kwh(date_iso: str, plant_id: str, token: str) -> float:
    """
    Growatt OpenAPI: GET /v1/plant/energy?plant_id=...&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
    Response ma rekordy z polami: date, energy. :contentReference[oaicite:3]{index=3}
    """
    url = f"{GROWATT_OPENAPI_BASE}v1/plant/energy"
    params = {
        "plant_id": plant_id,
        "start_date": date_iso,
        "end_date": date_iso,
        "time_unit": "day",
        "page": 1,
        "perpage": 20,
    }
    headers = {
        # PDF mówi: “add the token: TOKEN in header” → w praktyce działa jako "token"
        "token": token,
        "Accept": "application/json",
        "User-Agent": "argia-bsa/1.0",
    }

    r = requests.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    js = r.json()

    # standard: error_code == 0
    if isinstance(js, dict) and js.get("error_code") not in (None, 0):
        return 0.0

    data = js.get("data") if isinstance(js, dict) else None
    if not data:
        return 0.0

    # data może być listą rekordów
    # rekord: {"date":"YYYY-MM-DD","energy":123.45}
    if isinstance(data, list):
        for rec in data:
            if str(rec.get("date")) == date_iso:
                return _safe_float(rec.get("energy"))
        # jeśli jest tylko 1 rekord i data nie matchuje, weź pierwszy
        if len(data) == 1:
            return _safe_float(data[0].get("energy"))

    return 0.0


def _option_b_legacy_userpass_day_kwh(date_iso: str, plant_id: str, user: str, password: str) -> float:
    """
    Legacy przez growattServer. U Ciebie w Actions bywa 403 na login.
    """
    api = growattServer.GrowattApi()
    api.server_url = GROWATT_LEGACY_BASE

    api.login(user, password)

    # Legacy najczęściej ma “todayEnergy” (ale to “today” wg serwera, nie “wczoraj”)
    # więc próbujemy detail dla konkretnego dnia:
    data = api.plant_detail(plant_id, date_iso)
    if isinstance(data, dict):
        return _safe_float(data.get("today_energy") or data.get("todayEnergy") or data.get("energy") or 0)
    return 0.0


def fetch_growatt_day_kwh(date_iso: str, plants_to_fetch: Dict[str, str], plants_config: Dict[str, dict]) -> Dict[str, float]:
    """
    plants_to_fetch: {SiteID: PlantKey} gdzie SiteID w Twoim arkuszu to plant_id dla Growatt.
    """
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}

    # token: w config masz SecretName_API=GROWATT_API_TOKEN
    # więc bierzemy z env o tej nazwie (per plant), a jak brak to z GROWATT_API_TOKEN global.
    print(f"🚀 [Growatt:A] OpenAPI (token) for {date_iso}...")

    any_token_success = False

    for plant_id, p_key in plants_to_fetch.items():
        conf = plants_config.get(p_key, {})
        token_env_name = (conf.get("secret_api") or "GROWATT_API_TOKEN").strip()
        token = os.environ.get(token_env_name) or os.environ.get("GROWATT_API_TOKEN")

        if not token:
            print(f"   ⚠️ [Growatt:A] Missing token env var ({token_env_name}) for {p_key}")
            continue

        try:
            kwh = _option_a_openapi_token_day_kwh(date_iso, plant_id, token)
            results[p_key] = float(kwh or 0.0)
            if results[p_key] > 0:
                any_token_success = True
            print(f"   📊 [Growatt:A] {p_key} ({plant_id}): {results[p_key]} kWh")
        except Exception as e:
            print(f"   ⚠️ [Growatt:A] Failed {p_key} ({plant_id}): {e}")
            results[p_key] = 0.0

    # Fallback B tylko jeśli token ścieżka nic nie dała (i masz user/pass)
    if not any_token_success:
        print("⚠️ [Growatt:A] Token path returned all zeros – trying legacy as fallback...")
        user = os.environ.get("GROWATT_USERNAME")
        password = os.environ.get("GROWATT_PASSWORD")
        if not user or not password:
            print("❌ [Growatt:B] Missing credentials (user/password).")
            return results

        print(f"🚀 [Growatt:B] Legacy login for {date_iso}...")
        for plant_id, p_key in plants_to_fetch.items():
            try:
                kwh = _option_b_legacy_userpass_day_kwh(date_iso, plant_id, user, password)
                results[p_key] = float(kwh or 0.0)
                print(f"   📊 [Growatt:B] {p_key} ({plant_id}): {results[p_key]} kWh")
            except Exception as e:
                print(f"   ❌ [Growatt:B] Legacy API Error for {p_key} ({plant_id}): {e}")
                results[p_key] = 0.0

    return results
