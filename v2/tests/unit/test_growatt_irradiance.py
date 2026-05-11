"""Tests for argia.meteo.growatt_irradiance.

The pure integration math is the most critical part — wrong arithmetic
here would mean wrong PR (Performance Ratio) numbers across the entire
portfolio. Tests cover the math exhaustively before the HTTP layer.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from argia.core.time_utils import MX_TZ
from argia.meteo.growatt_irradiance import (
    GrowattIrradianceClient,
    GrowattWebSession,
    extract_radiance_points,
    find_latest_radiance_wm2,
    integrate_radiance_to_kwh_m2,
    interval_kwh_m2_from_wm2,
)
from tests.conftest import load_fixture


# ===================================================================
# Pure: trapezoidal integration
# ===================================================================


class TestTrapezoidalIntegration:
    """Exact arithmetic, hand-verified."""

    def _ts(self, hour: int) -> dt.datetime:
        return dt.datetime(2026, 4, 15, hour, 0, tzinfo=MX_TZ)

    def test_empty_returns_zero(self):
        assert integrate_radiance_to_kwh_m2([]) == 0.0

    def test_single_point_returns_zero(self):
        # Cannot integrate over zero time
        assert integrate_radiance_to_kwh_m2([(self._ts(12), 1000.0)]) == 0.0

    def test_constant_1000_for_one_hour_equals_one_kwh(self):
        # 1000 W/m² × 1h = 1000 Wh/m² = 1.0 kWh/m²
        points = [(self._ts(12), 1000.0), (self._ts(13), 1000.0)]
        assert integrate_radiance_to_kwh_m2(points) == 1.0

    def test_linear_ramp(self):
        # 0 → 1000 over 1h, average = 500 → 0.5 kWh/m²
        points = [(self._ts(6), 0.0), (self._ts(7), 1000.0)]
        assert integrate_radiance_to_kwh_m2(points) == 0.5

    def test_two_hour_average(self):
        # 0 W → 500 W → 1000 W over 2h
        # Trap: (0+500)/2 + (500+1000)/2 = 250 + 750 = 1000 Wh/m² = 1.0 kWh/m²
        points = [(self._ts(6), 0.0), (self._ts(7), 500.0), (self._ts(8), 1000.0)]
        assert integrate_radiance_to_kwh_m2(points) == 1.0

    def test_sorts_unsorted_input(self):
        points = [(self._ts(8), 1000.0), (self._ts(7), 500.0), (self._ts(6), 0.0)]
        # Same answer as the sorted test above
        assert integrate_radiance_to_kwh_m2(points) == 1.0

    def test_negative_values_clamped_to_zero(self):
        # Negative readings sometimes come from miscalibrated sensors.
        # We treat them as 0 to avoid subtracting from the integral.
        points = [(self._ts(12), -50.0), (self._ts(13), 1000.0)]
        # (0 + 1000)/2 * 1h = 500 Wh/m² = 0.5 kWh/m²
        assert integrate_radiance_to_kwh_m2(points) == 0.5

    def test_gap_capped_to_max_gap(self):
        # 12:00 → 23:00 = 11h gap. With max_gap=2h, treat as 2h.
        # avg = 500 W, 2h → 1000 Wh = 1.0 kWh
        points = [
            (self._ts(12), 1000.0),
            (self._ts(23), 0.0),
        ]
        result = integrate_radiance_to_kwh_m2(points, max_gap_sec=7200)
        assert result == 1.0  # not 5.5 (which would be 11h × 500 / 1000)

    def test_zero_or_negative_delta_skipped(self):
        # Two readings at exact same time — should not contribute (Δt = 0)
        points = [
            (self._ts(12), 1000.0),
            (self._ts(12), 500.0),
            (self._ts(13), 1000.0),
        ]
        # Only the 12→13 pair contributes after dedup-via-sort.
        # Sorted by ts, the same-ts pair has Δt=0 and is skipped.
        # Then 12→13 trap: (500+1000)/2 ... but order after sort isn't deterministic
        # for equal timestamps. Let's verify it's at least not double-counting.
        result = integrate_radiance_to_kwh_m2(points)
        # Worst case: takes the 1000→1000 pair → 1.0; best case 500→1000 → 0.75
        assert 0.7 <= result <= 1.0

    def test_realistic_day_from_fixture(self):
        """Hand-computed from the env_history fixture: ~6.235 kWh/m²."""
        rows = load_fixture("meteo", "growatt_env_history.json")["obj"]["datas"]
        points = extract_radiance_points(rows)
        result = integrate_radiance_to_kwh_m2(points)
        # Hand calc:
        #   7→8: (50+250)/2 * 1h = 150 Wh
        #   8→9: 375
        #   9→10: 625
        #  10→11: 825
        #  11→12: 950
        #  12→13: 975
        #  13→14: 875
        #  14→15: 700
        #  15→16: 475
        #  16→17: 225
        #  17→18: 60
        #  Sum = 6235 Wh = 6.235 kWh
        assert result == 6.235


# ===================================================================
# Pure: extract_radiance_points
# ===================================================================


class TestExtractRadiancePoints:
    def test_extracts_valid_rows(self):
        rows = load_fixture("meteo", "growatt_env_history.json")["obj"]["datas"]
        points = extract_radiance_points(rows)
        assert len(points) == 12
        assert all(p[1] >= 0 for p in points)

    def test_skips_rows_without_calendar(self):
        rows = [
            {"radiant": 500.0},  # no calendar
            {
                "calendar": {"year": 2026, "month": 3, "dayOfMonth": 15, "hourOfDay": 12},
                "radiant": 800.0,
            },
        ]
        points = extract_radiance_points(rows)
        assert len(points) == 1
        assert points[0][1] == 800.0

    def test_skips_rows_with_invalid_calendar(self):
        rows = [
            {
                "calendar": {"year": "not a year", "month": 3, "dayOfMonth": 15},
                "radiant": 500.0,
            },
        ]
        assert extract_radiance_points(rows) == []

    def test_skips_rows_without_radiant(self):
        rows = [
            {"calendar": {"year": 2026, "month": 3, "dayOfMonth": 15, "hourOfDay": 12}}
        ]
        assert extract_radiance_points(rows) == []

    def test_clamps_negative_radiant(self):
        rows = [
            {
                "calendar": {"year": 2026, "month": 3, "dayOfMonth": 15, "hourOfDay": 12},
                "radiant": -100.0,
            }
        ]
        points = extract_radiance_points(rows)
        assert len(points) == 1
        assert points[0][1] == 0.0

    def test_empty_input(self):
        assert extract_radiance_points([]) == []
        assert extract_radiance_points(None) == []  # type: ignore[arg-type]

    def test_all_returned_points_are_tz_aware(self):
        rows = load_fixture("meteo", "growatt_env_history.json")["obj"]["datas"]
        points = extract_radiance_points(rows)
        assert all(p[0].tzinfo is not None for p in points)


# ===================================================================
# Pure: find_latest_radiance_wm2
# ===================================================================


class TestFindLatestRadianceWm2:
    def test_picks_latest_timestamp(self):
        rows = load_fixture("meteo", "growatt_env_history.json")["obj"]["datas"]
        # Latest in fixture is 18:00 with radiant=20.0
        assert find_latest_radiance_wm2(rows) == 20.0

    def test_unsorted_input_still_picks_latest(self):
        rows = [
            {
                "calendar": {"year": 2026, "month": 3, "dayOfMonth": 15, "hourOfDay": 12},
                "radiant": 500.0,
            },
            {
                "calendar": {"year": 2026, "month": 3, "dayOfMonth": 15, "hourOfDay": 14},
                "radiant": 800.0,
            },
            {
                "calendar": {"year": 2026, "month": 3, "dayOfMonth": 15, "hourOfDay": 13},
                "radiant": 600.0,
            },
        ]
        assert find_latest_radiance_wm2(rows) == 800.0

    def test_empty_returns_none(self):
        assert find_latest_radiance_wm2([]) is None

    def test_no_valid_rows_returns_none(self):
        assert find_latest_radiance_wm2([{"junk": True}]) is None


# ===================================================================
# Pure: interval_kwh_m2_from_wm2
# ===================================================================


class TestIntervalKwhM2FromWm2:
    def test_basic_conversion(self):
        # 1000 W/m² for 60 min = 1.0 kWh/m²
        assert interval_kwh_m2_from_wm2(1000.0, 60) == 1.0

    def test_ten_minute_interval(self):
        # 600 W/m² × 10 min = 0.1 kWh/m²
        assert interval_kwh_m2_from_wm2(600.0, 10) == 0.1

    def test_zero_radiance(self):
        assert interval_kwh_m2_from_wm2(0.0, 10) == 0.0

    def test_negative_radiance(self):
        assert interval_kwh_m2_from_wm2(-100.0, 10) == 0.0

    def test_zero_interval(self):
        assert interval_kwh_m2_from_wm2(1000.0, 0) == 0.0

    def test_negative_interval(self):
        assert interval_kwh_m2_from_wm2(1000.0, -10) == 0.0


# ===================================================================
# HTTP client (mocked)
# ===================================================================


@pytest.fixture
def client():
    creds = GrowattWebSession(username="u", password="p")
    c = GrowattIrradianceClient(creds)
    c._logged_in = True
    return c


class TestEnvDeviceDiscovery:
    def test_picks_first_device(self, client):
        with patch.object(
            client, "_post",
            return_value=load_fixture("meteo", "growatt_env_list.json"),
        ):
            result = client.get_env_device("9275498")
        assert result == ("DYD1EZR007", 32)

    def test_prefer_specific_sn(self, client):
        with patch.object(
            client, "_post",
            return_value=load_fixture("meteo", "growatt_env_list.json"),
        ):
            result = client.get_env_device("9275498", prefer_sn="DYD0E8501G")
        assert result == ("DYD0E8501G", 1)

    def test_prefer_sn_with_addr(self, client):
        with patch.object(
            client, "_post",
            return_value=load_fixture("meteo", "growatt_env_list.json"),
        ):
            result = client.get_env_device(
                "9275498", prefer_sn="DYD1EZR007", prefer_addr=32
            )
        assert result == ("DYD1EZR007", 32)

    def test_prefer_sn_not_found_falls_back_to_first(self, client):
        with patch.object(
            client, "_post",
            return_value=load_fixture("meteo", "growatt_env_list.json"),
        ):
            result = client.get_env_device("9275498", prefer_sn="NONEXISTENT")
        # Falls back to first available
        assert result == ("DYD1EZR007", 32)

    def test_no_devices_returns_none(self, client):
        with patch.object(client, "_post", return_value={"datas": []}):
            assert client.get_env_device("9275498") is None

    def test_caches_result_per_plant(self, client):
        with patch.object(
            client, "_post",
            return_value=load_fixture("meteo", "growatt_env_list.json"),
        ) as mock:
            client.get_env_device("9275498")
            client.get_env_device("9275498")  # second call should hit cache
        assert mock.call_count == 1


class TestFetchDailyIrradiance:
    def test_full_round_trip(self, client):
        # Two calls: env list, then env history
        responses = iter([
            load_fixture("meteo", "growatt_env_list.json"),
            load_fixture("meteo", "growatt_env_history.json"),
        ])
        with patch.object(client, "_post", side_effect=lambda p, d: next(responses)):
            result = client.fetch_daily_irradiance_kwh_m2("9275498", "2026-04-15")
        assert result == 6.235

    def test_no_env_device_returns_zero(self, client):
        with patch.object(client, "_post", return_value={"datas": []}):
            result = client.fetch_daily_irradiance_kwh_m2("9275498", "2026-04-15")
        assert result == 0.0

    def test_no_history_data_returns_zero(self, client):
        responses = iter([
            load_fixture("meteo", "growatt_env_list.json"),
            {"obj": {"datas": [], "haveNext": False}},
        ])
        with patch.object(client, "_post", side_effect=lambda p, d: next(responses)):
            result = client.fetch_daily_irradiance_kwh_m2("9275498", "2026-04-15")
        assert result == 0.0

    def test_caches_result(self, client):
        # Pre-populate device cache to focus on result caching
        client._env_device_cache["9275498"] = ("DYD1EZR007", 32)
        with patch.object(
            client, "_post",
            return_value=load_fixture("meteo", "growatt_env_history.json"),
        ) as mock:
            client.fetch_daily_irradiance_kwh_m2("9275498", "2026-04-15")
            client.fetch_daily_irradiance_kwh_m2("9275498", "2026-04-15")
        # Second call should hit cache, not re-fetch
        assert mock.call_count == 1


class TestFetchCurrentIrradiance:
    def test_returns_latest_radiance(self, client):
        responses = iter([
            load_fixture("meteo", "growatt_env_list.json"),
            load_fixture("meteo", "growatt_env_history.json"),
        ])
        with patch.object(client, "_post", side_effect=lambda p, d: next(responses)):
            result = client.fetch_current_irradiance_wm2("9275498", "2026-04-15")
        assert result == 20.0  # latest reading at 18:00 in fixture

    def test_does_not_cache(self, client):
        # Even calling twice should re-fetch (10-min cron wants fresh data)
        client._env_device_cache["9275498"] = ("DYD1EZR007", 32)
        with patch.object(
            client, "_post",
            return_value=load_fixture("meteo", "growatt_env_history.json"),
        ) as mock:
            client.fetch_current_irradiance_wm2("9275498", "2026-04-15")
            client.fetch_current_irradiance_wm2("9275498", "2026-04-15")
        # Two history fetches expected (no caching of current readings)
        assert mock.call_count == 2

    def test_no_device_returns_none(self, client):
        with patch.object(client, "_post", return_value={"datas": []}):
            result = client.fetch_current_irradiance_wm2("9275498", "2026-04-15")
        assert result is None


class TestPagination:
    def test_stops_when_haveNext_false(self, client):
        client._env_device_cache["9275498"] = ("SN1", 1)
        with patch.object(
            client, "_post",
            return_value=load_fixture("meteo", "growatt_env_history.json"),
        ) as mock:
            client.fetch_env_history_rows("9275498", "SN1", 1, "2026-04-15")
        assert mock.call_count == 1  # haveNext=False in fixture, single page

    def test_paginates_when_haveNext_true(self, client):
        # First page haveNext=true, second haveNext=false
        page1 = {
            "obj": {
                "haveNext": True,
                "datas": [
                    {
                        "calendar": {
                            "year": 2026, "month": 3, "dayOfMonth": 15,
                            "hourOfDay": 7, "minute": 0, "second": 0,
                        },
                        "radiant": 50.0,
                    },
                ],
            }
        }
        page2 = {
            "obj": {
                "haveNext": False,
                "datas": [
                    {
                        "calendar": {
                            "year": 2026, "month": 3, "dayOfMonth": 15,
                            "hourOfDay": 12, "minute": 0, "second": 0,
                        },
                        "radiant": 800.0,
                    },
                ],
            }
        }
        responses = iter([page1, page2])
        client._page_sleep = 0  # speed up test
        with patch.object(client, "_post", side_effect=lambda p, d: next(responses)):
            rows = client.fetch_env_history_rows("9275498", "SN1", 1, "2026-04-15")
        assert len(rows) == 2

    def test_respects_max_pages(self, client):
        client._max_pages = 3
        client._page_sleep = 0
        # Always say "haveNext" so we'd loop forever without the cap
        always_more = {
            "obj": {
                "haveNext": True,
                "datas": [
                    {
                        "calendar": {
                            "year": 2026, "month": 3, "dayOfMonth": 15,
                            "hourOfDay": 12, "minute": 0, "second": 0,
                        },
                        "radiant": 500.0,
                    },
                ],
            }
        }
        with patch.object(client, "_post", return_value=always_more) as mock:
            client.fetch_env_history_rows("9275498", "SN1", 1, "2026-04-15")
        assert mock.call_count == 3  # capped at max_pages
