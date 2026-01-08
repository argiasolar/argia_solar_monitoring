import requests
import datetime
import json
import os
from oauth2client.service_account import ServiceAccountCredentials
import gspread

print("=== Start: SolarEdge Monitoring ===")

# API Key z GitHub Secrets
SOLAREDGE_API_KEY = os.environ['SOLAREDGE_API_KEY']
SHEET_ID = os.environ['GOOGLE_SHEET_ID']

# Twoje instalacje SolarEdge
SOLAREDGE_SITES = {
    'Hirschmann': '4362085',
    'Tetrapak': '4146396'
}

# Data wczorajsza
yesterday = (datetime.date.today() - datetime.timedelta(days=1))
start_date = yesterday.strftime('%Y-%m-%d')
end_date = yesterday.strftime('%Y-%m-%d')

print(f"Pobieranie danych za dzień: {start_date}")

base_url = "https://monitoringapi.solaredge.com"

rows = []
rows.append([start_date, f"Data pobrania: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} (UTC)"])
rows.append([])
rows.append(['Instalacja', 'Site ID', 'Produkcja wczoraj (kWh)'])

total_energy = 0

for name, site_id in SOLAREDGE_SITES.items():
    print(f"Pobieranie danych dla: {name} (Site ID: {site_id})")

    url = f"{base_url}/site/{site_id}/energyDetails"
    params = {
        'api_key': SOLAREDGE_API_KEY,
        'startDate': start_date,
        'endDate': end_date,
        'timeUnit': 'DAY'
    }

    response = requests.get(url, params=params)
    if response.status_code != 200:
        print(f"  Błąd API dla {name}: {response.status_code} – {response.text}")
        daily_energy = 0.0
    else:
        data = response.json()
        try:
            # SolarEdge zwraca wartość w Wh
            daily_energy_wh = data['energyDetails']['meters'][0]['values'][0]['value']
            daily_energy = round(daily_energy_wh / 1000, 3)  # Wh → kWh
        except (KeyError, IndexError, TypeError):
            print(f"  Brak danych produkcji dla {name}")
            daily_energy = 0.0

    rows.append([name, site_id, daily_energy])
    total_energy += daily_energy

rows.append([])
rows.append(['SUMA', '', round(total_energy, 3)])

# Wiersze testowe
rows.append([])
rows.append(["=== TEST ZAPISU – SKRYPT SOLAREDGE DZIAŁA! ===", "", ""])
rows.append(["Data testu", datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), "UTC"])
rows.append(["Liczba instalacji SolarEdge", len(SOLAREDGE_SITES), ""])

# Zapis do Google Sheets – niezawodna obsługa istniejącej zakładki
print("Zapisywanie danych SolarEdge do arkusza...")
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet = client.open_by_key(SHEET_ID)
# Odśwież listę zakładek, żeby uniknąć cache
    sheet.fetch_sheet_metadata()
    worksheets = sheet.worksheets()

    worksheet = None
    for ws in worksheets:
        if ws.title == 'SolarEdge':
            worksheet = ws
            print("Zakładka 'SolarEdge' znaleziona")
            break

    if worksheet is None:
        print("Zakładka 'SolarEdge' nie istnieje – tworzę nową...")
        worksheet = sheet.add_worksheet(title='SolarEdge', rows=1000, cols=10)
        print("Utworzono nową zakładkę 'SolarEdge'")

    worksheet.append_rows(rows)
    print("Dane SolarEdge zapisane pomyślnie!")


print(f"SUKCES! Raport SolarEdge za {start_date} gotowy (łącznie {round(total_energy, 3)} kWh)")
print("=== Koniec SolarEdge Monitoring ===")
