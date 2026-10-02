"""v279 - demo.argia.com.mx, end to end on the seeded PostgreSQL.

demo_gen runs for real (in this process) against the synthetic fleet,
whose customer names are the real ones - exactly the situation in
production. The tests prove what Tomasz asked for and what must never
happen:

* landing + /report/ + /monitoring/ + /map/ exist, nothing else does
  (no financial report, invoices, app, Ask ARGIA, setup, account);
* every plant is "ARGIA SOLAR <n>", every plant is PPA, every logo is
  the ARGIA SOLAR logo, the CAPEX plants are priced at the fleet's PPA
  tariff;
* NO real customer name anywhere in the published files (the gate);
* demo_gen cannot write to the database, refuses to run without the
  demo views, and keeps the last good demo when a leak is found;
* the portal's own pages are unchanged by the demo (search_path is
  per process).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import re
import subprocess
import sys

import pytest

from tests.portal.conftest import V2, _mx_today, reset_db

BUNDLE = V2 / "server" / "bundle"
DEMO_SQL = BUNDLE / "demo_schema.sql"
REAL = ["TAIGENE", "SAG", "VITALMEX", "PLASTIC OMNIUM", "HOLIDAY INN EXPRESS", "QUIMICA COYOACAN",
        "HIRSCHMANN", "RYDER", "BUDENHEIM", "SMS", "TETRA PAK", "PIRELLI", "GRUPO MODELO"]


def _apply_demo_schema(env):
    r = subprocess.run(["psql", "-q", "-X", "-v", "ON_ERROR_STOP=1", "-d", "argia_mont", "-f", str(DEMO_SQL)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _portal_root(tmp):
    """A stand-in for the portal web root: the files demo_gen may copy,
    plus one it must NOT copy (a reference PDF named after a customer)."""
    root = tmp / "portal"
    (root / "assets" / "photos").mkdir(parents=True)
    (root / "monitoring" / "assets" / "refs").mkdir(parents=True)
    (root / "portfolio" / "assets").mkdir(parents=True)
    (root / "assets" / "photos" / "gto1_t.jpg").write_bytes(b"\xff\xd8jpg")
    (root / "monitoring" / "assets" / "gto1.jpg").write_bytes(b"\xff\xd8jpg")
    (root / "monitoring" / "assets" / "refs" / "ARGIA_SOLAR_ref_Plastic_Omnium_ES.pdf").write_bytes(b"%PDF")
    (root / "favicon.png").write_bytes(b"\x89PNG")
    (root / "secret.html").write_text("TAIGENE invoice")
    return root


def run_demo(env, out, portal_root, extra_env=None, patch=None):
    """demo_gen.main() in this process (coverage counts it). -> (rc, stdout, stderr)"""
    saved_env, saved_argv, saved_path = dict(os.environ), list(sys.argv), list(sys.path)
    o, e = io.StringIO(), io.StringIO()
    try:
        os.environ.update({k: env[k] for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH")})
        os.environ["ARGIA_PORTAL_ROOT"] = str(portal_root)
        os.environ.update(extra_env or {})
        for m in ("portal_gen", "report_gen", "monitoring_gen", "portal_chrome", "demo_gen",
                  "argia_client_logos", "losses_view", "app_view"):
            sys.modules.pop(m, None)
        sys.path.insert(0, str(BUNDLE))
        import demo_gen
        if patch:
            patch(demo_gen)
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = demo_gen.main(["demo_gen.py", str(out)])
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        sys.argv, sys.path[:] = saved_argv, saved_path
        for m in ("portal_gen", "report_gen", "monitoring_gen", "portal_chrome", "demo_gen", "argia.alerts.naming"):
            sys.modules.pop(m, None)
    return rc, o.getvalue(), e.getvalue()


@pytest.fixture(scope="module")
def demo(pg_env, tmp_path_factory):
    reset_db(pg_env)
    r = subprocess.run([sys.executable, str(V2 / "scripts/loss_daily.py"), "--from",
                        (_mx_today() - dt.timedelta(days=8)).isoformat()],
                       env=dict(pg_env, ARGIA_PG_MIRROR="1", PYTHONPATH=str(V2)), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    _apply_demo_schema(pg_env)
    tmp = tmp_path_factory.mktemp("demo")
    out = tmp / "www"
    swap = tmp / "demo_photos"                 # v280: demo-only photo replacement
    swap.mkdir()
    (swap / "gto1.jpg").write_bytes(b"\xff\xd8demo-swap")
    (swap / "notes.txt").write_text("TAIGENE")    # not a photo name: ignored
    rc, so, se = run_demo(pg_env, out, _portal_root(tmp), extra_env={"ARGIA_DEMO_PHOTOS": str(swap)})
    assert rc == 0, so + se
    return out, so, tmp


def files(out):
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())


def text(out, rel):
    return (out / rel).read_text(encoding="utf-8")



LINK = re.compile(r"""(?:href|src)=["'](/[^"'#?]*)""")
EM = chr(0x2014)
ALLOWED_TOP = {"index.html", "favicon.png",
               "report", "monitoring", "map", "assets", "portfolio"}
# what the demo must NEVER contain
FORBIDDEN = ("report/financial/", "report/invoices/", "report/capex/", "monitoring/capex/",
             "app/", "engine/", "ags/", "invoices/", "setup/", "finance/", "projects/", "ask/")


def markup(p):
    """The page without its scripts: the shared chrome JS names /app/ and
    /setup/cfe/ for the portal's own tiles - code, not a link a reader sees."""
    return re.sub(r"<script\b.*?</script>", "", p.read_text(encoding="utf-8"), flags=re.S)


def all_files(out):
    return {p.relative_to(out).as_posix(): p for p in out.rglob("*") if p.is_file()}


def pages(out):
    return {k: p for k, p in all_files(out).items() if k.endswith((".html", ".csv"))}


def real_name_hits(out, names):
    """An implementation independent of demo_gen: plain case-insensitive
    word search for each real name and its distinctive words."""
    words = set()
    for n in names:
        head = re.split(r"\s+(?:PPA|CAPEX)\b|\(", n)[0].strip()
        words.add(head)
        words |= {w for w in re.split(r"[\s\-]+", head) if len(w) >= 5 and w.upper() not in ("GRUPO", "MODELO", "MEXICO", "EXPRESS", "TETRA", "PLASTIC")}
    hits = []
    for k, p in pages(out).items():
        s = re.sub(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "", p.read_text(encoding="utf-8"))
        for w in words:
            flags = 0 if len(w) <= 3 else re.I
            sep = r"[\s\-_]+"
            rx = r"(?<![A-Za-z0-9])" + sep.join(map(re.escape, re.split(r"[\s\-]+", w))) + r"(?![A-Za-z0-9])"
            m = re.search(rx, s, flags)
            if m:
                hits.append(f"{k}: {w!r} ...{s[max(0, m.start() - 30):m.end() + 30]}...")
    return hits


class TestProofAndInventory:
    def test_the_proof_line(self, demo, psql):
        _, so, _ = demo
        m = re.search(r"demo_gen: wrote (\d+) pages \+ (\d+) assets .* \((\d+) plants, all PPA; \d+ name\(s\) scrubbed; 0 leaks\)", so)
        assert m, so
        assert int(m.group(3)) == int(psql("SELECT count(*) FROM plant WHERE active")[0][0])

    def test_only_the_demo_sections_exist(self, demo):
        out, _, _ = demo
        top = {k.split("/")[0] for k in all_files(out)}
        assert top <= ALLOWED_TOP, top - ALLOWED_TOP
        bad = [k for k in all_files(out) if k.startswith(FORBIDDEN)]
        assert not bad, bad

    def test_the_three_tiles_and_nothing_else(self, demo):
        out, _, _ = demo
        s = text(out, "index.html")
        tiles = re.findall(r'<a href="(/[a-z]+/)" id="tile-', s)
        assert tiles == ["/report/", "/monitoring/", "/map/"]
        for gone in ('href="/ask/"', 'href="/setup/"', 'href="/finance/"', 'href="/account/"', 'href="/maintenance/"'):
            assert gone not in s, gone

    def test_no_page_links_to_an_app_the_demo_has_not(self, demo):
        out, _, _ = demo
        bad = {}
        for k, p in pages(out).items():
            for u in LINK.findall(markup(p)):
                if u.startswith(("/ask/", "/account/", "/setup/", "/finance/", "/projects/", "/maintenance/", "/invoices/",
                                 "/report/financial", "/report/invoices", "/report/capex", "/monitoring/capex")):
                    bad.setdefault(u, k)
        assert not bad, bad

    def test_every_internal_link_resolves(self, demo):
        out, _, _ = demo
        files = all_files(out)
        broken = {}
        for k, p in pages(out).items():
            for u in LINK.findall(markup(p)):
                if u in ("/", "/login", "/logout") or u.startswith(("/session/", "/monitoring/assets/refs/")):
                    continue
                rel = u.lstrip("/")
                if rel in files or (rel.rstrip("/") + "/index.html") in files:
                    continue
                broken.setdefault(u, k)
        # the seed has photos for one plant only; a missing photo renders as a placeholder on the portal too
        broken = {u: k for u, k in broken.items() if not re.match(r"^/(assets/photos|monitoring/assets)/[a-z0-9]+(_t)?\.jpg$", u)}
        assert not broken, broken

    def test_a_demo_photo_replaces_the_portal_photo_in_the_demo_only(self, demo):
        """v280 (Tomasz): the SAG photo shows the company sign - the demo gets
        another photo; the portal keeps its own."""
        out, _, tmp = demo
        for rel in ("monitoring/assets/gto1.jpg", "assets/photos/gto1.jpg"):
            assert (out / rel).read_bytes() == b"\xff\xd8demo-swap", rel
        assert (tmp / "portal" / "monitoring" / "assets" / "gto1.jpg").read_bytes() == b"\xff\xd8jpg"
        assert not (out / "assets" / "photos" / "notes.txt").exists()

    def test_only_whitelisted_assets_are_copied(self, demo):
        out, _, _ = demo
        files = all_files(out)
        assert "secret.html" not in files
        assert not [k for k in files if k.endswith(".pdf")]
        assert "monitoring/assets/gto1.jpg" in files and "assets/photos/gto1_t.jpg" in files


class TestNoRealName:
    def test_no_real_customer_name_in_any_published_file(self, demo, psql):
        out, _, _ = demo
        names = [r[0] for r in psql("SELECT customer FROM public.plant")] + REAL
        hits = real_name_hits(out, names)
        assert not hits, "\n".join(hits[:20])

    def test_no_customer_reference_page_or_logo_is_linked(self, demo):
        out, _, _ = demo
        for k, p in pages(out).items():
            s = p.read_text(encoding="utf-8")
            assert "argia.com.mx/es/references" not in s and "argia.com.mx/en/references" not in s, k
            assert not re.search(r'class="clogo[^"]*" src="data:', s), k

    def test_the_demo_did_not_touch_the_real_data(self, demo, psql):
        """search_path is per process: the portal still reads the real names."""
        rows = dict((r[0], r[1]) for r in psql("SELECT plant_key, portfolio FROM plant"))
        assert "CAPEX" in rows.values()
        assert psql("SELECT customer FROM plant WHERE plant_key = 'GTO1'")[0][0].startswith("TAIGENE")


class TestArgiaSolar:
    def test_every_active_plant_is_argia_solar_n_with_both_pages(self, demo, psql):
        out, _, _ = demo
        nm = dict((r[0], int(r[1])) for r in psql("SELECT plant_key, n FROM demo.name_map"))
        for (k,) in psql("SELECT plant_key FROM plant WHERE active ORDER BY 1"):
            n = nm[k]
            for sec in ("report", "monitoring"):
                s = text(out, f"{sec}/argia-solar-{n}/index.html")
                assert f"ARGIA SOLAR {n}" in s, (sec, k)
                assert f"<title>ARGIA SOLAR {n} - ARGIA</title>" in s or sec == "monitoring"

    def test_the_name_is_never_title_cased(self, demo):
        out, _, _ = demo
        bad = [k for k, p in pages(out).items() if re.search(r"Argia Solar \d|argia solar \d", p.read_text(encoding="utf-8"))]
        assert not bad, bad[:5]

    def test_the_open_report_button_opens_the_renamed_report(self, demo, psql):
        out, _, _ = demo
        s = text(out, "monitoring/argia-solar-2/index.html")
        assert 'href="/report/argia-solar-2/"' in s and "Open report" in s

    def test_every_plant_is_ppa(self, demo):
        out, _, _ = demo
        for k, p in pages(out).items():
            s = p.read_text(encoding="utf-8")
            assert 'class="pill off">CAPEX' not in s, k
            assert 'kicker">CAPEX <span' not in s, k            # no CAPEX section on the overviews
            assert 'data-g="capex"' not in s, k                 # no CAPEX group on the map
            assert "0 CAPEX" not in s and "teal = CAPEX" not in s, k

    def test_every_logo_is_the_argia_solar_logo(self, demo):
        out, _, _ = demo
        srcs = set()
        for p in pages(out).values():
            srcs |= set(re.findall(r'<img class="clogo[^"]*" src="([^"]+)"', p.read_text(encoding="utf-8")))
        assert srcs == {"/assets/demo/argia-solar.png"}
        assert (out / "assets/demo/argia-solar.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_the_logo_is_grey_and_black_on_mouse_over(self, demo):
        out, _, _ = demo
        s = text(out, "report/plants/index.html")
        assert ".clogo{filter:grayscale(1)!important;opacity:.32" in s
        assert ".pcard:hover .clogo,tr:hover .clogo,.clogo:hover{opacity:1}" in s


class TestCapexPricedAsPpa:
    def test_a_former_capex_plant_earns_at_the_fleet_tariff(self, demo, psql):
        out, _, _ = demo
        t = float(psql("SELECT tariff_mxn_per_kwh FROM demo.plant WHERE plant_key = 'GTO2'")[0][0])
        assert t == pytest.approx(2.3)                       # the seed's PPA tariff
        s = text(out, "report/argia-solar-2/index.html")
        assert "2.300 MXN/kWh" in s and "Est. revenue" in s

    def test_the_former_capex_losses_have_pesos(self, demo, psql):
        rows = psql("SELECT count(*), count(lost_mxn) FROM demo.loss_daily l JOIN public.plant p USING (plant_key)"
                    " WHERE p.portfolio = 'CAPEX'")
        n, priced = map(int, rows[0])
        assert n > 0 and priced == n

    def test_the_ppa_plants_keep_their_own_contract(self, psql):
        a = psql("SELECT count(*) FROM (SELECT c.* FROM public.contract_monthly c JOIN public.plant p USING (plant_key)"
                 " WHERE p.portfolio = 'PPA' EXCEPT SELECT c.* FROM demo.contract_monthly c"
                 " JOIN public.plant p USING (plant_key) WHERE p.portfolio = 'PPA') x")
        assert a[0][0] == "0"

    def test_demo_plant_has_every_column_of_plant(self, psql):
        cols = lambda sch: psql("SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns"
                                f" WHERE table_schema = '{sch}' AND table_name = 'plant'")[0][0]
        assert cols("demo") == cols("public")

    def test_the_tariff_is_weighted_by_measured_kwh(self, fresh_db, psql):
        """Two PPA plants at 2.00 and 3.00 MXN/kWh; the 3.00 one produced
        three times as much -> (2*1 + 3*3) / 4 = 2.75, not the plain 2.50."""
        _apply_demo_schema(fresh_db)
        psql("UPDATE contract_monthly SET tariff_mxn = CASE WHEN plant_key = 'GTO1' THEN 2.0 ELSE 3.0 END"
             " WHERE year = 2020 OR true;")
        psql("DELETE FROM contract_monthly WHERE plant_key NOT IN ('GTO1', 'MEX1');")
        psql("DELETE FROM daily_production WHERE date_trunc('month', prod_date) = date '2026-01-01';")
        psql("INSERT INTO daily_production (plant_key, prod_date, energy_kwh, source) VALUES"
             " ('GTO1', '2026-01-10', 1000, 'v2'), ('MEX1', '2026-01-10', 3000, 'v2');")
        t = psql("SELECT tariff_mxn FROM demo.ppa_tariff_month WHERE year = 2026 AND month = 1")[0][0]
        assert float(t) == pytest.approx(2.75)
        g = psql("SELECT tariff_mxn FROM demo.contract_monthly WHERE plant_key = 'GTO2' AND year = 2026 AND month = 1")
        assert float(g[0][0]) == pytest.approx(2.75)


class TestEveryPage:
    def test_complete_bilingual_no_em_dash_no_none(self, demo):
        out, _, _ = demo
        bad = []
        pats = re.compile(r">\s*(None|nan|NaN|undefined)\s*<|\bNone (kWh|MXN|kW|%)|\bnan (kWh|MXN|kW|%)")
        for k, p in pages(out).items():
            s = p.read_text(encoding="utf-8")
            if EM in s:
                bad.append(f"{k}: em dash")
            if k.endswith(".html"):
                if not s.lstrip().lower().startswith("<!doctype html"):
                    bad.append(f"{k}: not a document")
                if "data-es=" not in s and 'http-equiv="refresh" content="0' not in s:
                    bad.append(f"{k}: not bilingual")
                if pats.search(s) or "Traceback (most recent call last)" in s:
                    bad.append(f"{k}: None/nan/traceback")
        assert not bad, bad[:10]

    def test_no_dead_button_on_the_reconciliation_page(self, demo):
        out, _, _ = demo
        s = markup(out / "monitoring/recon/index.html")
        assert "Invoice annexes" not in s and "/report/invoices/" not in s

    def test_the_header_has_no_ask_button_and_no_user_menu(self, demo):
        out, _, _ = demo
        for k in ("index.html", "report/index.html", "monitoring/index.html", "map/index.html"):
            s = text(out, k)
            assert 'class="ib ask"' not in s and 'href="/account/"' not in s, k
            m = markup(out / k)                                   # own login: no user menu, no log-out button
            assert 'id="ubtn"' not in m and 'id="umenu"' not in m and "argiaLogout()" not in m, k
            assert "location.href='/ask/'" not in s, k
            assert "demo.argia.com.mx" in s or k == "index.html"


class TestSafety:
    def test_the_demo_session_cannot_write(self, demo, pg_env):
        env = dict(pg_env, PGOPTIONS="-c search_path=demo,public -c default_transaction_read_only=on")
        r = subprocess.run(["psql", "-X", "-d", "argia_mont", "-c", "UPDATE public.plant SET notes = 'x';"],
                           env=env, capture_output=True, text=True)
        assert r.returncode != 0 and "read-only" in r.stderr

    def test_a_leak_publishes_nothing_and_keeps_the_last_demo(self, demo, pg_env):
        """A scrub that misses (simulated: no scrub at all) must stop the
        publish: exit 1, the leak named in the journal, the last good demo
        untouched, no staging folder left behind."""
        out, _, tmp = demo
        before = {k: p.read_bytes() for k, p in all_files(out).items()}
        rc, so, se = run_demo(pg_env, out, tmp / "portal", patch=lambda m: setattr(m, "scrub", lambda t, r, n: (t, 0)))
        assert rc == 1 and "REFUSED" in se and "demo_gen: LEAK" in se
        assert {k: p.read_bytes() for k, p in all_files(out).items()} == before
        assert not (out.parent / "www.staging").exists()

    def test_no_demo_views_no_demo(self, demo, pg_env, psql):
        out, _, tmp = demo
        before = sorted(all_files(out))
        psql("DROP SCHEMA demo CASCADE;")
        try:
            rc, so, se = run_demo(pg_env, out, tmp / "portal")
            assert rc == 2 and "REFUSED" in se
            assert sorted(all_files(out)) == before          # the last good demo stays online
        finally:
            _apply_demo_schema(pg_env)
