#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, logging, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from argia_health_sheets import read_table, ensure_header, append_rows
from argia_growatt_health_client import GrowattMonitoringClient, GrowattAuth, normalize_text, normalize_sn, safe_float

LOG = logging.getLogger("argia.health.growatt")
MX_TZ = ZoneInfo("America/Mexico_City")


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_mx_str() -> str:
    dt = datetime.now(timezone.utc).replace(microsecond=0).astimezone(MX_TZ)
    return dt.strftime("%m/%d/%Y %H:%M:%S")


def read_snap(sheet_id: str, snap_range: str) -> List[Dict[str, Any]]:
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

    i_site = idx("SITEID")
    i_brand = idx("BRAND")
    i_plant = idx("PLANT_KEY")
    if i_plant is None:
        i_plant = idx("PLANTKEY")

    if i_site is None or i_brand is None or i_plant is None:
        raise RuntimeError(f"SNAP missing SITEID/BRAND/PLANT_KEY. Header={header}")

    inv_cols = [i for i, h in enumerate(header) if "INVERTER" in h]

    plants = []
    for r in rows:
        if len(r) <= max([i_site, i_brand, i_plant] + (inv_cols or [0])):
            continue
        if normalize_text(r[i_brand]).upper() != "GROWATT":
            continue

        sns = []
        for c in inv_cols:
            if c < len(r) and normalize_text(r[c]):
                sns.append(normalize_sn(r[c]))
        sns = list(dict.fromkeys([s for s in sns if s]))

        if sns:
            plants.append({"siteid": normalize_text(r[i_site]), "plant": normalize_text(r[i_plant]), "sns": sns})

    return plants


def build_header() -> List[str]:
    h = [
        "ExtractedAtUTC","Timestamp","SiteId","PlantName","SerialNumber",
        "Status","FaultCode",
        "Vpv1(V)","Ipv1(A)","VacRS(V)","VacST(V)","VacTR(V)",
        "PacR(W)","PacS(W)","PacT(W)","Pac(W)",
    ]
    for i in range(1, 17):
        h += [f"Vstr{i}(V)", f"Istr{i}(A)"]
    h += ["_SourceEndpoint"]
    return h


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
        raise RuntimeError("Missing Growatt credentials")

    ensure_header(sheet_id, tab, build_header())

    plants = read_snap(sheet_id, snap_range)
    LOG.info("Growatt plants in SNAP: %d", len(plants))
    if not plants:
        return

    cli = GrowattMonitoringClient(GrowattAuth(user=user, password=pwd))
    cli.login()

    extracted_at = now_utc_iso()
    ts = now_mx_str()

    rows_out: List[List[Any]] = []

    def val(flat: Dict[str, Any], key_norm: str) -> Any:
        if not flat:
            return ""
        v = flat.get(key_norm)
        f = safe_float(v, None)
        return f if f is not None else (normalize_text(v) if v is not None else "")

    for p in plants:
        siteid = p["siteid"]
        plantname = p["plant"]
        sns = p["sns"]

        cli.warm_plant_context(siteid)
        devices = cli.fetch_devices_best_for_sns(siteid, sns)

        for sn in sns:
            device = devices.get(sn, {})  # includes status/deviceType/datalogSn when available
            status = normalize_text(device.get("status", ""))
            fault = normalize_text(device.get("faultCode", "")) or normalize_text(device.get("faultMsg", ""))  # best effort

            kpi_flat = cli.fetch_health_kpi_for_sn(siteid, sn, device)

            row = [
                extracted_at,
                ts,
                siteid,
                plantname,
                sn,
                status,
                fault,
                val(kpi_flat, "vpv1"),
                val(kpi_flat, "ipv1"),
                val(kpi_flat, "vacrs"),
                val(kpi_flat, "vacst"),
                val(kpi_flat, "vactr"),
                val(kpi_flat, "pacr"),
                val(kpi_flat, "pacs"),
                val(kpi_flat, "pact"),
                val(kpi_flat, "pac"),
            ]

            for i in range(1, 17):
                row.append(val(kpi_flat, f"vstr{i}"))
                row.append(val(kpi_flat, f"istr{i}"))

            row.append(normalize_text(kpi_flat.get("_endpoint", "")))
            rows_out.append(row)

        time.sleep(float(os.getenv("GROWATT_SLEEP_SEC", "0.4")))

    append_rows(sheet_id, tab, rows_out)
    LOG.info("✅ Wrote %d rows into %s", len(rows_out), tab)


if __name__ == "__main__":
    main()
