"""Tests: dense ShineMaster history (argia.meteo.growatt_env) and its
integration entry in the irradiance module.

Ground truth for shapes comes from the proven v1 scripts: obj.datas rows
with a 0-BASED-month `calendar` dict and `radiant` W/m²; obj.haveNext /
obj.start paginate; v1's stall guard advances start when the API repeats
itself.
"""

import datetime as dt

import pytest

from argia.kpi.irradiance import (
    IrradianceSource,
    MAX_PLAUSIBLE_WM2,
    integrate_history_points,
)
from argia.meteo.growatt_env import (
    DEFAULT_ENV_ADDR,
    calendar_to_dt,
    fetch_env_day,
    fetch_env_day_auto,
    parse_env_history_page,
    parse_env_list,
    pick_env_device,
)


def cal(y, m1, d, hh, mm):
    """Build a Growatt calendar dict from a 1-based month."""
    return {"year": y, "month": m1 - 1, "dayOfMonth": d,
            "hourOfDay": hh, "minute": mm, "second": 0}


class TestCalendar:
    def test_month_is_zero_based(self):
        assert calendar_to_dt(cal(2026, 7, 5, 13, 30)) == \
            dt.datetime(2026, 7, 5, 13, 30)

    def test_garbage_returns_none(self):
        assert calendar_to_dt(None) is None
        assert calendar_to_dt({"year": 2026}) is None
        assert calendar_to_dt("2026-07-05") is None


class TestParsePage:
    def test_points_extracted_and_bad_rows_skipped(self):
        js = {"obj": {"datas": [
            {"calendar": cal(2026, 7, 5, 8, 0), "radiant": "412.5"},
            {"calendar": cal(2026, 7, 5, 8, 1), "radiant": -5},   # clamped>=0
            {"calendar": {"broken": 1}, "radiant": 100},          # bad ts
            {"radiant": 100},                                     # no calendar
            {"calendar": cal(2026, 7, 5, 8, 2)},                  # no radiant
            {"calendar": cal(2026, 7, 5, 8, 3), "radiant": "n/a"},
        ], "haveNext": False}}
        points, have_next, nxt = parse_env_history_page(js)
        assert points == [(dt.datetime(2026, 7, 5, 8, 0), 412.5),
                          (dt.datetime(2026, 7, 5, 8, 1), 0.0)]
        assert have_next is False and nxt is None

    def test_pagination_fields(self):
        js = {"obj": {"datas": [], "haveNext": True, "start": 17}}
        _, have_next, nxt = parse_env_history_page(js)
        assert have_next is True and nxt == 17

    def test_malformed_envelope(self):
        for js in (None, {}, {"obj": None}, {"obj": {"datas": None}}, []):
            assert parse_env_history_page(js) == ([], False, None)


class FakeWeb:
    """Scripted get_env_history responses."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_env_history(self, plant_id, sn, addr, day, start):
        self.calls.append(start)
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]

    def seed_env_page(self, plant_id):
        self.seeded = getattr(self, "seeded", []) + [plant_id]

    def get_env_list(self, plant_id, curr_page=1):
        return getattr(self, "env_list", {"datas": []})


def page(rows, have_next=False, start=None):
    obj = {"datas": rows, "haveNext": have_next}
    if start is not None:
        obj["start"] = start
    return {"obj": obj}


class TestFetchEnvDay:
    def test_two_pages_merged_sorted_deduped(self):
        web = FakeWeb([
            page([{"calendar": cal(2026, 7, 5, 8, 1), "radiant": 200},
                  {"calendar": cal(2026, 7, 5, 8, 0), "radiant": 100}],
                 have_next=True, start=2),
            page([{"calendar": cal(2026, 7, 5, 8, 1), "radiant": 200},  # dup
                  {"calendar": cal(2026, 7, 5, 8, 2), "radiant": 300}]),
        ])
        pts = fetch_env_day(web, "P1", "SN", DEFAULT_ENV_ADDR,
                            "2026-07-05", sleep_s=0)
        assert pts == [(dt.datetime(2026, 7, 5, 8, 0), 100.0),
                       (dt.datetime(2026, 7, 5, 8, 1), 200.0),
                       (dt.datetime(2026, 7, 5, 8, 2), 300.0)]
        assert web.calls == [0, 2]

    def test_stall_guard_advances_start(self):
        """API echoing the same start must not loop forever (v1 lesson)."""
        stuck = page([{"calendar": cal(2026, 7, 5, 8, 0), "radiant": 1}],
                     have_next=True, start=0)
        web = FakeWeb([stuck, stuck, page([], have_next=False)])
        pts = fetch_env_day(web, "P1", "SN", 32, "2026-07-05", sleep_s=0)
        assert len(pts) == 1
        assert web.calls[0] == 0 and web.calls[1] > 0   # advanced despite echo

    def test_max_pages_cap(self):
        endless = page([{"calendar": cal(2026, 7, 5, 8, 0), "radiant": 1}],
                       have_next=True, start=0)
        web = FakeWeb([endless])
        fetch_env_day(web, "P1", "SN", 32, "2026-07-05", max_pages=5, sleep_s=0)
        assert len(web.calls) == 5


class TestHistoryIntegration:
    def _minutely(self, wm2, minutes=120):
        base = dt.datetime(2026, 7, 5, 10, 0)
        return [(base + dt.timedelta(minutes=i), wm2)
                for i in range(minutes)]

    def test_constant_series_integrates_exactly(self):
        # 800 W/m² for ~2 h -> ~1.587 kWh/m² (trapezoid over 119 min)
        r = integrate_history_points(self._minutely(800.0))
        assert r.source == IrradianceSource.SHINEMASTER_HISTORY
        assert r.kwh_m2 == pytest.approx(800 * (119 / 60) / 1000, rel=1e-3)
        assert r.samples_used == 120

    def test_too_few_samples_falls_back(self):
        r = integrate_history_points(self._minutely(800.0, minutes=10))
        assert r.kwh_m2 is None and r.source == IrradianceSource.NONE

    def test_spikes_clamped_like_snapshot_path(self):
        pts = self._minutely(5000.0)   # absurd sensor spike
        r = integrate_history_points(pts)
        assert r.kwh_m2 == pytest.approx(
            MAX_PLAUSIBLE_WM2 * (119 / 60) / 1000, rel=1e-3)


def test_irr_compare_iterates_plant_objects_not_keys():
    """Live-run regression 2026-07-06: `portfolio.plants` yields KEYS
    (strings); the compare runner crashed on .datalogger_sn. It must use
    active_plants() like kpi_eod does."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "scripts" / "irr_compare.py").read_text()
    assert "portfolio.active_plants()" in src
    assert "for plant in portfolio.plants:" not in src


class TestLiveRunRegressions20260706:
    """The first live irr-compare returned 0 samples for every plant.
    Two causes, both replayed here:
    (a) v2's _post wraps responses in {_meta, response} — parsing the
        ENVELOPE for `obj` silently yields nothing;
    (b) Growatt env endpoints need plant-context seeding, and the
        configured datalogger SN may not be the env device (v1's warning)
        — getEnvList is the authoritative fallback."""

    def test_envelope_wrapped_page_parses(self):
        inner = {"obj": {"datas": [
            {"calendar": cal(2026, 7, 5, 8, 0), "radiant": 500}],
            "haveNext": False}}
        env = {"_meta": {"url": "x"}, "response": inner}
        points, _, _ = parse_env_history_page(env)
        assert points == [(dt.datetime(2026, 7, 5, 8, 0), 500.0)]

    def test_raw_text_envelope_parses(self):
        import json
        inner = {"obj": {"datas": [
            {"calendar": cal(2026, 7, 5, 8, 0), "radiant": 500}],
            "haveNext": False}}
        env = {"_meta": {}, "response": {"_raw_text": json.dumps(inner)}}
        points, _, _ = parse_env_history_page(env)
        assert len(points) == 1

    def test_env_list_parse_and_pick(self):
        js = {"datas": [{"datalogSn": "AAA", "addr": 32},
                        {"datalogSn": "BBB", "addr": 2}]}
        devs = parse_env_list(js)
        assert devs == [("AAA", 32), ("BBB", 2)]
        assert pick_env_device(devs, "BBB", None) == ("BBB", 2)
        assert pick_env_device(devs, "BBB", 2) == ("BBB", 2)
        assert pick_env_device(devs, "ZZZ", 9) == ("AAA", 32)  # first
        assert pick_env_device([], "AAA", 1) is None

    def test_auto_retries_with_envlist_device(self):
        good = page([{"calendar": cal(2026, 7, 5, 8, 0), "radiant": 100},
                     *[{"calendar": cal(2026, 7, 5, 8, i), "radiant": 100}
                       for i in range(1, 60)]])
        empty = page([])

        class Web(FakeWeb):
            def get_env_history(self, plant_id, sn, addr, day, start):
                self.calls.append((sn, addr))
                return good if sn == "REAL" else empty

        web = Web([])
        web.env_list = {"datas": [{"datalogSn": "REAL", "addr": 2}]}
        points, sn, addr = fetch_env_day_auto(
            web, "P1", "CONFIGURED", 32, "2026-07-05")
        assert sn == "REAL" and addr == 2
        assert len(points) == 60
        assert web.seeded == ["P1"]
