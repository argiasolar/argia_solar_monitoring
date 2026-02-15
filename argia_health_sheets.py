#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json
from typing import Any, List
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


def load_google_creds() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS (service account JSON as TEXT).")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def sheets_service():
    return build("sheets", "v4", credentials=load_google_creds(), cache_discovery=False)


def qrange(tab: str, a1: str) -> str:
    return f"'{tab}'!{a1}"


def ensure_sheet_exists(sheet_id: str, tab: str) -> None:
    svc = sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    titles = {s.get("properties", {}).get("title") for s in (meta.get("sheets") or [])}
    if tab in titles:
        return
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
    ).execute()


def ensure_header(sheet_id: str, tab: str, header: List[str]) -> None:
    ensure_sheet_exists(sheet_id, tab)
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=qrange(tab, "A1:ZZ1"),
    ).execute()
    existing = (resp.get("values") or [[]])[0]
    if not existing:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=qrange(tab, "A1"),
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()


def append_rows(sheet_id: str, tab: str, rows: List[List[Any]]) -> None:
    if not rows:
        return
    sheets_service().spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=qrange(tab, "A1"),
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def read_table(sheet_id: str, rng: str) -> List[List[Any]]:
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=rng,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    return resp.get("values", []) or []
