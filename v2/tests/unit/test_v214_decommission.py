"""v214 — the old site (report./monitoring./portfolio.argia.com.mx) is
decommissioned: every runtime link points at portal.argia.com.mx, the
old generator units are gone, CFE is open to every signed-in user, and
the Pi keeps working from a nightly portfolio snapshot instead of the
retired workbook (Tomasz 2026-09-06: "update all URL etc, clean any
relations with Pi")."""
import json
import pathlib
import re
import sys

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))

OLD_HOSTS = ("report.argia.com.mx", "monitoring.argia.com.mx", "portfolio.argia.com.mx")


def _runtime_files():
    for d in ("scripts", "argia", "pi"):
        yield from (V2 / d).rglob("*.py")
    yield from (V2 / "pi").rglob("*.sh")
    for f in BUNDLE.glob("*.py"):
        yield f


class TestNoOldHostLeft:
    def test_runtime_code_links_only_to_the_portal(self):
        offenders = []
        for f in _runtime_files():
            for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                code = ln.split("#", 1)[0]
                if any(h in code for h in OLD_HOSTS):
                    # the two remaining mentions are the old nginx log paths
                    # the usage card still reads (history) — nothing served
                    if "access.log" in code:
                        continue
                    offenders.append(f"{f.relative_to(V2)}:{i}: {ln.strip()[:90]}")
        assert not offenders, "\n".join(offenders)

    def test_mails_point_at_the_portal(self):
        assert "https://portal.argia.com.mx/monitoring/" in (V2 / "scripts/daily_perf_mail.py").read_text(encoding="utf-8")
        assert "https://portal.argia.com.mx/setup/" in (V2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")
        assert "https://portal.argia.com.mx/monitoring/" in (V2 / "argia/alerts/monitor.py").read_text(encoding="utf-8")
        assert "https://portal.argia.com.mx/monitoring/" in (V2 / "argia/alerts/ledger_mail.py").read_text(encoding="utf-8")
        fm = (V2 / "scripts/financial_mail.py").read_text(encoding="utf-8")
        assert 'WEBROOT = "/www/hosting/portal.argia.com.mx/www"' in fm
        assert 'os.path.join(WEBROOT, "report", "financial", "index.html")' in fm
        assert 'WEBROOT = "/www/hosting/portal.argia.com.mx/www"' in (V2 / "scripts/invoice_publish.py").read_text(encoding="utf-8")

    def test_generators_default_to_the_portal_root(self):
        for f in (BUNDLE / "report_gen.py", V2 / "server/monitoring_gen.py"):
            assert "else '/www/hosting/portal.argia.com.mx/www'" in f.read_text(encoding="utf-8"), f.name
        assert "OLD_INVOICES_DIR = '/www/hosting/portal.argia.com.mx/www/invoices'" in (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")

    def test_old_units_and_old_cfe_page_are_gone(self):
        for name in ("argia-dashboard.service", "argia-dashboard.timer", "argia-monitoring-gen.service",
                     "argia-monitoring-gen.timer", "cfe_page_gen.py", "enable_report_domains.sh"):
            assert not (BUNDLE / name).exists(), name
        assert (BUNDLE / "argia-portal-gen.timer").exists()
        am = (V2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")
        assert '"argia-portal-gen"' in am and '"argia-monitoring-gen"' not in am
        dp = (V2 / "scripts/daily_perf_mail.py").read_text(encoding="utf-8")
        assert '"argia-portal-gen": "Portal page generator"' in dp and '"argia-dashboard"' not in dp


class TestOldHostsRedirect:
    def test_report_host_maps_every_old_path(self):
        c = (BUNDLE / "report.argia.com.mx.conf").read_text(encoding="utf-8")
        assert "root /www/hosting" not in c and "argia_auth.conf" not in c   # serves nothing itself
        for rule in ("location = /              { return 301 https://portal.argia.com.mx/report/; }",
                     "location /financial/      { return 301 https://portal.argia.com.mx/report$request_uri; }",
                     "location /capex/          { return 301 https://portal.argia.com.mx/report$request_uri; }",
                     "location /portfolio/      { return 301 https://portal.argia.com.mx/map/; }",
                     "location /cfe/            { return 301 https://portal.argia.com.mx/setup/cfe/; }",
                     "return 301 https://portal.argia.com.mx/report$request_uri; }",
                     "location /                { return 301 https://portal.argia.com.mx$request_uri; }"):
            assert rule in c, rule
        # every plant code is in the plant-page map
        for code in ("gto1", "gto2", "mex1", "mex2", "mex3", "nl1", "nl2", "qro1", "slp1", "slp2", "tam1"):
            assert code in c
        assert "acme.conf" in c and "ssl_certificate" in c           # renewals + HTTPS redirects

    def test_monitoring_and_portfolio_hosts_redirect_to_the_portal(self):
        m = (BUNDLE / "nginx-monitoring.argia.com.mx.conf").read_text(encoding="utf-8")
        assert "return 301 https://portal.argia.com.mx/monitoring$request_uri" in m
        assert "report.argia.com.mx" not in m
        p = (BUNDLE / "portfolio.argia.com.mx.conf").read_text(encoding="utf-8")
        assert "return 301 https://portal.argia.com.mx/map/;" in p


class TestCfeForEveryone:
    def test_area_and_drawer(self):
        import auth_core as ac
        assert ac.area_for_path("/setup/cfe/") == ac.ALL
        assert ac.area_for_path("/setup/") == ac.ADMIN
        sa = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        assert "if drawer == 'cfe':                       # v214: open to everyone signed in" in sa
        assert "tabs = None if (is_global_ or org_) else [('cfe', 'CFE & tariffs', 'CFE y tarifas')]" in sa

    def test_header_can_show_a_shorter_tab_list(self):
        import portal_chrome as C
        h = C.header("setup", "cfe", tabs_override=[("cfe", "CFE & tariffs", "CFE y tarifas")])
        assert h.count('class="tab"') + h.count('class="tab on"') == 1 and 'href="/setup/cfe/"' in h
        full = C.header("setup", "cfe")
        assert full.count('class="tab"') + full.count('class="tab on"') == 5

    def test_front_door_sends_non_admins_to_cfe(self):
        pg = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert 'id="tile-{key}"' in pg and 'class="tblurb"' in pg
        assert "ts.href='/setup/cfe/'" in (BUNDLE / "portal_chrome.py").read_text(encoding="utf-8")


class TestPiSnapshot:
    def test_portfolio_from_records_builds_the_same_portfolio(self):
        from argia.core.config import portfolio_from_records
        plants = [{"plant_key": "GTO1", "customer": "TAIGENE PPA roof (Leon, GTO)", "brand": "growatt",
                   "site_id": "123", "kwp_dc": "818", "kwp_ac": "700", "lat": "21.1", "lon": "-101.6",
                   "expected_factor": "4.2", "pr_target": "0.8", "installation_date": "2024-01-01",
                   "secret_api_name": "", "secret_user_name": "GROWATT_USERNAME",
                   "secret_pass_name": "GROWATT_PASSWORD", "weather_plant_id": "", "datalogger_sn": "",
                   "datalogger_addr": "0", "active": "TRUE", "portfolio": "PPA"}]
        invs = [{"plant_key": "GTO1", "inverter_sn": "SN1", "label": "MAX 1", "rated_kw": "100", "active": "TRUE"}]
        pf = portfolio_from_records(plants, invs)
        p = pf.active_plants()
        assert [x.plant_key for x in p] == ["GTO1"] and p[0].portfolio == "PPA" and p[0].brand == "GROWATT"

    def test_export_shape_is_what_ppa_watch_reads(self):
        sys.path.insert(0, str(V2 / "scripts"))
        import portfolio_export as PE
        doc = PE.export([{"plant_key": "GTO1"}], [{"plant_key": "GTO1", "inverter_sn": "SN1"}])
        assert set(doc) == {"generated_utc", "plants", "inverters"}
        json.dumps(doc)
        pw = (V2 / "pi/report_watch/ppa_watch.py").read_text(encoding="utf-8")
        assert 'PORTFOLIO_JSON = os.path.expanduser("~/report_watch/portfolio.json")' in pw
        assert "portfolio_from_records(" in pw and "SheetsClient" not in pw and "GOOGLE_SHEET_ID_V2" not in pw

    def test_backup_writes_and_pi_pulls_the_snapshot(self):
        assert 'portfolio_export.py --out "$OUT/portfolio_latest.json"' in (BUNDLE / "db_backup.sh").read_text(encoding="utf-8")
        pull = (V2 / "pi/db_backups/pull_backup.sh").read_text(encoding="utf-8")
        assert "get portfolio_latest.json $HOME/report_watch/portfolio.json" in pull

    def test_pi_watchdog_probes_the_portal(self):
        rw = (V2 / "pi/report_watch/report_watch.sh").read_text(encoding="utf-8")
        assert 'URL="https://portal.argia.com.mx/login"' in rw
        assert "report.argia.com.mx" not in rw
