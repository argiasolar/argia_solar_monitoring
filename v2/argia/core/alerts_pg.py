"""The Alerts ledger in PostgreSQL (v194, Sheets retirement phase 3c).

The ``Alerts`` tab is the alert engine's state store: alerts_snapshot
(every 30 min) and alerts_daily read the WHOLE ledger, reconcile, and
write the whole block back; report.daily reads it for the open-alerts
section. Rows only ever update in place or append — history never
shrinks — so the PostgreSQL twin is an upsert keyed by ``alert_id``.

Shape: the sheet's 15 columns, verbatim (``alerts_state.ALERTS_HEADER``),
timestamps kept as the ISO-8601 text the engine writes and sorts on
(lexicographic order == chronological for these strings). Records are
served as ``read_table`` dicts so ``alerts_state.records_from_rows``
parses both sources identically.

Selection:  ARGIA_ALERTS_SOURCE = sheet | pg     (v194 default: sheet)
Backfill:   scripts/alerts_backfill_pg.py --apply, then parity CLEAN,
            then the switch — in one step, between two alerts_snapshot
            ticks, because the sheet keeps changing until the flip.
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, List

SOURCE_ENV = "ARGIA_ALERTS_SOURCE"
TABLE = "alert_ledger"

HEADER = [
    "alert_id", "alert_key", "plant_key", "inverter_sn",
    "metric", "severity", "state",
    "opened_utc", "last_seen_utc", "resolved_utc",
    "value", "threshold", "message", "channels_sent",
    "explanation",
]
NUMERIC = {"value", "threshold"}

ENSURE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    alert_id      text PRIMARY KEY,
    alert_key     text NOT NULL,
    plant_key     text NOT NULL DEFAULT '',
    inverter_sn   text NOT NULL DEFAULT '',
    metric        text NOT NULL DEFAULT '',
    severity      text NOT NULL DEFAULT '',
    state         text NOT NULL DEFAULT 'OPEN',
    opened_utc    text NOT NULL DEFAULT '',
    last_seen_utc text NOT NULL DEFAULT '',
    resolved_utc  text NOT NULL DEFAULT '',
    value         numeric,
    threshold     numeric,
    message       text NOT NULL DEFAULT '',
    channels_sent text NOT NULL DEFAULT '',
    explanation   text NOT NULL DEFAULT '',
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS {TABLE}_key_state ON {TABLE} (alert_key, state);
"""

SELECT_SQL = (f"SELECT {', '.join(HEADER)} FROM {TABLE}"
              " ORDER BY opened_utc, alert_id;")


def source(env=None) -> str:
    env = os.environ if env is None else env
    v = str(env.get(SOURCE_ENV, "sheet")).strip().lower()
    return "pg" if v == "pg" else "sheet"


# ---------------------------------------------------------------- pure

def _cell(name: str, raw) -> Any:
    s = ("" if raw is None else str(raw)).strip()
    if s == "" or name not in NUMERIC:
        return s
    try:
        f = float(s)
    except ValueError:
        return s
    return int(f) if f.is_integer() and "." not in s else f


def csv_to_records(text: str) -> List[Dict[str, Any]]:
    """psql --csv -> read_table-style dicts in HEADER order. Pure."""
    out: List[Dict[str, Any]] = []
    for rec in csv.DictReader(io.StringIO(text)):
        if not (rec.get("alert_id") or "").strip():
            continue
        out.append({h: _cell(h, rec.get(h, "")) for h in HEADER})
    return out


def _lit(v: Any, numeric: bool) -> str:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return "NULL" if numeric else "''"
    if numeric:
        return repr(float(v))
    return "'" + str(v).replace("'", "''") + "'"


def build_upsert_sql(rows: List[List[Any]]) -> str:
    """Rows in HEADER order (``alerts_state.record_to_row`` output) ->
    one INSERT ... ON CONFLICT (alert_id) DO UPDATE that sets every
    column: the engine's block write replaces the row, so does this.
    Pure; '' when there is nothing to write."""
    if not rows:
        return ""
    tuples = []
    for r in rows:
        r = list(r) + [""] * (len(HEADER) - len(r))
        tuples.append("(" + ", ".join(
            _lit(r[i], h in NUMERIC) for i, h in enumerate(HEADER)) + ")")
    sets = ", ".join(f"{h} = EXCLUDED.{h}" for h in HEADER if h != "alert_id")
    return (f"INSERT INTO {TABLE} ({', '.join(HEADER)}) VALUES\n"
            + ",\n".join(tuples)
            + f"\nON CONFLICT (alert_id) DO UPDATE SET {sets},"
            " updated_at = now();")


# ---------------------------------------------------------------- I/O

def _fetch_csv(sql: str) -> str:
    import subprocess
    db = os.environ.get("ARGIA_PG_DB", "argia_mont")
    r = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-d", db,
                        "-v", "ON_ERROR_STOP=1", "--csv", "-c", sql],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("psql failed: %s" % r.stderr.strip()[:300])
    return r.stdout


def ensure() -> None:
    from argia.store.pgq import psql_exec
    psql_exec(ENSURE_SQL)


def read_records() -> List[Dict[str, Any]]:
    return csv_to_records(_fetch_csv(SELECT_SQL))


def write_rows(rows: List[List[Any]]) -> int:
    from argia.store.pgq import psql_exec
    sql = build_upsert_sql(rows)
    if sql:
        psql_exec(sql)
    return len(rows)
