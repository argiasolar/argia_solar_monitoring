"""v313: a 0 W reading whose day counter kept growing is not a dark plant.

6 Oct 2026 10:30 MX: "CRITICAL plant produced nothing" for MEX2. The
telemetry: 10:15 all three inverters at ~59 kW; 10:20 an exact repeat of
10:15 (frozen, dropped by v303); 10:25 all three at 0 W while each day
counter rose ~5.2 kWh (110.6 -> 115.8 in 10 min = ~31 kW average);
10:30 back at 64 kW. The acute job ran at 10:30:11, one sample before
the recovery, and believed the zeros. Over 30 days 27 of the 56
"whole plant at 0 W" snapshots had growing counters. Rule now: a zero
whose own counter grew since the previous sample (at most 20 min back)
says the plant produced; a real trip shows up one sample later, when
the counter stops.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

from argia.analytics import acute as A
from argia.analytics.acute import counter_grew, day_counters, evaluate_acute
from argia.core.time_utils import MX_TZ, UTC

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))


def mx(h, m):
    return dt.datetime(2026, 10, 6, h, m, 20, tzinfo=MX_TZ).astimezone(UTC)


# (minute, power_w, etoday) per inverter, the real MEX2 shape (serials neutral)
MEX2_6OCT = {
    "A": [(15, 59277.0, 110.63), (25, 0.0, 115.80), (30, 63930.0, 117.83)],
    "B": [(15, 59641.0, 110.66), (25, 0.0, 115.86), (30, 64203.0, 118.04)],
    "C": [(15, 59223.0, 111.02), (25, 0.0, 116.21), (30, 63273.0, 118.24)],
}


def rows8(table, until_min):
    """8-field samples (ts, plant, sn, power, temp, status, fault, etoday)."""
    return [(mx(10, m), "MEX2", sn, pw, 42.0, 1, "", e)
            for sn, seq in table.items() for m, pw, e in seq if m <= until_min]


def offline(rows, now):
    return [b for b in evaluate_acute([r[:7] for r in rows], ["MEX2"], now, counters=day_counters(rows))
            if b.metric == "plant_offline"]


class TestTheSixOctoberCase:
    def test_the_1030_run_no_longer_alerts(self):
        assert offline(rows8(MEX2_6OCT, 25), mx(10, 30)) == []

    def test_without_counters_the_old_rule_still_fires(self):
        """Documents why it fired: the power field alone said 0 W."""
        rows = [r[:7] for r in rows8(MEX2_6OCT, 25)]
        assert [b.metric for b in evaluate_acute(rows, ["MEX2"], mx(10, 30))] == ["plant_offline"]


class TestRealOutagesStillAlert:
    def test_zero_with_a_flat_counter_is_dark(self):
        flat = {sn: [(15, 59000.0, 110.0), (25, 0.0, 110.0)] for sn in "ABC"}
        assert len(offline(rows8(flat, 25), mx(10, 30))) == 1

    def test_a_trip_just_after_a_sample_alerts_one_sample_later(self):
        """Tripped at 10:24: the 10:25 counter still grew (no alert), the
        10:30 counter is flat (alert at the 10:35 run)."""
        trip = {sn: [(15, 59000.0, 110.0), (25, 0.0, 114.0), (30, 0.0, 114.0)] for sn in "ABC"}
        assert offline(rows8(trip, 25), mx(10, 30)) == []
        assert len(offline(rows8(trip, 30), mx(10, 35))) == 1

    def test_a_previous_reading_older_than_20_min_proves_nothing(self):
        gap = {sn: [(0, 50000.0, 100.0), (25, 0.0, 115.0)] for sn in "ABC"}
        assert len(offline(rows8(gap, 25), mx(10, 30))) == 1

    def test_counter_noise_below_0_05_kwh_is_flat(self):
        noise = {sn: [(15, 59000.0, 110.00), (25, 0.0, 110.04)] for sn in "ABC"}
        assert len(offline(rows8(noise, 25), mx(10, 30))) == 1

    def test_one_growing_counter_is_enough_to_say_the_plant_was_not_dark(self):
        mixed = {"A": [(15, 59000.0, 110.0), (25, 0.0, 115.0)],
                 "B": [(15, 59000.0, 110.0), (25, 0.0, 110.0)]}
        assert offline(rows8(mixed, 25), mx(10, 30)) == []


class TestHelpers:
    def test_day_counters_skips_missing_counters_and_short_rows(self):
        t = mx(10, 0)
        c = day_counters([(t, "P", "A", 0.0, None, 1, "", None),
                          (t, "P", "B", 0.0, None, 1, ""),
                          (t + dt.timedelta(minutes=5), "P", "C", 1.0, None, 1, "", 2.5),
                          (t, "P", "C", 1.0, None, 1, "", 2.0)])
        assert c == {("P", "C"): [(t, 2.0), (t + dt.timedelta(minutes=5), 2.5)]}

    def test_counter_grew_needs_a_reading_at_that_moment_and_one_before(self):
        t = mx(10, 0)
        c = {("P", "A"): [(t, 1.0), (t + dt.timedelta(minutes=5), 1.5)]}
        assert counter_grew(c, "P", "A", t + dt.timedelta(minutes=5)) is True
        assert counter_grew(c, "P", "A", t) is False                     # nothing before
        assert counter_grew(c, "P", "A", t + dt.timedelta(minutes=9)) is False   # no reading then
        assert counter_grew(None, "P", "A", t) is False
        assert (A.COUNTER_GAP_MAX_MIN, A.COUNTER_GROWTH_KWH) == (20, 0.05)


class TestWiring:
    def test_the_snapshot_job_reads_and_passes_the_counters(self, monkeypatch):
        import alerts_snapshot as S
        from argia.telemetry import pg_source
        hdr = ["timestamp_utc", "plant_key", "inverter_sn", "power_w", "temperature_c", "status", "fault_code", "etoday_kwh"]
        grid = [hdr] + [[ts.isoformat(), p, sn, pw, tc, st, f, e] for ts, p, sn, pw, tc, st, f, e in rows8(MEX2_6OCT, 25)]
        monkeypatch.setattr(pg_source, "source", lambda env=None: "pg")
        monkeypatch.setattr(pg_source, "read_grid", lambda **kw: grid)
        got: dict = {}
        samples, _ = S._read_recent_samples(None, counters_out=got)
        assert all(len(s) == 7 for s in samples)
        assert got[("MEX2", "A")] == [(mx(10, 15), 110.63), (mx(10, 25), 115.80)]
        assert [b for b in evaluate_acute(samples, ["MEX2"], mx(10, 30), counters=got)
                if b.metric == "plant_offline"] == []
        src = (V2 / "scripts" / "alerts_snapshot.py").read_text(encoding="utf-8")
        assert "_read_recent_samples(sheets, counters_out=counters)" in src
        assert "counters=counters)" in src


def test_month_edge_is_capped_today():
    """Pins current behaviour found while running the suite on 6 Oct (the
    catch-up e2e test put its gap on 30 Sep): energy proven only by the NEXT
    month's lifetime counter is not credited to the month's last day - v305
    caps credit to the month's own lifetime step. Known, open, not changed
    here (money rule: needs Tomasz's OK)."""
    from argia.analytics import losses as L
    snaps = [("2026-09-29", 100.0, 1000.0), ("2026-09-30", 30.0, 1030.0), ("2026-10-01", 100.0, 1200.0)]
    credit = L.allocate_catch_up(L.catch_up(snaps), {"2026-09-30": 80.0})
    assert credit == {"2026-09-30": 70.0}
    counted = {"2026-09-29": 100.0, "2026-09-30": 30.0, "2026-10-01": 100.0}
    assert L.cap_to_month(credit, L.month_budget(snaps, counted)).get("2026-09-30", 0.0) == 0.0
