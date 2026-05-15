"""Tests for the _is_real_inverter helper used by sma_capture.py and
sma_discover_plants.py.

Test fixtures are taken VERBATIM from a real captured live_devices_13.json
to keep these tests aligned with what SMA actually returns. If SMA changes
the response shape, these tests will be the first to fail."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Two copies of the same helper — they MUST stay in sync. Test both.
from sma_capture import _is_real_inverter as _is_real_inverter_capture
from sma_discover_plants import _is_real_inverter as _is_real_inverter_discover


# ============================================================
# Real captured device fixtures (from plant 13)
# ============================================================

SENSOR = {
    "deviceId": "14",
    "name": "Satellit Sensor",
    "type": "Sensor technology",
    "product": "Satellite Sensor",
    "isActive": True,
}

DATALOGGER = {
    "deviceId": "15",
    "name": "MyTestLdm1",
    "type": "Monitoring and control",
    "product": "EDMM-10",
    "serial": "234534635778",
    "isActive": True,
}

REAL_INVERTER = {
    "deviceId": "16",
    "name": "My Inverter 1",
    "type": "Solar Inverters",
    "product": "STP 6000TL-20",
    "serial": "3421111",
    "generatorPower": 6000.0,
    "generatorPowerDc": 6000.0,
    "isActive": True,
}

REAL_INVERTER_5KW = {
    "deviceId": "17",
    "name": "My Inverter 2",
    "type": "Solar Inverters",
    "product": "STP 5000TL-20",
    "serial": "9687867",
    "generatorPower": 5000.0,
    "isActive": True,
}

BATTERY = {
    "deviceId": "19",
    "name": "My Battery 1",
    "type": "Battery Inverter",
    "product": "SBS6.0-10",
    "serial": "4562245",
    "isActive": True,
}

ENERGY_METER = {
    "deviceId": "20",
    "name": "My Energy Meter 1",
    "type": "Monitoring and control",
    "product": "Energy Meter",
    "serial": "567811567",
    "isActive": True,
}

# This is the tricky one — sandbox tags charging stations as "Solar Inverters"
# but they have no generatorPower. Must filter these out.
EV_CHARGER = {
    "deviceId": "23",
    "name": "My ChargingStation 1",
    "type": "Solar Inverters",
    "product": "STP50-41",
    "serial": "1334234534",
    "isActive": True,
}


# Run all tests against BOTH copies of the helper to keep them in sync
HELPERS = [_is_real_inverter_capture, _is_real_inverter_discover]


@pytest.mark.parametrize("is_real_inverter", HELPERS)
class TestRealInverter:
    def test_real_inverter_accepted(self, is_real_inverter):
        assert is_real_inverter(REAL_INVERTER) is True

    def test_real_inverter_smaller_accepted(self, is_real_inverter):
        assert is_real_inverter(REAL_INVERTER_5KW) is True


@pytest.mark.parametrize("is_real_inverter", HELPERS)
class TestNotInverter:
    def test_sensor_rejected(self, is_real_inverter):
        assert is_real_inverter(SENSOR) is False

    def test_datalogger_rejected(self, is_real_inverter):
        assert is_real_inverter(DATALOGGER) is False

    def test_battery_rejected(self, is_real_inverter):
        assert is_real_inverter(BATTERY) is False

    def test_energy_meter_rejected(self, is_real_inverter):
        assert is_real_inverter(ENERGY_METER) is False

    def test_ev_charger_rejected_despite_type(self, is_real_inverter):
        """Sandbox quirk: EV chargers have type='Solar Inverters' but no
        generatorPower. Must be filtered out."""
        assert is_real_inverter(EV_CHARGER) is False


@pytest.mark.parametrize("is_real_inverter", HELPERS)
class TestEdgeCases:
    def test_none_rejected(self, is_real_inverter):
        assert is_real_inverter(None) is False

    def test_non_dict_rejected(self, is_real_inverter):
        assert is_real_inverter("not a dict") is False
        assert is_real_inverter(123) is False
        assert is_real_inverter([1, 2, 3]) is False

    def test_empty_dict_rejected(self, is_real_inverter):
        assert is_real_inverter({}) is False

    def test_missing_type_rejected(self, is_real_inverter):
        assert is_real_inverter({"generatorPower": 5000.0}) is False

    def test_missing_generator_power_rejected(self, is_real_inverter):
        assert is_real_inverter({"type": "Solar Inverters"}) is False

    def test_zero_generator_power_rejected(self, is_real_inverter):
        """generatorPower=0 should still be rejected (defensive)."""
        assert is_real_inverter(
            {"type": "Solar Inverters", "generatorPower": 0}
        ) is False

    def test_negative_generator_power_rejected(self, is_real_inverter):
        assert is_real_inverter(
            {"type": "Solar Inverters", "generatorPower": -1000}
        ) is False

    def test_string_generator_power_handled(self, is_real_inverter):
        """SMA sometimes returns numbers as strings — handle gracefully."""
        assert is_real_inverter(
            {"type": "Solar Inverters", "generatorPower": "6000"}
        ) is True

    def test_garbage_generator_power_rejected(self, is_real_inverter):
        assert is_real_inverter(
            {"type": "Solar Inverters", "generatorPower": "garbage"}
        ) is False

    def test_wrong_type_string_rejected(self, is_real_inverter):
        """Type comparison is exact — 'solar inverters' (lowercase) is
        rejected. SMA always uses Title Case in the sandbox responses."""
        assert is_real_inverter(
            {"type": "solar inverters", "generatorPower": 5000}
        ) is False


@pytest.mark.parametrize("is_real_inverter", HELPERS)
class TestFromCapturedPlant:
    """Apply the filter to the full captured device list from plant 13 and
    verify exactly which devices are accepted."""

    ALL_DEVICES = [
        SENSOR, DATALOGGER, REAL_INVERTER, REAL_INVERTER_5KW,
        {"deviceId": "18", "name": "My Falcon 3", "type": "Solar Inverters",
         "product": "SB3.6-1AV-40", "serial": "463688",
         "generatorPower": 3600.0, "isActive": True},
        BATTERY, ENERGY_METER,
        {"deviceId": "21", "name": "My Gas Meter 1", "type": "Monitoring and control",
         "product": "Energy Meter", "serial": "89781197", "isActive": True},
        {"deviceId": "22", "name": "My Consumer 1", "type": "Monitoring and control",
         "product": "Remote Socket", "serial": "5411674564", "isActive": True},
        EV_CHARGER,
    ]

    def test_plant_13_has_exactly_three_real_inverters(self, is_real_inverter):
        accepted = [d for d in self.ALL_DEVICES if is_real_inverter(d)]
        assert len(accepted) == 3
        accepted_ids = {d["deviceId"] for d in accepted}
        # Inverters 16, 17, 18 — NOT 23 (charging station)
        assert accepted_ids == {"16", "17", "18"}
