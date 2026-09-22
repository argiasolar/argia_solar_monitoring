"""v262 - the render-time em-dash scrub.

v261 swept the templates; this is the guard for text the templates do
not control: alert messages opened before v261, notes typed into the
PMO sheets, ticket comments, json.dumps escapes. Every page, mail and
PDF writer passes its output through plain() once - and these tests pin
that every writer does.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))

from plain_text import plain as bundle_plain          # noqa: E402
from argia.core.text import plain                     # noqa: E402
from argia.alerts import emailer                      # noqa: E402

EM = chr(0x2014)
ESC = "\\" + "u2014"
ENT = "&" + "mdash;"
NUM = "&#" + "8212;"


class TestPlain:
    def test_clause_separator_becomes_spaced_hyphen(self):
        assert plain(f"1,440 kWh lost {EM} $3,611 MXN") == "1,440 kWh lost - $3,611 MXN"

    def test_tight_dash_between_words_is_spaced(self):
        assert plain(f"Monthly close{EM}the invoice gate") == "Monthly close - the invoice gate"

    def test_lone_placeholder_becomes_hyphen(self):
        assert plain(EM) == "-"
        assert plain(f"<span>{EM} kW</span>") == "<span>- kW</span>"

    def test_json_escape_and_html_entities_are_caught_too(self):
        assert plain('{"note": "a ' + ESC + ' b"}') == '{"note": "a - b"}'
        assert plain(f"a {ENT} b") == "a - b"
        assert plain(f"a{NUM}b") == "a - b"

    def test_text_without_a_dash_is_returned_as_is(self):
        s = "nothing to do - already plain"
        assert plain(s) is s

    def test_non_strings_pass_through(self):
        assert plain(None) is None
        assert plain(5) == 5

    def test_bundle_and_package_copies_are_the_same_function(self):
        cases = [f"a {EM} b", EM, f"x{EM}y", ESC, f"a {ENT} b", "plain", None]
        assert [bundle_plain(c) for c in cases] == [plain(c) for c in cases]
        a = (V2 / "server" / "bundle" / "plain_text.py").read_text(encoding="utf-8")
        b = (V2 / "argia" / "core" / "text.py").read_text(encoding="utf-8")
        body = lambda s: s[s.index("import re"):]   # noqa: E731  - same code below the docstring
        assert body(a) == body(b)


class TestEveryWriterScrubs:
    """Source-level pins: the scrub sits at each output chokepoint."""

    @pytest.mark.parametrize("rel, needle", [
        ("server/bundle/portal_chrome.py", "return plain('<!DOCTYPE html>"),
        ("server/bundle/portal_gen.py", "fh.write(plain(content))"),
        ("server/monitoring_gen.py", "fh.write(plain(content))"),
        ("server/bundle/report_gen.py", "fh.write(plain(content))"),
        ("server/bundle/ask_app.py", "@app.after_request\ndef _no_em_dash(r):"),
        ("server/bundle/auth_app.py", "@app.after_request\ndef _no_em_dash(r):"),
        ("server/bundle/fin_app.py", "@app.after_request\ndef _no_em_dash(r):"),
        ("server/bundle/maint_app.py", "@app.after_request\ndef _no_em_dash(r):"),
        ("server/bundle/setup_app.py", "@app.after_request\ndef _no_em_dash(r):"),
        ("argia/alerts/emailer.py", "subject, body = _plain(subject), _plain(body)"),
        ("argia/alerts/emailer.py", 'msg.add_alternative(_plain(html), subtype="html")'),
        ("argia/report/daily.py", "return _plain(_render_html(data))"),
        ("argia/report/dashboard_html.py", "return _plain(_template().replace("),
        ("argia/finance/annex.py", "return _plain(_render_annex_html(payload, generated_at, default_ym))"),
        ("scripts/invoice_publish.py", "f.write(_plain(idx))"),
    ])
    def test_writer_passes_its_output_through_plain(self, rel, needle):
        src = (V2 / rel).read_text(encoding="utf-8")
        assert needle in src, rel

    def test_monitoring_gen_writes_both_of_its_writers_through_plain(self):
        src = (V2 / "server/monitoring_gen.py").read_text(encoding="utf-8")
        assert src.count("fh.write(plain(content))") == 2   # write() and write_root()


class TestMailAndChrome:
    def test_mail_subject_and_both_bodies_are_scrubbed(self):
        msg = emailer.build_html_email(f"[ARGIA] 16 Sep {EM} 1 critical", f"plain {EM} text",
                                       f"<p>html {EM} text</p>", "m@x", ["a@b"])
        assert msg["Subject"] == "[ARGIA] 16 Sep - 1 critical"
        parts = [p.get_content() for p in msg.iter_parts()]
        assert any("plain - text" in p for p in parts) and any("html - text" in p for p in parts)
        assert not any(EM in p for p in parts)

    def test_chrome_page_scrubs_a_dash_that_arrived_in_the_body(self):
        import portal_chrome as C
        out = C.page("Tickets", f"<div>Open {EM} waiting</div>", "maintenance")
        assert "Open - waiting" in out and EM not in out

    def test_flask_hook_scrubs_html_but_leaves_other_types_alone(self):
        Flask = pytest.importorskip("flask").Flask   # the laptop venv has no flask; CI and pio06 do
        app = Flask("t")

        @app.after_request
        def _no_em_dash(r):
            if r.mimetype == 'text/html' and not r.direct_passthrough:
                r.set_data(plain(r.get_data(as_text=True)))
            return r

        @app.get("/h")
        def h():
            return f"<b>a {EM} b</b>"

        @app.get("/j")
        def j():
            return app.response_class(f'{{"k": "{EM}"}}', mimetype="application/json")

        c = app.test_client()
        assert c.get("/h").get_data(as_text=True) == "<b>a - b</b>"
        assert EM in c.get("/j").get_data(as_text=True)   # data endpoints untouched
