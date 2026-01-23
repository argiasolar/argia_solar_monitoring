import os
import growattServer

def fetch_growatt_data(target_date, plant_keys):
    print(f"🚀 [Growatt] Connecting to Server for {target_date}...")
    
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    
    if not user or not password:
        print("❌ [Growatt] Auth credentials missing.")
        return {key: 0 for key in plant_keys}

    try:
        api = growattServer.GrowattApi()
        login_response = api.login(user, password)
        
        # Pobieramy listę instalacji przypisanych do konta
        plant_list = api.plant_list(login_response['user_id'])
        
        results = {}
        for plant in plant_list['data']:
            plant_id = plant['plantId']
            # Pobieramy dane historyczne dla konkretnego dnia
            history = api.plant_history(plant_id, target_date)
            # Wyciągamy sumaryczną produkcję (eToday)
            energy = float(history.get('eToday', 0))
            
            # Mapujemy ID Growatta na Twoje klucze (np. SLP1)
            # Tutaj musimy znać powiązanie ID z Twoim kluczem
            results[plant['plantName']] = energy 
            
        print(f"✅ [Growatt] Fetched data for {len(results)} plants.")
        return results
    except Exception as e:
        print(f"❌ [Growatt] Connection error: {e}")
        return {key: 0 for key in plant_keys}
