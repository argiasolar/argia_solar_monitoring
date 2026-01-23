import os
import growattServer

def fetch_growatt_data(target_date, plant_keys):
    """Implementacja v3.2: Stabilne połączenie growattServer z logiką eToday."""
    print(f"🚀 [Growatt] Connecting to Growatt for {target_date}...")
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    results = {key: 0 for key in plant_keys}
    
    try:
        # v3.2: Inicjalizacja z wymuszeniem server_url
        api = growattServer.GrowattApi(True)
        api.server_url = "https://server.growatt.com/"
        
        login = api.login(user, password)
        if not login or 'user_id' not in login:
            print("⚠️ [Growatt] Login failed.")
            return results
            
        plants = api.plant_list(login['user_id'])
        
        for plant in plants.get('data', []):
            name = plant.get('plantName')
            # v3.2: Elastyczne dopasowanie nazw (strip i case-insensitive)
            matched_key = next((k for k in plant_keys if k.lower() == name.lower().strip()), None)
            
            if matched_key:
                # Pobieranie eToday z historii stacji
                hist = api.plant_history(plant.get('plantId'), target_date)
                energy = float(hist.get('eToday', 0))
                results[matched_key] = energy
                
        print(f"✅ [Growatt] Real data imported successfully.")
    except Exception as e:
        print(f"❌ [Growatt] Error: {str(e)}")
        
    return results
