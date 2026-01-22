import os
import json
import gspread
import requests
import datetime
import time
from google.oauth2.service_account import Credentials
import growattServer

# --- KONFIGURACJA ---
HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com"

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
    for attempt in range(4):
        try:
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

# --- MODUŁ HUAWEI (DOKŁADNIE TWOJA DZIAŁAJĄCA LOGIKA) ---
def huawei_login():
    url = f"{HUAWEI_BASE_URL}/thirdData/login"
    payload = {
        "userName": os.environ["HUAWEI_USERNAME"],
        "systemCode": os.environ["HUAWEI_PASSWORD"]
    }
    try:
        print(f"📡 Próba logowania Huawei (Twoja metoda) dla: {payload['userName']}")
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()["data"]["xsrfToken"]
    except Exception as e:
        print(f"❌ Huawei Login Error: {e}")
        return None

def fetch_huawei_energy(station_code, token, date_str):
    url = f"{HUAWEI_BASE_URL}/thirdData/getStationKpi"
    headers = {"xsrf-token": token}
    payload = {
        "stationCodes": station_code,
        "collectTime": date_str # Format YYYY-MM-DD
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json().get("data")
        if data and len(data) > 0:
            return round(data[0].get("dayPower", 0), 2)
        return 0
    except Exception as e:
        print(f"❌ Huawei Data Error ({station_code}): {e}")
        return 0

# --- GŁÓWNA LOGIKA ---
def main():
    print(f"🚀 Start Argia Solar Metering - {datetime.datetime.now()}")
    
    try:
        creds_json = os.environ.get('GOOGLE_CREDENTIALS')
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(os.environ.get('GOOGLE_SHEET_ID'))
        config_sheet = sh.worksheet("Config_Plants")
        raw_data_sheet = sh.worksheet("RawData")
    except Exception as e:
        print(f"🚨 Błąd Google Sheets: {e}"); return

    plants = config_sheet.get_all_records()
    # Format daty YYYY-MM-DD
    yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Pobranie tokena Huawei raz na całą pętlę
    h_token = huawei_login()

    for p in plants:
        plant_key = p['Plantkey']
        brand = str(p['Brand']).upper()
        s_id = str(p['SiteID']).strip()
        
        print(f"\n--- Przetwarzam: {plant_key} ({brand}) ---")
        
        real_energy = 0
        if brand == "GROWATT":
            real_energy = fetch_growatt_data(s_id, yesterday_str)
        elif brand == "HUAWEI":
            if h_token:
                real_energy = fetch_huawei_energy(s_id, h_token, yesterday_str)
            else:
                print(f"⚠️ Pomijam {plant_key} - brak tokena Huawei.")
        
        irradiance = get_weather_data(p['Latitude'], p['Longtitude'], yesterday_str)
        kwp_dc = float(p['kWp_DC'] or 0)
        possible_gen = round(kwp_dc * irradiance * 0.85, 2)
        real_pr = round(real_energy / (kwp_dc * irradiance), 3) if (irradiance > 0 and kwp_dc > 0) else 0
            
        row = [yesterday_str, plant_key, p['CustomerName'], real_energy, irradiance, possible_gen, real_pr, p['PR_Target']]
        
        try:
            raw_data_sheet.append_row(row)
            print(f"✅ Wynik: {real_energy} kWh | Pogoda: {irradiance} kWh/m2.")
        except Exception as e:
            print(f"❌ Błąd zapisu: {e}")

    print(f"\n✅ Synchronizacja zakończona.")

if __name__ == "__main__":
    main()
