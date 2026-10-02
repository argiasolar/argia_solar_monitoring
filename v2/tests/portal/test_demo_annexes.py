"""v288 - the example PDFs for demo.argia.com.mx (Reports > Annexes).

demo_annexes runs for real on the seeded PostgreSQL (real customer names in
the seed, as in production). CI has no chromium, so the printer is replaced
by a stand-in that keeps the HTML it was given: the tests read exactly what
would have been printed. The real print was checked on the sandbox and is
checked on pio06 by the deploy (PDF sizes, pdftotext scan).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys

import pytest

from tests.portal.conftest import V2, reset_db
from tests.portal.test_demo_site import REAL, _apply_demo_schema

BUNDLE = V2 / "server" / "bundle"


def run_annexes(env, out, patch=None):
    """demo_annexes.main() in this process -> (rc, stdout, stderr, {file: html})."""
    printed, seen_env = {}, {}
    saved_env, saved_argv, saved_path = dict(os.environ), list(sys.argv), list(sys.path)
    o, e = io.StringIO(), io.StringIO()
    try:
        os.environ.update({k: env[k] for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH")})
        os.environ["GOOGLE_SHEET_ID_V2"] = "must-be-dropped"     # a secret the job must not keep
        for m in ("portal_gen", "report_gen", "monitoring_gen", "portal_chrome", "demo_gen", "demo_annexes",
                  "argia_client_logos", "losses_view", "app_view"):
            sys.modules.pop(m, None)
        sys.path.insert(0, str(BUNDLE))
        import demo_annexes

        def fake_render(html_path, pdf_path, fragment=""):
            printed[os.path.basename(pdf_path)] = (open(html_path, encoding="utf-8").read(), fragment)
            seen_env["GOOGLE_SHEET_ID_V2"] = os.environ.get("GOOGLE_SHEET_ID_V2")
            seen_env["PGOPTIONS"] = os.environ.get("PGOPTIONS")
            with open(pdf_path, "wb") as fh:
                fh.write(b"%PDF-1.4 " + b"x" * 6000)
            return True
        demo_annexes.RENDER = fake_render
        if patch:
            patch(demo_annexes)
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = demo_annexes.main(["demo_annexes.py", str(out)])
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        sys.argv, sys.path[:] = saved_argv, saved_path
        for m in ("portal_gen", "report_gen", "monitoring_gen", "portal_chrome", "demo_gen", "demo_annexes",
                  "argia.alerts.naming"):
            sys.modules.pop(m, None)
    return rc, o.getvalue(), e.getvalue(), printed, seen_env


@pytest.fixture(scope="module")
def annexes(pg_env, tmp_path_factory):
    reset_db(pg_env)
    _apply_demo_schema(pg_env)
    out = tmp_path_factory.mktemp("annexes") / "annexes"
    rc, so, se, printed, seen = run_annexes(pg_env, out)
    assert rc == 0, so + se
    return out, so, printed, seen


def manifest(out):
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


class TestWhatIsMade:
    def test_every_emailed_report_type(self, annexes, psql):
        out, so, _, _ = annexes
        items = manifest(out)["items"]
        n = int(psql("SELECT count(*) FROM plant WHERE active")[0][0])
        groups = [it["group"] for it in items]
        assert groups.count("daily") == 2 and groups.count("financial") == 2 and groups.count("invoice") == n
        assert re.search(rf"demo_annexes: wrote {n + 4} PDFs .*0 leaks", so), so
        for it in items:
            assert (out / it["file"]).read_bytes().startswith(b"%PDF")
            assert re.match(r"^(ARGIA_SOLAR_|Factura_ARGIA_SOLAR_)[A-Za-z0-9_.-]+\.pdf$", it["file"]), it["file"]
            assert it["title_en"] and it["title_es"] and it["period"]

    def test_only_pdfs_and_the_manifest_are_published(self, annexes):
        out, _, _, _ = annexes
        assert {p.suffix for p in out.iterdir()} == {".pdf", ".json"}
        assert not (out.parent / "annexes.staging").exists()

    def test_both_daily_editions_and_both_financial_windows(self, annexes):
        out, _, printed, _ = annexes
        files = [it["file"] for it in manifest(out)["items"]]
        assert any("Daily_morning" in f for f in files) and any("Daily_evening" in f for f in files)
        frags = sorted(fr for f, (_, fr) in printed.items() if "Financial" in f)
        assert len(frags) == 2 and all(re.match(r"d0=\d{4}-\d\d-01&d1=\d{4}-\d\d-\d\d$", f) for f in frags)


    def test_the_daily_report_covers_the_whole_portfolio(self, annexes, psql):
        """v291: every plant is in the daily report (the portal's own report
        covers only plants with show_daily_report - in the demo that is all)."""
        _, _, printed, _ = annexes
        nums = [r[0] for r in psql("SELECT n FROM demo.name_map m JOIN plant p USING (plant_key) WHERE p.active")]
        for f, (html, _) in printed.items():
            if "Daily_" in f:
                missing = [n for n in nums if not re.search(rf"ARGIA SOLAR {n}(?!\d)", html)]
                assert not missing, (f, missing)


    def test_the_demo_views_put_every_plant_in_the_daily_report(self, annexes, psql):
        """Production keeps CAPEX plants out of the internal daily report
        (show_daily_report false); the demo view turns every plant on."""
        psql("UPDATE public.plant SET show_daily_report = false, show_dashboard = false WHERE portfolio = 'CAPEX';")
        try:
            rows = psql("SELECT bool_and(show_daily_report), bool_and(show_dashboard) FROM demo.plant")
            assert rows == [["t", "t"]]
        finally:
            psql("UPDATE public.plant SET show_daily_report = true, show_dashboard = true WHERE portfolio = 'CAPEX';")


class TestNoRealCustomer:
    def test_no_real_name_in_anything_printed(self, annexes, psql):
        _, _, printed, _ = annexes
        names = [r[0] for r in psql("SELECT customer FROM public.plant")] + REAL
        words = set()
        for nm in names:
            head = re.split(r"\s+(?:PPA|CAPEX)\b|\(", nm)[0].strip()
            words.add(head)
        bad = []
        for f, (html, _) in printed.items():
            body = re.sub(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "", html)
            for w in words:
                rx = r"(?<![A-Za-z0-9])" + r"[\s\-_]+".join(map(re.escape, re.split(r"[\s\-]+", w))) + r"(?![A-Za-z0-9])"
                if re.search(rx, body, 0 if len(w) <= 3 else re.I):
                    bad.append((f, w))
        assert not bad, bad[:10]

    def test_no_real_customer_logo_image_and_the_annex_carries_argia_solar(self, annexes):
        _, _, printed, _ = annexes
        sys.path.insert(0, str(BUNDLE))
        from argia_client_logos import CLIENT_LOGOS
        import demo_brand
        import base64
        ours = base64.b64encode(demo_brand.logo_png()).decode()[:200]
        for f, (html, _) in printed.items():
            for k, (nm, uri) in CLIENT_LOGOS.items():
                assert uri not in html, (f, nm)
            if f.startswith("Factura_"):
                assert ours in html, f
                assert re.search(r"ARGIA SOLAR \d+", html), f

    def test_no_ppa_or_laas_label_in_the_financial_report(self, annexes):
        _, _, printed, _ = annexes
        for f, (html, _) in printed.items():
            if "Financial" in f:
                assert "PPA + LaaS" not in html and 'data-en="Type"' not in html, f
            if f.startswith("Factura_"):
                assert "energía PPA" not in html, f


class TestSafety:
    def test_no_secret_and_a_read_only_demo_session_while_printing(self, annexes):
        _, _, _, seen = annexes
        assert seen["GOOGLE_SHEET_ID_V2"] is None
        assert "default_transaction_read_only=on" in seen["PGOPTIONS"] and "search_path=demo,public" in seen["PGOPTIONS"]

    def test_a_leak_publishes_nothing_and_keeps_the_last_set(self, annexes, pg_env):
        out, _, _, _ = annexes
        before = sorted(p.name for p in out.iterdir())
        rc, so, se, _, _ = run_annexes(pg_env, out, patch=lambda m: setattr(m.DG, "scrub", lambda t, r, n: (t, 0)))
        assert rc == 1 and "REFUSED" in se and "LEAK" in se
        assert sorted(p.name for p in out.iterdir()) == before

    def test_a_failed_print_publishes_nothing(self, annexes, pg_env):
        out, _, _, _ = annexes
        before = sorted(p.name for p in out.iterdir())

        def broken(m):
            m.RENDER = lambda h, p, fragment="": False
        rc, so, se, _, _ = run_annexes(pg_env, out, patch=broken)
        assert rc == 1 and "PDF FAILED" in se
        assert sorted(p.name for p in out.iterdir()) == before

    def test_no_demo_views_no_annexes(self, annexes, pg_env, psql):
        out, _, _, _ = annexes
        before = sorted(p.name for p in out.iterdir())
        psql("DROP SCHEMA demo CASCADE;")
        try:
            rc, so, se, printed, _ = run_annexes(pg_env, out)
            assert rc == 2 and "REFUSED" in se and not printed
            assert sorted(p.name for p in out.iterdir()) == before
        finally:
            _apply_demo_schema(pg_env)
