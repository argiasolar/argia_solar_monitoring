"""v296 - ARGIA for CPA: cpa_gen's real queries against the throwaway
PostgreSQL (tests/portal/conftest.py), then the whole site built from them.
Synthetic names (tests/fixtures/cpa/cpa.json) over the seeded fleet."""
from __future__ import annotations

import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
CFG = V2 / "tests" / "fixtures" / "cpa" / "cpa.json"
sys.path.insert(0, str(V2 / "server" / "bundle"))
sys.path.insert(0, str(V2))


def test_fetch_and_build_against_the_seeded_database(pg_env, monkeypatch, tmp_path):
    for k, v in pg_env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ARGIA_CPA_DIR", str(tmp_path / "cpa"))
    monkeypatch.setenv("ARGIA_PORTAL_ROOT", str(tmp_path / "portal"))
    import importlib
    import cpa_gen
    g = importlib.reload(cpa_gen)
    g.RENDER_PDF = lambda *a: False
    cfg = g.load_config(str(CFG))
    now = g.mx_now()
    sites, daily, live = g.fetch(cfg, now)
    assert [s.name for s in sites] == ["Northwind Foods", "Altamira Labs", "Sierra Plastics"]
    assert all(s.kwp > 0 and s.slug for s in sites)
    assert len(daily["MEX1"]) > 300 and all(d < now.date() for d, _ in daily["MEX1"])
    assert live["MEX3"].age_min is None and live["MEX3"].today_kwh == 0          # dark today in the seed
    assert live["GTO1"].age_min is not None and live["GTO1"].curve
    ctx = g.build(cfg, sites, daily, live, now, str(tmp_path / "stage"))
    assert g.check(str(tmp_path / "stage"), ctx) == []
    assert ctx.life["_all"].kwh > 0


def test_fetch_refuses_a_plant_that_is_not_in_the_database(pg_env, monkeypatch, tmp_path):
    for k, v in pg_env.items():
        monkeypatch.setenv(k, v)
    import importlib
    import cpa_gen
    g = importlib.reload(cpa_gen)
    with pytest.raises(ValueError):
        g.fetch({"sites": [{"key": "NOPE9", "name": "Ghost"}]}, g.mx_now())
