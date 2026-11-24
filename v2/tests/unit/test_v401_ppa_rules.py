"""v401 - the PPA monthly report applies the methodology rules (Tomasz, 24 Nov 2026: "run the numbers for the
PPA monthly performance report for past 5 months, make sure we applied all our rules"). Decided the same day:
EPI-600 carries the engineering verdict when every day of the month was computed (the HelioScope day-energy ratio
before that), and an EPI above 100 % is shown as 100 % here too. Invented plants only."""
from __future__ import annotations

import io
import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
for _p in (V2, V2 / "server" / "bundle", V2 / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from argia.kpi import design_model as DM                   # noqa: E402
from argia.report import epi_views_html as EH              # noqa: E402
from argia.report import ppa_monthly as PM                 # noqa: E402
from argia.report import ppa_monthly_view as V             # noqa: E402
from tests.unit.test_v374_ppa_monthly import EM, days, design   # noqa: E402

PLANTS = [{"plant_key": "DEMO1", "name": "Demo Roof", "kwp_dc": "100", "tariff_mxn_per_kwh": "2.0"},
          {"plant_key": "DEMO2", "name": "Other Land", "kwp_dc": "50", "tariff_mxn_per_kwh": "3.0"}]
CONTRACT = [{"plant_key": "DEMO1", "year": "2026", "month": "7", "contract_kwh": "15500", "tariff_mxn": "2.5"},
            {"plant_key": "DEMO2", "year": "2026", "month": "7", "contract_kwh": "6200", "tariff_mxn": "3.0"}]


def ctx(daily=None, station=(), epi600=None, losses=()):
    daily = daily if daily is not None else days("DEMO1", "2026-07", 31, 500, 6.0) + days("DEMO2", "2026-07", 31, 200, 6.0)
    return PM.Context(PLANTS, daily, CONTRACT, (), losses, (), (), (), {"DEMO1": design()}, station, epi600)


def with_source(rows, day, source):
    for r in rows:
        if r["prod_date"] == day:
            r["irradiance_source"] = source
    return rows


def full_epi600(pk, real, exp, n=31):
    return {pk: {f"2026-07-{d:02d}": [real, exp, 50] for d in range(1, n + 1)}}


# ------------------------------------------------------------------ the sun counts only when it was measured
class TestSunGate:
    def test_estimated_sun_is_not_a_reading(self):
        d = with_source(days("DEMO1", "2026-07", 31, 500, 6.0), "2026-07-05", "cloud_cover_model")
        p = PM.plant_month(ctx(d), "DEMO1", "2026-07", "2026-08-15")
        assert p["sun_days"] == 30 and p["sun_gated"] == {"estimated": 1}
        # that day counts its typical design day in the bridge: the steps still add up to the actual
        assert sum(p["bridge"].values()) + p["contract_kwh"] == pytest.approx(p["actual_kwh"])
        assert any("estimated from cloud cover" in en for en, _ in p["notes"])

    def test_undercounted_sun_is_skipped(self):
        d = days("DEMO1", "2026-07", 31, 500, 6.0)
        d[9]["irradiance_kwh_m2"] = "3.0"                     # 500 kWh on 100 kWp under 3.0 kWh/m2 = PR 1.67
        p = PM.plant_month(ctx(d), "DEMO1", "2026-07", "2026-08-15")
        assert p["sun_gated"] == {"undercounted": 1} and p["sun_days"] == 30
        clean = PM.plant_month(ctx(days("DEMO1", "2026-07", 31, 500, 6.0)), "DEMO1", "2026-07", "2026-08-15")
        assert p["epi"] == pytest.approx(clean["epi"])         # the undercounted day no longer lifts the index
        assert DM.PR_MAX == 1.05

    def test_station_coverage_gate_on_our_own_samples_only(self):
        st = [{"plant_key": "DEMO1", "prod_date": "2026-07-03", "coverage": "0.63", "status": "fail", "reasons": ""},
              {"plant_key": "DEMO1", "prod_date": "2026-07-04", "coverage": "0.63", "status": "fail", "reasons": ""}]
        d = with_source(days("DEMO1", "2026-07", 31, 500, 6.0), "2026-07-04", "shinemaster_history")
        p = PM.plant_month(ctx(d, station=st), "DEMO1", "2026-07", "2026-08-15")
        # 3 Jul: our snapshot samples were thin -> not a reading; 4 Jul came whole from the logger's own history
        assert p["sun_gated"] == {"coverage": 1} and p["sun_days"] == 30
        assert PM.STATION_COV_MIN == 0.90

    def test_drift_is_noted_never_adjusted(self):
        st = [{"plant_key": "DEMO1", "prod_date": "2026-07-03", "coverage": "1", "status": "fail",
               "reasons": "module-ambient +8.0 K vs its own start and the fleet"}]
        a = PM.plant_month(ctx(station=st), "DEMO1", "2026-07", "2026-08-15")
        b = PM.plant_month(ctx(), "DEMO1", "2026-07", "2026-08-15")
        assert a["epi"] == b["epi"] and a["sun_index"] == b["sun_index"]
        assert any("infrared check" in en for en, _ in a["notes"])

    def test_no_sun_note_without_a_design(self):
        d = with_source(days("DEMO2", "2026-07", 31, 200, 6.0), "2026-07-05", "cloud_cover_model")
        p = PM.plant_month(ctx(d), "DEMO2", "2026-07", "2026-08-15")
        assert not any("Sun not used" in en for en, _ in p["notes"])


# ------------------------------------------------------------------ EPI-600 carries the engineering verdict
class TestEpi600:
    def test_every_day_computed_gives_the_verdict(self):
        p = PM.plant_month(ctx(epi600=full_epi600("DEMO1", 90.0, 100.0)), "DEMO1", "2026-07", "2026-08-15")
        assert p["epi600"] == pytest.approx(0.90) and p["epi600_days"] == 31
        assert p["engineering"] == p["epi600"] and p["engineering_basis"] == "epi600"
        assert p["verdict"]["engineering"] == "bad"            # 90 % is red, whatever the HelioScope ratio says

    def test_a_missing_day_means_no_figure(self):
        e = full_epi600("DEMO1", 90.0, 100.0)
        del e["DEMO1"]["2026-07-17"]
        p = PM.plant_month(ctx(epi600=e), "DEMO1", "2026-07", "2026-08-15")
        assert p["epi600"] is None and p["epi600_days"] == 30
        assert p["engineering_basis"] == "design" and p["engineering"] == p["epi"]
        assert any("computed on 30 of 31 days" in en for en, _ in p["notes"])

    def test_a_day_without_samples_counts_as_computed(self):
        e = full_epi600("DEMO1", 90.0, 100.0)
        e["DEMO1"]["2026-07-17"] = None                         # settled, no valid sample at 600 W/m2
        p = PM.plant_month(ctx(epi600=e), "DEMO1", "2026-07", "2026-08-15")
        assert p["epi600"] == pytest.approx(0.90) and p["epi600_days"] == 31

    def test_portfolio_is_a_ratio_of_sums(self):
        e = full_epi600("DEMO1", 90.0, 100.0) | full_epi600("DEMO2", 30.0, 30.0)
        P = PM.build(ctx(epi600=e), "2026-07", "2026-08-15")["portfolio"]
        assert P["epi600"] == pytest.approx((31 * 120.0) / (31 * 130.0)) and P["epi600_plants"] == 2
        assert P["engineering"] == P["epi600"] and P["engineering_basis"] == "epi600"

    def test_without_the_engine_the_helioscope_ratio_stays(self):
        P = PM.build(ctx(), "2026-07", "2026-08-15")["portfolio"]
        assert P["engineering_basis"] == "design" and P["engineering"] == P["epi"]

    def test_headline_names_the_method(self):
        h = PM.build(ctx(epi600=full_epi600("DEMO1", 90.0, 100.0)), "2026-07", "2026-08-15")["headline"]
        assert "(EPI-600)" in h["en"] and "delivered 90% of what their design predicts" in h["en"]
        assert "(EPI-600)" in h["es"] and EM not in h["en"] + h["es"]


# ------------------------------------------------------------------ above 100 % is shown as 100 %
class TestCap:
    def test_same_cap_as_the_epi_card(self):
        assert PM.EPI_DISPLAY_MAX == EH.EPI_DISPLAY_MAX == 1.0
        assert PM.cap(1.08) == 1.0 and PM.cap(0.93) == 0.93 and PM.cap(None) is None

    def test_page_shows_100_the_numbers_stay(self):
        rep = PM.build(ctx(epi600=full_epi600("DEMO1", 108.0, 100.0)), "2026-07", "2026-08-15")
        p = rep["plants"][0]
        assert p["epi600"] == pytest.approx(1.08)              # the stored figure is not altered
        h = V.body(rep, V.print_lang("en"), "2026-08-15")
        assert ">108%" not in h and "+8%" not in h and ">100%" in h
        assert "delivered 100% of what their design predicts" in rep["headline"]["en"]

    def test_workbook_shows_100_and_keeps_the_kwh(self):
        openpyxl = pytest.importorskip("openpyxl")
        from argia.report import ppa_monthly_xlsx as X
        rep = PM.build(ctx(epi600=full_epi600("DEMO1", 108.0, 100.0)), "2026-07", "2026-08-15")
        ws = openpyxl.load_workbook(io.BytesIO(X.workbook(rep)))["Plants"]
        head = [c.value for c in ws[1]]
        row = {h: ws.cell(2, j + 1).value for j, h in enumerate(head)}
        assert row["EPI-600 (shown, max 100%)"] == 1.0 and row["EPI-600 actual kWh"] == pytest.approx(31 * 108.0)
        assert len(head) == len(set(head))


# ------------------------------------------------------------------ availability, totals, losses
class TestAvailabilityTotalsLosses:
    def test_availability_weighs_days_by_coverage_and_shows_it(self):
        d = days("DEMO1", "2026-07", 2, 500, 6.0)
        d[0].update(availability="1.0", avail_coverage="1.0")
        d[1].update(availability="0.5", avail_coverage="0.2")   # a day mostly unknown weighs little
        p = PM.plant_month(ctx(d), "DEMO1", "2026-07", "2026-08-15")
        assert p["availability"] == pytest.approx((1.0 + 0.5 * 0.2) / 1.2, abs=1e-4)
        assert p["availability_coverage"] == pytest.approx(0.6)

    def test_month_in_progress_stays_out_of_the_totals(self):
        d = days("DEMO1", "2026-06", 30, 400, 6.0) + days("DEMO1", "2026-07", 10, 500, 6.0)
        rep = PM.build(ctx(d), "2026-07", "2026-07-11")
        assert not rep["complete"] and rep["ytd"]["to"] == "2026-06" and rep["lifetime"]["to"] == "2026-06"
        assert rep["ytd"]["actual_kwh"] == 30 * 400.0
        closed = PM.build(ctx(d), "2026-07", "2026-08-15")
        assert closed["ytd"]["to"] == "2026-07"

    def test_estimated_sun_underperformance_before_v389_is_dropped(self):
        lo = [{"plant_key": "DEMO1", "prod_date": f"2026-07-{k:02d}", "lost_kwh": "30", "unavailability_kwh": "20",
               "overheating_kwh": "0", "underperformance_kwh": "10", "excused_kwh": "0", "lost_mxn": "75",
               "expected_basis": "weather-model" if k == 1 else "weather", "peers": "", "tariff_mxn": "2.5"}
              for k in range(1, 32)]
        lo[1]["expected_basis"], lo[1]["peers"] = "weather-model", "DEMO2"   # with peers it stays
        p = PM.plant_month(ctx(losses=lo), "DEMO1", "2026-07", "2026-08-15")
        assert p["loss"]["underperformance_kwh"] == pytest.approx(300.0)
        assert p["loss"]["lost_kwh"] == pytest.approx(31 * 30 - 10) and p["loss"]["lost_mxn"] == pytest.approx(31 * 75 - 25)
        assert p["loss"]["unavailability_kwh"] == pytest.approx(31 * 20)         # downtime always counts


# ------------------------------------------------------------------ pages, old frozen months, the generator
class TestRendering:
    def test_old_frozen_report_still_renders(self):
        rep = PM.build(ctx(), "2026-07", "2026-08-15")
        for p in rep["plants"]:
            for k in ("engineering", "engineering_basis", "epi600", "epi600_days", "availability_coverage"):
                p.pop(k, None)
        for k in ("engineering", "engineering_basis", "engineering_plants", "epi600", "epi600_plants"):
            rep["portfolio"].pop(k, None)
        h = V.body(rep, V.web_lang(), "2026-08-15")
        assert "Plant by plant" in h and EM not in h

    def test_helioscope_figures_are_marked(self):
        e = full_epi600("DEMO1", 90.0, 100.0)
        h = V.plants_table(PM.build(ctx(epi600=e), "2026-07", "2026-08-15"), V.print_lang("en"))
        assert ">90%<" in h
        assert V._basis_mark("design") in V.plants_table(PM.build(ctx(), "2026-07", "2026-08-15"), V.print_lang("en"))

    def test_index_shows_the_capped_figure(self):
        cards = [{"month": "2026-10", "href": "/ppa-report/2026-10/", "status": "draft", "actual_kwh": 1.0,
                  "promise": 0.85, "epi": 1.02, "basis": "design", "sun_index": 0.7, "lost_mxn": None}]
        h = V.index_body(cards, V.print_lang("en"), "2026-11-24")
        assert ">100%<sup" in h and "Performance" in h

    def test_generator_reads_the_engine_cache(self, monkeypatch):
        import ppa_monthly_gen as G
        from argia.epi import live as EPL
        monkeypatch.setattr(EPL, "fingerprint", lambda a, b, k: (_ for _ in ()).throw(OSError()) if k == "NOREG" else "fp")
        store = {("DEMO1", "2026-07-01"): {"tallies": {"epi600": {"plant": [9.0, 10.0, 5]}}},
                 ("DEMO1", "2026-07-02"): {"tallies": {"epi600": {}}}}
        monkeypatch.setattr(EPL, "read_cached", lambda cdir, k, d, fp: store.get((k, d.isoformat())))
        out = G.epi600_days(["DEMO1", "NOREG"], {"DEMO1": ["2026-07-01", "2026-07-02", "2026-07-03"]})
        assert out == {"DEMO1": {"2026-07-01": [9.0, 10.0, 5], "2026-07-02": None}}

    def test_generator_fetches_the_new_inputs_read_only(self):
        import ppa_monthly_gen as G
        assert "irradiance_source" in G.SQL["daily"] and "station_check_daily" in G.SQL["station"]
        assert "expected_basis, peers, tariff_mxn" in G.SQL["losses"] and "station" in G.OPTIONAL
        for sql in G.SQL.values():
            assert sql.lstrip().upper().startswith("SELECT")
