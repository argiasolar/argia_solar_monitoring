import os
import json
import datetime
import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build

import argia_weather as weather
import argia_huawei as huawei
import argia_growatt as growatt

# Pobieranie ID arkusza z sekretów
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def get_service():
    """Autoryzacja Google Sheets API."""
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json), 
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build('sheets', 'v4', credentials=creds)

def main():
    print("--- 🌟 ARGIA SOLAR MONITORING v4.5 (Pancerne Logowanie) ---")
    service = get_service()
    
    # 1. Ustalenie daty (wczoraj)
    yesterday_dt = datetime.datetime.now() - datetime.timedelta(days=1)
    date_iso = yesterday_dt.strftime('%Y-%m-%d')     # Dla API (2026-01-22)
    date_slash = yesterday_dt.strftime('%-m/%-d/%Y') # Dla Arkusza (1/22/2026)

    # 2. Pobranie konfiguracji stacji z Config_Plants
    try:
        config_res = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range="Config_Plants!A2:L25"
        ).execute()
        config_rows = config_res.get('values', [])
    except Exception as e:
        print(f"❌ Błąd pobierania konfiguracji: {e}")
        return

    plants_config = {}
    huawei_map = {}  # {SiteID: PlantKey}
    growatt_map = {} # {SiteID: PlantKey}

    for row in config_rows:
        if len(row) >= 8:
            p_key = row[0]   # np. MEX1
            brand = row[1].upper()
            s_id = str(row[6]).strip() # Zakładamy, że SiteID/StationCode jest w kolumnie G (indeks 6)
            
            plants_config[p_key] = {
                'brand': brand,
                'site_id': s_id,
                'kwp': float(row[2].replace(',', '.')),
                'target': float(row[7].replace(',', '.')),
                'name': row[8] if len(row) > 8 else p_key,
                'lat': float(row[10].replace(',', '.')) if len(row) > 10 else 0,
                'lon': float(row[11].replace(',', '.')) if len(row) > 11 else 0
            }

            if brand == "HUAWEI":
                huawei_map[s_id] = p_key
            elif brand == "GROWATT":
                growatt_map[s_id] = p_key

    # 3. Pobieranie produkcji z "pancernych" modułów
    all_prod = {}
    
    if huawei_map:
        # Przekazujemy date_iso (YYYY-MM-DD) i mapę {SiteID: MEX1}
        all_prod.update(huawei.fetch_huawei_data(date_iso, huawei_map))
        
    if growatt_map:
        # Growatt często woli ten sam format daty
        all_prod.update(growatt.fetch_growatt_data(date_iso, growatt_map))

    # 4. Składanie danych końcowych (Pogoda + PR)
    final_data = []
    total_energy = 0

    for p_key, energy in all_prod.items():
        if p_key in plants_config:
            conf = plants_config[p_key]
            
            # Pobieranie pogody (z Twoją logiką interpolacji)
            irr, clouds = weather.get_estimated_weather(p_key) 
            # Uwaga: Jeśli masz smart_weather w argia_weather.py, upewnij się, że nazwa funkcji się zgadza
            
            # Obliczenia PR i Forecast
            forecast = round(conf['kwp'] * irr * conf['target'], 2)
            pr = round(energy / (conf['kwp'] * irr), 3) if (irr > 0 and conf['kwp'] > 0) else 0
            
            final_data.append([
                date_slash,      # A: Date
                p_key,           # B: PlantKey
                conf['name'],    # C: CustomerName
                energy,          # D: Production (kWh)
                irr,             # E: Irradiance
                forecast,        # F: Possible/Forecast
                pr,              # G: Real PR
                conf['target'],  # H: PR Target
                clouds           # I: Clouds/Weather Info
            ])
            total_energy += energy

    # 5. Zapis do Google Sheets (Tylko jeśli cokolwiek pobrano)
    if total_energy > 0:
        body = {'values': final_data}
        try:
            service.spreadsheets().values().append(
                spreadsheetId=SHEET_ID, 
                range="RawData!A2",
                valueInputOption="USER_ENTERED", 
                body=body
            ).execute()
            print(f"✅ Sukces! Zapisano {len(final_data)} wierszy za dzień {date_slash}.")
        except Exception as e:
            print(f"❌ Błąd zapisu do Sheets: {e}")
    else:
        print("⚠️ Uwaga: Suma energii wynosi 0. Nic nie zapisano w RawData (Blokada API lub brak produkcji).")

if __name__ == "__main__":
    main()
