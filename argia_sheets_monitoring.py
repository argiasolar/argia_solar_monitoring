import os
import json
import base64
import logging
from typing import Any, Dict, List

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

LOG = logging.getLogger("argia.sheets.monitoring")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _load_sa_credentials_from_b64(env_name: str = "GOOGLE_CREDENTIALS") -> Credentials:
    b64 = os.getenv(env_name, "")
    if not b64:
        raise RuntimeError(f"Missing env {env_name}")

    raw = base64.b64decode(b64.encode("utf-8")).decode("utf-8")
    info = json.loads(raw)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def ensure_tab(spreadsheet_id: str, tab_name: str, headers: List[str]) -> None:
    creds = _load_sa_credentials_from_b64()
    service = build("sheets", "v4", credentials=creds)

    ss = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = ss.get("sheets", [])
    existing = {s["properties"]["title"] for s in sheets}

    requests = []
    if tab_name not in existing:
        LOG.info("Creating tab: %s", tab_name)
        requests.append({"addSheet": {"properties": {"title": tab_name}}})

    if requests:
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()

    # Write headers to row 1 (idempotent)
    range_a1 = f"{tab_name}!A1"
    body = {"values": [headers]}
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_a1,
        valueInputOption="RAW",
        body=body,
    ).execute()


def append_rows(spreadsheet_id: str, tab_name: str, rows: List[List[Any]]) -> None:
    if not rows:
        LOG.warning("No rows to append.")
        return

    creds = _load_sa_credentials_from_b64()
    service = build("sheets", "v4", credentials=creds)

    range_a1 = f"{tab_name}!A:Z"
    body = {"values": rows}

    resp = service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range_a1,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

    updates = resp.get("updates", {})
    LOG.info("Appended %s rows. Updates=%s", len(rows), updates)


def read_snap_config(spreadsheet_id: str) -> List[Dict[str, str]]:
    """
    Reads SNAP!A1:Z as a config table.
    Returns list of dicts per row (keys from header row).
    """
    creds = _load_sa_credentials_from_b64()
    service = build("sheets", "v4", credentials=creds)

    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range="SNAP!A1:Z",
    ).execute()

    values = resp.get("values", [])
    if not values or len(values) < 2:
        raise RuntimeError("SNAP tab is empty or missing data (needs header row + at least 1 record).")

    headers = values[0]
    rows = values[1:]

    out: List[Dict[str, str]] = []
    for row in rows:
        rec: Dict[str, str] = {}
        for i, h in enumerate(headers):
            rec[h] = row[i] if i < len(row) else ""
        # Skip completely empty rows
        if any(v.strip() for v in rec.values()):
            out.append(rec)

    return out
