"""Tests for argia.meteo.open_meteo."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from argia.meteo.open_meteo import (
    CloudCoverClient,
    compute_avg_cloudcover,
)
from tests.conftest import load_fixture


# ----------------- pure compute_avg_cloudcover -----------------


class TestComputeAvgCloudcover:
    def test_average_within_default_window(self):
        # Default daylight window is 7-19 (inclusive both ends).
        # Fixture cloudcover values for hours 7..19 (13 hours):
        #   50, 45, 40, 35, 30, 25, 20, 25, 30, 35, 40, 45, 50
        # sum = 470, mean = 470/13 ≈ 36.15 → rounded to 1 decimal = 36.2
        data = load_fixture("meteo", "open_meteo_archive_success.json")
        result = compute_avg_cloudcover(data, "2026-04-15")
        assert result == 36.2

    def test_custom_window(self):
        data = load_fixture("meteo", "open_meteo_archive_success.json")
        # Hour 12 only → cloudcover[12] = 25
        result = compute_avg_cloudcover(data, "2026-04-15", start_hour=12, end_hour=12)
        assert result == 25.0

    def test_empty_response_returns_none(self):
        data = load_fixture("meteo", "open_meteo_empty.json")
        assert compute_avg_cloudcover(data, "2026-04-15") is None

    def test_missing_hourly_key(self):
        assert compute_avg_cloudcover({}, "2026-04-15") is None

    def test_none_response(self):
        assert compute_avg_cloudcover(None, "2026-04-15") is None  # type: ignore[arg-type]

    def test_mismatched_array_lengths(self):
        bad = {"hourly": {"time": ["2026-04-15T12:00"], "cloudcover": []}}
        assert compute_avg_cloudcover(bad, "2026-04-15") is None

    def test_filters_by_date(self):
        # All rows but only those starting with "2026-04-15" should count
        mixed = {
            "hourly": {
                "time": [
                    "2026-04-14T12:00",  # different day
                    "2026-04-15T12:00",
                    "2026-04-15T13:00",
                    "2026-04-16T12:00",  # different day
                ],
                "cloudcover": [99, 50, 30, 99],
            }
        }
        # Only the two 2026-04-15 entries (50, 30 → mean 40)
        assert compute_avg_cloudcover(mixed, "2026-04-15", 12, 13) == 40.0

    def test_handles_string_numbers(self):
        # Defensive: API sometimes returns "50" instead of 50
        data = {
            "hourly": {
                "time": ["2026-04-15T12:00", "2026-04-15T13:00"],
                "cloudcover": ["40", "60"],
            }
        }
        assert compute_avg_cloudcover(data, "2026-04-15", 12, 13) == 50.0

    def test_rounds_to_one_decimal(self):
        data = {
            "hourly": {
                "time": ["2026-04-15T12:00", "2026-04-15T13:00", "2026-04-15T14:00"],
                "cloudcover": [10, 20, 31],  # mean = 20.333...
            }
        }
        assert compute_avg_cloudcover(data, "2026-04-15", 12, 14) == 20.3


# ----------------- HTTP client -----------------


class TestCloudCoverClient:
    def test_successful_archive_response(self):
        c = CloudCoverClient(retries=0)
        with patch.object(
            c, "_try_request",
            return_value=load_fixture("meteo", "open_meteo_archive_success.json"),
        ):
            assert c.fetch_avg_cloudcover_pct(19.4326, -99.1332, "2026-04-15") == 36.2

    def test_archive_empty_falls_back_to_forecast(self):
        c = CloudCoverClient(retries=0)
        archive_empty = load_fixture("meteo", "open_meteo_empty.json")
        forecast_data = load_fixture("meteo", "open_meteo_archive_success.json")

        responses = [archive_empty, forecast_data]

        def fake(_url, _params):
            return responses.pop(0)

        with patch.object(c, "_try_request", side_effect=fake):
            assert c.fetch_avg_cloudcover_pct(19.4326, -99.1332, "2026-04-15") == 36.2

    def test_both_apis_fail_returns_none(self):
        c = CloudCoverClient(retries=0)
        with patch.object(c, "_try_request", return_value=None):
            assert c.fetch_avg_cloudcover_pct(19.4326, -99.1332, "2026-04-15") is None

    def test_retry_eventually_succeeds(self):
        c = CloudCoverClient(retries=2)
        # Simulate network errors on first two attempts, success on third
        attempt_count = {"n": 0}

        def fake_get(*_args, **_kwargs):
            attempt_count["n"] += 1
            if attempt_count["n"] < 3:
                import requests as _r
                raise _r.RequestException("network glitch")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = load_fixture(
                "meteo", "open_meteo_archive_success.json"
            )
            return mock_resp

        with patch.object(c._session, "get", side_effect=fake_get):
            result = c.fetch_avg_cloudcover_pct(19.4326, -99.1332, "2026-04-15")
        assert result == 36.2

    def test_http_error_returns_none_eventually(self):
        c = CloudCoverClient(retries=0)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch.object(c._session, "get", return_value=mock_resp):
            assert c.fetch_avg_cloudcover_pct(19.4326, -99.1332, "2026-04-15") is None
