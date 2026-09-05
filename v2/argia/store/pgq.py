"""Thin PostgreSQL access for server-side jobs (pio06 only).

Same execution model as pg_mirror: ``runuser -u postgres -- psql`` with
peer auth — root-only, no password, no network listener. Import-safe
everywhere; only ``psql_rows``/``psql_exec`` require the server.
"""

from __future__ import annotations

import os
import subprocess
from typing import List

DB_ENV = "ARGIA_PG_DB"
DB_DEFAULT = "argia_mont"
_TIMEOUT = 120


def db_name(env=None) -> str:
    """The one place the database name is resolved (v207.2)."""
    env = os.environ if env is None else env
    return str(env.get(DB_ENV, DB_DEFAULT)).strip() or DB_DEFAULT


# kept for callers that read the constant
DB = db_name()


def _run(sql: str, extra: List[str], timeout: int = _TIMEOUT) -> str:
    r = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-d", db_name(),
         "-v", "ON_ERROR_STOP=1", "-q"] + extra,
        input=sql, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr[-800:]}")
    return r.stdout


def psql_csv(sql: str, timeout: int = _TIMEOUT) -> str:
    """SELECT -> CSV text with a header row (what ``psql --csv`` prints).
    v207.2: the single wrapper behind every door's ``_fetch_csv``."""
    return _run("", ["--csv", "-c", sql], timeout=timeout)


def psql_rows(sql: str) -> List[List[str]]:
    """SELECT -> list of rows (tab-split strings; '' for NULL)."""
    out = _run(sql, ["-t", "-A", "-F", "\t"])
    return [ln.split("\t") for ln in out.splitlines() if ln.strip()]


def psql_exec(sql: str, timeout: int = _TIMEOUT) -> None:
    """Execute DML/DDL (stdin); raises on any error."""
    _run(sql, [], timeout=timeout)
