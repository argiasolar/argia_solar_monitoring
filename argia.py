import os
import json
import gspread
import requests
import datetime
from google.oauth2.service_account import Credentials

def get_weather_data(lat, lon, date_str):
    """Pobiera irradiancję (kWh/m2) dla lokalizacji"""
    try:
        url = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date={date_str}&end_date={date_str}&daily=shortwave_radiation_sum&timezone=auto"
        res = requests.get(url).json()
        mj_m2 = res['daily']['shortwave_radiation_sum'][0]
        return round(mj_m2 / 3.6, 3) # Konwersja MJ na kWh
    except:
        return 0

def main():
    # 1. Połączenie z Google
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    
    sh = gc.open_by_key(os.environ['GOOGLE_SHEET_ID'])
    config_sheet = sh.worksheet("Config_Plants")
    raw_data_sheet = sh.worksheet("RawData")
    
    plants = config_sheet.get_all_records()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    
    for p in plants:
        print(f"Przetwarzam: {p['Plantkey']}")
        
        # Pobieranie pogody
        irradiance = get_weather_data(p['Latitude'], p['Longtitude'], yesterday)
        
        # --- MIEJSCE NA API FALOWNIKA ---
        # Tu w kolejnym kroku wstawimy realne pobieranie
        real_energy = 100.0 # Placeholder
        # --------------------------------
        
        # Obliczenia KPI
        kwp_dc = float(p['kWp_DC'])
        possible_gen = round(kwp_dc * irradiance * 0.85, 2)
        real_pr = round(real_energy / (kwp_dc * irradiance), 3) if irradiance > 0 else 0
        
        # Zapis do arkusza RawData
        row = [
            yesterday, 
            p['Plantkey'], 
            p['CustomerName'], 
            real_energy, 
            irradiance, 
            possible_gen, 
            real_pr,
            p['PR_Target']
        ]
        raw_data_sheet.append_row(row)

if __name__ == "__main__":
    main()
