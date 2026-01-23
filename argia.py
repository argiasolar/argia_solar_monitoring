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
    """Bezpieczna konwersja na liczbę (obsługuje przecinki i tekst)."""
    if value is None: return default
    try:
        clean_val = str(value).strip().replace(',', '.')
        return float(clean_val)
    except (ValueError, TypeError):
        return default

def main():
    print("--- 🌟 ARGIA SOLAR MONITORING v4.8 (Integrated SiteID) ---")
    service = get_service()
    
    # 1. Ustalenie dat (wczoraj)
    yesterday_dt = datetime.datetime.now() - datetime.timedelta(days=1)
    date_iso = yesterday_dt.strftime('%Y-%m-%d')
    date_slash = yesterday_dt.strftime('%-m/%-d/%Y')

    # 2. Pobranie konfiguracji stacji
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

    for row in config_rows:
        if len(row) >= 10:
            p_key = str(row[0]).strip() # np. SLP1
            brand = str(row[1]).strip().upper()
            s_id = str(row[9]).strip()  # SiteID z kolumny J (index 9)
            
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

    # 3. Pobieranie produkcji (Działamy na SiteID)
    all_prod = {}
    
    if huawei_map:
        all_prod.update(huawei.fetch_huawei_data(date_iso, huawei_map))
        
    if growatt_map:
        all_prod.update(growatt.fetch_growatt_data(date_iso, growatt_map))

    # 4. Przygotowanie danych do zapisu
    final_data = []
    
    # Przetwarzamy każdą stację z konfiguracji, aby mieć pewność, że każda ma swój wiersz
    for p_key, conf in plants_config.items():
        energy = all_prod.get(p_key, 0) # Bierzemy energię ze słownika wyników
        
        # Pobieranie pogody (używa PlantKey)
        irr, clouds = weather.get_estimated_weather(p_key)
        
        # Obliczenia (standardowe 0.8 jako ExpectedFactor)
        forecast = round(conf['kwp'] * irr * 0.8, 2)
        pr = round(energy / (conf['kwp'] * irr), 3) if (irr > 0 and conf['kwp'] > 0) else 0
        
        final_data.append([
            date_slash, p_key, conf['name'], energy, irr, forecast, pr, conf['target'], clouds
        ])

    # 5. Zapis do RawData
    if final_data:
        try:
            # Zapisujemy zawsze – jeśli energy=0, weryfikator to naprawi w Retry
            service.spreadsheets().values().append(
                spreadsheetId=SHEET_ID, 
                range="RawData!A2",
                valueInputOption="USER_ENTERED", 
                body={'values': final_data}
            ).execute()
            print(f"✅ Dane załadowane do RawData ({date_slash}).")
        except Exception as e:
            print(f"❌ Błąd zapisu do arkusza: {e}")
    else:
        print("⚠️ Brak danych do zapisu.")

if __name__ == "__main__":
    main()
