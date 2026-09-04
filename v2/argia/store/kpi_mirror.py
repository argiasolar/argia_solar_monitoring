"""KPI sheet -> daily_production mirror SQL (P1-9). PURE — no I/O.

Retires the GitHub round-trip (kpi_eod -> sheet -> Actions export ->
data branch -> sync_kpi, hours of lag) one hop at a time: the server
reads the sheet it just stamped and upserts PostgreSQL directly,
minutes after the 06:00 MX KPI run.

Protected upsert semantics (the sheet is ANALYTICS; the vendor counter
is the BILLING control):
- a blank sheet cell never overwrites stored data (COALESCE);
- a stored row whose status_note carries vendor-counter provenance
  keeps its energy_kwh, pr, pr_stc AND status_note — a sheet re-sync
  can never re-introduce an undercount or a stale PR. (Found in the
  2026-08-27 due diligence: the original sync_kpi COALESCE could.)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# Substring, not prefix: provenance notes come in two flavors ("energy
# from vendor daily counter (…)" written by the recon self-heal/backfill
# and "energy corrected from vendor daily counter (was …)" echoed back
# from the sheet by kpi_sheet_fix). Both mark a vendor-authoritative row.
VENDOR_NOTE_MARK = "vendor daily counter"
VENDOR_NOTE_PREFIX = VENDOR_NOTE_MARK  # backward-compat alias

# sheet header -> daily_production column (mirrors bundle/sync_kpi.py)
COLMAP = {
    "energy_kwh": "energy_kwh",
    "irradiance_kwh_m2": "irradiance_kwh_m2",
    "pr": "pr",
    "pr_stc": "pr_stc",
    "billable_kwh": "billable_kwh",
    "expected_kwh": "expected_kwh",
    "availability": "availability",
    "cloud_coverage_pct": "cloud_cover_pct",
    "data_class": "data_class",
    "inverters_reporting": "inverters_reporting",
    "status_note": "status_note",
    # v190 (Sheets retirement, phase 2a): the remaining KPI_Daily columns,
    # so daily_production is the COMPLETE record and the readers can
    # leave the sheet. Same protected/frozen semantics apply to all.
    "irradiance_source": "irradiance_source",
    "pr_confidence": "pr_confidence",
    "capacity_factor": "capacity_factor",
    "capacity_factor_confidence": "capacity_factor_confidence",
    "inverters_with_reboot": "inverters_with_reboot",
    "notes": "notes",
    "written_at_utc": "written_at_utc",
    "specific_yield": "specific_yield",
    "soiling_loss_pct": "soiling_loss_pct",
    "production_pct": "production_pct",
    "design_kwh": "design_kwh",
}
NUMERIC = {"energy_kwh", "irradiance_kwh_m2", "pr", "pr_stc",
           "billable_kwh", "expected_kwh", "availability",
           "cloud_cover_pct", "capacity_factor", "specific_yield",
           "soiling_loss_pct", "production_pct", "design_kwh"}
INTEGER = {"inverters_reporting", "inverters_with_reboot"}

# Idempotent: the v190 columns. Run before every mirror (CREATE/ADD IF NOT
# EXISTS is cheap and makes deploy order irrelevant).
ENSURE_SQL = """
ALTER TABLE daily_production
  ADD COLUMN IF NOT EXISTS irradiance_source          text,
  ADD COLUMN IF NOT EXISTS pr_confidence              text,
  ADD COLUMN IF NOT EXISTS capacity_factor            numeric,
  ADD COLUMN IF NOT EXISTS capacity_factor_confidence text,
  ADD COLUMN IF NOT EXISTS inverters_with_reboot      integer,
  ADD COLUMN IF NOT EXISTS notes                      text,
  ADD COLUMN IF NOT EXISTS written_at_utc             text,
  ADD COLUMN IF NOT EXISTS specific_yield             numeric,
  ADD COLUMN IF NOT EXISTS soiling_loss_pct           numeric,
  ADD COLUMN IF NOT EXISTS production_pct             numeric,
  ADD COLUMN IF NOT EXISTS design_kwh                 numeric;
""".strip()

# columns a vendor-corrected row keeps no matter what the sheet says
PROTECTED = ("energy_kwh", "billable_kwh", "pr", "pr_stc", "status_note")


def normalize_rows(records: List[Dict[str, Any]],
                   date_key: Callable[[Any], Optional[str]],
                   min_date: Optional[str] = None,
                   ) -> List[Dict[str, Any]]:
    """Sheet records (read_table dicts) -> clean rows for the upsert.

    Drops rows without a plant/date; ``min_date`` bounds the window so
    one mirror run never rewrites deep history. Values stay Python-
    typed; quoting happens in build_upsert_sql.
    """
    out: List[Dict[str, Any]] = []
    for r in records:
        d = date_key(r.get("date_iso"))
        pk = str(r.get("plant_key") or "").strip().upper()
        if not d or not pk:
            continue
        if min_date and d < min_date:
            continue
        row: Dict[str, Any] = {"prod_date": d, "plant_key": pk}
        for src, dst in COLMAP.items():
            raw = r.get(src)
            s = ("" if raw is None else str(raw)).strip()
            if s == "":
                row[dst] = None
            elif dst in INTEGER:
                try:
                    row[dst] = int(float(s))
                except ValueError:
                    row[dst] = None
            elif dst in NUMERIC:
                try:
                    row[dst] = float(s)
                except ValueError:
                    row[dst] = None
            else:
                row[dst] = s
        out.append(row)
    return out


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def build_upsert_sql(rows: List[Dict[str, Any]]) -> Optional[str]:
    """One INSERT ... ON CONFLICT statement with the protected SET
    clause. None when there is nothing to write."""
    if not rows:
        return None
    cols = ["plant_key", "prod_date"] + sorted(set(COLMAP.values())) + ["source"]
    vendor_guard = (f"daily_production.status_note LIKE "
                    f"'%{VENDOR_NOTE_MARK}%'")
    # A CLOSED month is invoiced history: no import path may modify any
    # column of its rows (2026-09-01 — the sibling sync_kpi.py path
    # un-fixed the freshly closed August 45 minutes after the close).
    frozen = ("""EXISTS (SELECT 1 FROM reconciliation_monthly rm
       WHERE rm.plant_key = daily_production.plant_key
       AND rm.ref_month = date_trunc('month',
           daily_production.prod_date)::date
       AND rm.closed_at IS NOT NULL)""")
    sets = []
    for c in cols:
        if c in ("plant_key", "prod_date", "source"):
            continue
        base = f"COALESCE(EXCLUDED.{c}, daily_production.{c})"
        if c in PROTECTED:
            base = (f"CASE WHEN {vendor_guard}"
                    f" THEN daily_production.{c} ELSE {base} END")
        sets.append(f"{c} = CASE WHEN {frozen}"
                    f" THEN daily_production.{c} ELSE {base} END")
    tuples = []
    for r in rows:
        vals = []
        for c in cols:
            if c == "source":
                vals.append("'v2'")
            elif c == "prod_date":
                vals.append(f"DATE '{r['prod_date']}'")
            else:
                vals.append(_lit(r.get(c)))
        tuples.append("(" + ", ".join(vals) + ")")
    return (f"INSERT INTO daily_production ({', '.join(cols)}) VALUES\n"
            + ",\n".join(tuples)
            + "\nON CONFLICT (plant_key, prod_date) DO UPDATE SET "
            + ", ".join(sets) + ";")
