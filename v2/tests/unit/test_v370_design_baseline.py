"""v370 - HelioScope as-connected designs as the internal O&M baseline (Tomasz, 12 Nov 2026).

"we should use them as our baseline for all calculations and monitoring, change expectations and calibrate
better" - decided the same day: internal only (the customer invoice annex keeps the contract curve), ageing
0.55 %/yr from interconnection. All plants, reports and numbers here are invented.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import types

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))
sys.path.insert(0, str(V2 / "tools"))

from argia.kpi import design_model as DM  # noqa: E402

GHI = [150, 160, 210, 225, 235, 215, 215, 210, 175, 170, 150, 140]
POA = [170, 172, 220, 226, 230, 210, 212, 210, 180, 185, 170, 160]
KWH = [20000, 21000, 27000, 28000, 28500, 27000, 28000, 27500, 23000, 23000, 20000, 18500]


def entry(**kw):
    e = {"source": "HelioScope 'PPA Demo Roof', A. Tester", "report_date": "2026-11-01", "kwp_dc": 180.0,
         "kwp_ac": 150.0, "sensor": "ghi", "cod": "2025-01-01", "degradation": 0.0055,
         "annual_kwh": float(sum(KWH)), "pr": 0.75, "condition_set": "Demo Moderate", "weather": "TMY demo",
         "months": [{"ghi": g, "poa": p, "grid_kwh": k, "soiling_pct": 3.0} for g, p, k in zip(GHI, POA, KWH)],
         "losses": {"Soiling": 3.0, "Temperature": 6.0}}
    e.update(kw)
    return e


def design(**kw):
    return DM.parse({"plants": {"DEMO1": entry(**kw)}})["DEMO1"]


# ------------------------------------------------------------------ the model
class TestParse:
    def test_valid_file(self):
        d = design()
        assert (d.plant_key, d.sensor, d.cod, d.kwp_dc) == ("DEMO1", "ghi", dt.date(2025, 1, 1), 180.0)
        assert d.annual_kwh == sum(KWH) and len(d.months) == 12 and d.losses[0] == ("Soiling", 3.0)

    def test_every_problem_listed_at_once(self):
        bad = entry(sensor="tilted", degradation=0.5, cod="soon", annual_kwh=1.0)
        bad["months"] = bad["months"][:11]
        with pytest.raises(DM.Invalid) as ex:
            DM.parse({"plants": {"DEMO1": bad, "x y": entry()}})
        msg = str(ex.value)
        for part in ("sensor", "degradation", "cod", "need exactly 12", "bad plant key"):
            assert part in msg

    def test_months_must_add_up_to_the_report(self):
        with pytest.raises(DM.Invalid, match="add up"):
            design(annual_kwh=sum(KWH) * 1.01)

    def test_kwp_or_kwh_typo_is_caught(self):
        with pytest.raises(DM.Invalid, match="kWh/kWp"):
            design(kwp_dc=18.0)

    def test_no_plants_object(self):
        with pytest.raises(DM.Invalid):
            DM.parse({"designs": []})


class TestFigures:
    def test_ageing_starts_at_interconnection(self):
        d = design()
        assert d.ageing(dt.date(2024, 6, 1)) == 1.0
        assert d.ageing(dt.date(2026, 1, 1)) == pytest.approx(0.9945, abs=1e-4)

    def test_design_month_is_aged_at_mid_month(self):
        d = design()
        assert d.design_kwh_month(2026, 7) == pytest.approx(28000 * d.ageing(dt.date(2026, 7, 15)))
        assert d.design_kwh_day(dt.date(2026, 7, 3)) == pytest.approx(d.design_kwh_month(2026, 7) / 31)
        assert d.design_kwh_year(2026) == pytest.approx(sum(d.design_kwh_month(2026, m) for m in range(1, 13)))

    def test_expected_uses_the_sensor_plane(self):
        g, p = design(sensor="ghi"), design(sensor="poa")
        assert g.per_irradiance(1) == pytest.approx(20000 / 150)
        assert p.per_irradiance(1) == pytest.approx(20000 / 170)
        day = dt.date(2026, 1, 10)
        assert g.expected_kwh(day, 5.0) == pytest.approx(20000 / 150 * 5.0 * g.ageing(day))

    def test_resource_vs_design_year(self):
        d = design()
        assert d.resource(2026, 7, 215 * 10 / 31, 10) == pytest.approx(1.0)
        assert d.resource(2026, 7, 0, 0) is None


class TestEpi:
    def test_counts_measured_days_and_skips_undercounted_sun(self):
        d = design(cod="2026-07-01")
        day = dt.date(2026, 7, 1)
        exp1 = d.expected_kwh(day, 6.0)
        rows = [(day, exp1 * 1.10, 6.0),                  # 110 %
                (dt.date(2026, 7, 2), 900.0, None),       # no sun measured: not counted
                (dt.date(2026, 7, 3), None, 6.0),         # no energy: not counted
                (dt.date(2026, 7, 4), 0.0, 0.0),          # night-dark sensor: not counted
                (dt.date(2026, 7, 5), 1200.0, 1.0)]       # PR 6.7 on 180 kWp: sun undercounted
        e = DM.epi(d, rows)
        assert (e.days, e.skipped) == (1, 1)
        assert e.epi == pytest.approx(1.10)

    def test_no_days_no_figure(self):
        assert DM.epi(design(), []).epi is None

    def test_band(self):
        assert DM.band(1.03) == "in line" and DM.band(0.97) == "in line"
        assert DM.band(1.031) == "above" and DM.band(0.969) == "below" and DM.band(None) is None

    def test_design_vs_contract_needs_the_whole_year(self):
        d = design()
        full = {m: 20000.0 for m in range(1, 13)}
        assert DM.design_vs_contract(d, 2026, full) == pytest.approx(d.design_kwh_year(2026) / 240000.0)
        full[6] = 0
        assert DM.design_vs_contract(d, 2026, full) is None


class TestLoad:
    def test_missing_file_changes_nothing(self, tmp_path):
        assert DM.load(str(tmp_path / "none.json")) == {}

    def test_broken_file_is_ignored_by_pages_refused_by_the_deploy(self, tmp_path, monkeypatch):
        f = tmp_path / "d.json"
        f.write_text(json.dumps({"plants": {"DEMO1": entry(sensor="?")}}))
        assert DM.load(str(f)) == {}
        with pytest.raises(DM.Invalid):
            DM.load(str(f), strict=True)
        f.write_text(json.dumps({"plants": {"DEMO1": entry()}}))
        monkeypatch.setenv(DM.PATH_ENV, str(f))
        assert list(DM.load()) == ["DEMO1"]


# ------------------------------------------------------------------ the extractor
REPORT = """PPA Demo Roof DEMO ROOF, Calle Falsa 123
Project
Name
DEMO ROOF
 Report
Design PPA Demo Roof
Module DC
Nameplate
180.00 kW
Inverter AC
Nameplate
150.00 kW
Load Ratio: 1.20
Annual
Production
300.0 MWh
Performance
Ratio
75.0%
kWh/kWp 1,694.4
Weather Dataset
TMY, 0.04° Grid (20.00,-100.00),
NREL (psm3)
Month
GHI
(kWh/m )
January 150.0 170.0 168.0 24,000.0 20,000.0
February 160.0 172.0 170.0 25,000.0 21,000.0
March 210.0 220.0 218.0 32,000.0 27,000.0
April 225.0 226.0 224.0 33,000.0 28,000.0
May 235.0 230.0 228.0 34,000.0 28,500.0
June 215.0 210.0 208.0 32,000.0 27,000.0
July 215.0 212.0 210.0 33,000.0 28,000.0
August 210.0 210.0 208.0 32,500.0 27,500.0
September 175.0 180.0 178.0 27,500.0 23,000.0
October 170.0 185.0 183.0 27,500.0 23,000.0
November 150.0 170.0 168.0 24,000.0 20,000.0
December 140.0 160.0 158.0 22,000.0 18,500.0
Soiling
Soiling: 3.0%
Electrical
Mismatch
Mismatch: 2.0%
Temperature: 6.0%
Annual Production Report produced by A. Tester
© 2026 Aurora Solar 1 / 4 November 1, 2026
Description Output
Energy to Grid 290,500.0 -0.5%
Description Demo Moderate
Weather Dataset TMY, 0.04° Grid (20.00,-100.00), NREL (psm3)
Soiling (%)
J F M A M J J A S O N D
3 4 5.5 6.5 6 3 1.5 1.5 1.5 2.5 3 3
"""


class TestExtract:
    def test_reads_the_report(self):
        import helioscope_extract as HX
        e = HX.parse_report(REPORT)
        assert (e["kwp_dc"], e["kwp_ac"], e["annual_kwh"], e["pr"]) == (180.0, 150.0, 290500.0, 0.75)
        assert e["months"][0] == {"ghi": 150.0, "poa": 170.0, "grid_kwh": 20000.0, "soiling_pct": 3.0}
        assert e["months"][11]["grid_kwh"] == 18500.0 and e["months"][3]["soiling_pct"] == 6.5
        assert e["condition_set"] == "Demo Moderate" and e["weather"].startswith("TMY")
        assert e["report_date"] == "2026-11-01" and e["source"] == "HelioScope 'PPA Demo Roof', A. Tester"
        assert e["losses"] == {"Soiling": 3.0, "Mismatch": 2.0, "Temperature": 6.0}

    def test_a_changed_layout_is_refused_not_guessed(self):
        import helioscope_extract as HX
        with pytest.raises(HX.Unreadable, match="month March.*Energy to Grid|Energy to Grid.*month March"):
            HX.parse_report(REPORT.replace("March 210.0", "Mar 210.0").replace("Energy to Grid", "Energy"))

    def test_upsert_validates_the_whole_file(self):
        import helioscope_extract as HX
        e = HX.entry(REPORT, "ghi", "2025-01-01")
        doc = HX.upsert({"plants": {"DEMO2": entry()}}, "demo1", e)
        assert sorted(doc["plants"]) == ["DEMO1", "DEMO2"]
        with pytest.raises(DM.Invalid):
            HX.upsert({}, "DEMO1", dict(e, sensor="sky"))

    def test_annual_check_against_energy_to_grid(self):
        import helioscope_extract as HX
        e = HX.entry(REPORT.replace("Energy to Grid 290,500.0", "Energy to Grid 390,500.0"), "ghi", "2025-01-01")
        with pytest.raises(DM.Invalid, match="add up"):
            HX.upsert({}, "DEMO1", e)


# ------------------------------------------------------------------ the internal page
pytest.importorskip("portal_chrome")
import design_view as DV  # noqa: E402

TODAY = dt.date(2026, 11, 12)
PLANTS = {"DEMO1": {"portfolio": "PPA", "kwp": 181.0}, "DEMO2": {"portfolio": "PPA", "kwp": 300.0},
          "DEMO3": {"portfolio": "CAPEX", "kwp": 50.0}}
NAME = {"DEMO1": "Demo Roof", "DEMO2": "Demo Pending", "DEMO3": "Demo Capex"}.get


def daily(d, ratio, first=dt.date(2026, 6, 1), last=dt.date(2026, 11, 11), sun=6.0):
    out, x = [], first
    while x <= last:
        out.append((x, d.expected_kwh(x, sun) * ratio, sun))
        x += dt.timedelta(days=1)
    return out


def page(designs, data, contract=None):
    return DV.render(designs, data, contract or {}, PLANTS, TODAY, NAME, lambda k: f"/monitoring/{k.lower()}/")


class TestPage:
    def test_nothing_loaded_says_so(self):
        assert "No HelioScope designs are loaded" in page({}, {})

    def test_summary_epi_and_pending_plants(self):
        d = design(caveat="Demo sensor dirty.")
        contract = {("DEMO1", 2026, m): 20000.0 for m in range(1, 13)}
        h = page({"DEMO1": d}, {"DEMO1": daily(d, 0.95)}, contract)
        assert 'class="pill warn"' in h and "95.0%" in h                   # 5 % below: amber
        assert "Design pending" in h and "DEMO2" in h and "Demo Capex" not in h.split("<table>")[1].split("</table>")[0]
        assert f"{d.design_kwh_year(2026) / 1000:,.1f}" in h and "240.0" in h
        assert "The portal counts 181.00 kWp, the design 180.00 kWp." in h
        assert "Demo sensor dirty." in h and "Internal page" in h

    def test_in_line_above_and_far_below(self):
        d = design()
        assert 'class="pill ok"' in page({"DEMO1": d}, {"DEMO1": daily(d, 1.01)})
        assert 'class="pill off"' in page({"DEMO1": d}, {"DEMO1": daily(d, 1.08)})
        assert 'class="pill bad"' in page({"DEMO1": d}, {"DEMO1": daily(d, 0.85)})

    def test_month_rows(self):
        d = design(cod="2026-08-01")
        rows = DV.month_rows(d, daily(d, 1.0), TODAY, 180.0)
        assert [r["ym"] for r in rows] == ["2026-08", "2026-09", "2026-10", "2026-11"]   # nothing before COD
        nov = rows[-1]
        assert nov["partial"] and nov["design"] == pytest.approx(d.design_kwh_month(2026, 11) * 11 / 30)
        assert nov["epi"].epi == pytest.approx(1.0)
        assert rows[0]["sun"] == pytest.approx(6.0 * 31 / 210)

    def test_commissioning_days_before_cod_do_not_count(self):
        d = design(cod="2026-04-01")
        data = daily(d, 0.50, first=dt.date(2026, 1, 1), last=dt.date(2026, 3, 31)) + daily(d, 1.0, first=dt.date(2026, 4, 1))
        ytd = page({"DEMO1": d}, {"DEMO1": data}).split("<table>")[1].split("</tr>")[1]
        assert ytd.count("100.0%") == 4 and "50.0" not in ytd

    def test_periods_end_yesterday(self):
        ps = {k: (a, b) for k, _, _, a, b in DV.periods(TODAY)}
        assert ps["30d"] == (dt.date(2026, 10, 13), dt.date(2026, 11, 11))
        assert ps["mtd"] == (dt.date(2026, 11, 1), dt.date(2026, 11, 11))
        assert ps["pm"] == (dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        assert ps["ytd"][0] == dt.date(2026, 1, 1)

    def test_psql_rows(self):
        data = DV.normalise([["DEMO1", "2026-11-01", "100.5", ""], ["DEMO1", "bad", "1", "1"], ["x"]])
        assert data == {"DEMO1": [(dt.date(2026, 11, 1), 100.5, None)]}
        assert DV.contracts([["DEMO1", "2026", "1", "20000"], ["DEMO1", "x"]]) == {("DEMO1", 2026, 1): 20000.0}

    def test_no_em_dash(self):
        d = design(caveat="x")
        assert chr(0x2014) not in page({"DEMO1": d}, {"DEMO1": daily(d, 1.0)})


# ------------------------------------------------------------------ internal daily report only
class TestDailyReport:
    def _build(self, monkeypatch, designs):
        import argia.kpi.pg_kpi_source as PKS
        import argia.report.daily as RD
        header = ["date_iso", "plant_key", "energy_kwh", "expected_kwh", "design_kwh", "data_class"]
        monkeypatch.setattr(PKS, "kpi_grid", lambda s, r: [header, ["2026-11-10", "DEMO1", "800", "820", "999", "full"]])
        monkeypatch.setattr(RD, "load_design_monthly", lambda s: {})
        monkeypatch.setattr(RD, "load_maintenance_events", lambda s: [])
        monkeypatch.setattr(RD, "read_day_bundle", lambda s, d: types.SimpleNamespace(rows_for_plant=lambda k: []))
        monkeypatch.setattr(RD, "load_alerts_ledger", lambda s: types.SimpleNamespace(records=[]))
        plant = types.SimpleNamespace(plant_key="DEMO1", customer="Demo Roof", kwp_dc=180.0, tariff_mxn_per_kwh=None)
        pf = types.SimpleNamespace(daily_report_plants=lambda: [plant], inverters_for=lambda k: [])
        return RD.build_report_data(None, pf, "2026-11-10", designs=designs)

    def test_internal_edition_uses_the_design(self, monkeypatch):
        d = design()
        data = self._build(monkeypatch, {"DEMO1": d})
        assert data.plants[0].design_kwh == pytest.approx(round(d.design_kwh_day(dt.date(2026, 11, 10)), 1))
        assert data.helioscope == ("DEMO1",)

    def test_without_designs_nothing_changes(self, monkeypatch):
        data = self._build(monkeypatch, None)
        assert data.plants[0].design_kwh == 999.0 and data.helioscope == ()

    def test_only_the_internal_script_passes_designs(self):
        assert "designs=design_model.load()" in (V2 / "scripts" / "report_daily.py").read_text(encoding="utf-8")
        for f in ("scripts/report_client_daily.py", "server/bundle/demo_annexes.py"):
            assert "designs=" not in (V2 / f).read_text(encoding="utf-8")

    def test_footer_names_the_source(self):
        from argia.report.daily import ReportData, render_html
        from tests.unit.test_report_daily import _plant
        ps = [_plant(k) for k in ("SLP1", "SLP2", "GTO1", "MEX1", "NL1", "MEX2")]
        h = render_html(ReportData(date_iso="2026-11-10", plants=ps, alerts=[], helioscope=("MEX1", "SLP1")))
        assert "HelioScope as-connected design (Nov 2026, MEX1, SLP1: monthly kWh aged 0.55 per cent" in h
        h = render_html(ReportData(date_iso="2026-11-10", plants=ps, alerts=[]))
        assert "Of design = production vs the contract design estimate" in h and "%%" not in h


# ------------------------------------------------------------------ the customer side is untouched
class TestInternalOnly:
    def test_annex_and_kpi_stamping_do_not_read_the_designs(self):
        for f in ("argia/finance/annex.py", "scripts/kpi_eod.py", "server/bundle/report_gen.py",
                  "argia/kpi/design.py"):
            assert "design_model" not in (V2 / f).read_text(encoding="utf-8"), f

    def test_page_is_behind_the_employee_gate_and_off_the_demo(self):
        import auth_core as A
        assert A.area_for_path("/monitoring/design/") == "monitoring"
        src = (V2 / "server" / "bundle" / "portal_gen.py").read_text(encoding="utf-8")
        assert "os.environ.get('ARGIA_INSTANCE') == 'demo'" in src.split("DESIGNS =")[1].split("\n")[0]
