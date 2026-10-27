"""v328 - onboard_plant.py end to end on the production schema.

A synthetic spec (customer specs stay on the server) is validated,
dry-run, applied in one transaction, re-applied as a no-op, and the
plant then loads through the same config door every job uses, with its
two Growatt ids intact.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

V2 = pathlib.Path(__file__).resolve().parents[2]
PK = "TST9"


def _spec(tmp_path, **plant_over):
    plant = {"plant_key": PK, "customer": "O'BRIEN TEST (Leon, GTO)", "brand": "GROWATT",
             "site_id": "111111,222222", "kwp_dc": 960.0, "kwp_ac": 750, "lat": 21.16741,
             "lon": -101.67529, "portfolio": "CAPEX", "installation_date": "2026-10-07",
             "expected_factor": "0.75", "pr_baseline": 0.85, "weather_plant_id": "9309575",
             "datalogger_sn": "DYD0E8501G", "datalogger_addr": "1", "module_count": "1500",
             "module_wp": "640", "show_dashboard": False, "show_daily_report": False,
             "show_financial": False, "client_channel": "test"}
    plant.update(plant_over)
    invs = [{"inverter_sn": f"SN{i}", "inverter_label": f"Inverter {i}", "rated_kw": 125,
             "rated_kw_dc": 160.0, "phase": "P1", "date_producing": "2026-10-07"} for i in range(1, 7)]
    spec = {"plant": plant, "inverters": invs,
            "design": {"annual_kwh": 1650000, "shape_from": "GTO1", "years": [2026, 2027], "from_month": 10}}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return str(p)


def _run(env, *args):
    r = subprocess.run([sys.executable, str(V2 / "scripts/onboard_plant.py"), *args],
                       env=dict(env, PYTHONPATH=str(V2)), capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def _md5(psql):
    return psql("SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM ("
                "SELECT plant_key || coalesce(customer,'') AS t FROM plant UNION ALL "
                "SELECT plant_key || inverter_sn FROM inverter UNION ALL "
                "SELECT plant_key || year || '-' || month || coalesce(design_kwh::text,'') FROM contract_monthly) x")[0][0]


def test_dry_run_writes_nothing(fresh_db, psql, tmp_path):
    before = _md5(psql)
    rc, out = _run(fresh_db, "--spec", _spec(tmp_path))
    assert rc == 0, out
    assert "DRY RUN" in out and f"plant {PK}: NEW; inverters new 6 of 6" in out
    assert _md5(psql) == before


def test_apply_writes_plant_inverters_design_and_loads_through_the_config_door(fresh_db, psql, tmp_path):
    rc, out = _run(fresh_db, "--spec", _spec(tmp_path), "--apply")
    assert rc == 0, out
    assert "VERIFY OK: plant rows 1, inverter rows 6" in out
    row = psql(f"SELECT customer, site_id, portfolio, active, show_dashboard, installation_date,"
               f" expected_factor FROM plant WHERE plant_key='{PK}'")[0]
    assert row == ["O'BRIEN TEST (Leon, GTO)", "111111,222222", "CAPEX", "t", "f", "2026-10-07", "0.75"]
    assert psql(f"SELECT count(*), sum(rated_kw) FROM inverter WHERE plant_key='{PK}' AND active")[0] == ["6", "750.000"]
    years = psql(f"SELECT year, count(*), round(sum(design_kwh)) FROM contract_monthly"
                 f" WHERE plant_key='{PK}' GROUP BY 1 ORDER BY 1")
    assert [r[:2] for r in years] == [["2026", "3"], ["2027", "12"]]
    assert abs(float(years[1][2]) - 1650000) <= 1

    # the config door every job uses (PG source) sees the plant, both Growatt ids intact
    code = ("import os; os.environ['ARGIA_CONFIG_SOURCE']='pg';"
            "from argia.core.config import load_portfolio;"
            "from argia.core.normalize import growatt_site_ids;"
            f"p=load_portfolio(None); c=p.plants['{PK}'];"
            "print(growatt_site_ids(c.site_id), c.kwp_dc, c.weather_plant_id, len(p.inverters_for(c.plant_key)))")
    r = subprocess.run([sys.executable, "-c", code], env=dict(fresh_db, PYTHONPATH=str(V2)),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-1500:]
    assert r.stdout.strip() == "['111111', '222222'] 960.0 9309575 6"


def test_second_apply_is_a_no_op_and_reports_differences(fresh_db, psql, tmp_path):
    rc, out = _run(fresh_db, "--spec", _spec(tmp_path), "--apply")
    assert rc == 0, out
    after_first = _md5(psql)
    rc, out = _run(fresh_db, "--spec", _spec(tmp_path, kwp_dc=961.0, module_wp="640.6667"), "--apply")
    assert rc == 0, out
    assert f"{PK} already exists" in out and "kwp_dc: stored '960.000', spec '961.0'" in out
    assert "inverters new 0 of 6" in out
    assert _md5(psql) == after_first
    assert psql(f"SELECT kwp_dc FROM plant WHERE plant_key='{PK}'")[0][0] == "960.000"


def test_invalid_spec_writes_nothing(fresh_db, psql, tmp_path):
    before = _md5(psql)
    rc, out = _run(fresh_db, "--spec", _spec(tmp_path, lat=48.2, lon=16.4), "--apply")
    assert rc == 2 and "SPEC INVALID" in out and "not in Mexico" in out
    assert _md5(psql) == before
