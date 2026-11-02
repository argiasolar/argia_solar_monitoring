"""v341: the arithmetic every invoice must satisfy, and the gate that holds
an invoice when one rule breaks (argia.finance.invariants)."""
import datetime as dt
import json
import math
import os
from unittest.mock import MagicMock

import pytest

from argia.finance import invariants as INV
from argia.finance.annex import ATOM_WIDTH
from argia.recon import engine as E
from scripts import invoice_publish as IP
from scripts import report_invoice_annex as ria

YM = "2026-10"


def _payload(kwh_per_day=1000.0, until="2026-10-31", history=None, kwp=600.0):
    d0 = dt.date(2026, 1, 1)
    days, atoms = [], []
    for i in range(365):
        iso = (d0 + dt.timedelta(days=i)).isoformat()
        days.append(iso)
        if iso <= until:
            atoms.append([kwh_per_day, 1100.0, 1050.0, 0.0, 0.2, 0.85, 1.0, 0.01])
        else:
            atoms.append([None] * ATOM_WIDTH)
    return {"plant_key": "GTO1", "kwp": kwp, "days": days, "atoms": atoms,
            "tariff_by_month": {"2026-%02d" % m: 2.0 for m in range(1, 13)},
            "co2_factor": 0.4, "history": history if history is not None else {
                "2026-09": {"kwh": 30000.0, "penalty": 0.0, "income": 60000.0},
                "2026-10": {"kwh": 31000.0, "penalty": 0.0, "income": 62000.0}}}


CLOSE = {"kwh": 31000.0, "closed": True}


def rules(vs):
    return sorted({v.rule for v in vs})


class TestAnnex:
    def test_a_correct_invoice_breaks_no_rule(self):
        assert INV.check_invoice(YM, _payload(), CLOSE, 31000.0, 31) == []

    def test_leak_november_days_on_the_october_invoice(self):
        """the 2 Nov 2026 bug: 1-2 Nov production on the October invoice"""
        vs = INV.check_annex(_payload(until="2026-11-02"), YM, 600.0)
        assert "LEAK" in rules(vs) and "YEAR" in rules(vs)
        assert "2 day(s) after 2026-10-31" in str(vs[0])

    def test_leak_a_later_register_month(self):
        p = _payload()
        p["history"]["2026-11"] = {"kwh": 3898.7, "penalty": 0.0, "income": 7700.0}
        assert "LEAK" in rules(INV.check_annex(p, YM, 600.0))

    @pytest.mark.parametrize("mutate", ["drop", "dup", "year"])
    def test_axis(self, mutate):
        p = _payload()
        if mutate == "drop":
            del p["days"][40], p["atoms"][40]
        elif mutate == "dup":
            p["days"][41] = p["days"][40]
        else:
            p["days"] = [d.replace("2026", "2025") for d in p["days"]]
        assert "AXIS" in rules(INV.check_annex(p, YM, 600.0))

    @pytest.mark.parametrize("bad", [-5.0, float("nan"), float("inf")])
    def test_value(self, bad):
        p = _payload()
        p["atoms"][100][0] = bad
        assert "VALUE" in rules(INV.check_annex(p, YM, 600.0))

    def test_physics_a_glitch_day(self):
        p = _payload()
        p["atoms"][200][0] = 9000.0                      # 15 kWh/kWp on 600 kWp
        assert rules(INV.check_annex(p, YM, 600.0)) == ["PHYSICS"]

    def test_empty_month(self):
        p = _payload(until="2026-09-30", history={})
        assert "EMPTY" in rules(INV.check_annex(p, YM, 600.0))

    def test_mxn_must_be_kwh_times_the_months_tariff(self):
        p = _payload()
        p["history"]["2026-10"]["income"] = 62000.01         # one centavo off is off
        assert rules(INV.check_annex(p, YM, 600.0)) == ["MXN"]
        p["history"]["2026-10"]["income"] = 62000.0 + 1e-9   # float noise is not
        assert INV.check_annex(p, YM, 600.0) == []

    def test_mxn_without_a_tariff(self):
        p = _payload()
        p["tariff_by_month"]["2026-10"] = None
        assert "MXN" in rules(INV.check_annex(p, YM, 600.0))


class TestBooks:
    def test_month_not_closed(self):
        assert rules(INV.check_books(YM, _payload(), {"kwh": 31000.0, "closed": False}, 31000.0, 31)) == ["CLOSED"]

    def test_invoice_differs_from_the_close(self):
        assert rules(INV.check_books(YM, _payload(), {"kwh": 30999.0, "closed": True}, 31000.0, 31)) == ["CLOSE"]

    def test_daily_table_must_add_up(self):
        """1 Nov 2026: the daily rows were the inverter sums, Taigene 7.8 kWh
        under the portal month that was invoiced"""
        assert rules(INV.check_books(YM, _payload(), CLOSE, 30992.2, 31)) == ["TABLE"]

    def test_daily_table_vendor_rounding_is_tolerated(self):
        """Vitalmex Oct 2026: Huawei's dailies 0.45 kWh under its month counter"""
        assert INV.check_books(YM, _payload(), CLOSE, 30999.55, 31) == []

    def test_daily_table_missing_a_day(self):
        assert rules(INV.check_books(YM, _payload(), CLOSE, 31000.0, 30)) == ["TABLE"]

    def test_first_run_of_the_month_uses_the_day_records(self):
        """on the 1st there is no register row yet: the annex's days are the invoice"""
        p = _payload(history={"2026-09": {"kwh": 30000.0, "penalty": 0.0, "income": 60000.0}})
        assert INV.check_books(YM, p, CLOSE, 31000.0, 31) == []
        assert rules(INV.check_books(YM, p, {"kwh": 31010.0, "closed": True}, 31000.0, 31)) == ["CLOSE"]

    def test_prior_month_drift(self, monkeypatch):
        real = INV.rollup_month

        def drifted(payload, ym):
            r = dict(real(payload, ym))
            if ym == "2026-09":
                r["billable_kwh"] += 12.0
            return r
        monkeypatch.setattr(INV, "rollup_month", drifted)
        assert "PRIOR" in rules(INV.check_books(YM, _payload(), CLOSE, 31000.0, 31))


# the six October 2026 invoices as they stand after v340 (register, close, daily table)
OCT_2026 = {"GTO1": (102141.50, 1.975, 201729.46, 102141.50), "NL1": (61035.00, 2.022, 123412.77, 61035.00),
            "SLP1": (17314.80, 2.596, 44949.22, 17314.80), "SLP2": (27364.80, 2.462, 67372.14, 27364.80),
            "MEX2": (62505.42, 2.367, 147950.33, 62504.97), "MEX1": (55139.65, 2.508, 138290.24, 55139.70)}


@pytest.mark.parametrize("pk", sorted(OCT_2026))
def test_october_2026_invoices_pass_every_rule(pk):
    kwh, tariff, mxn, table = OCT_2026[pk]
    p = _payload(kwh_per_day=table / 31, history={YM: {"kwh": kwh, "penalty": 0.0, "income": mxn}}, kwp=900.0)
    p["tariff_by_month"][YM] = tariff
    assert round(kwh * tariff, 2) == mxn
    assert INV.check_invoice(YM, p, {"kwh": kwh, "closed": True}, table, 31) == []


# ---------------------------------------------------------------- the gate
def _run_main(monkeypatch, tmp_path, payload, books):
    plant = MagicMock()
    plant.plant_key = "GTO1"
    pf = MagicMock()
    pf.plants = {"GTO1": plant}
    pf.financial_plants = lambda: [plant]
    monkeypatch.setattr(ria, "open_sheets", lambda: object())
    monkeypatch.setattr(ria, "load_portfolio", lambda s: pf)
    monkeypatch.setattr(ria, "reconciliation_gate", lambda ym, plants: (list(plants), []))
    monkeypatch.setattr(ria, "load_invoicing_overview", lambda y: {})
    monkeypatch.setattr(ria, "build_annex_data", lambda *a, **k: payload)
    monkeypatch.setattr(ria, "render_annex_html", lambda p, g, default_ym=None: "<html>new</html>")
    monkeypatch.setattr(ria, "month_books", lambda ym: books)
    return ria.main(["--month", YM, "--out-dir", str(tmp_path)])


def test_gate_publishes_a_correct_invoice(monkeypatch, tmp_path):
    books = {"GTO1": {"close": CLOSE, "daily_sum": 31000.0, "daily_days": 31}}
    assert _run_main(monkeypatch, tmp_path, _payload(), books) == 0
    assert (tmp_path / "factura_TAIGENE_202610.html").read_text() == "<html>new</html>"
    rep = json.loads((tmp_path / "invariants_202610.json").read_text())
    assert rep["plants"] == {"TAIGENE": []}


def test_gate_holds_a_leaking_invoice_and_removes_the_old_copy(monkeypatch, tmp_path):
    (tmp_path / "factura_TAIGENE_202610.html").write_text("<html>old</html>")
    (tmp_path / "factura_TAIGENE_202610.pdf").write_text("old pdf")
    books = {"GTO1": {"close": CLOSE, "daily_sum": 31000.0, "daily_days": 31}}
    assert _run_main(monkeypatch, tmp_path, _payload(until="2026-11-02"), books) == 2
    assert not (tmp_path / "factura_TAIGENE_202610.html").exists()
    assert not (tmp_path / "factura_TAIGENE_202610.pdf").exists()
    assert (tmp_path / "_held" / "factura_TAIGENE_202610.pdf").read_text() == "old pdf"
    rep = json.loads((tmp_path / "invariants_202610.json").read_text())
    assert rep["plants"]["TAIGENE"][0].startswith("LEAK")
    # the index lists it as held, with the rule
    held = IP.held_reasons(str(tmp_path), YM)
    assert held["TAIGENE"].startswith("LEAK")
    b = IP.blocked_reasons({YM: []}, {("GTO1", YM): ("PASS", True)}, {YM: held})
    assert any(n == "TAIGENE" and "HELD" in why and "LEAK" in why for n, why in b[YM])


def test_gate_holds_everything_when_the_books_are_unreadable(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, tmp_path, _payload(), {"__unreadable__": "psql down"}) == 2
    assert not (tmp_path / "factura_TAIGENE_202610.html").exists()


def test_gate_holds_when_the_check_itself_crashes(monkeypatch, tmp_path):
    books = {"GTO1": {"close": CLOSE, "daily_sum": 31000.0, "daily_days": 31}}
    assert _run_main(monkeypatch, tmp_path, {"broken": True}, books) == 2
    rep = json.loads((tmp_path / "invariants_202610.json").read_text())
    assert rep["plants"]["TAIGENE"][0].startswith("ERROR")


def test_not_closed_keeps_its_own_wording(tmp_path):
    (tmp_path / "invariants_202610.json").write_text(json.dumps(
        {"plants": {"SAG": ["CLOSED: the 2026-10 reconciliation is not closed"]}}))
    assert IP.held_reasons(str(tmp_path), YM) == {}


def test_drive_copy_of_a_held_invoice_goes_to_the_trash(monkeypatch, tmp_path):
    (tmp_path / "invariants_202610.json").write_text(json.dumps({"plants": {"TAIGENE": ["LEAK: x"], "SAG": []}}))
    d = MagicMock()
    d.ensure_folder.return_value = "F"
    d.find_file.return_value = "fid123"
    import argia.core.drive as drv
    monkeypatch.setattr(drv, "DriveClient", lambda: d)
    monkeypatch.setenv("GOOGLE_ARCHIVE_FOLDER_ID", "root")
    monkeypatch.setenv("GOOGLE_CREDENTIALS", "{}")
    IP.push_to_drive(YM, str(tmp_path))
    d.find_file.assert_called_once_with("F", "factura_TAIGENE_202610.pdf")
    d.trash_file.assert_called_once_with("fid123")


# ------------------------------------------------- the close (engine) side
class TestCloseGate:
    def _mc(self, **kw):
        a = dict(interval_sum_kwh=99990.0, vendor_daily_sum_kwh=100000.0, vendor_monthly_kwh=None,
                 lifetime_start_kwh=0.0, lifetime_end_kwh=100001.0, completeness_pct=99.0,
                 daily_days=31, days_in_month=31, daily_ref_sum_kwh=100000.0, daily_ref_days=31,
                 daily_table_sum_kwh=100000.0, daily_table_days=31)
        a.update(kw)
        return E.monthly_close(**a)

    def test_everything_adds_up_auto_closes(self):
        assert self._mc().status == E.STATUS_PASS

    def test_daily_table_off_by_more_than_the_rounding_holds(self):
        mc = self._mc(daily_table_sum_kwh=100007.8)
        assert mc.status == E.STATUS_REVIEW and "align the days" in mc.note

    def test_daily_table_missing_a_day_holds(self):
        assert self._mc(daily_table_days=30).status == E.STATUS_REVIEW

    def test_portal_vs_register_beyond_the_band_holds(self):
        mc = self._mc(lifetime_end_kwh=100300.0)              # 0.3 %: auto-closed before v341
        assert mc.status == E.STATUS_REVIEW and "auto-close needs 0.05%" in mc.note

    def test_zero_month_holds(self):
        mc = self._mc(interval_sum_kwh=0.0, vendor_daily_sum_kwh=0.0, lifetime_end_kwh=0.0,
                      daily_ref_sum_kwh=0.0, daily_table_sum_kwh=0.0)
        assert mc.status == E.STATUS_REVIEW and "0 kWh" in mc.note

    def test_one_source_alone_holds(self):
        mc = self._mc(vendor_daily_sum_kwh=None, daily_days=0, lifetime_start_kwh=None, lifetime_end_kwh=None)
        assert mc.billing_basis == E.BASIS_DAILY_REF and mc.status == E.STATUS_REVIEW
        assert "single source" in mc.note

    def test_old_callers_without_the_table_still_work(self):
        mc = E.monthly_close(99990.0, 100000.0, None, 0.0, 100001.0, 99.0, 31, 31, 100000.0, 31)
        assert mc.status == E.STATUS_PASS and not math.isnan(mc.billing_kwh)


def test_engine_and_invariants_share_the_table_tolerance():
    assert INV.TABLE_TOL_KWH == E.DAILY_TABLE_TOL_KWH


def test_report_file_name():
    assert os.path.basename(ria.report_path("/x", YM)) == "invariants_202610.json"


def test_cli_reads_the_published_annexes(tmp_path, capsys):
    from scripts import billing_invariants as BI
    d = tmp_path / YM
    d.mkdir()
    good, leak = _payload(), _payload(until="2026-11-02")
    (d / "factura_TAIGENE_202610.html").write_text("<script>const D = %s;\n</script>" % json.dumps(good))
    (d / "factura_SAG_202610.html").write_text("<script>const D = %s;\n</script>" % json.dumps(leak))
    rc = BI.main(["--months", YM, "--out-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "TAIGENE" in out and "SAG" in out and "LEAK" in out and "1 invoice(s) break a rule" in out


class TestMonthToDate:
    def test_three_ledgers_agree(self):
        assert E.mtd_check(50000.0, 15, 15, 50001.0, 50000.4) == ("OK", "")

    def test_daily_table_behind_the_register(self):
        st, note = E.mtd_check(49700.0, 15, 15, 50000.0, 50000.0)
        assert st == "REVIEW" and "daily table -300.0 kWh against the lifetime register" in note

    def test_portal_behind_the_register(self):
        st, note = E.mtd_check(50000.0, 15, 15, 49800.0, 50000.0)
        assert st == "REVIEW" and "vendor portal -200.0" in note

    def test_missing_day(self):
        st, note = E.mtd_check(50000.0, 14, 15, 50000.0, 50000.0)
        assert st == "REVIEW" and "14/15" in note

    def test_no_register_compares_table_and_portal(self):
        assert E.mtd_check(50000.0, 15, 15, 50000.0, None)[0] == "OK"
        assert E.mtd_check(50100.0, 15, 15, 50000.0, None)[0] == "REVIEW"

    def test_rounding_is_not_a_finding(self):
        assert E.mtd_check(1000.0, 1, 1, 1001.9, 1000.0)[0] == "OK"


def test_nightly_month_to_date_reads_the_three_ledgers(monkeypatch):
    from scripts import recon_snapshot as RS

    def rows(sql):
        if "FROM daily_production" in sql:
            return [["GTO1", "49700.0", "15"], ["MEX1", "40000.0", "15"]]
        if "sum(daily_kwh)" in sql:
            return [["GTO1", "50000.0"], ["MEX1", "35000.0"]]
        if "monthly_kwh, lifetime_kwh" in sql:
            return [["GTO1", "", "1050000.0"], ["MEX1", "40000.0", "540000.0"]]
        if "lifetime_kwh FROM" in sql:
            return [["GTO1", "1000000.0"], ["MEX1", "500000.0"]]
        return []
    sent = []
    monkeypatch.setattr(RS, "psql_rows", rows)
    monkeypatch.setattr(RS, "psql_exec", sent.append)
    n = RS.month_to_date(dt.date(2026, 11, 15), ["GTO1", "MEX1"], dry_run=False)
    assert n == 1                                     # GTO1 table 300 kWh behind; MEX1 month counter agrees
    ins = sent[-1]
    assert "'GTO1', DATE '2026-11-01', DATE '2026-11-15'" in ins and "'REVIEW'" in ins
    assert "'MEX1'" in ins and "'OK'" in ins
