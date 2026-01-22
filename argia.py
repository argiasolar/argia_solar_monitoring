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

# --- MODUŁ HUAWEI (Twoja logika + Sesja) ---
def get_huawei_connection():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Content-Type': 'application/json'
    })
    
    login_url = f"{HUAWEI_BASE_URL}/thirdData/login"
    payload = {
        "userName": os.environ["HUAWEI_USERNAME"],
        "systemCode": os.environ["HUAWEI_PASSWORD"]
    }
    
    try:
        print(f"📡 Logowanie Huawei LA5...")
        r = session.post(login_url, json=payload, timeout=30)
        # Jeśli nie ma 200, wypisujemy co serwer widzi
        if r.status_code != 200:
            print(f"❌ Błąd HTTP {r.status_code}: {r.text[:200]}")
            return None, None
            
        data = r.json()
        token = data.get("data", {}).get("xsrfToken")
        
        if token:
            print("✅ Huawei: Zalogowano.")
            return session, token
        else:
            print(f"⚠️ Huawei: Brak tokena. Odp: {data}")
            return None, None
    except Exception as e:
        print(f"❌ Huawei Connection Error: {e}")
        return None, None

def fetch_huawei_energy(session, token, station_code, date_str):
    url = f"{HUAWEI_BASE_URL}/thirdData/getStationKpi"
    headers = {"xsrf-token": token}
    payload = {"stationCodes": station_code, "collectTime": date_str}
    
    try:
        r = session.post(url, json=payload, headers=headers, timeout=30)
        data = r.json().get("data")
        if data and len(data) > 0:
            return round(data[0].get("dayPower", 0), 2)
        return 0
    except Exception as e:
        print(f"❌ Huawei Data Error: {e}")
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
    yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Inicjalizacja Huawei
    h_session, h_token = get_huawei_connection()

    for p in plants:
        brand = str(p['Brand']).upper()
        s_id = str(p['SiteID']).strip()
        p_key = p['Plantkey']
        
        print(f"\n--- Przetwarzam: {p_key} ({brand}) ---")
        
        real_energy = 0
        if brand == "GROWATT":
            real_energy = fetch_growatt_data(s_id, yesterday_str)
        elif brand == "HUAWEI":
            if h_session and h_token:
                real_energy = fetch_huawei_energy(h_session, h_token, s_id, yesterday_str)
            else:
                print(f"⚠️ Pomijam {p_key} - brak sesji.")
        
        irrad = get_weather_data(p['Latitude'], p['Longtitude'], yesterday_str)
        kwp = float(p['kWp_DC'] or 0)
        possible = round(kwp * irrad * 0.85, 2)
        pr = round(real_energy / (kwp * irrad), 3) if (irrad > 0 and kwp > 0) else 0
            
        row = [yesterday_str, p_key, p['CustomerName'], real_energy, irrad, possible, pr, p['PR_Target']]
        
        try:
            raw_data_sheet.append_row(row)
            print(f"✅ Wynik: {real_energy} kWh | Pogoda: {irrad}")
        except Exception as e:
            print(f"❌ Błąd zapisu: {e}")

    print(f"\n✅ Synchronizacja zakończona.")

if __name__ == "__main__":
    main()
