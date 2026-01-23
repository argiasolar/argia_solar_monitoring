import os
import requests

HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"

def fetch_huawei_data(yesterday_str, plants_to_fetch):
    print(f"🚀 [Huawei] Connecting to LA5 via /thirdData...")
    results = {p_key: 0 for p_key in plants_to_fetch.values()}
    
    try:
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", 
                             json={"userName": os.environ['HUAWEI_USERNAME'], 
                                   "systemCode": os.environ['HUAWEI_PASSWORD']}, timeout=20)
        token = r_log.headers.get('XSRF-TOKEN')
        if not token:
            return results

        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        for s_id, p_key in plants_to_fetch.items():
            # Próba A: Historia
            payload = {"stationCodes": s_id, "collectTime": yesterday_str, "dataItemKeys": "day_cap"}
            r_hist = requests.post(f"{HUAWEI_BASE_URL}/getHistoryStationData", headers=headers, json=payload, timeout=20)
            
            try:
                hist_json = r_hist.json()
                # Kluczowa poprawka: sprawdzamy czy hist_json to słownik, a nie tekst błędu
                if isinstance(hist_json, dict) and hist_json.get('data'):
                    val = hist_json['data'][0].get('dataItemMap', {}).get('day_cap', 0)
                    results[p_key] = round(float(val or 0), 2)
            except:
                results[p_key] = 0
            
            print(f"   📊 [Huawei] {p_key} ({s_id}): {results[p_key]} kWh")
            
    except Exception as e:
        print(f"❌ [Huawei] Error: {e}")
    return results
