import requests
import datetime
import json
import os
from oauth2client.service_account import ServiceAccountCredentials
import gspread

print("=== Start: Huawei FusionSolar Monitoring ===")

# Dane z GitHub Secrets
HUAWEI_USERNAME = os.environ['HUAWEI_USERNAME']
HUAWEI_PASSWORD = os.environ['HUAWEI_PASSWORD']
SHEET_ID = os.environ['GOOGLE_SHEET_ID']  # to samo ID co dla Growatt

# Żądane instalacje Huawei
DESIRED_PLANT_NAMES = ['SAG', 'VITALMEX']  # <-- jeśli masz inne nazwy, zmień tutaj

# Data wczorajsza
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
print(f"Pobieranie danych za dzień: {yesterday}")

base_url = "https://la5.fusionsolar.huawei.com/thirdData"

# 1. Logowanie
login_payload = {
    "userName": HUAWEI_USERNAME,
    "systemCode": HUAWEI_PASSWORD
}
login_response = requests.post(base_url + "/login", json=login_payload)
login_response.raise_for_status()

token = login_response.headers.get('Xsrf-Token') or login_response.headers.get('XSRF-TOKEN')
if not token:
    raise Exception("Nie udało się pobrać tokena XSRF z logowania Huawei")

headers = {
    'XSRF-TOKEN': token,
    'Content-Type': 'application/json'
}

print("Logowanie do Huawei udane")

# 2. Lista stacji
station_response = requests.post(base_url + "/getStationList", headers=headers, json={})
station_response.raise_for_status()
stations = station_response.json().get('data', [])

plant_info = {}
station_codes = []

for plant in stations:
    name = plant['stationName']
    if name in DESIRED_PLANT_NAMES:
        capacity = plant.get('capacity', 0)
        if 0 < capacity < 1:
            capacity *= 1000  # MW → kW

        plant_info[name] = {
            'stationCode': plant['stationCode'],
            'capacity': capacity or 400,  # fallback – dostosuj jeśli potrzeba
            'lat': plant.get('latitude', 19.4326),
            'long': plant.get('longitude', -99.1332)
        }
        station_codes.append(plant['stationCode'])

print(f"Znaleziono {len(plant_info)} instalacji Huawei: {list(plant_info.keys())}")

if not station_codes:
    print("Nie znaleziono żądanych instalacji Huawei – kończę")
else:
    # 3. Real KPI – produkcja dzienna
    kpi_payload = {"stationCodes": ",".join(station_codes)}
    kpi_response = requests.post(base_url + "/getStationRealKpi", headers=headers, json=kpi_payload)
    kpi_response.raise_for_status()
    kpi_data = kpi_response.json().get('data', [])

    rows = []
    rows.append([yesterday, f"Data pobrania: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} (UTC)"])
    rows.append([])
    rows.append(['Instalacja', 'Moc (kW)', 'Produkcja wczoraj (kWh)', 'Irradiancja (kWh/m²)', 'Śr. temp (°C)', 'Planowana (kWh)', 'KPI (%)'])

    total_actual = 0
    total_planned = 0

    for item in kpi_data:
        code = item['stationCode']
        plant_name = next((n for n, i in plant_info.items() if i['stationCode'] == code), None)
        if not plant_name:
            continue

        info = plant_info[plant_name]
        daily_energy = item['dataItemMap'].get('day_power', 0)  # kWh

        # Open-Meteo – irradiancja i temperatura
        meteo_url = f"https://archive-api.open-meteo.com/v1/archive?latitude={info['lat']}&longitude={info['long']}&start_date={yesterday}&end_date={yesterday}&daily=shortwave_radiation_sum,temperature_2m_mean&timezone=America%2FMexico_City"
        meteo_resp = requests.get(meteo_url)
        meteo_json = meteo_resp.json()

        irradiance_mj = meteo_json['daily']['shortwave_radiation_sum'][0] or 0
        irradiance_kwh = round(irradiance_mj / 3.6, 2)
        avg_temp = round(meteo_json['daily']['temperature_2m_mean'][0] or 25, 1)

        # Planowana produkcja z korektą temperatury
        base_efficiency = 0.85
        temp_loss = max(avg_temp - 25, 0) * 0.004
        efficiency = base_efficiency * (1 - temp_loss)
        planned = round(irradiance_kwh * info['capacity'] * efficiency, 2)

        kpi = round((daily_energy / planned * 100), 2) if planned > 0 else 0

        rows.append([plant_name, info['capacity'], daily_energy, irradiance_kwh, avg_temp, planned, f"{kpi}%"])

        total_actual += daily_energy
        total_planned += planned

    rows.append([])
    rows.append(['SUMA', '', total_actual, '', '', total_planned, ''])

    # Zapis do Google Sheets
    print("Zapisywanie danych Huawei do arkusza...")
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)

    sheet = client.open_by_key(SHEET_ID)
    try:
        worksheet = sheet.worksheet('Huawei')
        print("Zakładka 'Huawei' znaleziona")
    except gspread.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title='Huawei', rows=1000, cols=10)
        print("Utworzono nową zakładkę 'Huawei'")

    worksheet.append_rows(rows)
    print("Dane Huawei zapisane pomyślnie!")

print("=== Koniec Huawei Monitoring ===")
