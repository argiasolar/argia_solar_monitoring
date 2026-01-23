# argia_huawei.py
import os
import requests

HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"

def fetch_huawei_data(date_iso: str, plants_to_fetch: dict, timeout: int = 20):
    """
    plants_to_fetch: {SiteID: PlantKey} e.g. {"SAG": "MEX1"}
    Returns: {PlantKey: kWh_float}
    """
    print("🚀 [Huawei] Connecting to LA5 via /thirdData...")
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}

    user = os.environ.get("HUAWEI_USERNAME")
    pwd = os.environ.get("HUAWEI_PASSWORD")
    if not user or not pwd:
        print("❌ [Huawei] Missing env vars: HUAWEI_USERNAME/HUAWEI_PASSWORD")
        return results

    try:
        r_log = requests.post(
            f"{HUAWEI_BASE_URL}/login",
            json={"userName": user, "systemCode": pwd},
            timeout=timeout
        )
        token = r_log.headers.get("XSRF-TOKEN")
        if not token:
            print("❌ [Huawei] No XSRF token (login failed or blocked).")
            return results

        headers = {"XSRF-TOKEN": token, "Content-Type": "application/json"}

        for station_code, plant_key in plants_to_fetch.items():
            payload = {
                "stationCodes": station_code,
                "collectTime": date_iso,
                "dataItemKeys": "day_cap",
            }
            r_hist = requests.post(
                f"{HUAWEI_BASE_URL}/getHistoryStationData",
                headers=headers,
                json=payload,
                timeout=timeout,
            )

            val = 0.0
            try:
                hist = r_hist.json()
                if isinstance(hist, dict) and hist.get("data"):
                    val = hist["data"][0].get("dataItemMap", {}).get("day_cap", 0) or 0
            except Exception:
                val = 0.0

            try:
                results[plant_key] = round(float(val), 2)
            except Exception:
                results[plant_key] = 0.0

            print(f"   📊 [Huawei] {plant_key} ({station_code}): {results[plant_key]} kWh")

    except Exception as e:
        print(f"❌ [Huawei] Error: {e}")

    return results
