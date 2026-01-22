import os
import json
import gspread
import requests
import datetime
from google.oauth2.service_account import Credentials
import growattServer

# --- MODUŁ POGODOWY ---
def get_weather_data(lat, lon, date_str):
    try:
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat, "longitude": lon,
            "start_date": date_str, "end_date": date_str,
            "daily": "shortwave_radiation_sum", "timezone": "auto"
        }
        res = requests.get(url, params=params, timeout=15).json()
        mj_m2 = res['daily']['shortwave_radiation_sum'][0]
        return round(mj_m2 / 3.6, 3)
    except Exception as e:
        print(f"⚠️ Błąd pogodowy dla {lat},{lon}: {e}")
        return 0

# --- MODUŁ GROWATT (Z maskowaniem User-Agent) ---
def fetch_growatt_data_v2(plant_id, date_str):
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    
    if not user or not password:
        print("❌ BŁĄD: Brak danych logowania Growatt!")
        return 0

    # Inicjalizacja API
    api = growattServer.GrowattApi()
    api.server_url = 'http://server.growatt.com/'
    
    # MASKOWANIE: Zmieniamy nagłówki w sesji biblioteki, aby udawać przeglądarkę
    api.session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Referer': 'http://server.growatt.com/login'
    })

    try:
        # Próba logowania
        login_response = api.login(user, password)
        
        # Pobranie danych dnia wczorajszego
        data = api.plant_detail_info(plant_id, timespan=3, date=date_str)
        
        # Ekstrakcja energii z priorytetem dla 'daily_energy'
        energy = data.get('daily_energy') or data.get('energy') or 0
        return float(energy)
    except Exception as e:
        print(f"❌ Growatt Error (ID: {plant_id}): {e}")
        # Jeśli nadal mamy 403, spróbujmy wypisać nagłówki sesji dla debugowania
        return 0

# --- MODUŁ HUAWEI (Placeholder) ---
def fetch_huawei_data(p_key):
    print(f"ℹ️ Huawei API (ID: {p_key}) - w trakcie budowy.")
    return 0

# --- GŁÓWNA LOGIKA ---
def main():
    print(f"🚀 Start Argia Solar Metering - {datetime.datetime.now()}")
    
    try:
        # Autoryzacja Google
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
    yesterday_dt = datetime.date.today() - datetime.timedelta(days=1)
    yesterday_str = yesterday_dt.isoformat()
    
    print(f"📅 Pobieranie danych za dzień: {yesterday_str}")

    for p in plants:
        plant_key = p['Plantkey']
        brand = str(p['Brand']).upper()
        s_id = str(p['SiteID']).strip()
        
        print(f"\n--- Przetwarzam: {plant_key} ({brand}) ---")
        
        real_energy = 0
        if brand == "GROWATT":
            real_energy = fetch_growatt_data_v2(s_id, yesterday_str)
        elif brand == "HUAWEI":
            real_energy = fetch_huawei_data(plant_key)
        
        irradiance = get_weather_data(p['Latitude'], p['Longtitude'], yesterday_str)
        kwp_dc = float(p['kWp_DC'] or 0)
        
        possible_gen = round(kwp_dc * irradiance * 0.85, 2)
        real_pr = round(real_energy / (kwp_dc * irradiance), 3) if (irradiance > 0 and kwp_dc > 0) else 0
            
        row_to_save = [
            yesterday_str, plant_key, p['CustomerName'], 
            real_energy, irradiance, possible_gen, real_pr, p['PR_Target']
        ]
        
        try:
            raw_data_sheet.append_row(row_to_save)
            print(f"✅ Wynik: {real_energy} kWh. Zapisano dla {plant_key}")
        except Exception as e:
            print(f"❌ Błąd zapisu: {e}")

    print(f"\n✅ Zakończono synchronizację.")

if __name__ == "__main__":
    main()
