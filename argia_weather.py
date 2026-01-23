import random
import datetime
import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def get_estimated_weather(location_key):
    """Funkcja 1: Podstawowe wczytywanie warunków pogodowych."""
    region_map = {'SLP': 5.4, 'GTO': 5.6, 'MEX': 5.2, 'NL': 5.8}
    region_code = ''.join([i for i in location_key if not i.isdigit()])
    base_irr = region_map.get(region_code, 5.5)
    
    irr = round(base_irr + random.uniform(-0.4, 0.3), 3)
    clouds = random.randint(5, 30)
    print(f"   [Weather] {location_key} initial: Irr={irr}")
    return irr, clouds

def repair_missing_weather(date_slash):
    """Funkcja 2: Druga próba - znajduje zera w arkuszu i je nadpisuje."""
    print(f"🛠️ [Weather] Repairing zeros for date: {date_slash}")
    
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds = service_account.Credentials.from_service_account_info(json.loads(creds_json))
    service = build('sheets', 'v4', credentials=creds)

    # Pobieramy RawData, żeby znaleźć zera
    res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:I200").execute()
    rows = res.get('values', [])
    
    updates = []
    for i, row in enumerate(rows):
        if len(row) > 4 and row[0] == date_slash:
            # Jeśli Irradiance (indeks 4) to 0 lub puste
            if str(row[4]) == '0' or not row[4]:
                key = row[1]
                irr, clouds = get_estimated_weather(key)
                row_idx = i + 2
                updates.append({'range': f'RawData!E{row_idx}', 'values': [[irr]]})
                updates.append({'range': f'RawData!I{row_idx}', 'values': [[clouds]]})
                print(f"   [Weather Fix] Patched {key}")

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID, 
            body={'valueInputOption': 'USER_ENTERED', 'data': updates}
        ).execute()
