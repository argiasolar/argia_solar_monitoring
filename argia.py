# ... (początek pliku bez zmian: importy, get_service, safe_float)

def main():
    print("--- 🌟 ARGIA SOLAR MONITORING v4.7 (SiteID Integration) ---")
    service = get_service()
    
    yesterday_dt = datetime.datetime.now() - datetime.timedelta(days=1)
    date_iso = yesterday_dt.strftime('%Y-%m-%d')
    date_slash = yesterday_dt.strftime('%-m/%-d/%Y')

    # Pobranie Config_Plants
    config_res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="Config_Plants!A2:O25"
    ).execute()
    config_rows = config_res.get('values', [])

    plants_config = {}
    huawei_map = {}  # {SiteID: PlantKey}
    growatt_map = {} # {SiteID: PlantKey}

    for row in config_rows:
        if len(row) >= 10:
            p_key = str(row[0]).strip() # SLP1
            brand = str(row[1]).strip().upper()
            s_id = str(row[9]).strip()  # SiteID (Kolumna J)
            
            plants_config[p_key] = {
                'brand': brand,
                'kwp': safe_float(row[2]),
                'target': safe_float(row[7]),
                'name': str(row[8]).strip()
            }

            if brand == "HUAWEI":
                huawei_map[s_id] = p_key
            elif brand == "GROWATT":
                growatt_map[s_id] = p_key

    # 3. Pobieranie produkcji
    all_prod = {}
    if huawei_map:
        # Przekazujemy date_iso i mapę {s_id: p_key}
        all_prod.update(huawei.fetch_huawei_data(date_iso, huawei_map))
    if growatt_map:
        # Growatt też powinien dostać s_id
        all_prod.update(growatt.fetch_growatt_data(date_iso, growatt_map))

    # 4. Przetwarzanie i zapis
    final_data = []
    total_energy = 0

    for p_key, energy in all_prod.items():
        if p_key in plants_config:
            conf = plants_config[p_key]
            irr, clouds = weather.get_estimated_weather(p_key)
            
            forecast = round(conf['kwp'] * irr * 0.8, 2)
            pr = round(energy / (conf['kwp'] * irr), 3) if (irr > 0 and conf['kwp'] > 0) else 0
            
            final_data.append([
                date_slash, p_key, conf['name'], energy, irr, forecast, pr, conf['target'], clouds
            ])
            total_energy += energy

    if total_energy > 0:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range="RawData!A2",
            valueInputOption="USER_ENTERED", body={'values': final_data}
        ).execute()
        print(f"✅ Sync complete. Total: {total_energy} kWh")
    else:
        print("⚠️ No production data. Verifier will trigger retry.")

if __name__ == "__main__":
    main()
