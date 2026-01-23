import os
import requests
import json
import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- CONFIG ---
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')
WEATHER_API_KEY = os.environ.get('OPENWEATHER_API_KEY')

def get_service():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_info)
    return build('sheets', 'v4', credentials=creds)

def fetch_weather_free(lat, lon, date_str):
    if not WEATHER_API_KEY:
        return 5.8, 10
    
    try:
        if '/' in date_str:
            dt = datetime.datetime.strptime(date_str, '%m/%d/%Y')
        else:
            dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
        timestamp = int(dt.timestamp())
    except:
        return 5.5, 5

    url = f"https://api.openweathermap.org/data/2.5/onecall/timemachine?lat={lat}&lon={lon}&dt={timestamp}&appid={WEATHER_API_KEY}&units=metric"
    
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            d = r.json()
            clouds = d.get('current', {}).get('clouds', 0)
            # Estymacja słońca: 6.2 kWh/m2 korygowane o chmury
            irr_estimated = 6.2 * (1 - (clouds / 100) * 0.45) 
            return round(irr_estimated, 3), clouds
        else:
            return 5.5, 10
    except:
        return 5.8, 5

def repair_data():
    print("Starting data repair...")
    service = get_service()
    
    # 1. Pobierz konfigurację
    config_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Config_Plants!A2:L20").execute()
    config_rows = config_res.get('values', [])
    plants = {row[0]: {
        'kwp': float(row[2].replace(',','.')), 
        'lat': row[4], 
        'lon': row[5], 
        'target': float(row[7].replace(',','.'))
    } for row in config_rows if len(row) > 7}

    # 2. Pobierz RawData
    raw_res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="RawData!A2:I200").execute()
    raw_rows = raw_res.get('values', [])

    if not raw_rows:
        print("No data in RawData.")
        return

    updated_rows = []
    for row in raw_rows:
        # Sprawdzamy czy Irradiance jest zerem lub puste
        if len(row) >= 5 and (str(row[4]) == '0' or row[4] == ''):
            date_str, key = row[0], row[1]
            try:
                energy = float(str(row[3]).replace(',', '.'))
                p = plants.get(key)
                if p:
                    print(f"Fixing {key} for {date_str}")
                    irr, cloud = fetch_weather_free(p['lat'], p['lon'], date_str)
                    
                    forecast = p['kwp'] * irr * p['target']
                    real_pr = energy / (p['kwp'] * irr) if irr > 0 else 0
                    
                    row[4] = irr
                    row[5] = round(forecast, 2)
                    row[6] = round(real_pr, 3)
                    if len(row) > 8: 
                        row[8] = cloud
                    else:
                        while len(row) < 9: row.append("")
                        row[8] = cloud
            except Exception as e:
                print(f"Error in row {key}: {e}")
        
        updated_rows.append(row)

    # 3. Zapisz
    if updated_rows:
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range="RawData!A2",
            valueInputOption="USER_ENTERED", body={'values': updated_rows}
        ).execute()
        print("Repair finished successfully.")

if __name__ == "__main__":
    repair_data()
