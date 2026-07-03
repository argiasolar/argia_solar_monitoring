"""Monthly archive logic — plan #8 (pure parts).

At month end, the month's rows from the live Argia_Mont_v2 spreadsheet are
copied into a fresh archive spreadsheet ``Argia_Mont_Archive_YYYY_MM`` on
Drive, counts are verified, and ONLY THEN the archived telemetry rows are
pruned from the live sheet. KPI_Daily keeps its own 14-day pruning; the
Alerts ledger stays live (it is small and IS the operational history).

Everything here is pure and unit-tested; the script wires the I/O.

Safety invariants:
- copy -> verify -> prune, strictly in that order; a verify failure aborts
  the prune.
- pruning uses ONE contiguous row block per tab; if the month's rows are
  not contiguous (should never happen in an append-ordered tab), the prune
  for that tab is SKIPPED with a warning rather than guessed at.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

LOG = logging.getLogger("argia.archive.monthly")

ARCHIVE_TITLE_FMT = "Argia_Mont_Archive_{y:04d}_{m:02d}"

COPY_CHUNK_ROWS = 500
"""Rows per append call when copying into the archive. Deep tabs are ~140
columns wide; 500 rows keeps each request payload well under API limits."""

CELL_BUDGET_WARN = 8_000_000
"""Warn when the projected archive size approaches the 10M-cell limit."""

# Columns that are real datetimes in the live sheet. Read as
# UNFORMATTED_VALUE they come back as serial numbers; written RAW they'd
# display as 46174.12... unless the archive re-applies a date format.
# (timestamp_utc / *_utc columns are TEXT at the source and copy verbatim.)
DATETIME_FORMATS = {
    "date_iso": "yyyy-mm-dd",
    "timestamp_mx": "yyyy-mm-dd hh:mm:ss",
}


def datetime_format_columns(header: List[str]) -> List[Tuple[int, str]]:
    """(1-based column, pattern) for every datetime column in ``header``."""
    out: List[Tuple[int, str]] = []
    for i, h in enumerate(header, start=1):
        pattern = DATETIME_FORMATS.get(str(h).strip())
        if pattern:
            out.append((i, pattern))
    return out


def month_title(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return ARCHIVE_TITLE_FMT.format(y=y, m=m)


def previous_month(today: dt.date) -> str:
    first = today.replace(day=1)
    last_prev = first - dt.timedelta(days=1)
    return f"{last_prev.year:04d}-{last_prev.month:02d}"


@dataclass(frozen=True)
class MonthBlock:
    """Where one month's rows live inside a tab."""

    tab: str
    header: List[str]
    rows: List[List]          # the month's data rows (no header)
    start_row: int            # 1-indexed sheet row of the first month row
    end_row: int              # 1-indexed sheet row of the last month row
    contiguous: bool          # month rows form one unbroken block
    total_data_rows: int      # all data rows in the tab (any month)

    @property
    def count(self) -> int:
        return len(self.rows)


def locate_month_block(
    tab: str,
    data: List[List],
    month: str,
    key_of: Callable[[List], str],
) -> MonthBlock:
    """Find the month's rows in a tab's full data (header at data[0]).

    ``key_of(row)`` must return the row's "YYYY-MM-DD" day (or "" when the
    row can't be dated — such rows never match a month). Contiguity is
    checked explicitly: matching rows must form one unbroken block for the
    prune to be allowed.
    """
    header = [str(h) for h in (data[0] if data else [])]
    rows: List[List] = []
    first = last = None
    for i, row in enumerate(data[1:], start=2):     # sheet rows, 1-indexed
        day = key_of(row)
        if day[:7] == month:
            rows.append(row)
            if first is None:
                first = i
            last = i
    contiguous = bool(rows) and (last - first + 1 == len(rows))
    return MonthBlock(
        tab=tab, header=header, rows=rows,
        start_row=first or 0, end_row=last or 0,
        contiguous=contiguous,
        total_data_rows=max(0, len(data) - 1),
    )


def chunk_rows(rows: List[List], chunk: int = COPY_CHUNK_ROWS) -> List[List[List]]:
    return [rows[i:i + chunk] for i in range(0, len(rows), chunk)]


def projected_cells(blocks: List[MonthBlock]) -> int:
    return sum((b.count + 1) * max(1, len(b.header)) for b in blocks)


def verify_copy(block: MonthBlock, archived_data_rows: int) -> Tuple[bool, str]:
    """Row-count verification for one tab: archive must hold exactly the
    month's rows."""
    if archived_data_rows == block.count:
        return True, f"{block.tab}: {archived_data_rows} rows verified"
    return False, (f"{block.tab}: VERIFY FAILED — source month has "
                   f"{block.count} rows, archive has {archived_data_rows}")
