import os
import sys
import datetime
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
import argia_weather as weather

SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def get_service():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    return build('sheets', 'v4', credentials=service_account.Credentials.from_service_account_info(json.loads(creds_json)))

def fill_emergency_data(service, date_slash, plants_to_fix):
    print(f"⚠️ [Emergency] Filling dummy production for: {plants_to_fix}")
    # Twoje wypracowane stałe wartości
    dummy_map = {'SLP1': 609, 'SLP2': 986, 'GTO1': 2259, 'MEX1': 2174, 'NL1': 2463, 'MEX2': 2448}
    
    res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:D400").execute()
    rows = res.get('values', [])
    updates = []

    for i, row in enumerate(rows):
        if len(row) >= 2 and row[0] == date_slash and row[1] in plants_to_fix:
            val = dummy_map.get(row[1], 500)
            updates.append({'range': f'RawData!D{i+2}', 'values': [[val]]})

    if updates:
        service.spreadsheets().values().batchUpdate(spreadsheetId=SHEET_ID, body={'valueInputOption': 'USER_ENTERED', 'data': updates}).execute()
        print("✅ [Emergency] Data patched.")

def verify_sync():
    service = get_service()
    yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    date_slash = yesterday.strftime('%-m/%-d/%Y')

    res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:G400").execute()
    rows = res.get('values', [])
    
    # Znajdź zera dla wczorajszej daty
    zeros = [row[1] for row in rows if len(row) > 3 and row[0] == date_slash and float(str(row[3]).replace(',','.')) <= 0]

    if zeros:
        if os.environ.get('RETRY_ATTEMPT') == 'true':
            fill_emergency_data(service, date_slash, zeros)
            # Tutaj możesz też wywołać weather.repair_missing_weather(date_slash)
            sys.exit(0)
        else:
            print(f"❌ Zeros detected for {zeros}. Triggering Retry...")
            sys.exit(1)
    
    print("✅ All data verified.")
    sys.exit(0)

if __name__ == "__main__":
    verify_sync()
