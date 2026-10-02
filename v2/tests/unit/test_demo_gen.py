"""v279 - demo.argia.com.mx: the pure parts.

demo_gen's leak gate (scrub + scan), the demo name rule, and the static
guarantees: no outbound channel in the generator, a demo vhost that is
a separate site with its own login (nothing of the portal login). The end-to-end run on PostgreSQL is
tests/portal/test_demo_site.py.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))
import demo_gen as DG                                      # noqa: E402

REAL = {
    "GTO1": ["TAIGENE PPA roof (Leon, GTO)", "TAIGENE"],
    "MEX1": ["SAG PPA roof (CDMX, MEX)", "SAG"],
    "NL1": ["PLASTIC OMNIUM PPA land (Monterrey, NL)", "plastic omnium"],
    "GTO2": ["HIRSCHMANN-MEXICO CAPEX roof (San Miguel, GTO)", "HIRSCHMANN"],
    "LOAX1": ["GRUPO MODELO"],
    "SLP2": ["HOLIDAY INN EXPRESS"],
}
DEMO = {"GTO1": "ARGIA SOLAR 1", "MEX1": "ARGIA SOLAR 3", "NL1": "ARGIA SOLAR 6", "GTO2": "ARGIA SOLAR 2"}


@pytest.fixture(scope="module")
def rules():
    return DG.build_rules(REAL)


class TestDemoName:
    def test_the_demo_name_keeps_its_capitals(self):
        assert DG.demo_display("ARGIA SOLAR 3 (CDMX, MEX)", str.title) == "ARGIA SOLAR 3"

    def test_any_other_name_follows_the_portal_rule(self):
        assert DG.demo_display("TAIGENE PPA roof (Leon, GTO)", lambda c: "Taigene") == "Taigene"

    def test_demo_numbers_sort_numerically(self):
        names = ["ARGIA SOLAR 10 (x)", "ARGIA SOLAR 2 (y)", "ARGIA SOLAR 1 (z)"]
        assert sorted(names, key=DG.demo_number) == ["ARGIA SOLAR 1 (z)", "ARGIA SOLAR 2 (y)", "ARGIA SOLAR 10 (x)"]


class TestNameVariants:
    def test_the_location_is_not_a_variant(self):
        v = DG.name_variants("TAIGENE PPA roof (Leon, GTO)")
        assert "TAIGENE" in v and not any("Leon" in x for x in v)

    def test_a_hyphenated_name_gives_its_distinctive_part(self):
        assert {"HIRSCHMANN-MEXICO", "HIRSCHMANN"} <= DG.name_variants("HIRSCHMANN-MEXICO CAPEX roof (San Miguel, GTO)")

    def test_generic_words_alone_are_not_names(self):
        v = DG.name_variants("GRUPO MODELO")
        assert "GRUPO MODELO" in v and "GRUPO" not in v and "MODELO" not in v


class TestScrub:
    def test_every_spelling_of_a_name_is_replaced(self, rules):
        src = "Taigene, TAIGENE, taigene and Plastic Omnium / plastic-omnium / Plastic_Omnium"
        out, n = DG.scrub(src, rules, DEMO)
        assert n == 6
        assert "ARGIA SOLAR 1, ARGIA SOLAR 1, argia-solar-1" in out
        assert "ARGIA SOLAR 6 / argia-solar-6 / ARGIA SOLAR 6" in out
        assert not DG.leaks(out, rules)

    def test_a_url_slug_becomes_the_demo_slug(self, rules):
        out, _ = DG.scrub('<a href="https://argia.com.mx/es/references/-guanajuato-taigene">', rules, DEMO)
        assert "-guanajuato-argia-solar-1" in out

    def test_an_acronym_is_matched_in_capitals_only(self, rules):
        out, n = DG.scrub("SAG lost 3 kWh; usage and the message sag", rules, DEMO)
        assert out.startswith("ARGIA SOLAR 3 lost") and "usage" in out and "message sag" in out and n == 1

    def test_spanish_modelo_is_not_a_customer(self, rules):
        """Regression (found on the first demo run): 'modelo de clima' =
        'weather model'. GRUPO MODELO must not turn it into 'ARGIA SOLAR de clima'."""
        src = "si no el modelo de clima x su relación habitual real/modelo"
        out, n = DG.scrub(src, rules, DEMO)
        assert out == src and n == 0

    def test_the_whole_name_of_a_non_fleet_customer_is_still_caught(self, rules):
        out, _ = DG.scrub("Grupo Modelo and Holiday Inn Express", rules, DEMO)
        assert "Modelo" not in out and "Holiday" not in out

    def test_a_data_uri_is_never_touched(self, rules):
        uri = "data:image/png;base64,AAAA+SAG/taigene+BBBB=="
        out, n = DG.scrub(f'<img src="{uri}">', rules, DEMO)
        assert uri in out and n == 0
        assert not DG.leaks(f'<img src="{uri}">', rules)

    def test_leaks_reports_the_token_and_where(self, rules):
        hits = DG.leaks("....the plant Taigene is dark....", rules)
        assert hits and hits[0][0] == "TAIGENE" and "Taigene is dark" in hits[0][1]


class TestNoPpa:
    def test_the_tag_and_the_column_cell_go(self):
        src = '<h1 class="pt">ARGIA SOLAR 1</h1><span class="pill ok">PPA</span><td><span class="pill ok">PPA</span></td>'
        assert DG.demo_text(src) == '<h1 class="pt">ARGIA SOLAR 1</h1>'

    def test_words_are_rephrased_in_both_languages(self):
        assert DG.demo_text("MXN = 3 kWh x PPA tariff 2.3") == "MXN = 3 kWh x tariff 2.3"
        assert DG.demo_text("MXN = 3 kWh x tarifa PPA 2.3") == "MXN = 3 kWh x tarifa 2.3"
        assert DG.demo_text('<span data-en="9/11 PPA plants online">') == '<span data-en="9/11 plants online">'

    def test_the_total_row_loses_the_portfolio_cell(self):
        src = '<tr class="total"><td><b><span data-en="TOTAL" data-es="TOTAL">TOTAL</span></b></td><td></td><td class="muted">'
        assert DG.demo_text(src).count("<td") == 2


class TestDemoTitle:
    @pytest.mark.parametrize("rel,want", [
        ("index.html", "DEMO - ARGIA"),
        ("report/index.html", "DEMO - Report - ARGIA"),
        ("report/plants/index.html", "DEMO - Report - ARGIA"),
        ("report/argia-solar-3/index.html", "DEMO - Report - ARGIA SOLAR 3"),
        ("monitoring/losses/index.html", "DEMO - Monitoring - ARGIA"),
        ("monitoring/argia-solar-11/d/2026-09-30.html", "DEMO - Monitoring - ARGIA SOLAR 11"),
        ("map/index.html", "DEMO - Map - ARGIA"),
    ])
    def test_every_tab_starts_with_demo(self, rel, want):
        assert DG.demo_title(rel, "<html><title>Report - ARGIA</title>") == f"<html><title>{want}</title>"


class TestUnlinkAbsent:
    def test_a_ticket_link_becomes_text(self):
        out, n = DG.unlink_absent('<td><a class="tk" href="/maintenance/t/T-0002/" target="_blank">T-0002</a></td>')
        assert out == '<td><span class="tk">T-0002</span></td>' and n == 1

    def test_a_button_to_an_absent_app_disappears(self):
        src = 'x<a class="btn live" href="/report/invoices/"><svg></svg> Invoice annexes</a>y'
        assert DG.unlink_absent(src) == ("xy", 1)

    def test_demo_links_are_kept(self):
        src = '<a class="btn" href="/report/argia-solar-2/">Open report</a> <a href="/monitoring/losses/#x">x</a>'
        assert DG.unlink_absent(src) == (src, 0)

    @pytest.mark.parametrize("path", ["/setup/cfe/", "/report/invoices/", "/app/", "/maintenance/new/?plant=GTO1"])
    def test_every_absent_app_is_unlinked(self, path):
        out, n = DG.unlink_absent(f'<a href="{path}">go</a>')
        assert n == 1 and "href" not in out


class TestStaticGuarantees:
    SRC = (BUNDLE / "demo_gen.py").read_text(encoding="utf-8")

    def test_demo_gen_has_no_outbound_channel(self):
        """The demo must not be ABLE to mail, push, upload or call a vendor
        or a model - not just be configured not to."""
        tree = ast.parse(self.SRC)
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        banned = {"smtplib", "requests", "urllib", "http", "socket", "httpx", "anthropic", "googleapiclient",
                  "google", "email", "ftplib", "paramiko"}
        assert not mods & banned, mods & banned
        code = self.SRC.replace(ast.get_docstring(tree, clean=False) or "", "")
        code = "\n".join(ln.split("#", 1)[0] for ln in code.splitlines())      # comments may name what is absent
        for word in ("ntfy", "curl", "wget", "sendmail", "INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER "):
            assert word not in code, word

    def test_every_psql_of_the_demo_is_read_only(self):
        assert "default_transaction_read_only=on" in DG.DEMO_OPTS and DG.READ_ONLY in DG.DEMO_OPTS
        assert DG.DEMO_OPTS.startswith("-c search_path=demo,public")

    VHOST = (BUNDLE / "demo.argia.com.mx.conf").read_text(encoding="utf-8")

    def _code(self):
        return "\n".join(ln.split("#", 1)[0] for ln in self.VHOST.splitlines())

    def test_the_demo_has_its_own_login_file(self):
        code = self._code()
        assert re.search(r'^\s*auth_basic "ARGIA demo";', code, re.M)
        assert re.search(r"^\s*auth_basic_user_file /opt/argia/demo/demo\.htpasswd;", code, re.M)

    def test_nothing_of_the_portal_login_is_used(self):
        """Tomasz 2026-10-01: a separate webpage, never related to the portal -
        no session service, no portal users file, no proxy to any ARGIA app."""
        code = self._code()
        for word in ("auth_request", "argia_auth", "proxy_pass", "/opt/argia/auth", "8512", "portal.argia.com.mx"):
            assert word not in code, word

    def test_only_the_icon_is_outside_the_login(self):
        offs = re.findall(r"location\s+(?:=\s*)?(\S+)\s*\{[^}]*auth_basic off", self._code())
        assert offs == ["/favicon.png"]

    def test_its_own_root_and_certificate(self):
        code = self._code()
        assert "root /www/hosting/demo.argia.com.mx/www;" in code
        assert "/etc/letsencrypt/live/demo.argia.com.mx/fullchain.pem" in code

    def test_no_password_in_the_repo(self):
        for f in BUNDLE.glob("demo*"):
            t = f.read_text(encoding="utf-8", errors="ignore")
            assert not re.search(r"\$apr1\$|\$2[aby]\$|Welcome", t), f.name
