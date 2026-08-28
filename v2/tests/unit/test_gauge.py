"""Regression tests for the portal's production-vs-expected gauge.

monitoring_gen.py queries PostgreSQL at import time, so the gauge is
lifted out of the real source with ast and executed on its own — the
test therefore always runs the code that actually ships, and cannot
drift from a copy.

Bug this guards (reported 2026-08-28, /ppa 108% and /capex 69%): the
SVG large-arc flag was set to 1 whenever the fill exceeded half the
scale, which draws the MAJOR arc the long way round — visible as
broken, clipped arc segments. A semicircle gauge sweeps at most 180
degrees, so the flag must always be 0.
"""
import ast
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "server" / "monitoring_gen.py"


def _load_gauge():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    wanted = ("GAUGE_CX", "GAUGE_CY", "GAUGE_R", "GAUGE_MAX")
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "gauge_svg")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) in wanted
                        or (isinstance(t, ast.Tuple)
                            and any(getattr(e, "id", None) in wanted
                                    for e in t.elts))
                        for t in n.targets))]
    ns = {}
    exec(compile(ast.Module(body=body, type_ignores=[]),
                 "<monitoring_gen:gauge>", "exec"), ns)
    return ns["gauge_svg"], ns


gauge_svg, GNS = _load_gauge()
ARC_RE = re.compile(r'A(\d+),(\d+) 0 (\d) (\d) ([\d.]+),([\d.]+)')


def arcs(svg):
    """[(rx, ry, large_flag, sweep_flag, x_end, y_end)] for every arc."""
    return [(int(a), int(b), int(lg), int(sw), float(x), float(y))
            for a, b, lg, sw, x, y in ARC_RE.findall(svg)]


class TestArcFlag:
    def test_large_arc_flag_never_set(self):
        """The regression: 108% and 69% used to draw the major arc."""
        for pct in (0, 1, 50, 64.9, 65, 65.1, 69, 90, 99, 108, 130, 200):
            for rx, ry, large, sweep, _x, _y in arcs(gauge_svg(pct)):
                assert large == 0, f"large-arc flag set at {pct}%"
                assert sweep == 1, f"wrong sweep direction at {pct}%"
                assert rx == ry, "gauge arc must stay circular"

    def test_value_arc_ends_on_the_circle(self):
        """End point must sit on the radius — a wrong centre or sign
        flip would push the arc outside the viewBox (the clipped look)."""
        cx, cy, r = GNS["GAUGE_CX"], GNS["GAUGE_CY"], GNS["GAUGE_R"]
        for pct in (10, 45, 69, 88, 108, 129):
            value = arcs(gauge_svg(pct))[1]
            _rx, _ry, _lg, _sw, x, y = value
            assert math.isclose(math.hypot(x - cx, y - cy), r, abs_tol=0.2)
            assert y <= cy + 0.2, f"{pct}% dips below the diameter"


class TestSweep:
    def _angle(self, pct):
        """Degrees swept by the value arc, measured from the left end."""
        cx, cy = GNS["GAUGE_CX"], GNS["GAUGE_CY"]
        _rx, _ry, _lg, _sw, x, y = arcs(gauge_svg(pct))[1]
        return 180.0 - math.degrees(math.atan2(cy - y, x - cx))

    def test_half_scale_points_straight_up(self):
        gmax = GNS["GAUGE_MAX"]
        assert math.isclose(self._angle(gmax / 2), 90.0, abs_tol=0.5)

    def test_full_scale_is_a_half_turn(self):
        assert math.isclose(self._angle(GNS["GAUGE_MAX"]), 180.0,
                            abs_tol=0.5)

    def test_sweep_is_monotonic(self):
        seen = [self._angle(p) for p in (5, 20, 45, 69, 90, 108, 125)]
        assert seen == sorted(seen)

    def test_over_scale_is_clamped_not_wrapped(self):
        """>130% must stay a full semicircle, never wrap past the end."""
        full = self._angle(GNS["GAUGE_MAX"])
        for pct in (131, 180, 400):
            assert math.isclose(self._angle(pct), full, abs_tol=0.5)

    def test_zero_draws_track_only(self):
        assert len(arcs(gauge_svg(0))) == 1
        assert "#eceef0" in gauge_svg(0)


class TestBandsAndLayout:
    def test_colour_bands(self):
        assert "#c5221f" in gauge_svg(69)      # red   < 70
        assert "#e8a13d" in gauge_svg(80)      # amber 70-90
        assert "#1e8e3e" in gauge_svg(108)     # green > 90
        assert "#e8a13d" in gauge_svg(70)      # boundary is inclusive
        assert "#1e8e3e" in gauge_svg(90)

    def test_drawing_stays_inside_the_viewbox(self):
        """Stroke half-width included — anything outside gets clipped,
        which is what made the broken gauges look mispositioned."""
        cx, cy, r = GNS["GAUGE_CX"], GNS["GAUGE_CY"], GNS["GAUGE_R"]
        half = 6.0                                    # stroke-width 12
        vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', gauge_svg(50))
        w, h = float(vb.group(1)), float(vb.group(2))
        assert cx - r - half >= 0 and cx + r + half <= w
        assert cy - r - half >= 0 and cy + half <= h

    def test_svg_is_block_level(self):
        """Inline svg adds a descender gap that shifts the caption off
        the KPI label baseline."""
        assert "display:block" in gauge_svg(100)

    def test_percent_label_and_accessible_name(self):
        svg = gauge_svg(108.4)
        assert ">108%<" in svg
        assert 'aria-label="108% of expected (130% full scale)"' in svg

    def test_no_untranslatable_svg_legend(self):
        """The band legend moved to the tile tooltip: svg text is never
        rewritten by the EN/ES toggle, and at this scale it clipped."""
        svg = gauge_svg(108)
        assert "amber" not in svg and "red" not in svg
        assert svg.count("<text") == 1

    def test_width_is_overridable(self):
        assert "width:132px" in gauge_svg(50)
        assert "width:96px" in gauge_svg(50, width=96)
