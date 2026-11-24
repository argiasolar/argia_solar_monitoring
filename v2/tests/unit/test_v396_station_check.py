"""v396 - Arturo's methodology R10 (daily weather-station check) and the R2 station gate.
Synthetic data in the shape of the production findings (SAG sensor drifting warmer, a
station shared by three plants, a plant with no station)."""
from __future__ import annotations

import datetime as dt
import pathlib

from argia.analytics import station_check as SC

V2 = pathlib.Path(__file__).resolve().parents[2]


def _days(start, n):
    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]


class TestDrift:
    def test_a_sensor_drifting_warmer_than_the_fleet_fails(self):
        days = _days("2026-07-01", 140)
        sag = {d: 19.6 + 8.0 * i / 139 for i, d in enumerate(days)}           # +8 K over the season
        fleet = {f"S{j}": {d: 25.0 - 1.0 * i / 139 for i, d in enumerate(days)} for j in range(4)}
        dr = SC.drift_series({"SAG": sag, **fleet})
        assert dr[("SAG", days[-1])] > SC.DRIFT_MAX_K
        assert all(abs(dr[(s, days[-1])]) < 2 for s in fleet)

    def test_a_late_member_of_a_shared_station_inherits_the_group_reference(self):
        days = _days("2026-07-01", 140)
        sag = {d: 19.6 + 8.0 * i / 139 for i, d in enumerate(days)}
        sms = {d: v for d, v in list(sag.items())[70:]}                        # joined in September
        fleet = {f"S{j}": {d: 25.0 for d in days} for j in range(3)}
        shared = {"SAG": ("SMS",), "SMS": ("SAG",)}
        dr = SC.drift_series({"SAG": sag, "SMS": sms, **fleet}, shared)
        assert dr[("SMS", days[-1])] == dr[("SAG", days[-1])] > SC.DRIFT_MAX_K

    def test_shared_station_detection(self):
        a = {d: 5.0 + i * 0.1 for i, d in enumerate(_days("2026-11-01", 10))}
        g = SC.shared_groups({"MEX1": a, "MEX2": dict(a), "MEX3": dict(a), "GTO1": {d: v + 0.3 for d, v in a.items()}})
        assert g == {"MEX1": ("MEX2", "MEX3"), "MEX2": ("MEX1", "MEX3"), "MEX3": ("MEX1", "MEX2")}


class TestJudge:
    def test_coverage_gate(self):
        c = SC.judge("GTO2", "2026-11-20", 0.08, None, 0, "OK", None, None)
        assert c.status == "fail" and c.reasons == ("coverage 8.0% < 90%",)
        assert SC.judge("X", "d", 0.9, None, 0, "", None, None).status == "ok"
        assert SC.judge("X", "d", None, None, 0, "", None, None).status == "no_data"

    def test_satellite_review_is_information_not_failure(self):
        c = SC.judge("NL1", "d", 1.0, -0.3, 7, "REVIEW", 15.0, 0.5)
        assert c.status == "ok" and c.sat_status == "REVIEW"

    def test_coupling_needs_a_window_of_judged_days(self):
        daily = {"2026-11-20": (0.5, 40, 12.0), "2026-11-19": (0.6, 40, 12.0), "2026-11-18": (0.7, 40, 12.0)}
        slope, n = SC.coupling_window(daily, "2026-11-20")
        assert n == 3 and SC.judge("X", "d", 1.0, slope, n, "", None, None).status == "ok"     # 3 days: not enough
        daily["2026-11-17"] = (0.4, 40, 12.0)
        daily["2026-11-16"] = (-0.3, 5, 12.0)          # too few samples: not judged
        slope, n = SC.coupling_window(daily, "2026-11-20")
        c = SC.judge("X", "d", 1.0, slope, n, "", None, None)
        assert n == 4 and c.status == "fail" and "not coupled" in c.reasons[0]
        coupled = {f"2026-11-{d}": (-0.28, 40, 12.0) for d in range(14, 21)}
        assert SC.judge("X", "d", 1.0, *SC.coupling_window(coupled, "2026-11-20"), "", None, None).status == "ok"

    def test_drift_reason(self):
        c = SC.judge("MEX1", "d", 1.0, -0.25, 6, "OK", 27.2, 7.6, ("MEX2", "MEX3"))
        assert c.status == "fail" and c.reasons == ("module-ambient +7.6 K vs its own start and the fleet",)

    def test_upsert(self):
        sql = SC.upsert_sql([SC.judge("MEX1", "2026-11-20", 1.0, -0.25, 6, "OK", 27.2, 7.6, ("MEX2",))])
        assert "CREATE TABLE IF NOT EXISTS station_check_daily" in sql and "ON CONFLICT (plant_key, prod_date)" in sql
        assert "('MEX1', DATE '2026-11-20', 1.0000, -0.2500, 'OK', 27.20, 7.60, 'MEX2', 'fail'," in sql
        assert SC.upsert_sql([]) == ""


class TestWiring:
    def test_report_and_kpi(self):
        rg = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")
        assert "s.coverage < 0.9" in rg and "{_ST_OK} GROUP BY plant_key" in rg and "def station_tile(k):" in rg
        assert "station_check_for_recent_days(args.dry_run)" in (V2 / "scripts/kpi_eod.py").read_text(encoding="utf-8")
