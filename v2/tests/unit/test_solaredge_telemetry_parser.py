"""Tests for argia.vendors.solaredge_telemetry parser (Stage 5.1 + 5 regression).

Stage 5.1 ADDS:
- Per-phase nested L1Data/L2Data/L3Data extraction
- Line-to-line voltages vL1To2/vL2To3/vL3To1
- PhaseData dataclass

Synthetic fixtures match the SHAPE of real production captures (from
live_equipment_data_QRO1.json).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from argia.vendors.solaredge import SolarEdgeAPIError
from argia.vendors.solaredge_telemetry import (
    EMPTY_PHASE,
    PhaseData,
    fetch_inverter_telemetry,
    parse_telemetry_response,
)


# ============================================================
# Fixtures matching real QRO1 response shape
# ============================================================


def _l1_data(
    voltage: float = 251.07,
    current: float = 109.79,
    frequency: float = 60.025,
    active_power: float = 27064.04,
    apparent_power: float = 27681.03,
    reactive_power: float = -5775.17,
    cos_phi: float = 1.0,
) -> dict:
    return {
        "acVoltage": voltage,
        "acCurrent": current,
        "acFrequency": frequency,
        "activePower": active_power,
        "apparentPower": apparent_power,
        "reactivePower": reactive_power,
        "cosPhi": cos_phi,
    }


def _rich_telemetry(
    date: str = "2026-05-14 10:50:07",
    total_active: float = 80990.64,
    total_energy: float = 421_970_880.0,
    temperature: float = 53.11,
    dc_voltage: float = 893.17,
    mode: str = "MPPT",
    v_l1_l2: float = 435.14,
    v_l2_l3: float = 435.44,
    v_l3_l1: float = 435.33,
) -> dict:
    """Match the real QRO1 response shape with L1/L2/L3 nested data."""
    return {
        "date": date,
        "totalActivePower": total_active,
        "dcVoltage": dc_voltage,
        "groundFaultResistance": 466.51,
        "powerLimit": 100.0,
        "totalEnergy": total_energy,
        "temperature": temperature,
        "inverterMode": mode,
        "operationMode": 0,
        "vL1To2": v_l1_l2,
        "vL2To3": v_l2_l3,
        "vL3To1": v_l3_l1,
        "L1Data": _l1_data(voltage=251.07, current=109.79, active_power=27064.04),
        "L2Data": _l1_data(voltage=251.82, current=109.46, active_power=26997.76),
        "L3Data": _l1_data(voltage=251.08, current=109.42, active_power=26928.83),
    }


def _equipment_response(telemetries: list) -> dict:
    return {"data": {"count": len(telemetries), "telemetries": telemetries}}


# ============================================================
# STAGE 5.1: line-to-line voltages
# ============================================================


class TestLineToLineVoltages:
    def test_v_l1_to_l2_parsed(self):
        response = _equipment_response([_rich_telemetry(v_l1_l2=435.14)])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.v_l1_to_l2_v == 435.14

    def test_v_l2_to_l3_parsed(self):
        response = _equipment_response([_rich_telemetry(v_l2_l3=435.44)])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.v_l2_to_l3_v == 435.44

    def test_v_l3_to_l1_parsed(self):
        response = _equipment_response([_rich_telemetry(v_l3_l1=435.33)])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.v_l3_to_l1_v == 435.33

    def test_missing_v_fields_become_none(self):
        entry = {
            "date": "2026-05-14 12:00:00",
            "totalActivePower": 45200.0,
            "totalEnergy": 1000.0,
            "inverterMode": "MPPT",
            # No vL1To2 etc
        }
        response = _equipment_response([entry])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.v_l1_to_l2_v is None
        assert row.v_l2_to_l3_v is None
        assert row.v_l3_to_l1_v is None


# ============================================================
# STAGE 5.1: per-phase nested data
# ============================================================


class TestPhaseData:
    def test_l1_populated(self):
        response = _equipment_response([_rich_telemetry()])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert isinstance(row.l1, PhaseData)
        assert row.l1.ac_voltage_v == 251.07
        assert row.l1.ac_current_a == 109.79
        assert row.l1.ac_frequency_hz == 60.025
        assert row.l1.active_power_w == 27064.04
        assert row.l1.cos_phi == 1.0

    def test_l2_populated(self):
        response = _equipment_response([_rich_telemetry()])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.l2.ac_voltage_v == 251.82
        assert row.l2.active_power_w == 26997.76

    def test_l3_populated(self):
        response = _equipment_response([_rich_telemetry()])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.l3.ac_voltage_v == 251.08
        assert row.l3.active_power_w == 26928.83

    def test_missing_phase_returns_empty_phase(self):
        entry = {
            "date": "2026-05-14 12:00:00",
            "totalActivePower": 45200.0,
            "totalEnergy": 1000.0,
            "inverterMode": "MPPT",
            # No L1Data / L2Data / L3Data
        }
        response = _equipment_response([entry])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.l1 == EMPTY_PHASE
        assert row.l2 == EMPTY_PHASE
        assert row.l3 == EMPTY_PHASE

    def test_partial_phase_block(self):
        """If L1Data exists but L2Data is missing, only L1 populates."""
        entry = {
            "date": "2026-05-14 12:00:00",
            "totalActivePower": 45200.0,
            "totalEnergy": 1000.0,
            "inverterMode": "MPPT",
            "L1Data": _l1_data(voltage=250.0),
            # L2Data and L3Data missing
        }
        response = _equipment_response([entry])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.l1.ac_voltage_v == 250.0
        assert row.l2 == EMPTY_PHASE
        assert row.l3 == EMPTY_PHASE

    def test_phase_with_non_dict_input(self):
        """Defensive: L1Data being a string or null doesn't crash."""
        entry = {
            "date": "2026-05-14 12:00:00",
            "totalActivePower": 45200.0,
            "totalEnergy": 1000.0,
            "inverterMode": "MPPT",
            "L1Data": "not a dict",
            "L2Data": None,
            "L3Data": _l1_data(),
        }
        response = _equipment_response([entry])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.l1 == EMPTY_PHASE
        assert row.l2 == EMPTY_PHASE
        assert row.l3.ac_voltage_v == 251.07

    def test_phase_partial_fields_within_block(self):
        """A phase block with only some fields populated."""
        entry = {
            "date": "2026-05-14 12:00:00",
            "totalActivePower": 45200.0,
            "totalEnergy": 1000.0,
            "inverterMode": "MPPT",
            "L1Data": {
                "acVoltage": 251.0,
                "acFrequency": 60.0,
                # Other fields missing
            },
        }
        response = _equipment_response([entry])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.l1.ac_voltage_v == 251.0
        assert row.l1.ac_frequency_hz == 60.0
        assert row.l1.ac_current_a is None
        assert row.l1.active_power_w is None


# ============================================================
# Production-data realistic checks
# ============================================================


class TestRealisticProductionData:
    def test_phase_active_powers_sum_to_total(self):
        """L1.activePower + L2 + L3 should ~= totalActivePower (typically <0.1% deviation)."""
        response = _equipment_response([_rich_telemetry()])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        phase_total = (
            row.l1.active_power_w + row.l2.active_power_w + row.l3.active_power_w
        )
        # Within 1% of totalActivePower
        assert abs(phase_total - row.power_w) / row.power_w < 0.01

    def test_phase_frequencies_consistent(self):
        """All three phases should agree on grid frequency."""
        response = _equipment_response([_rich_telemetry()])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        freqs = [row.l1.ac_frequency_hz, row.l2.ac_frequency_hz, row.l3.ac_frequency_hz]
        # All within 0.1 Hz of each other
        assert max(freqs) - min(freqs) < 0.1


# ============================================================
# STAGE 5 REGRESSION: existing top-level fields keep working
# ============================================================


class TestStage5RegressionTopLevel:
    def test_power_w(self):
        response = _equipment_response([_rich_telemetry(total_active=80990.64)])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.power_w == 80990.64

    def test_temperature(self):
        response = _equipment_response([_rich_telemetry(temperature=53.11)])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.temperature_c == 53.11

    def test_dc_voltage(self):
        response = _equipment_response([_rich_telemetry(dc_voltage=893.17)])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.dc_voltage_v == 893.17

    def test_status_online(self):
        response = _equipment_response([_rich_telemetry(mode="MPPT")])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.status == 1

    def test_status_offline(self):
        response = _equipment_response([_rich_telemetry(mode="FAULT")])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.status == 3

    def test_etotal_kwh(self):
        response = _equipment_response([_rich_telemetry(total_energy=421_970_880.0)])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert row.etotal_kwh == 421970.88

    def test_etoday_from_diff(self):
        response = _equipment_response([
            _rich_telemetry(total_energy=421_500_000.0, date="2026-05-14 00:00:00"),
            _rich_telemetry(total_energy=421_970_880.0, date="2026-05-14 10:50:07"),
        ])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        # (421970880 - 421500000) / 1000 = 470.88
        assert row.etoday_kwh == 470.88

    def test_raw_telemetry_preserved(self):
        response = _equipment_response([_rich_telemetry()])
        row = parse_telemetry_response(response, "QRO1", "INV001")
        assert "L1Data" in row.raw_telemetry
        assert "vL1To2" in row.raw_telemetry


# ============================================================
# STAGE 5 REGRESSION: error paths
# ============================================================


class TestStage5RegressionErrors:
    def test_empty_telemetries_returns_none(self):
        assert parse_telemetry_response(_equipment_response([]), "QRO1", "INV1") is None

    def test_missing_data_key_returns_none(self):
        assert parse_telemetry_response({}, "QRO1", "INV1") is None

    def test_non_dict_returns_none(self):
        assert parse_telemetry_response(None, "QRO1", "INV1") is None
        assert parse_telemetry_response("nope", "QRO1", "INV1") is None


# ============================================================
# STAGE 5 REGRESSION: fetch_inverter_telemetry behavior
# ============================================================


class _FakeInverter:
    def __init__(self, sn):
        self.inverter_sn = sn


class _FakePlant:
    def __init__(self, key="QRO1", site_id="4146396"):
        self.plant_key = key
        self.site_id = site_id


class TestFetchRegression:
    def test_empty_inverters_skips_call(self):
        client = MagicMock()
        result = fetch_inverter_telemetry(client, _FakePlant(), [])
        assert result == []
        client._get_json.assert_not_called()

    def test_returns_parsed_rows(self):
        # v80: the fetch now applies a 60-min recency window, so the
        # fixture entry must carry a current timestamp (site-local
        # naive, as the API serves it)
        import datetime as _dt
        from zoneinfo import ZoneInfo as _Z
        entry = _rich_telemetry()
        entry["date"] = _dt.datetime.now(
            _Z("America/Mexico_City")).strftime("%Y-%m-%d %H:%M:%S")
        client = MagicMock()
        client._get_json.return_value = _equipment_response([entry])
        result = fetch_inverter_telemetry(
            client, _FakePlant(),
            [_FakeInverter("INV1"), _FakeInverter("INV2")],
        )
        assert len(result) == 2

    def test_rate_limit_reraises(self):
        client = MagicMock()
        client._get_json.side_effect = SolarEdgeAPIError("rate-limited HTTP 429")
        with pytest.raises(SolarEdgeAPIError, match="rate-limited"):
            fetch_inverter_telemetry(
                client, _FakePlant(), [_FakeInverter("INV1")],
            )

    def test_empty_telemetry_returns_no_row(self):
        """The GTO2 inverter 1 scenario — empty data is not an error."""
        client = MagicMock()
        client._get_json.return_value = _equipment_response([])
        result = fetch_inverter_telemetry(
            client, _FakePlant(), [_FakeInverter("OFFLINE_INV")],
        )
        assert result == []


class TestMultiEntryParsing:
    """v80: every 5-minute entry becomes a row (previously only the
    latest survived), with eToday cumulative from the day's first
    entry — the same semantics as every other vendor feed, so
    KPI max(EToday) aggregation is unchanged."""

    def _entries(self):
        base = 421_000_000.0
        out = []
        for i, (hh, wh) in enumerate([("08:00", 0.0), ("08:05", 2500.0),
                                      ("08:10", 6000.0)]):
            e = _rich_telemetry()
            e["date"] = "2026-05-14 %s:00" % hh
            e["totalEnergy"] = base + wh
            out.append(e)
        return out

    def test_all_entries_become_rows(self):
        from argia.vendors.solaredge_telemetry import (
            parse_telemetry_entries,
        )
        rows = parse_telemetry_entries(
            _equipment_response(self._entries()), "QRO1", "INV1")
        assert len(rows) == 3
        assert [r.etoday_kwh for r in rows] == [0.0, 2.5, 6.0]

    def test_min_ts_filters_rows_but_keeps_day_anchor(self):
        import datetime as _dt
        from argia.vendors.solaredge_telemetry import (
            MX_TZ, parse_telemetry_entries,
        )
        # cut at 08:07 site-local: only the 08:10 row survives, but its
        # eToday still measures from the 08:00 first entry
        cut = _dt.datetime(2026, 5, 14, 8, 7,
                           tzinfo=MX_TZ).astimezone(_dt.timezone.utc)
        rows = parse_telemetry_entries(
            _equipment_response(self._entries()), "QRO1", "INV1",
            min_ts_utc=cut)
        assert len(rows) == 1
        assert rows[0].etoday_kwh == 6.0

    def test_latest_wrapper_unchanged(self):
        from argia.vendors.solaredge_telemetry import (
            parse_telemetry_entries, parse_telemetry_response,
        )
        resp = _equipment_response(self._entries())
        latest = parse_telemetry_response(resp, "QRO1", "INV1")
        allrows = parse_telemetry_entries(resp, "QRO1", "INV1")
        assert latest.timestamp_utc == allrows[-1].timestamp_utc
        assert latest.etoday_kwh == 6.0
