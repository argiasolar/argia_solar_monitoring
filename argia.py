import os
import json
import gspread
import requests
import datetime
from google.oauth2.service_account import Credentials

# --- MODUŁ POGODOWY ---
def get_weather_data(lat, lon, date_str):
    """
    Pobiera irradiancję (kWh/m2) dla lokalizacji z Open-Meteo.
    """
    try:
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": date_str,
            "end_date": date_str,
            "daily": "shortwave_radiation_sum",
            "timezone": "auto"
        }
        res = requests.get(url, params=params, timeout=15).json()
        mj_m2 = res['daily']['shortwave_radiation_sum'][0]
        # Konwersja z MJ/m2 na kWh/m2 (1 kWh = 3.6 MJ)
        return round(mj_m2 / 3.6, 3)
    except Exception as e:
        print(f"⚠️ Błąd pogodowy dla {lat},{lon}: {e}")
        return 0

# --- MODUŁ GROWATT ---
def fetch_growatt_data(plant_id, api_token, date_str):
    """
    Pobiera dzienną produkcję z Growatt ShineServer (v1 API).
    """
    url = "http://server.growatt.com/v1/plant/energy"
    params = {
        "plant_id": plant_id,
        "token": api_token,
        "date": date_str
    }
    try:
        response = requests.get(url, params=params, timeout=20)
        data = response.json()
        
        if data.get("error_code") == 0:
            # API zwraca dane w polu "data" lub bezpośrednio w strukturze zależnie od wersji
            energy = data.get("data", {}).get("energy", 0)
            return float(energy)
        else:
            print(f"⚠️ Growatt API Error (ID: {plant_id}): {data.get('error_msg')}")
            return 0
    except Exception as e:
        print(f"❌ Growatt Connection Error (ID: {plant_id}): {e}")
        return 0

# --- MODUŁ HUAWEI (Placeholder) ---
def fetch_huawei_data(plant_config, date_str):
    """
    Tu w przyszłości dodamy logikę FusionSolar.
    Na razie zwraca 0, aby skrypt działał dalej.
    """
    print(f"ℹ️ Huawei API (ID: {plant_config['Plantkey']}) - moduł w trakcie budowy.")
    return 0

# --- GŁÓWNA LOGIKA APLIKACJI ---
def main():
    print(f"🚀 Start Argia Solar Metering - {datetime.datetime.now()}")
    
    # 1. Autoryzacja Google Sheets
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds_json = os.environ.get('GOOGLE_CREDENTIALS')
        if not creds_json:
            raise ValueError("Brak secretu GOOGLE_CREDENTIALS")
            
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        
        sheet_id = os.environ.get('GOOGLE_SHEET_ID')
        sh = gc.open_by_key(sheet_id)
        
        config_sheet = sh.worksheet("Config_Plants")
        raw_data_sheet = sh.worksheet("RawData")
    except Exception as e:
        print(f"🚨 Krytyczny błąd Google Sheets: {e}")
        return

    # 2. Pobranie konfiguracji i daty (wczoraj)
    plants = config_sheet.get_all_records()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    
    print(f"📅 Pobieranie danych za dzień: {yesterday}")

    for p in plants:
        plant_key = p['Plantkey']
        brand = str(p['Brand']).upper()
        
        print(f"\n--- Przetwarzam: {plant_key} ---")
        
        # 3. Pobranie energii z inwerterów
        real_energy = 0
        if brand == "GROWATT":
            token = os.environ.get('GROWATT_API_TOKEN')
            real_energy = fetch_growatt_data(p['SiteID'], token, yesterday)
        elif brand == "HUAWEI":
            real_energy = fetch_huawei_data(p, yesterday)
        
        # 4. Pobranie nasłonecznienia
        irradiance = get_weather_data(p['Latitude'], p['Longtitude'], yesterday)
        
        # 5. Obliczenia KPI
        kwp_dc = float(p['kWp_DC'])
        # Teoretyczna produkcja (uwzględniając 15% strat systemowych)
        possible_gen = round(kwp_dc * irradiance * 0.85, 2)
        
        # Realny Performance Ratio (PR)
        real_pr = 0
        if irradiance > 0 and kwp_dc > 0:
            real_pr = round(real_energy / (kwp_dc * irradiance), 3)
            
        # 6. Zapis do arkusza RawData
        # Kolejność: Data, Klucz, Klient, Real_kWh, Nasłonecznienie, Możliwa_kWh, Real_PR, Target_PR
        row_to_save = [
            yesterday, 
            plant_key, 
            p['CustomerName'], 
            real_energy, 
            irradiance, 
            possible_gen, 
            real_pr,
            p['PR_Target']
        ]
        
        try:
            raw_data_sheet.append_row(row_to_save)
            print(f"✅ Zapisano pomyślnie dla {plant_key}")
        except Exception as e:
            print(f"❌ Błąd zapisu wiersza dla {plant_key}: {e}")

    print(f"\n✅ Zakończono proces synchronizacji: {datetime.datetime.now()}")

if __name__ == "__main__":
    main()
