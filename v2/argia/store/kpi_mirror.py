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
}
NUMERIC = {"energy_kwh", "irradiance_kwh_m2", "pr", "pr_stc",
           "billable_kwh", "expected_kwh", "availability",
           "cloud_cover_pct"}
INTEGER = {"inverters_reporting"}

# columns a vendor-corrected row keeps no matter what the sheet says
PROTECTED = ("energy_kwh", "pr", "pr_stc", "status_note")


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
    sets = []
    for c in cols:
        if c in ("plant_key", "prod_date", "source"):
            continue
        base = f"COALESCE(EXCLUDED.{c}, daily_production.{c})"
        if c in PROTECTED:
            sets.append(f"{c} = CASE WHEN {vendor_guard}"
                        f" THEN daily_production.{c} ELSE {base} END")
        else:
            sets.append(f"{c} = {base}")
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
