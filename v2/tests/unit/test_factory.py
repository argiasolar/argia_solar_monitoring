"""Tests for argia.vendors.factory."""

from __future__ import annotations

import pytest

from argia.core.config import PlantConfig
from argia.vendors.factory import (
    VendorCredentialsMissing,
    build_client_for,
    build_clients_for_active_plants,
)
from argia.vendors.growatt import GrowattClient
from argia.vendors.huawei import HuaweiClient
from argia.vendors.solaredge import SolarEdgeClient


def _make_plant(**overrides) -> PlantConfig:
    """Helper to build a PlantConfig with sensible defaults for tests."""
    defaults = dict(
        plant_key="TEST1",
        customer="Test Customer",
        brand="GROWATT",
        site_id="123",
        kwp_dc=100.0,
        kwp_ac=80.0,
        lat=20.0,
        lon=-100.0,
        expected_factor=0.75,
        pr_target=0.85,
        installation_date="2025-01-01",
        secret_api_name="GROWATT_API_TOKEN",
        secret_user_name="",
        secret_pass_name="",
        weather_plant_id="123",
        datalogger_sn="DYDABC1234",
        datalogger_addr=1,
        active=True,
    )
    defaults.update(overrides)
    return PlantConfig(**defaults)


# ===================== Growatt =====================


class TestGrowatt:
    def test_growatt_with_api_token(self, monkeypatch):
        monkeypatch.setenv("GROWATT_API_TOKEN", "tok-abc-123")
        plant = _make_plant(brand="GROWATT", secret_api_name="GROWATT_API_TOKEN")
        client = build_client_for(plant)
        assert isinstance(client, GrowattClient)

    def test_growatt_falls_back_to_username_password(self, monkeypatch):
        # No API token, but classic web UI credentials exist
        monkeypatch.delenv("GROWATT_API_TOKEN", raising=False)
        monkeypatch.setenv("GROWATT_USERNAME", "user1")
        monkeypatch.setenv("GROWATT_PASSWORD", "pass1")
        plant = _make_plant(brand="GROWATT", secret_api_name="GROWATT_API_TOKEN")
        client = build_client_for(plant)
        assert isinstance(client, GrowattClient)

    def test_growatt_missing_all_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("GROWATT_API_TOKEN", raising=False)
        monkeypatch.delenv("GROWATT_USERNAME", raising=False)
        monkeypatch.delenv("GROWATT_PASSWORD", raising=False)
        plant = _make_plant(brand="GROWATT", secret_api_name="GROWATT_API_TOKEN")
        with pytest.raises(VendorCredentialsMissing):
            build_client_for(plant)

    def test_growatt_with_partial_web_credentials_raises(self, monkeypatch):
        # username set but password not — should fail (no token either)
        monkeypatch.delenv("GROWATT_API_TOKEN", raising=False)
        monkeypatch.setenv("GROWATT_USERNAME", "user1")
        monkeypatch.delenv("GROWATT_PASSWORD", raising=False)
        plant = _make_plant(brand="GROWATT", secret_api_name="GROWATT_API_TOKEN")
        with pytest.raises(VendorCredentialsMissing):
            build_client_for(plant)


# ===================== Huawei =====================


class TestHuawei:
    def test_huawei_with_credentials(self, monkeypatch):
        monkeypatch.setenv("HUAWEI_USERNAME", "huser")
        monkeypatch.setenv("HUAWEI_PASSWORD", "hpass")
        plant = _make_plant(
            brand="HUAWEI",
            site_id="NE=12345",
            secret_api_name="",
            secret_user_name="HUAWEI_USERNAME",
            secret_pass_name="HUAWEI_PASSWORD",
        )
        client = build_client_for(plant)
        assert isinstance(client, HuaweiClient)

    def test_huawei_missing_password_raises(self, monkeypatch):
        monkeypatch.setenv("HUAWEI_USERNAME", "huser")
        monkeypatch.delenv("HUAWEI_PASSWORD", raising=False)
        plant = _make_plant(
            brand="HUAWEI",
            site_id="NE=12345",
            secret_api_name="",
            secret_user_name="HUAWEI_USERNAME",
            secret_pass_name="HUAWEI_PASSWORD",
        )
        with pytest.raises(VendorCredentialsMissing):
            build_client_for(plant)

    def test_huawei_missing_username_raises(self, monkeypatch):
        monkeypatch.delenv("HUAWEI_USERNAME", raising=False)
        monkeypatch.setenv("HUAWEI_PASSWORD", "hpass")
        plant = _make_plant(
            brand="HUAWEI",
            secret_user_name="HUAWEI_USERNAME",
            secret_pass_name="HUAWEI_PASSWORD",
        )
        with pytest.raises(VendorCredentialsMissing):
            build_client_for(plant)


# ===================== SolarEdge =====================


class TestSolarEdge:
    def test_solaredge_with_key(self, monkeypatch):
        monkeypatch.setenv("SOLAREDGE_API_KEY", "se-key-abc")
        plant = _make_plant(
            brand="SOLAREDGE", secret_api_name="SOLAREDGE_API_KEY"
        )
        client = build_client_for(plant)
        assert isinstance(client, SolarEdgeClient)

    def test_solaredge_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("SOLAREDGE_API_KEY", raising=False)
        plant = _make_plant(
            brand="SOLAREDGE", secret_api_name="SOLAREDGE_API_KEY"
        )
        with pytest.raises(VendorCredentialsMissing):
            build_client_for(plant)


# ===================== Unknown brand / batch =====================


def test_unknown_brand_raises():
    plant = _make_plant(brand="UNKNOWN_BRAND")
    with pytest.raises(ValueError, match="Unknown brand"):
        build_client_for(plant)


def test_brand_is_case_insensitive(monkeypatch):
    """Lowercase brand from sheet should still work."""
    monkeypatch.setenv("GROWATT_API_TOKEN", "tok")
    plant = _make_plant(brand="growatt")
    client = build_client_for(plant)
    assert isinstance(client, GrowattClient)


class TestBuildClientsForActivePlants:
    def test_skips_inactive(self, monkeypatch):
        monkeypatch.setenv("GROWATT_API_TOKEN", "tok")
        plants = {
            "P1": _make_plant(plant_key="P1", active=True),
            "P2": _make_plant(plant_key="P2", active=False),
        }
        clients = build_clients_for_active_plants(plants)
        assert "P1" in clients
        assert "P2" not in clients

    def test_skips_plant_with_missing_credentials_but_continues(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("GROWATT_API_TOKEN", "tok")
        monkeypatch.delenv("HUAWEI_USERNAME", raising=False)
        monkeypatch.delenv("HUAWEI_PASSWORD", raising=False)
        plants = {
            "P1": _make_plant(plant_key="P1", brand="GROWATT"),
            "P2": _make_plant(
                plant_key="P2",
                brand="HUAWEI",
                secret_api_name="",
                secret_user_name="HUAWEI_USERNAME",
                secret_pass_name="HUAWEI_PASSWORD",
            ),
        }
        clients = build_clients_for_active_plants(plants)
        assert "P1" in clients
        assert "P2" not in clients  # missing creds → silently skipped

    def test_empty_portfolio(self):
        assert build_clients_for_active_plants({}) == {}
