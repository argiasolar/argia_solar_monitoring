import os
import requests
import json
import time

def fetch_huawei_data(target_date, plant_keys):
    """
    Pobiera dane z Huawei FusionSolar API dedykowanego dla regionu Ameryki Łacińskiej (LA5).
    """
    print(f"🚀 [Huawei] Connecting to FusionSolar LA5 API for {target_date}...")
    
    user = os.environ.get('HUAWEI_USERNAME')
    password = os.environ.get('HUAWEI_PASSWORD')
    
    # Adres serwera LA5 zgodnie z Twoją lokalizacją w portalu
    url_base = "https://la5.fusionsolar.huawei.com/openapi/v1/login"
    
    results = {key: 0 for key in plant_keys}
    
    # Nagłówki imitujące przeglądarkę, aby uniknąć blokady bota
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Content-Type': 'application/json'
    }
    
    try:
        session = requests.Session()
        session.headers.update(headers)
        
        # 1. Logowanie do LA5
        print(f"📡 [Huawei] Logging in to LA5 cluster...")
        login_body = {"userName": user, "systemCode": password}
        login_res = session.post(url_base, json=login_body, timeout=30)
        
        # Sprawdzenie tokena XSRF
        token = login_res.headers.get("xsrf-token")
        
        if not token:
            print(f"❌ [Huawei] Auth failed on LA5. Response Code: {login_res.status_code}")
            return results

        # Krótka pauza, żeby serwer nie uznał nas za agresywnego bota
        time.sleep(1)

        # 2. Pobieranie danych KPI dla stacji
        data_url = "https://la5.fusionsolar.huawei.com/openapi/v1/getStationRealKpi"
        headers_with_token = {"XSRF-TOKEN": token}
        
        # Łączymy klucze stacji w jeden ciąg (np. "MEX1,SLP1,GTO1")
        payload = {"stationCodes": ",".join(plant_keys)}
        
        res = session.post(data_url, json=payload, headers=headers_with_token, timeout=30)
        data = res.json()
        
        if data.get("success") and "data" in data:
            for entry in data["data"]:
                s_code = entry.get("stationCode")
                energy = entry.get("dailyEnergy", 0)
                if s_code in results:
                    # Zamieniamy na float i przypisujemy do klucza
                    results[s_code] = float(energy)
            print(f"✅ [Huawei] Real data imported for {len(data['data'])} plants from LA5.")
        else:
            print(f"⚠️ [Huawei] Login OK, but no data returned for LA5 cluster. Check if Station Codes match.")
            print(f"Debug Info: {data}")

    except Exception as e:
        print(f"❌ [Huawei] Connection Error on LA5: {str(e)}")
        
    return results
