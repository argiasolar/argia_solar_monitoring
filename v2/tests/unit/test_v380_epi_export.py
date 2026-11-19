"""v380 - the read-only export: period bounds, query text, refusal of odd keys."""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))
import epi600_export as X      # noqa: E402


def test_local_days_map_to_utc_minus_6():
    assert X.utc_bounds(dt.date(2026, 10, 1), dt.date(2026, 11, 1)) == ("2026-10-01 06:00+00", "2026-11-01 06:00+00")


def test_queries_are_copy_only_and_scoped():
    q = X.queries(dt.date(2026, 10, 1), dt.date(2026, 11, 1), ["MEX1", "GTO1"])
    assert set(q) == {"telemetry.csv", "strings.csv", "detail.csv", "daily.csv"}
    for sql in q.values():
        assert sql.startswith(X.READ_ONLY) and sql[len(X.READ_ONLY):].startswith("SELECT ")
        assert "'GTO1','MEX1'" in sql
        for word in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "COPY"):
            assert word not in sql.upper()
    assert "prod_date >= '2026-10-01' AND prod_date < '2026-11-01'" in q["daily.csv"]
    assert "ts_utc >= '2026-10-01 06:00+00' AND ts_utc < '2026-11-01 06:00+00'" in q["telemetry.csv"]


def test_read_only_session_and_the_single_psql_wrapper(monkeypatch, tmp_path):
    assert "TRANSACTION READ ONLY" in X.READ_ONLY and "statement_timeout" in X.READ_ONLY
    seen = {}
    monkeypatch.setattr(X.pgq, "psql_csv", lambda sql, timeout=0: seen.setdefault("sql", sql) and "a,b\n1,2\n")
    assert X.run_query(X.READ_ONLY + "SELECT 1", str(tmp_path / "x.csv")) == 8
    assert seen["sql"].startswith(X.READ_ONLY)


@pytest.mark.parametrize("bad", [["MEX1'; DROP TABLE x;--"], [], ["NL 1"]])
def test_odd_plant_keys_refused(bad):
    with pytest.raises(ValueError):
        X.queries(dt.date(2026, 10, 1), dt.date(2026, 11, 1), bad)
