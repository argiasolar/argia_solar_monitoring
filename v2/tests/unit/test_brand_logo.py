"""v251 — the wordmark is ARGIA / Smart Energy Solutions, not ARGIA SOLAR
(Tomasz 2026-09-10: "it looks that it is not about solar any more, it is
now the portal for whole company").

The three-line tagline is the whole reason these tests exist: the old
one-line wordmark read fine at 26px, this one does not. Every surface
that shows the full lockup must give it at least LOGO_HEIGHT_PX, and the
one place too tight for that (the daily mail header, where the logo is
deliberately smaller than its title) must take the compact mark instead.
"""
from __future__ import annotations

import base64
import io
import pathlib
import re
import struct
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))
sys.path.insert(0, str(V2 / "scripts"))

import argia_logo as L   # noqa: E402


def png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def decode(uri: str) -> bytes:
    m = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", uri)
    assert m, "the asset must be a base64 PNG data URI"
    return base64.b64decode(m.group(1))


class TestAsset:
    def test_the_full_lockup_is_a_real_transparent_png_of_the_right_shape(self):
        w, h = png_size(decode(L.LOGO_URI))
        assert (w, h) == (764, 120)
        assert 6.2 < w / h < 6.5, "the lockup is ~6.36:1 — the old ARGIA SOLAR one was 8.8:1"
        assert len(L.LOGO_URI) < 40_000, "keep the data URI small; every page and every mail carries it"

    def test_the_compact_mark_is_the_wordmark_without_the_tagline(self):
        w, h = png_size(decode(L.MARK_URI))
        assert 4.1 < w / h < 4.5, "'ARGIA' alone is ~4.3:1"
        assert len(L.MARK_URI) < len(L.LOGO_URI)

    def test_both_assets_are_transparent_so_they_sit_on_any_header(self):
        png = pytest.importorskip("PIL.Image")
        for uri in (L.LOGO_URI, L.MARK_URI):
            im = png.open(io.BytesIO(decode(uri))).convert("RGBA")
            assert min(p[3] for p in im.getdata()) == 0, "the artwork must have a transparent ground"

    def test_the_alt_text_names_the_company_not_the_solar_business(self):
        assert L.LOGO_ALT == "ARGIA — Smart Energy Solutions"
        assert L.LOGO_HEIGHT_PX == 34


class TestEverySurface:
    """Each file that renders the full lockup gives it 34px or more."""

    SURFACES = [
        (BUNDLE / "portal_chrome.py", r"\.wm \.wmlogo\{height:(\d+)px"),
        (BUNDLE / "report_gen.py", r"\.logo\{height:(\d+)px"),
        (V2 / "server" / "monitoring_gen.py", r"\.logo\{height:(\d+)px"),
        (BUNDLE / "setup_app.py", r'alt="\{LOGO_ALT\}" style="height:(\d+)px'),
        (BUNDLE / "auth_app.py", r"\.logo\{\{height:(\d+)px"),
        (V2 / "argia" / "report" / "daily.py", r"\.lockup img\{height:(\d+)px"),
        (V2 / "argia" / "report" / "dashboard_html.py", r'style="height:(\d+)px; display:block;"'),
        (V2 / "argia" / "finance" / "annex.py", r'alt="ARGIA — Smart Energy Solutions" style="height:(\d+)px"'),
        (V2 / "argia" / "finance" / "report.py", r'alt="ARGIA — Smart Energy Solutions" style="height:(\d+)px"'),
    ]

    def test_no_surface_shows_the_lockup_too_small_to_read(self):
        for path, rx in self.SURFACES:
            src = path.read_text(encoding="utf-8")
            found = [int(x) for x in re.findall(rx, src)]
            assert found, f"{path.name}: no logo height matched {rx}"
            assert min(found) >= L.LOGO_HEIGHT_PX, f"{path.name} shows the lockup at {min(found)}px — the tagline blurs"

    def test_the_daily_mail_takes_the_compact_mark_at_its_old_small_size(self):
        import daily_perf_mail as dpm
        src = (V2 / "scripts" / "daily_perf_mail.py").read_text(encoding="utf-8")
        assert 'height="19"' in src and "height:19px" in src, "v176: the mail logo stays smaller than its title"
        assert dpm.logo_png() == decode(L.MARK_URI), "the mail must embed the compact mark, not the three-line lockup"


class TestNoSolarBrandingLeft:
    def test_the_chrome_no_longer_says_argia_solar(self):
        offenders = []
        for path in list((V2 / "server").rglob("*.py")) + list((V2 / "argia").rglob("*.py")) + list((V2 / "scripts").rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"ARGIA SOLAR|Argia Solar", line):
                    # the accountants' cost centre 842 and the invoicing workbook are real
                    # names of real things — they are data, not our branding
                    if "workbook" in line or "Invoicing_Overview" in line or "costcenter" in path.name or "old ARGIA SOLAR wordmark" in line:
                        continue
                    offenders.append(f"{path.relative_to(V2)}:{n}: {line.strip()[:70]}")
        assert not offenders, "user-facing ARGIA SOLAR branding left:\n" + "\n".join(offenders)
