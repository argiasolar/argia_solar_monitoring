"""v216 — inverter thermal health: bands, peer deviation, thermal stress
index, suspected derating with kWh loss, the derating curve and its
knee, the SQL builders. A synthetic plant of four 125 kW inverters: three
cool at ~95 % of rated, one hot that follows them until 65 °C and then
falls 12 % behind (the Plastic Omnium INV4 story)."""
from __future__ import annotations

import datetime as dt

import pytest

from argia.analytics import thermal as TH

UTC = dt.timezone.utc
RATED = {"INV1": 125.0, "INV2": 125.0, "INV3": 125.0, "INV4": 125.0}


def day(hot_sn="INV4", hot_peak=74.0, derate_pct=12.0, ambient=34.0, n=60):
    """n 5-minute intervals 11:00-16:00 UTC. Cool units 55-60 °C at 118 kW;
    the hot unit climbs from 58 to hot_peak and loses derate_pct once >= 65."""
    out = []
    t0 = dt.datetime(2026, 9, 5, 17, 0, tzinfo=UTC)
    for i in range(n):
        ts = t0 + dt.timedelta(minutes=5 * i)
        for sn in RATED:
            if sn == hot_sn:
                temp = 58.0 + (hot_peak - 58.0) * i / (n - 1)
                kw = 118.0 * (1 - derate_pct / 100.0) if temp >= TH.T_HOT else 118.0
            else:
                temp = 55.0 + (int(sn[-1]) % 3) * 2.0
                kw = 118.0
            out.append((ts, sn, kw * 1000.0, temp, ambient))
    return out


class TestBands:
    def test_bands(self):
        assert [TH.band(t) for t in (30, 49.9, 50, 59.9, 60, 64.9, 65, 69.9, 70, 74.9, 75, 90)] == [
            "normal", "normal", "watch", "watch", "warning", "warning", "high", "high",
            "critical", "critical", "critical", "critical"]
        assert TH.band(None) == "unknown"


class TestDerating:
    def test_hot_inverter_is_flagged_with_measured_loss(self):
        days = TH.evaluate_day(day(), RATED)
        h = days["INV4"]
        assert h.peak_c == 74.0 and h.band == "critical" and h.samples == 60
        assert h.minutes_over_65 > 0 and h.events == 1
        # while hot it runs 12 % below the cool peers' 118 kW -> ~14.2 kW lost per interval
        assert h.derating_minutes == h.minutes_over_65
        expected_lost = 0.12 * 118.0 * (h.derating_minutes / 60.0)
        assert h.lost_kwh == pytest.approx(expected_lost, rel=0.02)
        assert h.dt_peer_peak_c == pytest.approx(74.0 - 57.0, abs=0.2)   # peers median 57
        assert h.dt_ambient_peak_c == pytest.approx(40.0, abs=0.1)
        assert h.cooling_health == "POOR"
        # the cool units: no loss, GOOD
        for sn in ("INV1", "INV2", "INV3"):
            c = days[sn]
            assert c.lost_kwh == 0 and c.derating_minutes == 0 and c.minutes_over_65 == 0
            assert c.cooling_health == "GOOD" and c.band == "watch"

    def test_hot_but_producing_is_not_derating(self):
        days = TH.evaluate_day(day(derate_pct=0.0), RATED)
        h = days["INV4"]
        assert h.minutes_over_65 > 0 and h.derating_minutes == 0 and h.lost_kwh == 0
        assert h.cooling_health == "POOR"           # hotter than peers by >= 10 C is still a cooling problem

    def test_small_dc_field_is_corrected_by_the_baseline(self):
        # INV4 always makes 10 % less than its peers (smaller DC field), never hot
        smp = [(ts, sn, (kw * 0.9 if sn == "INV4" else kw), (56.0 if sn == "INV4" else t), a)
               for ts, sn, kw, t, a in day(derate_pct=0.0)]
        d = TH.evaluate_day(smp, RATED)["INV4"]
        assert d.derating_minutes == 0 and d.cool_ratio is None       # never hotter than peers -> no reference
        # now the same unit runs hot: without a baseline the 10 % looks like loss, with it it does not
        hot = [(ts, sn, (kw * 0.9 if sn == "INV4" else kw), t, a) for ts, sn, kw, t, a in day(derate_pct=0.0)]
        naive = TH.evaluate_day(hot, RATED)["INV4"]
        corrected = TH.evaluate_day(hot, RATED, baseline={"INV4": 0.9})["INV4"]
        assert naive.lost_kwh > 0 and corrected.lost_kwh == 0

    def test_no_cooler_peer_means_no_reference(self):
        # everyone hot together (ambient 45 C): temperature stats yes, derating no
        smp = [(ts, sn, kw, t + 15.0, 45.0) for ts, sn, kw, t, a in day(derate_pct=0.0)]
        days = TH.evaluate_day(smp, RATED)
        assert all(d.derating_minutes == 0 for d in days.values())
        assert days["INV4"].peak_c == 89.0 and days["INV4"].dt_ambient_peak_c == pytest.approx(44.0, abs=0.1)

    def test_dawn_noise_ignored_and_missing_samples_tolerated(self):
        smp = [(ts, sn, (kw * 0.1 if i < 5 else kw), t, a)
               for i, (ts, sn, kw, t, a) in enumerate(day())]
        smp = [s for s in smp if not (s[1] == "INV2" and s[0].minute == 30)]   # a missing tick
        smp.append((None, "INV1", 1.0, 50.0, 30.0))                             # garbage row
        smp.append((dt.datetime(2026, 9, 5, 17, 0, tzinfo=UTC), "GHOST", 1.0, 50.0, 30.0))   # not configured
        days = TH.evaluate_day(smp, RATED)
        assert "GHOST" not in days and days["INV2"].samples == 55      # 5 ticks (:30 of each hour) missing

    def test_events_count_contiguous_runs(self):
        smp = []
        t0 = dt.datetime(2026, 9, 5, 17, 0, tzinfo=UTC)
        for i in range(40):
            hot = i in range(5, 10) or i in range(20, 30)
            smp.append((t0 + dt.timedelta(minutes=5 * i), "INV1", 100000.0, 68.0 if hot else 55.0, 30.0))
        d = TH.evaluate_day(smp, {"INV1": 125.0})["INV1"]
        assert d.events == 2 and d.minutes_over_65 == 75 and d.cooling_health == "n/a"


class TestCurve:
    def test_knee_and_loss_above_65(self):
        days = TH.evaluate_day(day(), RATED)
        # three such days merged (a bin needs KNEE_MIN_N samples to be the knee)
        bins = TH.merge_bins((b, n, s) for _ in range(3) for b, (n, s, _mn) in days["INV4"].bins.items())
        # cool bins ratio ~1, hot bins ~0.88 -> knee at the first hot bin with enough samples
        c = TH.derating_curve(bins)
        assert c["knee_c"] is not None and c["knee_c"] >= 65.0
        assert c["ratio_above_65"] == pytest.approx(0.88, abs=0.01) and c["loss_above_65_pct"] == pytest.approx(12.0, abs=1.0)
        cool = [p for p in c["points"] if p["bin_c"] < 65]
        assert all(abs(p["ratio"] - 1.0) < 0.01 for p in cool)

    def test_no_knee_when_the_inverter_keeps_up(self):
        days = TH.evaluate_day(day(derate_pct=0.0), RATED)
        bins = TH.merge_bins((b, n, s) for b, (n, s, _mn) in days["INV4"].bins.items())
        c = TH.derating_curve(bins)
        assert c["knee_c"] is None and c["loss_above_65_pct"] == pytest.approx(0.0, abs=1.0)

    def test_thin_bins_cannot_be_the_knee(self):
        assert TH.derating_curve({70.0: (3, 2.4)})["knee_c"] is None

    def test_knee_is_relative_to_the_units_own_cool_level(self):
        # NL1 2026-09: a unit with more DC than its peers runs at 1.09 when cool
        # and falls to 0.97 at 80 C — a 11 % drop, a knee, although 0.97 > 0.97*1
        bins = {60.0: (65, 65 * 1.09), 62.5: (81, 81 * 1.09), 65.0: (97, 97 * 1.11), 70.0: (100, 100 * 1.06),
                75.0: (172, 172 * 1.05), 77.5: (303, 303 * 1.02), 80.0: (458, 458 * 0.966)}
        c = TH.derating_curve(bins)
        assert c["cool_level"] == pytest.approx(1.09, abs=0.01)
        assert c["knee_c"] == 75.0 and c["loss_above_65_pct"] == pytest.approx(7.0, abs=1.5)   # 1.05 < 1.09*0.97

    def test_baseline_from_history_is_clamped_median(self):
        b = TH.baseline_from_history([("A", 0.9), ("A", 0.92), ("A", 0.88), ("B", 3.0), ("C", None)])
        assert b == {"A": 0.9, "B": 1.3}


class TestSql:
    def test_upsert_replaces_the_day(self):
        days = TH.evaluate_day(day(), RATED)
        sqls = TH.build_upsert_sql("NL1", "2026-09-05", days)
        assert sqls[0].startswith("INSERT INTO thermal_daily") and "ON CONFLICT (plant_key, inverter_sn, prod_date) DO UPDATE" in sqls[0]
        assert "('NL1','INV4',DATE '2026-09-05'," in sqls[0] and "'critical','POOR'" in sqls[0]
        assert sqls[1] == "DELETE FROM thermal_bins WHERE plant_key='NL1' AND prod_date=DATE '2026-09-05';"
        assert sqls[2].startswith("INSERT INTO thermal_bins")
        assert TH.build_upsert_sql("NL1", "2026-09-05", {"X": TH.InverterDay(sn="X")}) == []

    def test_telemetry_sql_and_ts(self):
        q = TH.telemetry_sql("GTO1", "2026-09-05")
        assert "plant_key = 'GTO1'" in q and "DATE '2026-09-05'" in q and "/*tag:thermal_samples*/" in q
        assert TH.parse_ts("2026-09-05 17:35:00+00") == dt.datetime(2026, 9, 5, 17, 35, tzinfo=UTC)
        assert TH.parse_ts("garbage") is None
        assert "thermal_daily" in TH.ENSURE_SQL and "thermal_bins" in TH.ENSURE_SQL


class TestAskTool:
    def test_get_thermal_health_totals_and_curve(self):
        import re
        from argia.ask import tools as T
        TAG = re.compile(r"/\*tag:(\w+)\*/")

        class FakeDB:
            def __init__(self, data):
                self.data, self.sql = data, []

            def __call__(self, sql):
                m = TAG.search(sql); assert m, sql[:80]
                self.sql.append(sql)
                return self.data.get(m.group(1), [])
        db = FakeDB({"plants": [["NL1", "Plastic Omnium", "GROWATT", "500.0", "PPA", "t", "2.1", "0.8"]],
                     "freshness": [["2026-09-04 16:05:00+00", "2026-09-03"]],
                     "thermal": [["NL1", "INV4", "30", "76.3", "4404", "600", "126", "13.1", "38.0", "4404", "812.4", "12", "5"],
                                 ["NL1", "INV1", "30", "61.0", "0", "0", "0", "1.2", "26.0", "0", "0", "0", "0"]],
                     "thermal_bins": [["INV4", "60.0", "40", "39.6"], ["INV4", "67.5", "30", "26.1"], ["INV4", "70.0", "25", "21.5"]]})
        out = T.run_tool(db, "get_thermal_health", {"plant": "NL1", "date_from": "2026-08-06", "date_to": "2026-09-05"})
        assert out["inverters"][0]["inverter_sn"] == "INV4" and out["inverters"][0]["band"] == "critical"
        assert out["inverters"][0]["hours_over_65"] == 73.4 and out["inverters"][0]["lost_kwh"] == 812.4
        assert out["totals"] == {"inverters": 2, "hours_over_65": 73.4, "events": 126, "derating_hours": 73.4, "lost_kwh": 812.4}
        assert out["derating_curve"]["knee_c"] == 67.5 and out["derating_curve"]["loss_above_65_pct"] == pytest.approx(13.0, abs=0.5)
        assert "not warranty limits" in out["note"]


class TestWiring:
    def test_acute_rule_is_graded(self):
        from argia.analytics import acute as A
        assert (A.TEMP_WARN_C, A.TEMP_HIGH_C, A.TEMP_CRIT_C, A.TEMP_PEER_DT_C) == (65.0, 70.0, 75.0, 5.0)
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alerts_snapshot.py").read_text(encoding="utf-8")
        assert "rated_kw=rated" in src

    def test_pages_carry_the_thermal_card(self):
        import pathlib
        v2 = pathlib.Path(__file__).resolve().parents[2]
        mg = (v2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
        assert "def thermal_card(pk, d):" in mg and "{thermal_card(pk, d)}" in mg and "FROM thermal_daily" in mg
        rg = (v2 / "server" / "bundle" / "report_gen.py").read_text(encoding="utf-8")
        assert "def thermal_card(k):" in rg and "parts['thermal'] = thermal_card(k)" in rg
        assert "not manufacturer warranty limits" in rg

    def test_job_unit_and_mail_lists(self):
        import pathlib
        v2 = pathlib.Path(__file__).resolve().parents[2]
        assert "thermal_daily.py" in (v2 / "server" / "bundle" / "argia-thermal.service").read_text(encoding="utf-8")
        assert "01:10:00 America/Mexico_City" in (v2 / "server" / "bundle" / "argia-thermal.timer").read_text(encoding="utf-8")
        assert '"argia-thermal"' in (v2 / "scripts" / "alert_mailer.py").read_text(encoding="utf-8")
        from argia.ask import sqltool as S
        assert "thermal_daily" in S.ALLOWED_TABLES and "thermal_daily" in S.TABLE_NOTES


class TestJobWiring:
    def test_script_backfills_oldest_first_and_has_a_report_mode(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "thermal_daily.py").read_text(encoding="utf-8")
        assert "for back in range(a.days_back - 1, -1, -1):" in src
        assert 'ap.add_argument("--report"' in src and "TH.derating_curve(TH.merge_bins(lst))" in src
        assert "BASELINE_DAYS = 30" in src
