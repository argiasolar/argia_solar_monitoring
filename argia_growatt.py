# argia_growatt.py
from __future__ import annotations

import os
import time
from typing import Dict, Any, Optional

import growattServer


def _to_float(x) -> float:
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return 0.0


def _extract_kwh_from_v1_history(resp: Any, date_iso: str) -> float:
    """
    Próbujemy wyciągnąć kWh z różnych możliwych struktur odpowiedzi.
    growattServer bywa niespójny zależnie od endpointu/regionu.
    """
    if not isinstance(resp, dict):
        return 0.0

    data = resp.get("data") or resp.get("obj") or resp.get("result") or resp
    # różne nazwy listy rekordów
    records = None
    for k in ("datas", "data", "list", "rows", "records"):
        if isinstance(data, dict) and isinstance(data.get(k), list):
            records = data.get(k)
            break
    if records is None and isinstance(data, list):
        records = data

    if isinstance(records, list):
        for r in records:
            if not isinstance(r, dict):
                continue
            d = str(r.get("date") or r.get("calendar") or r.get("time") or "").strip()
            if date_iso in d:
                for key in ("energy", "e", "value", "kwh", "generation", "pvEnergy", "todayEnergy"):
                    if key in r:
                        return _to_float(r.get(key))
        # fallback: pierwszy rekord
        r0 = records[0] if records else {}
        if isinstance(r0, dict):
            for key in ("energy", "e", "value", "kwh", "generation", "pvEnergy", "todayEnergy"):
                if key in r0:
                    return _to_float(r0.get(key))

    # fallback: bez listy, bezpośrednie klucze
    for key in ("todayEnergy", "today_energy", "energy", "kwh"):
        if key in data:
            return _to_float(data.get(key))

    return 0.0


def _extract_kwh_from_legacy_detail(resp: Any) -> float:
    if not isinstance(resp, dict):
        return 0.0
    # typowo:
    # - todayEnergy (z listy)
    # - today_energy / todayEnergy (z detail)
    for key in ("today_energy", "todayEnergy", "energy", "eDay", "eday"):
        if key in resp:
            return _to_float(resp.get(key))
    # czasem jest zagnieżdżone
    data = resp.get("data")
    if isinstance(data, dict):
        for key in ("today_energy", "todayEnergy", "energy", "eDay", "eday"):
            if key in data:
                return _to_float(data.get(key))
    return 0.0


def fetch_growatt_day_kwh(
    date_iso: str,
    plants_to_fetch: Dict[str, str],   # {SiteID: PlantKey}
    token: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    attempt: int = 0,
) -> Dict[str, float]:
    """
    Opcja A (preferowana): token (OpenAPI v1)
    Opcja B: legacy login (username/password) do server.growatt.com
    """
    results = {pk: 0.0 for pk in plants_to_fetch.values()}

    # --- OPCJA A: OpenAPI v1 token
    if token:
        try:
            print(f"🚀 [Growatt:A] OpenAPI v1 (token) for {date_iso}...")

            # zależnie od wersji biblioteki klasa może mieć różną nazwę
            api = None
            for cls_name in ("OpenApiV1", "GrowattApiV1"):
                if hasattr(growattServer, cls_name):
                    api = getattr(growattServer, cls_name)(token=token)
                    break
            if api is None:
                raise RuntimeError("growattServer missing OpenAPI v1 class (OpenApiV1/GrowattApiV1)")

            # opcjonalnie region URL z env
            openapi_url = os.environ.get("GROWATT_OPENAPI_URL")
            if openapi_url:
                api.server_url = openapi_url.rstrip("/") + "/"

            # Dla każdego plant_id bierzemy historię (najpewniejsze dla konkretnej daty)
            for site_id, plant_key in plants_to_fetch.items():
                kwh = 0.0
                try:
                    # time_unit zwykle "day"
                    resp = api.plant_energy_history_v1(
                        plant_id=site_id,
                        start_date=date_iso,
                        end_date=date_iso,
                        time_unit="day",
                        page=1,
                        perpage=20,
                    )
                    kwh = _extract_kwh_from_v1_history(resp, date_iso)

                    # fallback: overview
                    if kwh <= 0:
                        ov = api.plant_energy_overview_v1(site_id)
                        kwh = _extract_kwh_from_v1_history(ov, date_iso)

                    results[plant_key] = float(kwh)
                    print(f"   📊 [Growatt:A] {plant_key} ({site_id}): {results[plant_key]} kWh")
                except Exception as e:
                    print(f"   ⚠️ [Growatt:A] Failed {plant_key} ({site_id}): {e}")
                    results[plant_key] = 0.0

            # jeżeli token działa i dał cokolwiek >0, wracamy od razu
            if any(v > 0 for v in results.values()):
                return results

            print("⚠️ [Growatt:A] Token path returned all zeros – trying legacy as fallback...")

        except Exception as e:
            print(f"❌ [Growatt:A] Token/OpenAPI v1 error: {e}")
            print("➡️ Falling back to legacy (username/password) if provided...")

    # --- OPCJA B: legacy login
    if not username or not password:
        print("❌ [Growatt:B] Missing credentials (user/password).")
        return results

    try:
        print(f"🚀 [Growatt:B] Legacy login for {date_iso}...")
        api = growattServer.GrowattApi(True)  # random UA (biblioteka sama sugeruje)
        api.server_url = "https://server.growatt.com/"

        # delikatny backoff na kolejne retry
        if attempt:
            time.sleep(min(5 * attempt, 15))

        login_resp = api.login(username, password)
        user_id = None
        if isinstance(login_resp, dict):
            user = login_resp.get("user") or {}
            user_id = user.get("id")
        if not user_id:
            user_id = username  # fallback

        # 1) plant_list jako „primary”
        plist = api.plant_list(user_id)
        today_map: Dict[str, float] = {}
        if isinstance(plist, dict) and isinstance(plist.get("data"), list):
            for p in plist["data"]:
                pid = p.get("plantId")
                te = p.get("todayEnergy", 0)
                if pid is not None:
                    today_map[str(pid)] = _to_float(te)

        for site_id, plant_key in plants_to_fetch.items():
            kwh = today_map.get(str(site_id), 0.0)

            # 2) fallback detail dla konkretnej daty
            if kwh <= 0:
                try:
                    d = api.plant_detail(site_id, date_iso)
                    kwh = _extract_kwh_from_legacy_detail(d)
                except Exception as e:
                    print(f"   ⚠️ [Growatt:B] plant_detail failed {plant_key} ({site_id}): {e}")

            results[plant_key] = float(kwh)
            print(f"   📊 [Growatt:B] {plant_key} ({site_id}): {results[plant_key]} kWh")

        return results

    except Exception as e:
        print(f"❌ [Growatt:B] Legacy API Error: {e}")
        return results
