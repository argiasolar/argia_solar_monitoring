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
    print("🛠️ Start: Uzupełnianie danych historycznych (Meteo + Cloud Cover)...")
    
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

    # Upewniamy się, że kolumna I istnieje w nagłówku, jeśli nie, musisz ją dodać ręcznie w Sheet
    print(f"Znaleziono {len(rows)} wierszy do sprawdzenia.")

    for i, row in enumerate(rows):
        row_num = i + 2
        date_str = row[0]
        p_key = row[1]
        
        # Sprawdzamy czy mamy już dane o chmurach (kolumna I ma indeks 8)
        # Oraz czy pogoda (kolumna E, indeks 4) nie jest zerem
        current_weather = float(row[4].replace(',', '') or 0)
        current_cloud = row[8] if len(row) > 8 else ""

        if current_cloud == "" or current_weather == 0:
            conf = plants_conf.get(p_key)
            if not conf: continue

            print(f"🔄 Pobieranie danych dla {p_key} na dzień {date_str}...")
            irr, cloud = get_weather_and_cloud(conf['Latitude'], conf['Longtitude'], date_str)
            
            # Jeśli główna stacja zawiedzie, używamy interpolacji (średnia z sąsiadów)
            if irr is None or irr == 0:
                print(f"  ⚠ Brak danych dla {p_key}, próbuję interpolacji...")
                neighbors = ["SLP1", "GTO1", "MEX1"]
                i_vals, c_vals = [], []
                for n_key in neighbors:
                    if n_key == p_key: continue
                    n_conf = plants_conf[n_key]
                    n_irr, n_cloud = get_weather_and_cloud(n_conf['Latitude'], n_conf['Longtitude'], date_str)
                    if n_irr:
                        i_vals.append(n_irr)
                        c_vals.append(n_cloud)
                
                irr = round(sum(i_vals) / len(i_vals), 3) if i_vals else 0
                cloud = round(sum(c_vals) / len(c_vals), 1) if c_vals else 0

            # Przeliczamy możliwe i PR na podstawie (być może nowej) pogody
            energy = float(row[3].replace(',', '') or 0)
            kwp = float(conf['kWp_DC'] or 0)
            possible = round(kwp * irr * 0.85, 2)
            pr = round(energy / (kwp * irr), 3) if (irr > 0 and kwp > 0) else 0

            # Aktualizacja wiersza (Kolumny E, F, G oraz I)
            # E: Pogoda, F: Possible, G: PR, I: Cloud Cover
            raw_sheet.update(range_name=f'E{row_num}:G{row_num}', values=[[irr, possible, pr]])
            raw_sheet.update(range_name=f'I{row_num}', values=[[cloud]])
            
            print(f"  ✅ Zaktualizowano: Irr={irr}, Cloud={cloud}%")
            time.sleep(1) # Unikamy limitów API

    print("\n🏆 Baza danych historycznych została uzupełniona!")

if __name__ == "__main__":
    main()
