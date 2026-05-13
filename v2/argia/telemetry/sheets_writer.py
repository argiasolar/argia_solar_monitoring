"""Sheets writer for telemetry tabs.

Thin wrapper around ``SheetsClient`` that:
1. Ensures the target tab exists (creates if not).
2. Ensures the header row matches the schema (writes if A1 is empty).
3. Upserts rows on the schema's natural key.

The upsert behavior matters for live data: if a 5-min cron fires before the
inverter produces a new sample, the call effectively becomes a no-op for that
inverter (same key, same data, ``unchanged: 1``). Gaps in timestamps then
become visible signals downstream rather than getting papered over by repeated
appends of the same row.

Side effects are explicit — every public function writes to Sheets unless
``dry_run=True`` is passed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from argia.core.sheets import SheetsClient
from argia.telemetry.schema import TelemetrySchema

LOG = logging.getLogger("argia.telemetry.sheets_writer")


def ensure_telemetry_tab(
    sheets: SheetsClient,
    tab_name: str,
    schema: TelemetrySchema,
) -> None:
    """Create the tab (idempotent) and write the header if missing.

    Both operations are idempotent on the SheetsClient side:
    - ``ensure_tab`` is a no-op if the tab exists
    - ``ensure_header`` only writes if A1:ZZ1 is empty

    So calling this at the start of every cron run is safe and cheap.
    """
    sheets.ensure_tab(tab_name)
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
