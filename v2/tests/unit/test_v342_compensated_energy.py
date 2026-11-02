"""v342: compensated energy from customer maintenance, for any day.

Tomasz, 2 Nov 2026: "this event will show compensated energy for the day
instead of actual reading. So for example in Plastic Omnium we had such
event on 11th of october 2026 ... compensated energy of 1859kWh. I added
such event to Maintenance Events, but it didn't trigger the change of
invoice for October, we also have such problem in SAG 24th of October".

Cause: compensated energy was stamped once, by kpi_eod, for yesterday only;
an event logged afterwards never reached billable_kwh or the invoice. And
the formula was the contract (NL1: 2,602 kWh/day), not the agreed figure.
"""
import datetime as dt

import pytest

from argia.core.time_utils import MX_TZ
from argia.finance import invariants as INV
from argia.maintenance import deemed_sync as DS
from argia.maintenance.deemed import agreed_split, deemed_by_plant_day
from argia.maintenance.events import MaintenanceEvent, events_from_pg_rows
from scripts import invoice_publish as IP
from scripts import invoice_reissue as RE


def ev(start, end, kwh=None, cat="customer", appr="tomasz", pk="NL1"):
    t = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=MX_TZ)  # noqa: E731
    return MaintenanceEvent(pk, t(start), t(end) if end else None, cat, "", None, "", appr, kwh)


def contract(pk, y, m):
    return 80667.0 / 31                                   # NL1 Oct 2026 contract


def measured(pk, d, s, e):
    return 0.0


# ------------------------------------------------------------ the agreed kWh
class TestAgreedKwh:
    def test_nl1_11_october_is_the_agreed_1859_not_the_contract(self):
        e = ev("2026-10-11T01:01", "2026-10-11T23:00", kwh=1859.0)
        assert deemed_by_plant_day([e], contract, measured) == {("NL1", "2026-10-11"): 1859.0}

    def test_without_an_agreed_figure_the_contract_formula_stays(self):
        e = ev("2026-10-11T01:01", "2026-10-11T23:00")
        got = deemed_by_plant_day([e], contract, measured)
        assert got[("NL1", "2026-10-11")] == pytest.approx(80667.0 / 31)

    def test_a_multi_day_event_is_spread_by_daylight_and_adds_up(self):
        e = ev("2026-10-11T06:00", "2026-10-13T13:00", kwh=1000.0)   # 14 h + 14 h + 7 h of daylight
        split = agreed_split(e)
        assert split == {"2026-10-11": 400.0, "2026-10-12": 400.0, "2026-10-13": 200.0}
        assert sum(split.values()) == 1000.0

    def test_rounding_remainder_lands_on_the_last_day(self):
        e = ev("2026-10-11T06:00", "2026-10-13T20:00", kwh=100.0)
        split = agreed_split(e)
        assert round(sum(split.values()), 1) == 100.0

    def test_a_night_only_event_lands_on_its_first_day(self):
        assert agreed_split(ev("2026-10-11T21:00", "2026-10-12T05:00", kwh=50.0)) == {"2026-10-11": 50.0}

    @pytest.mark.parametrize("cat,appr", [("argia", "tomasz"), ("force_majeure", "tomasz"), ("customer", "")])
    def test_only_approved_customer_events_compensate(self, cat, appr):
        e = ev("2026-10-11T06:00", "2026-10-11T20:00", kwh=1859.0, cat=cat, appr=appr)
        assert deemed_by_plant_day([e], contract, measured) == {}

    def test_pg_rows_carry_the_kwh_and_the_id(self):
        rows = [["NL1", "2026-10-11T01:01:00", "2026-10-11T23:00:00", "customer", "", "", "x", "tomasz", "1859.0", "2"],
                ["NL1", "2026-10-12T01:01:00", "", "customer", "", "", "x", "tomasz", "-5", "3"],
                ["NL1", "2026-10-13T01:01:00", "", "customer", "", "", "x", "tomasz"]]
        a, b, c = events_from_pg_rows(rows)
        assert (a.deemed_kwh, a.event_id) == (1859.0, 2)
        assert b.deemed_kwh is None                        # a negative figure never becomes a credit
        assert c.deemed_kwh is None and c.event_id is None  # the old row shape still loads


# ------------------------------------------------------------ the daily table
class TestSyncPlan:
    def test_nl1_the_zero_day_gets_its_compensation(self):
        rows = {("NL1", "2026-10-10"): (2960.7, 2960.7), ("NL1", "2026-10-11"): (0.0, None)}
        changes, orphans = DS.plan(rows, {("NL1", "2026-10-11"): 1859.0})
        assert [(c.date_iso, c.new_billable, c.deemed_kwh) for c in changes] == [("2026-10-11", 1859.0, 1859.0)]
        assert orphans == []

    def test_a_partial_day_adds_on_top_of_what_was_measured(self):
        changes, _ = DS.plan({("NL1", "2026-10-11"): (700.0, 700.0)}, {("NL1", "2026-10-11"): 1159.0})
        assert changes[0].new_billable == 1859.0

    def test_already_right_is_left_alone(self):
        assert DS.plan({("NL1", "2026-10-11"): (0.0, 1859.0)}, {("NL1", "2026-10-11"): 1859.0}) == ([], [])

    def test_a_withdrawn_event_takes_its_compensation_off(self):
        changes, _ = DS.plan({("NL1", "2026-10-11"): (0.0, 1859.0)}, {})
        assert changes[0].new_billable == 0.0

    def test_billable_below_energy_is_the_resyncs_job_not_this(self):
        assert DS.plan({("NL1", "2026-10-11"): (100.0, 90.0)}, {}) == ([], [])

    def test_a_compensated_day_without_a_daily_row_is_reported_not_invented(self):
        changes, orphans = DS.plan({}, {("MEX1", "2026-10-24"): 1986.8})
        assert changes == [] and orphans == [("MEX1", "2026-10-24", 1986.8)]

    def test_month_total(self):
        rows = {("NL1", "2026-10-11"): (0.0, None), ("NL1", "2026-10-12"): (2684.2, 2684.2)}
        assert DS.month_deemed(rows, {("NL1", "2026-10-11"): 1859.0, ("NL1", "2026-09-30"): 5.0}) == {"NL1": 1859.0}

    def test_update_sql(self):
        c = DS.Change("NL1", "2026-10-11", 0.0, None, 1859.0, 1859.0)
        sql = DS.update_sql(c)
        assert "SET billable_kwh = 1859.000" in sql and "plant_key = 'NL1'" in sql and "2026-10-11" in sql
        assert "compensated 1859.0 kWh from customer maintenance" in sql

    def test_recent_months(self):
        assert DS.recent_months(dt.date(2026, 11, 2)) == ["2026-10", "2026-11"]
        assert DS.recent_months(dt.date(2027, 1, 15)) == ["2026-12", "2027-01"]


def test_sync_month_never_rewrites_an_invoiced_month_on_its_own(monkeypatch):
    monkeypatch.setattr(DS, "compute", lambda ym, plants: (
        {("NL1", "2026-10-11"): (0.0, None), ("SLP1", "2026-10-03"): (500.0, 500.0)},
        {("NL1", "2026-10-11"): 1859.0, ("SLP1", "2026-10-03"): 100.0}))
    monkeypatch.setattr(DS, "invoiced_plants", lambda ym: {"NL1": 0.0})
    sent = []
    import argia.store.pgq as pgq
    monkeypatch.setattr(pgq, "psql_exec", sent.append)
    r = DS.sync_month("2026-10", ["NL1", "SLP1"], apply=True)
    assert [c.plant_key for c in r["applied"]] == ["SLP1"]          # open plant-month only
    assert r["pending_invoiced"] == {"NL1": (0.0, 1859.0)}
    assert "'NL1'" not in sent[0] and "'SLP1'" in sent[0]
    sent.clear()
    r = DS.sync_month("2026-10", ["NL1", "SLP1"], apply=True, include_invoiced=True)
    assert sorted(c.plant_key for c in r["applied"]) == ["NL1", "SLP1"]


# ------------------------------------------------------------ the re-issue
REG = {"NL1": {"produced": 61035.0, "penalty": 0.0, "tariff": 2.022, "amount": 123412.77, "billable": 61035.0},
       "GTO1": {"produced": 102141.5, "penalty": 0.0, "tariff": 1.975, "amount": 201729.46, "billable": 102141.5}}


def test_reissue_nl1_october():
    new = RE.new_register(REG, {"NL1": 1859.0})
    assert new["NL1"] == (1859.0, 62894.0, 127171.67)              # 62,894 x 2.022
    assert new["GTO1"] == (0.0, 102141.5, 201729.46)               # untouched plants keep their figures
    sql = RE.update_sql("2026-10", {"NL1": new["NL1"]})
    assert sql.startswith("BEGIN;") and sql.endswith("COMMIT;")
    assert "penalty_kwh = 1859.000, billable_kwh = 62894.000, amount_mxn = 127171.67" in sql
    assert "produced_kwh" not in sql                                # measured energy is never touched


# ------------------------------------------------------------ the invoice gate
def _payload(penalty, day_deemed):
    days = [(dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(365)]
    atoms = []
    for d in days:
        if d > "2026-10-31":
            atoms.append([None] * 8)
        elif d == "2026-10-11":
            atoms.append([0.0, 0, 0, day_deemed, None, None, None, None])
        else:
            atoms.append([61035.0 / 30 if d[:7] == "2026-10" else 1000.0, 0, 0, 0.0, None, None, None, None])
    return {"days": days, "atoms": atoms, "kwp": 617.4, "tariff_by_month": {"2026-10": 2.022},
            "co2_factor": 0.4, "history": {"2026-10": {"kwh": 61035.0, "penalty": penalty,
                                                      "income": round((61035.0 + penalty) * 2.022, 2)}}}


def test_gate_holds_an_invoice_that_misses_the_compensation():
    """the days carry 1,859 kWh compensated, the register 0: HELD until the re-issue"""
    vs = INV.check_books("2026-10", _payload(0.0, 1859.0), {"kwh": 61035.0, "closed": True}, 61035.0, 31)
    assert [v.rule for v in vs] == ["DEEMED"] and "invoice_reissue.py --month 2026-10" in str(vs[0])


def test_gate_passes_the_reissued_nl1_october():
    p = _payload(1859.0, 1859.0)
    assert INV.check_invoice("2026-10", p, {"kwh": 61035.0, "closed": True}, 61035.0, 31) == []
    assert p["history"]["2026-10"]["income"] == 127171.67


# ------------------------------------------------------------ the register on the 1st
def test_first_run_register_keeps_measured_and_compensated_apart():
    import json
    p = _payload(0.0, 1859.0)
    p["history"] = {}
    html = "<script>const D = %s;\n</script>" % json.dumps(p)
    measured_kwh, comp, mxn = IP.annex_month_split(html, "2026-10")
    assert (round(measured_kwh, 1), comp) == (61035.0, 1859.0)
    assert mxn == pytest.approx(62894.0 * 2.022, abs=0.01)


def test_the_invoice_check_compares_measured_with_the_close():
    """a compensated month is not a MISMATCH: the close is measured energy"""
    assert IP.invoice_check(61035.0, 61035.0)[0] == "OK"
    assert IP.invoice_check(61035.0 + 1859.0, 61035.0)[0] == "MISMATCH"


def test_record_invoicing_checks_measured_against_the_close():
    import pathlib
    src = (pathlib.Path(IP.__file__)).read_text(encoding="utf-8")
    assert 'invoice_check(float(h["kwh"]), billing)' in src
    assert "annex_month_split(fh.read(), ym)" in src


def test_nightly_sync_runs_after_the_billable_resync():
    import pathlib
    src = (pathlib.Path(IP.__file__).parent / "recon_snapshot.py").read_text(encoding="utf-8")
    assert src.index("build_billable_resync_sql()") < src.index("DS.sync_month(ym, ppa, apply=True)")


# ------------------------------------------------------------ v343
def test_reissue_script_refuses_bad_input():
    import pathlib
    import subprocess
    sh = pathlib.Path(IP.__file__).parent.parent / "pi" / "reissue.sh"
    for args in (["2026-13", "dry"], ["2026-10", "now"], ["", ""], ["2026-10; rm", "dry"]):
        r = subprocess.run(["bash", str(sh), *args], capture_output=True, text=True)
        assert r.returncode == 2, (args, r.stdout)


def test_annex_shows_compensated_energy_on_the_charts_and_the_card():
    from argia.finance.annex import render_annex_html
    days = [(dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(365)]
    p = _payload(1859.0, 1859.0)
    p.update(plant_key="NL1", client="PLASTIC OMNIUM", factura_name="PLASTIC_OMNIUM", client_logo="",
             co2_factor_by_year={}, co2_factor_label="", co2_factor_contracted=False)
    assert p["days"] == days
    html = render_annex_html(p, "now", default_ym="2026-10")
    assert "Energía compensada kWh" in html and "--comp:" in html
    assert 'id="c_comp"' in html and '" compensada = "' in html
    assert "comp:a[AI.deemed]" in html
