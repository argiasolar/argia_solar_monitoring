"""Tests for argia.vendors.growatt."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from argia.core.config import InverterConfig, PlantConfig
from argia.vendors.base import InverterSnapshot
from argia.vendors.growatt import (
    GrowattAPIError,
    GrowattAuthError,
    GrowattClient,
)
from tests.conftest import load_fixture, FIXTURES_DIR


# ----------------- helpers -----------------


@pytest.fixture
def plant() -> PlantConfig:
    return PlantConfig(
        plant_key="MEX1",
        customer="SAG-MEXICO",
        brand="GROWATT",
        site_id="9275498",
        kwp_dc=598.0,
        lat=19.4326,
        lon=-99.1332,
        weather_plant_id="9275498",
        datalogger_sn="DYD0E8501G",
        datalogger_addr=1,
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
        InverterConfig("MEX1", "BNE7CGV0AB", "Inverter 1", 250.0, True),
        InverterConfig("MEX1", "BNE7CGV0CD", "Inverter 2", 250.0, True),
        InverterConfig("MEX1", "BNE7CGV0EF", "Inverter 3", 250.0, True),
    ]


@pytest.fixture
def open_api_client():
    return GrowattClient(api_token="fake-token")


@pytest.fixture
def web_only_client():
    c = GrowattClient(web_username="user", web_password="pass")
    c._web_logged_in = True  # bypass real login in tests
    return c


@pytest.fixture
def dual_client():
    c = GrowattClient(
        api_token="fake-token", web_username="user", web_password="pass"
    )
    c._web_logged_in = True
    return c


# ----------------- constructor -----------------


class TestConstructor:
    def test_requires_some_credentials(self):
        with pytest.raises(ValueError, match="api_token OR"):
            GrowattClient()

    def test_partial_web_creds_invalid(self):
        with pytest.raises(ValueError):
            GrowattClient(web_username="u")  # missing password

    def test_open_api_only_ok(self):
        GrowattClient(api_token="t")  # no exception

    def test_web_only_ok(self):
        GrowattClient(web_username="u", web_password="p")

    def test_brand_label(self, open_api_client):
        assert open_api_client.brand == "GROWATT"


# ----------------- Open API: day kWh -----------------


class TestOpenApiDayKwh:
    def test_returns_today_energy(self, open_api_client, plant):
        with patch.object(
            open_api_client,
            "_open_api_get",
            return_value=load_fixture("growatt", "openapi_plant_data_success.json"),
        ):
            assert open_api_client.fetch_day_kwh(plant, "2026-04-15") == 1245.5

    def test_calls_correct_endpoint(self, open_api_client, plant):
        with patch.object(
            open_api_client,
            "_open_api_get",
            return_value=load_fixture("growatt", "openapi_plant_data_success.json"),
        ) as mock:
            open_api_client.fetch_day_kwh(plant, "2026-04-15")
            mock.assert_called_with("/v1/plant/data", {"plant_id": "9275498"})

    def test_error_code_raises(self, open_api_client, plant):
        with patch.object(
            open_api_client,
            "_open_api_get",
            return_value=load_fixture("growatt", "openapi_auth_error.json"),
        ):
            # error_code is non-zero → should raise; with no web fallback,
            # the auth/api error bubbles up as None (web fallback unavailable)
            result = open_api_client.fetch_day_kwh(plant, "2026-04-15")
            # No web creds set, so auth/api error → None
            assert result is None

    def test_empty_data_returns_none(self, open_api_client, plant):
        with patch.object(
            open_api_client, "_open_api_get", return_value={"data": {}, "error_code": 0}
        ):
            assert open_api_client.fetch_day_kwh(plant, "2026-04-15") is None

    def test_alternative_field_name(self, open_api_client, plant):
        # Some Growatt accounts return camelCase
        with patch.object(
            open_api_client,
            "_open_api_get",
            return_value={"data": {"todayEnergy": 999.9}, "error_code": 0},
        ):
            assert open_api_client.fetch_day_kwh(plant, "2026-04-15") == 999.9


class TestOpenApiAuthErrorTriggersHttpException:
    """Ensure 401/403 from the underlying HTTP raises GrowattAuthError."""

    def test_401_raises_auth_error(self, open_api_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "unauthorized"
        with patch.object(open_api_client._session, "get", return_value=mock_resp):
            with pytest.raises(GrowattAuthError):
                open_api_client._open_api_get("/v1/plant/data", {})

    def test_403_raises_auth_error(self, open_api_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "forbidden"
        with patch.object(open_api_client._session, "get", return_value=mock_resp):
            with pytest.raises(GrowattAuthError):
                open_api_client._open_api_get("/v1/plant/data", {})

    def test_500_raises_api_error(self, open_api_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "boom"
        with patch.object(open_api_client._session, "get", return_value=mock_resp):
            with pytest.raises(GrowattAPIError):
                open_api_client._open_api_get("/v1/plant/data", {})


# ----------------- Fallback behavior -----------------


class TestFallback:
    def test_open_api_auth_error_falls_back_to_web(self, dual_client, plant):
        """When Open API auth fails AND web creds are configured, fall back."""

        def open_api_raises(path, params):
            raise GrowattAuthError("token rejected")

        with patch.object(dual_client, "_open_api_get", side_effect=open_api_raises):
            with patch.object(
                dual_client, "_fetch_day_kwh_web", return_value=999.0
            ) as web_mock:
                result = dual_client.fetch_day_kwh(plant, "2026-04-15")
        assert result == 999.0
        web_mock.assert_called_once()

    def test_no_web_fallback_returns_none(self, open_api_client, plant):
        """When Open API fails and no web creds → None, not exception."""

        def open_api_raises(path, params):
            raise GrowattAuthError("token rejected")

        with patch.object(open_api_client, "_open_api_get", side_effect=open_api_raises):
            assert open_api_client.fetch_day_kwh(plant, "2026-04-15") is None

    def test_open_api_returns_none_does_not_fall_back(self, dual_client, plant):
        """If Open API succeeds with no data, don't waste calls on web UI."""
        with patch.object(
            dual_client, "_open_api_get", return_value={"data": {}, "error_code": 0}
        ):
            with patch.object(
                dual_client, "_fetch_day_kwh_web", return_value=999.0
            ) as web_mock:
                result = dual_client.fetch_day_kwh(plant, "2026-04-15")
        assert result is None
        web_mock.assert_not_called()


# ----------------- Open API: inverters -----------------


class TestOpenApiInverters:
    def test_fetches_only_wanted_sns(self, open_api_client, plant, inverters):
        # Only ask for 1 of the 3 inverters in the fixture
        single_inverter = [inverters[0]]

        def fake_get(path, params):
            if path == "/v1/device/inverter/all":
                return load_fixture("growatt", "openapi_inverter_all.json")
            if path == "/v1/device/inverter/data":
                return load_fixture("growatt", "openapi_inverter_detail.json")
            raise AssertionError(f"unexpected path {path}")

        with patch.object(open_api_client, "_open_api_get", side_effect=fake_get):
            with patch("argia.vendors.growatt.time.sleep"):  # skip the polite delay
                snaps = open_api_client.fetch_inverter_snapshots(plant, single_inverter)

        assert len(snaps) == 1
        assert snaps[0].inverter_sn == "BNE7CGV0AB"

    def test_returns_inverter_snapshots(self, open_api_client, plant, inverters):
        def fake_get(path, params):
            if path == "/v1/device/inverter/all":
                return load_fixture("growatt", "openapi_inverter_all.json")
            return load_fixture("growatt", "openapi_inverter_detail.json")

        with patch.object(open_api_client, "_open_api_get", side_effect=fake_get):
            with patch("argia.vendors.growatt.time.sleep"):
                snaps = open_api_client.fetch_inverter_snapshots(plant, inverters)

        assert len(snaps) == 3
        assert all(isinstance(s, InverterSnapshot) for s in snaps)

    def test_empty_inverter_list(self, open_api_client, plant):
        assert open_api_client.fetch_inverter_snapshots(plant, []) == []

    def test_sn_not_in_response_omitted(self, open_api_client, plant):
        # Asking for an SN that isn't in the inverter list
        unknown = [InverterConfig("MEX1", "DOES_NOT_EXIST", "X", 100.0, True)]

        def fake_get(path, params):
            return load_fixture("growatt", "openapi_inverter_all.json")

        with patch.object(open_api_client, "_open_api_get", side_effect=fake_get):
            snaps = open_api_client.fetch_inverter_snapshots(plant, unknown)

        assert snaps == []


class TestOpenApiInverterParsing:
    def test_kw_to_w_conversion(self):
        list_item = {"sn": "ABC", "status": 1}
        detail = {"pac": 125.5}  # kW
        snap = GrowattClient._parse_open_api_inverter(list_item, detail, "MEX1")
        assert snap is not None
        assert snap.power_w == 125500.0

    def test_already_in_watts(self):
        list_item = {"sn": "ABC", "status": 1}
        detail = {"pac": 125500.0}  # already W
        snap = GrowattClient._parse_open_api_inverter(list_item, detail, "MEX1")
        assert snap is not None
        assert snap.power_w == 125500.0

    def test_offline_status_3(self):
        list_item = {"sn": "ABC", "status": 3}
        detail = {}
        snap = GrowattClient._parse_open_api_inverter(list_item, detail, "MEX1")
        assert snap is not None
        assert snap.status == 3

    def test_missing_sn_returns_none(self):
        snap = GrowattClient._parse_open_api_inverter({}, {}, "MEX1")
        assert snap is None

    def test_sn_normalized(self):
        list_item = {"sn": "  bne 7cgv 0ab  "}
        detail = {}
        snap = GrowattClient._parse_open_api_inverter(list_item, detail, "MEX1")
        assert snap is not None
        assert snap.inverter_sn == "BNE7CGV0AB"


# ----------------- Web UI: HTML scraping -----------------


class TestPlantEtodayHtml:
    def test_parses_val_device_plantEToday(self):
        html = (FIXTURES_DIR / "growatt" / "web_pv_page.html").read_text()
        assert GrowattClient._parse_plant_etoday_html(html) == 1245.5

    def test_handles_extra_whitespace(self):
        html = '<span class="val_device_plantEToday">  999.5   </span>'
        assert GrowattClient._parse_plant_etoday_html(html) == 999.5

    def test_handles_single_quotes(self):
        html = "<span class='val_device_plantEToday'>500</span>"
        assert GrowattClient._parse_plant_etoday_html(html) == 500.0

    def test_returns_none_when_not_found(self):
        assert GrowattClient._parse_plant_etoday_html("<html>nothing here</html>") is None

    def test_empty_html(self):
        assert GrowattClient._parse_plant_etoday_html("") is None


class TestWebItemExtraction:
    def test_extracts_datas(self):
        text = '{"datas": [{"sn": "ABC"}, {"sn": "DEF"}]}'
        items = GrowattClient._extract_items_from_json(text)
        assert len(items) == 2

    def test_extracts_data_key(self):
        text = '{"data": [{"sn": "ABC"}]}'
        items = GrowattClient._extract_items_from_json(text)
        assert len(items) == 1

    def test_invalid_json_returns_empty(self):
        assert GrowattClient._extract_items_from_json("not json") == []

    def test_non_dict_returns_empty(self):
        assert GrowattClient._extract_items_from_json('["just a list"]') == []

    def test_skips_non_dict_items(self):
        text = '{"datas": [{"sn": "OK"}, "string", null]}'
        items = GrowattClient._extract_items_from_json(text)
        assert len(items) == 1


class TestWebInverterParsing:
    def test_parses_full_row(self):
        rows = load_fixture("growatt", "web_device_list.json")["datas"]
        snap = GrowattClient._parse_web_inverter(rows[0], "MEX1")
        assert snap is not None
        assert snap.inverter_sn == "BNE7CGV0AB"
        assert snap.etoday_kwh == 412.5
        assert snap.power_w == 125500.0  # kW → W

    def test_offline_status(self):
        rows = load_fixture("growatt", "web_device_list.json")["datas"]
        snap = GrowattClient._parse_web_inverter(rows[2], "MEX1")
        assert snap is not None
        assert snap.status == 3

    def test_alternative_sn_keys(self):
        for key in ("sn", "deviceSn", "invSn", "serialNum"):
            snap = GrowattClient._parse_web_inverter({key: "ABC"}, "MEX1")
            assert snap is not None
            assert snap.inverter_sn == "ABC"


# ----------------- Safety guards -----------------


class TestSafetyGuards:
    """v1 has safety guards against destructive endpoints. v2 keeps them."""

    def test_get_refuses_setmax(self, web_only_client):
        with pytest.raises(ValueError, match="unsafe"):
            web_only_client._web_get("/device/setmaxParams")

    def test_post_refuses_delete_endpoint(self, web_only_client):
        with pytest.raises(ValueError, match="unsafe"):
            web_only_client._web_post("/device/deleteInverter")

    def test_post_refuses_save_endpoint(self, web_only_client):
        with pytest.raises(ValueError, match="unsafe"):
            web_only_client._web_post("/device/saveSettings")

    def test_post_refuses_unsafe_prefix(self, web_only_client):
        with pytest.raises(ValueError, match="unsafe"):
            web_only_client._web_post("/commonDeviceSetC/setX")

    def test_safe_paths_allowed(self, web_only_client):
        """Sanity check — known-safe paths don't trigger the guard."""
        with patch.object(web_only_client._session, "get") as mock:
            mock.return_value = MagicMock(status_code=200, text="")
            web_only_client._web_get("/device/photovoltaic", params={"plantId": "x"})
        with patch.object(web_only_client._session, "post") as mock:
            mock.return_value = MagicMock(status_code=200, text="")
            web_only_client._web_post("/device/getMAXList", data={})
