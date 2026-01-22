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
        return 0

def main():
    print("🛠️ Start: Inteligentna naprawa meteo (Metoda Średniej Lokalizacji)...")
    
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ['GOOGLE_SHEET_ID'])
    config_sheet = sh.worksheet("Config_Plants")
    raw_sheet = sh.worksheet("RawData")
    
    plants_conf = {p['Plantkey']: p for p in config_sheet.get_all_records()}
    data = raw_sheet.get_all_values()
    rows = data[1:]

    # Definiujemy trójkąt lokalizacji do uśredniania
    location_triangle = ["SLP1", "SLP2", "GTO1", "MEX1", "MEX2"]

    for i, row in enumerate(rows):
        row_num = i + 2
        date_str, p_key = row[0], row[1]
        energy = float(row[3].replace(',', '') or 0)
        weather = float(row[4].replace(',', '') or 0)

        if energy > 0 and weather == 0 and p_key in location_triangle:
            print(f"🔍 Brak danych dla {p_key} ({date_str}). Szukam danych w sąsiednich lokalizacjach...")
            
            neighbor_values = []
            # Szukamy danych u sąsiadów dla tej samej daty
            for neighbor_key in ["SLP1", "GTO1", "MEX1"]:
                if neighbor_key == p_key: continue
                
                n_conf = plants_conf[neighbor_key]
                val = get_weather_data(n_conf['Latitude'], n_conf['Longtitude'], date_str)
                if val > 0:
                    neighbor_values.append(val)
                    print(f"  - Pobrano dane z {neighbor_key}: {val}")
                time.sleep(0.5)

            if neighbor_values:
                avg_weather = round(sum(neighbor_values) / len(neighbor_values), 3)
                kwp = float(plants_conf[p_key]['kWp_DC'] or 0)
                possible = round(kwp * avg_weather * 0.85, 2)
                pr = round(energy / (kwp * avg_weather), 3) if kwp > 0 else 0
                
                raw_sheet.update(range_name=f'E{row_num}:G{row_num}', values=[[avg_weather, possible, pr]])
                print(f"  ✅ Naprawiono {p_key} średnią: {avg_weather} kWh/m2 (na podstawie {len(neighbor_values)} sąsiadów)")
            else:
                print(f"  ❌ Nie udało się znaleźć danych u żadnego sąsiada dla {date_str}")

    print("\n🏆 Inteligentna naprawa zakończona!")

if __name__ == "__main__":
    main()
