#!/usr/bin/env python3
"""Create the finance/PM tables (argia.fin.schema.ENSURE_SQL) — v244.

    fin_schema.py            # print what exists / is missing (tables and columns)
    fin_schema.py --apply    # CREATE IF NOT EXISTS + ADD COLUMN IF NOT EXISTS (idempotent, adds only)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # runs from anywhere, PYTHONPATH or not

from argia.fin import schema as S


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    from argia.store.pgq import psql_exec, psql_rows
    have = {r[0] for r in psql_rows("SET statement_timeout='10s'; SELECT table_name FROM information_schema.tables WHERE table_schema='public';")}
    missing = [t for t in S.TABLES if t not in have]
    cols = {(r[0], r[1]) for r in psql_rows("SET statement_timeout='10s'; SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public';")}
    lacking = [f"{t}.{c}" for t, c, _ in S.ADD_COLUMNS if t in have and (t, c) not in cols]
    print(f"fin schema: {len(S.TABLES)} tables designed, {len(S.TABLES) - len(missing)} present, {len(missing)} missing" + (": " + ", ".join(missing) if missing else "")
          + (f"; {len(lacking)} column(s) to add: {', '.join(lacking)}" if lacking else ""))
    if not a.apply:
        return 0 if not missing and not lacking else 2
    psql_exec("SET statement_timeout='60s';\n" + S.ENSURE_SQL + "\n" + S.MIGRATE_SQL)
    have = {r[0] for r in psql_rows("SET statement_timeout='10s'; SELECT table_name FROM information_schema.tables WHERE table_schema='public';")}
    cols = {(r[0], r[1]) for r in psql_rows("SET statement_timeout='10s'; SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public';")}
    still = [t for t in S.TABLES if t not in have]
    short = [f"{t}.{c}" for t, c, _ in S.ADD_COLUMNS if (t, c) not in cols]
    print(f"applied: {len(S.TABLES) - len(still)} present, {len(still)} missing" + (": " + ", ".join(still) if still else "")
          + (f"; columns still missing: {', '.join(short)}" if short else "; every declared column present"))
    return 0 if not still and not short else 1


if __name__ == "__main__":
    sys.exit(main())
