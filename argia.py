import os
import json
import gspread
import requests
import datetime
import time
from google.oauth2.service_account import Credentials
import growattServer

# --- KONFIGURACJA ---
HUAWEI_BASE_URL = "https://la5.fusionsolar.huawei.com"

# --- FUNKCJE POMOCNICZE ---
def parse_energy_value(value_str):
    if not value_str: return 0.0
    v = str(value_str).lower().strip()
    try:
        if 'mwh' in v:
            return round(float(v.replace('mwh', '').strip()) * 1000, 2)
        elif 'kwh' in v:
            return round(float(v.replace('kwh', '').strip()), 2)
        return float(v)
    except:
        return 0.0

def get_weather_data(lat, lon, date_str):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": date_str, "end_date": date_str,
        "daily": "shortwave_radiation_sum", "timezone": "auto"
    }
    for attempt in range(4):
        try:
            res = requests.get(url, params=params, timeout=45)
            res.raise_for_status()
            data = res.json()
            if 'daily' in data and data['daily']['shortwave_radiation_sum'][0] is not None:
                mj_m2 = data['daily']['shortwave_radiation_sum'][0]
                return round(mj_m2 / 3.6, 3)
            return 0
        except Exception as e:
            print(f"⚠️ Próba {attempt+1} pogoda ({lat},{lon}): {e}")
            time.sleep(15)
    return 0

# --- MODUŁ GROWATT ---
def fetch_growatt_data(target_plant_id, date_str):
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    api = growattServer.GrowattApi()
    api.server_url = 'http://server.growatt.com/'
    api.session.headers.update({'User-Agent': 'Mozilla/5.0'})
    try:
        login_res = api.login(user, password)
        user_id = login_res.get('user_id') or login_res.get('userId') or login_res.get('data', {}).get('userId')
        plants_response = api.plant_list(user_id)
        plants_list = plants_response if isinstance(plants_response, list) else plants_response.get('data', [])
        for p in plants_list:
            if str(p.get('plantId')) == str(target_plant_id):
                raw_energy = p.get('todayEnergy') or p.get('energy_today') or "0"
                return parse_energy_value(raw_energy)
        return 0
    except Exception as e:
        print(f"❌ Growatt Error: {e}")
        return 0

# --- MODUŁ HUAWEI (Wzmocniona Sesja) ---
def get_huawei_session():
    user = os.environ.get("HUAWEI_USERNAME")
    pw = os.environ.get("HUAWEI_PASSWORD")
    url = f"{HUAWEI_BASE_URL}/thirdData/login"
    
    session = requests.Session()
    session.headers.update({
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    
    try:
        print(f"📡 Próba logowania Huawei (LA5) dla: {user}")
        r = session.post(url, json={"userName": user, "systemCode": pw}, timeout=30)
        
        # Jeśli status nie jest 200, r.json() może wywalić błąd, więc sprawdzamy:
        if r.status_code != 200:
            print(f"❌ Huawei Login HTTP {r.status_code}: {r.text[:200]}")
            return None, None

        resp_json = r.json()
        token = resp_json.get("data", {}).get("xsrfToken")
        if token:
            print("✅ Huawei: Zalogowano pomyślnie.")
            return session, token
        else:
            print(f"⚠️ Huawei: Brak tokena. Odp: {resp_json}")
    except Exception as e:
        print(f"❌ Huawei Login Exception: {e}")
    return None, None

def fetch_huawei_energy(station_code, session, token, date_str):
    url = f"{HUAWEI_BASE_URL}/thirdData/getStationKpi"
    formatted_date = date_str.replace("-", "")
    session
