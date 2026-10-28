"""v329 - a frozen irradiance sensor must not take the KPI run down.

26 Oct 2026 on pio06: the SMS ShineMaster (shared by SAG, Vitalmex and SMS)
read a frozen 13 W/m2 all day -> 0.001 kWh/m2 -> Vitalmex PR 7,234 ->
"numeric field overflow" on the fleet upsert -> no KPI for ANY plant, and
the unit still reported success. Here the same day is replayed on the
seeded database: the real kpi_eod writes every plant, the faulty plant
falls back to the cloud model, and nothing absurd reaches the PR column.
"""
from __future__ import annotations

import datetime as dt
import importlib
import logging
import pathlib
import sys

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

SERVER_ENV = {
    "ARGIA_PG_MIRROR": "1",
    "ARGIA_TELEMETRY_SOURCE": "pg", "ARGIA_KPI_SOURCE": "pg", "ARGIA_FINANCE_SOURCE": "pg",
    "ARGIA_INVOICING_SOURCE": "pg", "ARGIA_ALERTS_SOURCE": "pg", "ARGIA_DASHBOARD_SOURCE": "pg",
    "ARGIA_CONFIG_SOURCE": "pg", "ARGIA_KPI_WRITE": "pg",
    "ARGIA_SHEET_TELEMETRY": "0", "ARGIA_SHEET_OUTBOX": "0",
}


def _yesterday(psql):
    return dt.date.fromisoformat(psql("SELECT ((now() AT TIME ZONE 'America/Mexico_City')::date - 1)::text")[0][0])


def _run_kpi(env, day, monkeypatch, caplog):
    for k, v in {**SERVER_ENV, **{k: env[k] for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH")}}.items():
        monkeypatch.setenv(k, v)
    caplog.set_level(logging.INFO)
    mod = importlib.import_module("kpi_eod")
    rc = mod.run(["--date", day.isoformat()])
    return rc, caplog.text


def test_frozen_sensor_day_writes_every_plant_and_no_absurd_pr(fresh_db, psql, monkeypatch, caplog):
    day = _yesterday(psql)
    # the 26 Oct pattern on one plant: its sensor frozen near zero all day
    # (0.1 W/m2 here: on the seed it gives the same order of PR as 26 Oct)
    psql(f"UPDATE telemetry SET irradiance_wm2 = 0.1 WHERE plant_key = 'MEX2'"
         f" AND (ts_utc AT TIME ZONE 'America/Mexico_City')::date = DATE '{day}'"
         f" AND irradiance_wm2 > 0")
    rc, log = _run_kpi(fresh_db, day, monkeypatch, caplog)
    assert rc in (0, 1), log[-3000:]               # 1 = partial day, by design
    assert "numeric field overflow" not in log and "crashed" not in log
    rows = {r[0]: r for r in psql(
        "SELECT plant_key, pr::text, irradiance_source, irradiance_kwh_m2::text FROM daily_production"
        f" WHERE prod_date = DATE '{day}' AND energy_kwh > 0")}
    with_telemetry = {r[0] for r in psql(
        "SELECT DISTINCT plant_key FROM telemetry"
        f" WHERE (ts_utc AT TIME ZONE 'America/Mexico_City')::date = DATE '{day}' AND power_w > 0")}
    assert with_telemetry and with_telemetry <= set(rows), sorted(with_telemetry - set(rows))
    # the faulty plant: the sensor was not believed
    mex2 = rows["MEX2"]
    assert mex2[2] == "cloud_cover_model", mex2
    assert mex2[1] == "" or 0 < float(mex2[1]) <= 1.2, mex2
    assert "MEX2" in log and "sensor fault" in log
    # every other plant keeps its own sensor and a plausible PR
    for pk, pr, src, _irr in rows.values():
        if pk != "MEX2" and pr:
            assert 0 <= float(pr) <= 1.2, (pk, pr)
            assert src.startswith("shinemaster"), (pk, src)


def test_a_crash_is_not_reported_as_success(monkeypatch):
    """argia-kpi treats rc 1 as success (a partial day). An exception must
    exit with something else, or systemd hides it (26 Oct)."""
    mod = importlib.import_module("kpi_eod")

    def boom(argv=None):
        raise RuntimeError("psql failed: ERROR:  numeric field overflow")
    monkeypatch.setattr(mod, "main", boom)
    assert mod.run([]) == mod.CRASH_EXIT == 70
    unit = (V2 / "server/bundle/argia-kpi.service").read_text(encoding="utf-8")
    assert "SuccessExitStatus=1" in unit and "70" not in unit.split("SuccessExitStatus=")[1].splitlines()[0]
