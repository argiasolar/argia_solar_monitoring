"""Tests for env-station temperature extraction in
argia.meteo.growatt_irradiance (slice 1: source extraction only).

These cover the same env-history rows the irradiance path already reads, now
also pulling Environment Temp (-> ambient_temp_c) and Backplane Temp
(-> module_temp_c, the PR_STC input). The guards matter: a single garbage
reading must never reach the PR_STC correction.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from argia.meteo.growatt_irradiance import (
    GrowattIrradianceClient,
    GrowattWebSession,
    TEMP_MAX_C,
    TEMP_MIN_C,
    _clean_temp_c,
    extract_env_temp_points,
    find_latest_env_temps,
)


def _cal(hour: int, minute: int = 0):
    """Growatt calendar dict. Months are 0-based (Java legacy): June -> 5."""
    return {
        "year": 2026,
        "month": 5,  # June
        "dayOfMonth": 30,
        "hourOfDay": hour,
        "minute": minute,
        "second": 0,
    }


def _client() -> GrowattIrradianceClient:
    return GrowattIrradianceClient(GrowattWebSession(username="u", password="p"))


# ===================================================================
# Pure: _clean_temp_c
# ===================================================================


class TestCleanTempC:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (33.2, 33.2),
            (45, 45.0),
            ("31.7", 31.7),
            ("  29.2  ", 29.2),
            (0.0, 0.0),   # 0 degC is a real reading, not a null sentinel
            (-5.0, -5.0),
        ],
    )
    def test_parses_valid(self, value, expected):
        assert _clean_temp_c(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [None, "", "   ", "abc", "###"])
    def test_missing_or_unparseable_is_none(self, value):
        assert _clean_temp_c(value) is None

    @pytest.mark.parametrize("value", [TEMP_MIN_C - 0.1, TEMP_MAX_C + 0.1, 999, -999])
    def test_out_of_range_is_none(self, value):
        assert _clean_temp_c(value) is None

    @pytest.mark.parametrize("value", [TEMP_MIN_C, TEMP_MAX_C])
    def test_range_bounds_are_inclusive(self, value):
        assert _clean_temp_c(value) == pytest.approx(value)

    def test_nan_is_none(self):
        assert _clean_temp_c(float("nan")) is None


# ===================================================================
# Pure: extract_env_temp_points
# ===================================================================


class TestExtractEnvTempPoints:
    def test_extracts_envtemp(self):
        rows = [{"calendar": _cal(12), "envTemp": 30.0, "panelTemp": 40.0}]
        pts = extract_env_temp_points(rows, "envTemp")
        assert len(pts) == 1
        assert pts[0][1] == pytest.approx(30.0)

    def test_extracts_paneltemp(self):
        rows = [{"calendar": _cal(12), "envTemp": 30.0, "panelTemp": 40.0}]
        pts = extract_env_temp_points(rows, "panelTemp")
        assert len(pts) == 1
        assert pts[0][1] == pytest.approx(40.0)

    def test_skips_rows_missing_the_field(self):
        rows = [{"calendar": _cal(12), "envTemp": 30.0}]  # no panelTemp
        assert extract_env_temp_points(rows, "panelTemp") == []

    def test_skips_rows_without_calendar(self):
        rows = [{"envTemp": 30.0}]
        assert extract_env_temp_points(rows, "envTemp") == []

    def test_skips_rows_with_invalid_calendar(self):
        rows = [{"calendar": {"year": "nope"}, "envTemp": 30.0}]
        assert extract_env_temp_points(rows, "envTemp") == []

    def test_skips_garbage_reading(self):
        rows = [{"calendar": _cal(12), "panelTemp": 999}]  # out of range
        assert extract_env_temp_points(rows, "panelTemp") == []

    def test_empty_input(self):
        assert extract_env_temp_points([], "envTemp") == []
        assert extract_env_temp_points([{"junk": True}], "envTemp") == []


# ===================================================================
# Pure: find_latest_env_temps
# ===================================================================


class TestFindLatestEnvTemps:
    def test_real_shinemaster_sample(self):
        # Datalogger DYD0DXH00M (Plastic Omnium NL), 2026-06-30.
        # 13:00 is the latest row: Environment Temp 33.2, Backplane Temp 45.2.
        rows = [
            {"calendar": _cal(12, 55), "envTemp": 33.4, "panelTemp": 48.7},
            {"calendar": _cal(13, 0), "envTemp": 33.2, "panelTemp": 45.2},
        ]
        ambient, module = find_latest_env_temps(rows)
        assert ambient == pytest.approx(33.2)
        assert module == pytest.approx(45.2)

    def test_module_runs_hotter_than_ambient_under_load(self):
        rows = [{"calendar": _cal(13), "envTemp": 33.7, "panelTemp": 51.2}]
        ambient, module = find_latest_env_temps(rows)
        assert module > ambient

    def test_freshest_valid_per_field(self):
        # Latest row's panelTemp is garbage; ambient takes the latest, module
        # falls back to the last VALID backplane reading.
        rows = [
            {"calendar": _cal(12, 50), "envTemp": 33.7, "panelTemp": 51.2},
            {"calendar": _cal(13, 0), "envTemp": 33.2, "panelTemp": "###"},
        ]
        ambient, module = find_latest_env_temps(rows)
        assert ambient == pytest.approx(33.2)   # latest envTemp
        assert module == pytest.approx(51.2)    # last valid panelTemp (12:50)

    def test_both_none_when_no_temps(self):
        rows = [{"calendar": _cal(12), "radiant": 800.0}]
        assert find_latest_env_temps(rows) == (None, None)

    def test_partial_module_only(self):
        rows = [{"calendar": _cal(12), "panelTemp": 51.2}]
        ambient, module = find_latest_env_temps(rows)
        assert ambient is None
        assert module == pytest.approx(51.2)

    def test_empty_rows(self):
        assert find_latest_env_temps([]) == (None, None)


# ===================================================================
# Client: fetch_current_env_temps (HTTP layer mocked)
# ===================================================================


class TestFetchCurrentEnvTemps:
    def test_happy_path(self):
        client = _client()
        env_list = {"datas": [{"datalogSn": "DYD0DXH00M", "addr": 33}]}
        env_hist = {
            "obj": {
                "datas": [
                    {"calendar": _cal(12, 55), "envTemp": 33.4, "panelTemp": 48.7},
                    {"calendar": _cal(13, 0), "envTemp": 33.2, "panelTemp": 45.2},
                ],
                "haveNext": False,
            }
        }
        responses = iter([env_list, env_hist])
        with patch.object(client, "_post", side_effect=lambda p, d: next(responses)):
            ambient, module = client.fetch_current_env_temps("9275498", "2026-06-30")
        assert ambient == pytest.approx(33.2)
        assert module == pytest.approx(45.2)

    def test_no_env_device_returns_none_none(self):
        client = _client()
        with patch.object(client, "_post", return_value={"datas": []}):
            assert client.fetch_current_env_temps("9275498", "2026-06-30") == (None, None)

    def test_no_rows_returns_none_none(self):
        client = _client()
        env_list = {"datas": [{"datalogSn": "SN1", "addr": 1}]}
        env_hist = {"obj": {"datas": [], "haveNext": False}}
        responses = iter([env_list, env_hist])
        with patch.object(client, "_post", side_effect=lambda p, d: next(responses)):
            assert client.fetch_current_env_temps("9275498", "2026-06-30") == (None, None)
