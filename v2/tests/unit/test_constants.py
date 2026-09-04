"""Guard the CO2 emission factor register — one table, every surface.

History of this file: the report once carried a private
``MX_GRID_KG_CO2_PER_KWH = 0.435``; it was lifted into
``argia.core.constants`` at 0.438 so report, annex and dashboard quoted
one number. That guard covered the annex and report_gen — but NOT
``server/monitoring_gen.py``, which quietly kept 0.435 until 2026-09-04.
The portfolio map had been reporting a different CO2 total from the
report site for weeks and no test noticed.

2026-09-04 (Tomasz): the factor is a REGISTER, not one number —
SEMARNAT/CRE publish it per year (2024's 0.444 currently applies), and
SAG contracted 0.202 for their whole history. These tests pin the table,
the override, and every literal copy of it that lives outside the
package.
"""

import re
from pathlib import Path

import pytest

import argia.report.daily as daily
from argia.core import co2
from argia.core.constants import CO2_KG_PER_KWH

V2 = Path(__file__).resolve().parents[2]


class TestRegister:
    def test_the_published_table(self):
        assert co2.FACTOR_BY_YEAR == {
            2020: 0.494, 2021: 0.423, 2022: 0.435,
            2023: 0.438, 2024: 0.444}

    def test_current_factor_is_2024s(self):
        assert co2.CURRENT == 0.444
        assert CO2_KG_PER_KWH == 0.444

    def test_a_year_in_the_table_returns_that_year(self):
        assert co2.factor(2021) == 0.423
        assert co2.factor(2023) == 0.438

    def test_years_after_the_table_keep_the_current_factor(self):
        # 2024 is "currently applicable", so 2025/2026 use it until CRE
        # publishes the next one
        assert co2.factor(2025) == 0.444
        assert co2.factor(2026) == 0.444
        assert co2.factor(2030) == 0.444

    def test_years_before_the_table_clamp_and_never_raise(self):
        assert co2.factor(2015) == 0.494

    def test_no_year_means_the_current_one(self):
        assert co2.factor() == co2.CURRENT

    def test_string_year_is_accepted(self):
        assert co2.factor("2023") == 0.438
        assert co2.factor_for_month("2023-07") == 0.438


class TestSagOverride:
    """SAG (MEX1) asked for 0.202 across their whole history."""

    def test_sag_uses_its_contracted_factor_every_year(self):
        for y in (2020, 2023, 2024, 2026, 2031):
            assert co2.factor(y, "MEX1") == 0.202

    def test_the_override_beats_the_year_table_outright(self):
        assert co2.factor(2021, "MEX1") != co2.factor(2021)

    def test_case_and_whitespace_do_not_defeat_it(self):
        assert co2.factor(2026, " mex1 ") == 0.202

    def test_no_other_plant_is_overridden(self):
        assert set(co2.PLANT_OVERRIDE) == {"MEX1"}
        for k in ("GTO1", "MEX2", "NL1", "SLP1", "SLP2", "QRO1", "TAM1"):
            assert co2.factor(2026, k) == 0.444

    def test_label_says_which_authority_applies(self):
        assert "contracted" in co2.label("MEX1")
        assert "0.202" in co2.label("MEX1")
        assert "SEMARNAT/CRE" in co2.label("GTO1")
        assert "0.444" in co2.label("GTO1")

    def test_factors_by_year_map_for_the_browser(self):
        m = co2.factors_by_year("MEX1")
        assert set(m.values()) == {0.202}
        assert co2.factors_by_year("GTO1")["2022"] == 0.435
        assert co2.factors_by_year("GTO1")["2026"] == 0.444


class TestNoPrivateCopiesLeft:
    def test_daily_report_has_no_private_constant(self):
        assert not hasattr(daily, "MX_GRID_KG_CO2_PER_KWH")

    def test_report_bundle_table_matches_the_register(self):
        """report_gen.py cannot import the argia package, so it carries a
        literal table. Same numbers or the annex and the report site
        disagree again."""
        src = (V2 / "server" / "bundle" / "report_gen.py").read_text(
            encoding="utf-8")
        m = re.search(r"CO2_BY_YEAR = (\{[^}]*\})", src, re.S)
        assert m, "CO2_BY_YEAR literal missing from report_gen"
        assert eval(m.group(1)) == co2.FACTOR_BY_YEAR      # noqa: S307
        o = re.search(r"CO2_PLANT_OVERRIDE = (\{[^}]*\})", src)
        assert o, "CO2_PLANT_OVERRIDE literal missing from report_gen"
        assert eval(o.group(1)) == co2.PLANT_OVERRIDE      # noqa: S307

    def test_monitoring_gen_fallback_matches_the_register(self):
        """The gap that let the map drift to 0.435 for weeks: nothing
        pinned monitoring_gen. It imports the register now, but its
        offline fallback must agree too."""
        src = (V2 / "server" / "monitoring_gen.py").read_text(
            encoding="utf-8")
        m = re.search(r"_CO2_BY_YEAR = (\{[^}]*\})", src, re.S)
        assert m, "monitoring_gen fallback table missing"
        assert eval(m.group(1)) == co2.FACTOR_BY_YEAR      # noqa: S307
        o = re.search(r"_CO2_OVERRIDE = (\{[^}]*\})", src)
        assert o and eval(o.group(1)) == co2.PLANT_OVERRIDE  # noqa: S307

    def test_no_surface_still_hardcodes_an_old_factor(self):
        """0.435 and 0.438 must not survive as bare CO2 literals in any
        rendering surface outside the declared register tables — that is
        exactly how the map drifted to 0.435 unnoticed."""
        tables = re.compile(r"_?CO2_BY_YEAR = \{[^}]*\}", re.S)
        for rel in ("server/monitoring_gen.py", "server/bundle/report_gen.py",
                    "argia/report/daily.py", "argia/finance/annex.py"):
            src = tables.sub("", (V2 / rel).read_text(encoding="utf-8"))
            for line in src.splitlines():
                assert "0.435" not in line and "0.438" not in line, \
                    f"{rel}: stray CO2 literal -> {line.strip()[:90]}"


class TestFleetStatsUsesPerPlantFactors:
    def _p(self, key, kwh):
        from argia.report.daily import PlantDay
        return PlantDay(
            plant_key=key, name=key, energy_kwh=kwh, expected_kwh=None,
            production_pct=None, pr=None, availability=None, soiling=None,
            cloud_pct=None, data_class="full", status_note="", kwp_dc=100.0)

    def test_single_plant_uses_the_year_factor(self):
        from argia.report.daily import fleet_stats
        st = fleet_stats([self._p("GTO1", 1000.0)], "2026-09-03")
        assert st["co2_kg"] == pytest.approx(1000.0 * 0.444)

    def test_sag_uses_its_own_factor(self):
        from argia.report.daily import fleet_stats
        st = fleet_stats([self._p("MEX1", 1000.0)], "2026-09-03")
        assert st["co2_kg"] == pytest.approx(1000.0 * 0.202)

    def test_fleet_total_is_summed_per_plant_not_one_scalar(self):
        from argia.report.daily import fleet_stats
        st = fleet_stats([self._p("GTO1", 1000.0), self._p("MEX1", 1000.0)],
                         "2026-09-03")
        assert st["co2_kg"] == pytest.approx(1000.0 * 0.444 + 1000.0 * 0.202)
        # the naive fleet-scalar answer would have been 888.0
        assert st["co2_kg"] != pytest.approx(2000.0 * 0.444)

    def test_an_older_day_uses_that_years_factor(self):
        from argia.report.daily import fleet_stats
        st = fleet_stats([self._p("GTO1", 1000.0)], "2022-06-01")
        assert st["co2_kg"] == pytest.approx(1000.0 * 0.435)

    def test_no_date_still_works(self):
        from argia.report.daily import fleet_stats
        st = fleet_stats([self._p("GTO1", 1000.0)])
        assert st["co2_kg"] == pytest.approx(1000.0 * co2.CURRENT)
