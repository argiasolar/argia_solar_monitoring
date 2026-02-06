import os
import json
import base64
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

LOG = logging.getLogger("argia.sheets.monitoring")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _load_sa_credentials_from_b64(env_name: str = "GOOGLE_SERVICE_ACCOUNT_JSON_B64") -> Credentials:
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
        requests.append({
            "addSheet": {"properties": {"title": tab_name}}
        })

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests}
        ).execute()

    # Write headers if empty (first row)
    # We just overwrite row 1 always (idempotent enough for monitoring)
    range_a1 = f"{tab_name}!A1"
    body = {"values": [headers]}
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_a1,
        valueInputOption="RAW",
        body=body
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
        body=body
    ).execute()

    updates = resp.get("updates", {})
    LOG.info("Appended rows. Updates: %s", updates)
