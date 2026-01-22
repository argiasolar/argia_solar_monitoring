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
            res = requests.get(url, params=params, timeout=20)
            return round(res.json()['daily']['shortwave_radiation_sum'][0] / 3.6, 3)
        except: time.sleep(10)
    return 0

# --- MODUŁ GROWATT ---
def fetch_growatt_data(target_plant_id, date_str):
    user, password = os.environ.get('GROWATT_USERNAME'), os.environ.get('GROWATT_PASSWORD')
    api = growattServer.GrowattApi()
    api.server_url = 'http://server.growatt.com/'
    api.session.headers.update({'User-Agent': 'Mozilla/5.0'})
    try:
        api.login(user, password)
        login_res = api.login(user, password)
        user_id = login_res.get('user_id') or login_res.get('userId')
        plants = api.plant_list(user_id)
        plants_list = plants if isinstance(plants, list) else plants.get('data', [])
        for p in plants_list:
            if str(p.get('plantId')) == str(target_plant_id):
                # Dla Growatt dzienne dane są stabilne w liście
                return parse_energy_value(p.get('todayEnergy') or p.get('energy_today'))
        return 0
    except: return 0

# --- MODUŁ HUAWEI (Wersja Historyczna - DZIAŁAJĄCA) ---
def get_huawei_yesterday_data(yesterday_str):
    try:
        # 1. Login
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", 
                             json={"userName": os.environ['HUAWEI_USERNAME'], 
                                   "systemCode": os.environ['HUAWEI_PASSWORD']})
        token = r_log.headers.get('Xsrf-Token') or r_log.headers.get('XSRF-TOKEN')
        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        # 2. Lista stacji
        r_list = requests.post(f"{HUAWEI_BASE_URL}/getStationList", headers=headers, json={})
        stations = r_list.json().get('data', [])
        codes = [s['stationCode'] for s in stations]
        
        # 3. KPI Historyczne za WCZORAJ
        payload = {
            "stationCodes": ",".join(codes),
            "collectTime": yesterday_str,
            "dataItemKeys": "day_cap"
        }
        r_hist = requests.post(f"{HUAWEI_BASE_URL}/getHistoryStationData", headers=headers, json=payload)
        h_data = r_hist.json().get('data', [])
        
        energy_map = {}
        for item in h_data:
            name = next((s['stationName'] for s in stations if s['stationCode'] == item['stationCode']), None)
            if name:
                val = item.get('dataItemMap', {}).get('day_cap', 0)
                energy_map[name] = round(float(val if val is not None else 0), 2)
        return energy_map
    except Exception as e:
        print(f"❌ Huawei Daily Error: {e}")
        return {}

# --- GŁÓWNA LOGIKA ---
def main():
    yesterday = (datetime.date.today() - datetime.timedelta(days=1))
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    print(f"🚀 Start Argia Daily Sync for: {yesterday_str}")
    
    # GSheets
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ['GOOGLE_SHEET_ID'])
    config_sheet = sh.worksheet("Config_Plants")
    raw_sheet = sh.worksheet("RawData")
    
    plants = config_sheet.get_all_records()
    huawei_map = get_huawei_yesterday_data(yesterday_str)
    
    final_rows = []
    for p in plants:
        brand = str(p['Brand']).upper()
        p_key = p['Plantkey']
        s_id = str(p['SiteID']).strip()
        print(f"Przetwarzam {p_key}...")

        energy = 0
        if brand == "GROWATT":
            energy = fetch_growatt_data(s_id, yesterday_str)
        elif brand == "HUAWEI":
            energy = huawei_map.get(s_id, 0)
            
        irrad = get_weather_data(p['Latitude'], p['Longtitude'], yesterday_str)
        kwp = float(p['kWp_DC'] or 0)
        possible = round(kwp * irrad * 0.85, 2)
        pr = round(energy / (kwp * irrad), 3) if (irrad > 0 and kwp > 0) else 0
        
        final_rows.append([yesterday_str, p_key, p['CustomerName'], energy, irrad, possible, pr, p['PR_Target']])

    if final_rows:
        raw_sheet.append_rows(final_rows)
        print(f"✅ Dane za {yesterday_str} zapisane.")

if __name__ == "__main__":
    main()
