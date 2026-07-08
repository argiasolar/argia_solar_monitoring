"""Guard the CO2 emission factor: a single shared constant, value 0.444.

Superseded 2026-07: the report used to carry its own local
``MX_GRID_KG_CO2_PER_KWH = 0.435``. It was lifted into
``argia.core.constants`` and set to 0.444 so the daily report, the
invoicing annex, the dashboard and all audit text quote one number.
These tests fail loudly if the factor drifts or a module reintroduces a
private copy.
"""

import argia.report.daily as daily
from argia.core.constants import CO2_KG_PER_KWH


def test_co2_factor_is_the_argia_standard():
    # Fleet-wide standard. If this changes, every report/annex/dashboard
    # CO2 number changes with it — intentional, single point of control.
    assert CO2_KG_PER_KWH == 0.444


def test_report_uses_the_shared_constant_not_a_local_copy():
    # The old private constant must be gone so it can never diverge again.
    assert not hasattr(daily, "MX_GRID_KG_CO2_PER_KWH")
    assert daily.CO2_KG_PER_KWH == 0.444


def test_fleet_stats_co2_uses_the_factor():
    from argia.report.daily import PlantDay, fleet_stats

    p = PlantDay(
        plant_key="X", name="X", energy_kwh=1000.0, expected_kwh=None,
        production_pct=None, pr=None, availability=None, soiling=None,
        cloud_pct=None, data_class="full", status_note="", kwp_dc=100.0,
    )
    st = fleet_stats([p])
    assert st["co2_kg"] == 1000.0 * CO2_KG_PER_KWH
