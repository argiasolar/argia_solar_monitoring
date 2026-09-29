"""v268 - the phone app (portal.argia.com.mx/app/), rendered from plain data.

Tomasz, 2026-09-29: "do it so I can see how it works on my iPhone" with
"the same single A letter logo that we are using for the website".
"""
from __future__ import annotations

import json
import re
import struct
import sys
import zlib
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))
import app_view as AV                                     # noqa: E402

EM = chr(0x2014)


def png_pixels(data: bytes):
    """(size, rows) of an 8-bit grayscale, filter-0 PNG as icon_png writes it."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        crc = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, kind
        chunks[kind] = chunks.get(kind, b"") + body
        pos += 12 + n
    w, h, depth, ctype = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, ctype) == (8, 0)                       # 8-bit grayscale, opaque (iOS wants no alpha)
    raw = zlib.decompress(chunks[b"IDAT"])
    rows = [raw[r * (w + 1) + 1:(r + 1) * (w + 1)] for r in range(h)]
    assert all(raw[r * (w + 1)] == 0 for r in range(h))
    return w, rows


def plant(**kw):
    base = {"slug": "sag", "name": "SAG", "where": "CDMX, MEX", "portfolio": "PPA", "kwp": 300,
            "state": "good", "state_en": "all reporting", "state_es": "todo reportando",
            "power_kw": 178.4, "today_kwh": 175.2, "inv_live": 3, "inv_total": 3, "alerts": [],
            "days": [{"date": "2026-09-27", "actual": 300.0, "expected": 1000.0, "lost_kwh": 700.0, "lost_mxn": 1680.0},
                     {"date": "2026-09-26", "actual": 990.0, "expected": 1000.0, "lost_kwh": 10.0, "lost_mxn": 24.0}],
            "loss30": {"days": 30, "kwh": 710.0, "mxn": 1704.0, "unavailability": 700.0, "overheating": 0.0,
                       "underperformance": 10.0, "unavailability_mxn": 1680.0, "underperformance_mxn": 24.0},
            "inverters": [
                {"label": "Inverter 1", "sn": "ES2470051825", "state": "ok", "power_kw": 64.04, "today_kwh": 196.7,
                 "temp": 58.0, "peak": 77.0, "temp_cls": "bad", "peer": 1.02, "peer_cls": "", "last": "11:45", "age_min": 3},
                {"label": "Inverter 2", "sn": "ES2470051826", "state": "stale", "power_kw": None, "today_kwh": 150.0,
                 "temp": None, "peak": 66.0, "temp_cls": "warn", "peer": 0.66, "peer_cls": "bad", "last": "10:02", "age_min": 110},
                {"label": "Inverter 3", "sn": "GR2489022511", "state": "silent", "power_kw": None, "today_kwh": None,
                 "temp": None, "peak": None, "temp_cls": "", "peer": None, "peer_cls": "", "last": "", "age_min": None}]}
    base.update(kw)
    return base


CAPEX = plant(slug="sms", name="SMS", portfolio="CAPEX", state="bad", state_en="no data today", state_es="sin datos hoy",
              power_kw=None, today_kwh=None, inv_live=0,
              days=[{"date": "2026-09-27", "actual": 100.0, "expected": 400.0, "lost_kwh": 300.0, "lost_mxn": None}],
              loss30={"days": 30, "kwh": 300.0, "mxn": None, "unavailability": 300.0},
              alerts=[{"sev": "CRITICAL", "since": "2026-09-29", "text": "Plant offline <script>x</script>", "inverter": ""}])
TICKETS = [
    {"number": "TK-MEX1-0001", "plant": "SAG", "title": "plant offline, check datalogger", "priority": "P1",
     "status_en": "New", "status_es": "Nuevo", "assignee": "", "opened": "2026-09-25", "inverter": "",
     "lost_mxn": 1679.4, "lost_kwh": 700.0, "over_sla": True},
    {"number": "TK-MEX2-0001", "plant": "Vitalmex", "title": "INV-02 low output", "priority": "P2",
     "status_en": "In progress", "status_es": "En curso", "assignee": "juan", "opened": "2026-09-24",
     "inverter": "Inverter 2", "lost_mxn": 297.0, "lost_kwh": 120.0, "over_sla": False},
]
FLEET = [plant(), CAPEX, plant(slug="taigene", name="Taigene", state="warn", state_en="1 inverter hot",
                               alerts=[{"sev": "WARNING", "since": "2026-09-28", "text": "Inverter 2 at 67 C", "inverter": "Inverter 2"}])]


@pytest.fixture(scope="module")
def page():
    return AV.render(FLEET, "2026-09-29 10:10", 1790000000, TICKETS, (1976.4, 820.0))


class TestIcon:
    @pytest.mark.parametrize("size", AV.ICON_SIZES)
    def test_icon_is_a_valid_opaque_png_of_the_right_size(self, size):
        w, rows = png_pixels(AV.icon_png(size))
        assert w == size and len(rows) == size

    def test_the_letter_is_the_websites_mark_centred_with_a_margin(self):
        w, rows = png_pixels(AV.icon_png(512))
        px = lambda x, y: rows[y][x]                       # noqa: E731
        bg, fg = AV.ICON_BG, AV.ICON_FG
        for x, y in ((0, 0), (511, 0), (0, 511), (511, 511), (256, 30), (256, 480)):
            assert px(x, y) == bg, (x, y)                   # margins stay background: rounded corners cut nothing
        assert px(256, 140) == fg                           # the apex bar
        assert px(256, 350) == bg                           # between the legs - no crossbar, it is the website's letter
        runs = re.findall(rb"[\x00-\x7f]+", bytes(rows[380]))   # dark runs on a row near the feet
        assert len(runs) == 2                                  # two legs, nothing else
        dark = [x for x in range(512) if rows[380][x] < 128]
        assert abs((dark[0] + dark[-1]) / 2 - 256) < 3        # centred
        top = min(y for y in range(512) if min(rows[y]) < 128)
        bottom = max(y for y in range(512) if min(rows[y]) < 128)
        assert abs(top - (511 - bottom)) <= 2                  # vertically too
        assert 0.5 < (bottom - top) / 512 < 0.62               # letter height ~56%: clear of iOS's rounded corners

    def test_edges_are_antialiased(self):
        _, rows = png_pixels(AV.icon_png(180))
        assert len({v for r in rows for v in r}) > 10

    def test_same_bytes_every_run(self):
        AV.icon_png.cache_clear()
        a = AV.icon_png(180)
        AV.icon_png.cache_clear()
        assert AV.icon_png(180) == a


class TestInstallable:
    def test_iphone_home_screen_tags(self, page):
        for tag in ('name="apple-mobile-web-app-capable" content="yes"', 'viewport-fit=cover',
                    'rel="apple-touch-icon" href="/apple-touch-icon.png"', 'apple-mobile-web-app-title" content="ARGIA"',
                    'rel="manifest" href="/app/manifest.webmanifest" crossorigin="use-credentials"'):
            assert tag in page, tag

    def test_manifest(self):
        m = json.loads(AV.manifest())
        assert m["display"] == "standalone" and m["start_url"] == "/app/" and m["scope"] == "/"
        assert {i["sizes"] for i in m["icons"]} == {"192x192", "512x512"}

    def test_service_worker_caches_nothing(self):
        assert "addEventListener('fetch'" not in AV.SW_JS and "caches." not in AV.SW_JS
        assert "notificationclick" in AV.SW_JS and "showNotification" in AV.SW_JS

    def test_page_registers_the_worker_and_offers_a_test_notification(self, page):
        assert "serviceWorker.register('/app/sw.js'" in page and 'onclick="testNote()"' in page

    def test_header_is_the_website_logo_not_the_letter(self, page):
        import argia_logo
        header = page[page.index("<header>"):page.index("</header>")]
        assert f'src="{argia_logo.LOGO_URI}"' in header and 'height="24"' in header
        assert "header .logo{height:24px" in page            # v270: 34px was 'way too big' on the phone
        assert "<polygon" not in header                      # the single letter is the Home Screen icon only

    def test_tab_bar_and_safe_area(self, page):
        assert page.count('class="tabbar"') == 1 and "env(safe-area-inset-bottom)" in page
        assert 'href="#tickets" data-tab="tickets"' in page          # v271: tickets live inside the app

    def test_the_app_never_links_to_a_portal_page(self, page):
        """v273, Tomasz on his iPhone: target=_blank did not open Safari (v271),
        the report turned the app white (v272), the full plant page is a desktop
        layout cut off at the right (v273). Every link stays inside the app."""
        assert "target=" not in page
        hrefs = re.findall(r'<a\b[^>]*\bhref="([^"]*)"', page)
        assert hrefs and all(h.startswith("#") for h in hrefs), [h for h in hrefs if not h.startswith("#")]
        assert "/logout" not in page                                # signing out inside the app would strand it


class TestContent:
    def test_every_plant_has_a_screen_and_a_fleet_card(self, page):
        for p in FLEET:
            assert f'id="v-p-{p["slug"]}"' in page and f'href="#p-{p["slug"]}"' in page

    def test_problems_first_on_the_fleet_screen(self, page):
        fleet = page[page.index('id="v-fleet"'):page.index('id="v-p-')]
        assert fleet.index("#p-sms") < fleet.index("#p-taigene") < fleet.index("#p-sag")

    def test_ppa_loss_in_pesos_by_cause(self, page):
        sag = page[page.index('id="v-p-sag"'):]
        sag = sag[:sag.index("</section>")]
        assert "$1,704" in sag and "$1,680" in sag and "Unavailability" in sag
        assert "30% · $1,680" in sag

    def test_capex_is_kwh_never_pesos(self, page):
        sms = page[page.index('id="v-p-sms"'):]
        sms = sms[:sms.index("</section>")]
        assert "$" not in sms and "300 kWh" in sms

    def test_alerts_critical_first_and_escaped(self, page):
        al = page[page.index('id="v-alerts"'):]
        al = al[:al.index("</section>")]
        assert al.index("Plant offline") < al.index("Inverter 2 at 67 C")
        assert "<script>x" not in page and "&lt;script&gt;x" in page
        assert '<i class="badge">2</i>' in page

    def test_fleet_losses_add_up(self, page):
        lo = page[page.index('id="v-losses"'):]
        lo = lo[:lo.index("</section>")]
        assert "$3,408" in lo                        # two PPA plants x 1,704; the CAPEX plant adds kWh only
        assert "1,720" in lo                         # 710 + 300 + 710 kWh

    def test_missing_numbers_are_a_dash_never_none(self, page):
        assert not re.search(r">\s*(None|nan|undefined)\s*<|None k?Wh|None kW", page)

    def test_every_word_in_both_languages(self, page):
        for m in re.finditer(r'data-en="([^"]*)" data-es="([^"]*)"', page):
            assert m.group(2), m.group(0)
        assert page.count("data-es=") > 60

    def test_no_plant_codes_and_no_em_dash(self, page):
        assert EM not in page and EM not in AV.SW_JS and EM not in AV.manifest()
        shown = re.sub(r'href="[^"]*"', "", page)            # ticket numbers (TK-<code>-n) only inside links
        assert not re.search(r"\b(SLP[12]|GTO[12]|NL[12]|MEX[123]|QRO1|TAM1)\b", shown)

    def test_empty_fleet_still_renders(self):
        s = AV.render([], "2026-09-29 10:10", 0)
        assert "No plants." in s and "No open alerts." in s and s.endswith("</html>")

    def test_plant_without_loss_figures(self):
        s = AV.render([plant(loss30={"days": 0, "kwh": 0.0, "mxn": None}, days=[])], "2026-09-29 10:10", 0)
        assert "no loss figures for this plant yet" in s


class TestTickets:
    """v271: the open tickets inside the app, with what each has cost so far."""

    def section(self, page):
        s = page[page.index('id="v-tickets"'):]
        return s[:s.index("</section>")]

    def test_each_ticket_with_its_money_and_the_total(self, page):
        s = self.section(page)
        assert s.count('class="row tk"') == 2
        assert "$1,679" in s and "$297" in s and "$1,976" in s
        assert "plant offline, check datalogger" in s and "Inverter 2" in s
        assert "over SLA" in s and "unassigned" in s and "juan" in s

    def test_ticket_numbers_carry_plant_codes_so_they_are_not_shown(self, page):
        s = re.sub(r'href="[^"]*"', "", self.section(page))
        assert "TK-MEX1" not in s and "TK-MEX2" not in s

    def test_tickets_are_read_only_in_the_app(self, page):
        s = self.section(page)
        assert "<a " not in s and "/maintenance/" not in s
        assert "use the portal on a computer" in s

    def test_no_tickets(self):
        s = AV.render(FLEET, "2026-09-29 10:10", 0)
        assert "No open tickets." in s and 'id="v-tickets"' in s


class TestInverters:
    """v273: the full plant page's inverter table, on the plant screen."""

    def card(self, page):
        s = page[page.index('id="v-p-sag"'):]
        s = s[:s.index("</section>")]
        return s[s.index(">Inverters<"):]

    def test_every_inverter_with_status_power_and_energy(self, page):
        c = self.card(page)
        assert c.count('class="row invr"') == 3
        assert "64.0 kW" in c and "196.7 kWh" in c
        assert "no data &gt; 30 min" in c or "no data > 30 min" in c
        assert "silent today" in c and "last data" in c and "10:02" in c

    def test_a_stale_inverter_shows_no_power_now(self, page):
        c = self.card(page)
        row = c[c.index("ES2470051826"):]
        row = row[:row.index('class="row invr"') if 'class="row invr"' in row else len(row)]
        assert "- kW" in row and "150.0 kWh" in row

    def test_heat_and_peer_colours(self, page):
        c = self.card(page)
        assert '<span class="red">58 / 77 °C</span>' in c
        assert '<span class="amber">- / 66 °C</span>' in c
        assert '<span class="red">66% ' in c
