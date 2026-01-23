# argia_verification.py
import os, json, datetime as dt
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
    now_local = dt.datetime.utcnow() + dt.timedelta(hours=-6)
    today_slash = f"{now_local.month}/{now_local.day}/{now_local.year}"
    print(f"🔍 [Verification] Checking: {today_slash}")

    res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:J5000").execute()
    rows = [r for r in res.get("values", []) if len(r) > 0 and r[0] == today_slash]

    if not rows:
        print("❌ No data for today.")
        exit(1)

    print("✅ Today's sync verified.")

if __name__ == "__main__":
    main()
