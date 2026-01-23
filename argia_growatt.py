import os
import growattServer

def fetch_growatt_data(target_date, plant_keys):
    """Pobiera REALNE dane z Growatt Server."""
    print(f"🚀 [Growatt] Connecting to Growatt Server for {target_date}...")
    
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    
    results = {key: 0 for key in plant_keys}
    
    try:
        api = growattServer.GrowattApi()
        login_res = api.login(user, password)
        user_id = login_res['user_id']
        
        plant_list = api.plant_list(user_id)
        
        count = 0
        for plant in plant_list.get('data', []):
            p_name = plant.get('plantName')
            # Szukamy dopasowania nazwy stacji do Twojego klucza (np. SLP1)
            if p_name in plant_keys:
                p_id = plant.get('plantId')
                # Pobieramy dane historyczne (format daty dla Growatt to zazwyczaj YYYY-MM-DD)
                history = api.plant_history(p_id, target_date)
                energy = float(history.get('eToday', 0))
                results[p_name] = energy
                count += 1
        
        print(f"✅ [Growatt] Fetched real data for {count} plants.")
        
    except Exception as e:
        print(f"❌ [Growatt] API Error: {str(e)}")
        
    return results
