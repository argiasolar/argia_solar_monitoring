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

# --- MODUŁ POGODY Z INTERPOLACJĄ (Sugerowane przez użytkownika) ---
def get_smart_weather(lat, lon, date_str, plants_config, current_p_key):
    def fetch(la, lo):
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": la, "longitude": lo, 
            "start_date": date_str, "end_date": date_str, 
            "daily": "shortwave_radiation_sum", "timezone": "auto"
        }
        try:
            res = requests.get(url, params=params, timeout=15)
            res.raise_for_status()
            return round(res.json()['daily']['shortwave_radiation_sum'][0] / 3.6, 3)
        except:
            return 0

    val = fetch(lat, lon)
    if val > 0:
        return val
    
    # INTERPOLACJA: Jeśli 0, szukamy u sąsiadów (SLP1, GTO1 lub MEX1)
    print(f"⚠️ Brak pogody dla {current_p_key}, próbuję interpolacji z sąsiednich stacji...")
    neighbors = ["SLP1", "GTO1", "MEX1"]
    vals = []
    for n_key in neighbors:
        if n_key == current_p_key or n_key not in plants_config:
            continue
        n_conf = plants_config[n_key]
        n_val = fetch(n_conf['Latitude'], n_conf['Longtitude'])
        if n_val > 0:
            vals.append(n_val)
    
    return round(sum(vals) / len(vals), 3) if vals else 0

# --- MODUŁ HUAWEI (Fallback: Historia -> RealTime) ---
def fetch_huawei_data(yesterday_str):
    try:
        # 1. Login
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", 
                             json={"userName": os.environ['HUAWEI_USERNAME'], 
                                   "systemCode": os.environ['HUAWEI_PASSWORD']},
                             timeout=20)
        token = r_log.headers.get('Xsrf-Token') or r_log.headers.get('XSRF-TOKEN')
        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        # 2. Lista stacji
        r_list = requests.post(f"{HUAWEI_BASE_URL}/getStationList", headers=headers, json={}, timeout=20)
        stations = r_list.json().get('data', [])
        
        energy_map = {}
        for s in stations:
            code = s['stationCode']
            
            # Próba A: Historia (day_cap)
            payload = {"stationCodes": code, "collectTime": yesterday_str, "dataItemKeys": "day_cap"}
            r_hist = requests.post(f"{HUAWEI_BASE_URL}/getHistoryStationData", headers=headers, json=payload, timeout=20)
            hist_data = r_hist.json().get('data', [])
            val = hist_data[0].get('dataItemMap', {}).get('day_cap') if hist_data else None
            
            # Próba B: Fallback na Real-Time (day_power - stan z końca dnia)
            if val is None or float(val) == 0:
                r_real = requests.post(f"{HUAWEI_BASE_URL}/getStationRealKpi", headers=headers, json={"stationCodes": code}, timeout=20)
                real_data = r_real.json().get('data', [])
                val = real_data[0].get('dataItemMap', {}).get('day_power') if real_data else 0
            
            energy_map[code] = round(float(val or 0), 2)
        return energy_map
    except Exception as e:
        print(f"❌ Huawei API Error: {e}")
        return {}

# --- MODUŁ GROWATT (Zabezpieczone logowanie) ---
def fetch_growatt_data(api, plant_id, date_str):
    try:
        # Próba uzyskania danych bezpośrednio z detali dnia
        data = api.plant_detail(plant_id, date_str)
        val = data.get('today_energy') or data.get('todayEnergy')
        if val: return float(val)
        
        # Rezerwowo z listy ogólnej
        plants = api.plant_list(api.session.auth[0])
        for p in plants:
            if str(p['plantId']) == str(plant_id):
                return float(p['todayEnergy'])
    except:
        return 0
    return 0

# --- GŁÓWNA LOGIKA ---
def main():
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"🚀 Synchronizacja Argia Solar za dzień: {yesterday}")
    
    # 1. Autoryzacja Google Sheets
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ['GOOGLE_SHEET_ID'])
    config_sheet = sh.worksheet("Config_Plants")
    raw_sheet = sh.worksheet("RawData")
    
    plants_config = {p['Plantkey']: p for p in config_sheet.get_all_records()}
    
    # 2. Pobieranie danych Huawei
    huawei_energies = fetch_huawei_data(yesterday)
    
    # 3. Logowanie Growatt (z ponowieniem)
    g_api = growattServer.GrowattApi()
    g_api.server_url = 'http://server.growatt.com/'
    growatt_ok = False
    for i in range(3):
        try:
            g_api.login(os.environ['GROWATT_USERNAME'], os.environ['GROWATT_PASSWORD'])
            growatt_ok = True
            break
        except:
            print(f"⚠️ Growatt login attempt {i+1} failed, retrying...")
            time.sleep(10)

    # 4. Przetwarzanie każdej instalacji
    final_rows = []
    total_energy_collected = 0

    # Sortujemy p_key, aby zachować stałą kolejność w arkuszu
    sorted_keys = sorted(plants_config.keys())
    
    for p_key in sorted_keys:
        conf = plants_config[p_key]
        brand = str(conf['Brand']).upper()
        s_id = str(conf['SiteID']).strip()
        
        energy = 0
        if brand == "HUAWEI":
            energy = huawei_energies.get(s_id, 0)
        elif brand == "GROWATT" and growatt_ok:
            energy = fetch_growatt_data(g_api, s_id, yesterday)
            
        weather = get_smart_weather(conf['Latitude'], conf['Longtitude'], yesterday, plants_config, p_key)
        
        kwp = float(conf['kWp_DC'] or 0)
        possible = round(kwp * weather * 0.85, 2)
        # PR może przekraczać 1 (np. przy panelach bifacjalnych)
        pr = round(energy / (kwp * weather), 3) if (weather > 0 and kwp > 0) else 0
        
        total_energy_collected += energy
        final_rows.append([yesterday, p_key, conf['CustomerName'], energy, weather, possible, pr, conf['PR_Target']])
        print(f"✅ {p_key}: {energy} kWh | Weather: {weather}")

    # 5. Zabezpieczenie przed zapisem pustych danych
    if total_energy_collected > 0:
        raw_sheet.append_rows(final_rows)
        print(f"💾 Sukces! Dane za {yesterday} zostały zapisane w arkuszu.")
    else:
        print("❌ BŁĄD: Suma energii dla wszystkich stacji wynosi 0. Zapis przerwany, aby chronić bazę danych.")

if __name__ == "__main__":
    main()
