import os
import json
import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

import argia_weather as weather
import argia_huawei as huawei
import argia_growatt as growatt

SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def get_service():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json), 
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build('sheets', 'v4', credentials=creds)

def safe_float(value, default=0.0):
    if value is None: return default
    try:
        # Usuwamy spacje i zamieniamy przecinki na kropki
        clean_val = str(value).strip().replace(',', '.')
        return float(clean_val)
    except (ValueError, TypeError):
        return default

def main():
    print("--- 🌟 ARGIA SOLAR MONITORING v4.6 (Mapowanie pod Config) ---")
    service = get_service()
    
    yesterday_dt = datetime.datetime.now() - datetime.timedelta(days=1)
    date_iso = yesterday_dt.strftime('%Y-%m-%d')
    date_slash = yesterday_dt.strftime('%-m/%-d/%Y')

    try:
        # Pobieramy szerszy zakres na wypadek nowych kolumn
        config_res = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range="Config_Plants!A2:O25"
        ).execute()
        config_rows = config_res.get('values', [])
    except Exception as e:
        print(f"❌ Błąd pobierania konfiguracji: {e}")
        return

    plants_config = {}
    huawei_map = {}  # {SiteID: PlantKey}
    growatt_map = {} # {SiteID: PlantKey}

    for row in config_rows:
        if len(row) >= 10: # Musimy mieć przynajmniej SiteID
            p_key = str(row[0]).strip()
            brand = str(row[1]).strip().upper()
            
            # Mapowanie wg Twojej struktury:
            kwp = safe_float(row[2])
            lat = safe_float(row[4])
            lon = safe_float(row[5])
            target = safe_float(row[7])
            name = str(row[8]).strip()
            s_id = str(row[9]).strip() # SiteID jest w 10. kolumnie (index 9)
            
            plants_config[p_key] = {
                'brand': brand,
                'site_id': s_id,
                'kwp': kwp,
                'target': target,
                'name': name,
                'lat': lat,
                'lon': lon
            }

            if brand == "HUAWEI":
                huawei_map[s_id] = p_key
            elif brand == "GROWATT":
                growatt_map[s_id] = p_key

    all_prod = {}
    # Pobieranie danych z modułów
    if huawei_map:
        all_prod.update(huawei.fetch_huawei_data(date_iso, huawei_map))
    if growatt_map:
        all_prod.update(growatt.fetch_growatt_data(date_iso, growatt_map))

    final_data = []
    total_energy = 0

    # Przetwarzanie wyników
    for s_id, energy in all_prod.items():
        # Szukamy klucza (np. SLP1) na podstawie SiteID
        p_key = next((k for k, v in plants_config.items() if v['site_id'] == s_id), None)
        
        if p_key and p_key in plants_config:
            conf = plants_config[p_key]
            
            # Pogoda
            irr, clouds = weather.get_estimated_weather(p_key) 
            
            forecast = round(conf['kwp'] * irr * 0.8, 2) # Używamy 0.8 jako ExpectedFactor
            pr = round(energy / (conf['kwp'] * irr), 3) if (irr > 0 and conf['kwp'] > 0) else 0
            
            final_data.append([
                date_slash, p_key, conf['name'], energy, irr, forecast, pr, conf['target'], clouds
            ])
            total_energy += energy

    # Zapis do Google Sheets
    if final_data:
        body = {'values': final_data}
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range="RawData!A2",
            valueInputOption="USER_ENTERED", body=body
        ).execute()
        print(f"✅ Sukces! Zapisano {len(final_data)} wierszy.")
    else:
        print("⚠️ Brak danych do zapisu (API zwróciły 0 lub błąd).")

if __name__ == "__main__":
    main()
