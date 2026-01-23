import os
import requests
from typing import Dict, Optional

HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"

def fetch_huawei_data(
    date_iso: str,
    plants_to_fetch: Dict[str, str],
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, float]:
    """
    plants_to_fetch: {StationCode/SiteID: PlantKey}
    Returns: {PlantKey: kWh}
    """
    print("🚀 [Huawei] Connecting via /thirdData...")
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}
    if not plants_to_fetch:
        return results

    user = user or os.environ.get("HUAWEI_USERNAME")
    password = password or os.environ.get("HUAWEI_PASSWORD")
    if not user or not password:
        print("❌ [Huawei] Missing credentials (HUAWEI_USERNAME/HUAWEI_PASSWORD).")
        return results

    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

    try:
        r_log = s.post(
            f"{HUAWEI_BASE_URL}/login",
            json={"userName": user, "systemCode": password},
            timeout=25
        )
        token = r_log.headers.get("XSRF-TOKEN") or r_log.headers.get("xsrf-token")
        if token:
            s.headers.update({"XSRF-TOKEN": token})
        else:
            print("⚠️ [Huawei] No XSRF-TOKEN received. Login might be blocked/changed.")
            return results

        for station_code, p_key in plants_to_fetch.items():
            payload = {
                "stationCodes": station_code,
                "collectTime": date_iso,
                "dataItemKeys": "day_cap"
            }
            r = s.post(f"{HUAWEI_BASE_URL}/getHistoryStationData", json=payload, timeout=25)
            try:
                j = r.json()
                if isinstance(j, dict) and j.get("data"):
                    val = j["data"][0].get("dataItemMap", {}).get("day_cap", 0)
                    results[p_key] = round(float(val or 0), 2)
            except Exception:
                results[p_key] = 0.0

            print(f"   📊 [Huawei] {p_key} ({station_code}): {results[p_key]} kWh")

    except Exception as e:
        print(f"❌ [Huawei] Error: {e}")

    return results
