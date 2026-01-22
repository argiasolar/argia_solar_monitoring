import os
import json
import gspread
import requests
import time
from google.oauth2.service_account import Credentials

def get_weather_and_cloud(lat, lon, date_str):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": date_str, "end_date": date_str,
        "daily": ["shortwave_radiation_sum", "cloud_cover_mean"],
        "timezone": "auto"
    }
    try:
        res = requests.get(url, params=params, timeout=20)
        res.raise_for_status()
        d = res.json()['daily']
        irr = round(d['shortwave_radiation_sum'][0] / 3.6, 3)
        cloud = d['cloud_cover_mean'][0]
        return irr, cloud
    except:
        return None, None

def main():
    print("🛠️  Starting final data repair and recalculation...")
    
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ['GOOGLE_SHEET_ID'])
    config_sheet = sh.worksheet("Config_Plants")
    raw_sheet = sh.worksheet("RawData")
    
    # Pobieramy konfigurację (potrzebujemy kWp_DC)
    plants_conf = {p['Plantkey']: p for p in config_sheet.get_all_records()}
    data = raw_sheet.get_all_values()
    rows = data[1:]

    for i, row in enumerate(rows):
        row_num = i + 2
        date_str = row[0]
        p_key = row[1]
        energy = float(row[3].replace(',', '') or 0)
        irr = float(row[4].replace(',', '') or 0)
        
        # Sprawdzamy czy wiersz wymaga naprawy (brak pogody lub brak chmur w kolumnie I)
        has_cloud = len(row) > 8 and row[8] != ""
        
        if irr == 0 or not has_cloud:
            conf = plants_conf.get(p_key)
            if not conf: continue

            print(f"🔄 Fixing {p_key} for {date_str}...")
            new_irr, new_cloud = get_weather_and_cloud(conf['Latitude'], conf['Longtitude'], date_str)
            
            # Interpolacja jeśli nadal 0
            if new_irr is None or new_irr == 0:
                neighbors = ["SLP1", "GTO1", "MEX1"]
                i_vals, c_vals = [], []
                for n_key in neighbors:
                    if n_key == p_key or n_key not in plants_conf: continue
                    n_c = plants_conf[n_key]
                    ni, nc = get_weather_and_cloud(n_c['Latitude'], n_c['Longtitude'], date_str)
                    if ni:
                        i_vals.append(ni)
                        c_vals.append(nc)
                new_irr = round(sum(i_vals) / len(i_vals), 3) if i_vals else 0
                new_cloud = round(sum(c_vals) / len(c_vals), 1) if c_vals else 0

            # KLUCZOWE: Ponowne wyliczenie parametrów
            kwp = float(conf['kWp_DC'] or 0)
            possible = round(kwp * new_irr * 0.85, 2)
            pr = round(energy / (kwp * new_irr), 3) if (new_irr > 0 and kwp > 0) else 0

            # Aktualizacja kolumn E, F, G (Weather, Possible, PR) oraz I (Cloud)
            raw_sheet.update(range_name=f'E{row_num}:G{row_num}', values=[[new_irr, possible, pr]])
            raw_sheet.update(range_name=f'I{row_num}', values=[[new_cloud]])
            
            print(f"  ✅ Done: Irr={new_irr}, Cloud={new_cloud}%")
            time.sleep(0.5)

    print("\n🏆 Database is now consistent and fully populated!")

if __name__ == "__main__":
    main()
