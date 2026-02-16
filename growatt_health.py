#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, logging, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List

from argia_health_sheets import read_table, ensure_header, append_rows
from argia_growatt_health_client import GrowattMonitoringClient, GrowattAuth, normalize_text, normalize_sn

LOG = logging.getLogger("argia.health.growatt")
MX_TZ = ZoneInfo("America/Mexico_City")


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def now_mx():
    dt = datetime.now(timezone.utc).astimezone(MX_TZ)
    return dt.strftime("%m/%d/%Y %H:%M:%S")


# ---------------------------------------------------------
# SNAP
# ---------------------------------------------------------
def read_snap(sheet_id, snap_range):
    values = read_table(sheet_id, snap_range)
    header = [h.upper() for h in values[0]]
    rows = values[1:]

    i_site = header.index("SITEID")
    i_brand = header.index("BRAND")
    i_plant = header.index("PLANT_KEY")

    inv_cols = [i for i, h in enumerate(header) if "INVERTER" in h]

    plants = []
    for r in rows:
        if r[i_brand].upper() != "GROWATT":
            continue
        sns = [normalize_sn(r[i]) for i in inv_cols if i < len(r) and r[i]]
        if sns:
            plants.append({"siteid": r[i_site], "plant": r[i_plant], "sns": sns})
    return plants


# ---------------------------------------------------------
def header():
    h = [
        "ExtractedAtUTC","Timestamp","SiteId","PlantName","SerialNumber","Status","FaultCode",
        "Vpv1","Ipv1","VacRS","VacST","VacTR","PacR","PacS","PacT","Pac"
    ]
    for i in range(1,17):
        h += [f"Vstr{i}",f"Istr{i}"]
    h += ["_endpoint"]
    return h


# ---------------------------------------------------------
def main():

    logging.basicConfig(level="INFO")

    SHEET = os.environ["GOOGLE_SHEET_ID"]
    TAB = "PV_Health_Monitoring"

    ensure_header(SHEET, TAB, header())

    snap = read_snap(SHEET, "SNAP!A1:Z1000")

    cli = GrowattMonitoringClient(
        GrowattAuth(
            user=os.environ["GROWATT_USERNAME"],
            password=os.environ["GROWATT_PASSWORD"],
        )
    )

    cli.login()

    rows = []

    for plant in snap:
        site = plant["siteid"]
        cli.warm_plant_context(site)

        devices = cli.fetch_devices_best_for_sns(site, plant["sns"])

        for sn in plant["sns"]:
            device = devices.get(sn, {})
            status = str(device.get("status",""))

            kpi = cli.fetch_health_kpi_for_sn(site, sn, device)

            def g(k): return kpi.get(k.lower(),"") if kpi else ""

            row = [
                now_utc_iso(), now_mx(), site, plant["plant"], sn,
                status,"",
                g("vpv1"),g("ipv1"),g("vacrs"),g("vacst"),g("vactr"),
                g("pacr"),g("pacs"),g("pact"),g("pac"),
            ]

            for i in range(1,17):
                row += [g(f"vstr{i}"),g(f"istr{i}")]

            row += [kpi.get("_endpoint","") if kpi else ""]

            rows.append(row)

        time.sleep(0.5)

    append_rows(SHEET, TAB, rows)
    LOG.info("Wrote %d rows", len(rows))


if __name__ == "__main__":
    main()
