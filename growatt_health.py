#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, logging, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from argia_health_sheets import read_table, ensure_header, append_rows
from argia_growatt_health_client import GrowattMonitoringClient, GrowattAuth, normalize_sn, normalize_text, safe_float, pick

LOG = logging.getLogger("argia.health.growatt")

MX_TZ = ZoneInfo("America/Mexico_City")


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_mx_str() -> str:
    dt = datetime.now(timezone.utc).replace(microsecond=0).astimezone(MX_TZ)
    return f"{dt.month}/{dt.day}/{dt.year} {dt.hour}:{dt.minute:02d}:{dt.second:02d}"


def looks_like_growatt_siteid(s: str) -> bool:
    return bool(re.fullmatch(r"\d{6,12}", (s or "").strip()))


def read_snap_growatt(sheet_id: str, snap_range: str) -> List[Dict[str, Any]]:
    values = read_table(sheet_id, snap_range)
    if not values:
        return []
    header = [normalize_text(h).upper() for h in values[0]]
    rows = values[1:]

    def idx(name: str) -> Optional[int]:
        try:
            return header.index(name.upper())
        except ValueError:
            return None

    i_plant = idx("PLANT_KEY") or idx("PLANTKEY")
    i_site = idx("SITEID")
    i_brand = idx("BRAND")

    if i_plant is None or i_site is None or i_brand is None:
        raise RuntimeError(f"SNAP missing Plant_Key / SITEID / Brand. Header={header}")

    inv_cols = [i for i, h in enumerate(header) if ("INVERTER" in h) or ("IVERTER" in h)]
    out: List[Dict[str, Any]] = []

    for r in rows:
        if len(r) <= max([i_plant, i_site, i_brand] + (inv_cols or [0])):
            continue

        plant = normalize_text(r[i_plant])          # we will treat this as PlantName for now
        siteid = normalize_text(r[i_site])
        brand = normalize_text(r[i_brand]).upper()

        if brand != "GROWATT":
            continue
        if not plant or not looks_like_growatt_siteid(siteid):
            continue

        sns: List[str] = []
        for j in inv_cols:
            if j < len(r):
                sn = normalize_text(r[j])
                if sn:
                    sns.append(normalize_sn(sn))

        sns = list(dict.fromkeys([s for s in sns if s]))
        if sns:
            out.append({"siteid": siteid, "plant": plant, "sns": sns})

    return out


def status_to_text_and_fault(item: Dict[str, Any]) -> (str, str):
    """
    Keep raw-ish status (don’t collapse to 1/3), and also best-effort fault code/message.
    Growatt fields vary a lot; we check multiple keys.
    """
    status = normalize_text(pick(item, ["status", "deviceStatus", "invStatus", "workStatus", "connStatus", "runStatus"])) or ""
    fault = normalize_text(pick(item, ["faultCode", "fault_code", "faultMsg", "faultMessage", "alarmCode", "alarmMsg"])) or ""

    m = item.get("dataItemMap")
    if isinstance(m, dict):
        if not fault:
            fault = normalize_text(pick(m, ["faultCode", "fault_code", "faultMsg", "alarmCode", "alarmMsg"])) or fault
        if not status:
            status = normalize_text(pick(m, ["status", "workStatus", "runStatus"])) or status

    return status, fault


def build_header() -> List[str]:
    header = [
        "ExtractedAtUTC",
        "Timestamp",
        "SiteId",
        "PlantName",
        "SerialNumber",
        "Status",
        "FaultCode",
        "Vpv1(V)",
        "Ipv1(A)",
        "VacRS(V)",
        "VacST(V)",
        "VacTR(V)",
        "PacR(W)",
        "PacS(W)",
        "PacT(W)",
        "Pac(W)",
    ]
    for i in range(1, 17):
        header.append(f"Vstr{i}(V)")
        header.append(f"Istr{i}(A)")
    header.append("_SourceEndpoint")  # debug: which endpoint produced KPI
    return header


def main() -> None:
    setup_logging()

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z1000").strip()
    tab = os.getenv("PV_HEALTH_TAB", "PV_Health_Monitoring").strip()

    user = (os.getenv("GROWATT_USERNAME") or os.getenv("GROWATT_USER") or "").strip()
    pwd = (os.getenv("GROWATT_PASSWORD") or os.getenv("GROWATT_PASS") or "").strip()
    if not user or not pwd:
        raise RuntimeError("Missing Growatt credentials (GROWATT_USERNAME/GROWATT_PASSWORD).")

    header = build_header()
    ensure_header(sheet_id, tab, header)

    snap = read_snap_growatt(sheet_id, snap_range)
    LOG.info("Growatt plants in SNAP: %d", len(snap))
    if not snap:
        return

    cli = GrowattMonitoringClient(GrowattAuth(user=user, password=pwd))
    cli.login()

    extracted_at = now_utc_iso()
    ts = now_mx_str()

    rows_out: List[List[Any]] = []

    for plant in snap:
        siteid = plant["siteid"]
        plantname = plant["plant"]
        sns = plant["sns"]

        cli.warm_plant_context(siteid)

        # 1) Status/fault from list endpoints (best match per SN)
        items_by_sn = cli.fetch_devices_best_for_sns(siteid, sns, page_size=50, max_pages=6)

        for sn in sns:
            base_item = items_by_sn.get(sn, {})
            status, fault = status_to_text_and_fault(base_item)

            # 2) KPI / strings
            kpi = cli.fetch_health_kpi_for_sn(siteid, sn)

            # helper get
            def g(key: str) -> Any:
                v = kpi.get(key)
                # if numeric-like, keep numeric
                f = safe_float(v, None)
                return f if f is not None else (normalize_text(v) if v is not None else "")

            row = [
                extracted_at,
                ts,
                siteid,
                plantname,
                sn,
                status,
                fault,
                g("Vpv1"),
                g("Ipv1"),
                g("VacRS"),
                g("VacST"),
                g("VacTR"),
                g("PacR"),
                g("PacS"),
                g("PacT"),
                g("Pac"),
            ]

            for i in range(1, 17):
                row.append(g(f"Vstr{i}"))
                row.append(g(f"Istr{i}"))

            row.append(normalize_text(kpi.get("_endpoint", "")))
            rows_out.append(row)

        # be polite to Growatt
        time_sleep = float(os.getenv("GROWATT_SLEEP_SEC", "0.4"))
        if time_sleep > 0:
            import time
            time.sleep(time_sleep)

    append_rows(sheet_id, tab, rows_out)
    LOG.info("✅ Wrote %d rows into %s", len(rows_out), tab)


if __name__ == "__main__":
    main()
