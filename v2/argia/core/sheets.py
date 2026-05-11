"""
Google Sheets client.

Single source of truth for all Sheets I/O. v1 had ~3 copies of this scattered
across files; this is the only one in v2.

Design choices:
- Service account auth via GOOGLE_CREDENTIALS env var (JSON as text).
- ``USER_ENTERED`` is the default for appends so datetime strings get parsed
  by Sheets (this was inconsistent in v1).
- Idempotent ``upsert_rows`` for daily aggregates — no more duplicate rows
  if a cron job re-runs.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

LOG = logging.getLogger("argia.core.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsClient:
    """Wrapper around the Google Sheets API. Constructor reads credentials."""

    def __init__(self, sheet_id: str, credentials_json: Optional[str] = None) -> None:
        if not sheet_id:
            raise ValueError("sheet_id is required")
        self.sheet_id = sheet_id

        raw = credentials_json or os.environ.get("GOOGLE_CREDENTIALS", "")
        if not raw:
            raise RuntimeError(
                "Missing Google credentials. Set GOOGLE_CREDENTIALS env var "
                "to the service account JSON (as a single line), or pass "
                "credentials_json explicitly."
            )

        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # ----------------------- low-level helpers -----------------------

    @staticmethod
    def _qrange(tab: str, a1: str) -> str:
        """Quote tab names so spaces and special chars are handled."""
        return f"'{tab}'!{a1}"

    def _values(self):
        return self._svc.spreadsheets().values()

    # ----------------------- read -----------------------

    def read_range(self, tab: str, a1_range: str = "A1:Z") -> List[List[Any]]:
        """
        Read a rectangular range. Returns rows as nested lists.
        Empty trailing cells in a row are NOT padded — caller must handle short rows.
        """
        resp = (
            self._values()
            .get(
                spreadsheetId=self.sheet_id,
                range=self._qrange(tab, a1_range),
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute()
        )
        return resp.get("values", []) or []

    def read_table(self, tab: str, a1_range: str = "A1:Z") -> List[Dict[str, Any]]:
        """
        Read a table with a header row. Returns list of dicts keyed by header.
        Empty rows are skipped. Missing trailing cells become "".
        """
        rows = self.read_range(tab, a1_range)
        if not rows:
            return []

        header = [str(h).strip() for h in rows[0]]
        out: List[Dict[str, Any]] = []
        for raw in rows[1:]:
            if not any(str(c).strip() for c in raw):
                continue  # skip blank rows
            record = {}
            for i, h in enumerate(header):
                record[h] = raw[i] if i < len(raw) else ""
            out.append(record)
        return out

    # ----------------------- write -----------------------

    def ensure_tab(self, tab: str) -> None:
        """Create the tab if it doesn't exist. Idempotent."""
        meta = self._svc.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
        existing = {
            s.get("properties", {}).get("title")
            for s in (meta.get("sheets") or [])
        }
        if tab in existing:
            return

        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
        LOG.info("Created tab '%s'", tab)

    def ensure_header(self, tab: str, header: List[str]) -> None:
        """
        Write the header row only if row 1 is empty. Does not overwrite an
        existing header even if it differs.
        """
        self.ensure_tab(tab)
        existing = self.read_range(tab, "A1:ZZ1")
        first_row = existing[0] if existing else []
        if any(str(c).strip() for c in first_row):
            return

        self._values().update(
            spreadsheetId=self.sheet_id,
            range=self._qrange(tab, "A1"),
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
        LOG.info("Wrote header to '%s' (%d cols)", tab, len(header))

    def append_rows(
        self,
        tab: str,
        rows: List[List[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> int:
        """
        Append rows at the bottom. Returns the number of rows appended.
        ``USER_ENTERED`` is the default so that ``M/D/YYYY H:MM:SS`` strings
        are parsed by Sheets as real datetimes.
        """
        if not rows:
            return 0
        self._values().append(
            spreadsheetId=self.sheet_id,
            range=self._qrange(tab, "A1"),
            valueInputOption=value_input_option,
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        return len(rows)

    def upsert_rows(
        self,
        tab: str,
        rows: List[List[Any]],
        natural_key_columns: List[int],
        header_row: int = 1,
    ) -> Dict[str, int]:
        """
        Insert rows that have a new natural key, update rows whose key already
        exists. Idempotent — running twice produces the same result.

        natural_key_columns: 0-based column indices that together form the
        unique key for a row. E.g. for DailyProduction the key is (date,
        plant_key) → ``[0, 1]``.

        Returns ``{"inserted": N, "updated": M, "unchanged": K}``.

        IMPORTANT: this issues one batch read + one batch write. It does NOT
        provide transactional safety — if two crons race they can both insert
        the same key. The Argia Pi runs a single cron so this is fine; if you
        ever parallelize, add a lock.
        """
        if not rows:
            return {"inserted": 0, "updated": 0, "unchanged": 0}

        # Load existing data (skip header)
        all_data = self.read_range(tab, "A1:ZZ")
        existing_data_rows = all_data[header_row:] if len(all_data) > header_row else []

        def key_of(row: List[Any]) -> tuple:
            return tuple(
                str(row[c]).strip() if c < len(row) else ""
                for c in natural_key_columns
            )

        # Map existing key → 1-based sheet row index
        existing_keys: Dict[tuple, int] = {}
        for i, row in enumerate(existing_data_rows):
            k = key_of(row)
            if any(part for part in k):
                # Sheet rows are 1-based; +1 for header offset, +1 for 0-index → +header_row+1
                existing_keys[k] = i + header_row + 1

        to_insert: List[List[Any]] = []
        to_update: List[tuple] = []  # (sheet_row_index, new_row)
        unchanged = 0

        for row in rows:
            k = key_of(row)
            if k in existing_keys:
                sheet_row = existing_keys[k]
                # compare row content (stringified) to detect unchanged
                old = existing_data_rows[sheet_row - header_row - 1]
                if [str(c) for c in old[: len(row)]] == [str(c) for c in row]:
                    unchanged += 1
                else:
                    to_update.append((sheet_row, row))
            else:
                to_insert.append(row)

        # Apply inserts
        if to_insert:
            self.append_rows(tab, to_insert)

        # Apply updates one row at a time (small N, simpler than batch)
        for sheet_row, row in to_update:
            self._values().update(
                spreadsheetId=self.sheet_id,
                range=self._qrange(tab, f"A{sheet_row}"),
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            ).execute()

        result = {
            "inserted": len(to_insert),
            "updated": len(to_update),
            "unchanged": unchanged,
        }
        LOG.info("upsert into '%s': %s", tab, result)
        return result
