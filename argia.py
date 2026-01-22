import os
import json
import gspread
import requests
import datetime
import time
from google.oauth2.service_account import Credentials
import growattServer

HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com/thirdData"

def get_smart_weather_and_cloud(lat, lon, date_str, plants_config, current_p_key):
    def fetch(la, lo):
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": la, "longitude": lo, 
            "start_date": date_str, "end_date": date_str, 
            "daily": ["shortwave_radiation_sum", "cloud_cover_mean"], # Dodano chmury
            "timezone": "auto"
        }
        try:
            res = requests.get(url, params=params, timeout=15)
            d = res.json()['daily']
            return round(d['shortwave_radiation_sum'][0] / 3.6, 3), d['cloud_cover_mean'][0]
        except: return 0, 0

    irr, cloud = fetch(lat, lon)
    if irr > 0: return irr, cloud
    
    # INTERPOLACJA (Punkt A/C Fix)
    neighbors = ["SLP1", "GTO1", "MEX1"]
    i_vals, c_vals = [], []
    for n_key in neighbors:
        if n_key == current_p_key or n_key not in plants_config: continue
        n_irr, n_cloud = fetch(plants_config[n_key]['Latitude'], plants_config[n_key]['Longtitude'])
        if n_irr > 0:
            i_vals.append(n_irr)
            c_vals.append(n_cloud)
    
    avg_irr = round(sum(i_vals) / len(i_vals), 3) if i_vals else 0
    avg_cloud = round(sum(c_vals) / len(c_vals), 1) if c_vals else 0
    return avg_irr, avg_cloud

# ... (funkcje fetch_huawei_data i fetch_growatt_data pozostają bez zmian jak w poprzedniej wersji) ...

def main():
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"🚀 Sync v3.0 (Weather + Clouds) za dzień: {yesterday}")
    
    # 1. GSheets Setup
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(os.environ['GOOGLE_SHEET_ID'])
    config_sheet = sh.worksheet("Config_Plants")
    raw_sheet = sh.worksheet("RawData")
    
    plants_config = {p['Plantkey']: p for p in config_sheet.get_all_records()}
    huawei_energies = fetch_huawei_data(yesterday)
    
    # 2. Growatt Login
    g_api = growattServer.GrowattApi()
    g_api.server_url = 'http://server.growatt.com/'
    try: g_api.login(os.environ['GROWATT_USERNAME'], os.environ['GROWATT_PASSWORD'])
    except: print("❌ Growatt Login Failed")

    final_rows = []
    total_energy = 0
    
    for p_key in sorted(plants_config.keys()):
        conf = plants_config[p_key]
        brand, s_id = str(conf['Brand']).upper(), str(conf['SiteID']).strip()
        
        energy = huawei_energies.get(s_id, 0) if brand == "HUAWEI" else fetch_growatt_data(g_api, s_id, yesterday)
        
        # Pobieramy nasłonecznienie ORAZ chmury
        irr, cloud = get_smart_weather_and_cloud(conf['Latitude'], conf['Longtitude'], yesterday, plants_config, p_key)
        
        kwp = float(conf['kWp_DC'] or 0)
        possible = round(kwp * irr * 0.85, 2)
        pr = round(energy / (kwp * irr), 3) if (irr > 0 and kwp > 0) else 0
        
        total_energy += energy
        # Nowa struktura wiersza: dodajemy Cloud Cover na końcu (kolumna I)
        final_rows.append([yesterday, p_key, conf['CustomerName'], energy, irr, possible, pr, conf['PR_Target'], cloud])
        print(f"✅ {p_key}: {energy} kWh | Cloud: {cloud}%")

    if total_energy > 0:
        raw_sheet.append_rows(final_rows)
        print(f"💾 Zapisano w arkuszu.")
    else:
        print("❌ Suma energii 0 - nie zapisano.")

if __name__ == "__main__": main()
