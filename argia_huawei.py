import os
import requests
import json

def fetch_huawei_data(target_date, plant_keys):
    """Implementacja v3.2: Obsługa sesji i stałych nagłówków FusionSolar."""
    print(f"🚀 [Huawei] Connecting to FusionSolar API for {target_date}...")
    user = os.environ.get('HUAWEI_USERNAME')
    password = os.environ.get('HUAWEI_PASSWORD')
    
    url_base = "https://intl.fusionsolar.huawei.com/openapi/v1/login"
    results = {key: 0 for key in plant_keys}
    
    # Nagłówki z v3.2, które imitują przeglądarkę i stabilizują sesję
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Content-Type': 'application/json'
    }
    
    try:
        session = requests.Session()
        session.headers.update(headers)
        
        login_res = session.post(url_base, json={"userName": user, "systemCode": password}, timeout=30)
        token = login_res.headers.get("xsrf-token")
        
        if not token:
            print("⚠️ [Huawei] Login failed (No XSRF Token). Possible DNS/Auth issue.")
            return results

        # v3.2: Pobieranie danych dla wszystkich stacji jednym strzałem
        data_url = "https://intl.fusionsolar.huawei.com/openapi/v1/getStationRealKpi"
        res = session.post(data_url, json={"stationCodes": ",".join(plant_keys)}, headers={"XSRF-TOKEN": token})
        data = res.json()
        
        if data.get("success") and "data" in data:
            for entry in data["data"]:
                results[entry["stationCode"]] = float(entry.get("dailyEnergy", 0))
            print(f"✅ [Huawei] Real data imported successfully.")
        else:
            print(f"⚠️ [Huawei] Connected, but no data for date {target_date}.")

    except Exception as e:
        print(f"❌ [Huawei] Error: {str(e)}")
        
    return results
