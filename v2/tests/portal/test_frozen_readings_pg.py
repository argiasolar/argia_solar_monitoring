"""v302 - frozen logger readings end to end, against the throwaway PostgreSQL.

Seeded fleet + one plant (MEX1) whose logger "went offline" 45 minutes ago:
its last real reading (40 kW per inverter) comes back unchanged every 5
minutes until now - exactly what FusionSolar did for SAG on 2 Oct 2026.
Before v302 every live page read that as 40 kW of live production."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import pathlib
import runpy
import subprocess
import sys

import pytest

from argia.telemetry import fresh as F

from .conftest import reset_db

V2 = pathlib.Path(__file__).resolve().parents[2]
FROZEN_SQL = """
DELETE FROM telemetry WHERE plant_key = 'MEX1' AND ts_utc > now() - interval '50 minutes';
INSERT INTO telemetry (ts_utc, plant_key, inverter_sn, status, power_w, etoday_kwh, vendor)
SELECT now() - make_interval(mins => m), 'MEX1', i.sn, 1, 40000, 300.5, 'HUAWEI'
  FROM (SELECT DISTINCT inverter_sn AS sn FROM telemetry WHERE plant_key = 'MEX1') i, generate_series(0, 45, 5) m;
"""


def _psql(env, sql):
    r = subprocess.run(["psql", "-d", "argia_mont", "-X", "-t", "-A", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return [ln.split("\t") for ln in r.stdout.splitlines() if ln.strip()]


@pytest.fixture(scope="module")
def frozen(pg_env):
    mx_now = dt.datetime.now(__import__("zoneinfo").ZoneInfo("America/Mexico_City"))
    if mx_now.hour == 0 and mx_now.minute < 50:
        pytest.skip("the frozen run would straddle midnight in Mexico City")
    reset_db(pg_env)
    _psql(pg_env, FROZEN_SQL)
    yield pg_env
    reset_db(pg_env)


def test_sql_and_python_flag_the_same_rows(frozen):
    rows = _psql(frozen, "SELECT inverter_sn, ts_utc, power_w, etoday_kwh FROM telemetry WHERE plant_key IN ('MEX1', 'GTO1') "
                         "AND power_w IS NOT NULL AND ts_utc > now() - interval '1 day' ORDER BY 1, 2;")
    by_inv = {}
    for sn, ts, p, e in rows:
        by_inv.setdefault(sn, []).append((dt.datetime.fromisoformat(ts.replace(" ", "T")), float(p), float(e) if e else None))
    want = set()
    for sn, rs in by_inv.items():
        for r, flag in zip(rs, F.repeats(rs)):
            if flag:
                want.add((sn, r[0]))
    got = {(sn, dt.datetime.fromisoformat(ts.replace(" ", "T"))) for sn, ts in _psql(
        frozen, f"WITH {F.repeat_cte(chr(39) + '2000-01-01' + chr(39))} SELECT inverter_sn, ts_utc FROM rep "
                "WHERE plant_key IN ('MEX1', 'GTO1') AND ts_utc > now() - interval '1 day';")}
    assert got == want
    mex1_invs = {sn for sn in by_inv if sn.startswith("MEX1")}
    assert len({sn for sn, _ in got}) == len(mex1_invs) and len(got) == 9 * len(mex1_invs)   # 9 repeats each, GTO1 none


@pytest.fixture(scope="module")
def portal(frozen, tmp_path_factory):
    """portal_gen for real over the frozen database; returns monitoring_gen's module state."""
    out = tmp_path_factory.mktemp("www_frozen")
    saved_env, saved_argv, saved_path = dict(os.environ), list(sys.argv), list(sys.path)
    try:
        os.environ.update({k: frozen[k] for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH")})
        sys.argv = [str(V2 / "server/bundle/portal_gen.py"), str(out)]
        for m in ("portal_gen", "report_gen", "monitoring_gen"):
            sys.modules.pop(m, None)
        with contextlib.redirect_stdout(io.StringIO()):
            g = runpy.run_path(str(V2 / "server/bundle/portal_gen.py"), run_name="__main__")
        mg = sys.modules["monitoring_gen"]
        yield mg, g, out
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        sys.argv, sys.path[:] = saved_argv, saved_path


def test_portal_inverters_age_from_the_last_real_reading(portal):
    mg, _, _ = portal
    invs = mg.LATEST.get(mg.TODAY, {}).get("MEX1", [])
    assert invs, "MEX1 has inverters today"
    assert all(i["age_min"] > mg.STALE_MIN for i in invs)          # 45 min old: the repeats do not refresh it
    assert all(i["power_w"] == 40000 for i in invs)                # the last REAL value, not shown as live


def test_portal_map_shows_no_live_power_for_the_frozen_plant(portal):
    mg, _, _ = portal
    row = {r["key"]: r for r in mg.portfolio_rows()}["MEX1"]
    assert row["live_kw"] == 0
    assert row["status"] in ("stale", "night")                      # never 'live'


def test_phone_app_marks_the_inverters_stale(portal):
    _, g, _ = portal
    states = {i["state"] for i in g["app_inverters"]("MEX1") if i["state"] != "silent"}
    assert states == {"stale"}
    assert all(i["power_kw"] is None for i in g["app_inverters"]("MEX1"))


def test_cpa_site_shows_no_live_power_for_the_frozen_plant(frozen, monkeypatch):
    for k, v in frozen.items():
        monkeypatch.setenv(k, v)
    sys.path.insert(0, str(V2 / "server" / "bundle"))
    import importlib
    import cpa_gen
    g = importlib.reload(cpa_gen)
    cfg = {"partner": "CPA", "sites": [{"key": "MEX1", "name": "Northwind Foods"}, {"key": "GTO1", "name": "Altamira Labs"}]}
    sites, daily, live = g.fetch(cfg, g.mx_now())
    assert live["MEX1"].kw == 0 and live["MEX1"].age_min > 30                # was 80 kW, 0 min before v302


def test_daily_perf_mail_sees_the_frozen_plant_as_old_data(frozen, monkeypatch):
    for k, v in frozen.items():
        monkeypatch.setenv(k, v)
    sys.path.insert(0, str(V2 / "scripts"))
    import importlib
    import daily_perf_mail
    m = importlib.reload(daily_perf_mail)
    today = m.gather_today()
    assert today["MEX1"][1] > 30                                     # minutes since the last REAL sample
