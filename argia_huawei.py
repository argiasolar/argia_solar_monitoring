import os
import requests
import json
import time

def fetch_huawei_data(target_date, plant_keys):
    print(f"🚀 [Huawei] Connecting to FusionSolar LA5 API for {target_date}...")
    user = os.environ.get('HUAWEI_USERNAME')
    password = os.environ.get('HUAWEI_PASSWORD')
    
    url_base = "https://la5.fusionsolar.huawei.com/openapi/v1/login"
    results = {key: 0 for key in plant_keys}
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Content-Type': 'application/json'
    }
    
    try:
        session = requests.Session()
        # Logowanie - wymuszamy JSON body bez zbędnych spacji
        login_payload = json.dumps({"userName": user, "systemCode": password})
        login_res = session.post(url_base, data=login_payload, headers=headers, timeout=30)
        
        token = login_res.headers.get("xsrf-token")
        
        # Jeśli tokena nie ma w nagłówku, sprawdzamy czy nie jest w JSON (niektóre regiony tak robią)
        if not token:
            print(f"⚠️ [Huawei] Token missing. Server Response: {login_res.text}")
            return results

        print(f"✅ [Huawei] Auth successful (Token received).")
        time.sleep(2) # Bezpieczna pauza

        data_url = "https://la5.fusionsolar.huawei.com/openapi/v1/getStationRealKpi"
        payload = {"stationCodes": ",".join(plant_keys)}
        
        res = session.post(data_url, json=payload, headers={"XSRF-TOKEN": token}, timeout=30)
        data = res.json()
        
        if data.get("success") and "data" in data:
            for entry in data["data"]:
                results[entry["stationCode"]] = float(entry.get("dailyEnergy", 0))
            print(f"✅ [Huawei] Real data imported.")
        else:
            print(f"⚠️ [Huawei] No data returned. Result: {data.get('failCode')}")

    except Exception as e:
        print(f"❌ [Huawei] Connection Error: {str(e)}")
        
    return results
