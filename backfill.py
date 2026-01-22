import os
import json
import gspread
import requests
import datetime
import time
from google.oauth2.service_account import Credentials

# --- KONFIGURACJA ---
HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"
START_DATE = "2026-01-01"
END_DATE = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

# --- FUNKCJE POMOCNICZE ---
def get_weather_data(lat, lon, date_str):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": date_str, "end_date": date_str,
        "daily": "shortwave_radiation_sum", "timezone": "auto"
    }
    try:
        res = requests.get(url, params=params, timeout=30)
        data = res.json()
        return round(data['daily']['shortwave_radiation_sum'][0] / 3.6, 3)
    except:
        return 0

# --- MODUŁ HUAWEI (Historyczny) ---
def get_huawei_history_data(date_str):
    try:
        r_log = requests.post(f"{HUAWEI_BASE_URL}/login", 
                             json={"userName": os.environ['HUAWEI_USERNAME'], 
                                   "systemCode": os.environ['HUAWEI_PASSWORD']})
        token = r_log.headers.get('Xsrf-Token') or r_log.headers.get('XSRF-TOKEN')
        headers = {'XSRF-TOKEN': token, 'Content-Type': 'application/json'}
        
        r_list = requests.post(f"{HUAWEI_BASE_URL}/getStationList", headers=headers, json={})
        stations = r_list.json().get('data', [])
        codes = [s['stationCode'] for s in stations]
        
        payload = {
            "stationCodes": ",".join(codes),
            "collectTime": date_str,
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
        print(f"❌ Huawei History Error: {e}")
        return {}

def main():
    print(f"⏳ Backfill Huawei + Meteo ({START_DATE} -> {END_DATE})")
    
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ['GOOGLE_SHEET_ID'])
    config_sheet = sh.worksheet("Config_Plants")
    raw_data_sheet = sh.worksheet("RawData")
    
    plants_config = config_sheet.get_all_records()
    
    start_dt = datetime.datetime.strptime(START_DATE, '%Y-%m-%d')
    end_dt = datetime.datetime.strptime(END_DATE, '%Y-%m-%d')
    current_dt = start_dt

    while current_dt <= end_dt:
        current_str = current_dt.strftime('%Y-%m-%d')
        print(f"\n📅 Dzień: {current_str}")
        
        daily_rows = []
        huawei_map = get_huawei_history_data(current_str)
        
        for p in plants_config:
            brand = str(p['Brand']).upper()
            s_id = str(p['SiteID']).strip()
            
            energy = 0
            if brand == "HUAWEI":
                energy = huawei_map.get(s_id, 0)
            elif brand == "GROWATT":
                print(f"  - {p['Plantkey']}: Pomijam (Growatt blokuje GitHub)")
                energy = 0 # Wstawiamy 0, uzupełnisz to później ręcznie
            
            irrad = get_weather_data(p['Latitude'], p['Longtitude'], current_str)
            kwp = float(p['kWp_DC'] or 0)
            possible = round(kwp * irrad * 0.85, 2)
            pr = round(energy / (kwp * irrad), 3) if (irrad > 0 and kwp > 0) else 0
            
            daily_rows.append([current_str, p['Plantkey'], p['CustomerName'], energy, irrad, possible, pr, p['PR_Target']])
        
        if daily_rows:
            raw_data_sheet.append_rows(daily_rows)
            print(f"✅ Zapisano: {current_str}")
        
        current_dt += datetime.timedelta(days=1)
        time.sleep(1)

    print("\n🏆 Backfill Huawei + Meteo zakończony!")

if __name__ == "__main__":
    main()
