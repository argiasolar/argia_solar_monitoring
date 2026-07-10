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

Stage 7.3b additions:
- ``write_cell(tab, row, col, value)`` — single-cell update
- ``write_row(tab, row, values)`` — overwrite a whole row starting at col A
- ``delete_row(tab, row)`` — delete a row, shifting subsequent rows up
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


def _col_to_a1(col: int) -> str:
    """1-indexed column number → A1 letter(s). 1→A, 26→Z, 27→AA, 52→AZ, ..."""
    if col < 1:
        raise ValueError(f"Column must be >= 1, got {col}")
    out = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        out = chr(65 + rem) + out
    return out



def _cells_equivalent(old, new) -> bool:
    """Sheets reads values back FORMATTED (6.0 -> "6", blank for
    empty), so raw string equality brands identical data as changed.
    Numbers compare as floats; everything else as stripped strings
    with blank == None. Without this, every overlap-window row
    re-writes on every poll — forever (v81, live 429s 2026-07-10)."""
    so, sn = str(old).strip(), str(new).strip()
    if so == sn:
        return True
    if (so == "" and new is None) or (sn == "" and old is None):
        return True
    try:
        return float(so) == float(sn)
    except (TypeError, ValueError):
        return False


def _rows_equivalent(old_row, new_row) -> bool:
    padded = list(old_row) + [""] * max(0, len(new_row) - len(old_row))
    return all(_cells_equivalent(padded[i], new_row[i])
               for i in range(len(new_row)))


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

        # Cache tab name → numeric sheetId (GID) for delete_row, which needs
        # the GID rather than the spreadsheet ID. Populated lazily.
        self._tab_gid_cache: Dict[str, int] = {}

    # ----------------------- low-level helpers -----------------------

    @staticmethod
    def _qrange(tab: str, a1: str) -> str:
        """Quote tab names so spaces and special chars are handled."""
        return f"'{tab}'!{a1}"

    def _values(self):
        return self._svc.spreadsheets().values()

    def _tab_gid(self, tab: str) -> int:
        """Look up the numeric sheetId for a tab. Cached after first call."""
        if tab in self._tab_gid_cache:
            return self._tab_gid_cache[tab]
        meta = self._svc.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
        for s in (meta.get("sheets") or []):
            props = s.get("properties", {})
            if props.get("title") == tab:
                gid = int(props.get("sheetId", 0))
                self._tab_gid_cache[tab] = gid
                return gid
        raise ValueError(f"Tab '{tab}' not found in spreadsheet {self.sheet_id}")

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
        # Invalidate cache because newly created tab won't be there yet
        self._tab_gid_cache.pop(tab, None)
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

    def write_header_row(self, tab: str, header: List[str]) -> None:
        """
        Overwrite row 1 with ``header`` unconditionally.

        Unlike ``ensure_header`` (which only writes when row 1 is empty), this
        replaces an existing header. Used by additive schema migrations that
        append a trailing column: the new header is identical to the old one
        plus the new column, so overwriting row 1 only adds the new cell and
        leaves all existing data rows untouched.
        """
        self.ensure_tab(tab)
        self._values().update(
            spreadsheetId=self.sheet_id,
            range=self._qrange(tab, "A1"),
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
        LOG.info("Overwrote header on '%s' (%d cols)", tab, len(header))

    def write_values(self, tab: str, a1_range: str, values: List[List]) -> None:
        """Write a 2-D block of values to ``tab!a1_range`` (RAW).

        Generic escape hatch for migrations that need to set specific cells or
        a column outside the row-oriented append/upsert helpers (e.g. adding
        and back-filling a single config column). Caller owns the range math.
        """
        self.ensure_tab(tab)
        self._values().update(
            spreadsheetId=self.sheet_id,
            range=self._qrange(tab, a1_range),
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
        LOG.info("Wrote %d row(s) to '%s'!%s", len(values), tab, a1_range)

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

    def write_cell(
        self,
        tab: str,
        row: int,
        col: int,
        value: Any,
        value_input_option: str = "RAW",
    ) -> None:
        """
        Write a single cell. Row and col are 1-indexed (row 1 is the header row,
        column 1 is A).

        Example: ``write_cell("Inverters", 5, 4, 100)`` writes 100 into D5.

        Uses ``RAW`` by default to avoid auto-formatting (e.g. converting "1.0"
        into a date). Pass ``value_input_option="USER_ENTERED"`` if you want
        the value parsed.

        Stage 7.3b — added so infer_plant_specs.py and kpi_daily.py can do
        surgical updates without rewriting whole rows.
        """
        if row < 1 or col < 1:
            raise ValueError(f"row and col must be >= 1 (got row={row}, col={col})")
        a1 = f"{_col_to_a1(col)}{row}"
        self._values().update(
            spreadsheetId=self.sheet_id,
            range=self._qrange(tab, a1),
            valueInputOption=value_input_option,
            body={"values": [[value]]},
        ).execute()

    def write_row(
        self,
        tab: str,
        row: int,
        values: List[Any],
        value_input_option: str = "USER_ENTERED",
    ) -> None:
        """
        Overwrite a whole row starting at column A. Row is 1-indexed.

        Example: ``write_row("KPI_Daily", 5, ["2026-05-14", "QRO1", ...])``
        writes the list across row 5 starting at A5.

        Cells beyond ``len(values)`` are NOT cleared — this only writes the
        cells you provide. If you need to clear trailing cells, pass empty
        strings for them.

        Stage 7.3b — added so kpi_daily.upsert_kpi_rows can update existing
        rows in place.
        """
        if row < 1:
            raise ValueError(f"row must be >= 1 (got {row})")
        if not values:
            return
        a1 = f"A{row}"
        self._values().update(
            spreadsheetId=self.sheet_id,
            range=self._qrange(tab, a1),
            valueInputOption=value_input_option,
            body={"values": [list(values)]},
        ).execute()

    def delete_row(self, tab: str, row: int) -> None:
        """
        Delete a row, shifting subsequent rows up. Row is 1-indexed.

        Example: ``delete_row("KPI_Daily", 5)`` removes row 5; what was row 6
        becomes row 5.

        Uses batchUpdate's ``deleteDimension``. Needs the numeric sheetId of
        the tab, not the spreadsheetId — looked up via ``_tab_gid`` and cached.

        WARNING: this is destructive. Callers should delete bottom-up when
        removing multiple rows to keep indices stable.

        Stage 7.3b — added so kpi_daily.prune_old_rows can actually delete.
        """
        if row < 1:
            raise ValueError(f"row must be >= 1 (got {row})")
        gid = self._tab_gid(tab)
        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={
                "requests": [{
                    "deleteDimension": {
                        "range": {
                            "sheetId": gid,
                            "dimension": "ROWS",
                            "startIndex": row - 1,  # API is 0-indexed
                            "endIndex": row,        # exclusive
                        },
                    },
                }],
            },
        ).execute()

    def delete_row_range(self, tab: str, start_row: int, end_row: int) -> None:
        """
        Delete rows ``start_row..end_row`` INCLUSIVE (1-indexed) in one call.

        The monthly archive prunes a whole month of telemetry — thousands of
        contiguous rows. Per-row deletion would be thousands of API calls;
        a single ``deleteDimension`` over the block is one.

        WARNING: destructive. Caller must have verified the block bounds.
        """
        if start_row < 1 or end_row < start_row:
            raise ValueError(f"bad row range {start_row}..{end_row}")
        gid = self._tab_gid(tab)
        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={
                "requests": [{
                    "deleteDimension": {
                        "range": {
                            "sheetId": gid,
                            "dimension": "ROWS",
                            "startIndex": start_row - 1,  # API is 0-indexed
                            "endIndex": end_row,          # exclusive
                        },
                    },
                }],
            },
        ).execute()

    def format_datetime_column(self, tab: str, col: int,
                               pattern: str = "yyyy-mm-dd hh:mm:ss") -> None:
        """Apply a date/datetime number format to one column (1-indexed),
        rows 2..end. A datetime cell read as UNFORMATTED_VALUE and written
        back RAW is a bare serial number — this makes it display as a date
        again."""
        gid = self._tab_gid(tab)
        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"requests": [{
                "repeatCell": {
                    "range": {"sheetId": gid, "startRowIndex": 1,
                              "startColumnIndex": col - 1,
                              "endColumnIndex": col},
                    "cell": {"userEnteredFormat": {
                        "numberFormat": {"type": "DATE_TIME",
                                         "pattern": pattern}}},
                    "fields": "userEnteredFormat.numberFormat",
                },
            }]},
        ).execute()

    def set_header_notes(self, tab: str, notes: Dict[str, str]) -> int:
        """Attach a Google Sheets note (hover comment) to header cells.

        ``notes`` maps header text -> note text. Header names are
        matched case-insensitively against row 1. Returns how many
        notes were set; unknown columns are logged and skipped, never
        fatal. Used for data-provenance annotations ("value comes from
        contract", "manual input", ...) so the audit trail lives inside
        the sheet itself.
        """
        header = [str(c or "").strip()
                  for c in (self.read_range(tab, "A1:ZZ1") or [[]])[0]]
        lookup = {h.lower(): i for i, h in enumerate(header) if h}
        gid = self._tab_gid(tab)
        requests = []
        missing = []
        for name, note in notes.items():
            i = lookup.get(name.lower())
            if i is None:
                missing.append(name)
                continue
            requests.append({"updateCells": {
                "range": {"sheetId": gid, "startRowIndex": 0,
                          "endRowIndex": 1, "startColumnIndex": i,
                          "endColumnIndex": i + 1},
                "rows": [{"values": [{"note": note}]}],
                "fields": "note"}})
        if missing:
            LOG.warning("set_header_notes(%s): columns not found: %s",
                        tab, missing)
        if requests:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": requests},
            ).execute()
            LOG.info("Set %d header note(s) on '%s'", len(requests), tab)
        return len(requests)

    def freeze_and_bold_header(self, tab: str) -> None:
        """Freeze row 1 and make it bold — the standard tab header look."""
        gid = self._tab_gid(tab)
        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"requests": [
                {"updateSheetProperties": {
                    "properties": {"sheetId": gid,
                                   "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {
                    "range": {"sheetId": gid, "startRowIndex": 0,
                              "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {
                        "textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold"}},
            ]},
        ).execute()

    def delete_tab_if_exists(self, tab: str) -> bool:
        """Delete a tab by name if present (e.g. the default 'Sheet1' left
        behind when a spreadsheet is created). Returns True if deleted.
        Refuses to delete the only remaining tab (API would reject it)."""
        try:
            gid = self._tab_gid(tab)
        except Exception:  # noqa: BLE001 — tab absent
            return False
        meta = self._svc.spreadsheets().get(
            spreadsheetId=self.sheet_id, fields="sheets(properties(sheetId))"
        ).execute()
        if len(meta.get("sheets", [])) <= 1:
            return False
        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"requests": [{"deleteSheet": {"sheetId": gid}}]},
        ).execute()
        return True

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
                old = existing_data_rows[sheet_row - header_row - 1]
                if _rows_equivalent(old, row):
                    unchanged += 1
                else:
                    to_update.append((sheet_row, row))
            else:
                to_insert.append(row)

        # Apply inserts
        if to_insert:
            self.append_rows(tab, to_insert)

        # Apply updates in ONE batch request (v81): the per-row loop
        # this replaces issued one HTTP write per row and, combined
        # with v80's overlap-window upserts, blew the Sheets quota of
        # 60 writes/min/user (live 429s, 2026-07-10). N updates now
        # cost 1 request.
        if to_update:
            self._values().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": [
                        {"range": self._qrange(tab, f"A{sheet_row}"),
                         "values": [row]}
                        for sheet_row, row in to_update
                    ],
                },
            ).execute()

        result = {
            "inserted": len(to_insert),
            "updated": len(to_update),
            "unchanged": unchanged,
        }
        LOG.info("upsert into '%s': %s", tab, result)
        return result
