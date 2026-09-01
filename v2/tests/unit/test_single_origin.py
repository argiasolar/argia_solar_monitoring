"""One host, one login: reports at the root of report.argia.com.mx,
live monitoring under /monitoring/, monitoring.argia.com.mx 301s there.

Browsers cache basic-auth credentials per hostname, so two hostnames
could never share a session — that is why the portal moved rather than
the auth being "fixed". The rules that must survive the move:

  * every monitoring link carries the /monitoring prefix, or the page
    would land on the report tree (404) or, worse, a same-named report
    page (/capex/ and /gto1/ exist in BOTH trees);
  * the CAPEX per-plant gating moves with it — a plant owner still
    reaches only their own live page, PPA and the fleet view stay
    internal;
  * the old hostname keeps working for existing links.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MONGEN = (ROOT / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
REPGEN = (ROOT / "server" / "bundle" / "report_gen.py").read_text(encoding="utf-8")
AUTH = (ROOT / "server" / "bundle" / "nginx-argia_auth.conf").read_text(encoding="utf-8")
OLD = (ROOT / "server" / "bundle" /
       "nginx-monitoring.argia.com.mx.conf").read_text(encoding="utf-8")

CAPEX_PLANTS = ("gto2", "qro1", "nl2", "mex3")
PPA_PLANTS = ("gto1", "mex1", "mex2", "nl1", "slp1", "slp2")


class TestMonitoringLinksArePrefixed:
    def test_base_is_configurable_and_defaults_to_monitoring(self):
        assert "BASE = os.environ.get('ARGIA_MON_BASE', '/monitoring')" \
            in MONGEN

    def test_no_root_absolute_page_links_left(self):
        """A monitoring PAGE link must carry the prefix. The only
        allowed bare "/..." links are the deliberate ways out of the
        monitoring tree: the logo, the "← Reports" button, /account/
        on the report host, and the /invoices/ register (2026-09-01,
        the recon board's invoice-annexes button)."""
        import re
        bad = [ln.strip() for ln in MONGEN.splitlines()
               if re.search(r'href="/(?!account/|invoices/|favicon|"|\s)',
                            ln)]
        assert bad == [], bad
        out = [ln.strip() for ln in MONGEN.splitlines()
               if re.search(r'href="/"', ln)]
        assert len(out) == 2, out
        assert any("logo" in ln for ln in out)
        assert any("Reports" in ln for ln in out)

    def test_nav_buttons_prefixed(self):
        for path in ("/", "/ppa/", "/capex/", "/performance/", "/recon/"):
            assert f'href="{{BASE}}{path}"' in MONGEN

    def test_plant_links_prefixed(self):
        assert 'href="{BASE}/{pk.lower()}/"' in MONGEN

    def test_day_archive_links_prefixed(self):
        assert 'f"{BASE}/{pk.lower()}/d/{dd}.html"' in MONGEN
        assert 'BASE + "/" + pk.lower() + "/"' in MONGEN

    def test_date_picker_prefixed(self):
        """Both the picker's jump target and the archive rows build
        the same prefixed day-page path."""
        assert MONGEN.count("{BASE}/{pk.lower()}/d/") >= 2

    def test_photo_assets_prefixed(self):
        assert "return f'{BASE}/assets/{name}'" in MONGEN

    def test_account_link_stays_at_the_root(self):
        """/account/ is a top-level location on the shared host."""
        assert 'href="/account/"' in MONGEN
        assert "'/account/whoami'" in MONGEN


class TestNoCrossSiteSwitching:
    def test_monitoring_has_no_reports_button(self):
        assert "Reports ↗" not in MONGEN

    def test_no_hardcoded_monitoring_hostname_anywhere(self):
        for src in (MONGEN, REPGEN):
            assert "https://monitoring.argia.com.mx" not in src

    def test_report_base_is_same_origin(self):
        assert "REPORT_BASE = os.environ.get('ARGIA_REPORT_BASE', '')" \
            in MONGEN

    def test_landing_links_to_monitoring_relatively(self):
        assert 'href="monitoring/"' in REPGEN


class TestAccessRulesSurviveTheMove:
    def _block(self, path):
        i = AUTH.index(f"location {path} ")
        if "{" not in AUTH[i:i + 40]:
            i = AUTH.index(f"location {path}\n")
        return AUTH[i:AUTH.index("}", i)]

    def test_fleet_stays_internal(self):
        assert "monitoring.htpasswd" in self._block("/monitoring/")

    def test_each_capex_plant_keeps_its_own_gate(self):
        for pk in CAPEX_PLANTS:
            assert f"{pk}.htpasswd" in self._block(f"/monitoring/{pk}/")

    def test_capex_overview_gated_by_capex_file(self):
        assert "capex.htpasswd" in self._block("/monitoring/capex/")

    def test_ppa_plants_get_no_client_facing_rule(self):
        """No /monitoring/<ppa>/ override exists, so PPA live pages
        fall through to the internal default."""
        for pk in PPA_PLANTS:
            assert f"location /monitoring/{pk}/" not in AUTH

    def test_assets_readable_by_every_signed_in_user(self):
        assert "all.htpasswd" in self._block("/monitoring/assets/")

    def test_capex_plant_rules_precede_the_catch_all(self):
        """nginx picks the longest prefix, but keep the file readable
        in the same order it is evaluated."""
        catch_all = AUTH.index("location /monitoring/ ")
        for pk in CAPEX_PLANTS:
            assert AUTH.index(f"location /monitoring/{pk}/") < catch_all


class TestOldHostnameKeepsWorking:
    def test_serves_only_redirects_now(self):
        assert "try_files" not in OLD
        assert OLD.count("return 301") >= 3

    def test_pages_land_under_the_new_prefix(self):
        assert ("return 301 https://report.argia.com.mx/monitoring"
                "$request_uri" in OLD)

    def test_account_page_is_not_pushed_under_monitoring(self):
        i = OLD.index("location /account/")
        assert "report.argia.com.mx$request_uri" in OLD[i:i + 120]
        assert "/monitoring$request_uri" not in OLD[i:i + 120]

    def test_acme_still_reachable_for_cert_renewal(self):
        assert "acme.conf" in OLD

    def test_certificate_still_configured(self):
        assert "monitoring.argia.com.mx/fullchain.pem" in OLD
