"""v391 - open energy alerts that the loss check has since proved false are closed (Tomasz: "make sure that
issues that are not real are removed"). Pure parts only; the ledger rows are invented."""
from __future__ import annotations

import datetime as dt

from argia.core.alerts_state import AlertRecord, AlertState, resolve_alert
from scripts import alerts_revalidate as R


def rec(aid, pk, metric="energy_daily_pct", state=AlertState.OPEN, seen="2026-11-23T12:30:19+00:00"):
    return AlertRecord(aid, f"{pk.lower()}:plant:{metric}", pk, "", metric, "CRITICAL", state,
                       seen, seen, "", 0.5, 0.7, f"[{pk}] produced 1183 kWh vs 2336 expected (51%)", "email")


def test_the_day_is_the_one_before_the_morning_run():
    assert R.alert_day(rec("A", "MEX2")) == "2026-11-22"                       # 06:30 MX on 23 Nov judged 22 Nov
    assert R.alert_day(rec("A", "MEX2", seen="2026-11-24T05:30:00+00:00")) == "2026-11-22"   # 23:30 MX on 23 Nov
    assert R.alert_day(rec("A", "MEX2", seen="")) is None


def test_the_22_nov_false_alarm_is_closed_and_real_ones_stay():
    recs = [rec("F1", "MEX1"), rec("F2", "MEX2"), rec("F3", "MEX3"), rec("R1", "GTO3"), rec("R2", "NL2"),
            rec("V", "MEX2", metric="vendor_alarm"), rec("OLD", "MEX2", state=AlertState.RESOLVED)]
    verdicts = {("MEX1", "2026-11-22"): ("peers", 1128.0, 1237.0, 0.0),
                ("MEX2", "2026-11-22"): ("peers", 1250.0, 1183.0, 0.0),
                ("MEX3", "2026-11-22"): ("weather-estimated", 644.0, 308.0, 0.0),
                ("GTO3", "2026-11-22"): ("peers", 1930.0, 378.0, 1552.1),          # really down: stays
                ("NL2", "2026-11-22"): ("weather-model", 840.0, 493.0, None)}      # not judged: stays
    got = {r.alert_id: why for r, why in R.false_alarms(recs, verdicts)}
    assert set(got) == {"F1", "F2", "F3"}
    assert "nothing lost" in got["F2"] and "neighbouring plants" in got["F2"] and "1,250" in got["F2"]
    assert "estimated sun" in got["F3"]


def test_a_plain_weather_verdict_does_not_close_an_alert():
    v = {("MEX2", "2026-11-22"): ("weather", 2000.0, 1990.0, 0.0)}
    assert R.false_alarms([rec("A", "MEX2")], v) == []


def test_no_verdict_no_change():
    assert R.false_alarms([rec("A", "MEX2")], {}) == []


def test_resolution_keeps_the_original_message():
    r, why = R.false_alarms([rec("F2", "MEX2")], {("MEX2", "2026-11-22"): ("peers", 1250.0, 1183.0, 0.0)})[0]
    out = resolve_alert(r, dt.datetime(2026, 11, 23, 18, 0, tzinfo=dt.timezone.utc), final_message=f"{r.message} [{why}]")
    assert out.state == AlertState.RESOLVED and out.message.startswith("[MEX2] produced 1183 kWh") and "false alarm" in out.message
    assert out.channels_sent == "email"                                              # no new mail
