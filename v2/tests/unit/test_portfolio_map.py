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


class TestRyderReference:
    """v177.3 — TAM1 (Ryder Nuevo Laredo) gets the same treatment as
    Tetra Pak: hosted PDF sheets (EN+ES) + site photos from the sheet
    (the solar carport). No plant is left without a reference now."""

    ASSETS = V2 / "server" / "assets"

    def test_ref_link_and_pdfs(self):
        assert "ARGIA_ref_Ryder_Nuevo_Laredo_ES.pdf" in GEN_SRC
        for name in ("ARGIA_ref_Ryder_Nuevo_Laredo_EN.pdf",
                     "ARGIA_ref_Ryder_Nuevo_Laredo_ES.pdf"):
            f = self.ASSETS / "refs" / name
            assert f.exists() and f.stat().st_size > 100_000, name
            assert f.read_bytes()[:5] == b"%PDF-", name

    @staticmethod
    def _jpeg_size(data):
        """(width, height) from JPEG SOF marker — stdlib only, the
        laptop venv has no Pillow (v177.3 lesson)."""
        i = 2
        while i < len(data) - 9:
            assert data[i] == 0xFF, "not a JPEG marker stream"
            marker = data[i + 1]
            seg = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8,
                                                         0xCC):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return w, h
            i += 2 + seg
        raise AssertionError("no SOF marker found")

    def test_tam1_photos_landscape_jpegs(self):
        for name in ("tam1.jpg", "tam1_t.jpg"):
            f = self.ASSETS / "photos" / name
            assert f.exists() and f.stat().st_size > 10_000, name
            data = f.read_bytes()
            assert data[:2] == b"\xff\xd8", name
            w, h = self._jpeg_size(data)
            assert w > h, (name, w, h)   # banner/card, not portrait

    def test_every_active_plant_has_a_reference_entry(self):
        # 11 plants: 8 website links + NL1/QRO1/TAM1 hosted PDFs
        import re as _re
        keys = set(_re.findall(r"^    '([A-Z0-9]+)':", GEN_SRC, _re.M))
        for pk in ("GTO1", "GTO2", "MEX1", "MEX2", "MEX3", "NL1", "NL2",
                   "QRO1", "SLP1", "SLP2", "TAM1"):
            assert pk in keys, pk


class TestPvoutLayer:
    """v182 — Solargis / Global Solar Atlas PVOUT (photovoltaic
    electricity potential) as an optional Leaflet overlay. Locked:
    the layer appears only when the colorized asset exists, carries
    the CC BY attribution, and the page renders fine without it."""

    def test_layer_is_conditional_on_the_asset(self):
        seg = GEN_SRC.split("def portfolio_page()")[1]
        assert "'pvout_mexico.json'" in seg
        assert "'pvout_mexico.png'" in seg
        assert "pv_bounds = None" in seg          # clean absence path
        assert "pv_js, pv_overlays, pv_legend = '', 'null', ''" in seg

    def test_image_overlay_with_attribution(self):
        seg = GEN_SRC.split("def portfolio_page()")[1]
        assert "L.imageOverlay('assets/pvout_mexico.png'" in seg
        assert "Global Solar Atlas 2.0 / Solargis" in seg
        assert "CC BY 4.0" in seg
        assert "opacity:.55" in seg

    def test_overlay_joins_the_layers_control(self):
        assert "{pv_overlays}" in GEN_SRC
        assert "'Solar potential (PVOUT)': pv" in GEN_SRC

    def test_legend_gradient_with_units(self):
        seg = GEN_SRC.split("def portfolio_page()")[1]
        assert "linear-gradient(90deg" in seg
        assert "kWh/kWp" in seg and "{pv_legend}" in GEN_SRC


plant_city = _exec_def("plant_city")


class TestPlantLegend:
    """v183 — plant legend under the map (name, code, city, kWp, an
    include/exclude checkbox per plant, PPA/CAPEX split, marker
    colors) + the Map link on the report home footer."""

    def test_plant_city_extraction(self):
        assert plant_city("TAIGENE PPA roof (Leon, GTO)") == "Leon, GTO"
        assert plant_city("SMS (CDMX,MEX)") == "CDMX,MEX"
        assert plant_city("No parens name") == ""
        assert plant_city("") == ""

    def test_rows_carry_city(self):
        assert "'city': plant_city(meta['customer'])," in GEN_SRC

    def test_legend_split_ppa_capex_with_marker_colors(self):
        seg = GEN_SRC.split("def portfolio_page()")[1]
        assert 'data-en="PPA plants"' in seg
        assert 'data-en="CAPEX plants"' in seg
        assert "'#2563eb' if r['ppa'] else '#0d9488'" in seg  # map fills
        assert "_ST_RING" in seg                     # status ring colors
        assert "{legend_card}" in seg

    def test_checkboxes_toggle_markers_and_persist(self):
        seg = GEN_SRC.split("def portfolio_page()")[1]
        assert 'class="ptog" data-k=' in seg
        assert "MK[p.key]=m;" in seg
        assert "map.removeLayer(MK[k])" in seg
        assert "map.addLayer(MK[k])" in seg
        assert "argia_map_hide" in seg               # remembered choice

    def test_report_home_links_the_map(self):
        rep = (V2 / "server" / "bundle" / "report_gen.py").read_text(
            encoding="utf-8")
        assert '<a href="portfolio/">{ic_map}' in rep
        assert "ic_map" in rep and "Mapa del portafolio" in rep
