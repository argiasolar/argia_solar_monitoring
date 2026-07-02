"""Daily energy reconciliation — v2 (KPI_Daily) vs v1 (ARGIA_Solar DailyData).

STAGE 1 of the v1→v2 migration: prove that v2's *collection* is at least as
good as v1's, while the two collectors are still independent (v1 on the Pi,
v2 on GitHub Actions). This window is the only time the comparison is a real
test of collection quality — once v2 moves to the Pi and dual-writes from a
single poll, both feeds share one collection and the comparison goes
tautological. So we build and run it now.

WHAT IT COMPARES
    energy  = the GATE. v2 KPI_Daily.energy_kwh vs v1 DailyData.Real_kWh, per
              plant per day. If these agree within tolerance, v2 is collecting
              as well as v1. This is the pass/fail signal.
    PR      = the DIAGNOSTIC (alongside, not a gate). v1's PR is *derived* with
              the same formula v2 uses — PR = E / (kwp_dc * irradiance_kwh_m2) —
              from v1's own DailyData numbers. When energy agrees but PR
              diverges, the cause is config (different kwp) or a different
              irradiance source, NOT collection. We surface v1_kwp, v1_irr and
              v2_irr so the divergence can be attributed precisely rather than
              guessed at.

DESIGN
    This module is PURE: no I/O, no SheetsClient, no network. It takes plain
    lists of dicts (as returned by SheetsClient.read_table) and returns typed
    ReconcileRow objects. All the fragile bits (Google Sheets serial dates,
    mixed key types, zero denominators) are handled here and unit-tested. The
    thin script (scripts/reconcile_daily.py) only does the reads and printing.

    Nothing in this module or its script ever WRITES to either sheet.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from argia.core.normalize import normalize_text, safe_float

# --------------------------------------------------------------------------
# Buckets — the classification a plant/day lands in.
# --------------------------------------------------------------------------
BUCKET_OK = "OK"                    # energy agrees within tolerance
BUCKET_ENERGY = "ENERGY-MISMATCH"   # energy differs > tolerance  -> collection problem (GATE FAIL)
BUCKET_PR = "PR-DIVERGENCE"         # energy agrees but PR differs -> config/irradiance (EXPECTED)
BUCKET_PARTIAL_V2 = "PARTIAL-V2"    # v2 day flagged incomplete (data_class=partial) -> not comparable
BUCKET_MISSING_V1 = "MISSING-V1"    # v2 has the day, v1 does not
BUCKET_MISSING_V2 = "MISSING-V2"    # v1 has the day, v2 does not

# v2 KPI_Daily.data_class value that means the day undercounted (last sample too
# early). Such a day can't fairly be compared, so it gets its own bucket and does
# NOT fail the energy gate.
DATA_CLASS_PARTIAL = "partial"

# Google Sheets / Excel serial-date epoch. Serial 0 == 1899-12-30.
_SHEETS_EPOCH = dt.date(1899, 12, 30)
# Plausible serial range for real dates (~1954 .. ~2119). Chosen so a serial
# date never collides with a 10-digit epoch (seconds) or 13-digit epoch (ms).
_SERIAL_MIN = 20000
_SERIAL_MAX = 80000


# --------------------------------------------------------------------------
# Normalizers — the type-fragility this whole exercise kept getting burned by.
# --------------------------------------------------------------------------
def date_key(value: Any) -> Optional[str]:
    """Normalize any date representation to an ISO ``YYYY-MM-DD`` string.

    Handles the four shapes these two sheets actually produce:
      - Google Sheets serial number (int/float, e.g. 46203 or 46203.5) — this
        is what ``UNFORMATTED_VALUE`` returns for a real date cell.
      - ``datetime.datetime`` / ``datetime.date`` (openpyxl / fixtures).
      - ISO string ``"2026-06-30"`` or ``"2026-06-30T..."``.
      - US string ``"6/30/2026"`` (some legacy DailyData rows).

    Returns None for blank/unparseable input. Never raises.
    """
    if value is None:
        return None

    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()

    # Serial number (the common API case).
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = float(value)
        if _SERIAL_MIN <= n <= _SERIAL_MAX:
            return (_SHEETS_EPOCH + dt.timedelta(days=int(n))).isoformat()
        return None  # numeric but not a plausible sheet-date serial

    s = normalize_text(value)
    if not s:
        return None

    # A serial that arrived as a string.
    try:
        n = float(s)
        if _SERIAL_MIN <= n <= _SERIAL_MAX:
            return (_SHEETS_EPOCH + dt.timedelta(days=int(n))).isoformat()
    except ValueError:
        pass

    # ISO (optionally with a time component).
    iso_candidate = s.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(iso_candidate).date().isoformat()
    except ValueError:
        pass

    # US M/D/YYYY (with or without a trailing time).
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue

    return None


def plant_key_norm(value: Any) -> str:
    """Normalize a plant key so ``" gto1 "`` and ``"GTO1"`` match."""
    return normalize_text(value).upper()


# --------------------------------------------------------------------------
# Small math helpers.
# --------------------------------------------------------------------------
def pct_diff(v1: Optional[float], v2: Optional[float]) -> Optional[float]:
    """Relative difference of v2 against the v1 baseline, in percent.

    ``(v2 - v1) / v1 * 100``. Returns 0.0 when both are exactly zero (a plant
    that produced nothing on both sides is a match, not a mismatch). Returns
    None when v1 is zero but v2 is not (undefined ratio — caller flags it), or
    when either side is missing.
    """
    if v1 is None or v2 is None:
        return None
    if v1 == 0:
        return 0.0 if v2 == 0 else None
    return (v2 - v1) / v1 * 100.0


def derive_pr(energy_kwh: Optional[float],
              kwp_dc: Optional[float],
              irradiance_kwh_m2: Optional[float]) -> Optional[float]:
    """PR = E / (kwp_dc * H), the same formula v2 uses. Rounded to 4dp.

    Returns None if any input is missing or a denominator is non-positive
    (no divide-by-zero, no negative-irradiance nonsense).
    """
    if energy_kwh is None or kwp_dc is None or irradiance_kwh_m2 is None:
        return None
    if kwp_dc <= 0 or irradiance_kwh_m2 <= 0:
        return None
    return round(energy_kwh / (kwp_dc * irradiance_kwh_m2), 4)


# --------------------------------------------------------------------------
# The reconcile row + classifier.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ReconcileRow:
    plant_key: str
    date_iso: str
    v1_energy_kwh: Optional[float]
    v2_energy_kwh: Optional[float]
    energy_delta_pct: Optional[float]   # (v2 - v1) / v1 * 100
    v1_irr: Optional[float]
    v2_irr: Optional[float]
    v1_kwp: Optional[float]
    v1_pr: Optional[float]              # derived from v1's own numbers
    v2_pr: Optional[float]              # taken from KPI_Daily
    pr_delta_pct: Optional[float]       # (v2_pr - v1_pr) / v1_pr * 100
    bucket: str
    within_tolerance: bool              # energy gate only
    note: str


def classify(v1_energy: Optional[float],
             v2_energy: Optional[float],
             energy_delta_pct: Optional[float],
             v1_pr: Optional[float],
             v2_pr: Optional[float],
             pr_delta_pct: Optional[float],
             tolerance_pct: float,
             v2_partial: bool = False) -> tuple[str, bool, str]:
    """Return ``(bucket, within_energy_tolerance, note)``.

    The GATE is energy only. PR divergence never fails the gate — it's
    expected once v2's corrected config kicks in — but it's reported so a
    real config bug isn't hidden. A day v2 flagged incomplete (``v2_partial``)
    lands in its own bucket and never fails the gate.
    """
    if v1_energy is None and v2_energy is None:
        return BUCKET_MISSING_V2, False, "no data on either side"
    if v1_energy is None:
        return BUCKET_MISSING_V1, False, "v2 has this day, v1 does not"
    if v2_energy is None:
        return BUCKET_MISSING_V2, False, "v1 has this day, v2 does not"

    # v2's day is flagged incomplete (last sample too early -> MAX(EToday)
    # undercounts). Not fairly comparable: report the delta for context but do
    # NOT fail the energy gate on it.
    if v2_partial:
        note = "v2 day incomplete (data_class=partial)"
        if energy_delta_pct is not None:
            note += f"; energy {energy_delta_pct:+.1f}%"
        return BUCKET_PARTIAL_V2, False, note

    # Both energies present.
    if v1_energy == 0 and v2_energy == 0:
        return BUCKET_OK, True, "both zero"
    if energy_delta_pct is None:
        # v1 == 0, v2 != 0 — v2 saw production v1 recorded as zero.
        return BUCKET_ENERGY, False, "v1 energy is 0 but v2 is non-zero"

    if abs(energy_delta_pct) > tolerance_pct:
        return BUCKET_ENERGY, False, (
            f"energy differs {energy_delta_pct:+.1f}% (> {tolerance_pct:g}%)"
        )

    # Energy agrees. Is PR also close, or has config/irradiance diverged?
    if v1_pr is not None and v2_pr is not None and pr_delta_pct is not None \
            and abs(pr_delta_pct) > tolerance_pct:
        return BUCKET_PR, True, (
            f"energy OK ({energy_delta_pct:+.1f}%) but PR {pr_delta_pct:+.1f}% "
            f"— compare v1_kwp/v2 config and irradiance source"
        )
    return BUCKET_OK, True, f"energy OK ({energy_delta_pct:+.1f}%)"


# --------------------------------------------------------------------------
# Table loaders — turn read_table dicts into {(plant, date): value-bundle}.
# --------------------------------------------------------------------------
def _first_present(row: Dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if k in row and normalize_text(row[k]) != "":
            return row[k]
    return None


def index_v2(kpi_rows: List[Dict[str, Any]]) -> Dict[tuple, Dict[str, Any]]:
    """Index KPI_Daily rows by (plant_key, date_iso).

    On duplicate keys, the LAST row wins (upsert should prevent dupes, but be
    defensive). Rows whose date or plant won't normalize are skipped.
    """
    out: Dict[tuple, Dict[str, Any]] = {}
    for r in kpi_rows:
        pk = plant_key_norm(_first_present(r, ["plant_key", "Plant_Key"]))
        d = date_key(_first_present(r, ["date_iso", "date", "Date"]))
        if not pk or not d:
            continue
        out[(pk, d)] = {
            "energy_kwh": safe_float(r.get("energy_kwh")),
            "irr": safe_float(r.get("irradiance_kwh_m2")),
            "pr": safe_float(r.get("pr")),
            "data_class": normalize_text(r.get("data_class")).lower(),
        }
    return out


def index_v1(daily_rows: List[Dict[str, Any]]) -> Dict[tuple, Dict[str, Any]]:
    """Index DailyData rows by (plant_key, date_iso)."""
    out: Dict[tuple, Dict[str, Any]] = {}
    for r in daily_rows:
        pk = plant_key_norm(_first_present(r, ["Plant_Key", "plant_key"]))
        d = date_key(_first_present(r, ["Date", "date"]))
        if not pk or not d:
            continue
        out[(pk, d)] = {
            "energy_kwh": safe_float(_first_present(r, ["Real_kWh", "real_kwh"])),
            "irr": safe_float(_first_present(r, ["Irradiance_kWh_m2", "irradiance_kwh_m2"])),
            "kwp": safe_float(_first_present(r, ["Size_kWp_DC", "size_kwp_dc", "kwp_dc"])),
        }
    return out


# --------------------------------------------------------------------------
# The build step.
# --------------------------------------------------------------------------
def build_reconcile(v1_rows: List[Dict[str, Any]],
                    v2_rows: List[Dict[str, Any]],
                    active_plants: Set[str],
                    tolerance_pct: float = 2.0,
                    include_dates: Optional[Set[str]] = None,
                    exclude_dates: Optional[Set[str]] = None) -> List[ReconcileRow]:
    """Join v1 + v2 daily rows and classify each (plant, date).

    Args:
        v1_rows: DailyData rows (read_table dicts).
        v2_rows: KPI_Daily rows (read_table dicts).
        active_plants: only these plant keys are reconciled (deactivated plants
            aren't in KPI_Daily and shouldn't be compared). Normalized upper.
        tolerance_pct: energy gate threshold, percent.
        include_dates: if given, restrict to these ISO dates.
        exclude_dates: ISO dates to drop (e.g. today's partial day).

    Returns rows sorted by (date, plant).
    """
    active = {plant_key_norm(p) for p in active_plants}
    v1 = index_v1(v1_rows)
    v2 = index_v2(v2_rows)

    keys = set(v1) | set(v2)
    exclude = exclude_dates or set()
    rows: List[ReconcileRow] = []

    for (pk, d) in keys:
        if pk not in active:
            continue
        if d in exclude:
            continue
        if include_dates is not None and d not in include_dates:
            continue

        a = v1.get((pk, d), {})
        b = v2.get((pk, d), {})
        v1_e = a.get("energy_kwh")
        v2_e = b.get("energy_kwh")
        v1_irr = a.get("irr")
        v2_irr = b.get("irr")
        v1_kwp = a.get("kwp")
        v2_pr = b.get("pr")
        v2_partial = b.get("data_class") == DATA_CLASS_PARTIAL
        v1_pr = derive_pr(v1_e, v1_kwp, v1_irr)

        e_delta = pct_diff(v1_e, v2_e)
        pr_delta = pct_diff(v1_pr, v2_pr)

        bucket, within, note = classify(
            v1_e, v2_e, e_delta, v1_pr, v2_pr, pr_delta, tolerance_pct,
            v2_partial=v2_partial,
        )

        rows.append(ReconcileRow(
            plant_key=pk,
            date_iso=d,
            v1_energy_kwh=v1_e,
            v2_energy_kwh=v2_e,
            energy_delta_pct=e_delta,
            v1_irr=v1_irr,
            v2_irr=v2_irr,
            v1_kwp=v1_kwp,
            v1_pr=v1_pr,
            v2_pr=v2_pr,
            pr_delta_pct=pr_delta,
            bucket=bucket,
            within_tolerance=within,
            note=note,
        ))

    rows.sort(key=lambda r: (r.date_iso, r.plant_key))
    return rows


def summarize(rows: List[ReconcileRow]) -> Dict[str, int]:
    """Count rows per bucket. Handy for the CLI summary + exit code."""
    counts: Dict[str, int] = {
        BUCKET_OK: 0, BUCKET_ENERGY: 0, BUCKET_PR: 0, BUCKET_PARTIAL_V2: 0,
        BUCKET_MISSING_V1: 0, BUCKET_MISSING_V2: 0,
    }
    for r in rows:
        counts[r.bucket] = counts.get(r.bucket, 0) + 1
    return counts
