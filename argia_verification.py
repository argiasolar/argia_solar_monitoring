# argia_verification.py
import os
import json
import datetime as dt
from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

def get_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)

def main():
    service = get_service()
    
    # Logika "dzisiaj" (UTC-6) spójna z argia.py
    now_local = dt.datetime.utcnow() + dt.timedelta(hours=-6)
    today_slash = f"{now_local.month}/{now_local.day}/{now_local.year}"
    
    print(f"🔍 [Verification] Checking data for TODAY: {today_slash}")

    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range="RawData!A2:J5000",
    ).execute()
    rows = res.get("values", [])

    # Filtrujemy wiersze z dzisiejszą datą
    today_rows = [r for r in rows if len(r) > 0 and r[0] == today_slash]

    if not today_rows:
        print(f"❌ No rows found for {today_slash}")
        exit(1)

    errors = []
    for r in today_rows:
        p_key = r[1]
        try:
            kwh = float(str(r[3]).replace(",", "."))
            irr = float(str(r[4]).replace(",", "."))
            
            # Weryfikacja: Produkcja i nasłonecznienie muszą być dodatnie
            if kwh <= 0:
                errors.append(f"{p_key} (Zero Production)")
            if irr <= 0:
                errors.append(f"{p_key} (Zero Irradiation)")
        except:
            errors.append(f"{p_key} (Data Format Error)")

    if errors:
        print(f"❌ Verification failed for: {', '.join(errors)}")
        exit(1)

    print("✅ All plants reported successfully with valid weather data.")

if __name__ == "__main__":
    main()
