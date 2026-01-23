import os
import json
import datetime
import sys
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIG ---
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def get_service():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_info)
    return build('sheets', 'v4', credentials=creds)

def verify_sync():
    print("🔍 [Verification] Starting data integrity check...")
    service = get_service()
    
    # Przygotowujemy oba formaty daty do sprawdzenia
    dt_yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    date_iso = dt_yesterday.strftime('%Y-%m-%d')
    date_slash = dt_yesterday.strftime('%-m/%-d/%Y') # Format 1/22/2026
    
    print(f"📅 [Verification] Looking for dates: {date_iso} or {date_slash}")

    # 1. Pobierz Config
    config_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Config_Plants!A2:A25").execute()
    expected_keys = [row[0] for row in config_res.get('values', []) if row]
    
    # 2. Pobierz RawData
    raw_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:G200").execute()
    raw_rows = raw_res.get('values', [])

    # Filtrujemy dane (sprawdzamy oba formaty daty)
    yesterday_data = [row for row in raw_rows if row[0] in [date_iso, date_slash]]
    found_keys = [row[1] for row in yesterday_data]

    missing_plants = [k for k in expected_keys if k not in found_keys]

    if missing_plants:
        print(f"❌ [Verification] FAILED: Missing data for: {missing_plants}")
        sys.exit(1)
    
    print(f"✅ [Verification] SUCCESS: Found all {len(found_keys)} plants.")
    sys.exit(0)

if __name__ == "__main__":
    verify_sync()
