"""Tests for argia.vendors.huawei."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from argia.core.config import InverterConfig, PlantConfig
from argia.vendors.base import InverterSnapshot
from argia.vendors.huawei import (
    HuaweiAPIError,
    HuaweiAuthError,
    HuaweiClient,
)
from tests.conftest import load_fixture


@pytest.fixture
def plant() -> PlantConfig:
    return PlantConfig(
        plant_key="MEX1",
        customer="SAG-MEXICO",
        brand="HUAWEI",
        site_id="NE=35314736",
        kwp_dc=598.0,
        lat=19.4326,
        lon=-99.1332,
        weather_plant_id="10069072",
        datalogger_sn="DYD1EZR007",
        datalogger_addr=32,
        kwp_ac=0.0,
        expected_factor=0.8,
        pr_target=0.85,
        installation_date='2025-01-01',
        secret_api_name='',
        secret_user_name='',
        secret_pass_name='',
        active=True,
    )


@pytest.fixture
def inverters() -> list:
    return [
        InverterConfig("MEX1", "ES2470051825", "Inverter 1", 250.0, True),
        InverterConfig("MEX1", "GR2499018270", "Inverter 2", 250.0, True),
        InverterConfig("MEX1", "GR2499018271", "Inverter 3", 250.0, True),
    ]


@pytest.fixture
def client() -> HuaweiClient:
    """Pre-authenticated client (skips real login)."""
    c = HuaweiClient("user", "pass")
    c._logged_in = True  # bypass login for tests
    return c


class TestConstructor:
    def test_requires_username(self):
        with pytest.raises(ValueError):
            HuaweiClient("", "pass")

    def test_requires_password(self):
        with pytest.raises(ValueError):
            HuaweiClient("user", "")

    def test_brand_is_huawei(self, client):
        assert client.brand == "HUAWEI"


class TestLogin:
    def test_login_success_sets_token(self):
        c = HuaweiClient("user", "pass")
        mock_session = MagicMock()
        mock_session.headers = {}  # behave like a real headers dict
        mock_response = MagicMock()
        mock_response.headers = {"XSRF-TOKEN": "abc123"}
        mock_response.cookies = {}
        mock_session.post.return_value = mock_response
        c._session = mock_session

        c.login()

        assert c._logged_in
        assert c._session.headers["XSRF-TOKEN"] == "abc123"

    def test_login_token_from_cookie(self):
        c = HuaweiClient("user", "pass")
        mock_session = MagicMock()
        mock_session.headers = {}
        mock_response = MagicMock()
        mock_response.headers = {}
        mock_response.cookies = {"XSRF-TOKEN": "from-cookie"}
        mock_session.post.return_value = mock_response
        c._session = mock_session

        c.login()

        assert c._session.headers["XSRF-TOKEN"] == "from-cookie"

    def test_login_failure_raises(self):
        c = HuaweiClient("user", "pass")
        mock_session = MagicMock()
        mock_session.headers = {}
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {}
        mock_response.cookies = {}
        mock_session.post.return_value = mock_response
        c._session = mock_session

        with pytest.raises(HuaweiAuthError):
            c.login()

        assert not c._logged_in


class TestFetchDayKwh:
    def test_success(self, client, plant):
        with patch.object(
            client,
            "_post_json",
            return_value=load_fixture("huawei", "getStationRealKpi_success.json"),
        ):
            result = client.fetch_day_kwh(plant, "2026-04-15")
            assert result == 480.5

    def test_failure_returns_none(self, client, plant):
        with patch.object(
            client,
            "_post_json",
            return_value=load_fixture("huawei", "getDevRealKpi_session_expired.json"),
        ):
            assert client.fetch_day_kwh(plant, "2026-04-15") is None

    def test_wrong_station_returns_none(self, client, plant):
        # Station code in response doesn't match plant.site_id
        wrong_response = {
            "success": True,
            "data": [{"stationCode": "NE=99999999", "dataItemMap": {"day_cap": 100}}],
        }
        with patch.object(client, "_post_json", return_value=wrong_response):
            assert client.fetch_day_kwh(plant, "2026-04-15") is None

    def test_empty_data_returns_none(self, client, plant):
        with patch.object(
            client, "_post_json", return_value={"success": True, "data": []}
        ):
            assert client.fetch_day_kwh(plant, "2026-04-15") is None

    def test_calls_correct_endpoint(self, client, plant):
        with patch.object(
            client, "_post_json", return_value={"success": True, "data": []}
        ) as mock:
            client.fetch_day_kwh(plant, "2026-04-15")
            mock.assert_called_once_with(
                "/getStationRealKpi", {"stationCodes": "NE=35314736"}
            )


class TestFetchInverterSnapshots:
    def test_success_three_inverters(self, client, plant, inverters):
        with patch.object(
            client,
            "_post_json",
            return_value=load_fixture("huawei", "getDevRealKpi_three_inverters.json"),
        ):
            snaps = client.fetch_inverter_snapshots(plant, inverters)

        assert len(snaps) == 3
        sns = {s.inverter_sn for s in snaps}
        assert sns == {"ES2470051825", "GR2499018270", "GR2499018271"}

    def test_returns_inverter_snapshot_dataclass(self, client, plant, inverters):
        with patch.object(
            client,
            "_post_json",
            return_value=load_fixture("huawei", "getDevRealKpi_three_inverters.json"),
        ):
            snaps = client.fetch_inverter_snapshots(plant, inverters)

        assert all(isinstance(s, InverterSnapshot) for s in snaps)

    def test_kwh_value_extracted(self, client, plant, inverters):
        with patch.object(
            client,
            "_post_json",
            return_value=load_fixture("huawei", "getDevRealKpi_three_inverters.json"),
        ):
            snaps = client.fetch_inverter_snapshots(plant, inverters)

        by_sn = {s.inverter_sn: s for s in snaps}
        assert by_sn["ES2470051825"].etoday_kwh == 120.5
        assert by_sn["GR2499018270"].etoday_kwh == 125.8

    def test_offline_inverter_status_3(self, client, plant, inverters):
        with patch.object(
            client,
            "_post_json",
            return_value=load_fixture("huawei", "getDevRealKpi_three_inverters.json"),
        ):
            snaps = client.fetch_inverter_snapshots(plant, inverters)

        by_sn = {s.inverter_sn: s for s in snaps}
        assert by_sn["GR2499018271"].status == 3
        assert by_sn["ES2470051825"].status == 1

    def test_power_kw_to_w_conversion(self, client, plant, inverters):
        # API returns 45.5 (kW); we should normalize to 45500 W
        with patch.object(
            client,
            "_post_json",
            return_value=load_fixture("huawei", "getDevRealKpi_three_inverters.json"),
        ):
            snaps = client.fetch_inverter_snapshots(plant, inverters)

        by_sn = {s.inverter_sn: s for s in snaps}
        assert by_sn["ES2470051825"].power_w == 45500.0

    def test_empty_inverter_list_returns_empty(self, client, plant):
        result = client.fetch_inverter_snapshots(plant, [])
        assert result == []

    def test_session_expired_raises(self, client, plant, inverters):
        with patch.object(
            client,
            "_post_json",
            return_value=load_fixture("huawei", "getDevRealKpi_session_expired.json"),
        ):
            with pytest.raises(HuaweiAPIError):
                client.fetch_inverter_snapshots(plant, inverters)

    def test_batches_large_inverter_lists(self, client, plant):
        # 60 inverters = 2 batches at SN_BATCH_SIZE=50
        many = [
            InverterConfig("MEX1", f"INV{i:03d}", f"Inverter {i}", 100.0, True)
            for i in range(60)
        ]
        with patch.object(
            client, "_post_json", return_value={"success": True, "data": []}
        ) as mock:
            client.fetch_inverter_snapshots(plant, many)
            assert mock.call_count == 2


class TestParseKpiItem:
    def test_returns_none_for_missing_sn(self):
        item = {"dataItemMap": {"day_cap": 100}}
        assert HuaweiClient._parse_kpi_item(item, "MEX1") is None

    def test_returns_none_for_non_dict(self):
        assert HuaweiClient._parse_kpi_item("string", "MEX1") is None  # type: ignore[arg-type]
        assert HuaweiClient._parse_kpi_item(None, "MEX1") is None  # type: ignore[arg-type]

    def test_normalizes_sn_uppercase(self):
        item = {"sn": "es2470051825", "dataItemMap": {}}
        snap = HuaweiClient._parse_kpi_item(item, "MEX1")
        assert snap is not None
        assert snap.inverter_sn == "ES2470051825"

    def test_alternative_sn_keys(self):
        for key in ("sn", "devSn", "deviceSn", "serialNum", "esn"):
            item = {key: "ABC123", "dataItemMap": {}}
            snap = HuaweiClient._parse_kpi_item(item, "MEX1")
            assert snap is not None
            assert snap.inverter_sn == "ABC123"

    def test_power_already_in_watts(self):
        # Value > 1000 means already in W
        item = {
            "sn": "ABC",
            "dataItemMap": {"active_power": 45500.0},
        }
        snap = HuaweiClient._parse_kpi_item(item, "MEX1")
        assert snap is not None
        assert snap.power_w == 45500.0

    def test_missing_data_item_map(self):
        item = {"sn": "ABC", "devStatus": "1"}
        snap = HuaweiClient._parse_kpi_item(item, "MEX1")
        assert snap is not None
        assert snap.power_w is None
        assert snap.etoday_kwh is None

    def test_timestamp_from_collect_time_ms(self):
        item = {
            "sn": "ABC",
            "collectTime": 1744744200000,  # ms
            "dataItemMap": {},
        }
        snap = HuaweiClient._parse_kpi_item(item, "MEX1")
        assert snap is not None
        assert snap.timestamp_utc.year == 2025
        assert snap.timestamp_utc.tzinfo is not None

    def test_timestamp_falls_back_to_now_when_missing(self):
        item = {"sn": "ABC", "dataItemMap": {}}
        snap = HuaweiClient._parse_kpi_item(item, "MEX1")
        assert snap is not None
        assert snap.timestamp_utc.tzinfo is not None  # always tz-aware
