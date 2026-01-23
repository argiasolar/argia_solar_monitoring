import os
import requests
import datetime
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIGURATION ---
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')
WEATHER_API_KEY = os.environ.get('OPENWEATHER_API_KEY')
RAW_DATA_SHEET = "RawData"
CONFIG_SHEET = "Config_Plants"

def get_service():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_info)
    return build('sheets', 'v4', credentials=creds)

def get_plants_config(service):
    """Pobiera dynamicznie listę stacji z zakładki Config_Plants."""
    range_name = f"{CONFIG_SHEET}!A2:L20" # Pobieramy kolumny od PlantKey do Longitude
    result = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=range_name).execute()
    values = result.get('values', [])
    
    config = {}
    for row in values:
        if len(row) >= 6:
            # Struktura: Key(0), Brand(1), kWp_DC(2), kWp_AC(3), Lat(4), Lon(5), PR_Target(7), Name(8)
            config[row[0]] = {
                'brand': row[1],
                'kwp': float(row[2].replace(',', '.')),
                'lat': float(row[4].replace(',', '.')),
                'lon': float(row[5].replace(',', '.')),
                'target': float(row[7].replace(',', '.')) if len(row) > 7 else 0.85,
                'name': row[8] if len(row) > 8 else "Unknown Customer"
            }
    return config

def fetch_huawei_data(target_date):
    """Logika pobierania danych z Huawei (Mock na ten moment)."""
    return {'SLP1': 609, 'SLP2': 986, 'GTO1': 2259, 'MEX1': 2174, 'NL1': 2463, 'MEX2': 2448}

def fetch_weather_data(lat, lon, date):
    """Pobiera nasłonecznienie i chmury z OpenWeather."""
    url = f"https://api.openweathermap.org/data/3.0/onecall/day_summary?lat={lat}&lon={lon}&date={date}&appid={WEATHER_API_KEY}&units=metric"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            d = r.json()
            # Sprawdź jednostki API - jeśli podaje w Wh, dzielimy przez 1000
            irradiance = d.get('irradiance', 0) / 1000 
            clouds = d.get('cloud_cover', {}).get('afternoon', 0)
            return irradiance, clouds
    except:
        pass
    return 0, 0

def main():
    print("🚀 Sync v3.2 (Dynamic Config) starting...")
    service = get_service()
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 1. Pobierz konfigurację z Arkusza
    plants = get_plants_config(service)
    
    # 2. Pobierz produkcję
    production = fetch_huawei_data(yesterday)
    
    final_data = []
    for key, energy in production.items():
        if key in plants:
            p = plants[key]
            irr, clouds = fetch_weather_data(p['lat'], p['lon'], yesterday)
            
            forecast = p['kwp'] * irr * p['target']
            real_pr = energy / (p['kwp'] * irr) if irr > 0 else 0
            
            final_data.append([
                yesterday, key, p['name'], energy, irr, forecast, real_pr, p['target'], clouds
            ])
    
    # 3. Wyślij do RawData
    if final_data:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{RAW_DATA_SHEET}!A2",
            valueInputOption="USER_ENTERED", body={'values': final_data}
        ).execute()
        print(f"✅ Successfully synced {len(final_data)} plants.")

if __name__ == "__main__":
    main()
