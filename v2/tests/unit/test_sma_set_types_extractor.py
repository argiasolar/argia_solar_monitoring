"""Tests for sma_capture._set_types_from_list.

The string-array shape comes from a real captured live_inverter_sets_16.json
in sandbox (May 2026). The dict-array shape is the documented form. Both
must work because we don't trust SMA's docs to match reality.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from sma_capture import _set_types_from_list


# Real captured response from sandbox inverter 16 (verbatim)
REAL_CAPTURED = {
    "plant": {
        "plantId": "13",
        "name": "Testplant 1",
        "timezone": "Europe/Berlin",
    },
    "device": {
        "deviceId": "16",
        "name": "My Inverter 1",
        "timezone": "Europe/Berlin",
    },
    "sets": ["Sensor", "EnergyAndPowerPv", "PowerDc", "PowerAc"],
}


class TestRealCaptured:
    def test_extracts_all_four_set_names(self):
        result = _set_types_from_list(REAL_CAPTURED)
        assert result == ["Sensor", "EnergyAndPowerPv", "PowerDc", "PowerAc"]


class TestStringArray:
    def test_simple_list(self):
        response = {"sets": ["A", "B", "C"]}
        assert _set_types_from_list(response) == ["A", "B", "C"]

    def test_empty_strings_filtered(self):
        response = {"sets": ["A", "", "B", "   "]}
        assert _set_types_from_list(response) == ["A", "B"]

    def test_strings_stripped(self):
        response = {"sets": ["  EnergyAndPowerPv  ", "PowerAc"]}
        assert _set_types_from_list(response) == ["EnergyAndPowerPv", "PowerAc"]


class TestDictArray:
    """Backward compat with the documented shape."""

    def test_setType_key(self):
        response = {"sets": [{"setType": "A"}, {"setType": "B"}]}
        assert _set_types_from_list(response) == ["A", "B"]

    def test_type_key_fallback(self):
        response = {"sets": [{"type": "A"}, {"type": "B"}]}
        assert _set_types_from_list(response) == ["A", "B"]

    def test_name_key_fallback(self):
        response = {"sets": [{"name": "A"}]}
        assert _set_types_from_list(response) == ["A"]

    def test_setType_preferred_over_type(self):
        response = {"sets": [{"setType": "X", "type": "Y"}]}
        assert _set_types_from_list(response) == ["X"]


class TestMixedAndEdge:
    def test_mixed_strings_and_dicts(self):
        response = {"sets": ["StringName", {"setType": "DictName"}]}
        assert _set_types_from_list(response) == ["StringName", "DictName"]

    def test_empty_sets_array(self):
        """A real sensor device returns this — sets exist as a key but is
        an empty list. From live_device_sets_14.json (Satellit Sensor)."""
        response = {
            "plant": {"plantId": "13"},
            "device": {"deviceId": "14"},
            "sets": [],
        }
        assert _set_types_from_list(response) == []

    def test_missing_sets_key(self):
        assert _set_types_from_list({"plant": {}, "device": {}}) == []

    def test_sets_not_a_list(self):
        assert _set_types_from_list({"sets": "not a list"}) == []
        assert _set_types_from_list({"sets": {"foo": "bar"}}) == []

    def test_non_dict_response(self):
        assert _set_types_from_list(None) == []
        assert _set_types_from_list("garbage") == []
        assert _set_types_from_list([1, 2, 3]) == []

    def test_garbage_elements_dropped(self):
        response = {"sets": ["A", 123, None, [1, 2], "B", {"no_recognized_key": "X"}]}
        assert _set_types_from_list(response) == ["A", "B"]

    def test_dict_with_empty_setType(self):
        response = {"sets": [{"setType": ""}, {"setType": "valid"}]}
        assert _set_types_from_list(response) == ["valid"]
