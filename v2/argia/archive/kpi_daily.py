"""KPI_Daily archive — Stage 7.3.

Append-only daily KPI persistence. One row per (date, plant_key). Live
sheet keeps the most recent ``HOT_WINDOW_DAYS`` days (default 14); older
rows are pruned by the EOD cron. Pruned rows are gone — there is no warm
archive in 7.3. If you need history beyond 14 days, Stage 7.6 will add a
yearly archive tab.

Why 14 days and not 7 or 30:
- 7 days is too short to smooth weekly weather cycles (one stretch of
  overcast days swallows the average)
- 30 days bloats the active sheet and slows reads on the Pi
- 14 days is enough to catch a rolling-week median and span both
  weekend/weekday patterns without resorting to a separate archive

Schema (KPI_Daily tab):

    date_iso              — local plant date (YYYY-MM-DD)
    plant_key
    energy_kwh            — end-of-day from sum of inverter etoday_kwh
    irradiance_kwh_m2     — daily integral
    irradiance_source     — shinemaster | cloud_cover_model | none
    pr                    — Performance Ratio
    pr_confidence         — HIGH | MEDIUM | LOW | NONE
    capacity_factor       — daily CF
    capacity_factor_confidence
    inverters_reporting   — count of inverters that contributed energy
    inverters_with_reboot — count of inverters that mid-day reset etoday
    notes                 — diagnostic string from compute_plant_pr()
    written_at_utc        — when this row was written

Natural key: (date_iso, plant_key). Re-running EOD for the same day
*overwrites* that day's row (idempotent), not appends.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from argia.core.normalize import normalize_text, safe_float
from argia.core.sheets import SheetsClient
from argia.core.time_utils import UTC
from argia.kpi.irradiance import IrradianceSource
from argia.kpi.performance import Confidence, PlantPerformanceDay

LOG = logging.getLogger("argia.archive.kpi_daily")

KPI_DAILY_TAB = "KPI_Daily"

HOT_WINDOW_DAYS = 14
"""How many days to keep in the live KPI_Daily tab."""

KPI_DAILY_HEADER = [
    "date_iso", "plant_key",
    "energy_kwh", "irradiance_kwh_m2", "irradiance_source",
    "pr", "pr_confidence", "capacity_factor", "capacity_factor_confidence",
    "inverters_reporting", "inverters_with_reboot",
    "notes", "written_at_utc",
    "pr_stc", "module_temp_c",
]


# ---------- data structure ----------


@dataclass(frozen=True)
class KpiDailyRow:
    """One row in KPI_Daily, typed."""

    date_iso: str
    plant_key: str
    energy_kwh: Optional[float]
    irradiance_kwh_m2: Optional[float]
    irradiance_source: str
    pr: Optional[float]
    pr_confidence: str
    capacity_factor: Optional[float]
    capacity_factor_confidence: str
    inverters_reporting: int
    inverters_with_reboot: int
    notes: str
    written_at_utc: str
    pr_stc: Optional[float] = None
    module_temp_c: Optional[float] = None


# ---------- serialization ----------


def perf_to_row(
    perf: PlantPerformanceDay, now_utc: Optional[dt.datetime] = None,
) -> List:
    """Convert a PlantPerformanceDay (from Stage 7.2) to a sheet row."""
    written = (now_utc or dt.datetime.now(UTC)).replace(microsecond=0)
    return [
        perf.date_iso,
        perf.plant_key,
        "" if perf.energy_kwh is None else perf.energy_kwh,
        "" if perf.irradiance_kwh_m2 is None else perf.irradiance_kwh_m2,
        perf.irradiance_source.value,
        "" if perf.pr is None else perf.pr,
        perf.pr_confidence.value,
        "" if perf.capacity_factor is None else perf.capacity_factor,
        perf.capacity_factor_confidence.value,
        perf.inverters_with_data,
        perf.inverters_with_reboot,
        perf.notes,
        written.isoformat(),
        "" if perf.pr_stc is None else perf.pr_stc,
        "" if perf.module_temp_c is None else perf.module_temp_c,
    ]


def row_to_kpi(row: Dict) -> Optional[KpiDailyRow]:
    """Parse a sheet row dict (header → value) back into a KpiDailyRow.
    Returns None for unparseable rows (caller logs and continues)."""
    date_iso = normalize_text(row.get("date_iso"))
    plant_key = normalize_text(row.get("plant_key"))
    if not date_iso or not plant_key:
        return None
    # Validate date format — drop garbage rows defensively
    try:
        dt.date.fromisoformat(date_iso)
    except (ValueError, TypeError):
        return None
    return KpiDailyRow(
        date_iso=date_iso,
        plant_key=plant_key,
        energy_kwh=safe_float(row.get("energy_kwh")),
        irradiance_kwh_m2=safe_float(row.get("irradiance_kwh_m2")),
        irradiance_source=normalize_text(row.get("irradiance_source")),
        pr=safe_float(row.get("pr")),
        pr_confidence=normalize_text(row.get("pr_confidence")).upper(),
        capacity_factor=safe_float(row.get("capacity_factor")),
        capacity_factor_confidence=normalize_text(
            row.get("capacity_factor_confidence")
        ).upper(),
        inverters_reporting=int(safe_float(row.get("inverters_reporting"), 0) or 0),
        inverters_with_reboot=int(safe_float(row.get("inverters_with_reboot"), 0) or 0),
        notes=normalize_text(row.get("notes")),
        written_at_utc=normalize_text(row.get("written_at_utc")),
        pr_stc=safe_float(row.get("pr_stc")),
        module_temp_c=safe_float(row.get("module_temp_c")),
    )


# ---------- public API ----------


def load_kpi_daily(sheets: SheetsClient) -> List[KpiDailyRow]:
    """Read all rows from KPI_Daily. Returns empty list on tab error."""
    try:
        raw = sheets.read_table(KPI_DAILY_TAB, "A1:O")
    except Exception as e:
        LOG.warning("Could not read %s: %s — returning []", KPI_DAILY_TAB, e)
        return []
    out: List[KpiDailyRow] = []
    for r in raw:
        parsed = row_to_kpi(r)
        if parsed is not None:
            out.append(parsed)
    LOG.info("Loaded %d rows from %s", len(out), KPI_DAILY_TAB)
    return out


def rows_for_window(
    rows: List[KpiDailyRow],
    end_date_iso: str,
    window_days: int,
    plant_key: Optional[str] = None,
) -> List[KpiDailyRow]:
    """Filter to a rolling-window. ``end_date_iso`` is INCLUDED.

    Example: end_date_iso='2026-05-14', window_days=7 →
        dates 2026-05-08 through 2026-05-14 inclusive.

    If plant_key is given, returns only that plant's rows.
    Returns rows sorted by date ascending then plant_key.
    """
    end_date = dt.date.fromisoformat(end_date_iso)
    start_date = end_date - dt.timedelta(days=window_days - 1)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()

    out = [
        r for r in rows
        if start_iso <= r.date_iso <= end_iso
        and (plant_key is None or r.plant_key == plant_key)
    ]
    out.sort(key=lambda r: (r.date_iso, r.plant_key))
    return out


def rows_for_plant_history(
    rows: List[KpiDailyRow],
    plant_key: str,
) -> List[KpiDailyRow]:
    """All available rows for one plant, oldest first."""
    out = [r for r in rows if r.plant_key == plant_key]
    out.sort(key=lambda r: r.date_iso)
    return out


def create_kpi_daily_tab_if_missing(sheets: SheetsClient) -> bool:
    """Bootstrap the KPI_Daily header. Idempotent, and self-heals the
    additive schema change: if the tab already carries the previous header
    (the current one minus the trailing pr_stc/module_temp_c columns), the
    header row is rewritten to the full current header. Existing data rows
    keep their values and read back blank in the new trailing columns.

    Returns True when the header was written or migrated, False when it was
    already current or left untouched.
    """
    sheets.ensure_tab(KPI_DAILY_TAB)
    existing = sheets.read_range(KPI_DAILY_TAB, "A1:ZZ1")
    header = [str(c).strip() for c in (existing[0] if existing else [])]
    while header and not header[-1]:
        header.pop()
    if not header:
        sheets.ensure_header(KPI_DAILY_TAB, KPI_DAILY_HEADER)
        LOG.info("Bootstrapped %s (header only)", KPI_DAILY_TAB)
        return True
    if header == KPI_DAILY_HEADER:
        return False
    if header == KPI_DAILY_HEADER[: len(header)]:
        # Older header that is a prefix of the current one -> append new cols.
        sheets.write_header_row(KPI_DAILY_TAB, KPI_DAILY_HEADER)
        LOG.info("Migrated %s header to %d cols", KPI_DAILY_TAB, len(KPI_DAILY_HEADER))
        return True
    LOG.warning(
        "%s header is unexpected (%d cols); leaving as-is", KPI_DAILY_TAB, len(header)
    )
    return False


# ---------- upsert ----------


def upsert_kpi_rows(
    sheets: SheetsClient,
    new_rows: List[List],
    dry_run: bool = False,
) -> Dict[str, int]:
    """Upsert one or more KPI_Daily rows.

    Key: (date_iso, plant_key). If a row already exists for that key, it
    is overwritten. Otherwise the new row is appended.

    Args:
        new_rows: list of 13-cell lists in KPI_DAILY_HEADER order
            (typically from perf_to_row()).
        dry_run: if True, return stats but write nothing.

    Returns:
        dict with counts: inserted, updated, unchanged, failed.
    """
    if not new_rows:
        return {"inserted": 0, "updated": 0, "unchanged": 0, "failed": 0}

    # Validate row widths early — easier to debug than a sheets error
    for i, row in enumerate(new_rows):
        if len(row) != len(KPI_DAILY_HEADER):
            raise ValueError(
                f"new_rows[{i}] has {len(row)} cells, "
                f"expected {len(KPI_DAILY_HEADER)}"
            )

    # Build key map from new rows
    new_by_key = {(str(r[0]), str(r[1])): r for r in new_rows}

    # Read existing rows (raw)
    try:
        existing = sheets.read_range(KPI_DAILY_TAB, "A2:O")
    except Exception as e:
        LOG.warning("Could not read %s for upsert: %s", KPI_DAILY_TAB, e)
        existing = []

    # Walk existing rows: track which keys are updates vs untouched
    updates: List[Tuple[int, List]] = []  # (sheet_row_number, new_cells)
    keys_seen: set = set()
    unchanged = 0
    for i, row in enumerate(existing):
        if len(row) < 2:
            continue
        key = (str(row[0]).strip(), str(row[1]).strip())
        if not key[0] or not key[1]:
            continue
        if key in new_by_key:
            new_cells = new_by_key[key]
            # Sheets row index: A2 is row 2
            sheet_row_num = i + 2
            # Compare existing vs new to skip no-op updates
            old_str = [str(c) for c in row[:len(KPI_DAILY_HEADER)]]
            new_str = [str(c) for c in new_cells]
            if old_str == new_str:
                unchanged += 1
                keys_seen.add(key)
            else:
                updates.append((sheet_row_num, new_cells))
                keys_seen.add(key)

    # Inserts = new_rows whose key wasn't found in existing
    inserts = [r for r in new_rows if (str(r[0]), str(r[1])) not in keys_seen]

    if dry_run:
        LOG.info(
            "DRY RUN: would insert=%d update=%d unchanged=%d",
            len(inserts), len(updates), unchanged,
        )
        return {
            "inserted": len(inserts),
            "updated": len(updates),
            "unchanged": unchanged,
            "failed": 0,
        }

    failed = 0
    # Apply updates first (in-place writes)
    for sheet_row_num, cells in updates:
        try:
            sheets.write_row(KPI_DAILY_TAB, sheet_row_num, cells)
        except Exception as e:
            LOG.warning("Failed to update %s row %d: %s",
                        KPI_DAILY_TAB, sheet_row_num, e)
            failed += 1
    # Then appends
    if inserts:
        try:
            sheets.append_rows(KPI_DAILY_TAB, inserts, value_input_option="RAW")
        except Exception as e:
            LOG.error("Failed to append %d rows to %s: %s",
                      len(inserts), KPI_DAILY_TAB, e)
            failed += len(inserts)

    LOG.info(
        "Upsert KPI_Daily: inserted=%d updated=%d unchanged=%d failed=%d",
        len(inserts), len(updates), unchanged, failed,
    )
    return {
        "inserted": len(inserts) - failed if failed <= len(inserts) else 0,
        "updated": len(updates),
        "unchanged": unchanged,
        "failed": failed,
    }


# ---------- pruning ----------


def find_prunable_rows(
    sheets: SheetsClient,
    today_iso: str,
    window_days: int = HOT_WINDOW_DAYS,
) -> List[int]:
    """Return SHEET ROW NUMBERS (1-indexed) of rows whose date is older
    than the rolling window. Read-only — does NOT delete anything.

    Use this to preview a prune. The actual deletion happens via
    ``prune_old_rows`` with ``apply=True``.
    """
    today = dt.date.fromisoformat(today_iso)
    cutoff = today - dt.timedelta(days=window_days)
    cutoff_iso = cutoff.isoformat()

    try:
        existing = sheets.read_range(KPI_DAILY_TAB, "A2:O")
    except Exception as e:
        LOG.warning("Could not read %s for prune scan: %s", KPI_DAILY_TAB, e)
        return []

    out: List[int] = []
    for i, row in enumerate(existing):
        if not row:
            continue
        date_cell = str(row[0]).strip() if row else ""
        if not date_cell:
            continue
        try:
            dt.date.fromisoformat(date_cell)
        except (ValueError, TypeError):
            continue
        if date_cell < cutoff_iso:
            out.append(i + 2)  # A2 is row 2

    return out


def prune_old_rows(
    sheets: SheetsClient,
    today_iso: str,
    window_days: int = HOT_WINDOW_DAYS,
    apply: bool = False,
) -> Dict[str, int]:
    """Remove rows older than the rolling window. **DESTRUCTIVE when apply=True.**

    Defaults to dry-run for safety. Pass ``apply=True`` to actually delete.

    Args:
        today_iso: anchor date; rows older than (today - window_days) are removed.
        window_days: rolling window size in days. Default 14.
        apply: if False, log what would be deleted and return; do NOT delete.

    Returns:
        dict: {found, deleted, kept}
    """
    prunable_rows = find_prunable_rows(sheets, today_iso, window_days)

    if not prunable_rows:
        LOG.info("No rows older than %d days; nothing to prune", window_days)
        return {"found": 0, "deleted": 0, "kept": 0}

    if not apply:
        LOG.warning(
            "DRY RUN: would delete %d rows from %s "
            "(rows %s). Pass apply=True to actually delete.",
            len(prunable_rows), KPI_DAILY_TAB,
            f"{prunable_rows[0]}..{prunable_rows[-1]}",
        )
        return {"found": len(prunable_rows), "deleted": 0, "kept": 0}

    # Delete from the bottom up so row indices stay stable during deletion
    deleted = 0
    failed = 0
    for sheet_row_num in sorted(prunable_rows, reverse=True):
        try:
            sheets.delete_row(KPI_DAILY_TAB, sheet_row_num)
            deleted += 1
        except Exception as e:
            LOG.warning("Failed to delete %s row %d: %s",
                        KPI_DAILY_TAB, sheet_row_num, e)
            failed += 1

    LOG.info("Pruned %d rows from %s (failed: %d)",
             deleted, KPI_DAILY_TAB, failed)
    return {"found": len(prunable_rows), "deleted": deleted, "kept": failed}
