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
from argia.core.time_utils import UTC, utc_to_mx
from argia.kpi.irradiance import IrradianceSource
from argia.kpi.performance import Confidence, PlantPerformanceDay
from argia.kpi.reconcile import date_key

LOG = logging.getLogger("argia.archive.kpi_daily")

KPI_DAILY_TAB = "KPI_Daily"

HOT_WINDOW_DAYS = 14
"""How many days to keep in the live KPI_Daily tab."""

# ---------- coverage / data_class ----------
#
# KPI_Daily.energy_kwh is MAX(EToday) per inverter, summed. EToday is cumulative
# and monotonic within a day, so the daily total is correct *only if a sample
# landed after production ended*. On GitHub Actions the scheduler drops runs, so
# some days' last sample lands early afternoon and the total silently undercounts
# (e.g. 2026-06-30 last sample 13:18 -> ~35% low across the whole fleet). We stamp
# each row's coverage so the reconcile can tell "v2 undercounted" apart from
# "v2 disagrees". On the Pi (reliable cadence) days will almost always be 'full'.

DATA_CLASS_FULL = "full"
DATA_CLASS_PARTIAL = "partial"
DATA_CLASS_NO_DATA = "no_data"

DATA_COVERAGE_CUTOFF_HOUR = 18
"""A plant's last MX-local sample must be at/after this hour for the day to count
as 'full'. Conservative on purpose: a false 'partial' just drops a good day from
the reconcile; a false 'full' would let an undercounted day pass as a match."""

DATA_CLASS_COL_NAME = "data_class"

KPI_DAILY_HEADER = [
    "date_iso", "plant_key",
    "energy_kwh", "irradiance_kwh_m2", "irradiance_source",
    "pr", "pr_confidence", "capacity_factor", "capacity_factor_confidence",
    "inverters_reporting", "inverters_with_reboot",
    "notes", "written_at_utc", "pr_stc",
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
    """Bootstrap the KPI_Daily header. Idempotent and non-destructive.

    The live tab may carry MORE columns than KPI_DAILY_HEADER (extra analytics
    columns to the right, e.g. specific_yield / availability / expected_kwh).
    KPI_DAILY_HEADER is the prefix this job owns and writes; anything beyond it
    is left untouched. Cases:
      * empty tab            -> write our header.
      * existing starts with our header (same or richer) -> leave as-is.
      * existing is an older prefix of our header         -> append our cols.
      * anything else        -> leave as-is + warn (never clobber).
    """
    sheets.ensure_tab(KPI_DAILY_TAB)
    existing = sheets.read_range(KPI_DAILY_TAB, "A1:ZZ1")
    header = [str(c).strip() for c in (existing[0] if existing else [])]
    while header and not header[-1]:
        header.pop()
    n = len(KPI_DAILY_HEADER)
    if not header:
        sheets.ensure_header(KPI_DAILY_TAB, KPI_DAILY_HEADER)
        LOG.info("Bootstrapped %s (header only)", KPI_DAILY_TAB)
        return True
    if header[:n] == KPI_DAILY_HEADER:
        return False  # sheet already has all our columns (possibly plus extras)
    if KPI_DAILY_HEADER[: len(header)] == header:
        sheets.write_header_row(KPI_DAILY_TAB, KPI_DAILY_HEADER)
        LOG.info("Appended new KPI_Daily columns (now %d)", n)
        return True
    LOG.warning(
        "%s header diverges from expected (%d cols); leaving as-is",
        KPI_DAILY_TAB, len(header),
    )
    return False


# ---------- upsert ----------


def _kpi_key(date_cell, plant_cell) -> Tuple[str, str]:
    """Normalized natural key (date_iso, plant_key) for upsert matching.

    ``date_iso`` in the sheet may be TEXT ('2026-06-30', from a RAW insert) or a
    date SERIAL (after a USER_ENTERED update reparsed it). A naive ``str()`` key
    matches the first re-run but not the second, silently appending a duplicate.
    Normalizing both sides with ``date_key`` makes matching type-agnostic.
    """
    return (date_key(date_cell), normalize_text(plant_cell).upper())


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

    # Build key map from new rows (type-agnostic on date_iso)
    new_by_key = {_kpi_key(r[0], r[1]): r for r in new_rows}

    # Read existing rows (raw)
    try:
        existing = sheets.read_range(KPI_DAILY_TAB, "A2:N")
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
        key = _kpi_key(row[0], row[1])
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
    inserts = [r for r in new_rows if _kpi_key(r[0], r[1]) not in keys_seen]

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
            # USER_ENTERED (not RAW) so date_iso is parsed to a real date, exactly
            # like the update path (write_row) does. A RAW insert stored date_iso
            # as TEXT while updates stored it as a date, leaving the column mixed —
            # which breaks the downstream QUERY(IMPORTRANGE) in ARGIA_Solar (QUERY
            # infers one type per column and nulls the minority, dropping the
            # text-date rows from DailyData_v2 / Reconcile). written_at_utc's ISO
            # string is not date-parsed by Sheets, so it stays text as before.
            sheets.append_rows(KPI_DAILY_TAB, inserts, value_input_option="USER_ENTERED")
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


# ---------- coverage stamping ----------


def classify_coverage(
    last_sample_utc: Optional[dt.datetime],
    cutoff_hour: int = DATA_COVERAGE_CUTOFF_HOUR,
) -> str:
    """Classify a plant-day's telemetry coverage from its LAST sample time.

    ``last_sample_utc`` is the newest telemetry timestamp for the plant that day
    (aware UTC). Because EToday is cumulative and monotonic within a day, the
    daily MAX(EToday) is only trustworthy if a sample landed late enough:

        None                         -> "no_data"
        last MX-local hour >= cutoff -> "full"
        otherwise                    -> "partial"

    Pure function — no I/O.
    """
    if last_sample_utc is None:
        return DATA_CLASS_NO_DATA
    mx = utc_to_mx(last_sample_utc)
    return DATA_CLASS_FULL if mx.hour >= cutoff_hour else DATA_CLASS_PARTIAL


def stamp_column(
    sheets: SheetsClient,
    col_name: str,
    stamps: Dict[Tuple[str, str], object],
    dry_run: bool = False,
) -> int:
    """Write one named column's cells for the given (date_iso, plant_key) rows.

    Surgical: reads KPI_Daily once, finds ``col_name`` BY NAME, maps each
    (date, plant) to its sheet row, and writes only that one cell per row.
    Touches nothing else — safe regardless of what owns neighbouring columns.
    Shared by data_class / cloud_coverage_pct / expected_kwh / availability.

    Dates are normalized with ``date_key`` on both sides because KPI_Daily stores
    ``date_iso`` as a real date (read back as a serial), so a naive string compare
    would never match. Returns the number of cells written (counted but not
    written in dry-run; 0 if the column/rows aren't found).
    """
    if not stamps:
        return 0
    try:
        data = sheets.read_range(KPI_DAILY_TAB, "A1:ZZ")
    except Exception as e:  # noqa: BLE001
        LOG.warning("stamp_column(%s): could not read %s: %s",
                    col_name, KPI_DAILY_TAB, e)
        return 0
    if not data:
        return 0

    header = [normalize_text(h) for h in data[0]]
    if col_name not in header:
        LOG.warning(
            "stamp_column: KPI_Daily has no '%s' column; skipping",
            col_name,
        )
        return 0
    target_col = header.index(col_name)          # 0-based
    di_col = header.index("date_iso")
    pk_col = header.index("plant_key")

    # (canonical_date, PLANT) -> 1-based sheet row number
    key_to_row: Dict[Tuple[str, str], int] = {}
    for i, row in enumerate(data[1:], start=2):  # data row 1 == sheet row 2
        if di_col >= len(row) or pk_col >= len(row):
            continue
        d = date_key(row[di_col])
        pk = normalize_text(row[pk_col]).upper()
        if not d or not pk:
            continue
        key_to_row[(d, pk)] = i

    written = 0
    for (date_iso, plant_key), value in stamps.items():
        d = date_key(date_iso)
        pk = normalize_text(plant_key).upper()
        row_num = key_to_row.get((d, pk))
        if row_num is None:
            LOG.warning(
                "stamp_column(%s): no KPI_Daily row for (%s, %s) — skipping",
                col_name, date_iso, plant_key,
            )
            continue
        if dry_run:
            LOG.info("[DRY RUN] would stamp %s row %d %s=%s",
                     plant_key, row_num, col_name, value)
            written += 1
            continue
        sheets.write_cell(KPI_DAILY_TAB, row_num, target_col + 1, value)  # 1-based col
        written += 1
    return written


def stamp_data_class(
    sheets: SheetsClient,
    stamps: Dict[Tuple[str, str], str],
    dry_run: bool = False,
) -> int:
    """Write ``data_class`` for the given (date_iso, plant_key) rows.

    Thin wrapper over :func:`stamp_column` kept for a stable name at the
    call sites and in tests.
    """
    return stamp_column(sheets, DATA_CLASS_COL_NAME, stamps, dry_run=dry_run)


# ---------- cloud coverage ----------

CLOUD_COVERAGE_COL_NAME = "cloud_coverage_pct"

# Daylight window (MX local hours, inclusive start / exclusive end) used when
# averaging cloud cover. Cloud samples outside production hours say nothing
# about production conditions, and June-30-style stray night rows (00:40)
# would otherwise skew the mean.
CLOUD_DAYLIGHT_START_HOUR = 6
CLOUD_DAYLIGHT_END_HOUR = 20


def mean_cloud_cover(
    samples: List[Tuple[dt.datetime, Optional[float]]],
) -> Optional[float]:
    """Daylight-window mean of cloud-cover samples for one plant-day.

    ``samples`` is [(timestamp_utc, cloud_cover_pct), ...] straight from the
    telemetry rows; values stay in telemetry's native scale, which is PERCENT
    (0-100). NOTE: v1's DailyData.Cloud_Coverage is a FRACTION (0-1), so any
    consumer that must match v1 semantics (e.g. the DailyData_v2 QUERY at the
    Stage 5 repoint) divides this column by 100. Verified on real days:
    v2 62.5 / 100 = 0.625 vs v1 0.623 (SLP1 2026-06-30).

    Rules:
    - Only samples whose MX-local hour is in [06:00, 20:00) count.
    - ``None`` values are ignored.
    - No usable samples -> ``None`` (leave the cell alone rather than fake a 0).
    Pure function — no I/O.
    """
    vals = []
    for ts, cloud in samples:
        if cloud is None or ts is None:
            continue
        mx = utc_to_mx(ts)
        if CLOUD_DAYLIGHT_START_HOUR <= mx.hour < CLOUD_DAYLIGHT_END_HOUR:
            vals.append(float(cloud))
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


# ---------- expected energy ----------

EXPECTED_KWH_COL_NAME = "expected_kwh"


def compute_expected_kwh(
    kwp_dc: Optional[float],
    irradiance_kwh_m2: Optional[float],
    expected_factor: Optional[float],
) -> Optional[float]:
    """Expected daily energy: ``kwp_dc × irradiance × expected_factor``.

    Same formula and semantics as v1's Theoretical_kWh (verified: SLP1
    2024-03-01 = 189.2 × 6.01 × 0.73 = 830.08, matching v1's stored value),
    using ``expected_factor`` from the Plants tab — NOT ``pr_target``, which is
    the aspirational drift/soiling reference, not the realistic daily
    expectation. Any missing/non-positive input -> ``None`` (never fake a 0:
    a 0 would read as "expected nothing", which is very different from
    "couldn't compute").

    Pure function — no I/O.
    """
    if not kwp_dc or kwp_dc <= 0:
        return None
    if irradiance_kwh_m2 is None or irradiance_kwh_m2 <= 0:
        return None
    if not expected_factor or expected_factor <= 0:
        return None
    return round(kwp_dc * irradiance_kwh_m2 * expected_factor, 2)


# ---------- availability ----------

AVAILABILITY_COL_NAME = "availability"

SLOT_GAP_SEC = 300
"""Two samples further apart than this start a new poll-slot. Within one poll,
device-reported timestamps can spread several minutes (verified: one GTO1 poll
spans 10:55:35 -> 10:59:35), so calendar-minute keying fragments a single poll
into many slots and fakes low availability. Gap-clustering is robust for both
GitHub's sparse cadence and the Pi's future ~10-min cadence."""


def compute_availability(
    samples: List[Tuple[dt.datetime, str, Optional[int]]],
    expected_sns: List[str],
    slot_gap_sec: int = SLOT_GAP_SEC,
) -> Optional[float]:
    """Plant-day availability: mean over configured inverters of the fraction
    of daylight poll-slots in which the inverter reported status=1 (online).

    ``samples`` is [(timestamp_utc, inverter_sn, status), ...] from telemetry.
    ``expected_sns`` is the CONFIGURED inverter list (Inverters tab) — judging
    against config is deliberate: an inverter that dies and stops reporting
    entirely must drag availability down, not silently drop out of the mean.

    Slotting: daylight samples (06:00–19:59 MX) are sorted by time and
    clustered — a gap > ``slot_gap_sec`` starts a new slot — so one poll whose
    per-device timestamps spread a few minutes still counts as ONE slot.

    Semantics, on purpose:
    - status=1 in a slot -> available in that slot. status=3, or no row at all
      in that slot -> unavailable.
    - Online-but-0W counts as AVAILABLE: availability measures communication /
      uptime; underproduction is the performance indicators' job (plan #4).
    - No daylight slots, or no expected inverters -> ``None`` (unknowable, not 0).

    Pure function — no I/O. Returned value is a 0-1 fraction, 4 dp.
    """
    expected = [str(s).strip() for s in expected_sns if str(s).strip()]
    if not expected:
        return None

    daylight = []
    for ts, sn, status in samples:
        if ts is None:
            continue
        mx = utc_to_mx(ts)
        if CLOUD_DAYLIGHT_START_HOUR <= mx.hour < CLOUD_DAYLIGHT_END_HOUR:
            daylight.append((ts, str(sn).strip(), status))
    if not daylight:
        return None

    daylight.sort(key=lambda x: x[0])
    n_slots = 0
    online: Dict[str, int] = {}
    slot_online: set = set()
    prev_ts = None
    for ts, sn, status in daylight:
        if prev_ts is None or (ts - prev_ts).total_seconds() > slot_gap_sec:
            # close previous slot, open a new one
            for s in slot_online:
                online[s] = online.get(s, 0) + 1
            slot_online = set()
            n_slots += 1
        prev_ts = ts
        if status == 1:
            slot_online.add(sn)
    for s in slot_online:  # close the last slot
        online[s] = online.get(s, 0) + 1

    frac = sum(online.get(sn, 0) / n_slots for sn in expected) / len(expected)
    return round(frac, 4)


def normalize_kpi_date_iso(sheets: SheetsClient, dry_run: bool = True) -> Dict[str, int]:
    """One-time repair: rewrite any TEXT ``date_iso`` cell as a real date.

    Old RAW inserts stored ``date_iso`` as text while updates stored it as a real
    date, leaving the column mixed. The downstream ``QUERY(IMPORTRANGE(...))`` in
    ARGIA_Solar infers one type per column and nulls the minority, so the text
    rows silently drop out of DailyData_v2 / Reconcile until reformatted by hand.
    This converts the text cells to real dates so the whole column is uniform.

    ``read_range`` returns a real-date cell as a serial (number) and a text-date
    cell as a string, so a string value is the signal that a cell needs fixing.
    Only ``date_iso`` cells are touched — nothing else. Dry-run by default.
    Returns counts: scanned / text_dates / fixed.
    """
    result = {"scanned": 0, "text_dates": 0, "fixed": 0}
    try:
        data = sheets.read_range(KPI_DAILY_TAB, "A1:ZZ")
    except Exception as e:  # noqa: BLE001
        LOG.warning("normalize_kpi_date_iso: could not read %s: %s", KPI_DAILY_TAB, e)
        return result
    if not data:
        return result
    header = [normalize_text(h) for h in data[0]]
    if "date_iso" not in header:
        LOG.warning("normalize_kpi_date_iso: no date_iso column; nothing to do")
        return result
    di = header.index("date_iso")

    for i, row in enumerate(data[1:], start=2):  # data row 1 == sheet row 2
        if di >= len(row):
            continue
        cell = row[di]
        if cell is None or cell == "":
            continue
        result["scanned"] += 1
        if not isinstance(cell, str):
            continue  # already a real date (serial number)
        result["text_dates"] += 1
        canon = date_key(cell)  # -> "YYYY-MM-DD" (or "" if unparseable)
        if not canon:
            LOG.warning("normalize_kpi_date_iso: row %d unparseable date %r — skipping",
                        i, cell)
            continue
        if dry_run:
            LOG.info("[DRY RUN] row %d: would convert text %r -> date %s", i, cell, canon)
            result["fixed"] += 1
            continue
        sheets.write_cell(KPI_DAILY_TAB, i, di + 1, canon,
                          value_input_option="USER_ENTERED")
        result["fixed"] += 1

    LOG.info("normalize_kpi_date_iso: scanned=%d text_dates=%d fixed=%d%s",
             result["scanned"], result["text_dates"], result["fixed"],
             " (dry-run)" if dry_run else "")
    return result


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
        existing = sheets.read_range(KPI_DAILY_TAB, "A2:N")
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
