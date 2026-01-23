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
    
    # Ustawiamy datę sprawdzania (wczoraj)
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    print(f"📅 [Verification] Checking data for: {yesterday}")

    # 1. Pobierz Config, żeby wiedzieć, ile stacji POWINNO być
    config_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Config_Plants!A2:A25").execute()
    expected_keys = [row[0] for row in config_res.get('values', []) if row]
    
    # 2. Pobierz ostatnie 50 wierszy z RawData
    raw_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:G100").execute()
    raw_rows = raw_res.get('values', [])

    # Filtrujemy dane tylko z wczoraj
    yesterday_data = [row for row in raw_rows if row[0] == yesterday]
    found_keys = [row[1] for row in yesterday_data]

    missing_plants = [k for k in expected_keys if k not in found_keys]
    plants_with_zeros = [row[1] for row in yesterday_data if float(str(row[3]).replace(',','.')) <= 0 or float(str(row[4]).replace(',','.')) <= 0]

    # --- LOGIKA DECYZYJNA ---
    if missing_plants:
        print(f"❌ [Verification] FAILED: Missing data for plants: {missing_plants}")
        sys.exit(1) # Wyjście z błędem wymusza retry w GitHub Actions
    
    if plants_with_zeros:
        print(f"❌ [Verification] FAILED: Found zeros in production/weather for: {plants_with_zeros}")
        sys.exit(1)

    print("✅ [Verification] SUCCESS: All data for yesterday is present and valid.")
    sys.exit(0)

if __name__ == "__main__":
    verify_sync()
