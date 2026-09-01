"""Publishing the invoice annexes to /invoices/ (button target).

The generator itself (fail-closed gate, rollups) is covered by
test_invoice_annex.py; this file covers the publishing layer added for
the 2026-09-01 close: the month/plant scan, the index page people click
through, and the chromium detection that decides whether true PDFs are
possible.
"""
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

import invoice_publish as ip                               # noqa: E402


def publish_tree(tmp_path, months):
    for ym, plants in months.items():
        d = tmp_path / ym
        d.mkdir()
        stamp = ym.replace("-", "")
        for name, pdf in plants:
            (d / f"factura_{name}_{stamp}.html").write_text("x")
            if pdf:
                (d / f"factura_{name}_{stamp}.pdf").write_text("x")
    return str(tmp_path)


class TestScan:
    def test_finds_months_plants_and_pdf_state(self, tmp_path):
        root = publish_tree(tmp_path, {
            "2026-08": [("TAIGENE", True), ("SAG", False)]})
        got = ip.scan_months(root)
        assert got == {"2026-08": [("SAG", True, False),
                                   ("TAIGENE", True, True)]}

    def test_multiword_factura_names_survive_the_parse(self, tmp_path):
        """PLASTIC_OMNIUM has an underscore of its own — the yyyymm
        must be split off the END, not the first underscore."""
        root = publish_tree(tmp_path, {
            "2026-07": [("PLASTIC_OMNIUM", True)]})
        assert ip.scan_months(root) == {
            "2026-07": [("PLASTIC_OMNIUM", True, True)]}

    def test_ignores_stray_directories_and_files(self, tmp_path):
        (tmp_path / "not-a-month").mkdir()
        (tmp_path / "2026-08").mkdir()
        (tmp_path / "2026-08" / "notes.txt").write_text("x")
        assert ip.scan_months(str(tmp_path)) == {}

    def test_empty_root_is_empty(self, tmp_path):
        assert ip.scan_months(str(tmp_path)) == {}


class TestIndexPage:
    MONTHS = {"2026-08": [("TAIGENE", True, True),
                          ("HOLIDAY_INN", True, False)],
              "2026-07": [("TAIGENE", True, True)]}

    def test_newest_month_comes_first(self):
        page = ip.render_index(self.MONTHS)
        assert page.index("2026-08") < page.index("2026-07")

    def test_pdf_link_is_a_download(self):
        page = ip.render_index(self.MONTHS)
        assert ('href="2026-08/factura_TAIGENE_202608.pdf" download'
                in page)

    def test_a_missing_pdf_says_pending_instead_of_linking(self):
        page = ip.render_index(self.MONTHS)
        assert "factura_HOLIDAY_INN_202608.pdf" not in page
        assert "PDF pending" in page

    def test_client_names_appear(self):
        page = ip.render_index(self.MONTHS)
        assert "TAIGENE" in page and "HOLIDAY INN" in page

    def test_bilingual_like_the_rest_of_the_site(self):
        """EN users read EN, ES users read ES (stored language); the
        PDFs themselves stay Spanish."""
        page = ip.render_index(self.MONTHS)
        assert 'data-en="Client" data-es="Cliente"' in page
        assert 'data-en="August 2026" data-es="Agosto 2026"' in page
        assert "argia_lang" in page                 # applies stored lang

    def test_each_month_has_a_summary_line(self):
        page = ip.render_index(
            self.MONTHS,
            records={"2026-08": {
                "TAIGENE": (132955.8, 262587.71, "OK"),
                "HOLIDAY_INN": (45553.5, 112152.72, "OK")}})
        assert "178,509 kWh" in page
        assert "$374,740.43 MXN" in page

    def test_blocked_plants_are_shown_not_hidden(self):
        page = ip.render_index(
            {"2026-08": [("TAIGENE", True, True)]},
            blocked_now={"2026-08": [("VITALMEX", "reconciliation not "
                                                  "closed")]})
        assert "VITALMEX" in page
        assert "reconciliation not closed" in page

    def test_empty_state_is_honest(self):
        page = ip.render_index({})
        assert "No annexes published yet" in page

    def test_the_way_back_to_reports(self):
        assert 'href="/"' in ip.render_index({})

    def test_not_indexable(self):
        assert 'name="robots" content="noindex,nofollow"' in \
            ip.render_index({})


class TestChromium:
    def test_missing_chromium_is_none_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(ip.shutil, "which", lambda n: None)
        assert ip.find_chromium() is None

    def test_first_available_wins(self, monkeypatch):
        monkeypatch.setattr(
            ip.shutil, "which",
            lambda n: "/usr/bin/" + n if n == "chromium" else None)
        assert ip.find_chromium() == "/usr/bin/chromium"


class TestInvoicingCheck:
    """Tomasz 2026-09-01: "always check last month invoice vs the last
    day status, keep it somewhere as invoicing." The close row's
    billing_kwh is the month-end vendor position; this check runs at
    publish time and every plant-month is stored in the invoicing
    table."""

    def test_matching_invoice_is_ok(self):
        st, dk, dp = ip.invoice_check(132955.8, 132955.8)
        assert (st, dk, dp) == ("OK", 0.0, 0.0)

    def test_rounding_noise_stays_ok(self):
        st, _, dp = ip.invoice_check(102513.3, 102513.5)
        assert st == "OK" and abs(dp) < 0.001

    def test_a_real_shortfall_is_a_mismatch(self):
        """The stale-billable bug's shape: 2.1% under the close."""
        st, dk, _ = ip.invoice_check(130125.5, 132951.5)
        assert st == "MISMATCH" and dk == -2826.0

    def test_an_overbill_is_a_mismatch_too(self):
        st, _, _ = ip.invoice_check(45742.2, 45552.3)
        assert st == "MISMATCH"

    def test_no_close_row_is_no_basis_not_a_pass(self):
        assert ip.invoice_check(1000.0, None)[0] == "NO_BASIS"
        assert ip.invoice_check(None, 1000.0)[0] == "NO_BASIS"
        assert ip.invoice_check(1000.0, 0)[0] == "NO_BASIS"


class TestIndexCarriesTheRegister:
    MONTHS = {"2026-08": [("TAIGENE", True, True)]}

    def test_recorded_kwh_and_mxn_appear(self):
        page = ip.render_index(
            self.MONTHS,
            records={"2026-08": {"TAIGENE": (132955.8, 262587.71, "OK")}})
        assert "132,955.8" in page
        assert "$262,587.71" in page

    def test_a_mismatch_is_flagged_on_the_row(self):
        page = ip.render_index(
            self.MONTHS,
            records={"2026-08": {"TAIGENE": (1.0, 2.0, "MISMATCH")}})
        assert "MISMATCH" in page

    def test_no_record_renders_a_dash_not_a_crash(self):
        page = ip.render_index(self.MONTHS)
        assert "&mdash;" in page


class TestDrivePush:
    def test_missing_credentials_is_a_skip_not_a_crash(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_ARCHIVE_FOLDER_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CREDENTIALS", raising=False)
        assert ip.push_to_drive("2026-08", "/nonexistent") == 0

    def test_a_drive_error_never_breaks_the_publish(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_ARCHIVE_FOLDER_ID", "x")
        monkeypatch.setenv("GOOGLE_CREDENTIALS", "not-json")
        assert ip.push_to_drive("2026-08", "/nonexistent") == 0


class TestDownloadAllBundle:
    """One click, every plant, one month (Tomasz 2026-09-01)."""

    def test_zip_holds_every_pdf_of_the_month(self, tmp_path):
        import zipfile
        d = tmp_path / "2026-08"
        d.mkdir()
        for n in ("TAIGENE", "SAG"):
            (d / f"factura_{n}_202608.pdf").write_bytes(b"%PDF-x")
        (d / "factura_TAIGENE_202608.html").write_text("x")   # not bundled
        assert ip.build_month_zip(str(d), "2026-08") == 2
        z = zipfile.ZipFile(str(d / "facturas_202608.zip"))
        assert sorted(z.namelist()) == ["factura_SAG_202608.pdf",
                                        "factura_TAIGENE_202608.pdf"]

    def test_rebuild_replaces_never_appends(self, tmp_path):
        import zipfile
        d = tmp_path / "2026-08"
        d.mkdir()
        (d / "factura_TAIGENE_202608.pdf").write_bytes(b"%PDF-1")
        ip.build_month_zip(str(d), "2026-08")
        (d / "factura_SAG_202608.pdf").write_bytes(b"%PDF-2")
        ip.build_month_zip(str(d), "2026-08")
        z = zipfile.ZipFile(str(d / "facturas_202608.zip"))
        assert len(z.namelist()) == 2

    def test_no_pdfs_means_no_bundle_even_a_stale_one(self, tmp_path):
        d = tmp_path / "2026-09"
        d.mkdir()
        (d / "facturas_202609.zip").write_bytes(b"old")
        assert ip.build_month_zip(str(d), "2026-09") == 0
        assert not (d / "facturas_202609.zip").exists()

    def test_month_zips_scan(self, tmp_path):
        d = tmp_path / "2026-08"
        d.mkdir()
        (d / "facturas_202608.zip").write_bytes(b"z")
        (d / "not_a_bundle.zip").write_bytes(b"z")
        assert ip.month_zips(str(tmp_path)) == {"2026-08": True}

    def test_the_button_appears_only_when_the_bundle_exists(self):
        months = {"2026-08": [("TAIGENE", True, True)]}
        with_zip = ip.render_index(months, zips={"2026-08": True})
        without = ip.render_index(months)
        assert 'href="2026-08/facturas_202608.zip" download' in with_zip
        assert "facturas_202608.zip" not in without

    def test_the_button_is_bilingual(self):
        page = ip.render_index({"2026-08": [("TAIGENE", True, True)]},
                               zips={"2026-08": True})
        assert ('data-en="Download all (ZIP)"'
                ' data-es="Descargar todo (ZIP)"') in page
