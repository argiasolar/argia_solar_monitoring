#!/usr/bin/env python3
"""Create the finance/PM tables (argia.fin.schema.ENSURE_SQL) — v244.

    fin_schema.py            # print what exists / is missing
    fin_schema.py --apply    # CREATE IF NOT EXISTS (idempotent, adds only)
"""
from __future__ import annotations

import argparse
import sys

from argia.fin import schema as S


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    from argia.store.pgq import psql_exec, psql_rows
    have = {r[0] for r in psql_rows("SET statement_timeout='10s'; SELECT table_name FROM information_schema.tables WHERE table_schema='public';")}
    missing = [t for t in S.TABLES if t not in have]
    print(f"fin schema: {len(S.TABLES)} tables designed, {len(S.TABLES) - len(missing)} present, {len(missing)} missing" + (": " + ", ".join(missing) if missing else ""))
    if not a.apply:
        return 0 if not missing else 2
    psql_exec("SET statement_timeout='60s';\n" + S.ENSURE_SQL)
    have = {r[0] for r in psql_rows("SET statement_timeout='10s'; SELECT table_name FROM information_schema.tables WHERE table_schema='public';")}
    still = [t for t in S.TABLES if t not in have]
    print(f"applied: {len(S.TABLES) - len(still)} present, {len(still)} missing" + (": " + ", ".join(still) if still else ""))
    return 0 if not still else 1


if __name__ == "__main__":
    sys.exit(main())
