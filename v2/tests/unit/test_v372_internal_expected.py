"""v372 - one internal expectation from the HelioScope design (Tomasz, 12 Nov 2026: "Separate internal
figure"): the internal daily report, the daily alert rule, the loss page, the 19:00 mail and the
/monitoring/ pages judge a plant with a design against the design for the sun that was measured; the
stored expected_kwh (the invoice annex 'teorica') and every customer-facing reader stay as they were.
All plants and numbers are invented.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
import types

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

from argia.kpi import design_model as DM          # noqa: E402
from tests.unit.test_v370_design_baseline import design   # noqa: E402  (the invented DEMO1 design)

DAY = dt.date(2026, 7, 10)


class TestInternalExpected:
    def test_design_for_the_measured_sun(self):
        d = design()
        x, basis = DM.internal_expected({"DEMO1": d}, "demo1", DAY, 6.0, 500.0)
        assert basis == "design" and x == pytest.approx(d.expected_kwh(DAY, 6.0), abs=0.01)
        assert DM.internal_expected({"DEMO1": d}, "DEMO1", "2026-07-10", "6.0", "500")[1] == "design"

    def test_stored_figure_otherwise(self):
        d = design(cod="2026-08-01")
        assert DM.internal_expected({"DEMO1": d}, "DEMO1", DAY, 6.0, 500.0) == (500.0, "factor")   # before COD
        assert DM.internal_expected({"DEMO1": d}, "DEMO1", dt.date(2026, 9, 1), None, "500") == (500.0, "factor")
        assert DM.internal_expected({"DEMO1": d}, "DEMO1", dt.date(2026, 9, 1), 0.0, None) == (None, "factor")
        assert DM.internal_expected({}, "DEMO1", DAY, 6.0, 500.0) == (500.0, "factor")
        assert DM.internal_expected(None, "OTHER", DAY, 6.0, "x") == (None, "factor")

    def test_factor_and_clean_expectation(self):
        d = design()
        assert d.factor(DAY) == pytest.approx(d.per_irradiance(7) * d.ageing(DAY))
        assert d.clean_expected_kwh(DAY, 6.0) == pytest.approx(d.expected_kwh(DAY, 6.0) / 0.97)    # 3 % soiling


class TestSoilingDrift:
    def test_against_the_clean_design_of_the_month(self):
        d = design()
        clean = d.clean_expected_kwh(DAY, 6.0)
        assert DM.soiling_drift(d, DAY, clean * 0.92, 6.0) == pytest.approx(0.08, abs=1e-4)
        assert DM.soiling_drift(d, DAY, clean * 1.05, 6.0) == 0.0          # cleaner than clean is not negative

    def test_no_figure_when_it_would_lie(self):
        d = design()
        assert DM.soiling_drift(d, DAY, 99999.0, 6.0) is None             # the sun was undercounted
        assert DM.soiling_drift(d, DAY, 100.0, 0.0) is None
        assert DM.soiling_drift(d, DAY, None, 6.0) is None
        assert DM.soiling_drift(design(cod="2026-08-01"), DAY, 100.0, 6.0) is None


class TestDailyReportInternalEdition:
    def _build(self, monkeypatch, designs, irr="6.0"):
        import argia.kpi.pg_kpi_source as PKS
        import argia.report.daily as RD
        header = ["date_iso", "plant_key", "energy_kwh", "irradiance_kwh_m2", "expected_kwh", "production_pct",
                  "soiling_loss_pct", "design_kwh", "data_class"]
        monkeypatch.setattr(PKS, "kpi_grid", lambda s, r: [header, ["2026-07-10", "DEMO1", "800", irr, "820", "0.9756",
                                                                    "0.02", "999", "full"]])
        monkeypatch.setattr(RD, "load_design_monthly", lambda s: {})
        monkeypatch.setattr(RD, "load_maintenance_events", lambda s: [])
        monkeypatch.setattr(RD, "read_day_bundle", lambda s, d: types.SimpleNamespace(rows_for_plant=lambda k: []))
        monkeypatch.setattr(RD, "load_alerts_ledger", lambda s: types.SimpleNamespace(records=[]))
        plant = types.SimpleNamespace(plant_key="DEMO1", customer="Demo Roof", kwp_dc=180.0, tariff_mxn_per_kwh=None)
        pf = types.SimpleNamespace(daily_report_plants=lambda: [plant], inverters_for=lambda k: [])
        return RD.build_report_data(None, pf, "2026-07-10", designs=designs).plants[0]

    def test_theoretical_of_plan_and_soiling_come_from_the_design(self, monkeypatch):
        d = design()
        p = self._build(monkeypatch, {"DEMO1": d})
        x = round(d.expected_kwh(DAY, 6.0), 1)
        assert p.expected_kwh == x and p.production_pct == pytest.approx(800 / x, abs=1e-4)
        assert p.soiling == DM.soiling_drift(d, DAY, 800.0, 6.0)

    def test_no_sun_measured_keeps_the_stamped_figures(self, monkeypatch):
        p = self._build(monkeypatch, {"DEMO1": design()}, irr="")
        assert (p.expected_kwh, p.production_pct, p.soiling) == (820.0, 0.9756, 0.02)

    def test_client_edition_unchanged(self, monkeypatch):
        p = self._build(monkeypatch, None)
        assert (p.expected_kwh, p.production_pct, p.soiling) == (820.0, 0.9756, 0.02)


class TestAlertRule:
    def test_energy_rule_reads_the_design(self, monkeypatch):
        import argia.kpi.pg_kpi_source as PKS
        import alerts_daily as AD
        header = ["date_iso", "plant_key", "energy_kwh", "specific_yield", "expected_kwh", "data_class",
                  "irradiance_kwh_m2"]
        rows = [header, ["2026-07-10", "DEMO1", "800", "4.4", "820", "full", "6.0"],
                ["2026-07-10", "OTHER", "500", "4.0", "600", "full", "6.0"]]
        monkeypatch.setattr(PKS, "kpi_grid", lambda s, r: rows)
        d = design()
        out = AD._read_kpi_day(None, "2026-07-10", {"DEMO1": d})
        assert out["DEMO1"]["expected"] == pytest.approx(d.expected_kwh(DAY, 6.0), abs=0.01)
        assert out["OTHER"]["expected"] == 600.0
        assert AD._read_kpi_day(None, "2026-07-10")["DEMO1"]["expected"] == 820.0

    def test_the_job_passes_the_designs(self):
        assert "_read_kpi_day(sheets, date_iso, design_model.load())" in (V2 / "scripts" / "alerts_daily.py").read_text(encoding="utf-8")


class TestLossesAndMail:
    def test_loss_engine_weather_input_is_the_design(self, monkeypatch):
        import loss_daily as LD
        monkeypatch.setattr(LD, "psql_rows", lambda sql: [["DEMO1", "2026-07-10", "800", "820", "6.0"],
                                                         ["OTHER", "2026-07-10", "500", "600", "6.0"]])
        d = design()
        prod = LD.production(DAY, DAY, {"DEMO1": d})
        assert prod[("DEMO1", "2026-07-10")] == (800.0, pytest.approx(d.expected_kwh(DAY, 6.0), abs=0.01))
        assert prod[("OTHER", "2026-07-10")] == (500.0, 600.0)
        assert LD.production(DAY, DAY)[("DEMO1", "2026-07-10")] == (800.0, 820.0)

    def test_mail_month_to_date(self, monkeypatch):
        import argia.store.pgq as PQ
        import daily_perf_mail as M
        d = design()
        monkeypatch.setattr(DM, "load", lambda *a, **k: {"DEMO1": d})
        monkeypatch.setattr(PQ, "psql_rows", lambda sql: [["DEMO1", "2026-07-01", "800", "820", "6.0"],
                                                         ["DEMO1", "2026-07-02", "700", "", ""],
                                                         ["OTHER", "2026-07-01", "500", "600", "6.0"]])
        out = M.gather_mtd(dt.date(2026, 7, 3))
        x = d.expected_kwh(dt.date(2026, 7, 1), 6.0)
        assert out["DEMO1"] == (1500.0, 800.0, pytest.approx(x, abs=0.01))     # day 2: no figure, not paired
        assert out["OTHER"] == (500.0, 500.0, 600.0)


class TestWiring:
    def test_monitoring_pages_use_the_design_off_the_demo(self):
        src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
        assert "DESIGNS = {} if os.environ.get('ARGIA_INSTANCE') == 'demo' else _DM.load()" in src
        assert src.count("design_expected(") >= 3 and "DESIGNS[pk].factor(" in src

    def test_customer_side_untouched(self):
        for f in ("scripts/kpi_eod.py", "argia/finance/annex.py", "scripts/dashboard_update.py",
                  "scripts/invoice_publish.py", "server/bundle/report_gen.py", "scripts/report_client_daily.py"):
            src = (V2 / f).read_text(encoding="utf-8")
            assert "design_model" not in src and "internal_expected" not in src, f


class TestMonitoringPerformancePage:
    def test_thirty_day_expected_uses_the_design(self):
        from tests.unit.test_v242_qa_mirek import MON, _days, _mon_ns
        ns = _mon_ns()
        seg = MON[MON.index("def design_expected("):]
        exec(compile(seg[:seg.index("\n\n\n")], "mon_design", "exec"), ns)
        d = design(cod="2026-01-01")
        ns.update(DESIGNS={"DEMO1": d}, _DM=DM)
        days = _days("2026-08-09", 30)
        rows = [("DEMO1", x, "0.8", "", "1", "800", "820", "1", "6.0") for x in days]
        rows += [("OTHER", x, "0.8", "", "1", "500", "600", "1", "6.0") for x in days]
        p = ns["perf_summary"](rows, "2026-09-08", {"DEMO1": "2026-01-01", "OTHER": "2026-01-01"})
        want = sum(d.expected_kwh(dt.date.fromisoformat(x), 6.0) for x in days)
        assert p["DEMO1"]["exp"] == pytest.approx(want, rel=1e-4) and p["OTHER"]["exp"] == 600.0 * 30
