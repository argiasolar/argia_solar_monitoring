"""KPI_Daily writes to PostgreSQL (v193, Sheets retirement phase 2b).

Until now kpi_eod wrote the sheet (upsert_kpi_rows + ten stamp_column
calls) and kpi_pg_mirror copied the sheet into ``daily_production`` at
14:15 CEST. Every reader has been on ``daily_production`` since v190;
this module lets the WRITER go there too, through the same two calls
kpi_eod already makes.

    ARGIA_KPI_WRITE = sheet | both | pg      (v193 default: sheet)

  sheet  the sheet only (the mirror still bridges) — today's behaviour
  both   PostgreSQL first, then the sheet — the shadow period; the
         mirror becomes a no-op check (parity must stay IDENTICAL)
  pg     PostgreSQL only; the sheet is no longer written

Semantics, deliberately those of the mirror (kpi_mirror.build_upsert_sql):
a row upsert or a single-column stamp becomes one INSERT ... ON CONFLICT
in which only the columns provided change, a NULL never overwrites data,
protected columns on vendor-authoritative rows stay, and CLOSED months
are frozen. So kpi_eod re-running over a corrected month can no longer
undo the correction — on the sheet it could, and the mirror had to
filter that out afterwards.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

from argia.store.kpi_mirror import (COLMAP, INTEGER, NUMERIC, build_upsert_sql,
                                    normalize_rows)

LOG = logging.getLogger(__name__)

MODE_ENV = "ARGIA_KPI_WRITE"
MODES = ("sheet", "both", "pg")


def mode(env=None) -> str:
    env = os.environ if env is None else env
    v = str(env.get(MODE_ENV, "pg")).strip().lower()
    return v if v in MODES else "pg"


def writes_pg(env=None) -> bool:
    return mode(env) in ("both", "pg")


def writes_sheet(env=None) -> bool:
    return mode(env) in ("sheet", "both")


# ---------------------------------------------------------------- pure

def rows_from_lists(header: List[str], new_rows: List[List[Any]],
                    date_key) -> List[Dict[str, Any]]:
    """KPI_Daily row lists (perf_to_row output, in ``header`` order) ->
    mirror rows. PURE."""
    return normalize_rows([dict(zip(header, r)) for r in new_rows], date_key)


def rows_from_stamps(col_name: str, stamps: Dict[Tuple[str, str], Any],
                     date_key) -> List[Dict[str, Any]]:
    """{(date_iso, plant_key): value} for ONE sheet column -> partial
    mirror rows (only that column set; everything else NULL = keep).
    Raises on a column daily_production does not carry — a stamp that
    silently went nowhere is exactly the failure this must not have.
    PURE."""
    if col_name not in COLMAP:
        raise KeyError(f"daily_production has no column for KPI_Daily "
                       f"'{col_name}'")
    recs = [{"date_iso": d, "plant_key": pk, col_name: v}
            for (d, pk), v in stamps.items()]
    rows = normalize_rows(recs, date_key)
    pg_col = COLMAP[col_name]
    keep = {"prod_date", "plant_key", pg_col}
    out = [{k: v for k, v in r.items() if k in keep} for r in rows]
    # A text stamp of '' means "clear the note" (status_note on a day
    # that recovered); normalize_rows turned it into NULL = keep. Put
    # the empty string back so the clear reaches PostgreSQL (the vendor
    # guard still protects a provenance note).
    if pg_col not in NUMERIC and pg_col not in INTEGER:
        by_key = {(r["prod_date"], r["plant_key"]): r for r in out}
        for rec in recs:
            d = date_key(rec["date_iso"])
            r = by_key.get((d, str(rec["plant_key"]).strip().upper()))
            if r is not None and rec[col_name] is not None \
                    and str(rec[col_name]).strip() == "":
                r[pg_col] = ""
    return out


def upsert_sql(rows: List[Dict[str, Any]]) -> str:
    """The mirror's statement plus RETURNING, so the caller can count
    inserted vs updated rows (xmax = 0 marks a fresh insert)."""
    sql = build_upsert_sql(rows) or ""
    if not sql:
        return ""
    assert sql.endswith(";")
    return sql[:-1] + "\nRETURNING plant_key, prod_date::text, (xmax = 0) AS inserted;"


# ---------------------------------------------------------------- I/O

def _run(sql: str) -> List[List[str]]:
    from argia.store.pgq import psql_rows
    return psql_rows(sql)


def upsert_rows(header: List[str], new_rows: List[List[Any]], date_key,
                dry_run: bool = False) -> Dict[str, int]:
    """PostgreSQL side of kpi_daily.upsert_kpi_rows. Same stats dict."""
    rows = rows_from_lists(header, new_rows, date_key)
    if not rows:
        return {"inserted": 0, "updated": 0, "unchanged": 0, "failed": 0}
    if dry_run:
        LOG.info("[PG DRY RUN] would upsert %d daily_production row(s): %s",
                 len(rows), sorted((r["prod_date"], r["plant_key"]) for r in rows))
        return {"inserted": 0, "updated": len(rows), "unchanged": 0, "failed": 0}
    out = _run(upsert_sql(rows))
    ins = sum(1 for r in out if len(r) >= 3 and r[2] in ("t", "true", "True"))
    LOG.info("[PG] daily_production upsert: inserted=%d updated=%d",
             ins, len(out) - ins)
    return {"inserted": ins, "updated": len(out) - ins, "unchanged": 0,
            "failed": len(rows) - len(out)}


def existing_keys_sql(rows: List[Dict[str, Any]]) -> str:
    dates = sorted({r["prod_date"] for r in rows})
    lst = ", ".join(f"DATE '{d}'" for d in dates)
    return ("SELECT plant_key, prod_date::text FROM daily_production"
            f" WHERE prod_date IN ({lst});")


def only_existing(rows: List[Dict[str, Any]], existing) -> List[Dict[str, Any]]:
    """Like the sheet's stamp_column: a stamp for a (day, plant) that has
    no row is skipped with a warning, never creates a skeleton row. PURE."""
    have = {(str(r[0]).upper(), str(r[1])) for r in existing if len(r) >= 2}
    out = []
    for r in rows:
        if (r["plant_key"], r["prod_date"]) in have:
            out.append(r)
        else:
            LOG.warning("stamp: no daily_production row for (%s, %s) — skipping",
                        r["prod_date"], r["plant_key"])
    return out


def stamp(col_name: str, stamps: Dict[Tuple[str, str], Any], date_key,
          dry_run: bool = False) -> int:
    """PostgreSQL side of kpi_daily.stamp_column. Returns rows touched."""
    if not stamps:
        return 0
    rows = rows_from_stamps(col_name, stamps, date_key)
    if not rows:
        return 0
    rows = only_existing(rows, _run(existing_keys_sql(rows)))
    if not rows:
        return 0
    if dry_run:
        for r in rows:
            LOG.info("[PG DRY RUN] would stamp %s %s %s=%s", r["plant_key"],
                     r["prod_date"], COLMAP[col_name], r.get(COLMAP[col_name]))
        return len(rows)
    out = _run(upsert_sql(rows))
    LOG.info("[PG] stamped %s on %d row(s)", COLMAP[col_name], len(out))
    return len(out)
