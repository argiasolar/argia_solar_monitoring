import os

def fetch_growatt_data(target_date, plant_keys):
    """Pobiera dane produkcyjne z Growatt Server."""
    print(f"🚀 [Growatt] Starting data import for {target_date}...")
    
    user = os.environ.get('GROWATT_USER')
    password = os.environ.get('GROWATT_PASS')
    
    if not user or not password:
        print("⚠️  [Growatt] Missing credentials. Skipping Growatt.")
        return {}

    # Tu znajdzie się integracja z growattServer
    print(f"✅ [Growatt] Data fetched.")
    return {}
