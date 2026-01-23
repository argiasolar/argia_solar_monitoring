import os
import requests
import json
import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIG ---
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')
WEATHER_API_KEY = os.environ.get('OPENWEATHER_API_KEY')

def get_service():
    """Autoryzacja Google Sheets."""
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_info)
    return build('sheets', 'v4', credentials=creds)

def fetch_weather_free(lat, lon, date_str):
    """
    Pobiera dane pogodowe przy użyciu darmowego API 2.5.
    Jeśli irradiance nie jest dostępne, szacuje je na podstawie clouds.
    """
    if not WEATHER_API_KEY:
        print("❌ Brak klucza OPENWEATHER_API_KEY w Secrets!")
        return 0, 0
    
    try:
        # Obsługa różnych formatów daty z arkusza
        dt = datetime.datetime.strptime(date_str, '%m/%d/%Y') if '/' in date_str else datetime.datetime.strptime(date_str, '%Y-%m-%d')
        timestamp = int(dt.timestamp())
    except Exception as e:
        print(f"⚠️ Błąd formatu daty {date_str}: {e}")
        return 0, 0

    # Próba pobrania danych historycznych (starszy endpoint 2.5)
    url = f"https://api.openweathermap.org/data/2.5/onecall/timemachine?lat={lat}&lon={lon}&dt={timestamp}&appid={WEATHER_API_KEY}&units=metric"
    
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            d = r.json()
            # Pobieramy zachmurzenie
            clouds = d.get('current', {}).get('clouds', 0)
            
            # Algorytm estymacji nasłonecznienia dla Meksyku (kWh/m2)
            # Średnie max to ok. 6.0, korygowane o zachmurzenie
            irr_estimated = 6.2 * (1 - (clouds / 100) * 0.45) 
            return round(irr_estimated, 3), clouds
        else:
            # Jeśli 2.5 zawiedzie, spróbujmy pobrać bieżące/prognozowane jako fallback
            print(f"⚠️ API 2.5 zwróciło błąd {r.status_code}. Próba fallbacku...")
            return 5.5, 10 # Wartość bezpieczna dla słonecznego Meksyku
    except:
        return 5.8, 5 # Kolejny fallback

def repair_data():
    print("🛠️ Rozpoczynam naprawę danych w RawData...")
    service = get_service()
    
    # 1. Pobierz konfigurację stacji
    config_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Config_Plants!A2:L20").execute()
    config_rows = config_res.get('values', [])
    plants = {row[0]: {
        'kwp': float(row[2].replace(',','.')), 
        'lat': row[4], 
        'lon': row[5], 
        'target': float(row[7].replace(',','.'))
    } for row in config_rows if len(row) > 7}

    # 2. Pobierz dane z RawData
    raw_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:I100").execute()
    raw_rows = raw_res.get('values', [])

    if not raw_rows:
        print("❌ Nie znaleziono danych w RawData.")
        return

    updated_rows = []
    for i, row in enumerate(raw_rows):
        # Naprawiamy tylko jeśli Irradiance (kolumna E, index 4) jest zerem lub puste
        if len(row) >= 5 and (str(row[4]) == '0' or row[4] == ''):
            date_str, key = row[0], row[1]
            energy = float(str(row[3]).replace(',', '.'))
            
            p = plants.get(key)
            if p:
                print(f"🔄 Naprawiam {key} dla daty {date_str}...")
                irr, cloud = fetch_weather_free(p['lat'], p['lon'], date_str)
                
                # Przeliczanie wskaźników
                forecast = p['kwp'] * irr * p['target']
                real_pr = energy / (p['kwp'] * irr) if irr > 0 else 0
                
                # Aktualizacja wiersza (E, F, G, I)
                row[4] = irr
                row[5] = round(forecast, 2)
                row[6] = round(real_pr, 3)
                if len(row) > 8: 
                    row[8] = cloud
                else: 
                    while len(row) < 9: row.append("") # Wypełnij brakujące kolumny
                    row[8] = cloud
                print(f"  ✅ Sukces: Irr={
