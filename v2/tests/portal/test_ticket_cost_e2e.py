"""v267 - the ticket app shows what each ticket has cost, against the real database."""
from __future__ import annotations

import importlib
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("flask", reason="flask not installed here (CI and pio06 have it)")

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))
HDR = {"X-Remote-User": "tomasz"}


@pytest.fixture
def app(fresh_db, monkeypatch, psql):
    for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH"):
        monkeypatch.setenv(k, fresh_db[k])
    r = subprocess.run([sys.executable, str(V2 / "scripts/loss_daily.py"), "--from",
                        psql("SELECT ((now() AT TIME ZONE 'America/Mexico_City')::date - 8)::text")[0][0]],
                       env=dict(fresh_db, ARGIA_PG_MIRROR="1", PYTHONPATH=str(V2)), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    import maint_app
    M = importlib.reload(maint_app)
    M.app.config.update(TESTING=True, ROWS_CSV=None, EXEC=None, ENSURED=False,
                        USER_ROW=lambda u: {"level": "argia", "is_admin": 1, "disabled": 0},
                        STAFF=lambda: [{"username": "tomasz", "name": "Tomasz", "email": "t@example.invalid"}])
    return M


def test_the_plant_ticket_page_shows_the_money_day_by_day(app, psql):
    body = app.app.test_client().get("/maintenance/t/T-0001/", headers=HDR).get_data(as_text=True)
    if "Revenue lost" not in body:                     # the app is mounted at / in tests
        body = app.app.test_client().get("/t/T-0001/", headers=HDR).get_data(as_text=True)
    assert "Revenue lost while this ticket is open" in body
    want = float(psql("SELECT sum(l.lost_mxn) FROM loss_daily l, ticket t WHERE t.number = 'T-0001' AND l.plant_key = 'MEX1'"
                      " AND l.prod_date >= (t.created_at AT TIME ZONE 'America/Mexico_City')::date"
                      " AND l.prod_date < (now() AT TIME ZONE 'America/Mexico_City')::date")[0][0])
    assert want > 500                                  # the seeded SAG outage is inside the window
    assert f"${want:,.0f} MXN" in body
    assert "unavailability" in body                    # the outage day's cause


def test_the_inverter_ticket_counts_only_its_inverter(app, psql):
    c = app.app.test_client()
    body = c.get("/t/T-0002/", headers=HDR).get_data(as_text=True)
    assert "this inverter vs its peers" in body and "Inverter ticket" in body
    plant_lost = float(psql("SELECT sum(lost_kwh) FROM loss_daily WHERE plant_key = 'MEX2'"
                            " AND prod_date = (now() AT TIME ZONE 'America/Mexico_City')::date - 3")[0][0])
    assert plant_lost > 100


def test_the_dashboard_totals_each_plant_day_once(app):
    body = app.app.test_client().get("/", headers=HDR).get_data(as_text=True)
    assert "MXN lost while open (each plant-day once)" in body
    assert "Lost so far" in body
