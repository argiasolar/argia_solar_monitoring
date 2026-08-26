"""Thin PostgreSQL access for server-side jobs (pio06 only).

Same execution model as pg_mirror: ``runuser -u postgres -- psql`` with
peer auth — root-only, no password, no network listener. Import-safe
everywhere; only ``psql_rows``/``psql_exec`` require the server.
"""

from __future__ import annotations

import os
import subprocess
from typing import List

DB = os.environ.get("ARGIA_PG_DB", "argia_mont")
_TIMEOUT = 120


def _run(sql: str, extra: List[str]) -> str:
    r = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-d", DB,
         "-v", "ON_ERROR_STOP=1", "-q"] + extra,
        input=sql, capture_output=True, text=True, timeout=_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr[-800:]}")
    return r.stdout


def psql_rows(sql: str) -> List[List[str]]:
    """SELECT -> list of rows (tab-split strings; '' for NULL)."""
    out = _run(sql, ["-t", "-A", "-F", "\t"])
    return [ln.split("\t") for ln in out.splitlines() if ln.strip()]


def psql_exec(sql: str) -> None:
    """Execute DML/DDL; raises on any error."""
    _run(sql, [])
