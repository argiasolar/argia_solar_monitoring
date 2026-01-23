import os
import requests
import json

def fetch_huawei_data(target_date, plant_keys):
    """Pobiera REALNE dane z Huawei FusionSolar API."""
    print(f"🚀 [Huawei] Connecting to FusionSolar API for {target_date}...")
    
    user = os.environ.get('HUAWEI_USERNAME')
    password = os.environ.get('HUAWEI_PASSWORD')
    
    # URL dla regionu International/NA
    url_base = "https://intl.fusionsolar.huawei.com/openapi/v1/login"
    
    results = {key: 0 for key in plant_keys}
    
    try:
        login_body = {"userName": user, "systemCode": password}
        session = requests.Session()
        login_res = session.post(url_base, json=login_body, timeout=20)
        
        token = login_res.headers.get("xsrf-token")
        if not token:
            print("❌ [Huawei] Error: Failed to obtain API token. Check credentials.")
            return results

        # Pobieranie danych dla każdej stacji
        data_url = "https://intl.fusionsolar.huawei.com/openapi/v1/getStationRealKpi"
        headers = {"XSRF-TOKEN": token}
        
        # Huawei pozwala na zapytanie o wiele stacji na raz (stationCodes to string rozdzielony przecinkami)
        station_codes = ",".join(plant_keys)
        payload = {"stationCodes": station_codes}
        
        res = session.post(data_url, json=payload, headers=headers)
        data = res.json()
        
        if data.get("success") and "data" in data:
            for entry in data["data"]:
                s_code = entry.get("stationCode")
                energy = entry.get("dailyEnergy", 0)
                if s_code in results:
                    results[s_code] = float(energy)
            print(f"✅ [Huawei] Fetched real data for {len(data['data'])} plants.")
        else:
            print(f"⚠️ [Huawei] API returned success=False or empty data.")

    except Exception as e:
        print(f"❌ [Huawei] API Error: {str(e)}")
        
    return results
