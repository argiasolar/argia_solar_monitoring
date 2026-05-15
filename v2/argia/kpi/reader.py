"""Reader — load one day's telemetry rows from ``Telemetry_Argia``.

Why ``Telemetry_Argia`` and not ``Telemetry_<KEY>``:
- Cross-vendor uniform shape (15 cols, known indices)
- Already has irradiance + cloud cover joined per row (saves a re-join)
- Tested for idempotency, so no duplicate rows

Stage 7.2 limitation: only reads the live tab. Stage 7.3 will add a daily
archive (``Telemetry_Argia_Archive``) and update this reader to fall back
to it for dates older than today.

Returned structure: ``DayBundle`` — all rows for one date, partitioned by
plant_key with helper lookups. Pure data, no methods that touch the sheet.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from argia.core.normalize import normalize_sn, normalize_text, safe_float
from argia.core.sheets import SheetsClient
from argia.core.time_utils import MX_TZ, UTC

LOG = logging.getLogger("argia.kpi.reader")


# Column positions in Telemetry_Argia (ARGIA_COMMON_COLS).
# Pinning these as constants means the reader doesn't need to depend on
# the schema module — and if the schema ever shifts, this is the only
# place we have to update.
COL_TIMESTAMP_UTC = 0
COL_TIMESTAMP_MX = 1
COL_VENDOR = 2
COL_PLANT_KEY = 3
COL_INVERTER_SN = 4
COL_INVERTER_LABEL = 5
COL_STATUS = 6
COL_POWER_W = 7
COL_ETODAY_KWH = 8
COL_TEMPERATURE_C = 9
COL_FAULT_CODE = 10
COL_IRRADIANCE_WM2 = 11
COL_IRRADIANCE_KWH_M2_5M = 12
COL_CLOUD_COVER_PCT = 13
COL_AMBIENT_TEMP_C = 14

ARGIA_TAB_NAME = "Telemetry_Argia"


@dataclass(frozen=True)
class InverterRow:
    """One row from Telemetry_Argia, typed for KPI use.

    All fields can be None when the source row had blank/garbage values.
    Higher-level code is responsible for skipping rows where required
    fields are missing.
    """

    timestamp_utc: dt.datetime
    plant_key: str
    inverter_sn: str
    inverter_label: str
    vendor: str
    status: int                           # 1 online, 3 offline
    power_w: Optional[float]
    etoday_kwh: Optional[float]
    temperature_c: Optional[float]
    fault_code: str
    irradiance_wm2: Optional[float]       # from ShineMaster — instantaneous
    irradiance_kwh_m2_5m: Optional[float]  # pre-computed Δkwh over 5 min
    cloud_cover_pct: Optional[float]
    ambient_temp_c: Optional[float]


@dataclass(frozen=True)
class DayBundle:
    """All telemetry rows for one date, partitioned by plant.

    Use the helper methods rather than accessing ``rows`` directly — they
    encode the "filter to this date in MX local time" logic and return
    consistent orderings.
    """

    date_iso: str
    rows: Tuple[InverterRow, ...] = ()

    # Index: plant_key → list of rows for that plant, time-sorted ascending.
    _by_plant: Dict[str, List[InverterRow]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        idx: Dict[str, List[InverterRow]] = {}
        for r in self.rows:
            idx.setdefault(r.plant_key, []).append(r)
        for k in idx:
            idx[k].sort(key=lambda r: r.timestamp_utc)
        object.__setattr__(self, "_by_plant", idx)

    def plant_keys(self) -> List[str]:
        """All plant_keys that have at least one row this day."""
        return sorted(self._by_plant.keys())

    def rows_for_plant(self, plant_key: str) -> List[InverterRow]:
        """Time-sorted rows for one plant. Empty list if no data."""
        return list(self._by_plant.get(plant_key, []))

    def rows_for_inverter(
        self, plant_key: str, inverter_sn: str,
    ) -> List[InverterRow]:
        """Time-sorted rows for one specific inverter."""
        sn = normalize_sn(inverter_sn)
        return [
            r for r in self._by_plant.get(plant_key, [])
            if r.inverter_sn == sn
        ]

    def inverter_sns_for_plant(self, plant_key: str) -> List[str]:
        """Unique inverter SNs that appeared in this plant's rows today.

        Note: returns SNs OBSERVED in telemetry, which can differ from
        the Inverters tab's roster — a configured inverter with no rows
        will not appear here. That's the right behavior for KPI: we can
        only compute things from data we actually have."""
        seen = set()
        out: List[str] = []
        for r in self._by_plant.get(plant_key, []):
            if r.inverter_sn not in seen:
                seen.add(r.inverter_sn)
                out.append(r.inverter_sn)
        return out


# ---------- row parsing ----------


def _parse_timestamp(cell) -> Optional[dt.datetime]:
    """Parse a Telemetry_Argia timestamp_utc cell into an aware UTC datetime.

    Sheets returns these as either an ISO string (USER_ENTERED kept text)
    or a float serial number (USER_ENTERED parsed as date). We handle both.
    """
    if cell is None or cell == "":
        return None
    if isinstance(cell, (int, float)):
        # Google Sheets serial date (days since 1899-12-30)
        try:
            base = dt.datetime(1899, 12, 30, tzinfo=UTC)
            return base + dt.timedelta(days=float(cell))
        except (ValueError, OverflowError):
            return None
    s = str(cell).strip()
    if not s:
        return None
    try:
        # Handle 'Z' suffix
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _parse_status(cell) -> int:
    """Parse status cell. Default to 1 (online) when unknown — KPI math
    treats offline as 'no contribution'; an unknown status row is more
    likely a write quirk than a real offline state."""
    f = safe_float(cell)
    if f is None:
        return 1
    try:
        return int(f)
    except (ValueError, TypeError):
        return 1


def _row_from_cells(cells: List) -> Optional[InverterRow]:
    """Convert a raw sheet row (list of cells) into an InverterRow.

    Returns None if essential identity fields (timestamp, plant_key, sn)
    are missing — those rows are unusable for KPI.
    """
    # Pad short rows so indexing doesn't crash on tail-empty cells
    if len(cells) < 15:
        cells = list(cells) + [""] * (15 - len(cells))

    ts = _parse_timestamp(cells[COL_TIMESTAMP_UTC])
    plant_key = normalize_text(cells[COL_PLANT_KEY])
    sn = normalize_sn(cells[COL_INVERTER_SN])
    if ts is None or not plant_key or not sn:
        return None

    return InverterRow(
        timestamp_utc=ts,
        plant_key=plant_key,
        inverter_sn=sn,
        inverter_label=normalize_text(cells[COL_INVERTER_LABEL]),
        vendor=normalize_text(cells[COL_VENDOR]).upper(),
        status=_parse_status(cells[COL_STATUS]),
        power_w=safe_float(cells[COL_POWER_W]),
        etoday_kwh=safe_float(cells[COL_ETODAY_KWH]),
        temperature_c=safe_float(cells[COL_TEMPERATURE_C]),
        fault_code=normalize_text(cells[COL_FAULT_CODE]),
        irradiance_wm2=safe_float(cells[COL_IRRADIANCE_WM2]),
        irradiance_kwh_m2_5m=safe_float(cells[COL_IRRADIANCE_KWH_M2_5M]),
        cloud_cover_pct=safe_float(cells[COL_CLOUD_COVER_PCT]),
        ambient_temp_c=safe_float(cells[COL_AMBIENT_TEMP_C]),
    )


def parse_rows(raw_rows: List[List]) -> List[InverterRow]:
    """Public for testing: convert a list of raw cell lists into typed rows.

    Skips the header row (detected as first row containing 'timestamp_utc'
    in cell 0) and any row that fails parsing. Returns rows in original
    sheet order — callers should not rely on this; use DayBundle methods
    which sort by timestamp."""
    if not raw_rows:
        return []

    start = 0
    first_cell = str(raw_rows[0][0]) if raw_rows[0] else ""
    if first_cell.lower().startswith("timestamp"):
        start = 1

    out: List[InverterRow] = []
    skipped = 0
    for cells in raw_rows[start:]:
        row = _row_from_cells(cells)
        if row is None:
            skipped += 1
            continue
        out.append(row)
    if skipped:
        LOG.info("Skipped %d unparseable rows", skipped)
    return out


# ---------- date filtering ----------


def _date_window_utc(date_iso: str, site_tz=MX_TZ) -> Tuple[dt.datetime, dt.datetime]:
    """Return [start_utc, end_utc) for a given LOCAL date.

    Solar plants operate on local civil time. A "day" is 00:00 to 24:00
    in the plant's local timezone, then converted to UTC for filtering.
    Returns half-open interval so consecutive days don't double-count.
    """
    try:
        date = dt.date.fromisoformat(date_iso)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid date_iso '{date_iso}': {e}") from e

    start_local = dt.datetime.combine(date, dt.time(0, 0), tzinfo=site_tz)
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def filter_to_date(
    rows: List[InverterRow],
    date_iso: str,
    site_tz=MX_TZ,
) -> List[InverterRow]:
    """Return only the rows whose timestamp_utc falls in the local date.

    Pure function — easy to test with synthetic rows."""
    start_utc, end_utc = _date_window_utc(date_iso, site_tz)
    return [r for r in rows if start_utc <= r.timestamp_utc < end_utc]


# ---------- public entry ----------


def read_day_bundle(
    sheets: SheetsClient,
    date_iso: str,
    tab_name: str = ARGIA_TAB_NAME,
    site_tz=MX_TZ,
) -> DayBundle:
    """Read all telemetry rows from ``tab_name`` and return a DayBundle
    filtered to one local date.

    Uses the live Telemetry_Argia tab. Stage 7.3 will extend this to
    read from an archive tab for past dates.
    """
    try:
        raw_rows = sheets.read_range(tab_name, "A1:O")
    except Exception as e:
        LOG.warning("Could not read %s: %s — returning empty DayBundle", tab_name, e)
        return DayBundle(date_iso=date_iso)

    all_rows = parse_rows(raw_rows)
    LOG.info(
        "%s: %d total rows parsed; filtering to date %s",
        tab_name, len(all_rows), date_iso,
    )
    day_rows = filter_to_date(all_rows, date_iso, site_tz)
    LOG.info(
        "DayBundle %s: %d rows across %d plants",
        date_iso, len(day_rows),
        len({r.plant_key for r in day_rows}),
    )
    return DayBundle(date_iso=date_iso, rows=tuple(day_rows))
