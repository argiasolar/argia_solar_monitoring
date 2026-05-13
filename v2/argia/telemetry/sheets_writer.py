"""Sheets writer for telemetry tabs.

Wraps ``SheetsClient`` to:
1. Ensure the target tab exists (creates if not).
2. Ensure the header matches the schema (writes if A1 is empty; **REFUSES**
   if a non-empty header doesn't match the schema's column list — this
   prevents column-misaligned writes after a schema change).
3. Upsert rows on the schema's natural key.

The header sanity check is new in Stage 4. Stage 3's ``ensure_header`` simply
preserved whatever was in row 1; that was fine when there was only one schema.
Now that ``ARGIA_SCHEMA`` changed shape from wide to narrow, writing into an
old-shape tab would scramble column alignment silently. Refusing loudly is
much better than scrambling silently.

Side effects are explicit — every public function writes to Sheets unless
``dry_run=True``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from argia.core.sheets import SheetsClient
from argia.telemetry.schema import TelemetrySchema

LOG = logging.getLogger("argia.telemetry.sheets_writer")


class SchemaMismatchError(RuntimeError):
    """Raised when an existing tab's header doesn't match the schema.

    The fix is always the same: delete the tab manually in Sheets, then re-run.
    """


def _read_existing_header(sheets: SheetsClient, tab: str) -> List[str]:
    """Return the first row of the tab as a list of strings. Empty if no header."""
    rows = sheets.read_range(tab, "A1:ZZ1")
    if not rows:
        return []
    return [str(c).strip() for c in rows[0]]


def _header_matches(existing: List[str], schema: TelemetrySchema) -> bool:
    """Compare an existing header row to the schema's column list.

    Trailing empty cells in ``existing`` are ignored (Sheets sometimes returns
    extra empty cells when ZZ1 is queried). Otherwise the comparison is exact.
    """
    # Trim trailing empty cells
    trimmed = list(existing)
    while trimmed and not trimmed[-1]:
        trimmed.pop()

    expected = list(schema.columns)
    return trimmed == expected


def ensure_telemetry_tab(
    sheets: SheetsClient,
    tab_name: str,
    schema: TelemetrySchema,
) -> None:
    """Create the tab (idempotent) and write the header if missing.

    If the tab already has a header and it doesn't match the schema, raise
    ``SchemaMismatchError`` with a clear message. Caller must delete the tab
    manually before retrying.
    """
    sheets.ensure_tab(tab_name)

    existing = _read_existing_header(sheets, tab_name)
    if existing:
        # Tab has content. Confirm it matches our schema.
        if _header_matches(existing, schema):
            return  # all good, header is correct
        raise SchemaMismatchError(
            f"Tab '{tab_name}' exists but its header doesn't match the "
            f"'{schema.name}' schema. "
            f"Expected {schema.column_count} columns "
            f"(first={schema.columns[0]!r}, last={schema.columns[-1]!r}); "
            f"found {len(existing)} columns "
            f"(first={(existing[0] if existing else '')!r}, "
            f"last={(existing[-1] if existing else '')!r}). "
            f"To fix: delete the tab '{tab_name}' in the Sheets UI, then re-run."
        )

    # No header yet — write it
    sheets.ensure_header(tab_name, schema.header)


def write_telemetry_rows(
    sheets: SheetsClient,
    tab_name: str,
    schema: TelemetrySchema,
    rows: List[List[Any]],
    dry_run: bool = False,
) -> Dict[str, int]:
    """Upsert rows into the given telemetry tab.

    Returns the upsert stats dict ``{inserted, updated, unchanged}``. In dry-run
    mode, returns zeros without touching Sheets.

    Pre-flight: every row must have ``schema.column_count`` cells. Anything else
    is a programming error — fail loudly rather than write a misaligned row.
    """
    if not rows:
        return {"inserted": 0, "updated": 0, "unchanged": 0}

    expected = schema.column_count
    for idx, row in enumerate(rows):
        if len(row) != expected:
            raise ValueError(
                f"row {idx} has {len(row)} cells, schema '{schema.name}' "
                f"expects {expected}"
            )

    if dry_run:
        LOG.info(
            "[DRY RUN] would upsert %d rows into '%s' (key cols=%s)",
            len(rows),
            tab_name,
            list(schema.natural_key_columns),
        )
        return {"inserted": 0, "updated": 0, "unchanged": 0, "dry_run": len(rows)}  # type: ignore[dict-item]

    stats = sheets.upsert_rows(
        tab=tab_name,
        rows=rows,
        natural_key_columns=list(schema.natural_key_columns),
    )
    LOG.info(
        "Upserted '%s': inserted=%d updated=%d unchanged=%d",
        tab_name,
        stats.get("inserted", 0),
        stats.get("updated", 0),
        stats.get("unchanged", 0),
    )
    return stats
