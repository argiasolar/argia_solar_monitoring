# argia_huawei.py
from __future__ import annotations

import requests
from typing import Dict, Optional


HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"


def _to_float(x) -> float:
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return 0.0


def fetch_huawei_day_kwh(
    date_iso: str,
    plants_to_fetch: Dict[str, str],   # {stationCode: PlantKey}
    username: Optional[str],
    password: Optional[str],
    attempt: int = 0,
) -> Dict[str, float]:
    results = {pk: 0.0 for pk in plants_to_fetch.values()}

    if not username or not password:
        print("❌ [Huawei] Missing credentials (HUAWEI_USERNAME/HUAWEI_PASSWORD).")
        return results

    try:
        print("🚀 [Huawei] Connecting via /thirdData...")

        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})

        # delikatny backoff na retry
        if attempt:
            s.timeout = 25

        r_log = s.post(
            f"{HUAWEI_BASE_URL}/login",
            json={"userName": username, "systemCode": password},
            timeout=25,
        )
        r_log.raise_for_status()

        # XSRF bywa w cookies
        xsrf = s.cookies.get("XSRF-TOKEN") or r_log.headers.get("XSRF-TOKEN")
        if xsrf:
            s.headers.update({"XSRF-TOKEN": xsrf})

        # Huawei endpoint history
        for station_code, plant_key in plants_to_fetch.items():
            try:
                payload = {
                    "stationCodes": station_code,
                    "collectTime": date_iso,
                    "dataItemKeys": "day_cap",
                }
                r = s.post(
                    f"{HUAWEI_BASE_URL}/getHistoryStationData",
                    json=payload,
                    timeout=25,
                )
                r.raise_for_status()
                js = r.json()

                val = 0.0
                if isinstance(js, dict) and js.get("data"):
                    # data[0].dataItemMap.day_cap
                    val = _to_float(js["data"][0].get("dataItemMap", {}).get("day_cap", 0))

                results[plant_key] = round(val, 2)
                print(f"   📊 [Huawei] {plant_key} ({station_code}): {results[plant_key]} kWh")

            except Exception as e:
                print(f"   ⚠️ [Huawei] Failed {plant_key} ({station_code}): {e}")
                results[plant_key] = 0.0

        return results

    except Exception as e:
        print(f"❌ [Huawei] Error: {e}")
        return results
