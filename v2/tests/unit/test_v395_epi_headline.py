"""v395 - Arturo's methodology R6 (EPI-600 headline, PR labelled by basis) and R9 (current
state: last 7 days, last day, month without days with known events)."""
from __future__ import annotations

import pathlib

import pytest

from argia.report import methodology as MC

V2 = pathlib.Path(__file__).resolve().parents[2]


def _t(real, exp, n=20):
    return {'epi600': {'plant': [real, exp, n]}, 'avail': {}, 'steps600': n}


def _month(values, start=1):
    return {f"2026-11-{d:02d}": (_t(v * 1000, 1000) if v is not None else None)
            for d, v in enumerate(values, start=start)}


class TestSummary:
    def test_vitalmex_shaped_month(self):
        # 12 days: outage on 7-8 (low), soiling afterwards (0.85), rain gain 12-13 (0.95)
        vals = [0.90, 0.89, 0.88, 0.88, 0.87, 0.88, 0.30, 0.55, 0.86, 0.86, 0.87, 0.95]
        days = _month(vals)
        ev = MC.event_days({"2026-11-07": 0.33, "2026-11-08": 0.76, "2026-11-09": 1.0}, [])
        s = MC.epi_summary(days, "2026-11-12", ev)
        assert s['mtd'] == pytest.approx(sum(vals) / 12, abs=1e-4)            # ratio of sums (equal weights here)
        assert s['excluded'] == ["2026-11-07", "2026-11-08"]
        assert s['mtd_wo_events'] == pytest.approx((sum(vals) - 0.30 - 0.55) / 10, abs=1e-4)
        assert s['d7'] == pytest.approx(sum(vals[-7:]) / 7, abs=1e-4) and s['day'] == 0.95
        assert s['band'] == 'bad' and s['computed'] == s['days'] == 12
        assert s['reasons']["2026-11-07"] == "availability 33%"

    def test_ratio_of_sums_not_mean_of_ratios(self):
        days = {"2026-11-01": _t(900, 1000), "2026-11-02": _t(50, 100)}       # 90 % on a big day, 50 % on a dark one
        assert MC.epi_summary(days, "2026-11-02", {})['mtd'] == pytest.approx(950 / 1100, abs=1e-4)

    def test_missing_days_and_bands(self):
        days = _month([0.98, None, 0.95])
        s = MC.epi_summary(days, "2026-11-03", {})
        assert s['computed'] == 2 and s['days'] == 3 and s['mtd_wo_events'] is None
        assert [MC.epi_band(v) for v in (0.97, 0.969, 0.93, 0.929, None)] == ['good', 'warn', 'warn', 'bad', '']

    def test_last_7_days_cross_the_month(self):
        days = {f"2026-10-{d}": _t(500, 1000) for d in (28, 29, 30, 31)}
        days.update(_month([1.0, 1.0, 1.0]))
        s = MC.epi_summary(days, "2026-11-03", {})
        assert s['mtd'] == 1.0 and s['d7'] == pytest.approx((4 * 0.5 + 3) / 7, abs=1e-4)

    def test_maintenance_days_are_events(self):
        ev = MC.event_days({}, [("2026-10-24", "2026-10-25", "customer")])
        assert ev == {"2026-10-24": "maintenance (customer)", "2026-10-25": "maintenance (customer)"}


class TestReport:
    SRC = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")

    def test_epi_tile_is_the_headline_and_pr_says_its_basis(self):
        assert "def epi_tile(k):" in self.SRC and "tiles.append(epi_tile(k))" in self.SRC
        assert self.SRC.index("tiles.append(epi_tile(k))") < self.SRC.index('id="t_pr"')
        assert 't("Performance ratio, 30d (raw)"' in self.SRC
        assert "gap on temp-normalized PR vs configured clean baseline" in self.SRC
        assert "_EPL.read_cached(_EPL.CACHE_DIR, _k, _dd, _fp)" in self.SRC

    def test_epi_tile_renders(self):
        import html
        src = self.SRC
        seg = src[src.index("def epi_tile(k):"):src.index("def plant_parts(k):")]
        ns = {"MC": MC, "t": lambda a, b: a, "ti": lambda a, b: "<i/>", "html": html,
              "avail_d": {("P", "2026-11-07"): 0.33}, "maint_days": {}, "asof": "2026-11-08",
              "epi_days": {"P": _month([0.9] * 6 + [0.3, 0.9])}}
        exec(compile(seg, "rg", "exec"), ns)
        out = ns["epi_tile"]("P")
        assert 'class="tile bad" id="t_epi"' in out and "82.5%" in out
        assert "without event days (11-07): 90.0%" in out and "last day 90.0% (11-08)" in out


def test_acceptance_knows_the_epi_window():
    src = (V2 / "scripts/methodology_acceptance.py").read_text(encoding="utf-8")
    assert 'if t == "epi600_window":' in src and "EPL.read_cached" in src
