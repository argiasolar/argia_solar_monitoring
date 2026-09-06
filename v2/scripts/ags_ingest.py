"""Load the ARGIA Golden Standard training deck into the ``knowledge``
table for Ask ARGIA (v215). Weekly (argia-ags-ingest.timer) or by hand:

    python3 scripts/ags_ingest.py                 # fetch the live page
    python3 scripts/ags_ingest.py --file ags.html # from a saved copy
    python3 scripts/ags_ingest.py --dry-run       # parse, print, write nothing

Requires ARGIA_PG_MIRROR=1 (pio06); exits 0 quietly elsewhere.
"""
from __future__ import annotations

import argparse
import logging
import sys

from argia.ask import knowledge as K
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.ags_ingest")


def fetch(url: str, timeout: int = 90) -> str:
    import requests
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "argia-ask-ingest/1.0"})
    r.raise_for_status()
    return r.text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AGS -> knowledge table")
    parser.add_argument("--url", default=K.AGS_URL)
    parser.add_argument("--file", default=None, help="parse a saved copy instead of fetching")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.dry_run and not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    page = open(args.file, encoding="utf-8").read() if args.file else fetch(args.url)
    rows = K.parse_ags(page)
    per_lang = {l: sum(1 for r in rows if r["lang"] == l) for l in K.LANGS}
    LOG.info("parsed %d slides: %s (%d chars of text)", len(rows), per_lang,
             sum(len(r["body"]) for r in rows))
    if len(rows) < 100:
        LOG.error("too few slides parsed (%d) — page layout changed? nothing written", len(rows))
        return 1
    if args.dry_run:
        for r in rows[:5]:
            print(f"[{r['lang']} #{r['n']}] {r['title']!r}: {r['body'][:120]!r}")
        return 0
    psql_exec(K.ENSURE_SQL)
    for sql in K.build_upsert_sql(rows):
        psql_exec(sql)
    for l, n in per_lang.items():
        if n:
            psql_exec(K.build_prune_sql(K.AGS_DOC, l, max(r["n"] for r in rows if r["lang"] == l)))
    count = psql_rows("SELECT lang, count(*) FROM knowledge WHERE doc='AGS' GROUP BY 1 ORDER BY 1;")
    LOG.info("DONE: knowledge rows %s", count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
