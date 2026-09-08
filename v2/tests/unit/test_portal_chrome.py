"""v208 — portal.argia.com.mx chrome and wiring (no PostgreSQL needed).

The rules Tomasz set on 2026-09-05: one header everywhere with exactly
Home · Ask ARGIA · You plus the section's sub-tabs; customer names
first, codes as a grey addon; nothing from the old site dropped
(bilingual, tooltips, flip tiles, logo hover, print)."""
import ast
import pathlib
import re
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))

import portal_chrome as C   # noqa: E402
import auth_core as ac      # noqa: E402


# ------------------------------------------------------------ header rule
class TestHeader:
    def test_exactly_three_buttons_on_every_page(self):
        for section in [None] + list(C.SECTIONS):
            h = C.header(section)
            assert h.count('class="ib') == 3, section
            assert 'title="Home"' in h and 'title="Ask ARGIA' in h and 'title="You"' in h

    def test_sub_tabs_are_the_agreed_ones(self):
        h = C.header("report", "financial")
        tabs = re.findall(r'class="tab( on)?" href="([^"]+)"', h)
        assert [u for _on, u in tabs] == ["/report/", "/report/ppa/", "/report/capex/",
                                          "/report/plants/", "/report/financial/", "/report/invoices/"]
        assert [u for on, u in tabs if on] == ["/report/financial/"]
        h = C.header("monitoring")
        assert [u for _on, u in re.findall(r'class="tab( on)?" href="([^"]+)"', h)] == [
            "/monitoring/", "/monitoring/ppa/", "/monitoring/capex/",
            "/monitoring/performance/", "/monitoring/recon/"]      # v213: nothing dropped
        h = C.header("setup", "people")
        assert [u for _on, u in re.findall(r'class="tab( on)?" href="([^"]+)"', h)] == [
            "/setup/people/", "/setup/plants/", "/setup/finance/", "/setup/cfe/", "/setup/system/"]
        assert re.findall(r'class="tab', C.header("map")) == []      # the map is just the map

    def test_language_and_logout_live_in_the_you_menu_only(self):
        h = C.header("report")
        assert h.count("setLang('en',true)") == 1 and h.count("argiaLogout()") == 1
        assert "My account" in h


# ------------------------------------------------------------- naming rule
class TestNaming:
    def test_display_name_matches_monitoring_gen(self):
        # the map already had the rule (v177.1); the portal must give the same answers
        src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "display_name")
        ns = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "mg", "exec"), ns)
        for cust in ["TAIGENE PPA roof (Leon, GTO)", "SAG PPA roof (CDMX, MEX)", "SMS (CDMX,MEX)",
                     "HOLIDAY INN EXPRESS, Turistica Arizona PPA roof (SLP, SLP)",
                     "QUIMICA COYOACAN PPA land (SLP, SLP)", "PLASTIC OMNIUM PPA land (Monterrey, NL)",
                     "RYDER (Nuevo Laredo, TAM)", "TETRA PAK (Queretaro, QRO)"]:
            assert C.display_name(cust) == ns["display_name"](cust), cust
        assert C.display_name("TAIGENE PPA roof (Leon, GTO)") == "Taigene"
        assert C.display_name("SAG PPA roof (CDMX, MEX)") == "SAG"

    def test_name_first_code_is_an_addon(self):
        h = C.pname("GTO1", "TAIGENE PPA roof (Leon, GTO)")
        assert h.index("Taigene") < h.index("GTO1")
        assert 'class="pcode">GTO1' in h
        assert 'class="pname"' in h

    def test_slugs_cover_every_plant_and_are_url_safe(self):
        for p in ac.PLANTS:
            assert p.upper() in C.SLUGS
        for s in C.SLUGS.values():
            assert re.fullmatch(r"[a-z0-9-]+", s), s
        assert C.slugify("Química Coyoacán") == "quimica-coyoacan"
        assert C.slug("SLP2") == "holiday-inn-express"

    def test_location_from_customer_string(self):
        assert C.location_of("TAIGENE PPA roof (Leon, GTO)") == "Leon, GTO"
        assert C.location_of("SAG") == ""


# --------------------------------------------------- nothing dropped: parts
class TestCarriedOver:
    def test_bilingual_mechanism_is_the_old_one(self):
        p = C.page("x", "<p>" + C.t("Hello", "Hola") + "</p>", "report")
        assert 'data-en="Hello" data-es="Hola"' in p
        assert "localStorage.getItem('argia_lang')" in p and "function setLang" in p
        assert "fetch('/session/whoami'" in p and "fetch('/ask/me'" in p

    def test_tooltip_and_flip_tile(self):
        plain = C.tile("Production", "Producción", "1", tip=("Sum of rows", "Suma"))
        assert 'class="ti"' in plain and 'class="tipbox"' in plain and "haswhy" not in plain
        flip = C.tile("Availability", "Disponibilidad", "93.1%", tone="bad", why_en="SLA not met", why_es="SLA no cumplido")
        assert 'class="tile flip haswhy"' in flip and 'class="face back bad"' in flip
        assert "why this color" in flip and "SLA not met" in flip
        # a green tile never flips, even with a reason
        assert "haswhy" not in C.tile("A", "A", "1", tone="good", why_en="x")
        assert ".tile.haswhy:hover .flipin" in C.CSS and "rotateY(180deg)" in C.CSS

    def test_logo_hover_and_print_rules(self):
        assert ".clogo{" in C.CSS and "grayscale(1)" in C.CSS
        assert ".pcard:hover .clogo" in C.CSS
        assert "@media print" in C.CSS and ".face.back" in C.CSS.split("@media print")[1]

    def test_legacy_redirect_names_the_old_site(self):
        r = C.redirect_page("https://report.argia.com.mx/financial/", "Financial — old site", "Financiero — sitio anterior")
        assert 'url=https://report.argia.com.mx/financial/' in r


# ------------------------------------------------------------------ wiring
class TestWiring:
    def test_generators_only_write_under_main(self):
        for rel in ("server/bundle/report_gen.py", "server/monitoring_gen.py"):
            src = (V2 / rel).read_text(encoding="utf-8")
            assert "if __name__ == '__main__':" in src, rel
            tail = src[src.index("def main():"):]
            assert "write(" in tail
            head = src[:src.index("def main():")]
            assert not re.search(r"^write\(", head, re.M), rel

    def test_portal_gen_clears_argv_before_importing_the_generators(self):
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert src.index("sys.argv = sys.argv[:1]") < src.index("import report_gen as RG")
        assert src.index("import report_gen as RG") < src.index("import monitoring_gen as MG")

    def test_every_sub_tab_has_a_page(self):
        # setup/* and ask/ are the live apps (nginx proxies them on the portal host too)
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        for section, (_en, _es, subs) in C.SECTIONS.items():
            if section in ("setup", "maintenance", "finance", "projects"):   # live apps behind nginx, not generated pages
                continue
            for s, _a, _b in subs:
                rel = f"{section}/{s + '/' if s else ''}index.html"
                assert f"'{rel}'" in src, rel
        for top in ("map/index.html", "engine/index.html", "ags/index.html"):
            assert f"'{top}'" in src
        snippet = (BUNDLE / "nginx-argia_session.conf").read_text(encoding="utf-8")
        assert "location /setup/ {" in snippet and "location /ask/ {" in snippet and "location /account/ {" in snippet
        assert "location /maintenance/ {" in snippet
        assert "location /finance/ {" in snippet and "location /projects/ {" in snippet   # v244 fin app

    def test_nginx_vhost_and_units(self):
        conf = (BUNDLE / "portal.argia.com.mx.conf").read_text(encoding="utf-8")
        assert "server_name portal.argia.com.mx;" in conf
        assert "root /www/hosting/portal.argia.com.mx/www;" in conf
        assert "include /etc/nginx/snippets/argia_auth.conf;" in conf
        assert "include /etc/nginx/scripts/acme.conf;" in conf
        boot = (BUNDLE / "portal.argia.com.mx.http.conf").read_text(encoding="utf-8")
        assert "ssl" not in boot and "acme.conf" in boot
        svc = (BUNDLE / "argia-portal-gen.service").read_text(encoding="utf-8")
        assert "portal_gen.py /www/hosting/portal.argia.com.mx/www" in svc
        tmr = (BUNDLE / "argia-portal-gen.timer").read_text(encoding="utf-8")
        assert "OnCalendar=*:0/5" in tmr and "Persistent=true" in tmr

    @pytest.mark.parametrize("uri,area", [
        ("/", ac.ALL), ("/report/", ac.ALL), ("/report/ppa/", ac.ALL), ("/report/plants/", ac.ALL),
        ("/report/financial/", "financial"), ("/report/invoices/", "financial"), ("/map/", "financial"),
        ("/report/capex/", "capex"), ("/monitoring/capex/", "capex"),
        ("/report/taigene/", "gto1"), ("/report/tetra-pak/", "qro1"),
        ("/monitoring/tetra-pak/", "qro1"), ("/monitoring/taigene/", "monitoring"),
        ("/assets/photos/nl1_t.jpg", ac.ALL), ("/setup/", ac.ADMIN), ("/ask/", ac.ALL),
    ])
    def test_portal_paths_keep_the_same_grants(self, uri, area):
        assert ac.area_for_path(uri) == area


# ------------------------------------------------------------- v209 rules
class TestV209:
    def test_landing_carries_no_fleet_data(self):
        """Designers, sales and office staff land here: no production,
        revenue, alerts or plant names on the front door."""
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        body = src[src.index("def landing():"):src.index("# ------------------------------------------------------------- report pages")]
        for forbidden in ("fleet_now(", "open_alerts(", "RG.atoms", "RG.monthly_kwh", "semaphore(", "CLIENT_LOGOS", "logo("):
            assert forbidden not in body, forbidden
        assert 'id="gname"' in body and "Good morning" in body and "Buenos días" in body
        for dest in ("/report/", "/monitoring/", "/map/", "/engine/", "/ags/", "/setup/"):
            assert f'href="{dest}"' in body or f"'{dest}'" in body, dest

    def test_engine_and_ags_destinations(self):
        assert C.ENGINE_URL == "https://engine.sprinkler.agency/engine"
        assert C.AGS_URL.startswith("https://sprinkler.agency/argiagoldenstandard/")
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert "C.redirect_page(C.ENGINE_URL" in src and "C.redirect_page(C.AGS_URL" in src

    def test_greeting_name_and_language_come_from_the_account(self):
        assert "d.first" in C.JS and "getElementById('gname')" in C.JS
        assert "if(!stored&&d.lang){setLang(d.lang);}" in C.JS
        assert "fetch('/session/lang'" in C.JS and "setLang('en',true)" in C.user_menu()
        auth = (BUNDLE / "auth_app.py").read_text(encoding="utf-8")
        assert "@app.post('/session/lang')" in auth and "'lang': u.get('lang', 'en')" in auth
        assert "ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'" in (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        assert "location = /session/lang" in (BUNDLE / "nginx-argia_session.conf").read_text(encoding="utf-8")

    def test_charts_paint_in_the_portal_palette(self):
        # report_gen's SVG fragments fill with var(--s1) / var(--s2): black without these
        assert "--s1:#05b1a9" in C.CSS and "--s2:#eb6834" in C.CSS and "--surface:#fff" in C.CSS
        # the bars carry class="bar" / "bar exp" (report_gen.columns_svg): teal actual, grey expected
        assert ".bar{fill:var(--s1)}" in C.CSS and ".bar.exp{fill:#c9ced4}" in C.CSS
        assert ".line.wx{" in C.CSS and ".tick{fill:var(--muted)" in C.CSS

    def test_folder_tabs(self):
        assert "border-radius:9px 9px 0 0" in C.CSS and ".tab.on{" in C.CSS

    def test_table_logo_column_aligns_names(self):
        assert ".lcell .lbox{width:84px" in C.CSS
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert 'class="lcell"><span class="lbox">{logo(k)}</span>' in src

    def test_plant_page_is_one_implementation_two_skins(self):
        rg = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")
        assert "def plant_parts(k):" in rg and "def plant_page(k):" in rg
        old = rg[rg.index("def plant_page(k):"):]
        assert "plant_parts(k)" in old and "pdf_bottom()" in old       # old page still complete
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert "RG.plant_parts(k)" in src and "write(f'report/{C.slug(k)}/index.html', plant_report(k))" in src
        assert "write(f'report/{k.lower()}/index.html', C.redirect_page(f'/report/{C.slug(k)}/'" in src



# ------------------------------------------------------------- v210 rules
class TestV210:
    def test_official_logo_in_the_header_everywhere(self):
        assert C.LOGO_URI.startswith("data:image/png;base64,")
        for section in [None] + list(C.SECTIONS):
            assert '<img class="wmlogo" src="data:image/png;base64,' in C.header(section), section

    def test_engine_goes_to_its_engine_path(self):
        assert C.ENGINE_URL == "https://engine.sprinkler.agency/engine"

    def test_folder_row_cannot_scroll(self):
        tabs_rule = C.CSS[C.CSS.index(".tabs{"):C.CSS.index("}", C.CSS.index(".tabs{"))]
        assert "overflow-x:auto" not in tabs_rule and "flex-wrap:wrap" in tabs_rule
        tab_rule = C.CSS[C.CSS.index(".tab{"):C.CSS.index("}", C.CSS.index(".tab{"))]
        assert "top:1px" not in tab_rule

    def test_scoped_css_handles_media_blocks(self):
        out = C.scoped_css("table{a:1}\nth,td{b:2}\n@media print{x{c:3} .y,.z{d:4}}\n.btn{e:5}", ".s")
        assert ".s table{a:1}" in out and ".s th,.s td{b:2}" in out
        assert "@media print{.s x{c:3}" in out and ".s .y,.s .z{d:4}}" in out and ".s .btn{e:5}" in out

    def test_setup_and_ask_apps_wear_the_portal_chrome(self):
        sa = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        assert "def _portal_host():" in sa and "PC.page(t_en, head + f'<div class=\"setupbody\">" in sa
        assert "if is_global and not _portal_host():" in sa and "return render(drawer='people')" in sa
        aa = (BUNDLE / "ask_app.py").read_text(encoding="utf-8")
        assert "def _portal_page():" in aa and "_portal_page() if _portal_host() else PAGE_OLD" in aa

    def test_landing_tile_texts(self):
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        body = src[src.index("def landing():"):src.index("# ------------------------------------------------------------- report pages")]
        assert "financial.'" in body and "invoices" not in body
        assert "every 5 minutes" not in body and "on hover" not in body and "designer training" not in body

    def test_tables_carry_a_total_row(self):
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert 'class="total"' in src and "PR kWp-weighted" in src

    def test_financial_invoices_map_are_real_pages(self):
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert "write('report/financial/index.html', financial_report())" in src
        assert "write('report/invoices/index.html', invoices_page())" in src
        assert "write('map/index.html', map_page())" in src
        assert "RG.financial_body()" in src and "MG.portfolio_page(skin='portal')" in src
        assert "IP.index_body(" in src and "base='/invoices/'" in src
        rg = (BUNDLE / "report_gen.py").read_text(encoding="utf-8")
        assert "def financial_body():" in rg and "financial_body()]" in rg
        mg = (V2 / "server/monitoring_gen.py").read_text(encoding="utf-8")
        assert "def portfolio_page(skin='old'):" in mg and "if skin == 'portal':" in mg
        ip = (V2 / "scripts/invoice_publish.py").read_text(encoding="utf-8")
        assert "def index_body(months, blocked_now=None, records=None, zips=None, base=\"\"):" in ip


# ------------------------------------------------------------- v211 rules
class TestV211:
    def test_live_plant_pages_and_archive_days_on_the_portal(self):
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert "MG.plant_page(k, d, skin='portal')" in src
        assert "write(f'monitoring/{C.slug(k)}/index.html', monitoring_plant(k, MG.TODAY))" in src
        assert "write(f'monitoring/{C.slug(k)}/d/{d}.html', monitoring_plant(k, d))" in src
        assert "write(f'monitoring/{k.lower()}/index.html', C.redirect_page(f'/monitoring/{C.slug(k)}/'" in src
        # the body's code links become slug links; the old page is untouched
        assert "parts['body'].replace(code_path, slug_path)" in src
        mg = (V2 / "server/monitoring_gen.py").read_text(encoding="utf-8")
        assert "def plant_page(pk, d, skin='old'):" in mg and "return page(meta['customer'], body, sub, refresh=live)" in mg

    def test_ask_bar_is_back_on_the_landing_page(self):
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        body = src[src.index("def landing():"):src.index("# ------------------------------------------------------------- report pages")]
        assert 'class="askbar askonly" href="/ask/"' in body

    def test_setup_hides_the_duplicate_row_on_the_portal(self):
        sa = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        # v212: the DRAWER row (.dnav) was the duplicate, not the anchor chips
        assert ".setupbody .dnav{display:none}" in sa
        assert ".setupbody .tabbar{display:none}" not in sa


# ------------------------------------------------------------- v212 rules
class TestV212:
    """Tomasz 2026-09-05: "the bottom scroll bars are pointless" and
    "why we still have double cards in the setup? … remove the bottom
    one"."""

    def test_skin_reset_removes_the_40px_overflow(self):
        css = C.skin_reset(".monbody")
        assert ".monbody .card{overflow:visible}" in css
        assert "margin-left:0;margin-right:0" in css and ".monbody .card>table{width:100%" in css
        # every rule is scoped — nothing leaks into the portal's own cards
        for rule in re.findall(r"([^{}]+)\{", css):
            for sel in rule.split(","):
                assert sel.strip().startswith(".monbody"), sel

    def test_reset_is_appended_after_the_scoped_skin_css(self):
        pg = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert "C.scoped_css(MG.STYLE, '.monbody') + C.skin_reset('.monbody') + MON_OVERRIDES" in pg
        sa = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        assert "PC.scoped_css(SETUP_CONTENT_CSS + cat.CATALOG_CSS, '.setupbody')" in sa
        assert "+ PC.skin_reset('.setupbody') + SETUP_PORTAL_CSS" in sa

    def test_setup_keeps_one_row_of_folders(self):
        sa = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        assert ".setupbody .dnav{display:none}" in sa
        # the anchor chips inside a drawer survive, restyled — not hidden
        assert ".setupbody .tabbar{position:static" in sa


# ------------------------------------------------------------- v213 rules
class TestV213:
    """Tomasz 2026-09-06: same-size percentages, CFE table that fits and
    wraps its note, PPA-only map default with tiles following the
    legend, the audit block spaced like a card — and the decommission
    audit's findings: performance + reconciliation pages and the
    signed-out / no-access pages on the portal."""

    def test_table_pills_take_the_table_font_size(self):
        assert ".card td .pill{font-size:inherit;padding:1px 9px}" in C.CSS

    def test_audit_details_block_is_spaced_like_a_card(self):
        assert ".card>details{margin:16px 20px}" in C.CSS
        assert ".card.audit p{margin:6px 0;line-height:1.55" in C.CSS

    def test_cfe_charge_table_has_no_unit_column_and_wraps_its_note(self):
        cx = (BUNDLE / "cfe_explorer.py").read_text(encoding="utf-8")
        assert "'<span class=\"unit\">'+(UNITS[ch]||'')+'</span></td>'" in cx
        assert "overflow-x:auto" not in cx
        assert ".cfex .note{white-space:normal}" in cx

    def test_performance_and_recon_pages_on_the_portal(self):
        pg = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert "write('monitoring/performance/index.html', monitoring_performance())" in pg
        assert "write('monitoring/recon/index.html', monitoring_recon())" in pg
        assert "MG.performance_page(skin='portal')" in pg and "MG.recon_page(skin='portal')" in pg
        mg = (V2 / "server/monitoring_gen.py").read_text(encoding="utf-8")
        assert "def performance_page(skin='old'):" in mg and "def recon_page(skin='old'):" in mg
        assert "inv = '/report/invoices/' if skin == 'portal' else '/invoices/'" in mg
        # the old controls-row button survives as a header button
        assert 'buttons=f\'<a class="btn" href="/report/invoices/">' in pg

    def test_signed_out_and_no_access_wear_the_portal_chrome(self):
        pg = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert "write('logged-out.html', signed_out_page())" in pg
        assert "write('no-access.html', no_access_page())" in pg
        assert "RG.logged_out_page()" not in pg and "RG.no_access_page()" not in pg


class TestV224Fit:
    """v224 (Tomasz, 2026-09-07 screenshots): Setup cards did not fit their
    text — the catalog's <section class="tab"> collided with the portal
    chrome's `.tab` (the tab-bar button: white-space:nowrap, padding,
    grey background), so every note ran off the card in one line and a
    grey box framed each section. Tables wider than a card now scroll
    inside the card, never the page."""

    def test_setup_sections_do_not_wear_the_tab_button_class(self):
        import pathlib
        v2 = pathlib.Path(__file__).resolve().parents[2]
        cat = (v2 / "server/bundle/setup_catalog.py").read_text(encoding="utf-8")
        assert '<section class="dtab" id=' in cat and 'section class="tab"' not in cat
        assert "section.dtab{scroll-margin-top:124px;}" in cat        # below the sticky header + tab bar
        setup = (v2 / "server/bundle/setup_app.py").read_text(encoding="utf-8")
        assert "section.tab" not in setup and "section.dtab>h2.tabh" in setup

    def test_chrome_wraps_wide_tables_in_a_scroll_box(self):
        assert ".tscroll{overflow-x:auto" in C.CSS and ".card>.tscroll{margin:0 20px 16px;max-width:calc(100% - 40px)}" in C.CSS
        assert "function argiaFit(){" in C.JS and "document.querySelectorAll('.wrap table')" in C.JS
        assert "argiaFit();" in C.JS
        r = C.skin_reset(".setupbody")
        assert ".setupbody .card>.tscroll{margin-left:0;margin-right:0;max-width:100%}" in r
        assert ".setupbody .tscroll>table{width:100%" in r

    def test_monitoring_alert_messages_wrap(self):
        import pathlib
        mg = (pathlib.Path(__file__).resolve().parents[2] / "server/monitoring_gen.py").read_text(encoding="utf-8")
        assert 'th.wrap-text,td.wrap-text{white-space:normal;text-align:left;min-width:260px;}' in mg
        assert '<td class="wrap-text">{esc(alert_text(a["msg"], pk, a["sn"]))}</td>' in mg


class TestV225PortalNames:
    """v225: names first, codes as detail — on the monitoring page's open
    alerts too (Tomasz 2026-09-07: 'fix the naming on the portal pages')."""

    def test_alerts_card_uses_the_naming_layer(self):
        import pathlib
        mg = (pathlib.Path(__file__).resolve().parents[2] / "server/monitoring_gen.py").read_text(encoding="utf-8")
        assert "from argia.alerts import naming as _naming" in mg
        assert "NAMES = (_naming.names_from_rows(" in mg
        assert '<td>{inverter_html(pk, a["sn"])}</td><td>{esc(alert_phrase(a["metric"]))}</td>' in mg      # v230: label + serial
        assert '<td class="wrap-text">{esc(alert_text(a["msg"], pk, a["sn"]))}</td>' in mg
        assert 'data-en="Issue" data-es="Problema"' in mg
        # the sanitiser is the same one the mails use: prefix, bracketed plant, severity tag
        assert '_SEV_TAG = re.compile(r"\\s*\\[(?:CRITICAL|WARNING|INFO)\\]\\s*$")' in mg
        assert "re.sub(r\"^\\[[A-Z0-9]{3,6}\\]\\s*\", '', txt)" in mg

    def test_ledger_message_reads_like_the_mail(self):
        from argia.alerts import naming
        n = naming.Names({"NL1": "Plastic Omnium"}, {("NL1", "JGMAE6500G"): "Inverter 4"})
        msg = "NL1 JGMAE6500G: day-peak temperature 72.0 degC — suspected derating 30 min vs cooler peers"
        assert n.text(msg, "NL1", "JGMAE6500G") == "day-peak temperature 72.0 degC — suspected derating 30 min vs cooler peers"
        assert n.inverter("NL1", "JGMAE6500G") == "Inverter 4 (JGMAE6500G)" and n.inverter_short("NL1", "JGMAE6500G") == "Inverter 4" and naming.phrase("inverter_temp_high") == "inverter running hot"


class TestV230SerialEverywhere:
    """v230 (Tomasz): 'if we see just Inverter 3 it is not good enough' —
    every place a person reads an inverter shows label AND serial, one
    format: text 'Inverter 3 (JGMAE65009)', pages the serial in a .sn span."""

    def test_naming_layer_one_format(self):
        from argia.alerts import naming
        n = naming.Names({"NL1": "Plastic Omnium"}, {("NL1", "JGMAE65009"): "Inverter 3"})
        assert n.inverter("NL1", "JGMAE65009") == "Inverter 3 (JGMAE65009)"
        assert n.inverter_full("NL1", "JGMAE65009") == "Inverter 3 (JGMAE65009)"
        assert n.inverter_html("NL1", "JGMAE65009") == 'Inverter 3 <span class="sn">JGMAE65009</span>'
        assert n.inverter("NL1", "UNKNOWN") == "inverter UNKNOWN" and n.inverter_html("NL1", "UNKNOWN") == 'inverter <span class="sn">UNKNOWN</span>'
        assert n.inverter("NL1", "") == "" and n.inverter_html("NL1", None) == ""
        assert naming.inverter_html("<b>", "S&N") == '&lt;b&gt; <span class="sn">S&amp;N</span>'
        # a serial inside a sentence is replaced by label + serial, never by the label alone
        assert n.text("NL1 JGMAE65009: JGMAE65009 hot", "NL1", "JGMAE65009") == "Inverter 3 (JGMAE65009) hot"

    def test_generators_render_label_and_serial(self):
        mg = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
        assert 'f\'<tr><td>{inverter_html(pk, i["sn"])}</td>\'' in mg            # latest-sample table
        assert "% (inverter_html(pk, r['sn']), tone.get(r['band'], 'off')" in mg   # thermal table
        assert "{esc(inverter_name(pk, sn))}</span>')" in mg                       # chart legend
        assert "'label': NAMES.inverter_short(r[1], r[2]) if NAMES else r[3]" in mg   # registry label, not the sample's
        assert 'esc(i["label"])' not in mg
        rg = (BUNDLE / "report_gen.py").read_text(encoding="utf-8")
        assert "rows.append(f'<tr><td>{_naming.inverter_html(label, sn)}</td>'" in rg
        assert ".sn{font-family:ui-monospace" in rg and ".sn{font-family:ui-monospace" in (BUNDLE / "portal_chrome.py").read_text(encoding="utf-8")
        ma = (BUNDLE / "maint_app.py").read_text(encoding="utf-8")
        assert ma.count("n.inverter_html(t.plant_key, t.inverter_sn)") == 2 and "inverter_full(" not in ma
        dh = (V2 / "argia" / "report" / "dashboard_html.py").read_text(encoding="utf-8")
        assert "r.inverter_label + ' (' + r.inverter_sn + ')'" in dh

    def test_daily_mail_detail_and_ask_carry_the_serial(self):
        sys.path.insert(0, str(V2 / "scripts"))
        import daily_perf_mail as dpm
        assert dpm.issue_detail("inverter-silent:GTO1:SN9", {"label": "Inverter 3"}) == "Inverter 3 (SN9)"
        assert dpm.issue_detail("inverter-silent:GTO1:SN9", {}) == "inverter SN9"
        assert "labels stored with a sample" not in dpm.inverter_labels.__doc__ and "registry" in dpm.inverter_labels.__doc__


class TestV235Tooltips:
    """v235 (Tomasz): tile tooltips were painted under the next card (the v234
    .card{position:relative} plus the flip tiles' perspective), the h2 tooltips
    inherited the title's bold, the empty ticket list touched the card edge."""

    def test_open_tooltip_paints_over_the_next_card_and_is_not_bold(self):
        css = (BUNDLE / "portal_chrome.py").read_text(encoding="utf-8")
        assert ".tile:hover,.tile:focus-within{z-index:70}" in css
        tip = css.split(".tipbox{", 1)[1].split("}", 1)[0]
        assert "font-weight:400" in tip and "text-transform:none" in tip and "white-space:normal" in tip and "z-index:30" in tip

    def test_empty_ticket_list_keeps_the_card_padding(self):
        ma = (BUNDLE / "maint_app.py").read_text(encoding="utf-8")
        assert '<p class="muted" style="margin:0;padding:16px 20px">{T("No tickets.", "Sin tickets.")}</p>' in ma


class TestV236ReferenceLanguage:
    """v236 (Tomasz): the Reference button on the live pages opened the
    Spanish sheet whatever the interface language."""

    def _mg(self):
        import re
        src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
        ns = {}
        exec(compile(src[src.index("REF_LINKS = {"):src.index("# Vendor monitoring portals")], "mg_seg", "exec"), ns)
        return ns

    def test_both_languages_derive_from_one_table(self):
        ns = self._mg()
        assert ns["ref_link"]("GTO1", "es") == "https://argia.com.mx/es/references/-guanajuato-taigene"
        assert ns["ref_link"]("GTO1", "en") == "https://argia.com.mx/en/references/-guanajuato-taigene"
        assert ns["ref_link"]("NL1", "en") == "/monitoring/assets/refs/ARGIA_SOLAR_ref_Plastic_Omnium_EN.pdf"
        assert ns["ref_link"]("NL1", "es").endswith("_ES.pdf") and ns["ref_link"]("ZZZ") == ""
        # every EN sheet the table implies exists in the repo
        for pk in ("NL1", "QRO1", "TAM1"):
            for lang in ("en", "es"):
                assert (V2 / "server" / "assets" / "refs" / ns["ref_link"](pk, lang).rsplit("/", 1)[1]).exists()

    def test_button_carries_both_and_shows_english_first(self):
        ns = self._mg()
        b = ns["ref_button"]("SLP2")
        assert 'href="https://argia.com.mx/en/references/holiday-inn"' in b
        assert 'data-href-en="https://argia.com.mx/en/references/holiday-inn"' in b and 'data-href-es="https://argia.com.mx/es/references/holiday-inn"' in b
        assert ns["ref_button"]("ZZZ") == ""
        assert "ref_btn = ref_button(pk)" in (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")

    def test_set_lang_swaps_the_href(self):
        css = (BUNDLE / "portal_chrome.py").read_text(encoding="utf-8")
        assert "document.querySelectorAll('a[data-href-en]').forEach(a=>{a.href=(l==='es'&&a.dataset.hrefEs)?a.dataset.hrefEs:a.dataset.hrefEn;});" in css


class TestV238FinancialCurrency:
    """v238 (Tomasz): the financial report showed bare numbers — every amount is MXN and says so."""

    def test_tiles_tables_and_kicker_say_mxn(self):
        rg = (BUNDLE / "report_gen.py").read_text(encoding="utf-8")
        for kid in ("k_exp", "k_act", "k_net"):
            assert f'<span id="{kid}">—</span> <span class="unit">MXN</span>' in rg, kid
        assert '<tr><th></th><th class="num">MXN</th></tr>' in rg                      # the two summary tables
        assert 'data-en="Exp. revenue MXN"' in rg and 'data-en="Debt service MXN"' in rg and '<th class="num">O&M MXN</th>' in rg
        assert 'all amounts MXN, sin IVA' in rg
        pg = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert 'all amounts MXN, sin IVA (LaaS USD fees at the loan FX)' in pg
        assert ".thero .unit{" in (BUNDLE / "portal_chrome.py").read_text(encoding="utf-8")
