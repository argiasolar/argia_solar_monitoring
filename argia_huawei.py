import os
import requests

HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"

def fetch_huawei_data(yesterday_str, plants_to_fetch):
    print(f"🚀 [Huawei] Connecting to LA5 via /thirdData...")
    results = {s_id: 0 for s_id in plants_to_fetch.keys()}
    
    try:
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", 
                             json={"userName": os.environ['HUAWEI_USERNAME'], 
                                   "systemCode": os.environ['HUAWEI_PASSWORD']}, timeout=20)
        token = r_log.headers.get('XSRF-TOKEN')
        if not token:
            print("❌ [Huawei] Auth failed: No token.")
            return results

        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        for s_id in plants_to_fetch.keys():
            val = 0
            # Próba A: Historia
            payload = {"stationCodes": s_id, "collectTime": yesterday_str, "dataItemKeys": "day_cap"}
            r_hist = requests.post(f"{HUAWEI_BASE_URL}/getHistoryStationData", headers=headers, json=payload, timeout=20)
            hist_json = r_hist.json()
            
            # Bezpieczne wyciąganie danych (poprawka NoneType)
            data_list = hist_json.get('data')
            if data_list and len(data_list) > 0:
                val = data_list[0].get('dataItemMap', {}).get('day_cap', 0)
            
            # Próba B: Fallback na Real-Time
            if not val or float(val) == 0:
                r_real = requests.post(f"{HUAWEI_BASE_URL}/getStationRealKpi", headers=headers, json={"stationCodes": s_id}, timeout=20)
                real_json = r_real.json()
                real_data = real_json.get('data')
                if real_data and len(real_data) > 0:
                    val = real_data[0].get('dataItemMap', {}).get('day_power', 0)
            
            results[s_id] = round(float(val or 0), 2)
        print("✅ [Huawei] Request finished.")
    except Exception as e:
        print(f"❌ [Huawei] Error: {e}")
    return results
