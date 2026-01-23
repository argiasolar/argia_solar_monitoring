import os
import requests

HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"

def fetch_huawei_data(yesterday_str, plants_to_fetch):
    """
    plants_to_fetch: słownik {SiteID: PlantKey} przekazany z argia.py
    """
    print(f"🚀 [Huawei] Connecting to LA5 via /thirdData...")
    # Inicjalizujemy wyniki używając PlantKey (MEX1, MEX2 itd.)
    results = {p_key: 0 for p_key in plants_to_fetch.values()}
    
    try:
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", 
                             json={"userName": os.environ['HUAWEI_USERNAME'], 
                                   "systemCode": os.environ['HUAWEI_PASSWORD']}, timeout=20)
        token = r_log.headers.get('XSRF-TOKEN')
        if not token:
            print("❌ [Huawei] Auth failed: No token.")
            return results

        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        for s_id, p_key in plants_to_fetch.items():
            val = 0
            # Próba A: Historia używając SiteID (s_id)
            payload = {"stationCodes": s_id, "collectTime": yesterday_str, "dataItemKeys": "day_cap"}
            r_hist = requests.post(f"{HUAWEI_BASE_URL}/getHistoryStationData", headers=headers, json=payload, timeout=20)
            
            # Bezpieczne wyciąganie danych z JSON
            hist_json = r_hist.json()
            data_list = hist_json.get('data')
            if data_list and len(data_list) > 0:
                val = data_list[0].get('dataItemMap', {}).get('day_cap', 0)
            
            # Próba B: Fallback na Real-Time używając SiteID (s_id)
            if not val or float(val) == 0:
                r_real = requests.post(f"{HUAWEI_BASE_URL}/getStationRealKpi", headers=headers, json={"stationCodes": s_id}, timeout=20)
                real_json = r_real.json()
                real_data = real_json.get('data')
                if real_data and len(real_data) > 0:
                    val = real_data[0].get('dataItemMap', {}).get('dailyEnergy', 0) # W real-time to dailyEnergy
            
            # Zapisujemy wynik pod PlantKey (p_key) dla Google Sheets
            results[p_key] = round(float(val or 0), 2)
            print(f"   📊 [Huawei] {p_key} ({s_id}): {results[p_key]} kWh")
            
    except Exception as e:
        print(f"❌ [Huawei] Error: {e}")
        
    return results
