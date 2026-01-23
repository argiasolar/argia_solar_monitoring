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
    """Autoryzacja Google Sheets API."""
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json), 
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build('sheets', 'v4', credentials=creds)

def safe_float(value, default=0.0):
    """Bezpieczna konwersja na liczbę."""
    if value is None: return default
    try:
        clean_val = str(value).strip().replace(',', '.')
        return float(clean_val)
    except (ValueError, TypeError):
        return default

def main():
    print("--- 🌟 ARGIA SOLAR MONITORING v4.7 (SiteID Integration) ---")
    service = get_service()
    
    yesterday_dt = datetime.datetime.now() - datetime.timedelta(days=1)
    date_iso = yesterday_dt.strftime('%Y-%m-%d')
    date_slash = yesterday_dt.strftime('%-m/%-d/%Y')

    # 1. Pobranie Config_Plants
    try:
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

    # 2. Mapowanie stacji wg Twojej struktury kolumn
    for row in config_rows:
        if len(row) >= 10:
            p_key = str(row[0]).strip() # SLP1
            brand = str(row[1]).strip().upper()
            s_id = str(row[9]).strip()  # SiteID (Kolumna J / Index 9)
            
            plants_config[p_key] = {
                'brand': brand,
                'kwp': safe_float(row[2]),
                'target': safe_float(row[7]),
                'name': str(row[8]).strip(),
                'site_id': s_id
            }

            if brand == "HUAWEI":
                huawei_map[s_id] = p_key
            elif brand == "GROWATT":
                growatt_map[s_id] = p_key

    # 3. Pobieranie produkcji (Używamy SiteID do zapytań API)
    all_prod = {}
    if huawei_map:
        all_prod.update(huawei.fetch_huawei_data(date_iso, huawei_map))
    if growatt_map:
        # Przekazujemy date_iso i mapę {SiteID: PlantKey}
        all_prod.update(growatt.fetch_growatt_data(date_iso, growatt_map))

    # 4. Przetwarzanie i przygotowanie do zapisu
    final_data = []
    total_energy = 0

    for p_key, energy in all_prod.items():
        if p_key in plants_config:
            conf = plants_config[p_key]
            
            # Pobieranie pogody (moduł weather używa PlantKey)
            irr, clouds = weather.get_estimated_weather(p_key)
            
            # Obliczenia
            forecast = round(conf['kwp'] * irr * 0.8, 2)
            pr = round(energy / (conf['kwp'] * irr), 3) if (irr > 0 and conf['kwp'] > 0) else 0
            
            final_data.append([
                date_slash, p_key, conf['name'], energy, irr, forecast, pr, conf['target'], clouds
            ])
            total_energy += energy

    # 5. Zapis do RawData
    if final_data and total_energy > 0:
        try:
            service.spreadsheets().values().append(
                spreadsheetId=SHEET_ID, range="RawData!A2",
                valueInputOption="USER_ENTERED", body={'values': final_data}
            ).execute()
            print(f"✅ Sync complete. Total energy: {total_energy} kWh")
        except Exception as e:
            print(f"❌ Błąd zapisu do arkusza: {e}")
    else:
        print("⚠️ No production data collected. Verifier will handle retry.")

if __name__ == "__main__":
    main()
