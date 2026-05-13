"""Tests for argia.vendors.growatt (the dual-strategy facade).

Stage 2 change (2026-05-13): the web UI tests have been rewritten. The
old HTML-scraping and multi-endpoint device-list code is gone, replaced
by integration with argia.vendors.growatt_web. The Open API tests are
unchanged — that path was not touched in Stage 2.

Test layout:
  TestConstructor                    -- ctor validation, brand label
  TestOpenApiDayKwh                  -- Open API plant/data
  TestOpenApiAuthErrorTriggersHttp   -- 401/403/500 → typed exceptions
  TestOpenApiInverters               -- Open API per-inverter pipeline
  TestOpenApiInverterParsing         -- pure parser of inverter list+detail
  TestFallback                       -- Open API → web fallback control flow
  TestWebDayKwh                      -- Stage-2: getMAXTotalData wiring
  TestWebInverterSnapshots           -- Stage-2: getMAXHistory per-SN wiring
  TestWebClientLazyInit              -- web client built once, login idempotent
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from argia.core.config import InverterConfig, PlantConfig
from argia.core.time_utils import MX_TZ
from argia.vendors.base import InverterSnapshot
from argia.vendors.growatt import (
    GrowattAPIError,
    GrowattAuthError,
    GrowattClient,
)
from argia.vendors.growatt_web import (
    GrowattAPIError as WebAPIError,
    GrowattAuthError as WebAuthError,
)
from tests.conftest import load_fixture


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
        installation_date="2025-01-01",
        secret_api_name="",
        secret_user_name="",
        secret_pass_name="",
        active=True,
    )


@pytest.fixture
def taigene_plant() -> PlantConfig:
    """The actual plant the Stage 0 fixtures were captured from (TAIGENE)."""
    return PlantConfig(
        plant_key="GTO1",
        customer="TAIGENE",
        brand="GROWATT",
        site_id="9309575",
        kwp_dc=606.0,
        lat=21.1,
        lon=-101.75,
        weather_plant_id="9309575",
        datalogger_sn="",
        datalogger_addr=0,
        kwp_ac=0.0,
        expected_factor=0.8,
        pr_target=0.85,
        installation_date="2024-10-24",
        secret_api_name="",
        secret_user_name="",
        secret_pass_name="",
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
def taigene_inverters() -> list:
    """The 4 SNs the Stage 0 fixtures cover."""
    return [
        InverterConfig("GTO1", "JFM7DXN00T", "Inverter 1", 152.0, True),
        InverterConfig("GTO1", "JFM7DXN00U", "Inverter 2", 152.0, True),
        InverterConfig("GTO1", "JFM5D8900B", "Inverter 3", 152.0, True),
        InverterConfig("GTO1", "JFMCE9D014", "Inverter 4", 152.0, True),
    ]


@pytest.fixture
def open_api_client():
    return GrowattClient(api_token="fake-token")


@pytest.fixture
def web_only_client():
    c = GrowattClient(web_username="user", web_password="pass")
    return c


@pytest.fixture
def dual_client():
    c = GrowattClient(
        api_token="fake-token", web_username="user", web_password="pass"
    )
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

    def test_error_code_returns_none_without_fallback(self, open_api_client, plant):
        with patch.object(
            open_api_client,
            "_open_api_get",
            return_value=load_fixture("growatt", "openapi_auth_error.json"),
        ):
            # No web creds set, so auth/api error → None (no exception bubbles)
            result = open_api_client.fetch_day_kwh(plant, "2026-04-15")
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
            with patch("argia.vendors.growatt.time.sleep"):  # skip polite delay
                snaps = open_api_client.fetch_inverter_snapshots(plant, single_inverter)

        assert len(snaps) == 1
        assert snaps[0].inverter_sn == "BNE7CGV0AB"


class TestOpenApiInverterParsing:
    """Pure parser tests — independent of network."""

    def test_offline_status_from_list(self):
        list_item = {"sn": "ABC123", "status": 3}
        snap = GrowattClient._parse_open_api_inverter(list_item, {}, "MEX1")
        assert snap is not None
        assert snap.status == 3

    def test_missing_sn_returns_none(self):
        snap = GrowattClient._parse_open_api_inverter({}, {}, "MEX1")
        assert snap is None

    def test_sn_normalized(self):
        list_item = {"sn": "  bne 7cgv 0ab  "}
        snap = GrowattClient._parse_open_api_inverter(list_item, {}, "MEX1")
        assert snap is not None
        assert snap.inverter_sn == "BNE7CGV0AB"


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


# ----------------- Stage 2: Web UI day kWh via getMAXTotalData -----------------


def _today_mx_iso() -> str:
    """The MX-local "today" the facade compares against."""
    return dt.datetime.now(MX_TZ).strftime("%Y-%m-%d")


class TestWebDayKwh:
    """The new web path: getMAXTotalData → parse_max_total_data → etoday."""

    def test_returns_etoday_from_fixture(self, web_only_client, taigene_plant):
        fixture = load_fixture("growatt_web", "GTO1_getMAXTotalData.json")
        with patch.object(web_only_client, "_get_web_client") as mock_get:
            mock_web = MagicMock()
            mock_web.get_max_total_data.return_value = fixture
            mock_get.return_value = mock_web

            result = web_only_client.fetch_day_kwh(taigene_plant, _today_mx_iso())

        # The real fixture has eToday="786.8" — string in JSON, coerced to float
        assert result == 786.8
        mock_web.get_max_total_data.assert_called_once_with("9309575")

    def test_non_today_returns_none_without_calling_web(
        self, web_only_client, taigene_plant
    ):
        """Web endpoint only knows 'today'; ask for any other day → skip."""
        with patch.object(web_only_client, "_get_web_client") as mock_get:
            result = web_only_client.fetch_day_kwh(taigene_plant, "2020-01-01")
        assert result is None
        mock_get.assert_not_called()

    def test_web_auth_error_returns_none(self, web_only_client, taigene_plant):
        with patch.object(
            web_only_client, "_get_web_client",
            side_effect=WebAuthError("bad creds"),
        ):
            result = web_only_client.fetch_day_kwh(taigene_plant, _today_mx_iso())
        assert result is None

    def test_web_api_error_returns_none(self, web_only_client, taigene_plant):
        with patch.object(web_only_client, "_get_web_client") as mock_get:
            mock_web = MagicMock()
            mock_web.get_max_total_data.side_effect = WebAPIError("HTTP 500")
            mock_get.return_value = mock_web

            result = web_only_client.fetch_day_kwh(taigene_plant, _today_mx_iso())
        assert result is None

    def test_parser_returns_none_yields_none(self, web_only_client, taigene_plant):
        """If the fixture is a result=0 envelope, parser returns None → we return None."""
        empty_envelope = {"result": 0, "obj": None, "msg": ""}
        with patch.object(web_only_client, "_get_web_client") as mock_get:
            mock_web = MagicMock()
            mock_web.get_max_total_data.return_value = empty_envelope
            mock_get.return_value = mock_web

            result = web_only_client.fetch_day_kwh(taigene_plant, _today_mx_iso())
        assert result is None


# ----------------- Stage 2: Web UI inverter snapshots via getMAXHistory ---


class TestWebInverterSnapshots:
    """The new per-inverter path: getMAXHistory per SN → latest row → snapshot."""

    def test_builds_snapshots_for_each_sn(
        self, web_only_client, taigene_plant, taigene_inverters
    ):
        history_fixture = load_fixture(
            "growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json"
        )
        # Use only one inverter so we only need one fixture
        single = [taigene_inverters[0]]

        with patch.object(web_only_client, "_get_web_client") as mock_get:
            mock_web = MagicMock()
            mock_web.get_max_history.return_value = history_fixture
            mock_get.return_value = mock_web

            with patch("argia.vendors.growatt.time.sleep"):
                snaps = web_only_client.fetch_inverter_snapshots(
                    taigene_plant, single
                )

        assert len(snaps) == 1
        assert isinstance(snaps[0], InverterSnapshot)
        assert snaps[0].plant_key == "GTO1"
        assert snaps[0].inverter_sn == "JFM7DXN00T"
        # Latest row has real power data
        assert snaps[0].power_w is not None
        assert snaps[0].etoday_kwh is not None
        assert snaps[0].timestamp_utc.tzinfo is not None  # UTC-aware

    def test_calls_get_max_history_per_sn(
        self, web_only_client, taigene_plant, taigene_inverters
    ):
        history_fixture = load_fixture(
            "growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json"
        )
        # 4 inverters → 4 calls
        with patch.object(web_only_client, "_get_web_client") as mock_get:
            mock_web = MagicMock()
            mock_web.get_max_history.return_value = history_fixture
            mock_get.return_value = mock_web

            with patch("argia.vendors.growatt.time.sleep"):
                snaps = web_only_client.fetch_inverter_snapshots(
                    taigene_plant, taigene_inverters
                )

        assert mock_web.get_max_history.call_count == 4
        # Each call should have used today's MX-local date and start=0
        today_local = _today_mx_iso()
        for call in mock_web.get_max_history.call_args_list:
            assert call.kwargs.get("start", 0) == 0 or (
                len(call.args) >= 3 and call.args[2] == 0
            )
            # Date is passed positionally as the 2nd arg
            args = list(call.args) + [call.kwargs.get("date_iso")]
            assert today_local in args

    def test_empty_inverter_list_returns_empty(self, web_only_client, taigene_plant):
        """No inverters asked for → no web calls, empty result."""
        with patch.object(web_only_client, "_get_web_client") as mock_get:
            result = web_only_client.fetch_inverter_snapshots(taigene_plant, [])
        assert result == []
        mock_get.assert_not_called()

    def test_auth_failure_returns_empty(
        self, web_only_client, taigene_plant, taigene_inverters
    ):
        with patch.object(
            web_only_client, "_get_web_client",
            side_effect=WebAuthError("bad creds"),
        ):
            result = web_only_client.fetch_inverter_snapshots(
                taigene_plant, taigene_inverters
            )
        assert result == []

    def test_api_error_on_one_sn_continues_with_others(
        self, web_only_client, taigene_plant, taigene_inverters
    ):
        """One failed SN should not stop the loop — collect what we can."""
        history_fixture = load_fixture(
            "growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json"
        )

        def side_effect(sn, date_iso, start=0):
            if sn == "JFM5D8900B":  # 3rd inverter raises
                raise WebAPIError("HTTP 500")
            return history_fixture

        with patch.object(web_only_client, "_get_web_client") as mock_get:
            mock_web = MagicMock()
            mock_web.get_max_history.side_effect = side_effect
            mock_get.return_value = mock_web

            with patch("argia.vendors.growatt.time.sleep"):
                snaps = web_only_client.fetch_inverter_snapshots(
                    taigene_plant, taigene_inverters
                )

        # 3 of 4 succeeded
        assert len(snaps) == 3
        sns = {s.inverter_sn for s in snaps}
        assert "JFM5D8900B" not in sns

    def test_auth_loss_mid_loop_aborts_remaining(
        self, web_only_client, taigene_plant, taigene_inverters
    ):
        """Auth loss is terminal — no point hammering further."""
        history_fixture = load_fixture(
            "growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json"
        )

        call_count = {"n": 0}

        def side_effect(sn, date_iso, start=0):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return history_fixture
            raise WebAuthError("session expired")

        with patch.object(web_only_client, "_get_web_client") as mock_get:
            mock_web = MagicMock()
            mock_web.get_max_history.side_effect = side_effect
            mock_get.return_value = mock_web

            with patch("argia.vendors.growatt.time.sleep"):
                snaps = web_only_client.fetch_inverter_snapshots(
                    taigene_plant, taigene_inverters
                )

        # First inverter succeeded, 2nd raised → loop exited
        assert len(snaps) == 1
        # Don't bother calling 3rd and 4th
        assert call_count["n"] == 2


class TestWebClientLazyInit:
    """The web client is built once per facade and login is idempotent."""

    def test_web_client_built_lazily(self, web_only_client):
        """No web client until first web call."""
        assert web_only_client._web_client is None

    def test_web_client_not_built_for_open_api_only_calls(
        self, open_api_client, plant
    ):
        with patch.object(
            open_api_client,
            "_open_api_get",
            return_value={"data": {"today_energy": 100.0}, "error_code": 0},
        ):
            open_api_client.fetch_day_kwh(plant, "2026-04-15")
        # The Open API path never touches the web client
        assert open_api_client._web_client is None

    def test_web_client_cached_across_calls(self, web_only_client, taigene_plant):
        """Multiple fetch calls share one web client instance."""
        fixture = load_fixture("growatt_web", "GTO1_getMAXTotalData.json")

        # We patch the GrowattWebClient class so each new() returns a tracked mock
        from argia.vendors import growatt as growatt_module

        with patch.object(growatt_module, "GrowattWebClient") as mock_cls:
            instance = MagicMock()
            instance.get_max_total_data.return_value = fixture
            mock_cls.return_value = instance

            web_only_client.fetch_day_kwh(taigene_plant, _today_mx_iso())
            web_only_client.fetch_day_kwh(taigene_plant, _today_mx_iso())

        # Class instantiated exactly once across 2 fetch calls
        assert mock_cls.call_count == 1
        # login() called on each fetch (idempotent inside)
        assert instance.login.call_count == 2
