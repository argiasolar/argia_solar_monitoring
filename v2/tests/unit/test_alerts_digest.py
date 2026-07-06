"""Tests: daily open-alerts digest (argia.alerts.digest).

The gap it closes (2026-07-06): mail-once dedupe left three GTO1 FAULT
inverters silently open for days. The digest mails one summary per
morning while anything is open, and nothing when the ledger is clean —
so inbox silence means "all clear" again.
"""

import datetime as dt

from argia.alerts.digest import (
    DIGEST_KEY,
    DIGEST_METRIC,
    apply_daily_digest,
    reportable_alerts,
    summarize_open_alerts,
)
from argia.core.alerts_state import AlertState, open_alert, resolve_alert

NOW = dt.datetime(2026, 7, 7, 12, 30, tzinfo=dt.timezone.utc)


def rec(key="gto1:inv:a:inverter_fault", plant="GTO1", metric="inverter_fault",
        sev="CRITICAL", opened="2026-07-05T14:15:44+00:00", n=1):
    from dataclasses import replace
    r = open_alert(alert_id=f"ALT-20260705-{n:03d}", alert_key=key,
                   plant_key=plant, inverter_sn="A", metric=metric,
                   severity=sev, now_utc=NOW, value=None, threshold=None,
                   message=f"{plant} breach", explanation="x")
    return replace(r, opened_utc=opened)   # frozen dataclass: use replace


class TestSummarize:
    def test_nothing_open_means_silence(self):
        records = [resolve_alert(rec(), NOW)]
        assert summarize_open_alerts(records, NOW) is None

    def test_counts_grouping_and_age(self):
        records = [
            rec(key="gto1:inv:a:inverter_fault", n=1),
            rec(key="gto1:inv:b:inverter_fault", n=2),
            rec(key="mex1:inv:c:inverter_relative", plant="MEX1",
                metric="inverter_relative", n=3),
            rec(key="nl1:inv:d:inverter_temp_high", plant="NL1",
                metric="inverter_temp_high", sev="WARNING",
                opened="2026-07-03T14:00:00+00:00", n=4),
        ]
        sev, msg, exp = summarize_open_alerts(records, NOW)
        assert sev == "CRITICAL"
        assert "3 critical / 1 warning" in msg
        assert "GTO1: inverter fault \u00d72 (2d)" in exp
        assert "NL1: inverter temp high (4d)" in exp
        assert "repeats each morning" in exp

    def test_warning_only_digest_is_warning(self):
        records = [rec(sev="WARNING")]
        sev, msg, _ = summarize_open_alerts(records, NOW)
        assert sev == "WARNING" and "0 critical / 1 warning" in msg

    def test_old_digest_rows_never_count_themselves(self):
        records = [rec(key=DIGEST_KEY, metric=DIGEST_METRIC, plant="PORTFOLIO")]
        assert summarize_open_alerts(records, NOW) is None


class TestApply:
    def test_opens_digest_and_notifier_will_mail_it(self):
        records = [rec()]
        res = apply_daily_digest(records, NOW)
        assert res.changed and res.opened is not None
        d = records[-1]
        # OPEN state + fresh id = exactly what the notifier mails once
        assert d.state == AlertState.OPEN
        assert d.metric == DIGEST_METRIC and d.alert_key == DIGEST_KEY
        assert d.alert_id.startswith("ALT-20260707-")

    def test_yesterdays_digest_resolved_todays_opened(self):
        records = [rec()]
        apply_daily_digest(records, NOW - dt.timedelta(days=1))
        res = apply_daily_digest(records, NOW)
        digests = [r for r in records if r.metric == DIGEST_METRIC]
        assert len(digests) == 2
        assert digests[0].state == AlertState.RESOLVED
        assert digests[1].state == AlertState.OPEN
        assert res.resolved_ids == [digests[0].alert_id]

    def test_all_clear_resolves_old_digest_and_stays_silent(self):
        records = [rec()]
        apply_daily_digest(records, NOW - dt.timedelta(days=1))
        records[0] = resolve_alert(records[0], NOW)   # real issue fixed
        res = apply_daily_digest(records, NOW)
        assert res.opened is None                     # silence restored
        assert all(r.state != AlertState.OPEN for r in records)

    def test_no_open_no_change_at_all(self):
        records = [resolve_alert(rec(), NOW)]
        res = apply_daily_digest(records, NOW)
        assert not res.changed and records[-1].metric != DIGEST_METRIC


class TestReportableAlerts:
    def test_digest_never_pollutes_report_counts(self):
        records = [rec()]
        apply_daily_digest(records, NOW)
        visible = reportable_alerts(records)
        assert len(visible) == 1
        assert visible[0].metric == "inverter_fault"
