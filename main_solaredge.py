import requests
import datetime
import json
import os
from oauth2client.service_account import ServiceAccountCredentials
import gspread

print("=== Start: SolarEdge Monitoring ===")

# Konfiguracja z GitHub Secrets
SOLAREDGE_API_KEY = os.environ['SOLAREDGE_API_KEY']
SHEET_ID = os.environ['GOOGLE_SHEET_ID']  # to samo ID co dla Growatt i Huawei

# Lista Site ID Twoich instalacji SolarEdge
# Dodaj swoje Site ID (z URL portalu: https://monitoring.solaredge.com/sites/123456/overview)
SOLAREDGE_SITE_IDS = ['123456', '789012']  # <--- ZMIEŃ na swoje Site ID (jako stringi)

# Data wczorajsza
yesterday = (datetime.date.today() - datetime.timedelta(days=1))
start_date = yesterday.strftime('%Y-%m-%d')
end_date = yesterday.strftime('%Y-%m-%d')

print(f"Pobieranie danych za dzień: {start_date}")

base_url = "https://monitoringapi.solaredge.com"

rows = []
rows.append([start_date, f"Data pobrania: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} (UTC)"])
rows.append([])
rows.append(['Instalacja (Site ID)', 'Produkcja wczoraj (kWh)'])

total_energy = 0

for site_id in SOLAREDGE_SITE_IDS:
    print(f"Pobieranie danych dla Site ID: {site_id}")

    # Endpoint: energy details (dzienna produkcja)
    url = f"{base_url}/site/{site_id}/energyDetails"
    params = {
        'api_key': SOLAREDGE_API_KEY,
        'startDate': start_date,
        'endDate': end_date,
        'timeUnit': 'DAY'
    }

    response = requests.get(url, params=params)
    if response.status_code != 200:
        print(f"  Błąd API dla Site ID {site_id}: {response.status_code} – {response.text}")
        daily_energy = 0.0
    else:
        data = response.json()
        # SolarEdge zwraca listę wartości dziennych
        try:
            daily_energy = data['energyDetails']['meters'][0]['values'][0]['value'] / 1000  # Wh → kWh
            daily_energy = round(daily_energy, 3)
        except (KeyError, IndexError, TypeError):
            print(f"  Brak danych produkcji dla Site ID {site_id}")
            daily_energy = 0.0

    rows.append([site_id, daily_energy])
    total_energy += daily_energy

rows.append([])
rows.append(['SUMA', total_energy])

# Dodatkowe wiersze testowe
rows.append([])
rows.append(["=== TEST ZAPISU – SKRYPT SOLAREDGE DZIAŁA! ===", ""])
rows.append(["Data testu", datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), "UTC"])
rows.append(["Liczba instalacji SolarEdge", len(SOLAREDGE_SITE_IDS), ""])

# Zapis do Google Sheets
print("Zapisywanie danych SolarEdge do arkusza...")
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet = client.open_by_key(SHEET_ID)
try:
    worksheet = sheet.worksheet('SolarEdge')
    print("Zakładka 'SolarEdge' znaleziona")
except gspread.WorksheetNotFound:
    worksheet = sheet.add_worksheet(title='SolarEdge', rows=1000, cols=10)
    print("Utworzono nową zakładkę 'SolarEdge'")

worksheet.append_rows(rows)
print("Dane SolarEdge zapisane pomyślnie!")

print(f"SUKCES! Raport SolarEdge za {start_date} gotowy (łącznie {total_energy} kWh)")
print("=== Koniec SolarEdge Monitoring ===")
