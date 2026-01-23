import os
import requests
import datetime
import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIGURATION ---
# Upewnij się, że te nazwy odpowiadają Twoim sekretom w GitHub Actions
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')
WEATHER_API_KEY = os.environ.get('OPENWEATHER_API_KEY')
HUAWEI_API_KEY = os.environ.get('HUAWEI_API_KEY') # Jeśli używasz API Key

# Zakresy w Google Sheets
RAW_DATA_SHEET = "RawData"

def fetch_weather_data(lat, lon, date):
    """Pobiera dane historyczne o pogodzie (Irradiance i Clouds)."""
    # Uproszczony model dla OpenWeather (wymaga subskrypcji 'One Call')
    # Jeśli korzystasz z darmowego, pobieramy dane aktualne/prognozowane
    url = f"https://api.openweathermap.org/data/3.0/onecall/day_summary?lat={lat}&lon={lon}&date={date}&appid={WEATHER_API_KEY}&units=metric"
    try:
        response = requests.get(url)
        data = response.json()
        # Zwracamy nasłonecznienie (D) i chmury (H)
        irradiance = data.get('irradiance', 0) / 1000 # konwersja do kWh/m2
        clouds = data.get('cloud_cover', {}).get('afternoon', 0)
        return irradiance, clouds
    except:
        return 0, 0

def fetch_huawei_data(target_date):
    """
    Pobiera dane o produkcji z falowników Huawei.
    Tutaj wstawiamy brakującą wcześniej funkcję.
    """
    print(f"Connecting to Huawei FusionSolar for date: {target_date}")
    # Placeholder dla logiki API Huawei (zależnie od Twojego tokenu)
    # Zwraca słownik: { 'PlantKey': kWh_value }
    mock_data = {
        'SLP1': 609,
        'SLP2': 986,
        'GTO1': 2259,
        'MEX1': 2174,
        'NL1': 2463,
        'MEX2': 2448
    }
    return mock_data

def update_google_sheets(all_data):
    """Wysyła zebrane dane do arkusza RawData."""
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    with open('creds.json', 'w') as f:
        f.write(creds_json)
    
    creds = service_account.Credentials.from_service_account_file('creds.json')
    service = build('sheets', 'v4', credentials=creds)
    
    # Przygotowanie wierszy do dodania
    values = []
    for entry in all_data:
        values.append([
            entry['date'], entry['key'], entry['customer'], 
            entry['actual'], entry['irradiance'], entry['forecast'],
            entry['real_pr'], entry['target_pr'], entry['clouds']
        ])

    body = {'values': values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID, range=f"{RAW_DATA_SHEET}!A2",
        valueInputOption="USER_ENTERED", body=body).execute()

def main():
    print("🚀 Sync v3.0 (Weather + Clouds) starting...")
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 1. Pobierz dane z Huawei
    huawei_energies = fetch_huawei_data(yesterday)
    
    # 2. Pobierz konfigurację stacji (dla współrzędnych i kWp)
    # W wersji demo używamy statycznej listy, w wersji full pobierasz z arkusza Config_Plants
    plants_config = {
        'SLP1': {'lat': 22.15, 'lon': -100.98, 'kwp': 189, 'target_pr': 0.85},
        'NL1': {'lat': 25.68, 'lon': -100.31, 'kwp': 545, 'target_pr': 0.95}
    }
    
    final_sync_list = []
    
    for key, energy in huawei_energies.items():
        config = plants_config.get(key, {'lat': 0, 'lon': 0, 'kwp': 100, 'target_pr': 0.85})
        
        # 3. Pobierz pogodę
        irradiance, clouds = fetch_weather_data(config['lat'], config['lon'], yesterday)
        
        # 4. Obliczenia
        forecast = config['kwp'] * irradiance * config['target_pr']
        real_pr = energy / (config['kwp'] * irradiance) if irradiance > 0 else 0
        
        final_sync_list.append({
            'date': yesterday, 'key': key, 'customer': 'Client Name',
            'actual': energy, 'irradiance': irradiance, 'forecast': forecast,
            'real_pr': real_pr, 'target_pr': config['target_pr'], 'clouds': clouds
        })
    
    # 5. Wyślij do Sheets
    update_google_sheets(final_sync_list)
    print("✅ Sync completed successfully.")

if __name__ == "__main__":
    main()
