#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA – Growatt Health Monitoring
--------------------------------
Writes a wide row per inverter into Google Sheets tab: PV_Health_Monitoring

Columns:
ExtractedAtUTC, Timestamp, SiteId, PlantName, SerialNumber, Status, FaultCode,
Vpv1(V), Ipv1(A), VacRS(V), VacST(V), VacTR(V),
PacR(W), PacS(W), PacT(W), Pac(W),
Vstr1(V), Istr1(A) ... Vstr16(V), Istr16(A),
_SourceEndpoint

How it works:
1) Read SNAP sheet to get Growatt plants (SiteId + Plant_Key + Brand).
2) Login to Growatt web.
3) For each plantId:
   - activate plant session pages
   - list inverters via /device/getInverterList2
   - for each inverter SN, pull details via /panel/getDeviceInfo (deviceTypeName=tlx)
4) Append results to PV_Health_Monitoring.

Env required:
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS   (service-account JSON as TEXT)
- GROWATT_USER
- GROWATT_PASS

Optional:
- SNAP_RANGE     default "SNAP!A1:Z"
- HEALTH_TAB     default "PV_Health_Monitoring"
- LOG_LEVEL      default "INFO"
- DEBUG_OUT_DIR  default "out_health"
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from argia_growatt_health_client import GrowattMonitoringClient, GrowattAuth, normalize_text, normalize_sn, safe_float


LOG = logging.getLogger("argia.health.growatt")


# ----------------------------
# Logging
# ----------------------------
def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


# ----------------------------
# Time helpers
# ----------------------------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_local_timestamp_str() -> str:
    # We store timestamp as local-ish readable string; runner is UTC.
    # Keep consistent with your other scripts: "M/D/YYYY HH:MM:SS"
    dt = datetime.now().astimezone()
    return f"{dt.month}/{dt.day}/{dt.year} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


# ----------------------------
# Filesystem helpers
# ----------------------------
def ensure_dir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


# ----------------------------
# Google Sheets
# ----------------------------
def load_google_creds() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS secret (service account JSON as text).")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def sheets_service():
    return build("sheets", "v4", credentials=load_google_creds(), cache_discovery=False)


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def read_snap_growatt(sheet_id: str, snap_range: str) -> List[Dict[str, str]]:
    """
    Returns list of dicts: {siteid, plant_key, plant_name(optional)}
    Filter: Brand == Growatt (case-insensitive).
    """
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=snap_range,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()

    values = resp.get("values", []) or []
    if not values:
        return []

    header = values[0]
    hmap = {_norm_header(str(h)): i for i, h in enumerate(header)}

    # Accept several possible header spellings
    idx_site = None
    for k in ("siteid", "site_id", "plantid", "plant_id"):
        if k in hmap:
            idx_site = hmap[k]
            break

    idx_brand = None
    for k in ("brand", "manufacturer", "vendor"):
        if k in hmap:
            idx_brand = hmap[k]
            break

    idx_pkey = None
    for k in ("plant_key", "plantkey", "key"):
        if k in hmap:
            idx_pkey = hmap[k]
            break

    idx_pname = None
    for k in ("plantname", "plant_name", "name"):
        if k in hmap:
            idx_pname = hmap[k]
            break

    if idx_site is None or idx_brand is None:
        raise RuntimeError(f"SNAP missing SiteId and/or Brand columns. Header={header}")

    out: List[Dict[str, str]] = []
    for row in values[1:]:
        brand = str(row[idx_brand]).strip() if idx_brand < len(row) else ""
        if brand.lower() != "growatt":
            continue
        site = str(row[idx_site]).strip() if idx_site < len(row) else ""
        if not re.fullmatch(r"\d{6,12}", site):
            continue

        pkey = ""
        if idx_pkey is not None and idx_pkey < len(row):
            pkey = str(row[idx_pkey]).strip()

        pname = ""
        if idx_pname is not None and idx_pname < len(row):
            pname = str(row[idx_pname]).strip()

        out.append({"siteid": site, "plant_key": pkey, "plant_name": pname})

    # Deduplicate by siteid
    seen = set()
    uniq = []
    for x in out:
        if x["siteid"] in seen:
            continue
        seen.add(x["siteid"])
        uniq.append(x)

    return uniq


def ensure_header(sheet_id: str, tab: str, header: List[str]) -> None:
    svc = sheets_service()
    rng = f"{tab}!A1:ZZ1"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    existing = (resp.get("values") or [[]])[0] if resp else []
    existing = existing or []
    if len(existing) == 0:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
        LOG.info("Ensured header on tab '%s'", tab)


def append_rows(sheet_id: str, tab: str, rows: List[List[Any]]) -> None:
    if not rows:
        return
    svc = sheets_service()
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


# ----------------------------
# Mapping helpers (Growatt key variations)
# ----------------------------
def _lower_keys(d: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (d or {}).items():
        out[str(k).strip().lower()] = v
    return out


def pick_ci(d: Dict[str, Any], candidates: List[str]) -> Any:
    """
    Case-insensitive pick. candidates should be already lowercase-ish names.
    Also tries to match by stripping non-alphanum for robustness.
    """
    if not d:
        return None
    dl = _lower_keys(d)

    # direct matches first
    for c in candidates:
        ck = c.strip().lower()
        if ck in dl and dl[ck] not in (None, "", "null"):
            return dl[ck]

    # normalized matches
    def nk(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    dl2 = {nk(k): v for k, v in dl.items()}
    for c in candidates:
        k2 = nk(c)
        if k2 in dl2 and dl2[k2] not in (None, "", "null"):
            return dl2[k2]

    return None


def kpi_map_from_obj(obj: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """
    Extract KPI-like fields from /panel/getDeviceInfo obj.

    We map by key name patterns commonly seen:
      vpv1, ipv1, vacrs, vacst, vactr, pacr/pacs/pact/pac, vstr1..16, istr1..16
    """
    o = obj or {}

    out: Dict[str, Optional[float]] = {}

    out["Vpv1(V)"] = safe_float(pick_ci(o, ["vpv1", "v_pv1", "pv1v", "vpv_1"]))
    out["Ipv1(A)"] = safe_float(pick_ci(o, ["ipv1", "i_pv1", "pv1i", "ipv_1"]))

    out["VacRS(V)"] = safe_float(pick_ci(o, ["vacrs", "vac_r_s", "vacrs_v", "u_rs"]))
    out["VacST(V)"] = safe_float(pick_ci(o, ["vacst", "vac_s_t", "vacst_v", "u_st"]))
    out["VacTR(V)"] = safe_float(pick_ci(o, ["vactr", "vac_t_r", "vactr_v", "u_tr"]))

    out["PacR(W)"] = safe_float(pick_ci(o, ["pacr", "p_r", "pac_r"]))
    out["PacS(W)"] = safe_float(pick_ci(o, ["pacs", "p_s", "pac_s"]))
    out["PacT(W)"] = safe_float(pick_ci(o, ["pact", "p_t", "pac_t"]))
    out["Pac(W)"] = safe_float(pick_ci(o, ["pac", "power", "acpower", "p_ac"]))

    # Strings 1..16
    for i in range(1, 17):
        out[f"Vstr{i}(V)"] = safe_float(pick_ci(o, [f"vstr{i}", f"v_str{i}", f"str{i}v", f"u_str{i}"]))
        out[f"Istr{i}(A)"] = safe_float(pick_ci(o, [f"istr{i}", f"i_str{i}", f"str{i}i", f"i_str{i}a"]))

    return out


def fault_code_from_any(device_row: Dict[str, Any], obj: Optional[Dict[str, Any]]) -> str:
    """
    Status=3 wants fault code. Growatt sometimes provides:
      faultCode, faultcode, errCode, errorCode, faultMsg, lastFaultCode...
    We'll return best available string.
    """
    candidates = ["faultcode", "fault_code", "errcode", "errorcode", "faultmsg", "lastfaultcode", "alarmcode"]

    # device list row first
    v1 = pick_ci(device_row or {}, candidates)
    if v1 not in (None, "", "null"):
        return normalize_text(v1)

    # then deviceInfo obj
    v2 = pick_ci(obj or {}, candidates)
    if v2 not in (None, "", "null"):
        return normalize_text(v2)

    return ""


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    setup_logging()

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z").strip()
    tab = os.getenv("HEALTH_TAB", "PV_Health_Monitoring").strip()
    debug_out = os.getenv("DEBUG_OUT_DIR", "out_health").strip()

    ensure_dir(debug_out)

    # Always create a marker so upload-artifact never says "No files"
    with open(os.path.join(debug_out, "RUN_MARKER.txt"), "w", encoding="utf-8") as f:
        f.write(now_utc_iso() + "\n")

    # Read SNAP -> Growatt plants
    snap = read_snap_growatt(sheet_id, snap_range)
    LOG.info("Growatt plants in SNAP: %s", len(snap))

    # Header
    header = ["ExtractedAtUTC", "Timestamp", "SiteId", "PlantName", "SerialNumber", "Status", "FaultCode",
              "Vpv1(V)", "Ipv1(A)", "VacRS(V)", "VacST(V)", "VacTR(V)",
              "PacR(W)", "PacS(W)", "PacT(W)", "Pac(W)"]
    for i in range(1, 17):
        header += [f"Vstr{i}(V)", f"Istr{i}(A)"]
    header += ["_SourceEndpoint"]

    ensure_header(sheet_id, tab, header)

    user = os.getenv("GROWATT_USER", "").strip()
    pwd = os.getenv("GROWATT_PASS", "").strip()
    if not user or not pwd:
        raise RuntimeError("Missing GROWATT_USER or GROWATT_PASS")

    client = GrowattMonitoringClient(GrowattAuth(user=user, password=pwd), debug_out_dir=debug_out)
    client.login()

    extracted_at = now_utc_iso()
    ts = now_local_timestamp_str()

    rows: List[List[Any]] = []

    for plant in snap:
        plant_id = plant["siteid"]
        plant_name_from_snap = plant.get("plant_name", "")

        client.activate_plant_session(plant_id)

        devices = client.list_inverters(plant_id)
        LOG.info("Found %s devices in plant %s", len(devices), plant_id)

        for dev in devices:
            sn = normalize_sn(dev.get("sn") or dev.get("deviceSn") or dev.get("invSn") or "")
            if not sn:
                continue

            status = normalize_text(dev.get("status"))
            plant_name = normalize_text(dev.get("plantName")) or plant_name_from_snap or normalize_text(dev.get("alias")) or ""

            # Device type name: UI uses tr class 'tlx' for inverters.
            device_type_name = normalize_text(dev.get("deviceTypeName")) or "tlx"

            obj = client.get_device_info(plant_id, device_type_name, sn)

            # KPI mapping
            kpi: Dict[str, Optional[float]] = {}
            if obj:
                kpi = kpi_map_from_obj(obj)

            # Fault code only really meaningful for status=3, but we fill when available.
            fault = ""
            if status == "3":
                fault = fault_code_from_any(dev, obj)

            row = [
                extracted_at,
                ts,
                plant_id,
                plant_name,
                sn,
                status,
                fault,
                kpi.get("Vpv1(V)"),
                kpi.get("Ipv1(A)"),
                kpi.get("VacRS(V)"),
                kpi.get("VacST(V)"),
                kpi.get("VacTR(V)"),
                kpi.get("PacR(W)"),
                kpi.get("PacS(W)"),
                kpi.get("PacT(W)"),
                kpi.get("Pac(W)"),
            ]

            for i in range(1, 17):
                row += [kpi.get(f"Vstr{i}(V)"), kpi.get(f"Istr{i}(A)")]

            row += ["/panel/getDeviceInfo"]
            rows.append(row)

    append_rows(sheet_id, tab, rows)
    LOG.info("✅ Wrote %s rows into %s", len(rows), tab)


if __name__ == "__main__":
    main()
