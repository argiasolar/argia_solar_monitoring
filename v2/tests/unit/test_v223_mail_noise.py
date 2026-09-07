"""v223 — less mail, each one carrying something (Tomasz, 2026-09-07):

* 2026-09-06 13:55-14:35 MX every Growatt plant went blank at once
  (PostgreSQL on pio06 unreachable for 35 min) and the daily silent rule
  blamed seven inverters at two plants -> fleet-wide blanks are the
  collector's, never an inverter's;
* 06:07 and 06:37 MX the infra mailer called SAG "CRITICAL: no
  telemetry today" (twice: the first-time INSERT never stamped
  last_sent) — plant conditions leave that mailer; the ledger owns them,
  with a WARNING for a data gap;
* a FusionSolar row with no power, no counter and no temperature is no
  sample for the acute tier;
* CRITICAL = energy lost or a unit off — "no telemetry" is a WARNING;
* the daily_digest pseudo-alert is retired (the morning mail carries the
  still-open reminder itself).
"""
from __future__ import annotations

import datetime as dt
import pathlib

from argia.analytics.inverter_health import Severity
from argia.analytics.silent import (COLLECTOR_OVERLAP, collector_windows, evaluate_silent_gaps,
                                    inside_collector_windows)
from argia.core.time_utils import MX_TZ, UTC

V2 = pathlib.Path(__file__).resolve().parents[2]


def mx(h, m=0, day=6):
    return dt.datetime(2026, 9, day, h, m, tzinfo=MX_TZ).astimezone(UTC)


def _fleet(blank_from=(13, 55), blank_to=(14, 35), plants=("GTO1", "NL1", "SLP1", "SLP2", "NL2", "MEX3"), survivors=("GTO2",)):
    """Timestamps per plant, 09:00-17:00 every 5 min; ``plants`` go blank
    in the window, ``survivors`` (the SolarEdge job) keep reporting."""
    a, b = mx(*blank_from), mx(*blank_to)
    out = {}
    for pk in plants + survivors:
        ts = []
        t = mx(9, 0)
        while t < mx(17, 0):
            if pk in survivors or not (a <= t < b):
                ts.append(t)
            t += dt.timedelta(minutes=5)
        out[pk] = ts
    return out


class TestCollectorWindows:
    def test_fleet_wide_blank_is_one_window(self):
        w = collector_windows(_fleet())
        assert w == [(mx(13, 55), mx(14, 35))]

    def test_one_plant_blank_is_not_a_collector_window(self):
        f = _fleet(plants=("GTO1",), survivors=("NL1", "SLP1", "SLP2", "GTO2", "NL2"))
        assert collector_windows(f) == []

    def test_needs_two_plants_and_tolerates_empty(self):
        assert collector_windows({"GTO1": [mx(10)]}) == []
        assert collector_windows({}) == []
        assert collector_windows({"A": [], "B": []}) == []

    def test_overlap_rule(self):
        w = [(mx(13, 55), mx(14, 35))]
        assert inside_collector_windows(mx(13, 50), mx(14, 35), w)          # the Taigene gaps: 13:50/13:51 -> 14:35/14:36
        assert inside_collector_windows(mx(13, 51), mx(14, 36), w)
        assert not inside_collector_windows(mx(13, 0), mx(14, 35), w)       # started an hour before the blank
        assert not inside_collector_windows(mx(14, 20), mx(19, 59), w)      # the SLP2 real link drop
        assert not inside_collector_windows(mx(13, 55), mx(14, 35), [])
        assert 0.5 < COLLECTOR_OVERLAP <= 1.0


def _plant_day(gap_from, gap_to, sn="A", sib="B"):
    """One plant: A blank in [gap_from, gap_to), B reporting all day at
    100 kW with a climbing counter; A's counter climbs the same."""
    rows = []
    t = mx(9, 0)
    c = 0.0
    while t < mx(17, 0):
        c += 8.0
        rows.append((t, sib, c, 100000.0))
        if not (gap_from <= t < gap_to):
            rows.append((t, sn, c, 100000.0))
        t += dt.timedelta(minutes=5)
    return rows


class TestSilentRuleIgnoresCollectorBlanks:
    RATED = {"A": 125.0, "B": 125.0}

    def test_gap_inside_the_fleet_blank_is_not_an_alert(self):
        rows = _plant_day(mx(13, 55), mx(14, 35))
        # without the fleet context the old rule fires (comms, WARNING)
        old = evaluate_silent_gaps("GTO1", rows, self.RATED, mx(20, 0))
        assert len(old) == 1 and old[0].kind == "comms"
        # with it: nothing — the whole fleet was blank
        assert evaluate_silent_gaps("GTO1", rows, self.RATED, mx(20, 0),
                                    collector=collector_windows(_fleet())) == []

    def test_a_real_gap_still_fires_next_to_a_fleet_blank(self):
        rows = _plant_day(mx(11, 0), mx(12, 30))
        b = evaluate_silent_gaps("GTO1", rows, self.RATED, mx(20, 0), collector=collector_windows(_fleet()))
        assert len(b) == 1 and b[0].inverter_sn == "A" and "10:55-12:30 MX" in b[0].message   # gap = last seen -> back

    def test_daily_script_feeds_the_fleet_windows(self):
        src = (V2 / "scripts" / "alerts_daily.py").read_text(encoding="utf-8")
        assert "windows = collector_windows(" in src and "collector=windows" in src
        assert "collector blank %s-%s MX (fleet-wide)" in src


class TestSeverityPolicy:
    def test_no_telemetry_day_is_a_warning(self):
        from argia.analytics.data_health import evaluate_data_stale
        b = evaluate_data_stale({}, ["MEX1"], "2026-09-06")
        assert b[0].severity is Severity.WARNING and "production unknown" in b[0].message

    def test_acute_severities_unchanged_where_energy_is_at_stake(self):
        from argia.analytics import acute as A
        assert A.SILENT_CRIT_MIN == 180 and A.TEMP_HIGH_C == 70.0     # off for 3 h / measured loss stay CRITICAL

    def test_explanations_state_the_rule(self):
        from argia.alerts import ledger_mail as LM
        subj, text, html = LM.render_mail([])
        assert "CRITICAL = energy being lost or a unit off" in html


class TestAcuteUsableRows:
    def test_empty_vendor_row_is_no_sample(self):
        import sys
        sys.path.insert(0, str(V2 / "scripts"))
        import alerts_snapshot as S
        assert not S.usable_sample(None, None, None)
        assert S.usable_sample(0.0, None, None) and S.usable_sample(None, 12.5, None) and S.usable_sample(None, None, 41.0)
        src = (V2 / "scripts" / "alerts_snapshot.py").read_text(encoding="utf-8")
        assert 'if not usable_sample(pw, etoday, safe_float(cell("temperature_c"))):' in src

    def test_a_plant_of_empty_rows_ages_into_data_stale(self):
        from argia.analytics.acute import evaluate_acute
        now = mx(10, 0, day=7)
        # MEX2 fresh; MEX1 absent from the tail (its rows were dropped as unusable)
        tail = [(now - dt.timedelta(minutes=5 * k), "MEX2", "X", 50000.0, 40.0, 1, "0") for k in range(36)]
        b = evaluate_acute(tail, ["MEX1", "MEX2"], now, absent_gap_hours=3.0)
        assert [(x.plant_key, x.metric, x.severity) for x in b] == [("MEX1", "data_stale", Severity.WARNING)]


class TestInfraMailerScope:
    def test_plant_rules_left_the_mailer(self):
        src = (V2 / "scripts" / "alert_mailer.py").read_text(encoding="utf-8")
        assert "monitor.plant_alerts(" not in src and "monitor.inverter_alerts(" not in src
        assert "gather_freshness" not in src and "gather_silent_inverters" not in src
        assert "monitor.infra_alerts(" in src and "monitor.drift_alerts(" in src

    def test_first_time_key_remembers_it_was_mailed(self):
        src = (V2 / "scripts" / "alert_mailer.py").read_text(encoding="utf-8")
        assert "INSERT INTO alert_state (key, severity, last_sent) VALUES" in src
        assert "{'now()' if mailed else 'NULL'}" in src
        assert "sent_keys.extend(a.key for a in dropped)" in src


class TestDigestRetired:
    def test_resolve_only(self):
        from argia.alerts.digest import DIGEST_KEY, DIGEST_METRIC, resolve_digest_rows
        from argia.alerts.engine import make_alert_id
        from argia.core.alerts_state import open_alert
        now = dt.datetime(2026, 9, 7, 12, 30, tzinfo=UTC)
        recs = [open_alert(alert_id=make_alert_id(now, 1), alert_key=DIGEST_KEY, plant_key="PORTFOLIO", inverter_sn="",
                           metric=DIGEST_METRIC, severity="CRITICAL", now_utc=now - dt.timedelta(days=1),
                           value=None, threshold=None, message="d", explanation=""),
                open_alert(alert_id=make_alert_id(now, 2), alert_key="gto1:plant:energy_daily_pct", plant_key="GTO1",
                           inverter_sn="", metric="energy_daily_pct", severity="CRITICAL", now_utc=now,
                           value=1.0, threshold=None, message="m", explanation="")]
        res = resolve_digest_rows(recs, now)
        assert res.resolved_ids == [recs[0].alert_id] and res.opened is None
        assert not recs[0].is_open() and recs[1].is_open()
        assert resolve_digest_rows(recs, now).changed is False

    def test_daily_script_no_longer_opens_a_digest_alert(self):
        src = (V2 / "scripts" / "alerts_daily.py").read_text(encoding="utf-8")
        assert "apply_daily_digest" not in src and "resolve_digest_rows(result.records, now_utc)" in src
