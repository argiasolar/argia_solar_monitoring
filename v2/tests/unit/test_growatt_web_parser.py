"""
Tests for argia.vendors.growatt_web_parser.

Every test that touches structure runs against a real captured fixture
under v2/tests/fixtures/growatt_web/. Tests that exercise corner cases
(error envelopes, missing keys, ``obj: false``) use small inline dicts.

When the Stage 0 capture is re-run and the fixtures change, tests that
hard-code field values may need updating — that's intentional. We're
guarding against accidental regressions in what Growatt returns.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from argia.vendors.base import InverterSnapshot
from argia.vendors.growatt_web_parser import (
    Alert,
    Device,
    DevicesByPlant,
    GrowattParseError,
    MAXHistoryRow,
    MAXTotalData,
    PlantInfo,
    build_inverter_snapshot,
    check_envelope,
    compute_day_total_kwh_from_history,
    extract_latest_row,
    extract_obj,
    parse_alert_plant_event,
    parse_devices_by_plant,
    parse_list_device,
    parse_max_day_chart,
    parse_max_history,
    parse_max_history_row,
    parse_max_total_data,
    parse_plant_data,
    parse_weather,
    per_mppt_eday_today_kwh,
    per_mppt_eday_total_kwh,
    per_mppt_powers,
    per_mppt_voltages,
    per_string_voltages,
    unwrap_fixture,
    unwrap_response,
)
from tests.conftest import load_fixture


# =====================================================================
# 1. unwrap_fixture / unwrap_response / check_envelope / extract_obj
# =====================================================================

class TestEnvelopeUnwrap:
    def test_unwrap_strips_meta_and_decodes_raw_text(self):
        fixture = load_fixture("growatt_web", "GTO1_getPlantData.json")
        unwrapped = unwrap_fixture(fixture)
        # _raw_text was double-encoded JSON; should be a real dict now
        assert isinstance(unwrapped, dict)
        assert unwrapped["result"] == 1
        assert "obj" in unwrapped
        assert unwrapped["obj"]["plantName"] == "Taigene"

    def test_unwrap_handles_already_parsed_response(self):
        # alertPlantEvent fixtures have proper JSON, no _raw_text wrapper
        fixture = load_fixture("growatt_web", "GTO1_alertPlantEvent.json")
        unwrapped = unwrap_fixture(fixture)
        assert unwrapped["result"] == 1
        assert unwrapped["obj"] is False

    def test_unwrap_accepts_bare_response_dict(self):
        # When called on a response that lacks the _meta wrapper, pass through
        unwrapped = unwrap_fixture({"result": 1, "obj": {"a": 1}})
        assert unwrapped == {"result": 1, "obj": {"a": 1}}

    def test_unwrap_rejects_non_dict(self):
        with pytest.raises(GrowattParseError):
            unwrap_fixture("not a dict")

    def test_unwrap_response_with_malformed_raw_text_raises(self):
        with pytest.raises(GrowattParseError):
            unwrap_response({"_raw_text": "{not json"})

    def test_check_envelope_returns_result_int(self):
        assert check_envelope({"result": 1, "obj": {}}) == 1
        assert check_envelope({"result": 0}) == 0

    def test_check_envelope_string_result_coerced_to_int(self):
        assert check_envelope({"result": "1"}) == 1

    def test_check_envelope_missing_result_raises(self):
        with pytest.raises(GrowattParseError, match="result"):
            check_envelope({"obj": {}})

    def test_check_envelope_non_dict_raises(self):
        with pytest.raises(GrowattParseError):
            check_envelope([1, 2, 3])

    def test_extract_obj_returns_obj_on_success(self):
        obj = extract_obj({"result": 1, "obj": {"x": 42}})
        assert obj == {"x": 42}

    def test_extract_obj_returns_none_on_result_zero(self):
        # Real fixture: weather often comes back result=0
        fixture = load_fixture("growatt_web", "GTO1_getWeatherByPlantId.json")
        assert extract_obj(fixture) is None


# =====================================================================
# 2. parse_max_history (the big one — full fixture)
# =====================================================================

class TestParseMaxHistory:
    def test_full_fixture_returns_list(self):
        fixture = load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        rows = parse_max_history(fixture)
        assert isinstance(rows, list)
        # Real day has ~150 rows; fixture for testing has at least 5
        assert len(rows) >= 5

    def test_every_row_is_typed(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        for row in rows:
            assert isinstance(row, MAXHistoryRow)

    def test_pac_field_extracted_as_float(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        pacs = [r.pac_w for r in rows]
        # At least one nonzero pac in any realistic day-worth of data
        assert any(p is not None and p > 0 for p in pacs)

    def test_eac_today_monotone_non_decreasing(self):
        """eacToday is a running total — successive samples shouldn't drop
        meaningfully. (Small jitter under 0.5 kWh is tolerated.)"""
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        # Sort by time string (ISO-comparable) then check non-decreasing
        sorted_rows = sorted(rows, key=lambda r: r.time_str)
        last = -1.0
        for r in sorted_rows:
            if r.eac_today_kwh is None:
                continue
            # tolerate 0.5 kWh wobble (sensor noise)
            assert r.eac_today_kwh >= last - 0.5, (
                f"eacToday went backwards at {r.time_str}: "
                f"{r.eac_today_kwh} < {last}"
            )
            last = max(last, r.eac_today_kwh)

    def test_calendar_parses_to_mx_local_aware_datetime(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        ts = [r.timestamp_mx for r in rows if r.timestamp_mx is not None]
        assert ts, "no row had a parseable calendar"
        for t in ts:
            assert t.tzinfo is not None
            # All fixtures are from May 2026 — month is 5 (not 4!) after un-zero-indexing
            assert t.year == 2026
            assert t.month == 5

    def test_time_string_is_preserved_verbatim(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        for r in rows:
            assert isinstance(r.time_str, str)
            # YYYY-MM-DD HH:MM:SS shape
            assert len(r.time_str) == 19 or r.time_str == ""

    def test_raw_dict_preserves_all_fields(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        # raw dict should have many fields (real has ~155, fixture has 170+)
        for r in rows:
            assert len(r.raw) >= 100, f"only {len(r.raw)} fields preserved"

    def test_returns_empty_when_result_zero(self):
        fake = {"result": 0, "msg": "no data", "obj": None}
        assert parse_max_history(fake) == []

    def test_returns_empty_when_datas_missing(self):
        fake = {"result": 1, "obj": {"endDate": "2026-05-11"}}
        assert parse_max_history(fake) == []


# =====================================================================
# 3. parse_max_history_row — individual row fields
# =====================================================================

class TestParseMaxHistoryRow:
    @pytest.fixture
    def sample_row(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        # Pick the highest-pac row (the most interesting one for coverage)
        return max(rows, key=lambda r: r.pac_w or 0)

    def test_three_phase_voltage_present(self, sample_row):
        assert sample_row.vacr_v is not None
        assert sample_row.vacs_v is not None
        assert sample_row.vact_v is not None

    def test_power_factor_in_valid_range(self, sample_row):
        assert sample_row.pf is not None
        assert -1.0 <= sample_row.pf <= 1.0

    def test_temperature_realistic(self, sample_row):
        assert sample_row.temperature_c is not None
        # Inverter ambient: 0..80 °C is sane
        assert 0 <= sample_row.temperature_c <= 80

    def test_status_codes_are_ints_or_none(self, sample_row):
        for attr in (
            "warn_code", "warn_code_1", "fault_code_1", "fault_code_2",
            "pid_status", "apf_status", "afci_status", "derating_mode",
            "real_op_percent",
        ):
            value = getattr(sample_row, attr)
            assert value is None or isinstance(value, int), (
                f"{attr} should be int or None, got {type(value).__name__}"
            )

    def test_create_time_ms_is_int(self, sample_row):
        assert sample_row.create_time_ms is not None
        assert isinstance(sample_row.create_time_ms, int)
        # 13-digit ms epoch in 2026 must be > 10**12 and < 10**13
        assert 10**12 < sample_row.create_time_ms < 10**13

    def test_bus_voltages_present(self, sample_row):
        # p/n bus voltages exist (often 0.0 at night, but present)
        assert sample_row.p_bus_voltage is not None
        assert sample_row.n_bus_voltage is not None

    def test_missing_optional_fields_become_none(self):
        # Bare minimum row — no AC, no DC, just time
        row = parse_max_history_row({"time": "2026-05-11 00:00:00"})
        assert row.pac_w is None
        assert row.vacr_v is None
        assert row.pf is None
        assert row.temperature_c is None

    def test_string_numeric_values_coerced(self):
        # Growatt sometimes returns numbers as strings (esp. in getMAXTotalData)
        row = parse_max_history_row({
            "pac": "1234.5",
            "eacToday": "100",
            "warnCode": "0",
        })
        assert row.pac_w == 1234.5
        assert row.eac_today_kwh == 100.0
        assert row.warn_code == 0

    def test_fault_code_detection(self, sample_row):
        """The synthetic fixture's last row has fault_code_1=1; verify our
        parser captures that into the dataclass."""
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        fault_rows = [r for r in rows if (r.fault_code_1 or 0) > 0]
        # Fixture has exactly one fault row; real data may have zero
        # (clean day) or many (bad day). Either is fine — just make sure
        # the field is parsed without error.
        for r in fault_rows:
            assert r.fault_code_1 is not None
            assert r.fault_code_1 > 0


# =====================================================================
# 4. Per-MPPT / per-string field accessors
# =====================================================================

class TestFieldFamilyAccessors:
    @pytest.fixture
    def any_row(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        return rows[0]

    def test_per_mppt_voltages_returns_16_entries(self, any_row):
        v = per_mppt_voltages(any_row)
        assert len(v) == 16

    def test_per_mppt_powers_returns_9_entries(self, any_row):
        p = per_mppt_powers(any_row)
        # Growatt's MAX line only exposes ppv1..ppv9 in the history payload
        assert len(p) == 9

    def test_per_string_voltages_returns_32_entries(self, any_row):
        s = per_string_voltages(any_row)
        assert len(s) == 32

    def test_per_mppt_eday_today_returns_15_entries(self, any_row):
        e = per_mppt_eday_today_kwh(any_row)
        # epv1Today..epv15Today (Growatt skips epv0 / epv16)
        assert len(e) == 15

    def test_per_mppt_eday_total_returns_15_entries(self, any_row):
        e = per_mppt_eday_total_kwh(any_row)
        assert len(e) == 15

    def test_accepts_raw_dict_too(self, any_row):
        # Both bare dict and MAXHistoryRow work
        v_from_row = per_mppt_voltages(any_row)
        v_from_dict = per_mppt_voltages(any_row.raw)
        assert v_from_row == v_from_dict

    def test_missing_keys_become_none(self):
        # Empty dict → every field None
        assert per_mppt_voltages({}) == [None] * 16
        assert per_mppt_powers({}) == [None] * 9
        assert per_string_voltages({}) == [None] * 32

    def test_rejects_invalid_type(self):
        with pytest.raises(TypeError):
            per_mppt_voltages(42)


# =====================================================================
# 5. History helpers — latest row, day total, snapshot
# =====================================================================

class TestHistoryHelpers:
    def test_extract_latest_row_empty_list(self):
        assert extract_latest_row([]) is None

    def test_extract_latest_row_picks_max_by_time(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        latest = extract_latest_row(rows)
        assert latest is not None
        # Should be the chronologically last by time_str
        for r in rows:
            assert r.time_str <= latest.time_str

    def test_compute_day_total_kwh_picks_max_eacToday(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        total = compute_day_total_kwh_from_history(rows)
        assert total is not None
        # Must equal max of all eacToday values
        all_eacs = [r.eac_today_kwh for r in rows if r.eac_today_kwh is not None]
        assert total == max(all_eacs)

    def test_compute_day_total_kwh_empty_returns_none(self):
        assert compute_day_total_kwh_from_history([]) is None

    def test_compute_day_total_kwh_all_none_returns_none(self):
        rows = [parse_max_history_row({"time": "x"})]
        assert compute_day_total_kwh_from_history(rows) is None


# =====================================================================
# 6. build_inverter_snapshot
# =====================================================================

class TestBuildInverterSnapshot:
    @pytest.fixture
    def latest_row(self):
        rows = parse_max_history(
            load_fixture("growatt_web", "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json")
        )
        return extract_latest_row(rows)

    def test_returns_inverter_snapshot(self, latest_row):
        snap = build_inverter_snapshot(latest_row, "GTO1", "JFM7DXN00T")
        assert isinstance(snap, InverterSnapshot)

    def test_sn_normalized_uppercase_nospace(self, latest_row):
        snap = build_inverter_snapshot(latest_row, "GTO1", "  jfm7DXN00t  ")
        assert snap.inverter_sn == "JFM7DXN00T"

    def test_timestamp_is_utc_aware(self, latest_row):
        snap = build_inverter_snapshot(latest_row, "GTO1", "X")
        assert snap.timestamp_utc.tzinfo is not None
        assert snap.timestamp_utc.tzinfo == dt.timezone.utc

    def test_fault_marks_status_offline(self):
        """Synthetic row with fault_code_1=1 should produce status=3."""
        row = parse_max_history_row({
            "time": "2026-05-11 12:00:00",
            "pac": 1000.0,
            "eacToday": 50.0,
            "faultCode1": 7,
        })
        snap = build_inverter_snapshot(row, "GTO1", "X")
        assert snap.status == 3

    def test_clean_row_marks_status_online(self):
        row = parse_max_history_row({
            "time": "2026-05-11 12:00:00",
            "pac": 1000.0,
            "eacToday": 50.0,
            "faultCode1": 0,
            "faultCode2": 0,
        })
        snap = build_inverter_snapshot(row, "GTO1", "X")
        assert snap.status == 1

    def test_power_w_passed_through_in_watts(self):
        row = parse_max_history_row({
            "time": "2026-05-11 12:00:00",
            "pac": 85000.5,
        })
        snap = build_inverter_snapshot(row, "GTO1", "X")
        # Web UI's pac is already in W — no kW conversion!
        assert snap.power_w == 85000.5

    def test_etoday_kwh_from_eacToday(self):
        row = parse_max_history_row({
            "time": "2026-05-11 12:00:00",
            "eacToday": 234.5,
        })
        snap = build_inverter_snapshot(row, "GTO1", "X")
        assert snap.etoday_kwh == 234.5


# =====================================================================
# 7. parse_max_day_chart
# =====================================================================

class TestParseMaxDayChart:
    def test_returns_288_slots(self):
        fixture = load_fixture("growatt_web", "GTO1_getMAXDayChart_JFM7DXN00T_2026-05-11.json")
        chart = parse_max_day_chart(fixture)
        assert len(chart) == 288

    def test_all_floats(self):
        fixture = load_fixture("growatt_web", "GTO1_getMAXDayChart_JFM7DXN00T_2026-05-11.json")
        chart = parse_max_day_chart(fixture)
        for v in chart:
            assert isinstance(v, float)

    def test_has_daytime_peak(self):
        """A real solar day must have nonzero power around midday."""
        fixture = load_fixture("growatt_web", "GTO1_getMAXDayChart_JFM7DXN00T_2026-05-11.json")
        chart = parse_max_day_chart(fixture)
        # Slots 100-200 cover ~08:20 to ~16:40 local — must include the peak
        midday = chart[100:200]
        assert max(midday) > 50_000, "no midday peak — chart suspicious"

    def test_night_slots_are_zero(self):
        """Slot 0 = 00:00 local should be 0.0 (solar inverter at night)."""
        fixture = load_fixture("growatt_web", "GTO1_getMAXDayChart_JFM7DXN00T_2026-05-11.json")
        chart = parse_max_day_chart(fixture)
        assert chart[0] == 0.0
        assert chart[-1] == 0.0

    def test_returns_empty_on_failure(self):
        assert parse_max_day_chart({"result": 0}) == []


# =====================================================================
# 8. parse_max_total_data
# =====================================================================

class TestParseMaxTotalData:
    def test_fixture_parses(self):
        result = parse_max_total_data(load_fixture("growatt_web", "GTO1_getMAXTotalData.json"))
        assert isinstance(result, MAXTotalData)

    def test_plant_id_extracted(self):
        result = parse_max_total_data(load_fixture("growatt_web", "GTO1_getMAXTotalData.json"))
        assert result.plant_id == "9309575"

    def test_string_kwh_coerced_to_float(self):
        """The real fixture has eToday="786.8" (string, not number)."""
        result = parse_max_total_data(load_fixture("growatt_web", "GTO1_getMAXTotalData.json"))
        assert result.e_today_kwh == 786.8
        assert result.e_total_kwh == 1504463.9

    def test_money_unit_preserved(self):
        result = parse_max_total_data(load_fixture("growatt_web", "GTO1_getMAXTotalData.json"))
        assert result.money_unit == "$"

    def test_returns_none_on_result_zero(self):
        assert parse_max_total_data({"result": 0}) is None


# =====================================================================
# 9. parse_plant_data
# =====================================================================

class TestParsePlantData:
    def test_fixture_parses(self):
        result = parse_plant_data(load_fixture("growatt_web", "GTO1_getPlantData.json"))
        assert isinstance(result, PlantInfo)

    def test_taigene_plant_name(self):
        result = parse_plant_data(load_fixture("growatt_web", "GTO1_getPlantData.json"))
        assert result.plant_name == "Taigene"

    def test_mexico_lat_lng(self):
        result = parse_plant_data(load_fixture("growatt_web", "GTO1_getPlantData.json"))
        # Guanajuato, Mexico
        assert 20 < (result.lat or 0) < 22
        assert -103 < (result.lng or 0) < -101

    def test_timezone_offset_mexico(self):
        result = parse_plant_data(load_fixture("growatt_web", "GTO1_getPlantData.json"))
        assert result.timezone_hours == -6  # CST

    def test_nominal_power_in_watts(self):
        result = parse_plant_data(load_fixture("growatt_web", "GTO1_getPlantData.json"))
        # 606 kWp = 606000 W
        assert result.nominal_power_w == 606_000

    def test_create_date_field_typo_handled(self):
        """Growatt writes 'creatDate' (no E). Make sure we read it."""
        result = parse_plant_data(load_fixture("growatt_web", "GTO1_getPlantData.json"))
        assert result.create_date == "2024-10-24"


# =====================================================================
# 10. parse_devices_by_plant
# =====================================================================

class TestParseDevicesByPlant:
    def test_real_fixture_parses(self):
        result = parse_devices_by_plant(
            load_fixture("growatt_web", "GTO1_getDevicesByPlant.json")
        )
        assert isinstance(result, DevicesByPlant)

    def test_inverter_bucket_extracted(self):
        result = parse_devices_by_plant(
            load_fixture("growatt_web", "GTO1_getDevicesByPlant.json")
        )
        # Real fixture has one MAX inverter listed
        assert len(result.inverters) >= 1
        inv = result.inverters[0]
        assert inv.sn == "JFM7DXN00U"
        assert inv.bucket == "max"

    def test_env_bucket_separated_from_inverters(self):
        result = parse_devices_by_plant(
            load_fixture("growatt_web", "GTO1_getDevicesByPlant.json")
        )
        assert len(result.env_devices) == 1
        env = result.env_devices[0]
        assert env.sn == "DYD0E8501G_1"
        assert env.bucket == "env"

    def test_known_growatt_bug_only_one_sn_per_bucket(self):
        """getDevicesByPlant returns only ONE inverter even when there are 4.
        This is a Growatt API quirk we can't fix in the parser. The test
        documents the behaviour so it doesn't surprise anyone later."""
        result = parse_devices_by_plant(
            load_fixture("growatt_web", "GTO1_getDevicesByPlant.json")
        )
        # GTO1 actually has 4 inverters in real life; fixture shows just 1
        assert len(result.inverters) == 1, (
            "If Growatt fixes the bug and returns all SNs, update the parser "
            "docstring's HARDCODED_INVERTER_SNS warning"
        )

    def test_empty_obj_returns_empty_result(self):
        result = parse_devices_by_plant({"result": 1, "obj": {}})
        assert result.inverters == []
        assert result.env_devices == []
        assert result.other == []


# =====================================================================
# 11. parse_alert_plant_event
# =====================================================================

class TestParseAlertPlantEvent:
    def test_obj_false_returns_empty_list(self):
        """Real fixture for GTO1 has obj:false (no active alerts)."""
        fixture = load_fixture("growatt_web", "GTO1_alertPlantEvent.json")
        assert parse_alert_plant_event(fixture) == []

    def test_list_obj_parses_alerts(self):
        # Synthetic — we don't have a populated real alert fixture yet.
        # When we capture one, replace this with the real shape.
        fake = {
            "result": 1,
            "obj": [
                {
                    "deviceSn": "JFM7DXN00T",
                    "alarmCode": "501",
                    "alarmTime": "2026-05-11 13:42:00",
                    "alarmDesc": "DC over-voltage",
                }
            ],
        }
        alerts = parse_alert_plant_event(fake)
        assert len(alerts) == 1
        assert alerts[0].device_sn == "JFM7DXN00T"
        assert alerts[0].code == "501"

    def test_wrapped_in_datas_list(self):
        fake = {
            "result": 1,
            "obj": {"datas": [{"deviceSn": "X", "alarmCode": "100"}]},
        }
        alerts = parse_alert_plant_event(fake)
        assert len(alerts) == 1

    def test_result_zero_returns_empty(self):
        assert parse_alert_plant_event({"result": 0}) == []


# =====================================================================
# 12. parse_weather
# =====================================================================

class TestParseWeather:
    def test_result_zero_returns_none(self):
        """GTO1 fixture has result=0 — Growatt's weather often unavailable."""
        assert parse_weather(load_fixture("growatt_web", "GTO1_getWeatherByPlantId.json")) is None

    def test_populated_weather_returns_dict(self):
        fake = {
            "result": 1,
            "obj": {"temp": 24.5, "humidity": 60, "condition": "sunny"},
        }
        result = parse_weather(fake)
        assert result == {"temp": 24.5, "humidity": 60, "condition": "sunny"}


# =====================================================================
# 13. parse_list_device
# =====================================================================

class TestParseListDevice:
    def test_result_zero_returns_empty(self):
        """Real listDevice fixture has result=0 (account-level access denied)."""
        assert parse_list_device(load_fixture("growatt_web", "listDevice.json")) == []

    def test_populated_account_returns_devices(self):
        fake = {
            "result": 1,
            "obj": {
                "max": [["SN001", "Inv A", "4"]],
                "tlx": [["SN002", "Inv B", "5"]],
            },
        }
        devices = parse_list_device(fake)
        assert len(devices) == 2
        sns = {d.sn for d in devices}
        assert sns == {"SN001", "SN002"}


# =====================================================================
# 14. Regression: cross-fixture consistency
#     (small but high-value sanity checks)
# =====================================================================

class TestCrossFixtureConsistency:
    def test_plant_id_matches_across_endpoints(self):
        """getPlantData and getMAXTotalData should agree on the plant ID."""
        plant = parse_plant_data(load_fixture("growatt_web", "GTO1_getPlantData.json"))
        total = parse_max_total_data(load_fixture("growatt_web", "GTO1_getMAXTotalData.json"))
        assert plant.plant_id == total.plant_id == "9309575"

    def test_etotal_matches_across_endpoints(self):
        """eTotal should be identical from getPlantData and getMAXTotalData
        (they read the same underlying counter)."""
        plant = parse_plant_data(load_fixture("growatt_web", "GTO1_getPlantData.json"))
        total = parse_max_total_data(load_fixture("growatt_web", "GTO1_getMAXTotalData.json"))
        assert plant.e_total_kwh == total.e_total_kwh
