import os
import json
import gspread
import requests
import time
from google.oauth2.service_account import Credentials

def get_weather_data(lat, lon, date_str):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": date_str, "end_date": date_str,
        "daily": "shortwave_radiation_sum", "timezone": "auto"
    }
    try:
        res = requests.get(url, params=params, timeout=20)
        res.raise_for_status()
        data = res.json()
        return round(data['daily']['shortwave_radiation_sum'][0] / 3.6, 3)
    except:
        return None # Zwracamy None, żeby odróżnić od błędu

def main():
    print("🛠️ Start: Naprawa brakujących danych pogodowych...")
    
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ['GOOGLE_SHEET_ID'])
    config_sheet = sh.worksheet("Config_Plants")
    raw_sheet = sh.worksheet("RawData")
    
    plants_conf = {p['Plantkey']: p for p in config_sheet.get_all_records()}
    data = raw_sheet.get_all_values()
    headers = data[0]
    rows = data[1:]

    updates = []
    
    for i, row in enumerate(rows):
        # i + 2, bo GSheets liczy od 1 i mamy nagłówek
        row_num = i + 2 
        
        date_str = row[0]
        p_key = row[1]
        energy = float(row[3].replace(',', '') or 0)
        weather = float(row[4].replace(',', '') or 0)
        
        # Warunek: Jest energia, ale brakuje pogody
        if energy > 0 and weather == 0:
            if p_key in plants_conf:
                conf = plants_conf[p_key]
                print(f"Refetching weather for {p_key} on {date_str}...")
                
                new_weather = get_weather_data(conf['Latitude'], conf['Longtitude'], date_str)
                
                if new_weather is not None and new_weather > 0:
                    kwp = float(conf['kWp_DC'] or 0)
                    possible = round(kwp * new_weather * 0.85, 2)
                    pr = round(energy / (kwp * new_weather), 3) if kwp > 0 else 0
                    
                    # Przygotowujemy komórki do aktualizacji (Kolumny E, F, G)
                    # Zakładamy: E=Pogoda, F=Possible, G=PR
                    raw_sheet.update(f'E{row_num}:G{row_num}', [[new_weather, possible, pr]])
                    print(f"  ✅ Naprawiono: {new_weather} kWh/m2")
                    time.sleep(1) # Bezpieczeństwo przed timeoutem
                else:
                    print(f"  ⚠️ Nadal brak danych dla {date_str}")

    print("\n🏆 Naprawa zakończona!")

if __name__ == "__main__":
    main()
