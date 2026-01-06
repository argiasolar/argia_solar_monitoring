import requests
import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json

print("=== Start: Growatt Monitoring (API Token) ===")

# API Token z GitHub Secrets (dodamy nowy secret)
API_TOKEN = os.environ['GROWATT_API_TOKEN']
GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', 'ARGA Solar')

# Data wczorajsza
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
print(f"Pobieranie danych za dzień: {yesterday}")

base_url = "https://openapi.growatt.com/v1"

headers = {
    "token": API_TOKEN
}

# 1. Lista plantów
plant_list_url = f"{base_url}/plant/list"
response = requests.get(plant_list_url, headers=headers)
response.raise_for_status()
plants_data = response.json()

plants = plants_data.get('data', {}).get('plants', [])
if not plants:
    raise Exception("Nie znaleziono instalacji – sprawdź token")

print(f"Znaleziono {len(plants)} instalacji")

# Przygotowanie wierszy
rows = []
rows.append([yesterday, f"Data pobrania: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} (UTC)"])
rows.append([])
rows.append(['Nazwa instalacji', 'Plant ID', 'Produkcja wczoraj (kWh)'])

total_energy = 0

for plant in plants:
    plant_id = plant['plant_id']
    plant_name = plant.get('name', 'Bez nazwy')

    print(f"Pobieranie danych dla: {plant_name} (ID: {plant_id})")

    # 2. Energia dzienna – POST request
    energy_url = f"{base_url}/plant/day/energy"
    payload = {
        "plantId": plant_id,
        "date": yesterday
    }

    energy_response = requests.post(energy_url, headers=headers, json=payload)
    if energy_response.status_code == 200:
        energy_data = energy_response.json()
        day_energy_wh = energy_data.get('data', {}).get('energy', 0)
        day_energy_kwh = round(day_energy_wh / 1000, 3)
    else:
        print(f"  Błąd dla {plant_name}: {energy_response.text}")
        day_energy_kwh = 0.0

    rows.append([plant_name, plant_id, day_energy_kwh])
    total_energy += day_energy_kwh

rows.append([])
rows.append(['RAZEM', '', round(total_energy, 3)])

# Zapis do Google Sheets (ten sam kod co wcześniej)
print("Zapisywanie do Google Sheets...")
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet = client.open(GOOGLE_SHEET_NAME)
try:
    worksheet = sheet.worksheet('Growatt')
except gspread.WorksheetNotFound:
    worksheet = sheet.add_worksheet(title='Growatt', rows=1000, cols=10)

worksheet.append_rows(rows)

print(f"SUKCES! Zapisano dane – łącznie {round(total_energy, 3)} kWh")
print("=== Koniec ===")
