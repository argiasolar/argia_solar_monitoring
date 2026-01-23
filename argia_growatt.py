from __future__ import annotations

import time
import random
from typing import Dict, Optional

import growattServer


def _is_403(e: Exception) -> bool:
    s = str(e).lower()
    return "403" in s or "forbidden" in s

def _sleep(backoff: float) -> None:
    time.sleep(backoff + random.uniform(0, 1.5))

def _apply_headers(api: growattServer.GrowattApi, server_url: str) -> None:
    # "normalniejsze" nagłówki niż domyślne python-requests
    api.session.headers.update({
        "User-Agent": "okhttp/4.9.3",
        "Accept": "application/json, text/plain, */*",
        "Referer": server_url,
        "Connection": "keep-alive",
    })

def fetch_growatt_data(
    date_iso: str,
    plants_to_fetch: Dict[str, str],
    user: Optional[str] = None,
    password: Optional[str] = None,
    server_url: str = "https://server.growatt.com/",
) -> Dict[str, float]:
    """
    plants_to_fetch: {SiteID: PlantKey}
    Returns: {PlantKey: kWh}
    """
    print(f"🚀 [Growatt] Connecting for {date_iso}...")

    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    if not plants_to_fetch:
        return results

    if not user or not password:
        print("❌ [Growatt] Missing credentials (user/password).")
        return results

    api = growattServer.GrowattApi()
    api.server_url = server_url.rstrip("/") + "/"
    _apply_headers(api, api.server_url)

    # Login with backoff, stop on 403
    for attempt in range(3):
        try:
            api.login(user, password)
            break
        except Exception as e:
            if _is_403(e):
                print("❌ [Growatt] 403 Forbidden on login -> prawdopodobna blokada / bot-protection. Stop.")
                return results
            backoff = 8 * (2 ** attempt)
            print(f"⚠️ [Growatt] Login failed ({attempt+1}/3): {e} -> sleep ~{backoff}s")
            _sleep(backoff)
    else:
        print("❌ [Growatt] Login failed after retries.")
        return results

    # Minimal calls: plant_list once; plant_detail only if needed
    try:
        login_id = api.session.auth[0] if getattr(api.session, "auth", None) else user
        all_plants = api.plant_list(login_id)
        plant_list_data = all_plants.get("data", []) if isinstance(all_plants, dict) else []
        today_map = {str(p.get("plantId")): p.get("todayEnergy", 0) for p in plant_list_data if p.get("plantId") is not None}

        for s_id, p_key in plants_to_fetch.items():
            val = today_map.get(str(s_id), 0)

            # Jeśli lista zwraca 0, próbuj detail (czasem działa na yesterday)
            if val in (None, 0, "0", "0.0"):
                try:
                    d = api.plant_detail(s_id, date_iso)
                    if isinstance(d, dict):
                        val = d.get("today_energy") or d.get("todayEnergy") or d.get("energy") or 0
                except Exception as e:
                    if _is_403(e):
                        print("❌ [Growatt] 403 Forbidden on data -> blokada. Stop dalszych prób dla Growatt.")
                        # nie przerywamy całej pętli, ale nie retryujemy agresywnie
                    else:
                        print(f"   ⚠️ [Growatt] plant_detail failed for {p_key} ({s_id}): {e}")
                    val = 0

            try:
                results[p_key] = float(val or 0)
            except Exception:
                results[p_key] = 0.0

            print(f"   📊 [Growatt] {p_key} ({s_id}): {results[p_key]} kWh")

        return results

    except Exception as e:
        if _is_403(e):
            print("❌ [Growatt] 403 Forbidden during fetch -> prawdopodobna blokada.")
        else:
            print(f"❌ [Growatt] General API Error: {e}")
        return results
