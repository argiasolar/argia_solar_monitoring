import os
import growattServer
import time

def fetch_growatt_data(yesterday_str, plant_keys):
    """Logika z pliku zintegrowanego: 3x Retry + Detail Backup."""
    print(f"🚀 [Growatt] Connecting with 3-attempt retry logic...")
    results = {key: 0 for key in plant_keys}
    
    api = growattServer.GrowattApi()
    api.server_url = 'http://server.growatt.com/'
    
    # 1. Pancerna pętla logowania z Twojego pliku
    growatt_ok = False
    for i in range(3):
        try:
            api.login(os.environ['GROWATT_USERNAME'], os.environ['GROWATT_PASSWORD'])
            growatt_ok = True
            break
        except:
            print(f"⚠️ Growatt login attempt {i+1} failed, retrying...")
            time.sleep(10)

    if not growatt_ok:
        return results

    # 2. Pobieranie danych dla każdej stacji (Zabezpieczone)
    for s_id in plant_keys:
        try:
            # Próba A: Detail
            data = api.plant_detail(s_id, yesterday_str)
            val = data.get('today_energy') or data.get('todayEnergy')
            
            # Próba B: Backup z listy ogólnej
            if not val:
                plants = api.plant_list(api.session.auth[0])
                for p in plants:
                    if str(p['plantId']) == str(s_id):
                        val = p['todayEnergy']
                        break
            results[s_id] = float(val or 0)
        except:
            results[s_id] = 0
            
    print("✅ [Growatt] Data imported with retry protection.")
    return results
