import os
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from argia_growatt_monitoring import GrowattMonitoringClient, GrowattAuth
from argia_sheets_monitoring import ensure_tab, append_rows


TAB_NAME = "Growatt_Inverter_30m"

HEADERS = [
    "ts_utc",
    "ts_local",
    "plant_id",
    "inverter_sn",
    "inverter_name",
    "status",
    "p_ac_w",
    "e_today_kwh",
    "e_total_kwh",
    "raw_json",
]


def setup_logging():
    level = logging.DEBUG if os.getenv("ARGIA_MONITORING_DEBUG", "0") == "1" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )


def parse_csv_env(name: str) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _pick(d: Dict[str, Any], keys: List[str]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def normalize_realtime(inverter_sn: str, inv_meta: Dict[str, Any], rt: Dict[str, Any]) -> Tuple[Any, Any, Any, Any]:
    """
    Different endpoints return different JSON shapes.
    We keep it robust and log raw_json regardless.
    """
    # Try common placements
    data = rt.get("data") if isinstance(rt, dict) else None
    if isinstance(data, dict):
        src = data
    else:
        src = rt if isinstance(rt, dict) else {"_raw": rt}

    p_ac_w = _pick(src, ["pac", "p_ac", "acPower", "power", "powerNow", "pAc", "p"])
    e_today_kwh = _pick(src, ["eToday", "todayEnergy", "etoday", "e_today", "energyToday"])
    e_total_kwh = _pick(src, ["eTotal", "totalEnergy", "etotal", "e_total", "energyTotal"])
    status = _pick(src, ["status", "inverterStatus", "deviceStatus", "runStatus"])

    # Name from inverter list if present
    inv_name = _pick(inv_meta, ["alias", "name", "invName", "deviceName", "sn"]) or inverter_sn

    return inv_name, status, p_ac_w, e_today_kwh, e_total_kwh


def main():
    setup_logging()
    LOG = logging.getLogger("argia.snap")

    user = os.getenv("GROWATT_USER", "")
    pw = os.getenv("GROWATT_PASS", "")
    if not user or not pw:
        raise RuntimeError("Missing GROWATT_USER / GROWATT_PASS")

    spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID", "") or os.getenv("GOOGLE_SHEETS_ID".replace("SHEETS", "SHEET"), "")
    if not spreadsheet_id:
        raise RuntimeError("Missing GOOGLE_SHEETS_ID secret/env")

    plant_ids = parse_csv_env("GROWATT_PLANT_IDS")
    if not plant_ids:
        raise RuntimeError("Missing GROWATT_PLANT_IDS (CSV)")

    # Login
    client = GrowattMonitoringClient(GrowattAuth(user, pw))
    client.login()

    # Ensure tab + headers
    ensure_tab(spreadsheet_id, TAB_NAME, HEADERS)

    now_utc = datetime.now(timezone.utc)
    ts_utc = now_utc.isoformat().replace("+00:00", "Z")
    # Mexico City local time is often what you want in charts; keep it simple without extra deps:
    # (If your runner timezone is UTC, ts_local = ts_utc; you can later convert in Sheets.)
    ts_local = ts_utc

    rows: List[List[Any]] = []

    for plant_id in plant_ids:
        LOG.info("🏭 Plant: %s", plant_id)

        inv_list = client.list_inverters_for_plant(plant_id)
        LOG.info("Found inverter entries: %s", len(inv_list))

        for inv in inv_list:
            inverter_sn = str(_pick(inv, ["sn", "deviceSn", "invSn", "serialNum", "serial", "id"]) or "").strip()
            if not inverter_sn:
                LOG.warning("Skipping inverter without SN. Meta=%s", inv)
                continue

            LOG.info("  🔌 Inverter SN: %s", inverter_sn)
            rt = client.get_inverter_realtime(inverter_sn)

            inv_name, status, p_ac_w, e_today_kwh, e_total_kwh = normalize_realtime(inverter_sn, inv, rt)

            raw_json = rt
            # Keep raw compact-ish; Sheets cell limit is huge but not infinite
            raw_str = str(raw_json)
            if len(raw_str) > 45000:
                raw_str = raw_str[:45000] + "...(trimmed)"

            rows.append([
                ts_utc,
                ts_local,
                plant_id,
                inverter_sn,
                inv_name,
                status,
                p_ac_w,
                e_today_kwh,
                e_total_kwh,
                raw_str,
            ])

    append_rows(spreadsheet_id, TAB_NAME, rows)
    LOG.info("✅ Done. Appended %s rows.", len(rows))


if __name__ == "__main__":
    main()
