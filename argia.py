import os
import json
import gspread
import requests
import datetime
import time
from google.oauth2.service_account import Credentials
import growattServer

# --- KONFIGURACJA ---
# Lista serwerów Huawei do sprawdzenia
HUAWEI_REGIONS = [
    "https://la5.fusionsolar.huawei.com",
    "https://eu5.fusionsolar.huawei.com",
    "https://intl.fusionsolar.huawei.com", # Czasem bez intl
    "https://uni001eu5.fusionsolar.huawei.com"
]

# --- FUNKCJE POMOCNICZE ---
def parse_energy_value(value_str):
    if not value_str: return 0.0
    v = str(value_str).lower().strip()
    try:
        if 'mwh' in v:
            return round(float(v.replace('mwh', '').strip()) * 1000, 2)
        elif 'kwh' in v:
            return round(float(v.replace('kwh', '').strip()), 2)
        return float(v)
    except:
        return 0.0

def get_weather_data(lat, lon, date_str):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": date_str, "end_date": date_str,
        "daily": "shortwave_radiation_sum", "timezone": "auto"
    }
    for attempt in range(4): # Zwiększone do 4 prób
        try:
            # Zwiększony timeout i dodany stream=False dla stabilności na GitHub
            res = requests.get(url, params=params, timeout=45)
            res.raise_for_status()
            data = res.json()
            if 'daily' in data and data['daily']['shortwave_radiation_sum'][0] is not None:
                mj_m2 = data['daily']['shortwave_radiation_sum'][0]
                return round(mj_m2 / 3.6, 3)
            return 0
        except Exception as e:
            print(f"⚠️ Próba {attempt+1} pogoda ({lat},{lon}): {e}")
            time.sleep(15)
    return 0

# --- MODUŁ GROWATT ---
def fetch_growatt_data(target_plant_id, date_str):
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    api = growattServer.GrowattApi()
    api.server_url = 'http://server.growatt.com/'
    api.session.headers.update({'User-Agent': 'Mozilla/5.0'})
    try:
        login_res = api.login(user, password)
        user_id = login_res.get('user_id') or login_res.get('userId') or login_res.get('data', {}).get('userId')
        plants_response = api.plant_list(user_id)
        plants_list = plants_response if isinstance(plants_response, list) else plants_response.get('data', [])
        for p in plants_list:
            if str(p.get('plantId')) == str(target_plant_id):
                raw_energy = p.get('todayEnergy') or p.get('energy_today') or "0"
                return parse_energy_value(raw_energy)
        return 0
    except Exception as e:
        print(f"❌ Growatt Error: {e}")
        return 0

# --- MODUŁ HUAWEI (Smart Discovery) ---
def get_huawei_session():
    user = os.environ.get("HUAWEI_USERNAME")
    pw = os.environ.get("HUAWEI_PASSWORD")
    
    for region_url in HUAWEI_REGIONS:
        url = f"{region_url}/thirdData/login"
        try:
            print(f"📡 Testuję region Huawei: {region_url}...")
            r = requests.post(url, json={"userName": user, "systemCode": pw}, timeout=15)
            if r.status_code == 200:
                token = r.json().get("data", {}).get("xsrfToken")
                if token:
                    print(f"✅ Połączono z Huawei przez: {region_url}")
                    return token, region_url
        except:
            continue
    print("❌ Nie udało się zalogować do żadnego regionu Huawei.")
    return None, None

def fetch_huawei_energy(station_code, token, base_url, date_str):
    url = f"{base_url}/thirdData/getStationKpi"
    formatted_date = date_str.replace("-", "")
    try:
        r = requests.post(url, 
                         json={"stationCodes": station_code, "collectTime": formatted_date}, 
                         headers={"xsrf-token": token}, 
                         timeout=20)
        data = r.json().get("data", [])
        return float(data[0].get("dayPower", 0)) if data else 0
    except Exception as e:
        print(f"❌ Huawei Data Error ({station_code}): {e}")
        return 0

# --- GŁÓWNA LOGIKA ---
def main():
    print(f"🚀 Start Argia Solar Metering - {datetime.datetime.now()}")
    
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds_json = os.environ.get('GOOGLE_CREDENTIALS')
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(os.environ.get('GOOGLE_SHEET_ID'))
        config_sheet = sh.worksheet("Config_Plants")
        raw_data_sheet = sh.worksheet("RawData")
    except Exception as e:
        print(f"🚨 Błąd Google Sheets: {e}")
        return

    plants = config_sheet.get_all_records()
    yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    
    # Huawei Discovery
    huawei_token, huawei_url = get_huawei_session()

    for p in plants:
        plant_key = p['Plantkey']
        brand = str(p['Brand']).upper()
        s_id = str(p['SiteID']).strip()
        
        print(f"\n--- Przetwarzam: {plant_key} ({brand}) ---")
        
        real_energy = 0
        if brand == "GROWATT":
            real_energy = fetch_growatt_data(s_id, yesterday_str)
        elif brand == "HUAWEI":
            if huawei_token:
                real_energy = fetch_huawei_energy(s_id, huawei_token, huawei_url, yesterday_str)
            else:
                print(f"⚠️ Pomijam {plant_key} - brak sesji.")
        
        irradiance = get_weather_data(p['Latitude'], p['Longtitude'], yesterday_str)
        kwp_dc = float(p['kWp_DC'] or 0)
        possible_gen = round(kwp_dc * irradiance * 0.85, 2)
        real_pr = round(real_energy / (kwp_dc * irradiance), 3) if (irradiance > 0 and kwp_dc > 0) else 0
            
        row_to_save = [yesterday_str, plant_key, p['CustomerName'], real_energy, irradiance, possible_gen, real_pr, p['PR_Target']]
        
        try:
            raw_data_sheet.append_row(row_to_save)
            print(f"✅ Wynik: {real_energy} kWh | Pogoda: {irradiance} kWh/m2.")
        except Exception as e:
            print(f"❌ Błąd zapisu: {e}")

    print(f"\n✅ Synchronizacja zakończona.")

if __name__ == "__main__":
    main()
