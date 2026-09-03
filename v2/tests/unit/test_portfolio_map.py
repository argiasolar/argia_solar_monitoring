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


display_name = _exec_def("display_name")


class TestV177_1Feedback:
    """Tomasz's screenshot round: CARTO tiles demanded an API key,
    lifetime tiles crowded the row, values overflowed their tiles,
    and plant codes leaked into the UI."""

    def test_display_name_strips_codes_and_boilerplate(self):
        assert display_name("TAIGENE PPA roof (Leon, GTO)") == "Taigene"
        assert display_name("SAG PPA roof (CDMX, MEX)") == "SAG"
        assert display_name("PLASTIC OMNIUM PPA land (Monterrey, NL)") \
            == "Plastic Omnium"
        assert display_name(
            "HOLIDAY INN EXPRESS, Turistica Arizona PPA roof (SLP, SLP)"
        ) == "Holiday Inn Express"
        assert display_name("HIRSCHMANN-MEXICO (San Miguel, GTO)") \
            == "Hirschmann-Mexico"
        assert display_name("SMS (CDMX,MEX)") == "SMS"
        assert display_name("") == ""

    def test_carto_tiles_are_gone(self):
        assert "cartocdn" not in GEN_SRC   # anonymous use now refused

    def test_satellite_default_with_streets_toggle(self):
        assert ("server.arcgisonline.com/ArcGIS/rest/services/"
                "World_Imagery") in GEN_SRC
        assert "World_Boundaries_and_Places" in GEN_SRC   # name overlay
        assert "tile.openstreetmap.org" in GEN_SRC
        assert "sat.addTo(map);" in GEN_SRC
        assert "L.control.layers" in GEN_SRC

    def test_no_lifetime_tiles_in_kpi_row(self):
        seg = GEN_SRC.split("tiles = f'''")[1].split("'''")[0]
        assert "Lifetime energy" not in seg
        assert "Lifetime PPA revenue" not in seg
        # lifetime numbers still live on the hover card
        assert "'life_mwh'" in GEN_SRC and "Lifetime</td>" in GEN_SRC

    def test_tile_values_scale_to_fit(self):
        assert "clamp(" in GEN_SRC        # .tval shrinks, never spills

    def test_no_plant_codes_in_map_ui(self):
        seg = GEN_SRC.split("def portfolio_page()")[1]
        assert "'<h3>'+p.name+'</h3>'" in seg           # tooltip title
        assert "p.key+' — '" not in seg                 # code header gone
        assert '"pwrap"' in seg and "'+p.label+'" in seg  # name label
        # the code survives ONLY inside the click URL
        assert "window.location='/'+p.key.toLowerCase()+'/';" in seg

    def test_marker_label_is_display_name(self):
        assert "'label': display_name(meta['customer'])," in GEN_SRC


class TestReferenceSheets:
    """v177.2 — NL1 and QRO1 have no page on argia.com.mx, so their
    reference buttons serve the official PDF sheets from the portal
    itself. Tomasz: correct spelling is Tetra PAK, and the NL1 photo
    must come from the Plastic Omnium sheet (the old one showed a
    different project)."""

    ASSETS = V2 / "server" / "assets"

    def test_ref_links_point_at_hosted_pdfs(self):
        assert ("'NL1': '/monitoring/assets/refs/'\n"
                "           'ARGIA_SOLAR_ref_Plastic_Omnium_ES.pdf',"
                ) in GEN_SRC
        assert "ARGIA_SOLAR_ref_Tetra_Pak_ES.pdf" in GEN_SRC

    def test_all_four_pdfs_in_repo(self):
        for name in ("ARGIA_SOLAR_ref_Plastic_Omnium_EN.pdf",
                     "ARGIA_SOLAR_ref_Plastic_Omnium_ES.pdf",
                     "ARGIA_SOLAR_ref_Tetra_Pak_EN.pdf",
                     "ARGIA_SOLAR_ref_Tetra_Pak_ES.pdf"):
            p = self.ASSETS / "refs" / name
            assert p.exists() and p.stat().st_size > 100_000, name
            assert p.read_bytes()[:5] == b"%PDF-", name

    def test_tetra_pak_never_spelled_pack(self):
        assert "Tetra_Pack" not in GEN_SRC
        assert not list((self.ASSETS / "refs").glob("*Pack*"))

    def test_nl1_and_qro1_photos_in_repo(self):
        for name in ("nl1.jpg", "nl1_t.jpg", "qro1.jpg", "qro1_t.jpg"):
            p = self.ASSETS / "photos" / name
            assert p.exists() and p.stat().st_size > 10_000, name
            assert p.read_bytes()[:2] == b"\xff\xd8", name   # JPEG
