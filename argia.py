import os
import json
import datetime
import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build

import argia_weather as weather
import argia_huawei as huawei
import argia_growatt as growatt

SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def get_service():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds = service_account.Credentials.from_service_account_info(json.loads(creds_json))
    return build('sheets', 'v4', credentials=creds)

def main():
    print("--- 🌟 ARGIA SOLAR MONITORING v4.0 (Logic v3.2) ---")
    service = get_service()
    
    # Daty dla API (ISO) i dla arkusza (Slash)
    yesterday_dt = datetime.datetime.now() - datetime.timedelta(days=1)
    date_iso = yesterday_dt.strftime('%Y-%m-%d')
    date_slash = yesterday_dt.strftime('%-m/%-d/%Y')

    # 1. Pobierz konfigurację stacji
    config_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Config_Plants!A2:L25").execute()
    config_rows = config_res.get('values', [])
    
    plants_config = {}
    for row in config_rows:
        if len(row) >= 8:
            plants_config[row[0]] = {
                'brand': row[1].upper(),
                'kwp': float(row[2].replace(',', '.')),
                'target': float(row[7].replace(',', '.')),
                'name': row[8] if len(row) > 8 else row[0]
            }

    # 2. Podział na marki i pobranie danych
    huawei_keys = [k for k, v in plants_config.items() if v['brand'] == 'HUAWEI']
    growatt_keys = [k for k, v in plants_config.items() if v['brand'] == 'GROWATT']
    
    all_prod = {}
    if huawei_keys:
        all_prod.update(huawei.fetch_huawei_data(date_iso, huawei_keys))
    if growatt_keys:
        all_prod.update(growatt.fetch_growatt_data(date_iso, growatt_keys))

    # 3. Przetwarzanie końcowe (Pogoda + PR)
    final_data = []
    for key, energy in all_prod.items():
        if key in plants_config:
            p = plants_config[key]
            irr, clouds = weather.get_estimated_weather(key)
            forecast = round(p['kwp'] * irr * p['target'], 2)
            real_pr = round(energy / (p['kwp'] * irr), 3) if irr > 0 and energy > 0 else 0
            
            final_data.append([
                date_slash, key, p['name'], energy, irr, forecast, real_pr, p['target'], clouds
            ])

    # 4. Eksport do Google Sheets
    if final_data:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range="RawData!A2",
            valueInputOption="USER_ENTERED", body={'values': final_data}
        ).execute()
        print(f"✅ [Success] Synced {len(final_data)} rows.")

if __name__ == "__main__":
    main()
