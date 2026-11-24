"""v397 - Arturo's methodology R11: soiling measured between clean events (rain, logged
cleanings), the gain of each event, and the rain log. Synthetic data in the shape of
Vitalmex / SAG in November 2026."""
from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from argia.analytics import soiling_measured as S
from argia.kpi import satellite as SAT

V2 = pathlib.Path(__file__).resolve().parents[2]


def _days(start, n):
    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]


def _index(values, start="2026-11-01"):
    return dict(zip(_days(start, len(values)), values))


class TestMeasure:
    def test_vitalmex_shaped_month(self):
        # dirty 0.75 until the rain of 12 Nov, 0.88 after, then 1 %/day of soiling
        vals = [0.75] * 11 + [None] + [0.88, 0.88, 0.88] + [0.88 * (1 - 0.01 * i) for i in range(1, 9)]
        idx = {d: v for d, v in _index(vals).items() if v is not None}
        m = S.measure(idx, {"2026-11-12": "rain"}, "2026-11-23")
        assert m.last_event.day == "2026-11-12" and m.reference == pytest.approx(0.88)
        assert m.events[-1].gain == pytest.approx(0.88 / 0.75 - 1, abs=1e-4)     # +17.3 %
        assert m.loss == pytest.approx(1 - 0.88 * 0.93 / 0.88, abs=1e-3)       # median of the last 3 days
        assert m.days_since == 11 and m.rate_per_day > 0

    def test_needs_six_valid_days_after_the_event(self):
        idx = _index([0.8] * 5 + [0.9] * 4)
        m = S.measure(idx, {"2026-11-05": "rain"}, "2026-11-09")
        assert m.reference == 0.9 and m.loss is None and m.current is None

    def test_a_fresh_rain_is_measured_not_replaced_by_an_older_event(self):
        # Taigene-shaped, 24 Nov: rain on 10 Oct and on the data edge (23 Nov) - the 23 Nov rain
        # is the last event; with no valid day after it the level is still being measured
        idx = _index([0.96] * 3 + [0.9] * 40, start="2026-10-11")
        m = S.measure(idx, {"2026-10-10": "rain", "2026-11-23": "rain"}, "2026-11-23")
        assert m.last_event.day == "2026-11-23" and m.reference is None and m.loss is None and m.days_since == 0

    def test_no_event_no_number(self):
        m = S.measure(_index([0.8] * 10), {}, "2026-11-10")
        assert m.last_event is None and m.loss is None and m.events == ()

    def test_gain_needs_both_sides(self):
        m = S.measure(_index([0.8, 0.8, 0.9, 0.9, 0.9]), {"2026-11-02": "cleaning"}, "2026-11-05")
        assert m.events[0].gain is None and m.events[0].kind == "cleaning"


class TestInputs:
    def test_valid_days(self):
        rows = {"a": (0.8, 4.0, 1.0, "shinemaster_history", False), "b": (0.8, 2.0, 1.0, "x", False),
                "c": (0.8, 4.0, 0.9, "x", False), "d": (0.8, 4.0, 1.0, "cloud_cover_model", False),
                "e": (0.8, 4.0, 1.0, "x", True), "f": (None, 4.0, 1.0, "x", False), "g": (0.8, 4.0, None, "proxy:MEX1", False)}
        assert S.valid_days(rows) == {"a": 0.8, "g": 0.8}

    def test_events(self):
        ev = S.clean_events({"d1": 7.0, "d2": 4.9, "d3": 12.0}, {"d3": "cleaning", "d4": "cleaning"})
        assert ev == {"d1": "rain", "d3": "cleaning", "d4": "cleaning"}

    def test_rain_from_the_same_open_meteo_call(self):
        assert "precipitation_sum" in SAT.build_url(19.4, -99.1)
        pay = {"daily_units": {"precipitation_sum": "mm"}, "daily": {"time": ["2026-11-12", "2026-11-13"],
                                                                     "precipitation_sum": [7.04, None]}}
        assert SAT.parse_daily_rain(pay) == {"2026-11-12": 7.0}
        assert SAT.parse_daily_rain({"daily_units": {"precipitation_sum": "inch"}, "daily": pay["daily"]}) == {}
        assert SAT.parse_daily_rain({}) == {}

    def test_rain_upsert(self):
        import importlib.util
        import sys
        sys.path.insert(0, str(V2 / "scripts"))
        spec = importlib.util.spec_from_file_location("sat397", V2 / "scripts" / "satellite_check.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert m.rain_sql("MEX1", {"2026-11-12": 7.0}) == (
            "INSERT INTO weather_daily (plant_key, day, rain_mm) VALUES ('MEX1', DATE '2026-11-12', 7.0)"
            " ON CONFLICT (plant_key, day) DO UPDATE SET rain_mm = EXCLUDED.rain_mm;")
        assert "CREATE TABLE IF NOT EXISTS weather_daily" in m.RAIN_DDL


def test_report_and_setup_wiring():
    rg = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")
    assert "def soiling_tile(k):" in rg and "tiles.append(soiling_tile(k))" in rg and "category = 'cleaning'" in rg
    su = (V2 / "server/bundle/setup_app.py").read_text(encoding="utf-8")
    assert "MAINT_CATEGORIES = ('customer', 'argia', 'force_majeure', 'cleaning')" in su
    assert "e.get('cat') != 'customer'" in su          # only customer events are ever billed
