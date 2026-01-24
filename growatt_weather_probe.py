import growattServer
import logging

class GrowattWeatherProbe:
    def __init__(self, username, password):
        self.api = growattServer.GrowattApi()
        try:
            self.login_info = self.api.login(username, password)
            self.user_id = self.login_info['user']['id']
        except Exception as e:
            logging.error(f"Login failed: {e}")
            raise

    def get_weather_data(self, plant_id):
        """
        Extracts irradiance and cloud cover.
        Note: Growatt often stores weather data in 'env_info' or 'weather_data' 
        depending on the specific hardware/region.
        """
        try:
            # Fetch full plant status
            plant_info = self.api.plant_info(plant_id)
            
            # Extracting specific weather station attributes
            # Standard Growatt weather station fields:
            # 'irradiance' (W/m2), 'cloud' (%)
            weather = {
                "plant_name": plant_info.get("plantName"),
                "irradiance": plant_info.get("irradiance", 0.0),
                "cloud_cover": plant_info.get("cloud", 0), 
                "timestamp": plant_info.get("lastUpdateTime")
            }
            return weather
        except Exception as e:
            logging.error(f"Error fetching data for plant {plant_id}: {e}")
            return None
