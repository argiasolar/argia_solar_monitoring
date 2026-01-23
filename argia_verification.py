import os
import sys
import json
import datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
TZ_NAME = os.environ.get("TZ_NAME", "America/Mexico_City")

def get_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)

def fmt_mdy(d: datetime.date) -> str:
    return f"{d.month}/{d.day}/{d.year}"

def safe_float(v) -> float:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0

def main():
    if not SHEET_ID:
        print("❌ Missing GOOGLE_SHEET_ID")
        sys.exit(1)

    tz = ZoneInfo(TZ_NAME)
    date_slash = fmt_mdy((datetime.datetime.now(tz=tz) - datetime.timedelta(days=1)).date())
    print(f"🔍 [Verification] Checking date: {date_slash}")

    service = get_service()

    # policz ile plantów jest w config
    cfg = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Config_Plants!A2:A200").execute().get("values", [])
    plant_count = len([r for r in cfg if r and str(r[0]).strip()])

    rows = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:J5000").execute().get("values", [])
    yrows = [r for r in rows if len(r) >= 2 and str(r[0]).strip() == date_slash]

    if not yrows:
        print("❌ No rows found for target date. Sync likely failed.")
        sys.exit(1)

    if plant_count and len(yrows) != plant_count:
        print(f"⚠️ Row count mismatch. Config plants={plant_count}, RawData rows={len(yrows)} (date={date_slash})")

    dummy = []
    bad = []
    for r in yrows:
        pk = r[1]
        kwh = safe_float(r[3]) if len(r) > 3 else 0
        irr = safe_float(r[4]) if len(r) > 4 else 0
        transfer = (r[9] if len(r) > 9 else "").strip().upper()

        if transfer == "NO":
            dummy.append(pk)
        if kwh <= 0 or irr <= 0:
            bad.append(pk)

    if bad:
        print(f"❌ Found non-positive kWh/irr for: {bad}")
        sys.exit(1)

    if dummy:
        print(f"🟠 Dummy data present (Transfer=NO) for: {dummy}")
    else:
        print("✅ All plants have real data (Transfer=YES).")

    sys.exit(0)

if __name__ == "__main__":
    main()
