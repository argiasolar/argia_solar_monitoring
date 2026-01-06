import requests
import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json

print("=== Start: Growatt Monitoring (API Token) ===")

# API Token z GitHub Secrets
API_TOKEN = os.environ['GROWATT_API_TOKEN']

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

# Przygotowanie wierszy z danymi (produkcja będzie 0 kWh przy tokenie end-user)
rows = []
rows.append([yesterday, f"Data pobrania: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} (UTC)"])
rows.append([])
rows.append(['Nazwa instalacji', 'Plant ID', 'Produkcja wczoraj (kWh)'])

total_energy = 0

for plant in plants:
    plant_id = plant['plant_id']
    plant_name = plant.get('name', 'Bez nazwy')

    print(f"Pobieranie danych dla: {plant_name} (ID: {plant_id})")

    # Próba pobrania energii dziennej
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
        print(f"  Błąd API dla {plant_name}: {energy_response.status_code} – brak dostępu do danych historycznych")
        day_energy_kwh = 0.0

    rows.append([plant_name, plant_id, day_energy_kwh])
    total_energy += day_energy_kwh

rows.append([])
rows.append(['RAZEM', '', round(total_energy, 3)])

# Dodatkowe wyraźne wiersze testowe – żebyś od razu widział zapis
rows.append([])
rows.append(["=== TEST ZAPISU – SKRYPT DZIAŁA! ===", "", ""])
rows.append(["Data testu", datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), "UTC"])
rows.append(["Liczba instalacji", len(plants), ""])
rows.append(["Uwaga", "Produkcja = 0 kWh – token z ShinePhone nie daje dostępu do danych dziennych", ""])
rows.append(["Rozwiązanie", "Poproś Growatt o token instalatora (przez oss.growatt.com)", ""])

# Zapis do Google Sheets – przez ID arkusza
print("Łączenie z Google Sheets przez ID...")

creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

# Zmień na swoje ID arkusza ARGIA Solar (z URL: https://docs.google.com/spreadsheets/d/TWOJE_ID/edit)
SHEET_ID = '16rzpz5gvzSh4WdBQ2qv7pD_EY0V7r0IrvfKVj1Fl0wk'

sheet = client.open_by_key(SHEET_ID)

try:
    worksheet = sheet.worksheet('Growatt')
    print("Zakładka 'Growatt' znaleziona")
except gspread.WorksheetNotFound:
    worksheet = sheet.add_worksheet(title='Growatt', rows=1000, cols=10)
    print("Utworzono nową zakładkę 'Growatt'")

print("Zapisywanie danych do arkusza...")
worksheet.append_rows(rows)
print("Dane zapisane pomyślnie!")

print(f"SUKCES! Raport za {yesterday} gotowy (łączna produkcja widoczna: {round(total_energy, 3)} kWh)")
print("=== Koniec Growatt Monitoring ===")
