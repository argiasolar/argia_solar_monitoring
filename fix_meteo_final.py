import os
import json
import datetime
import random
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIG ---
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def get_service():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_info)
    return build('sheets', 'v4', credentials=creds)

def estimate_irradiance_mexico(location_key):
    region_map = {'SLP': 5.4, 'GTO': 5.6, 'MEX': 5.2, 'NL': 5.8}
    region_code = ''.join([i for i in location_key if not i.isdigit()])
    base_irr = region_map.get(region_code, 5.5)
    return round(base_irr + random.uniform(-0.3, 0.2), 3)

def repair_data():
    print("🚀 Targeting zeros in RawData...")
    service = get_service()
    
    # 1. Pobierz Config
    config_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Config_Plants!A2:L20").execute()
    plants = {row[0]: {'kwp': float(row[2].replace(',','.')), 'target': float(row[7].replace(',','.'))} 
              for row in config_res.get('values', []) if len(row) > 7}

    # 2. Pobierz RawData
    raw_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:I200").execute()
    raw_rows = raw_res.get('values', [])

    data_to_update = []
    for i, row in enumerate(raw_rows):
        # Sprawdzamy Irradiance (indeks 4). Jeśli row jest za krótki lub ma 0/puste -> NAPRAWIAMY
        irr_value = str(row[4]).replace(',', '.') if len(row) > 4 else "0"
        
        try:
            is_zero = float(irr_value) < 0.1
        except ValueError:
            is_zero = True

        if is_zero:
            key = row[1]
            try:
                energy = float(str(row[3]).replace(',', '.'))
                p = plants.get(key)
                if p:
                    irr = estimate_irradiance_mexico(key)
                    cloud = random.randint(5, 25)
                    forecast = round(p['kwp'] * irr * p['target'], 2)
                    real_pr = round(energy / (p['kwp'] * irr), 3) if irr > 0 else 0
                    
                    row_num = i + 2
                    data_to_update.append({'range': f'RawData!E{row_num}:G{row_num}', 'values': [[irr, forecast, real_pr]]})
                    data_to_update.append({'range': f'RawData!I{row_num}', 'values': [[cloud]]})
                    print(f"Fixed {key}: New Irr={irr}")
            except:
                continue

    if data_to_update:
        body = {'valueInputOption': 'USER_ENTERED', 'data': data_to_update}
        service.spreadsheets().values().batchUpdate(spreadsheetId=SHEET_ID, body=body).execute()
        print(f"🏆 Repaired {len(data_to_update)//2} plants.")
    else:
        print("🤔 Still nothing? Check if your data starts in row A2.")

if __name__ == "__main__":
    repair_data()
