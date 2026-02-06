import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from argia_growatt_monitoring import GrowattMonitoringClient, GrowattAuth
from argia_sheets_monitoring import ensure_tab, append_rows, read_snap_config

TAB_NAME = "Growatt_Inverter_30m"

HEADERS = [
    "ts_utc",
    "ts_local",
    "Plant_Key",
    "SITEID",
    "Inverter_SN",
    "Inverter_Name",
    "Status",
    "P_AC_W",
    "E_Today_kWh",
    "E_Total_kWh",
    "Raw_JSON",
]


def setup_logging() -> None:
    level = logging.DEBUG if os.getenv("ARGIA_MONITORING_DEBUG", "0") == "1" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _pick(d: Dict[str, Any], keys: List[str]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _as_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        if isinstance(x, str) and x.strip() == "":
            return None
        return float(x)
    except Exception:
        return None


def normalize_realtime(inverter_sn: str, inverter_name_hint: str, rt: Dict[str, Any]) -> Tuple[str, Any, Any, Any, Any]:
    """
    Normalizes common fields across possible Growatt JSON shapes.

    We DO NOT assume exact keys; we attempt several.
    We ALWAYS store Raw_JSON for later refinement.
    """
    src: Dict[str, Any] = rt
    if isinstance(rt.get("data"), dict):
        src = rt["data"]  # type: ignore

    status = _pick(src, ["status", "inverterStatus", "deviceStatus", "runStatus", "invStatus"])
    p_ac_w = _pick(src, ["pac", "p_ac", "acPower", "power", "powerNow", "pAc", "p"])
    e_today_kwh = _pick(src, ["eToday", "todayEnergy", "etoday", "e_today", "energyToday"])
    e_total_kwh = _pick(src, ["eTotal", "totalEnergy", "etotal", "e_total", "energyTotal"])

    # Try to pick a better name if present in realtime payload
    name = _pick(src, ["alias", "name", "invName", "deviceName"]) or inverter_name_hint or inverter_sn

    return (
        str(name),
        status,
        _as_float(p_ac_w),
        _as_float(e_today_kwh),
        _as_float(e_total_kwh),
    )


def _safe_trim(s: str, limit: int = 45000) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "...(trimmed)"


def main() -> None:
    setup_logging()
    LOG = logging.getLogger("argia.snap")

    username = os.getenv("GROWATT_USERNAME", "")
    password = os.getenv("GROWATT_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Missing GROWATT_USERNAME / GROWATT_PASSWORD")

    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not spreadsheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    # Load mapping from SNAP tab
    snap = read_snap_config(spreadsheet_id)
    LOG.info("Loaded %s rows from SNAP tab.", len(snap))

    # Login to Growatt
    client = GrowattMonitoringClient(GrowattAuth(username=username, password=password))
    client.login()

    # Ensure output tab + headers
    ensure_tab(spreadsheet_id, TAB_NAME, HEADERS)

    now_utc = datetime.now(timezone.utc)
    ts_utc = now_utc.isoformat().replace("+00:00", "Z")

    # We keep ts_local same for now; convert in Sheets if needed.
    # (We can add Mexico City tz conversion later if you want.)
    ts_local = ts_utc

    rows: List[List[Any]] = []

    inverter_cols = ["INVERTER1", "INVERTER2", "INVERTER3", "INVERTER4"]

    for rec in snap:
        plant_key = (rec.get("Plant_Key") or "").strip()
        siteid = (rec.get("SITEID") or "").strip()

        if not plant_key and not siteid:
            continue

        LOG.info("🏭 %s | SITEID=%s", plant_key, siteid)

        for col in inverter_cols:
            sn = (rec.get(col) or "").strip()
            if not sn:
                continue

            LOG.info("  🔌 %s=%s", col, sn)

            try:
                rt = client.get_inverter_realtime(siteid, sn)
            except Exception as e:
                LOG.error("    ❌ Failed realtime for %s: %s", sn, e)
                # still append an error row (useful for alerting / auditing)
                rows.append([ts_utc, ts_local, plant_key, siteid, sn, "", "ERROR", "", "", "", str(e)])
                continue

            inverter_name_hint = sn
            inv_name, status, p_ac_w, e_today_kwh, e_total_kwh = normalize_realtime(sn, inverter_name_hint, rt)

            raw_str = _safe_trim(json.dumps(rt, ensure_ascii=False))

            rows.append(
                [
                    ts_utc,
                    ts_local,
                    plant_key,
                    siteid,
                    sn,
                    inv_name,
                    status,
                    p_ac_w if p_ac_w is not None else "",
                    e_today_kwh if e_today_kwh is not None else "",
                    e_total_kwh if e_total_kwh is not None else "",
                    raw_str,
                ]
            )

    append_rows(spreadsheet_id, TAB_NAME, rows)
    LOG.info("✅ Done. Appended %s rows to %s.", len(rows), TAB_NAME)


if __name__ == "__main__":
    main()
