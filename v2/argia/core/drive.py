"""Minimal Google Drive client — only what the monthly archive needs.

Separate from SheetsClient on purpose: it needs the broader ``drive``
scope, and the Drive API must be ENABLED in the service account's GCP
project (Sheets API alone doesn't grant it). ``scripts/archive_preflight.py``
verifies both before the archive is ever run.

Files the service account creates are owned by the service account, so the
archive spreadsheets are created INSIDE a folder that the human shared with
the SA — that's what makes them visible in the human's Drive. Google-native
spreadsheets consume no storage quota, so SA quota is a non-issue.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

LOG = logging.getLogger("argia.core.drive")

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


class DriveClient:
    """Thin wrapper over Drive v3 for archive-file management."""

    def __init__(self, credentials_json: Optional[str] = None,
                 service=None) -> None:
        if service is not None:          # injectable for tests
            self._svc = service
            return
        raw = credentials_json or os.environ.get("GOOGLE_CREDENTIALS", "")
        if not raw:
            raise RuntimeError(
                "Missing Google credentials. Set GOOGLE_CREDENTIALS env var.")
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info,
                                                      scopes=DRIVE_SCOPES)
        self._svc = build("drive", "v3", credentials=creds,
                          cache_discovery=False)

    # ---------- preflight helpers ----------

    def whoami(self) -> str:
        """Service-account identity as Drive sees it (proves API + scope)."""
        about = self._svc.about().get(fields="user(emailAddress)").execute()
        return about.get("user", {}).get("emailAddress", "?")

    def folder_name(self, folder_id: str) -> str:
        """Folder's name (proves the folder is shared with the SA)."""
        meta = self._svc.files().get(
            fileId=folder_id, fields="name,mimeType",
            supportsAllDrives=True).execute()
        return meta.get("name", "?")

    # ---------- archive-file management ----------

    def find_spreadsheet(self, folder_id: str, title: str) -> Optional[str]:
        """Spreadsheet id of ``title`` inside ``folder_id`` — or None.

        Makes archive creation idempotent: a re-run reuses the existing file
        instead of creating "Archive_2026_07 (1)"-style duplicates.
        """
        q = (f"name = '{title}' and '{folder_id}' in parents "
             f"and mimeType = '{SPREADSHEET_MIME}' and trashed = false")
        resp = self._svc.files().list(
            q=q, fields="files(id,name)", pageSize=2,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def create_spreadsheet(self, folder_id: str, title: str) -> str:
        """Create an empty spreadsheet named ``title`` inside ``folder_id``."""
        meta = {"name": title, "mimeType": SPREADSHEET_MIME,
                "parents": [folder_id]}
        f = self._svc.files().create(
            body=meta, fields="id", supportsAllDrives=True).execute()
        LOG.info("Created spreadsheet '%s' (%s) in folder %s",
                 title, f["id"], folder_id)
        return f["id"]

    def trash_file(self, file_id: str) -> None:
        """Move a file to trash (used by the preflight's create/cleanup test)."""
        self._svc.files().update(
            fileId=file_id, body={"trashed": True},
            supportsAllDrives=True).execute()
