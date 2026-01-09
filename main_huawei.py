import requests
import datetime
import json
import os
from oauth2client.service_account import ServiceAccountCredentials
import gspread

print("=== Start: Huawei FusionSolar Monitoring ===")

# Konfiguracja z GitHub Secrets
HUAWEI_USERNAME = os.environ['HUAWEI_USERNAME']
HUAWEI_PASSWORD = os.environ['HUAWEI_PASSWORD']
SHEET_ID = os.environ['GOOGLE_SHEET_ID']

# Żądane nazwy plantów Huawei
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
    print("=== Koniec Huawei Monitoring ===")
else:
    # 3. Real KPI – produkcja dzienna
    kpi_payload = {"stationCodes": ",".join(station_codes)}
    kpi_response = requests.post(base_url + "/getStationRealKpi", headers=headers, json=kpi_payload)
    kpi_response.raise_for_status()
    kpi_data = kpi_response.json().get('data', [])

    # Budowanie wierszy – WSZYSTKO z dokładnie 7 kolumnami
    rows = []
    rows.append([yesterday, f"Data pob
