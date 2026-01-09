import requests
import datetime
import json
import os
from oauth2client.service_account import ServiceAccountCredentials
import gspread

print("=== Start: Weather Monitoring for Airports ===")

# Konfiguracja z GitHub Secrets (te same co w innych modułach)
GOOGLE_CREDENTIALS = os.environ['GOOGLE_CREDENTIALS']
SHEET_ID = os.environ['GOOGLE_SHEET_ID']

# Lotniska i ich współrzędne (bardzo dokładne)
AIRPORTS = {
    'Silao (BJX)': {'lat': 20.993464, 'lon': -101.480847},
    'Monterrey (MTY)': {'lat': 25.778489, 'lon': -100.106878},
    'Mexico City (MEX)': {'lat': 19.436303, 'lon': -99.072097}
}

# Data wczorajsza
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
print(f"Pobieranie pogody za dzień: {yesterday}")

rows = []
rows.append([yesterday, f"Data pobrania: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} (UTC)"])
rows.append([])
rows.append([
    'Lotnisko', 
    'Średnia temp (°C)', 
    'Max temp (°C)', 
    'Min temp (°C)', 
    'Suma opadów (mm)', 
    'Irradiancja (kWh/m²)', 
    'Średnia prędkość wiatru (km/h)'
])

for name, coords in AIRPORTS.items():
    print(f"Pobieranie danych dla: {name}")

    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={coords['lat']}"
        f"&longitude={coords['lon']}"
        f"&start_date={yesterday}"
        f"&end_date={yesterday}"
        f"&daily=temperature_2m_mean,temperature_2m_max,temperature_2m_min,precipitation_sum,shortwave_radiation_sum,wind_speed_10m_mean"
        f"&timezone=America/Mexico_City"
    )

    response = requests.get(url)
    if response.status_code != 200:
        print(f"  Błąd API dla {name}: {response.status_code}")
        row = [name, '', '', '', '', '', '']
    else:
        data = response.json()
        daily = data.get('daily', {})

        irradiance_mj = daily.get('shortwave_radiation_sum', [0])[0] or 0
        irradiance_kwh = round(irradiance_mj / 3.6, 2)  # MJ/m² → kWh/m²

        row = [
            name,
            round(daily.get('temperature_2m_mean', [0])[0], 1),
            round(daily.get('temperature_2m_max', [0])[0], 1),
            round(daily.get('temperature_2m_min', [0])[0], 1),
            round(daily.get('precipitation_sum', [0])[0], 1),
            irradiance_kwh,
            round(daily.get('wind_speed_10m_mean', [0])[0], 1)
        ]

    rows.append(row)

# Zapis do Google Sheets – zakładka Weather
print("Zapisywanie danych pogodowych do arkusza...")
creds_dict = json.loads(GOOGLE_CREDENTIALS)
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet = client.open_by_key(SHEET_ID)

try:
    worksheet = sheet.worksheet('Weather')
    print("Zakładka 'Weather' znaleziona")
except gspread.WorksheetNotFound:
    worksheet = sheet.add_worksheet(title='Weather', rows=1000, cols=10)
    print("Utworzono nową zakładkę 'Weather'")

worksheet.append_rows(rows)
print("Dane pogodowe zapisane pomyślnie!")

print("=== Koniec Weather Monitoring ===")
