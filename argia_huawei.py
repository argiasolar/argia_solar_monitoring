# argia_huawei.py
from __future__ import annotations

import os
import datetime as dt
from typing import Dict

import requests


DEFAULT_BASE = (os.environ.get("HUAWEI_BASE_URL") or "https://la5.fusionsolar.huawei.com/thirdData").rstrip("/")


def _safe_float(x) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return 0.0


def _collect_time_ms(date_iso: str) -> int:
    # SmartPVMS w praktyce akceptuje “day” po local-midnight w ms.
    d = dt.date.fromisoformat(date_iso)
    midnight = dt.datetime(d.year, d.month, d.day, 0, 0, 0)
    # Mexico City ~ UTC-6 (dla prostoty); jeśli chcesz perfekcyjnie: zoneinfo.
    midnight_utc = midnight + dt.timedelta(hours=6)
    return int(midnight_utc.timestamp() * 1000)


def fetch_huawei_day_kwh(date_iso: str, plants_to_fetch: Dict[str, str], plants_config: Dict[str, dict]) -> Dict[str, float]:
    """
    plants_to_fetch: {stationCode: PlantKey} np. {"SAG": "MEX1"}
    """
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}

    # w config masz SecretUser_Name i SecretPass_Name, ale zwykle to te same globalne sekrety
    # więc bierzemy z env.
    user = os.environ.get("HUAWEI_USERNAME")
    password = os.environ.get("HUAWEI_PASSWORD")

    if not user or not password:
        print("❌ [Huawei] Missing HUAWEI_USERNAME / HUAWEI_PASSWORD")
        return results

    print("🚀 [Huawei] Connecting via /thirdData (getKpiStationDay)...")

    sess = requests.Session()
    sess.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    # Login → XSRF-TOKEN w headerze odpowiedzi (wymagane w kolejnych requestach). :contentReference[oaicite:5]{index=5}
    r = sess.post(f"{DEFAULT_BASE}/login", json={"userName": user, "systemCode": password}, timeout=25)
    r.raise_for_status()

    token = r.headers.get("XSRF-TOKEN") or r.cookies.get("XSRF-TOKEN")
    if not token:
        print("❌ [Huawei] No XSRF-TOKEN received; login may have failed.")
        return results

    sess.headers.update({"XSRF-TOKEN": token})

    collect_ms = _collect_time_ms(date_iso)

    for station_code, p_key in plants_to_fetch.items():
        try:
            payload = {"stationCodes": station_code, "collectTime": collect_ms}
            rr = sess.post(f"{DEFAULT_BASE}/getKpiStationDay", json=payload, timeout=25)
            rr.raise_for_status()
            js = rr.json()

            val = 0.0
            # typowy shape: {"data":[{"dataItemMap":{...}}], "success":true, ...}
            data = js.get("data") if isinstance(js, dict) else None
            if isinstance(data, list) and data:
                m = (data[0] or {}).get("dataItemMap") or {}

                # różne instalacje zwracają różne klucze – bierzemy pierwszy sensowny
                candidates = [
                    "inverterYield",  # często
                    "PVYield",        # często
                    "day_cap",        # starsze/legacy
                    "today_energy",
                    "todayEnergy",
                ]
                for k in candidates:
                    if k in m:
                        val = _safe_float(m.get(k))
                        break

                # jeśli dalej 0, a map ma inne pola → diagnostyka w logu (krótko)
                if val <= 0 and isinstance(m, dict) and m:
                    keys_preview = ", ".join(list(m.keys())[:12])
                    print(f"   🧩 [Huawei] {p_key} keys preview: {keys_preview}")

            results[p_key] = round(float(val or 0.0), 2)
            print(f"   📊 [Huawei] {p_key} ({station_code}): {results[p_key]} kWh")

        except Exception as e:
            print(f"   ⚠️ [Huawei] Failed {p_key} ({station_code}): {e}")
            results[p_key] = 0.0

    return results
