"""v387: build the portal documentation page (portal.argia.com.mx/docs/) from en.md / es.md.

Tomasz 20 Nov 2026: "create a document describing all the monitoring tool including all
details, mathematical formulas, assumptions, models, how we aggregate, how we average, how
we estimate everything ... so who ever has questions can see it immediately, include
screenshots ... use argia logo not name ... english and spanish ... what kind of API is
required on inverter/meter side".

* The two languages have the same sections in the same order with the same ids
  (``## Title {#id}``); the page shows one language at a time and keeps the reader's place.
* Every number that is a rule in the code is written as a placeholder and read from the
  code at build time - ``{{c:module.NAME}}`` (the value), ``{{pct:module.NAME}}`` (x100, %),
  ``{{pct1:...}}`` (x100, one decimal). The value is parsed from the source file, nothing is
  imported (monitoring_gen reads the database on import), so the page can never drift from
  the code: a renamed or removed constant stops the build.
* No external library: a small Markdown subset (headings, paragraphs, lists, tables, bold,
  italics, links, images, ```formula blocks) is converted here.
* Images: img/<lang>_<key>.jpg; ``![caption](key)`` picks the reader's language.

python3 docs/portal_docs/build.py [out_dir]   -> out_dir/index.html + out_dir/img/*.jpg
"""
from __future__ import annotations

import ast
import html
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.dirname(os.path.dirname(HERE))
LANGS = ("en", "es")
NO_DASH = (chr(0x2014), chr(0x2013))
SOURCES = {                       # placeholder module -> source file (parsed, never imported)
    "availability": "argia/kpi/availability.py",
    "views": "argia/epi/views.py",
    "engine": "argia/epi/engine.py",
    "model": "argia/epi/model.py",
    "gaps": "argia/epi/gaps.py",
    "live": "argia/epi/live.py",
    "thermal": "argia/analytics/thermal.py",
    "losses": "argia/analytics/losses.py",
    "layout": "argia/analytics/string_layout.py",
    "register": "argia/analytics/register_check.py",
    "station": "argia/analytics/station_check.py",
    "health": "argia/analytics/inverter_health.py",
    "recon": "argia/recon/engine.py",
    "design": "argia/kpi/design_model.py",
    "meter": "argia/telemetry/meter.py",
    "mg": "server/monitoring_gen.py",
}
UI = {
    "en": {"title": "Monitoring documentation", "eyebrow": "ARGIA portal",
           "sub": "How every figure on the monitoring pages is measured, calculated and checked.",
           "contents": "Contents", "search": "Search the contents", "none": "No section matches.",
           "close": "Close", "enlarge": "Open full size", "top": "Back to top", "lang": "Language",
           "back": "Back to the portal", "updated": "Generated from the code on", "menu": "Contents"},
    "es": {"title": "Documentación del monitoreo", "eyebrow": "Portal ARGIA",
           "sub": "Cómo se mide, se calcula y se verifica cada cifra de las páginas de monitoreo.",
           "contents": "Contenido", "search": "Buscar en el contenido", "none": "Ninguna sección coincide.",
           "close": "Cerrar", "enlarge": "Abrir en tamaño completo", "top": "Volver arriba", "lang": "Idioma",
           "back": "Volver al portal", "updated": "Generado desde el código el", "menu": "Contenido"},
}


# ------------------------------------------------------------------ constants from the code
def constants(mod: str) -> dict:
    """Top-level literal assignments of a source file (``A = 1`` and ``A, B = 1, 2``)."""
    tree = ast.parse(open(os.path.join(V2, SOURCES[mod]), encoding="utf-8").read())
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            try:
                val = ast.literal_eval(node.value)
            except Exception:  # noqa: BLE001 - not a literal: not a documented constant
                continue
            if isinstance(tgt, ast.Name):
                out[tgt.id] = val
            elif isinstance(tgt, ast.Tuple) and isinstance(val, tuple) and len(tgt.elts) == len(val):
                for e, v in zip(tgt.elts, val):
                    if isinstance(e, ast.Name):
                        out[e.id] = v
    return out


_CACHE: dict = {}
PH = re.compile(r"\{\{(c|pct|pct1):([a-z]+)\.([A-Z0-9_]+)\}\}")


def fmt_num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (int, float)):
        return f"{v:,}" if abs(v) >= 10000 else str(v)
    if isinstance(v, tuple):
        return "(" + ", ".join(fmt_num(x) for x in v) + ")"
    return str(v)


def resolve(text: str, where: str) -> str:
    def sub(m):
        kind, mod, name = m.groups()
        if mod not in SOURCES:
            sys.exit(f"{where}: unknown module {mod!r} in {m.group(0)}")
        consts = _CACHE.setdefault(mod, constants(mod))
        if name not in consts:
            sys.exit(f"{where}: {mod}.{name} is not a constant of {SOURCES[mod]} - the documentation is out of date")
        v = consts[name]
        if kind == "c":
            return fmt_num(v)
        x = round(float(v) * 100, 2)
        return (f"{x:.2f}".rstrip("0").rstrip(".") if kind == "pct1" or not x.is_integer() else f"{x:.0f}") + " %"
    return PH.sub(sub, text)


# ------------------------------------------------------------------ markdown subset
def inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((#[a-z0-9-]+|https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    return s


def table(lines):
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    head, body = rows[0], rows[2:]
    out = ['<div class="tw"><table><thead><tr>' + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"]
    for r in body:
        if len(r) != len(head):
            raise ValueError(f"table row has {len(r)} cells, header {len(head)}: {r}")
        out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
    return "".join(out) + "</tbody></table></div>"


IMG_DIR = os.path.join(HERE, "img")


def jpeg_size(path: str):
    """(width, height) of a JPEG from its SOF marker - no Pillow on the server. None if unreadable."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h)
        i += 2 + seg
    return None


def img_size(lang: str, key: str):
    return jpeg_size(os.path.join(IMG_DIR, f"{lang}_{key}.jpg"))


def blocks(md: str, lang: str, images: set) -> str:
    """Body of one section -> HTML."""
    out, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        if ln.startswith("```formula"):
            j = i + 1
            while lines[j].strip() != "```":
                j += 1
            out.append('<pre class="formula">' + html.escape("\n".join(lines[i + 1:j])) + "</pre>")
            i = j + 1
            continue
        if ln.startswith("### "):
            out.append(f"<h3>{inline(ln[4:].strip())}</h3>")
            i += 1
            continue
        m = re.match(r"^!\[(.+?)\]\(([a-z0-9_]+)\)\s*$", ln)
        if m:
            cap, key = m.groups()
            images.add(key)
            src = f"img/{lang}_{key}.jpg"
            wh = img_size(lang, key)
            dim = f' width="{wh[0]}" height="{wh[1]}"' if wh else ""
            out.append(f'<figure><button type="button" class="zoom" data-src="{src}">'
                       f'<img src="{src}"{dim} alt="{html.escape(cap)}" loading="lazy" decoding="async"></button>'
                       f"<figcaption>{inline(cap)}</figcaption></figure>")
            i += 1
            continue
        if ln.startswith("|"):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            out.append(table(lines[i:j]))
            i = j
            continue
        if re.match(r"^(- |\d+\. )", ln):
            ordered = bool(re.match(r"^\d+\. ", ln))
            items, j = [], i
            while j < len(lines) and re.match(r"^(- |\d+\. )", lines[j]):
                items.append(re.sub(r"^(- |\d+\. )", "", lines[j]))
                j += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
            i = j
            continue
        j = i
        para = []
        while j < len(lines) and lines[j].strip() and not re.match(r"^(```|### |\||- |\d+\. |!\[)", lines[j]):
            para.append(lines[j].strip())
            j += 1
        out.append(f"<p>{inline(' '.join(para))}</p>")
        i = j
    return "\n".join(out)


def render(lang: str):
    src = open(os.path.join(HERE, f"{lang}.md"), encoding="utf-8").read()
    src = resolve(src, f"{lang}.md")
    parts = [p for p in re.split(r"(?m)^## ", src) if p.strip()]
    toc, secs, images = [], [], set()
    for n, part in enumerate(parts, 1):
        head, body = part.split("\n", 1)
        m = re.match(r"^(.*?)\s*\{#([a-z0-9-]+)\}\s*$", head)
        if not m:
            sys.exit(f"{lang}.md: section without an id: {head!r}")
        title, sid = m.groups()
        h3 = re.findall(r"(?m)^### (.+)$", body)
        toc.append({"id": sid, "n": n, "t": title, "k": " ".join([title] + h3)})
        secs.append(f'<section class="sec" id="{sid}"><p class="num">{n:02d}</p><h2>{inline(title)}</h2>'
                    f"{blocks(body, lang, images)}</section>")
    out = "\n".join(secs)
    for d in NO_DASH:
        if d in out:
            sys.exit(f"{lang}.md: an em or en dash is not allowed")
    if "{{" in out:
        sys.exit(f"{lang}.md: unresolved placeholder near {out[out.index('{{'):out.index('{{') + 40]!r}")
    return out, toc, images


def build(out_dir: str, img_dir: str = None, stamp: str = None) -> dict:
    import datetime as dt
    sys.path.insert(0, os.path.join(V2, "server", "bundle"))
    from argia_logo import LOGO_URI          # the official wordmark (the logo, never the name as text)
    img_dir = img_dir or os.path.join(HERE, "img")
    html_by, toc_by, imgs_by = {}, {}, {}
    for lang in LANGS:
        html_by[lang], toc_by[lang], imgs_by[lang] = render(lang)
    ids = [[t["id"] for t in toc_by[x]] for x in LANGS]
    if ids[0] != ids[1]:
        sys.exit(f"the languages have different sections: {ids}")
    if imgs_by["en"] != imgs_by["es"]:
        sys.exit(f"the languages use different pictures: {sorted(imgs_by['en'] ^ imgs_by['es'])}")
    missing = [f"{x}_{k}.jpg" for x in LANGS for k in sorted(imgs_by[x]) if not os.path.exists(os.path.join(img_dir, f"{x}_{k}.jpg"))]
    if missing:
        sys.exit(f"missing pictures: {missing}")
    for x in LANGS:                        # every in-page link points at a section
        for href in re.findall(r'href="#([a-z0-9-]+)"', html_by[x]):
            if href not in ids[0]:
                sys.exit(f"{x}: link to #{href}, no such section")
    stamp = stamp or dt.date.today().isoformat()
    tpl = open(os.path.join(HERE, "page.tpl.html"), encoding="utf-8").read()
    data = {"ui": UI, "toc": toc_by, "stamp": stamp}
    page = (tpl.replace("{{LOGO}}", LOGO_URI)
            .replace("{{EN}}", html_by["en"]).replace("{{ES}}", html_by["es"])
            .replace("{{DATA}}", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    for d in NO_DASH:
        if d in page:
            sys.exit("an em or en dash in the page")
    os.makedirs(os.path.join(out_dir, "img"), exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    n = 0
    for x in LANGS:
        for k in sorted(imgs_by[x]):
            shutil.copyfile(os.path.join(img_dir, f"{x}_{k}.jpg"), os.path.join(out_dir, "img", f"{x}_{k}.jpg"))
            n += 1
    return {"sections": ids[0], "images": n, "bytes": len(page)}


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "_build")
    r = build(out)
    print(f"ok: {len(r['sections'])} sections, {r['images']} pictures, {r['bytes']:,} bytes -> {out}")
