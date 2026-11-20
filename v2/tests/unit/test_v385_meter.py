"""v385 - grid meters: vendor parsing, sign convention, day energy, consumption and solar share.
Synthetic data only (an invented site with a 30 kW base load and a 100 kWp roof)."""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

import pytest

from argia.telemetry import meter as M
from scripts import meter_collect as MC

MX = ZoneInfo("America/Mexico_City")
UTC = dt.timezone.utc
DAY = "2026-03-21"


def load_w(t):           # site consumption, W
    return 30000.0


def pv_w(t):             # solar, W: a clear-sky bell 07:00-19:00 peaking at 80 kW at 13:00
    h = t.hour + t.minute / 60
    return 80000.0 * math.sin(math.pi * (h - 7) / 12) if 7 < h < 19 else 0.0


def steps(day=DAY):
    t0 = dt.datetime.fromisoformat(day).replace(tzinfo=MX)
    return [t0 + dt.timedelta(minutes=5 * k) for k in range(288)]


def huawei_payload(day=DAY, imp0=1000.0, exp0=500.0, dev=42):
    rows, imp, exp = [], imp0, exp0
    for t in steps(day):
        net = load_w(t) - pv_w(t)                    # + import
        rows.append({"devId": dev, "collectTime": int(t.timestamp() * 1000),
                     "dataItemMap": {"active_power": -net, "reverse_active_cap": round(imp, 2),
                                     "active_cap": round(exp, 2)}})
        imp += max(net, 0) * 5 / 60 / 1000
        exp += max(-net, 0) * 5 / 60 / 1000
    return {"success": True, "data": rows}, imp - imp0, exp - exp0


def growatt_rows(day=DAY):
    rows, imp, exp = [], 0.0, 0.0
    for t in steps(day):
        net = load_w(t) - pv_w(t)
        third = net / 3
        rows.append({"calendar": {"year": t.year, "month": t.month - 1, "dayOfMonth": t.day,
                                  "hourOfDay": t.hour, "minute": t.minute, "second": 49},
                     "activePower": max(net, 0), "activePowerL1": third, "activePowerL2": third,
                     "activePowerL3": third, "positiveActiveTodayEnergy": int(imp),
                     "reverseActiveTodayEnergy": int(exp), "activeEnergy": 360000 + imp,
                     "reverseActiveEnergy": 12345678})
        imp += max(net, 0) * 5 / 60 / 1000
        exp += max(-net, 0) * 5 / 60 / 1000
    return rows[::-1], imp, exp                      # Growatt answers newest first


def test_huawei_sign_is_flipped_to_import_positive():
    pl, _, _ = huawei_payload()
    s = M.parse_huawei_five_minutes("DEMO", pl)
    assert len(s) == 288 and s[0].meter_id == "42"
    night, noon = s[0], s[156]                       # 13:00, the peak
    assert night.grid_w == pytest.approx(30000) and noon.grid_w == pytest.approx(-50000)   # 80 kW solar, 30 kW load
    assert night.ts_utc == dt.datetime(2026, 3, 21, 6, 0, tzinfo=UTC)                      # MX midnight = 06:00 UTC


def test_growatt_month_is_zero_based_and_phases_are_summed():
    rows, _, _ = growatt_rows()
    s = M.parse_growatt_meter_history("DEMO", "LOG1:36", rows, MX)
    first = min(s, key=lambda x: x.ts_utc)
    assert first.ts_utc.astimezone(MX) == dt.datetime(2026, 3, 21, 0, 0, 49, tzinfo=MX)     # month 2 = March
    noon = [x for x in s if x.ts_utc.astimezone(MX).hour == 13 and x.ts_utc.astimezone(MX).minute == 0][0]
    assert noon.grid_w == pytest.approx(-50000)                       # the clamped activePower (0) is not used
    assert noon.exp_day_kwh is not None and noon.exp_total_kwh is None  # reverseActiveEnergy never read


def test_growatt_time_rejects_junk():
    assert M.growatt_time({}, MX) is None
    assert M.growatt_time({"year": 2026, "month": 12, "dayOfMonth": 1, "hourOfDay": 0, "minute": 0}, MX) is None


def test_day_energy_huawei_lifetime_counters_match_the_truth():
    pl, imp, exp = huawei_payload()
    e = M.day_energy(M.parse_huawei_five_minutes("DEMO", pl), DAY, MX)
    assert e["basis"] == "lifetime-counter" and e["n"] == 288
    # the counters of the first and last sample: the last 5 minutes are missing (night import, 2.5 kWh)
    assert e["imp"] == pytest.approx(imp - 2.5, abs=0.02) and e["exp"] == pytest.approx(exp, abs=0.02)
    assert e["imp_power"] == pytest.approx(e["imp"], rel=1e-3) and e["exp_power"] == pytest.approx(e["exp"], rel=1e-3)


def test_day_energy_huawei_uses_the_previous_day_last_reading():
    a, _, _ = huawei_payload("2026-03-20")
    last = a["data"][-1]["dataItemMap"]
    b, imp, exp = huawei_payload(DAY, imp0=last["reverse_active_cap"] + 2.5, exp0=last["active_cap"])
    s = M.parse_huawei_five_minutes("DEMO", a) + M.parse_huawei_five_minutes("DEMO", b)
    e = M.day_energy(s, DAY, MX)
    assert e["imp"] == pytest.approx(imp, abs=0.02)                   # the 23:55-00:00 import is now counted


def test_day_energy_counter_reset_falls_back_to_power():
    pl, _, _ = huawei_payload()
    pl["data"][-1]["dataItemMap"]["reverse_active_cap"] = 3.0          # a new meter
    e = M.day_energy(M.parse_huawei_five_minutes("DEMO", pl), DAY, MX)
    assert e["basis"] == "power"


def test_day_energy_growatt_day_registers():
    rows, imp, exp = growatt_rows()
    e = M.day_energy(M.parse_growatt_meter_history("DEMO", "L:36", rows, MX), DAY, MX)
    assert e["basis"] == "day-register"
    assert e["imp"] == int(imp - 2.5) and e["exp"] == int(exp)        # integer kWh registers
    assert e["imp_power"] == pytest.approx(imp - 2.5, rel=1e-3)


def test_integrate_skips_long_gaps_and_splits_hours():
    pl, _, _ = huawei_payload()
    s = M.parse_huawei_five_minutes("DEMO", pl)
    gap = [x for k, x in enumerate(s) if not 24 <= k < 48]             # 02:00-04:00 missing
    assert M.integrate(gap, MX)[DAY][0] == pytest.approx(M.integrate(s, MX)[DAY][0] - 30 * 2 - 2.5, abs=0.05)
    h = M.integrate(s, MX, by_hour=True)
    assert h[(DAY, 3)] == pytest.approx([30.0, 0.0]) and h[(DAY, 13)][1] > 0 and h[(DAY, 13)][0] == 0


def test_balance_consumption_and_solar_share():
    b = M.balance(pv_kwh=500.0, imp_kwh=400.0, exp_kwh=100.0)
    assert b["cons"] == 800.0 and b["used"] == 400.0 and b["share"] == 0.5 and b["ok"]
    assert M.balance(None, 1, 1) is None


def test_balance_flags_more_export_than_solar():
    b = M.balance(pv_kwh=100.0, imp_kwh=400.0, exp_kwh=150.0)          # impossible behind one meter
    assert not b["ok"] and b["share"] is None
    assert M.balance(100.0, 400.0, 101.5)["ok"]                        # within the 2 kWh tolerance


def test_synthetic_site_closes_the_energy_balance():
    """The meter + the inverters must give back the invented load: 30 kW x 24 h = 720 kWh."""
    pl, _, _ = huawei_payload()
    s = M.parse_huawei_five_minutes("DEMO", pl)
    e = M.integrate(s, MX)[DAY]
    pv = sum(pv_w(t) for t in steps()) * 5 / 60 / 1000
    b = M.balance(pv, e[0], e[1])
    assert b["cons"] == pytest.approx(720.0 - 2.5, abs=0.1)            # the last 5 minutes are not integrated
    assert 0 < b["share"] < 1


def test_period_share_is_a_ratio_of_sums():
    a, b = M.balance(100, 900, 0), M.balance(900, 100, 0)            # 10 % and 90 %
    c = M.balance(100, 400, 150)                                      # bad day, left out
    assert M.period_share([a, b, c, None]) == pytest.approx(1000 / 2000)


def test_short_days():
    assert M.short_days({"2026-03-20": 288, "2026-03-19": 100}, ["2026-03-20", "2026-03-19", "2026-03-18"]) == \
        ["2026-03-19", "2026-03-18"]


def test_upsert_sql():
    pl, _, _ = huawei_payload()
    s = M.parse_huawei_five_minutes("O'K", pl)
    sql = M.build_upsert_sql(s + s[:3])
    assert sql.count("\n(") == 288 and "'O''K'" in sql and "ON CONFLICT (plant_key, meter_id, ts_utc)" in sql
    assert "'2026-03-21T06:00:00Z'" in sql
    assert M.build_upsert_sql([]) is None
    assert M.store([], dry_run=True) == 0 and M.store(s, dry_run=True) == 288


def test_collector_vendor_answers():
    ans = {"obj": {"datas": [{"datalogSn": "LOG1", "addr": 36, "alias": "x"}, {"datalogSn": "LOG1", "addr": 36}]}}
    assert MC.growatt_meters(ans) == [{"datalog_sn": "LOG1", "addr": "36"}]
    assert MC.growatt_json({"response": {"_raw_text": '{"a": 1}'}}) == {"a": 1}
    assert MC.growatt_json({"response": {"_raw_text": "<html>"}}) == {}
    assert MC.huawei_meters({"data": [{"id": 5, "devTypeId": 17}, {"id": 6, "devTypeId": 1}]}) == ["5"]
    with pytest.raises(MC.RateLimited):
        MC.huawei_check({"success": False, "failCode": 407})
    with pytest.raises(RuntimeError):
        MC.huawei_check({"success": False, "failCode": 20009})


def test_collector_days_to_fetch():
    today = dt.date(2026, 3, 21)
    full = {(today - dt.timedelta(days=k)).isoformat(): 288 for k in range(1, 8)}
    assert MC.days_to_fetch(today, full) == ["2026-03-21"]
    assert MC.days_to_fetch(today, full, hour=0) == ["2026-03-21", "2026-03-20"]
    assert MC.days_to_fetch(today, {}) == ["2026-03-21", "2026-03-20", "2026-03-19"]      # two heals per run
    assert len(MC.days_to_fetch(today, {}, backfill=30)) == 31


# ------------------------------------------------------------------ the card and its wiring

import pathlib  # noqa: E402

from argia.report import consumption_html as CH  # noqa: E402

V2 = pathlib.Path(__file__).resolve().parents[2]
EM = chr(0x2014)


def _days(n=30, end="2026-03-30"):
    e = dt.date.fromisoformat(end)
    out = []
    for k in range(n - 1, -1, -1):
        d = (e - dt.timedelta(days=k)).isoformat()
        out.append((d, M.balance(500.0, 400.0, 100.0), "day-register"))
    return out


def test_card_numbers():
    days = _days()
    days[-2] = (days[-2][0], M.balance(100.0, 400.0, 150.0), "power")      # a bad day: greyed, left out
    hours = {h: {"used": 10.0, "imp": 20.0, "exp": 0.0} for h in range(24)}
    h = CH.card("2026-03-30", hours, days, live=False)
    assert 'class="card mtcard"' in h and "800 kWh" in h and "50%" in h
    assert "Solar share, 7 days" in h and "Cobertura solar" in h and EM not in h
    assert 'style="opacity:.45"' in h                                         # the bad day's table row
    assert h.count("<tr") == 31                                               # header + 30 days


def test_card_flags_an_impossible_day_and_hides_its_share():
    days = _days()
    days[-1] = (days[-1][0], M.balance(100.0, 400.0, 150.0), "power")
    h = CH.card("2026-03-30", {}, days, live=True)
    assert "export is larger than its solar" in h and "so far" in h


def test_card_empty_without_meter_data():
    assert CH.card("2026-03-30", {}, [("2026-03-30", None, "")], live=True) == ""


def test_generator_and_demo_wiring():
    src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    assert src.count("{meter_card(pk, d)}") == 1 and "def meter_card(pk, d)" in src
    assert "FROM meter_sample" in src and "MTR = CONS = None" in src         # no table = no card, no crash
    demo = (V2 / "server" / "bundle" / "demo_gen.py").read_text(encoding="utf-8")
    assert "MG.METER_DAY.clear()" in demo and "'class=\"card mtcard\"'" in demo


def test_units_and_schema():
    b = V2 / "server" / "bundle"
    assert "run_job.sh meter meter_collect.py" in (b / "argia-meter.service").read_text()
    assert "OnCalendar=*-*-* *:02/15:00 America/Mexico_City" in (b / "argia-meter.timer").read_text()
    fx = (V2 / "tests" / "fixtures" / "pg" / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE public.meter_sample (" in fx and "meter_sample_pkey PRIMARY KEY (plant_key, meter_id, ts_utc)" in fx
    for col in ("grid_w", "imp_total_kwh", "exp_total_kwh", "imp_day_kwh", "exp_day_kwh"):
        assert col in M.ENSURE_SQL


def test_collector_waits_for_telemetry(monkeypatch):
    monkeypatch.setattr(MC.time, "sleep", lambda s: None)
    calls = iter([True, True, False])
    assert MC.wait_for_telemetry(60, lambda: next(calls)) is True
    assert MC.wait_for_telemetry(10, lambda: True) is False


def test_huawei_session_rules():
    """Huawei keeps one session per account (found in the dry run: our session was ended by the
    telemetry run's login). Calls only 1:30-4:30 into each 5-minute slot; 305 = stop, never re-login."""
    at = lambda m, s: dt.datetime(2026, 3, 21, 10, m, s, tzinfo=MX)
    assert MC.slot_ok(at(2, 0)) and MC.slot_ok(at(17, 30)) and MC.slot_ok(at(4, 30))
    assert not MC.slot_ok(at(0, 30)) and not MC.slot_ok(at(4, 31)) and not MC.slot_ok(at(5, 59))
    assert not MC.may_call(at(2, 0), active=lambda: True) and MC.may_call(at(2, 0), active=lambda: False)
    with pytest.raises(MC.RateLimited):
        MC.huawei_check({"success": False, "failCode": 305, "message": "USER_MUST_RELOGIN"})
    t = (V2 / "server" / "bundle" / "argia-meter.timer").read_text()
    assert "AccuracySec=1s" in t and ":02/15:00" in t                  # starts 2:00 into a slot


def test_gate(monkeypatch):
    monkeypatch.setattr(MC.time, "sleep", lambda s: None)
    seq = iter([False, False, True])
    monkeypatch.setattr(MC, "may_call", lambda: next(seq))
    assert MC.gate(patient=True) is True
    monkeypatch.setattr(MC, "may_call", lambda: False)
    assert MC.gate(patient=False) is False and MC.gate(patient=True, max_wait_s=30) is False


def test_one_huawei_login_per_run_and_never_a_retry(monkeypatch):
    import argia.vendors.huawei as HW
    logins = []

    class Fake:
        def __init__(self, username, password):
            pass

        def login(self):
            logins.append(1)
            raise HW.HuaweiAuthError("no XSRF-TOKEN")

    monkeypatch.setattr(HW, "HuaweiClient", Fake)
    ses = MC.Sessions()
    for _ in range(3):
        with pytest.raises(MC.RateLimited):
            ses.huawei()
    assert len(logins) == 1                                           # a failed login is not repeated in the run

    class Ok(Fake):
        def login(self):
            logins.append(2)

    monkeypatch.setattr(HW, "HuaweiClient", Ok)
    ses = MC.Sessions()
    assert ses.huawei() is ses.huawei() and logins.count(2) == 1       # every meter shares the one session


def test_a_failed_discovery_keeps_the_known_meters_and_is_not_cached(tmp_path, monkeypatch):
    prev = [{"plant_key": "A", "brand": "HUAWEI", "meter_id": "1"}, {"plant_key": "B", "brand": "GROWATT", "meter_id": "x:36"}]
    assert MC.merge_discovery([{"plant_key": "B", "brand": "GROWATT", "meter_id": "x:36"}], {"A"}, prev) == \
        [prev[1], prev[0]]
    import json as _j
    st = tmp_path / "meters.json"
    st.write_text(_j.dumps({"fetched": 0, "meters": prev}))                # stale: asks again
    monkeypatch.setattr(MC, "STATE_DIR", str(tmp_path))

    def disc(pf, keys, ses, failed):
        failed.add("A")
        return [prev[1]]

    monkeypatch.setattr(MC, "discover", disc)
    got = MC.load_meters(None, None, False, False, None)
    assert {m["plant_key"] for m in got} == {"A", "B"}
    assert _j.loads(st.read_text())["fetched"] == 0                        # not rewritten: next run asks again
