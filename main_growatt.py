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

if response.status_code != 200:
    print(f"Błąd pobierania listy instalacji: {response.status_code} – {response.text}")
    raise Exception("Nie udało się pobrać listy instalacji")

try:
    plants_data = response.json()
except json.JSONDecodeError:
    print("Błąd parsowania JSON z listy instalacji")
    print("Odpowiedź:", response.text)
    raise

plants = plants_data.get('data', {}).get('plants', [])
if not plants:
    raise Exception("Nie znaleziono instalacji – sprawdź token lub odpowiedź API")

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

# Dodatkowe wyraźne wiersze testowe – żebyś widział zapis
rows.append([])
rows.append(["*****************************************", "", ""])
rows.append(["*** SKRYPT GROWATT DZIAŁA – ZAPIS UDANY! ***", "", ""])
rows.append(["*****************************************", "", ""])
rows.append(["Data testu", datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), "UTC"])
rows.append(["Liczba instalacji", len(plants), ""])
rows.append(["Uwaga", "Produkcja = 0 kWh – token end-user nie daje dostępu do historii", ""])
rows.append(["Rozwiązanie", "Poproś Growatt o token instalatora (przez oss.growatt.com)", ""])

# Zapis do Google Sheets przez ID
print("Łączenie z Google Sheets przez ID...")
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

SHEET_ID = '16rzpz5gvzSh4WdBQ2qv7pD_EY0V7r0IrvfKVj1Fl0wk'  # Twoje ID arkusza ARGIA Solar

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
