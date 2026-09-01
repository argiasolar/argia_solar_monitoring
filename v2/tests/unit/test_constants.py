"""Guard the CO2 emission factor: a single shared constant, value 0.438.

0.438 kg/kWh is the SEMARNAT/CRE Mexican grid factor, standardised by
Tomasz on 2026-09-01 after the August close caught the invoice annex
(0.444) disagreeing with the report site (0.438). One number, one
place; the report_gen bundle keeps a literal copy that this file pins
to the same value.

Superseded 2026-07: the report used to carry its own local
``MX_GRID_KG_CO2_PER_KWH = 0.435``. It was lifted into
``argia.core.constants`` and set to 0.438 so the daily report, the
invoicing annex, the dashboard and all audit text quote one number.
These tests fail loudly if the factor drifts or a module reintroduces a
private copy.
"""

import argia.report.daily as daily
from argia.core.constants import CO2_KG_PER_KWH


def test_co2_factor_is_the_argia_standard():
    # Fleet-wide standard. If this changes, every report/annex/dashboard
    # CO2 number changes with it — intentional, single point of control.
    assert CO2_KG_PER_KWH == 0.438


def test_report_uses_the_shared_constant_not_a_local_copy():
    # The old private constant must be gone so it can never diverge again.
    assert not hasattr(daily, "MX_GRID_KG_CO2_PER_KWH")
    assert daily.CO2_KG_PER_KWH == 0.438


def test_fleet_stats_co2_uses_the_factor():
    from argia.report.daily import PlantDay, fleet_stats

    p = PlantDay(
        plant_key="X", name="X", energy_kwh=1000.0, expected_kwh=None,
        production_pct=None, pr=None, availability=None, soiling=None,
        cloud_pct=None, data_class="full", status_note="", kwp_dc=100.0,
    )
    st = fleet_stats([p])
    assert st["co2_kg"] == 1000.0 * CO2_KG_PER_KWH


def test_the_report_bundle_copy_agrees():
    """report_gen.py cannot import the argia package (it lives in the
    server bundle), so it carries a literal CO2_T_PER_MWH. Same number
    or the customer annex and the report site disagree again."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "server" / "bundle" /
           "report_gen.py").read_text(encoding="utf-8")
    m = re.search(r"^CO2_T_PER_MWH = ([0-9.]+)", src, re.M)
    assert m, "CO2_T_PER_MWH literal missing from report_gen"
    assert float(m.group(1)) == CO2_KG_PER_KWH
