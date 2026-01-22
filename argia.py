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
    
    payload = {"userName": user, "systemCode": pw}
    
    try:
        print(f"📡 Próba logowania (LA5) dla użytkownika: {user}")
        r = session.post(url, json=payload, timeout=30)
        
        # Logujemy status dla debugowania
        print(f"📡 Status HTTP: {r.status_code}")
        
        try:
            resp_json = r.json()
        except:
            print(f"❌ Serwer nie zwrócił JSON-a. Odpowiedź: {r.text[:100]}")
            return None, None

        token = resp_json.get("data", {}).get("xsrfToken")
        if token:
            print("✅ Huawei: Zalogowano pomyślnie.")
            # Zwracamy sesję i token, bo sesja trzyma ciasteczka!
            return session, token
        else:
            print(f"⚠️ Huawei: Brak tokena. Odpowiedź: {resp_json}")
    except Exception as e:
        print(f"❌ Huawei Login Exception: {e}")
    return None, None

def fetch_huawei_energy(station_code, session, token, date_str):
    url = f"{HUAWEI_BASE_URL}/thirdData/getStationKpi"
    formatted_date = date_str.replace("-", "")
    
    # Dodajemy token do nagłówków sesji
    session.headers.update({'xsrf-token': token})
    
    payload = {"stationCodes": station_code, "collectTime": formatted_date}
    try:
        r = session.post(url, json=payload, timeout=30)
        data = r.json().get("data", [])
        if data and len(data) > 0:
            return float(data[0].get("dayPower", 0))
        return 0
    except Exception as e:
        print(f"❌ Huawei Data Error ({station_code}): {e}")
        return 0
