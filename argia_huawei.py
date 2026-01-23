# argia_huawei.py
import os
import requests
import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Mexico_City")
DEFAULT_BASE = "https://la5.fusionsolar.huawei.com/thirdData"


def _base_url():
    return (os.environ.get("HUAWEI_BASE_URL") or DEFAULT_BASE).rstrip("/")


def _to_collect_time_ms(date_iso: str) -> int:
    y, m, d = [int(x) for x in date_iso.split("-")]
    dt = datetime.datetime(y, m, d, 0, 0, 0, tzinfo=TZ)
    return int(dt.timestamp() * 1000)


def _pick_energy_value(data_item_map: dict) -> float:
    # typowe klucze spotykane w Huawei thirdData
    for k in ("inverterYield", "PVYield", "day_cap", "dayCap", "energy", "yield"):
        if k in data_item_map and data_item_map[k] is not None:
            try:
                return float(str(data_item_map[k]).replace(",", "."))
            except Exception:
                pass
    return 0.0


def fetch_huawei_day_kwh(date_iso: str, plants_to_fetch: dict, plants_config: dict) -> dict:
    """
    plants_to_fetch: {StationCode: PlantKey} e.g. {'SAG': 'MEX1'}
    plants_config: dict from Config_Plants to resolve SecretUser/SecretPass env var names

    Returns: {PlantKey: kWh_float}
    """
    print("🚀 [Huawei] Connecting via /thirdData...")
    results = {p_key: 0.0 for p_key in plants_to_fetch.values()}

    if not plants_to_fetch:
        return results

    # Resolve creds from ANY Huawei plant in this batch (assuming shared creds)
    any_p = next(iter(plants_to_fetch.values()))
    secret_user_name = plants_config.get(any_p, {}).get("secret_user") or "HUAWEI_USERNAME"
    secret_pass_name = plants_config.get(any_p, {}).get("secret_pass") or "HUAWEI_PASSWORD"

    username = os.environ.get(secret_user_name)
    password = os.environ.get(secret_pass_name)

    if not username or not password:
        print(f"❌ [Huawei] Missing creds from env: {secret_user_name} / {secret_pass_name}")
        return results

    base = _base_url()
    collect_ms = _to_collect_time_ms(date_iso)

    try:
        # login
        r_log = requests.post(
            f"{base}/login",
            json={"userName": username, "systemCode": password},
            timeout=25,
        )
        r_log.raise_for_status()

        token = r_log.headers.get("XSRF-TOKEN")
        if not token:
            print("❌ [Huawei] Missing XSRF-TOKEN after login")
            return results

        headers = {"XSRF-TOKEN": token, "Content-Type": "application/json"}

        # Batch KPI day request
        station_codes = ",".join(plants_to_fetch.keys())
        payload = {"stationCodes": station_codes, "collectTime": collect_ms}

        r = requests.post(f"{base}/getKpiStationDay", headers=headers, json=payload, timeout=25)
        r.raise_for_status()
        j = r.json()

        data = j.get("data") or []
        if isinstance(data, dict):
            # czasem API zwraca dict zamiast listy
            data = [data]

        for item in data:
            sc = str(item.get("stationCode") or "").strip()
            if not sc:
                continue
            p_key = plants_to_fetch.get(sc)
            if not p_key:
                continue

            dim = item.get("dataItemMap") or {}
            val = _pick_energy_value(dim)
            results[p_key] = round(float(val or 0.0), 2)

        for s_id, p_key in plants_to_fetch.items():
            print(f"   📊 [Huawei] {p_key} ({s_id}): {results.get(p_key, 0.0)} kWh")

        return results

    except Exception as e:
        for s_id, p_key in plants_to_fetch.items():
            print(f"   ⚠️ [Huawei] Failed {p_key} ({s_id}): {e}")
        return results
