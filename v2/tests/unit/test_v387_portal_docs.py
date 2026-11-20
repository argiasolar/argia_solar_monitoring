"""v387 - the portal documentation (docs/portal_docs -> portal.argia.com.mx/docs/).

The page explains every figure with the rule's numbers read from the code at build time, so
these tests are the drift guard: a renamed constant, a section missing in one language, a
broken in-page link, a missing picture or a dash stops the build here, not on the portal."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
DOCS = V2 / "docs" / "portal_docs"


@pytest.fixture(scope="module")
def B():
    spec = importlib.util.spec_from_file_location("portal_docs_build", DOCS / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def built(B, tmp_path_factory):
    out = tmp_path_factory.mktemp("docs")
    r = B.build(str(out), stamp="2026-11-20")
    return out, r, (out / "index.html").read_text(encoding="utf-8")


def test_builds_with_both_languages_and_every_picture(built):
    out, r, page = built
    assert len(r["sections"]) >= 18 and r["images"] == len(list((out / "img").glob("*.jpg")))
    assert '<template id="tpl-en">' in page and '<template id="tpl-es">' in page
    assert chr(0x2014) not in page and chr(0x2013) not in page and "{{" not in page


def test_the_logo_not_the_name(built):
    page = built[2]
    bar = page[page.index('<header class="bar">'):page.index("</header>")]
    assert 'src="data:image/png;base64,' in bar                 # the official wordmark picture
    assert ">ARGIA<" not in bar


def test_sections_match_and_the_portal_links_land(built):
    page = built[2]
    data = json.loads(re.search(r'<script id="docs-data" type="application/json">(.*?)</script>', page, re.S).group(1).replace("<\\/", "</"))
    ids = {lang: [s["id"] for s in data["toc"][lang]] for lang in ("en", "es")}
    assert ids["en"] == ids["es"]
    src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    targets = re.findall(r"\['[a-z0-9 ,-]+','([a-z]+)'\]", src[src.index("var DOCS="):src.index("function docOf")])
    assert targets and set(targets) <= set(ids["en"])          # every "?" on a card opens a real section


def test_numbers_come_from_the_code(B):
    from argia.epi import views as V
    from argia.kpi import availability as A
    assert B.resolve("{{pct:views.GAP_SHARE}}", "t") == f"{V.GAP_SHARE * 100:.0f} %"
    assert B.resolve("{{c:availability.STEP_MIN}}", "t") == str(A.STEP_MIN)
    assert B.resolve("{{pct1:design.DEFAULT_DEGRADATION}}", "t") == "0.55 %"
    assert B.resolve("{{c:mg.PR_MAX}}", "t") == "1.05"                        # parsed, monitoring_gen never imported
    with pytest.raises(SystemExit):
        B.resolve("{{c:views.NO_SUCH_RULE}}", "t")


def test_every_placeholder_in_the_sources_resolves(B):
    for lang in ("en", "es"):
        text = (DOCS / f"{lang}.md").read_text(encoding="utf-8")
        assert B.PH.findall(text)
        assert "{{" not in B.resolve(text, lang)


def test_markdown_subset(B):
    h = B.blocks("intro **bold** and [link](#kpis)\n\n- a\n- b\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n```formula\na < b\n```", "en", set())
    assert "<strong>bold</strong>" in h and '<a href="#kpis">link</a>' in h and "<ul><li>a</li>" in h
    assert "<td>1</td>" in h and '<pre class="formula">a &lt; b</pre>' in h


def test_jpeg_size_without_pillow(B):
    pic = sorted((DOCS / "img").glob("*.jpg"))[0]
    w, h = B.jpeg_size(str(pic))
    assert w > 100 and h > 50
    assert B.jpeg_size(str(DOCS / "en.md")) is None


def test_portal_wiring():
    mg = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    assert "DOCS_BASE = '/docs/'" in mg and "FOLD_JS.replace('__DOCS__', DOCS_BASE)" in mg
    demo = (V2 / "server" / "bundle" / "demo_gen.py").read_text(encoding="utf-8")
    assert "MG.DOCS_BASE = ''" in demo                               # the demo has no documentation
    chrome = (V2 / "server" / "bundle" / "portal_chrome.py").read_text(encoding="utf-8")
    assert '<a href="/docs/">' in chrome and "How the monitoring works" in chrome


def test_page_script_parses(built, tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    page = built[2]
    js = page[page.rindex("<script>") + 8:page.rindex("</script>")]
    f = tmp_path / "docs.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
