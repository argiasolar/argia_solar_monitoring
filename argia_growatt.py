import os
import growattServer
import time

def fetch_growatt_data(yesterday_str, plants_to_fetch):
    """
    plants_to_fetch: słownik {SiteID: PlantKey} np. {'9275498': 'SLP1'}
    """
    print(f"🚀 [Growatt] Connecting to Growatt for {yesterday_str}...")
    
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    
    # Inicjalizujemy wyniki używając PlantKey (SLP1, SLP2 itd.)
    results = {p_key: 0 for p_key in plants_to_fetch.values()}
    
    api = growattServer.GrowattApi()
    api.server_url = 'http://server.growatt.com/'
    
    # 1. Pancerna pętla logowania (3 próby z Twojego zintegrowanego pliku)
    logged_in = False
    for i in range(3):
        try:
            api.login(user, password)
            logged_in = True
            break
        except Exception as e:
            print(f"⚠️ [Growatt] Login attempt {i+1} failed, retrying in 10s... ({e})")
            time.sleep(10)

    if not logged_in:
        print("❌ [Growatt] Could not login after 3 attempts. GitHub IP might be blocked.")
        return results

    # 2. Pobieranie danych dla każdej stacji przy użyciu SiteID
    try:
        # Pobieramy listę stacji raz jako backup
        login_id = api.session.auth[0] if api.session.auth else user
        all_plants = api.plant_list(login_id)
        
        for s_id, p_key in plants_to_fetch.items():
            val = 0
            try:
                # Próba A: Detale dnia (najdokładniejsze)
                data = api.plant_detail(s_id, yesterday_str)
                # Sprawdzamy oba możliwe klucze, których używa Growatt
                val = data.get('today_energy') or data.get('todayEnergy')
                
                # Próba B: Jeśli detail zawiedzie lub jest 0, szukamy w liście ogólnej
                if val is None or float(val) == 0:
                    # all_plants['data'] to lista stacji
                    plant_list_data = all_plants.get('data', [])
                    for p in plant_list_data:
                        if str(p.get('plantId')) == str(s_id):
                            val = p.get('todayEnergy', 0)
                            break
                
                results[p_key] = float(val or 0)
                print(f"   📊 [Growatt] {p_key} ({s_id}): {results[p_key]} kWh")
                
            except Exception as e:
                print(f"   ⚠️ [Growatt] Could not fetch data for {p_key} ({s_id}): {e}")
                results[p_key] = 0

        return results

    except Exception as e:
        print(f"❌ [Growatt] General API Error: {e}")
        return results
