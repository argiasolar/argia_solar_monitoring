import random
import datetime

def get_estimated_weather(location_key):
    """
    Estymuje nasłonecznienie na podstawie lokalizacji w Meksyku.
    Zwraca (irradiance, clouds).
    """
    # Średnie statystyczne PSH (Peak Sun Hours) dla regionów
    region_map = {
        'SLP': 5.4,  # San Luis Potosi
        'GTO': 5.6,  # Leon/Guanajuato
        'MEX': 5.2,  # Mexico City
        'NL':  5.8   # Monterrey
    }
    
    # Wyciąganie kodu regionu z klucza (np. SLP1 -> SLP)
    region_code = ''.join([i for i in location_key if not i.isdigit()])
    base_irr = region_map.get(region_code, 5.5)
    
    # Dodanie losowości dla realizmu (jitter)
    irr = round(base_irr + random.uniform(-0.4, 0.3), 3)
    clouds = random.randint(5, 30)
    
    print(f"   [Weather] Location: {location_key} -> Est. Irr: {irr}, Clouds: {clouds}%")
    return irr, clouds
