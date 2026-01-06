import growattServer
import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json

print("=== Start: Growatt Monitoring ===")

# Pobieranie konfiguracji z GitHub Secrets
GROWATT_USERNAME = os.environ['GROWATT_USERNAME']
GROWATT_PASSWORD = os.environ['GROWATT_PASSWORD']
GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', 'ARGA Solar')

# Data wczorajsza
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
print(f"Pobieranie danych za dzień: {yesterday}")

# Logowanie do Growatt
api = growattServer.GrowattApi()
login_response = api.login(GROWATT_USERNAME, GROWATT_PASSWORD)

if not login_response.get('success'):
    print("BŁĄD LOGOWANIA DO GROWATT:")
    print(login_response)
    raise Exception("Nie udało się zalogować do konta Growatt")

print("Logowanie udane!")

# Pobranie listy wszystkich instalacji
plants_response = api.plant_list()
plants = plants_response.get('data', [])

if not plants:
    raise Exception("Nie znaleziono żadnych instalacji na koncie Growatt")

print(f"Znaleziono {len(plants)} instalacji")

# Przygotowanie danych do arkusza
rows = []
rows.append([yesterday, f"Data pobrania: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} (UTC)"])
rows.append([])
rows.append(['Nazwa instalacji', 'Plant ID', 'Produkcja wczoraj (kWh)'])

total_energy = 0

for plant in plants:
    plant_id = plant['plantId']
    plant_name = plant.get('plantName', 'Bez nazwy')

    print(f"Pobieranie danych dla: {plant_name} (ID: {plant_id})")

    try:
        detail = api.plant_detail(plant_id, date=yesterday)
        
        # Growatt używa różnych kluczy w zależności od modelu inwertera
        day_energy_wh = (
            detail.get('energy') or
            detail.get('eToday') or
            detail.get('todayEnergy') or
            detail.get('epvToday') or
            detail.get('pac') or  # czasem tylko moc bieżąca
            0
        )
        
        day_energy_kwh = round(day_energy_wh / 1000, 3) if day_energy_wh else 0.0

    except Exception as e:
        print(f"  Błąd pobierania danych dla {plant_name}: {e}")
        day_energy_kwh = 0.0

    rows.append([plant_name, plant_id, day_energy_kwh])
    total_energy += day_energy_kwh

# Suma całkowita
rows.append([])
rows.append(['RAZEM', '', round(total_energy, 3)])

# Zapis do Google Sheets
print("Łączenie z Google Sheets...")

creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet = client.open(GOOGLE_SHEET_NAME)

try:
    worksheet = sheet.worksheet('Growatt')
    print("Zakładka 'Growatt' znaleziona")
except gspread.WorksheetNotFound:
    worksheet = sheet.add_worksheet(title='Growatt', rows=1000, cols=10)
    print("Utworzono nową zakładkę 'Growatt'")

worksheet.append_rows(rows)
print(f"SUKCES! Zapisano dane za {yesterday}")
print(f"Łączna produkcja wczoraj: {round(total_energy, 3)} kWh")
print("=== Koniec: Growatt Monitoring ===")
