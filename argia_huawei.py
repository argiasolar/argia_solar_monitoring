import os
import requests

HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"

def fetch_huawei_data(yesterday_str, plants_to_fetch):
    """plants_to_fetch to słownik {SiteID: PlantKey}"""
    print(f"🚀 [Huawei] Connecting to LA5 via /thirdData...")
    results = {p_key: 0 for p_key in plants_to_fetch.values()}
    
    try:
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", 
                             json={"userName": os.environ['HUAWEI_USERNAME'], 
                                   "systemCode": os.environ['HUAWEI_PASSWORD']}, timeout=20)
        token = r_log.headers.get('XSRF-TOKEN')
        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        for s_id, p_key in plants_to_fetch.items():
            # Próba A: Historia
            payload = {"stationCodes": s_id, "collectTime": yesterday_str, "dataItemKeys": "day_cap"}
            r_hist = requests.post(f"{HUAWEI_BASE_URL}/getHistoryStationData", headers=headers, json=payload, timeout=20)
            val = r_hist.json().get('data', [{}])[0].get('dataItemMap', {}).get('day_cap')
            
            # Próba B: Fallback
            if val is None or float(val) == 0:
                r_real = requests.post(f"{HUAWEI_BASE_URL}/getStationRealKpi", headers=headers, json={"stationCodes": s_id}, timeout=20)
                val = r_real.json().get('data', [{}])[0].get('dataItemMap', {}).get('day_power')
            
            results[p_key] = round(float(val or 0), 2)
        print("✅ [Huawei] Data synced.")
    except Exception as e:
        print(f"❌ Huawei API Error: {e}")
    return results
