import os
import json
import gspread
import requests
import datetime
import time
from google.oauth2.service_account import Credentials

# --- TWOJE DANE PRODUKCYJNE WBUDOWANE W SKRYPT ---
# Dane za okres 01.01.2026 - 20.01.2026
RAW_PROD_DATA = {
    "SLP1": [609, 76, 608, 627, 604, 520, 602, 625, 588, 204, 291, 528, 380, 575, 605, 560, 762, 343, 601, 287],
    "SLP2": [986, 1012, 999, 1033, 982, 868, 988, 1040, 987, 305, 638, 822, 683, 921, 1000, 929, 494, 678, 1023, 505],
    "GTO1": [2259, 2375, 2281, 2367, 2017, 2285, 2304, 2344, 2017, 1836, 1642, 2283, 1402, 1696, 2165, 1841, 1476, 2049, 1984, 920],
    "MEX1": [2174, 2253, 2188, 2247, 2154, 1655, 1953, 2304, 1794, 1496, 878, 784, 747, 670, 737, 1718, 1855, 1908, 2435, 1943],
    "NL1": [2463, 2647, 2778, 2744, 2617, 2501, 2481, 1722, 1573, 875, 497, 1486, 1955, 2963, 2934, 2324, 2293, 3120, 2804, 703],
    "MEX2": [2448, 2542, 2476, 2527, 2449, 2436, 2445, 2345, 2362, 2207, 2241, 2326, 2215, 1961, 1672, 1758, 1693, 1819, 2143, 1999]
}

def get_weather_data(lat, lon, date_str):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": date_str, "end_date": date_str,
        "daily": "shortwave_radiation_sum", "timezone": "auto"
    }
    for attempt in range(3):
        try:
            res = requests.get(url, params=params, timeout=30)
            res.raise_for_status()
            return round(res.json()['daily']['shortwave_radiation_sum'][0] / 3.6, 3)
        except:
            time.sleep(2)
    return 0

def main():
    print("⏳ Start: Backfill z wbudowanych danych (Brak plików zewnętrznych)...")
    
    # 1. Połączenie
    try:
        creds_json = os.environ.get('GOOGLE_CREDENTIALS')
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(os.environ.get('GOOGLE_SHEET_ID'))
        config_sheet = sh.worksheet("Config_Plants")
        raw_data_sheet = sh.worksheet("RawData")
    except Exception as e:
        print(f"🚨 Błąd GSheets: {e}"); return

    # 2. Konfiguracja
    plants_config = {p['Plantkey']: p for p in config_sheet.get_all_records()}
    
    final_rows = []
    
    # 3. Pętla po instalacjach
    for p_key, energy_list in RAW_PROD_DATA.items():
        if p_key not in plants_config:
            print(f"⚠️ Pominąłem {p_key} - brak w arkuszu Config_Plants")
            continue
            
        conf = plants_config[p_key]
        print(f"--- Przetwarzam historię dla: {p_key} ---")
        
        # Pętla po dniach (od 1 do 20 stycznia)
        for i, energy in enumerate(energy_list):
            day_val = i + 1
            date_str = f"2026-01-{day_val:02d}"
            
            # Pogoda
            irrad = get_weather_data(conf['Latitude'], conf['Longtitude'], date_str)
            
            # KPI
            kwp = float(conf['kWp_DC'] or 0)
            possible = round(kwp * irrad * 0.85, 2)
            pr = round(energy / (kwp * irrad), 3) if (irrad > 0 and kwp > 0) else 0
            
            final_rows.append([
                date_str, p_key, conf['CustomerName'], energy, irrad, possible, pr, conf['PR_Target']
            ])
            time.sleep(0.3) # Szybki throttle dla Open-Meteo

    # 4. Zapis
    if final_rows:
        raw_data_sheet.append_rows(final_rows)
        print(f"✅ Sukces! Dodano {len(final_rows)} wierszy historycznych.")

if __name__ == "__main__":
    main()
