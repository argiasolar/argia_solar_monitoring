"""v207.2 - one psql wrapper. Every door's _fetch_csv delegates to
argia.store.pgq.psql_csv; the Google client libs load lazily."""
import builtins
import pathlib
import re
import subprocess
import sys

import pytest

from argia.store import pgq

V2 = pathlib.Path(__file__).resolve().parents[2]
DOORS = ["argia/core/alerts_pg.py", "argia/core/config_pg.py",
         "argia/finance/invoicing_pg.py", "argia/finance/pg_source.py",
         "argia/kpi/pg_kpi_source.py", "argia/report/dashboard_pg.py",
         "argia/telemetry/pg_source.py", "scripts/archive_month_pg.py"]


def test_only_pgq_and_the_bulk_writers_spawn_psql():
    hits = []
    for p in list((V2 / "argia").rglob("*.py")) + list((V2 / "scripts").glob("*.py")):
        src = p.read_text(encoding="utf-8")
        if '"runuser"' in src or "'runuser'" in src:
            hits.append(str(p.relative_to(V2)).replace("\\", "/"))
    # pgq (the wrapper) and the two bulk upsert writers (stdin, own
    # timeouts) - v207.3 folds those onto pgq too
    assert sorted(hits) == ["argia/store/pg_detail.py", "argia/store/pg_mirror.py",
                            "argia/store/pgq.py"]


@pytest.mark.parametrize("rel", DOORS)
def test_every_door_delegates_to_pgq(rel):
    src = (V2 / rel).read_text(encoding="utf-8")
    body = src[src.index("def _fetch_csv(sql: str) -> str:"):]
    body = body[:body.index("\ndef ", 10)]
    assert "psql_csv(sql" in body and "subprocess" not in body


def test_psql_csv_shape(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd; seen["kw"] = kw
        class R: returncode = 0; stdout = "a,b\n1,2\n"; stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv(pgq.DB_ENV, "argia_test")
    assert pgq.psql_csv("SELECT 1", timeout=7) == "a,b\n1,2\n"
    assert seen["cmd"][:6] == ["runuser", "-u", "postgres", "--", "psql", "-d"]
    assert seen["cmd"][6] == "argia_test" and "--csv" in seen["cmd"]
    assert seen["kw"]["timeout"] == 7


def test_psql_failure_raises(monkeypatch):
    def fake_run(cmd, **kw):
        class R: returncode = 1; stdout = ""; stderr = "FATAL: nope"
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="psql failed"):
        pgq.psql_csv("SELECT 1")


def test_package_imports_without_google_libs(monkeypatch):
    """The whole package must import on a box without googleapiclient."""
    real_import = builtins.__import__

    def blocked(name, *a, **kw):
        if name.startswith("google"):
            raise ModuleNotFoundError(name)
        return real_import(name, *a, **kw)
    for m in [k for k in sys.modules if k.startswith("argia")]:
        monkeypatch.delitem(sys.modules, m)
    monkeypatch.setattr(builtins, "__import__", blocked)
    import argia.core.config  # noqa: F401  (imports argia.core.sheets)
    import argia.core.sheets as S
    assert S.NullSheets().sheet_id == ""
