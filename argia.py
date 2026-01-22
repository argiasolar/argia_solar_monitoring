import os
import json
import gspread
import requests
import datetime
import time
from google.oauth2.service_account import Credentials
import growattServer

# --- KONFIGURACJA ---
HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"

# --- FUNKCJE POMOCNICZE ---
def parse_energy_value(value_str):
    if not value_str: return 0.0
    v = str(value_str).lower().strip()
    try:
        if 'mwh' in v: return round(float(v.replace('mwh', '').strip()) * 1000, 2)
        if 'kwh' in v: return round(float(v.replace('kwh', '').strip()), 2)
        return float(v)
    except: return 0.0

def get_weather_data(lat, lon, date_str):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {"latitude": lat, "longitude": lon, "start_date": date_str, "end_date": date_str, "daily": "shortwave_radiation_sum", "timezone": "auto"}
    for attempt in range(4):
        try:
            res = requests.get(url, params=params, timeout=45)
            data = res.json()
            return round(data['daily']['shortwave_radiation_sum'][0] / 3.6, 3)
        except:
            time.sleep(15)
    return 0

# --- MODUŁ GROWATT (Przywrócona działająca wersja) ---
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
        print(f"❌ Growatt Error ({target_plant_id}): {e}")
        return 0

# --- MODUŁ HUAWEI (Twoja działająca metoda) ---
def get_huawei_data_map():
    try:
        # 1. Login
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", json={"userName": os.environ['HUAWEI_USERNAME'], "systemCode": os.environ['HUAWEI_PASSWORD']})
        token = r_log.headers.get('Xsrf-Token') or r_log.headers.get('XSRF-TOKEN')
        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        # 2. Lista stacji
        r_list = requests.post(f"{HUAWEI_BASE_URL}/getStationList", headers=headers, json={})
        stations = r_list.json().get('data', [])
        codes = [s['stationCode'] for s in stations]
        
        # 3. KPI
        r_kpi = requests.post(f"{HUAWEI_BASE_URL}/getStationRealKpi", headers=headers, json={"stationCodes": ",".join(codes)})
        kpi_results = r_kpi.json().get('data', [])
        
        energy_map = {}
        for k in kpi_results:
            name = next((s['stationName'] for s in stations if s['stationCode'] == k['stationCode']), None)
            if name:
                energy_map[name] = k['dataItemMap'].get('day_power', 0)
        return energy_map
    except Exception as e:
        print(f"❌ Huawei Sync Error: {e}")
        return {}

# --- MAIN ---
def main():
    print(f"🚀 Start Argia Solar Metering - {datetime.datetime.now()}")
    
    # GSheets Setup
    try:
        creds_json = os.environ.get('GOOGLE_CREDENTIALS')
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(os.environ.get('GOOGLE_SHEET_ID'))
        config_sheet = sh.worksheet("Config_Plants")
        raw_data_sheet = sh.worksheet("RawData")
    except Exception as e:
        print(f"🚨 GSheets Error: {e}"); return

    plants = config_sheet.get_all_records()
    yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Pobieramy dane Huawei raz
    huawei_production = get_huawei_data_map()

    for p in plants:
        brand = str(p['Brand']).upper()
        s_id = str(p['SiteID']).strip()
        p_key = p['Plantkey']
        print(f"\n--- Przetwarzam: {p_key} ({brand}) ---")
        
        real_energy = 0
        if brand == "GROWATT":
            real_energy = fetch_growatt_data(s_id, yesterday_str)
        elif brand == "HUAWEI":
            real_energy = huawei_production.get(s_id, 0)
        
        # Pogoda i KPI
        irrad = get_weather_data(p['Latitude'], p['Longtitude'], yesterday_str)
        kwp = float(p['kWp_DC'] or 0)
        possible = round(kwp * irrad * 0.85, 2)
        pr = round(real_energy / (kwp * irrad), 3) if (irrad > 0 and kwp > 0) else 0
            
        row = [yesterday_str, p_key, p['CustomerName'], real_energy, irrad, possible, pr, p['PR_Target']]
        
        try:
            raw_data_sheet.append_row(row)
            print(f"✅ Wynik: {real_energy} kWh | Pogoda: {irrad} kWh/m2.")
        except Exception as e:
            print(f"❌ Błąd zapisu: {e}")

    print(f"\n✅ Synchronizacja zakończona.")

if __name__ == "__main__":
    main()
