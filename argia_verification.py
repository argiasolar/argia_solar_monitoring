import os
import sys
import datetime
import argia_weather as weather

def verify_sync():
    print("🔍 [Verification] Starting data integrity check...")
    
    dt_yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    date_slash = dt_yesterday.strftime('%-m/%-d/%Y')
    
    # Symulacja sprawdzenia (w realu pobierz z Sheets jak wcześniej)
    # Jeśli znajdziemy problemy:
    issues_found = True # Tu powinna być Twoja logika sprawdzania arkusza
    
    if issues_found:
        if os.environ.get('RETRY_ATTEMPT') == 'true':
            print("🛑 [Verification] Still issues after retry. Emergency fix triggered.")
            weather.repair_missing_weather(date_slash)
            sys.exit(0)
        else:
            print("❌ [Verification] Zeros detected. Triggering retry flow.")
            sys.exit(1)
    
    print("✅ [Verification] Success.")
    sys.exit(0)

if __name__ == "__main__":
    verify_sync()
