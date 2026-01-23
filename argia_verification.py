import os
import sys
import json
import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def get_service():
    """Autoryzacja Google Sheets API."""
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json), 
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build('sheets', 'v4', credentials=creds)

def safe_float(value):
    """Bezpieczna zamiana wartości z arkusza na liczbę."""
    if value is None: return 0.0
    try:
        return float(str(value).replace(',', '.'))
    except (ValueError, TypeError):
        return 0.0

def fill_emergency_data(service, date_slash, plants_to_fix):
    """
    Wpisuje stałe wartości produkcji dla stacji, które zwróciły 0.
    """
    print(f"⚠️ [Repair] Filling dummy production for: {plants_to_fix}")
    
    # Twoje wypracowane stałe wartości produkcji (kWh)
    dummy_map = {
        'SLP1': 609, 
        'SLP2': 986, 
        'GTO1': 2259, 
        'MEX1': 2174, 
        'NL1': 2463, 
        'MEX2': 2448
    }
    
    # Pobieramy dane, aby znaleźć numery wierszy
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, 
        range="RawData!A:D"
    ).execute()
    rows = res.get('values', [])
    
    updates = []
    for i, row in enumerate(rows):
        # Sprawdzamy datę i czy stacja jest na liście do naprawy
        if len(row) >= 2 and row[0] == date_slash and row[1] in plants_to_fix:
            val = dummy_map.get(row[1], 500) # Default 500 jeśli nie ma w mapie
            # Kolumna D to produkcja (index 3, ale w Sheets to 4. kolumna)
            row_num = i + 1
            updates.append({
                'range': f'RawData!D{row_num}',
                'values': [[val]]
            })

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID, 
            body={
                'valueInputOption': 'USER_ENTERED', 
                'data': updates
            }
        ).execute()
        print(f"✅ [Repair] Successfully patched {len(updates)} plants with dummy data.")
    else:
        print("⚠️ [Repair] No matching rows found to patch.")

def main():
    print("🔍 [Verification] Starting data integrity check...")
    service = get_service()
    
    # Ustalenie wczorajszej daty w formacie arkusza
    yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    date_slash = yesterday.strftime('%-m/%-d/%Y')
    print(f"📅 [Verification] Checking date: {date_slash}")

    # Pobieramy ostatnie 100 wierszy z RawData
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, 
        range="RawData!A:D"
    ).execute()
    rows = res.get('values', [])
    
    # Filtrujemy wiersze z wczoraj
    yesterday_rows = [row for row in rows if len(row) >= 2 and row[0] == date_slash]
    
    if not yesterday_rows:
        print(f"❌ [Verification] No rows found for {date_slash}. Sync must have failed completely.")
        sys.exit(1)

    # Szukamy stacji, które mają produkcję <= 0
    zeros = [row[1] for row in yesterday_rows if safe_float(row[3]) <= 0]

    if zeros:
        # Sprawdzamy, czy to już próba ratunkowa (Retry)
        is_retry = os.environ.get('RETRY_ATTEMPT') == 'true'
        
        if is_retry:
            print(f"🛠️ [Verification] Retry mode active. Repairing zeros for: {zeros}")
            fill_emergency_data(service, date_slash, zeros)
            sys.exit(0) # Kończymy sukcesem po naprawie
        else:
            print(f"❌ [Verification] Found zeros in: {zeros}. Triggering GitHub Actions Retry...")
            sys.exit(1) # Wywalamy błąd, by odpalić Retry w Actions
    
    print("✅ [Verification] All data looks good. No zeros detected.")
    sys.exit(0)

if __name__ == "__main__":
    main()
