import os
import growattServer

def fetch_growatt_data(target_date, plant_keys):
    print(f"🚀 [Growatt] Connecting to Growatt Server for {target_date}...")
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    results = {key: 0 for key in plant_keys}
    
    try:
        # Poprawka: Inicjalizacja bez spornego argumentu
        api = growattServer.GrowattApi()
        api.server_url = "https://server.growatt.com/"
        
        login_res = api.login(user, password)
        if not login_res or 'user_id' not in login_res:
            print("❌ [Growatt] Login failed.")
            return results
            
        plants = api.plant_list(login_res['user_id'])
        
        # Obsługa struktury danych z v3.2
        plant_data = plants.get('data') if isinstance(plants.get('data'), list) else []
        
        for plant in plant_data:
            raw_name = plant.get('plantName', '').strip()
            p_id = plant.get('plantId')
            
            matched_key = next((k for k in plant_keys if k.lower() == raw_name.lower()), None)
            
            if matched_key:
                history = api.plant_history(p_id, target_date)
                energy = float(history.get('eToday', 0))
                results[matched_key] = energy
                print(f"   📊 [Growatt] {matched_key}: {energy} kWh")
        
        print(f"✅ [Growatt] Sync complete.")

    except Exception as e:
        print(f"❌ [Growatt] API Error: {str(e)}")
        
    return results
