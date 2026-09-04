"""Run log in PostgreSQL — the `sync_run` table (v188, 2026-09-04).

Replaces the `SyncRuns` sheet tab as the record of what ran. That tab was
write-only (nothing ever read it back) but it was the *only* audit trail
of job executions, so it gets a table rather than just dying.

Same execution model as pg_mirror / pgq: ``runuser -u postgres -- psql``,
peer auth, server-only. Import-safe everywhere; ``record()`` is a no-op
off the server (``ARGIA_PG_MIRROR`` unset) and never raises — a logging
failure must never turn a green run red.

The row shape is the SyncRuns row exactly (8 columns, canonical order),
so both writers — job_log's ``instrument`` and telemetry_5m's
``_finalize_and_log_run`` — hand over what they already build.
"""
from __future__ import annotations

import logging
import os
import socket
from typing import List, Optional, Sequence

LOG = logging.getLogger("argia.store.sync_run")

TABLE = "sync_run"

# (run_id, started_at_utc, finished_at_utc, script, status,
#  plants_processed, rows_written, error)  — the SyncRuns row
COLS = ("run_id", "started_at", "finished_at", "script", "status",
        "processed", "rows_written", "error", "host")

ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS sync_run (
  id           bigserial PRIMARY KEY,
  run_id       text        NOT NULL,
  started_at   timestamptz,
  finished_at  timestamptz,
  script       text        NOT NULL,
  status       text        NOT NULL,
  processed    integer     NOT NULL DEFAULT 0,
  rows_written integer     NOT NULL DEFAULT 0,
  error        text        NOT NULL DEFAULT '',
  host         text        NOT NULL DEFAULT '',
  logged_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sync_run_script_started
  ON sync_run (script, started_at DESC);
""".strip()


def enabled() -> bool:
    """PG run-logging is on wherever the PG mirror is (the server)."""
    return os.environ.get("ARGIA_PG_MIRROR", "") == "1"


def _lit(v) -> str:
    s = "" if v is None else str(v).strip()
    return "'" + s.replace("'", "''") + "'"


def _ts(v) -> str:
    s = "" if v is None else str(v).strip()
    return _lit(s) + "::timestamptz" if s else "NULL"


def _int(v) -> str:
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return "0"


def build_insert_sql(row: Sequence, host: Optional[str] = None) -> Optional[str]:
    """One INSERT for a SyncRuns-shaped row. Pure. None when the row is
    unusable (needs at least run_id, script, status)."""
    if row is None or len(row) < 5:
        return None
    r = list(row) + [""] * (8 - len(row))
    run_id, started, finished, script, status = r[:5]
    if not str(run_id).strip() or not str(script).strip() \
            or not str(status).strip():
        return None
    vals = [
        _lit(run_id), _ts(started), _ts(finished), _lit(script),
        _lit(status), _int(r[5]), _int(r[6]), _lit(r[7][:2000] if r[7] else ""),
        _lit(host if host is not None else socket.gethostname()),
    ]
    return ("INSERT INTO sync_run (" + ",".join(COLS) + ") VALUES ("
            + ",".join(vals) + ");")


def record(row: Sequence, log: Optional[logging.Logger] = None) -> bool:
    """Ensure the table, insert the row. Returns True when written.
    Never raises."""
    lg = log or LOG
    if not enabled():
        return False
    sql = build_insert_sql(row)
    if sql is None:
        lg.warning("sync_run: row not loggable: %r", row)
        return False
    try:
        from argia.store.pgq import psql_exec
        psql_exec(ENSURE_SQL + "\n" + sql)
        return True
    except Exception as e:  # noqa: BLE001 — best effort by contract
        lg.warning("sync_run: could not record run: %s", e)
        return False


def recent(script: Optional[str] = None, limit: int = 20) -> List[List[str]]:
    """Newest runs, for the admin panel's Jobs tab. Server-only."""
    from argia.store.pgq import psql_rows
    where = f" WHERE script = {_lit(script)}" if script else ""
    return psql_rows(
        "SELECT started_at, finished_at, script, status, processed, "
        f"rows_written, left(error, 120) FROM sync_run{where} "
        f"ORDER BY started_at DESC NULLS LAST LIMIT {int(limit)};")
