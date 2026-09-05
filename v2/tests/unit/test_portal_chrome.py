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
            "/monitoring/", "/monitoring/ppa/", "/monitoring/capex/"]
        h = C.header("setup")
        assert [u for _on, u in re.findall(r'class="tab( on)?" href="([^"]+)"', h)] == [
            "/setup/", "/setup/users/", "/setup/plants/", "/setup/finance/", "/setup/cfe/", "/setup/system/"]
        assert re.findall(r'class="tab', C.header("map")) == []      # the map is just the map

    def test_language_and_logout_live_in_the_you_menu_only(self):
        h = C.header("report")
        assert h.count("setLang('en')") == 1 and h.count("argiaLogout()") == 1
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
        r = C.redirect_page(C.LEGACY + "/financial/", "Financial — old site", "Financiero — sitio anterior")
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

    def test_every_sub_tab_has_a_page_or_a_legacy_redirect(self):
        src = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        for section, (_en, _es, subs) in C.SECTIONS.items():
            for s, _a, _b in subs:
                rel = f"{section}/{s + '/' if s else ''}index.html"
                assert f"'{rel}'" in src, rel
        for top in ("map/index.html", "setup/index.html", "ask/index.html", "engine/index.html", "ags/index.html"):
            assert f"'{top}'" in src

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
