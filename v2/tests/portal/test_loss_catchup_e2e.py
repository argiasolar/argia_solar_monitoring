"""v276 - a lost connection is not lost production, end to end on the real database.

Tomasz, 2026-09-30: "usually it looks that we are losing connections not the
production in many cases. verify again against reconciled data". SAG's
September: on 19, 25 and 28 Sep the vendor day counter froze during a data
outage and the next night's lifetime counter carried the missing energy.

Here Vitalmex (MEX2) gets the same story six days ago: data stops at noon,
the day counter freezes at 30%, the next night the lifetime counter jumps by
the missing 70%. loss_daily must credit it back.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]


def _run_loss(env, d_from, d_to=None):
    args = [sys.executable, str(V2 / "scripts/loss_daily.py"), "--from", d_from] + (["--to", d_to] if d_to else [])
    r = subprocess.run(args, env=dict(env, ARGIA_PG_MIRROR="1", PYTHONPATH=str(V2)), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


@pytest.fixture
def gap_day(fresh_db, psql):
    today = dt.date.fromisoformat(psql("SELECT (now() AT TIME ZONE 'America/Mexico_City')::date::text")[0][0])
    g = today - dt.timedelta(days=6)
    e_g, e_n = (float(psql(f"SELECT energy_kwh FROM daily_production WHERE plant_key='MEX2' AND prod_date=DATE '{d}'")[0][0])
                for d in (g, g + dt.timedelta(days=1)))
    frozen = round(0.3 * e_g, 3)
    psql(f"UPDATE daily_production SET energy_kwh = {frozen} WHERE plant_key='MEX2' AND prod_date=DATE '{g}';")
    psql("DELETE FROM telemetry WHERE plant_key='MEX2' AND (ts_utc AT TIME ZONE 'America/Mexico_City')::date = "
         f"DATE '{g}' AND extract(hour FROM ts_utc AT TIME ZONE 'America/Mexico_City') >= 12;")
    l0 = 1_000_000.0
    psql("INSERT INTO vendor_counter_snapshot (plant_key, vendor, snap_date, daily_kwh, monthly_kwh, lifetime_kwh, note) VALUES "
         f"('MEX2','growatt',DATE '{g - dt.timedelta(days=1)}', {e_g}, NULL, {l0}, ''),"
         f"('MEX2','growatt',DATE '{g}', {frozen}, NULL, {l0 + frozen}, ''),"
         f"('MEX2','growatt',DATE '{g + dt.timedelta(days=1)}', {e_n}, NULL, {l0 + e_g + e_n}, '');")
    yield g, e_g, frozen
    # leave the database as the portal fixture expects it (seed + the nightly loss job)
    for cmd in (["dropdb", "--if-exists", "--force", "argia_mont"], ["createdb", "-T", "argia_seed", "argia_mont"]):
        subprocess.run(cmd, env=fresh_db, capture_output=True, check=True)
    _run_loss(fresh_db, (today - dt.timedelta(days=8)).isoformat())


def test_the_next_night_lifetime_counter_proves_the_energy(fresh_db, psql, gap_day):
    g, e_g, frozen = gap_day
    _run_loss(fresh_db, (g - dt.timedelta(days=2)).isoformat(), (g + dt.timedelta(days=2)).isoformat())
    row = psql("SELECT counter_kwh, catchup_kwh, actual_kwh, expected_kwh, lost_kwh, unavailability_kwh, tolerance_kwh, lost_mxn"
               f" FROM loss_daily WHERE plant_key='MEX2' AND prod_date=DATE '{g}'")[0]
    counter, catchup, actual, expected, lost, unav, tol, mxn = map(float, row)
    assert counter == pytest.approx(frozen, abs=0.1)
    assert catchup > 0.6 * e_g                                  # the missing 70% came back (capped by the shortfall)
    assert actual == pytest.approx(counter + catchup, abs=0.2)
    assert lost < 0.10 * expected                               # before v276: about 70% of the day 'lost'
    assert mxn == pytest.approx(lost * float(psql("SELECT tariff_mxn FROM loss_daily WHERE plant_key='MEX2' "
                                                  f"AND prod_date=DATE '{g}'")[0][0]), abs=0.5)


def test_without_the_evidence_the_same_day_is_a_loss(fresh_db, psql, gap_day):
    """Same gap, but the next night's snapshot is missing: nothing proves the
    energy, so the day stays a loss - the method never invents production."""
    g, e_g, _ = gap_day
    psql(f"DELETE FROM vendor_counter_snapshot WHERE plant_key='MEX2' AND snap_date = DATE '{g + dt.timedelta(days=1)}';")
    _run_loss(fresh_db, (g - dt.timedelta(days=2)).isoformat(), (g + dt.timedelta(days=2)).isoformat())
    catchup, lost, expected = map(float, psql("SELECT catchup_kwh, lost_kwh, expected_kwh FROM loss_daily"
                                              f" WHERE plant_key='MEX2' AND prod_date=DATE '{g}'")[0])
    assert catchup == 0 and lost > 0.5 * expected
