"""Tests: /portfolio/ fleet map (v177).

Locked here: circle AREA tracks kWp (sqrt diameter scale, clamped),
map status agrees with the alert thresholds, the page is gated as
'financial' (it shows fleet-wide PPA revenue), CAPEX plants never get
fabricated money, clicks land on the plant's performance report, and
the pretty subdomain stays a redirect — one host, one login.

monitoring_gen.py queries PG at import, so the pure helpers are
exec-extracted from source (the test_plant_page_tiles pattern).
"""

import pathlib
import re

V2 = pathlib.Path(__file__).resolve().parents[2]
GEN_SRC = (V2 / "server" / "monitoring_gen.py").read_text(
    encoding="utf-8")
AUTH_SRC = (V2 / "server" / "bundle" / "auth_core.py").read_text(
    encoding="utf-8")
NGINX_SRC = (V2 / "server" / "bundle" / "nginx-argia_session.conf"
             ).read_text(encoding="utf-8")
VHOST_SRC = (V2 / "server" / "bundle" / "portfolio.argia.com.mx.conf"
             ).read_text(encoding="utf-8")


def _exec_def(name, extra=""):
    """exec one top-level def from monitoring_gen source."""
    m = re.search(rf"\ndef {name}\(.*?(?=\ndef |\nclass |\n# ---)",
                  GEN_SRC, re.S)
    assert m, f"def {name} not found"
    ns = {}
    exec(extra + m.group(0), ns)          # noqa: S102
    return ns[name]


circle_px = _exec_def("circle_px")
map_status = _exec_def("map_status", "STALE_MIN = 30\n")


class TestCirclePx:
    def test_sqrt_scale_known_plants(self):
        # GTO1 818 kWp near the cap, MEX3 155 kWp well above the floor
        assert 60 <= circle_px(818.33) <= 64
        assert 33 <= circle_px(154.98) <= 40

    def test_monotonic_in_kwp(self):
        sizes = [circle_px(k) for k in (100, 200, 400, 600, 800)]
        assert sizes == sorted(sizes)
        assert sizes[0] < sizes[-1]

    def test_clamps_and_junk(self):
        assert circle_px(0) == 24
        assert circle_px(None) == 24
        assert circle_px(99999) == 64

    def test_area_not_radius_tracks_kwp(self):
        # 4x capacity ~= 2x diameter (before clamps), never 4x
        d1, d4 = circle_px(150) - 16, circle_px(600) - 16
        assert 1.7 < d4 / d1 < 2.3


class TestMapStatus:
    def test_night_outside_window(self):
        assert map_status(5.0, 1000, False) == ("night", "off")

    def test_dark_without_data(self):
        assert map_status(None, 0, True) == ("dark", "bad")
        assert map_status(10.0, 0, True) == ("dark", "bad")

    def test_stale_over_threshold(self):
        assert map_status(31.0, 500, True) == ("stale", "warn")

    def test_live_when_fresh(self):
        assert map_status(4.0, 500, True) == ("live", "good")

    def test_threshold_is_the_alert_mailers(self):
        m = re.search(r"\ndef map_status.*?(?=\ndef )", GEN_SRC, re.S)
        assert "STALE_MIN" in m.group(0)   # never a private constant


class TestPageWiring:
    def test_written_outside_monitoring_area(self):
        assert "write_root('portfolio/index.html', portfolio_page())" \
            in GEN_SRC

    def test_auth_gate_is_financial(self):
        assert "'/portfolio/': 'financial'," in AUTH_SRC

    def test_nginx_serves_portfolio(self):
        assert "location /portfolio/ { try_files $uri $uri/ =404; }" \
            in NGINX_SRC

    def test_subdomain_is_redirect_only(self):
        # one host = one login (2026-08-28); the pretty name 301s in
        assert "return 301 https://report.argia.com.mx/portfolio/;" \
            in VHOST_SRC
        assert "auth_request" not in VHOST_SRC
        # no root/try_files directive — this vhost must serve no files
        assert not re.search(r"^\s*root\s", VHOST_SRC, re.M)
        assert "try_files" not in VHOST_SRC

    def test_leaflet_pinned_from_cdnjs(self):
        for asset in ("leaflet/1.9.4/leaflet.min.css",
                      "leaflet/1.9.4/leaflet.min.js"):
            assert f"cdnjs.cloudflare.com/ajax/libs/{asset}" in GEN_SRC

    def test_click_opens_performance_report(self):
        assert "window.location='/'+p.key.toLowerCase()+'/';" in GEN_SRC

    def test_marker_size_comes_from_circle_px(self):
        assert "'px': circle_px(meta['kwp'])," in GEN_SRC

    def test_capex_never_gets_fabricated_money(self):
        # tariff 0 plants: energy yes, MXN None — no invented savings
        assert "if is_ppa else None" in GEN_SRC
        assert "tariff = cur_tariff.get(pk, 0.0) if is_ppa else 0.0" \
            in GEN_SRC

    def test_money_labeled_as_estimate(self):
        page_seg = GEN_SRC.split("def portfolio_page()")[1]
        assert "sin IVA" in page_seg
        assert "≈" in page_seg

    def test_lifetime_revenue_uses_monthly_tariffs(self):
        seg = GEN_SRC.split("def portfolio_rows()")[1].split("\ndef ")[0]
        assert "contract_monthly" in seg
        assert "coalesce(nullif(cm.tariff_mxn,0)" in seg

    def test_map_link_in_monitoring_controls(self):
        assert 'href="/portfolio/" data-en="Map"' in GEN_SRC

    def test_plant_without_coordinates_is_skipped_not_crashed(self):
        seg = GEN_SRC.split("def portfolio_rows()")[1].split("\ndef ")[0]
        assert "if lat is None or lon is None:" in seg
