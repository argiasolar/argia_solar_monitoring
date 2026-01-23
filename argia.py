import os
import json
import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Importy naszych modułów
import argia_weather as weather
import argia_huawei as huawei
import argia_growatt as growatt

SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def get_google_service():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_info)
    return build('sheets', 'v4', credentials=creds)

def main():
    print("--- 🌟 ARGIA SOLAR MONITORING v4.0 ---")
    service = get_google_service()
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 1. Pobierz Config
    print("📡 [Config] Fetching plant configurations...")
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

    # 2. Pobierz Produkcję
    huawei_keys = [k for k, v in plants_config.items() if v['brand'] == 'HUAWEI']
    growatt_keys = [k for k, v in plants_config.items() if v['brand'] == 'GROWATT']
    
    prod_data = {}
    prod_data.update(huawei.fetch_huawei_data(yesterday, huawei_keys))
    prod_data.update(growatt.fetch_growatt_data(yesterday, growatt_keys))

    # 3. Przetwarzanie i Pogoda
    final_rows = []
    for key, energy in prod_data.items():
        if key in plants_config:
            p = plants_config[key]
            irr, clouds = weather.get_estimated_weather(key)
            
            forecast = round(p['kwp'] * irr * p['target'], 2)
            real_pr = round(energy / (p['kwp'] * irr), 3) if irr > 0 else 0
            
            final_rows.append([
                yesterday, key, p['name'], energy, irr, forecast, real_pr, p['target'], clouds
            ])

    # 4. Wysyłka do Google
    if final_rows:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range="RawData!A2",
            valueInputOption="USER_ENTERED", body={'values': final_rows}
        ).execute()
        print(f"✅ [Success] Synced {len(final_rows)} rows to Google Sheets.")
    else:
        print("❌ [Error] No data to sync.")

if __name__ == "__main__":
    main()
