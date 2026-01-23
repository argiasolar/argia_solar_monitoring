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
    
    dt_yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    date_slash = dt_yesterday.strftime('%-m/%-d/%Y')
    print(f"📅 [Verification] Target date: {date_slash}")

    # Pobieramy RawData (kolumny A do G)
    res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:G200").execute()
    rows = res.get('values', [])
    
    yesterday_rows = [r for r in rows if len(r) > 4 and r[0] == date_slash]
    
    plants_with_errors = []
    for r in yesterday_rows:
        try:
            # Czyścimy dane z przecinków i spacji przed zamianą na liczbę
            prod = float(str(r[3]).replace(',', '.').strip()) if len(r) > 3 else 0
            irr = float(str(r[4]).replace(',', '.').strip()) if len(r) > 4 else 0
            
            if prod <= 0 or irr <= 0:
                plants_with_errors.append(r[1])
        except Exception:
            plants_with_errors.append(r[1])

    if plants_with_errors:
        if os.environ.get('RETRY_ATTEMPT') == 'true':
            print(f"🛑 [Verification] Still errors. Triggering Repair for: {plants_with_errors}")
            weather.repair_missing_weather(date_slash)
            sys.exit(0)
        else:
            print(f"❌ [Verification] Zeros detected for: {plants_with_errors}. Triggering Retry...")
            sys.exit(1)
    
    print(f"✅ [Verification] Success. Found {len(yesterday_rows)} valid records.")
    sys.exit(0)

if __name__ == "__main__":
    verify_sync()
