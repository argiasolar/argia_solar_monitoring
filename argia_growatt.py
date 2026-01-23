import os

def fetch_growatt_data(target_date, plant_keys):
    """Zwraca realne dane lub 0 w przypadku błędu."""
    print(f"🚀 [Growatt] Attempting login for {target_date}...")
    
    # Poprawione na Twoje rzeczywiste nazwy w Secrets
    user = os.environ.get('GROWATT_USERNAME')
    password = os.environ.get('GROWATT_PASSWORD')
    
    if not user or not password:
        print("❌ [Growatt] Error: GROWATT_USERNAME or PASSWORD missing in environment.")
        return {key: 0 for key in plant_keys}

    # Symulacja błędu do czasu wdrożenia API
    return {key: 0 for key in plant_keys}
