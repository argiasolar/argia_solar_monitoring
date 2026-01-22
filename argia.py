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

# --- MODUŁ POGODY Z INTERPOLACJĄ ---
def get_smart_weather(lat, lon, date_str, plants_config, current_p_key):
    def fetch(la, lo):
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {"latitude": la, "longitude": lo, "start_date": date_str, "end_date": date_str, "daily": "shortwave_radiation_sum", "timezone": "auto"}
        try:
            res = requests.get(url, params=params, timeout=15)
            return round(res.json()['daily']['shortwave_radiation_sum'][0] / 3.6, 3)
        except: return 0

    val = fetch(lat, lon)
    if val > 0: return val
    
    # Jeśli 0, szukamy u sąsiadów (SLP1, GTO1 lub MEX1)
    print(f"⚠️ Brak pogody dla {current_p_key}, próbuję interpolacji...")
    neighbors = ["SLP1", "GTO1", "MEX1"]
    vals = []
    for n_key in neighbors:
        if n_key == current_p_key or n_key not in plants_config: continue
        n_val = fetch(plants_config[n_key]['Latitude'], plants_config[n_key]['Longtitude'])
        if n_val > 0: vals.append(n_val)
    
    return round(sum(vals) / len(vals), 3) if vals else 0

# --- MODUŁ HUAWEI Z FALLBACKIEM ---
def fetch_huawei_data(yesterday_str):
    try:
        # 1. Login
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", json={"userName": os.environ['HUAWEI_USERNAME'], "systemCode": os.environ['HUAWEI_PASSWORD']})
        token = r_log.headers.get('Xsrf-Token') or r_log.headers.get('XSRF-TOKEN')
        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        # 2. Lista stacji
        r_list = requests.post(f"{HUAWEI_BASE_URL}/getStationList", headers=headers, json={})
        stations = r_list.json().get('data', [])
        
        energy_map = {}
        for s in stations:
            code = s['stationCode']
            name = s['stationName']
            
            # Próba 1: Historia
            payload = {"stationCodes": code, "collectTime": yesterday_str, "dataItemKeys": "day_cap"}
            r_hist = requests.post(f"{HUAWEI_BASE_URL}/getHistoryStationData", headers=headers, json=payload)
            val = r_hist.json().get('data', [{}])[0].get('dataItemMap', {}).get('day_cap')
            
            # Próba 2: Fallback na Real-Time (jeśli historia milczy lub jest 0)
            if val is None or float(val) == 0:
                r_real = requests.post(f"{HUAWEI_BASE_URL}/getStationRealKpi", headers=headers, json={"stationCodes": code})
                val = r_real.json().get('data', [{}])[0].get('dataItemMap', {}).get('day_power')
            
            energy_map[code] = round(float(val or 0), 2)
        return energy_map
    except: return {}

# --- MODUŁ GROWATT ---
def fetch_growatt_data(api, plant_id, date_str):
    try:
        # Używamy plant_detail dla konkretnej daty historycznej
        data = api.plant_detail(plant_id, date_str)
        return float(data.get('today_energy', 0))
    except:
        # Fallback do listy ogólnej jeśli detail zawiedzie
        try:
            for p in api.plant_list(api.session.auth[0]):
                if str(p['plantId']) == str(plant_id): return float(p['todayEnergy'])
        except: return 0

# --- GŁÓWNA LOGIKA ---
def main():
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"🚀 Synchronizacja za dzień: {yesterday}")
    
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ['GOOGLE_SHEET_ID'])
    config_sheet = sh.worksheet("Config_Plants")
    raw_sheet = sh.worksheet("RawData")
    
    plants_config = {p['Plantkey']: p for p in config_sheet.get_all_records()}
    huawei_energies = fetch_huawei_data(yesterday)
    
    # Growatt API login
    g_api = growattServer.GrowattApi()
    g_api.server_url = 'http://server.growatt.com/'
    try: g_api.login(os.environ['GROWATT_USERNAME'], os.environ['GROWATT_PASSWORD'])
    except: print("❌ Growatt Login Failed")

    final_rows = []
    for p_key, conf in plants_config.items():
        brand = str(conf['Brand']).upper()
        s_id = str(conf['SiteID']).strip()
        
        energy = 0
        if brand == "HUAWEI":
            energy = huawei_energies.get(s_id, 0)
        elif brand == "GROWATT":
            energy = fetch_growatt_data(g_api, s_id, yesterday)
            
        weather = get_smart_weather(conf['Latitude'], conf['Longtitude'], yesterday, plants_config, p_key)
        kwp = float(conf['kWp_DC'] or 0)
        possible = round(kwp * weather * 0.85, 2)
        pr = round(energy / (kwp * weather), 3) if (weather > 0 and kwp > 0) else 0
        
        final_rows.append([yesterday, p_key, conf['CustomerName'], energy, weather, possible, pr, conf['PR_Target']])
        print(f"✅ {p_key}: {energy} kWh")

    if final_rows:
        raw_sheet.append_rows(final_rows)
        print(f"💾 Zapisano dane za {yesterday}")

if __name__ == "__main__":
    main()
