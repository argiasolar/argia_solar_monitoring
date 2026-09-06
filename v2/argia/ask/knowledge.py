"""Ask ARGIA — the knowledge base (v215): the ARGIA Golden Standard
designer training (364 slides, EN/ES/CZ) parsed into PostgreSQL
``knowledge`` rows with full-text search, so the assistant can answer
"what does the standard say about ..." with the slide it comes from.

The AGS page (sprinkler.agency/argiagoldenstandard/...WHITE.html) keeps
every slide as HTML inside one inline script:
``const DATA = {"en": {"slides": [html, ...]}, "es": {...}, "cz": {...}}``.
``parse_ags`` pulls that object out, strips the HTML (images included —
they are base64 blobs) and keeps per slide: language, number, title
(first heading) and the plain text. Pure: no network, no database.

The same table can hold other documents later (reference PDFs): the
``doc`` column names the source.
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

AGS_URL = ("https://sprinkler.agency/argiagoldenstandard/"
           "ARGIA_Golden_Standard_Designer_Training_WHITE.html")
AGS_DOC = "AGS"
LANGS = ("en", "es", "cz")
MAX_TEXT = 6000          # per slide, after stripping — a slide is never longer

ENSURE_SQL = """CREATE TABLE IF NOT EXISTS knowledge (
    doc        text NOT NULL,
    lang       text NOT NULL,
    n          int  NOT NULL,
    title      text NOT NULL DEFAULT '',
    body       text NOT NULL DEFAULT '',
    tsv        tsvector GENERATED ALWAYS AS (
                 setweight(to_tsvector('simple', coalesce(title,'')), 'A') ||
                 setweight(to_tsvector('simple', coalesce(body,'')), 'B')) STORED,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc, lang, n)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_tsv ON knowledge USING gin (tsv);"""


# ------------------------------------------------------------- parsing
def extract_data(page_html: str) -> Dict:
    """The ``DATA`` object of the AGS page. Raises ValueError when the
    page does not carry it (the layout changed — ingest must fail loud)."""
    m = re.search(r"const\s+DATA\s*=\s*", page_html)
    if not m:
        raise ValueError("AGS page: 'const DATA =' not found")
    obj, _end = json.JSONDecoder().raw_decode(page_html, m.end())
    if not isinstance(obj, dict) or not any(k in obj for k in LANGS):
        raise ValueError("AGS page: DATA has no language keys")
    return obj


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")
_HEAD = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.S | re.I)
_BLOCK_END = re.compile(r"</(p|div|li|h[1-6]|tr|section|article|blockquote)>|<br\s*/?>", re.I)


def slide_text(slide_html: str) -> Tuple[str, str]:
    """(title, text) of one slide. Images and scripts go, block ends
    become newlines, entities are decoded, whitespace collapses."""
    s = re.sub(r"<img[^>]*>", " ", slide_html, flags=re.I)
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    m = _HEAD.search(s)
    title = _clean(_TAG.sub(" ", m.group(1))) if m else ""
    s = _BLOCK_END.sub("\n", s)
    s = _TAG.sub(" ", s)
    text = _clean(s)
    return title[:200], text[:MAX_TEXT]


def _clean(s: str) -> str:
    s = _html.unescape(s).replace("\u00a0", " ")
    s = _WS.sub(" ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n"))
    s = _NL.sub("\n", s)
    return s.strip()


def parse_ags(page_html: str, langs: Sequence[str] = LANGS) -> List[Dict]:
    """[{doc, lang, n, title, body}] for every non-empty slide."""
    data = extract_data(page_html)
    out: List[Dict] = []
    for lang in langs:
        slides = (data.get(lang) or {}).get("slides") or []
        for i, h in enumerate(slides, 1):
            title, body = slide_text(h or "")
            if not body:
                continue
            out.append({"doc": AGS_DOC, "lang": lang, "n": i, "title": title, "body": body})
    return out


# ----------------------------------------------------------------- SQL
def _txt(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def build_upsert_sql(rows: Iterable[Dict], batch: int = 200) -> List[str]:
    """INSERT ... ON CONFLICT statements (batched) replacing the slides;
    a re-ingest of an updated deck overwrites in place."""
    rows = list(rows)
    out: List[str] = []
    for i in range(0, len(rows), batch):
        vals = ",\n".join(
            f"({_txt(r['doc'])},{_txt(r['lang'])},{int(r['n'])},{_txt(r['title'])},{_txt(r['body'])})"
            for r in rows[i:i + batch])
        out.append("INSERT INTO knowledge (doc, lang, n, title, body) VALUES\n" + vals +
                   "\nON CONFLICT (doc, lang, n) DO UPDATE SET title=EXCLUDED.title,"
                   " body=EXCLUDED.body, updated_at=now();")
    return out


def build_prune_sql(doc: str, lang: str, keep_n: int) -> str:
    """Slides past the new deck's length are gone from the source."""
    return f"DELETE FROM knowledge WHERE doc={_txt(doc)} AND lang={_txt(lang)} AND n > {int(keep_n)};"


def search_sql(query: str, lang: str = "en", limit: int = 5, doc: str = AGS_DOC) -> str:
    """Ranked full-text search; falls back to a substring match when the
    parsed query has no lexemes (very short or all-stopword input).
    Returns n, title, body, rank."""
    q = _txt(query[:300])
    lang = lang if lang in LANGS else "en"
    limit = max(1, min(int(limit), 10))
    return (
        "/*tag:knowledge_search*/ "
        "SELECT n, title, body, round(ts_rank(tsv, q)::numeric, 4) AS rank"
        f" FROM knowledge, websearch_to_tsquery('simple', {q}) q"
        f" WHERE doc={_txt(doc)} AND lang={_txt(lang)}"
        f" AND (tsv @@ q OR body ILIKE '%' || {q} || '%' OR title ILIKE '%' || {q} || '%')"
        f" ORDER BY (tsv @@ q) DESC, rank DESC, n LIMIT {limit};")


def excerpt(body: str, query: str, width: int = 700) -> str:
    """The part of the slide around the first query word, so a long
    slide still shows the relevant lines."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    low = body.lower()
    pos = min((low.find(w) for w in words if low.find(w) >= 0), default=-1)
    if pos < 0 or len(body) <= width:
        return body[:width]
    start = max(0, pos - width // 3)
    return ("…" if start else "") + body[start:start + width] + ("…" if start + width < len(body) else "")
