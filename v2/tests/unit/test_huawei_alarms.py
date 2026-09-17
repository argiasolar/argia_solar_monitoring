"""v257 — the Huawei alarm list we were never reading.

On 2026-09-16 FusionSolar held three Major "Device Fault" alarms on SAG's
inverters with Huawei's own cause and repair text, while ARGIA knew only
that power had gone to zero. The fixture is the payload captured from
the live account on 2026-09-17 with the identifiers replaced — field
names and types are the vendor's, not a guess."""
from __future__ import annotations

import json
import pathlib
import sys

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

from argia.vendors import huawei_alarms as HA   # noqa: E402

PAYLOAD = json.loads((V2 / "tests/fixtures/huawei/alarm_list.json").read_text(encoding="utf-8"))


class TestParse:
    def test_the_captured_payload_parses_into_every_field(self):
        got = HA.parse_alarms(PAYLOAD)
        assert len(got) == 3
        a = got[0]
        assert a.alarm_name == "Network Connection Error"
        assert a.alarm_id == 999999999 and a.cause_id == -1 and a.alarm_type == 0
        assert a.dev_type_id == 63 and a.esn_code == "0000A0000000"
        assert a.lev == 2 and a.status == 1
        assert a.station_name == "DEMO PLANT ONE"
        assert a.alarm_cause.startswith("The server has not received")
        assert a.repair_suggestion.startswith("1. Check whether the device is powered on")

    def test_it_accepts_the_bare_data_list_too(self):
        assert len(HA.parse_alarms(PAYLOAD["data"])) == 3

    def test_junk_never_raises(self):
        for junk in (None, {}, [], {"data": None}, {"data": "nope"}, {"data": [None, 7, "x"]}, 42):
            assert HA.parse_alarms(junk) == [] or all(
                isinstance(x, HA.HuaweiAlarm) for x in HA.parse_alarms(junk))

    def test_a_record_missing_everything_still_parses(self):
        a = HA.parse_alarms({"data": [{}]})[0]
        assert a.alarm_id is None and a.alarm_name == "" and a.raised_utc is None
        assert a.severity == "CRITICAL", "an alarm we cannot read is not assumed harmless"


class TestSeverity:
    def test_lev_2_is_major_as_the_portal_shows_it(self):
        """The FusionSolar UI labelled the SAG alarms Major; lev=2 is the
        only level confirmed against the portal."""
        a = HA.parse_alarms(PAYLOAD)[0]
        assert a.lev == 2 and a.level_label == "Major" and a.severity == "CRITICAL"

    def test_the_documented_order_for_the_rest(self):
        mk = lambda lev: HA.parse_alarms({"data": [{"lev": lev}]})[0]   # noqa: E731
        assert mk(1).level_label == "Critical" and mk(1).severity == "CRITICAL"
        assert mk(3).level_label == "Minor" and mk(3).severity == "WARNING"
        assert mk(4).level_label == "Warning" and mk(4).severity == "WARNING"

    def test_an_unknown_level_is_never_quietly_downgraded(self):
        a = HA.parse_alarms({"data": [{"lev": 99}]})[0]
        assert a.level_label == "Unknown" and a.severity == "CRITICAL"


class TestActiveAndIdentity:
    def test_only_status_1_is_active(self):
        got = HA.parse_alarms(PAYLOAD)
        assert [a.active for a in got] == [True, True, False]
        assert len(HA.active_alarms(got)) == 2

    def test_the_key_is_stable_per_device_and_alarm(self):
        got = HA.parse_alarms(PAYLOAD)
        assert got[0].key() == "NE=00000001:0000A0000000:999999999"
        assert got[0].key() != got[1].key()
        again = HA.parse_alarms(PAYLOAD)
        assert [a.key() for a in again] == [a.key() for a in got], "keys must not drift between runs"

    def test_raise_time_is_epoch_milliseconds(self):
        a = HA.parse_alarms(PAYLOAD)[0]
        assert a.raised_utc is not None
        assert a.raised_utc.year == 2026 and a.raised_utc.tzinfo is not None
        assert HA.parse_alarms({"data": [{"raiseTime": "junk"}]})[0].raised_utc is None


class TestTheDataloggerCase:
    def test_a_logger_alarm_is_recognised_as_a_whole_site_outage(self):
        """devTypeId 63 is the datalogger. SAG's 2026-09-17 alarm was on
        the logger, which is why every inverter reading went blank."""
        logger, inverter = HA.parse_alarms(PAYLOAD)[0], HA.parse_alarms(PAYLOAD)[1]
        assert logger.is_datalogger and not inverter.is_datalogger
        assert "DATALOGGER" in HA.message(logger, "MEX1")
        assert "whole site is off the air" in HA.explanation(logger)
        assert "off the air" not in HA.explanation(inverter)


class TestWhatThePersonReads:
    def test_the_message_names_the_vendor_alarm_severity_and_device(self):
        m = HA.message(HA.parse_alarms(PAYLOAD)[1], "MEX1")
        assert "MEX1" in m and "2064" in m and "Device Fault" in m and "Major" in m
        assert "Inverter 3" in m and "[CRITICAL]" in m and "since 2026-" in m

    def test_the_explanation_is_huaweis_own_words_not_ours(self):
        e = HA.explanation(HA.parse_alarms(PAYLOAD)[1])
        assert "A major fault has occurred on the internal circuit" in e
        assert "Cause (Huawei):" in e and "Huawei's suggestion:" in e
        assert "do not turn on the AC or DC switch" in e

    def test_an_empty_alarm_produces_no_invented_text(self):
        assert HA.explanation(HA.parse_alarms({"data": [{}]})[0]) == ""
