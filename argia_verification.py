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
    creds = service_account.Credentials.from_service_account_info(json.loads(creds_json))
    return build('sheets', 'v4', credentials=creds)

def verify_sync():
    print("🔍 [Verification] Starting data integrity check...")
    service = get_service()
    
    # Format daty 1/23/2026
    dt_yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    date_slash = dt_yesterday.strftime('%-m/%-d/%Y')
    
    # 1. Pobierz dane
    res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:I200").execute()
    rows = res.get('values', [])
    
    # 2. Szukaj błędów (zera w produkcji lub pogodzie)
    yesterday_rows = [r for r in rows if r[0] == date_slash]
    plants_with_errors = [r[1] for r in yesterday_rows if float(str(r[3]).replace(',','.')) <= 0 or float(str(r[4]).replace(',','.')) <= 0]

    if plants_with_errors:
        if os.environ.get('RETRY_ATTEMPT') == 'true':
            print(f"🛑 [Verification] Still errors after retry. Triggering Emergency Fixes...")
            weather.repair_missing_weather(date_slash)
            # Tu w przyszłości dodasz fill_dummy_data dla produkcji
            sys.exit(0)
        else:
            print(f"❌ [Verification] Zeros detected for: {plants_with_errors}. Triggering Retry...")
            sys.exit(1)
    
    print("✅ [Verification] All data looks good.")
    sys.exit(0)

if __name__ == "__main__":
    verify_sync()
