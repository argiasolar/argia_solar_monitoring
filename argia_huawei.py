import os
import requests

HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"

def fetch_huawei_data(yesterday_str, plant_keys):
    """Logika z pliku zintegrowanego: /thirdData + History Fallback."""
    print(f"🚀 [Huawei] Connecting to LA5 via /thirdData...")
    results = {key: 0 for key in plant_keys}
    
    try:
        # 1. Logowanie (Dokładnie tak jak w Twoim pliku)
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", 
                             json={"userName": os.environ['HUAWEI_USERNAME'], 
                                   "systemCode": os.environ['HUAWEI_PASSWORD']},
                             timeout=20)
        token = r_log.headers.get('Xsrf-Token') or r_log.headers.get('XSRF-TOKEN')
        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        # 2. Pobieranie danych dla każdej stacji
        for code in plant_keys:
            # Próba A: Historia
            payload = {"stationCodes": code, "collectTime": yesterday_str, "dataItemKeys": "day_cap"}
            r_hist = requests.post(f"{HUAWEI_BASE_URL}/getHistoryStationData", headers=headers, json=payload, timeout=20)
            hist_data = r_hist.json().get('data', [])
            val = hist_data[0].get('dataItemMap', {}).get('day_cap') if hist_data else None
            
            # Próba B: Fallback na Real-Time (jeśli historia pusta)
            if val is None or float(val) == 0:
                r_real = requests.post(f"{HUAWEI_BASE_URL}/getStationRealKpi", headers=headers, json={"stationCodes": code}, timeout=20)
                real_data = r_real.json().get('data', [])
                val = real_data[0].get('dataItemMap', {}).get('day_power') if real_data else 0
            
            results[code] = round(float(val or 0), 2)
        print("✅ [Huawei] Real data imported via /thirdData fallback.")
    except Exception as e:
        print(f"❌ Huawei API Error: {e}")
    return results
