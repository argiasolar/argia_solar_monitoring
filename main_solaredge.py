import requests
import datetime
import json
import os
from oauth2client.service_account import ServiceAccountCredentials
import gspread

print("=== Start: SolarEdge Monitoring ===")

# Dwa osobne API Keys – jeden na firmę
API_KEY_HIRSCHMANN = os.environ['SOLAREDGE_API_KEY_HIRSCHMANN']
API_KEY_TETRAPAK = os.environ['SOLAREDGE_API_KEY_TETRAPAK']
SHEET_ID = os.environ['GOOGLE_SHEET_ID']

# Instalacje z przypisanym API Key
SOLAREDGE_SITES = {
    'Hirschmann': {'site_id': '4362085', 'api_key': API_KEY_HIRSCHMANN},
    'Tetrapak':   {'site_id': '4146396', 'api_key': API_KEY_TETRAPAK}
}

# Data wczorajsza
yesterday = (datetime.date.today() - datetime.timedelta(days=1))
start_time = f"{yesterday} 00:00:00"
end_time = f"{yesterday} 23:59:59"

print(f"Pobieranie danych za dzień: {yesterday}")

base_url = "https://monitoringapi.solaredge.com"

rows = []
rows.append([str(yesterday), f"Data pobrania: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} (UTC)"])
rows.append([])
rows.append(['Instalacja', 'Site ID', 'Produkcja wczoraj (kWh)'])

total_energy = 0

for name, info in SOLAREDGE_SITES.items():
    site_id = info['site_id']
    api_key = info['api_key']

    print(f"Pobieranie danych dla: {name} (Site ID: {site_id})")

    url = f"{base_url}/site/{site_id}/energyDetails"
    params = {
        'api_key': api_key,
        'startTime': start_time,
        'endTime': end_time,
        'timeUnit': 'DAY'
    }

    response = requests.get(url, params=params)
    if response.status_code != 200:
        print(f"  Błąd API dla {name}: {response.status_code} – {response.text}")
        daily_energy = 0.0
    else:
        data = response.json()
        try:
            daily_energy_wh = data['energyDetails']['meters'][0]['values'][0]['value']
            daily_energy = round(daily_energy_wh / 1000, 3)
        except (KeyError, IndexError, TypeError):
            print(f"  Brak danych produkcji dla {name}")
            daily_energy = 0.0

    rows.append([name, site_id, daily_energy])
    total_energy += daily_energy

rows.append([])
rows.append(['SUMA', '', round(total_energy, 3)])

# Testowe wiersze
rows.append([])
rows.append(["=== TEST ZAPISU – SKRYPT SOLAREDGE DZIAŁA! ===", "", ""])
rows.append(["Data testu", datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), "UTC"])
rows.append(["Liczba instalacji SolarEdge", len(SOLAREDGE_SITES), ""])

# Zapis do Google Sheets – zakładka SolarEdge istnieje
print("Zapisywanie danych SolarEdge do arkusza...")
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet = client.open_by_key(SHEET_ID)
worksheet = sheet.worksheet('SolarEdge')
print("Zakładka 'SolarEdge' znaleziona – dopisuję dane")

worksheet.append_rows(rows)
print("Dane SolarEdge zapisane pomyślnie!")

print(f"SUKCES! Raport SolarEdge za {yesterday} gotowy (łącznie {round(total_energy, 3)} kWh)")
print("=== Koniec SolarEdge Monitoring ===")
