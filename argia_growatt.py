import os
import datetime
from zoneinfo import ZoneInfo

import growattServer


TZ = ZoneInfo("America/Mexico_City")


def dummy_kwh_for(plantkey: str) -> float:
    # Twoje sprawdzone stałe (możesz je później przenieść do arkusza jako "fallback map")
    dummy_map = {
        "SLP1": 609,
        "SLP2": 986,
        "GTO1": 2259,
        "MEX1": 2174,
        "NL1": 2463,
        "MEX2": 2448,
    }
    return float(dummy_map.get(plantkey, 500))


def _parse_energy_history(resp, date_iso: str) -> float:
    """
    Growatt v1 history często wygląda jak:
      {"data":{"energys":[{"date":"2026-01-22","energy":123.4}, ...]}}
    ale czasem bywa inna struktura – robimy parser odporny.
    """
    if not isinstance(resp, dict):
        return 0.0

    data = resp.get("data", resp)

    # 1) klasyczny format
    energys = None
    if isinstance(data, dict):
        energys = data.get("energys") or data.get("energy") or data.get("items")

    if isinstance(energys, list):
        for it in energys:
            try:
                if str(it.get("date")) == date_iso:
                    return float(it.get("energy", 0) or 0)
            except Exception:
                continue

    # 2) fallback: czasem jest prosto "today_energy"
    for k in ("today_energy", "todayEnergy", "energy", "yield"):
        if isinstance(data, dict) and k in data:
            try:
                return float(data[k] or 0)
            except Exception:
                pass

    return 0.0


def fetch_growatt_data(date_iso: str, plants_to_fetch: dict, plants_config: dict) -> dict:
    """
    plants_to_fetch: {PlantID(str): PlantKey} e.g. {'9275498': 'SLP1'}
    Uses secrets from Config_Plants:
      - SecretName_API (token env var name) preferred
      - legacy username/password only if enabled
    """
    # Resolve token env var name from any Growatt plant (usually shared)
    any_p = next(iter(plants_to_fetch.values()))
    secret_api_name = plants_config[any_p].get("secret_api") or "GROWATT_API_TOKEN"
    token = os.environ.get(secret_api_name)

    out = {p_key: 0.0 for p_key in plants_to_fetch.values()}

    # -------------------
    # OPTION A (PRIMARY): OpenAPI v1 token
    # -------------------
    if token:
        print(f"🚀 [Growatt:A] OpenAPI v1 (token) for {date_iso}...")

        try:
            api = growattServer.OpenApiV1(token=token)

            # For each plant, query 1-day history (start=end=yesterday)
            for plant_id, p_key in plants_to_fetch.items():
                try:
                    # Prefer *_v1 if present; fallback to non-suffixed (library version differences)
                    if hasattr(api, "plant_energy_history_v1"):
                        resp = api.plant_energy_history_v1(
                            plant_id, date_iso, date_iso, "day", 1, 50
                        )
                    else:
                        resp = api.plant_energy_history(
                            plant_id, date_iso, date_iso, "day", 1, 50
                        )

                    val = _parse_energy_history(resp, date_iso)
                    out[p_key] = round(float(val or 0.0), 2)
                    print(f"   📊 [Growatt:A] {p_key} ({plant_id}): {out[p_key]} kWh")

                except Exception as e:
                    print(f"   ⚠️ [Growatt:A] Failed {p_key} ({plant_id}): {e}")
                    out[p_key] = 0.0

            # If token path returns all zeros, we *can* try legacy, but only if enabled
            if all(v <= 0 for v in out.values()):
                print("⚠️ [Growatt:A] Token path returned all zeros – trying legacy as fallback...")
            else:
                return out

        except Exception as e:
            print(f"❌ [Growatt:A] Token API init/error: {e}")

    else:
        print(f"❌ [Growatt:A] Missing token env var: {secret_api_name}")

    # -------------------
    # OPTION B (FALLBACK): Legacy login user/pass (often blocked on GitHub Actions IP)
    # -------------------
    legacy_enabled = os.environ.get("GROWATT_LEGACY_ENABLE", "false").lower() == "true"
    if not legacy_enabled:
        return out

    print(f"🚀 [Growatt:B] Legacy login for {date_iso}...")

    user = os.environ.get("GROWATT_USERNAME")
    password = os.environ.get("GROWATT_PASSWORD")
    if not user or not password:
        print("❌ [Growatt:B] Missing credentials (user/password).")
        return out

    try:
        api2 = growattServer.GrowattApi(True)  # random UA (helps sometimes)
        login_response = api2.login(user, password)
        user_id = login_response["user"]["id"]

        # Legacy detail endpoint expects (plant_id, timespan, date) in some versions,
        # but in your earlier code you used plant_detail(plant_id, date) – that mismatch can cause zeros.
        # So we use plant_energy_data if available, else plant_detail with safe guessing.
        for plant_id, p_key in plants_to_fetch.items():
            val = 0.0
            try:
                if hasattr(api2, "plant_energy_data"):
                    resp = api2.plant_energy_data(plant_id)
                    # Usually includes todayEnergy only; not reliable for "yesterday"
                    # We'll fallback to detail if date needed.
                    val = float(resp.get("data", {}).get("todayEnergy", 0) or 0)

                # Try plant_detail with explicit timespan/day if supported
                if hasattr(api2, "plant_detail"):
                    try:
                        # many implementations: plant_detail(plant_id, timespan, date)
                        resp2 = api2.plant_detail(plant_id, 1, date_iso)  # 1=day
                        if isinstance(resp2, dict):
                            val2 = resp2.get("todayEnergy") or resp2.get("today_energy") or 0
                            if val2:
                                val = float(val2)
                    except TypeError:
                        # older signature plant_detail(plant_id, date)
                        resp2 = api2.plant_detail(plant_id, date_iso)
                        if isinstance(resp2, dict):
                            val2 = resp2.get("todayEnergy") or resp2.get("today_energy") or 0
                            if val2:
                                val = float(val2)

                out[p_key] = round(float(val or 0.0), 2)
                print(f"   📊 [Growatt:B] {p_key} ({plant_id}): {out[p_key]} kWh")

            except Exception as e:
                print(f"   ⚠️ [Growatt:B] Failed {p_key} ({plant_id}): {e}")
                out[p_key] = 0.0

        return out

    except Exception as e:
        print(f"❌ [Growatt:B] Legacy API Error: {e}")
        return out
