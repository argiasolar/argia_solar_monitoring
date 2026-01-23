import os
import growattServer

def fetch_growatt_data(target_date, plant_keys):
    """
    Sprawdzone rozwiązanie z v3.2: Logowanie przez server.growatt.com 
    z poprawką na nazewnictwo i pobieranie eToday.
    """
    print(f"🚀 [Growatt] Connecting to Growatt Server for {target_date}...")
    
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    results = {key: 0 for key in plant_keys}
    
    try:
        # Nasze wywalczone ustawienie: True (global) i konkretny URL
        api = growattServer.GrowattApi(add_not_archived=True)
        api.server_url = "https://server.growatt.com/"
        
        login_res = api.login(user, password)
        if not login_res or 'user_id' not in login_res:
            print("❌ [Growatt] Login failed. Check credentials.")
            return results
            
        # Pobieramy listę wszystkich stacji przypisanych do użytkownika
        plants = api.plant_list(login_res['user_id'])
        
        for plant in plants.get('data', []):
            raw_name = plant.get('plantName', '').strip()
            p_id = plant.get('plantId')
            
            # Dopasowanie nazwy (niezależnie od wielkości liter i spacji)
            matched_key = next((k for k in plant_keys if k.lower() == raw_name.lower()), None)
            
            if matched_key:
                # Wypracowana metoda: pobieramy historię dla konkretnego dnia
                history = api.plant_history(p_id, target_date)
                # eToday to klucz, który zawsze zwracał nam właściwą produkcję
                energy = float(history.get('eToday', 0))
                results[matched_key] = energy
                print(f"   📊 [Growatt] {matched_key}: {energy} kWh")
        
        print(f"✅ [Growatt] Successfully imported data for matched plants.")

    except Exception as e:
        print(f"❌ [Growatt] Error during API communication: {str(e)}")
        
    return results
