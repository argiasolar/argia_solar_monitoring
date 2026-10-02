"""v285 - vendor history import end to end on the real database schema.

Tomasz, 2026-10-01: "do the SMS so it matches the total we have currently in
Growatt". The script reads a saved vendor capture (no network here), plans,
writes in one transaction and proves every month equals the vendor's total.
Synthetic numbers on the seed's SMS plant.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import pathlib
import subprocess
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
PK = "MEX3"


def _run(env, *args):
    r = subprocess.run([sys.executable, str(V2 / "scripts/vendor_history_import.py"), *args],
                       env=dict(env, PYTHONPATH=str(V2)), capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


@pytest.fixture
def month_case(fresh_db, psql, tmp_path):
    today = dt.date.fromisoformat(psql("SELECT (now() AT TIME ZONE 'America/Mexico_City')::date::text")[0][0])
    first = (today.replace(day=1) - dt.timedelta(days=150)).replace(day=1)
    n = calendar.monthrange(first.year, first.month)[1]
    last = first.replace(day=n)
    ym = first.strftime("%Y-%m")
    # distinct, clearly measured days (no three alike in a row)
    psql(f"UPDATE daily_production SET energy_kwh = 300 + 37 * (extract(day FROM prod_date)::int % 7)"
         f" WHERE plant_key='{PK}' AND prod_date BETWEEN DATE '{first}' AND DATE '{last}'")
    stored = {r[0]: float(r[1]) for r in psql(
        f"SELECT prod_date::text, energy_kwh FROM daily_production WHERE plant_key='{PK}'"
        f" AND prod_date BETWEEN DATE '{first}' AND DATE '{last}'")}
    assert len(stored) == n, "the seed should hold the whole month"
    days = [stored[(first + dt.timedelta(days=i)).isoformat()] for i in range(n)]
    days[2] += 100.0                       # day 3: the vendor saw more  -> UPDATE
    days[9] = days[10] = days[11] = 50.0   # days 10-12: a gap the vendor spread evenly
    days[19] = 400.0                       # day 20: missing here         -> NEW
    psql(f"DELETE FROM daily_production WHERE plant_key='{PK}' AND prod_date = DATE '{first + dt.timedelta(days=19)}'")
    total = round(sum(days) - 120.0, 1)    # the month total does not contain 120 of the fill
    year = [0.0] * 12
    year[first.month - 1] = total
    cap = {ym: days, f"year_{first.year}": year, "captured_utc": "2026-10-01 00:00:00"}
    path = tmp_path / "cap.json"
    path.write_text(json.dumps(cap))
    outside = psql(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM daily_production t"
                   f" WHERE NOT (plant_key='{PK}' AND prod_date BETWEEN DATE '{first}' AND DATE '{last}')")[0][0]
    return dict(first=first, last=last, n=n, ym=ym, stored=stored, days=days, total=total,
                cap=str(path), outside=outside)


def _args(c, *extra):
    return ["--plant-key", PK.lower(), "--from", c["first"].isoformat(), "--to", c["last"].isoformat(),
            "--capture", c["cap"], *extra]


def _md5(psql):
    return psql("SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM daily_production t")[0][0]


def test_dry_run_plans_and_writes_nothing(fresh_db, psql, month_case):
    before = _md5(psql)
    rc, out = _run(fresh_db, *_args(month_case))
    assert rc == 0, out
    assert "DRY RUN: nothing written" in out
    assert f"new 1 | update 4 | same {month_case['n'] - 5}" in out, out
    assert _md5(psql) == before


def test_apply_matches_the_vendor_month_and_touches_nothing_else(fresh_db, psql, month_case):
    c = month_case
    rc, out = _run(fresh_db, *_args(c, "--apply"))
    assert rc == 0, out
    assert "VERIFY OK" in out, out
    got = float(psql(f"SELECT sum(energy_kwh) FROM daily_production WHERE plant_key='{PK}'"
                     f" AND prod_date BETWEEN DATE '{c['first']}' AND DATE '{c['last']}'")[0][0])
    assert got == pytest.approx(c["total"], abs=0.001)

    def day(i):
        return psql(f"SELECT energy_kwh, source, status_note FROM daily_production WHERE plant_key='{PK}'"
                    f" AND prod_date = DATE '{c['first'] + dt.timedelta(days=i)}'")[0]
    e3, src3, note3 = day(2)
    assert float(e3) == pytest.approx(c["days"][2], abs=0.001) and "vendor daily counter" in note3 and "was " in note3
    e20, src20, note20 = day(19)
    assert float(e20) == 400.0 and src20 == "v2" and "row created by the history import" in note20
    for i in (9, 10, 11):                                   # the fill absorbed the 120 kWh
        assert float(day(i)[0]) == pytest.approx(10.0, abs=0.005)
    e5, _, note5 = day(4)                                   # an untouched day keeps its value and note
    assert float(e5) == pytest.approx(c["stored"][(c["first"] + dt.timedelta(days=4)).isoformat()], abs=0.001)
    assert "history import" not in (note5 or "")
    outside = psql(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM daily_production t"
                   f" WHERE NOT (plant_key='{PK}' AND prod_date BETWEEN DATE '{c['first']}' AND DATE '{c['last']}')")[0][0]
    assert outside == c["outside"]


def test_second_run_is_a_no_op(fresh_db, psql, month_case):
    assert _run(fresh_db, *_args(month_case, "--apply"))[0] == 0
    before = _md5(psql)
    rc, out = _run(fresh_db, *_args(month_case, "--apply"))
    assert rc == 0 and "new 0 | update 0" in out and "VERIFY OK" in out, out
    assert _md5(psql) == before


def test_unknown_plant_and_bad_range_are_refused(fresh_db, month_case):
    assert _run(fresh_db, "--plant-key", "NOPE", "--from", "2025-01-01", "--to", "2025-01-31",
                "--capture", month_case["cap"])[0] == 2
    assert _run(fresh_db, "--plant-key", PK, "--from", "2025-02-01", "--to", "2025-01-31",
                "--capture", month_case["cap"])[0] == 2


def test_days_a_few_wh_off_are_rewritten_so_the_month_is_exact(fresh_db, psql, month_case):
    """v286: SMS September missed Growatt's month by 0.043 kWh because days
    within 0.05 kWh were left alone. Now the month must be exact."""
    c = month_case
    for i in (0, 4, 6):
        d = c["first"] + dt.timedelta(days=i)
        psql(f"UPDATE daily_production SET energy_kwh = energy_kwh + 0.02 WHERE plant_key='{PK}' AND prod_date = DATE '{d}'")
    rc, out = _run(fresh_db, *_args(c, "--apply"))
    assert rc == 0 and "VERIFY OK" in out, out
    got = float(psql(f"SELECT sum(energy_kwh) FROM daily_production WHERE plant_key='{PK}'"
                     f" AND prod_date BETWEEN DATE '{c['first']}' AND DATE '{c['last']}'")[0][0])
    assert got == pytest.approx(c["total"], abs=0.0005)
