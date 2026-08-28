#!/usr/bin/env python3
"""CFE tariff scraper — runs on the ARGIA Pi (Zapopan, Mexican IP).

Scrapes the official integrated final tariffs ("tarifas finales del
suministro basico") from app.cfe.mx for the 10 business/industrial
tariff categories x 17 CFE divisions x requested months, and emits a
CSV loadable by cfe_load.py (source=cfe_scrape).

DB1/DB2 (domestic) are NOT published on these pages; they stay on the
master_db_10 seed. Component-level charges (TRANSMISION, CENACE,
SERVICIOS CONEXOS NO MEM, FACTOR DE CARGA) are also not published
here and are never overwritten by this scraper.

The site sits behind an Incapsula WAF: a real browser (Playwright
chromium) passes the JS challenge; plain HTTP gets 403 even from a
Mexican IP (verified 2026-08-27).

Usage:
  cfe_scrape.py --discover                 # build ~/cfe/divmap.json
  cfe_scrape.py --months 2026-08 --out f.csv
  cfe_scrape.py --months 2025-09:2026-08 --out backfill.csv
  cfe_scrape.py --months 2026-08 --tariffs GDMTH,PDBT --out f.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata

BASE = "https://app.cfe.mx/Aplicaciones/CCFE/Tarifas/"
NEG = BASE + "TarifasCRENegocio/Tarifas/"
IND = BASE + "TarifasCREIndustria/Tarifas/"
TARIFF_URLS = {
    "PDBT": NEG + "PequenaDemandaBT.aspx",
    "GDBT": NEG + "GranDemandaBT.aspx",
    "GDMTO": NEG + "GranDemandaMTO.aspx",
    "GDMTH": NEG + "GranDemandaMTH.aspx",
    "DIST": IND + "DemandaIndustrialSub.aspx",
    "DIT": IND + "DemandaIndustrialTran.aspx",
    "RABT": NEG + "RiegoAgricolaBT.aspx",
    "RAMT": NEG + "RiegoAgricolaMT.aspx",
    "APBT": NEG + "AlumbradoPublicoBT.aspx",
    "APMT": NEG + "AlumbradoPublicoMT.aspx",
}
KNOWN_REGIONS = {
    "BAJA CALIFORNIA", "BAJA CALIFORNIA SUR", "BAJIO",
    "CENTRO OCCIDENTE", "CENTRO ORIENTE", "CENTRO SUR",
    "GOLFO CENTRO", "GOLFO NORTE", "JALISCO", "NOROESTE", "NORTE",
    "ORIENTE", "PENINSULAR", "SURESTE", "VALLE DE MEXICO CENTRO",
    "VALLE DE MEXICO NORTE", "VALLE DE MEXICO SUR",
}
MES_TAG = {1: "ENE", 2: "FEB", 3: "MAR", 4: "ABR", 5: "MAY", 6: "JUN",
           7: "JUL", 8: "AGO", 9: "SEP", 10: "OCT", 11: "NOV",
           12: "DIC"}
P = "ctl00$ContentPlaceHolder1$"
SEL_ANIO = f"select[name='{P}Fecha$ddAnio']"
SEL_MES = f"select[name='{P}Fecha2$ddMes']"
# past years render a DIFFERENT month control (found 2026-08-27:
# current year = Fecha2$ddMes, earlier years = MesVerano3$ddMesConsulta)
SEL_MES_PAST = f"select[name='{P}MesVerano3$ddMesConsulta']"
SEL_EDO = f"select[name='{P}EdoMpoDiv$ddEstado']"
SEL_MPO = f"select[name='{P}EdoMpoDiv$ddMunicipio']"
SEL_DIV = f"select[name='{P}EdoMpoDiv$ddDivision']"
DIVMAP_PATH = os.path.expanduser("~/cfe/divmap.json")


def norm(s: str) -> str:
    """Uppercase, accent-stripped, single-spaced."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip().upper()


def map_charge(horario: str, cargo: str):
    """Page (Int. Horario, Cargo) -> (charge_type, unit). None = skip."""
    h, cg = norm(horario), norm(cargo)
    if cg.startswith("FIJO"):
        return "SUMINISTRO BASICO", "MXN/MONTH"
    if "ENERGIA" in cg or cg.startswith("VARIABLE"):
        if h in ("BASE", "-", ""):
            return "ENERGIA BASE", "MXN/KWH"
        if h == "INTERMEDIA":
            return "ENERGIA INTERMEDIA", "MXN/KWH"
        if h == "PUNTA":
            return "ENERGIA PUNTA", "MXN/KWH"
        return f"ENERGIA {h}", "MXN/KWH"
    if cg.startswith("DISTRIBUCION"):
        return "DISTRIBUCION", "MXN/KW-MONTH"
    if cg.startswith("CAPACIDAD"):
        return "CAPACIDAD", "MXN/KW-MONTH"
    return cg, ""     # unknown concept: keep page name, flag downstream


TABLE_RE = re.compile(
    r'<table class="table table-bordered[^"]*">(.*?)</table>', re.S)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_charge_table(html: str):
    """Return (tariff_tag, month_tag, rows) from the rendered page, or
    (None, None, []) when no charge table is present. rows =
    [(horario, cargo, unit_text, value_float)]."""
    m = TABLE_RE.search(html)
    if not m:
        return None, None, []
    trs = ROW_RE.findall(m.group(1))
    if not trs:
        return None, None, []
    header = [strip_tags(c) for c in CELL_RE.findall(trs[0])]
    month_tag = header[-1] if header else None
    # two layouts: hourly tariffs (GDMTH/DIST/DIT) carry an extra
    # "Int. Horario" column; the flat tariffs (PDBT, GDMTO, RABT, ...)
    # go straight to Cargo/Unidades/value
    has_horario = any(h.startswith("Int") for h in header)
    full = 6 if has_horario else 5
    data = 4 if has_horario else 3
    tariff_tag = None
    rows = []
    for tr in trs[1:]:
        cells = [strip_tags(c) for c in CELL_RE.findall(tr)]
        if not cells:
            continue
        # first data row carries tariff code + description (rowspan)
        if len(cells) >= full:
            tariff_tag = cells[0]
            cells = cells[2:]
        if len(cells) != data:
            continue
        if has_horario:
            horario, cargo, unit_text, val = cells
        else:
            horario = "-"
            cargo, unit_text, val = cells
        val = val.replace(",", "").replace("$", "").strip()
        try:
            rows.append((horario, cargo, unit_text, float(val)))
        except ValueError:
            continue
    return tariff_tag, month_tag, rows


class CfePage:
    """One tariff page with the WAF cookie settled."""

    def __init__(self, page, url):
        self.page = page
        self.url = url
        self._year = None
        self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        self.page.wait_for_timeout(9000)   # Incapsula JS challenge
        self.page.goto(url, wait_until="networkidle", timeout=90000)
        self.page.wait_for_timeout(1500)

    def _pb(self, selector, value):
        """Select + wait out the ASPX postback (full-page __doPostBack:
        the DOM is replaced, so wait for the select to come back)."""
        last = None
        for _try in range(4):
            try:
                self.page.wait_for_selector(selector, timeout=20000)
                self.page.select_option(selector, value)
                self.page.wait_for_load_state("networkidle")
                self.page.wait_for_timeout(1200)
                return
            except Exception as ex:
                last = ex
                self.page.wait_for_timeout(2000)
        raise last

    def opts(self, selector):
        last = None
        for _try in range(4):
            try:
                self.page.wait_for_selector(selector, timeout=20000)
                return self.page.eval_on_selector(
                    selector,
                    "e => Array.from(e.options)"
                    ".map(o => [o.value, o.text])")
            except Exception as ex:
                last = ex
                self.page.wait_for_timeout(2000)
        raise last

    def set_year(self, year):
        if self._year == year:
            return          # same-value select still fires a postback
        self._pb(SEL_ANIO, str(year))
        self._year = year

    def set_month(self, month):
        """Current year uses Fecha2$ddMes; past years swap it for
        MesVerano3$ddMesConsulta. Pick whichever is on the page."""
        self.page.wait_for_selector(f"{SEL_MES}, {SEL_MES_PAST}",
                                    timeout=20000)
        sel = (SEL_MES if self.page.query_selector(SEL_MES)
               else SEL_MES_PAST)
        self._pb(sel, str(month))

    def set_location(self, estado_v, muni_v):
        self._pb(SEL_EDO, str(estado_v))
        self._pb(SEL_MPO, str(muni_v))
        divs = [v for v, _t in self.opts(SEL_DIV) if v != "0"]
        if not divs:
            return None
        self._pb(SEL_DIV, divs[0])
        return norm(dict(self.opts(SEL_DIV))[divs[0]])

    def division_name(self):
        sel = [t for v, t in self.opts(SEL_DIV) if v != "0"]
        return norm(sel[0]) if sel else None

    def html(self):
        return self.page.content()


def discover_divmap(pw):
    """Build division -> (estado_value, muni_value) using GDMTH.
    One muni per division is enough (tariffs are per division).
    Multi-division states (CDMX, Edo. de Mexico) are walked muni by
    muni until no new division appears. Crash-resilient: partial
    divmap is persisted after every find, the browser is restarted on
    a crash, and already-processed estados are skipped on resume."""
    divmap, done = {}, set()
    if os.path.exists(DIVMAP_PATH):
        divmap = json.load(open(DIVMAP_PATH))
    done_path = DIVMAP_PATH + ".done"
    if os.path.exists(done_path):
        done = set(json.load(open(done_path)))

    def save():
        json.dump(divmap, open(DIVMAP_PATH, "w"), indent=1)
        json.dump(sorted(done), open(done_path, "w"))

    attempts = 0
    while attempts < 6:
        attempts += 1
        b = pw.chromium.launch()
        try:
            page = b.new_page(locale="es-MX")
            cp = CfePage(page, TARIFF_URLS["GDMTH"])
            cp.set_year(2026)
            cp.set_month(1)
            estados = [(v, t) for v, t in cp.opts(SEL_EDO)
                       if v != "0" and v not in done]
            if not estados:
                break
            for ev, et in estados:
                cp._pb(SEL_EDO, ev)
                munis = [(v, t) for v, t in cp.opts(SEL_MPO)
                         if v != "0"]
                deep = norm(et) in ("CIUDAD DE MEXICO",
                                    "ESTADO DE MEXICO")
                probe = munis if deep else munis[:1]
                misses = 0
                for mv, _mt in probe:
                    cp._pb(SEL_MPO, mv)
                    div = cp.division_name()
                    if div and div not in divmap:
                        divmap[div] = [ev, mv]
                        misses = 0
                        print(f"  {div} <- {et} ({ev},{mv})",
                              flush=True)
                        save()
                    else:
                        misses += 1
                        if deep and misses >= 12:
                            break
                done.add(ev)
                save()
        except Exception as ex:
            print(f"discovery crash (attempt {attempts}): "
                  f"{ex}"[:200], flush=True)
            time.sleep(5)
        finally:
            b.close()
        if KNOWN_REGIONS <= set(divmap):
            break
    missing = KNOWN_REGIONS - set(divmap)
    if missing:
        print(f"WARNING: divisions not found: {sorted(missing)}")
    save()
    print(f"divmap: {len(divmap)} divisions -> {DIVMAP_PATH}")
    return divmap


def month_range(spec: str):
    """'2026-08' or '2025-09:2026-08' -> [(year, month), ...]"""
    def parse(s):
        y, m = s.split("-")
        return int(y), int(m)
    if ":" in spec:
        a, b = spec.split(":")
        (y, m), (y2, m2) = parse(a), parse(b)
        out = []
        while (y, m) <= (y2, m2):
            out.append((y, m))
            m += 1
            if m > 12:
                y, m = y + 1, 1
        return out
    return [parse(spec)]


def scrape_cell(cp, code, div, ev, mv, y, m, located, writer,
                manifest):
    """One tariff/division/month. Returns new `located`. Raises on
    browser-level failure so the caller can restart chromium.
    Duplicate rows across restarts are harmless: the loader upserts
    by (tariff, region, month, charge_type)."""
    key = f"{code}/{div}/{y}-{m:02d}"
    cp.set_year(y)
    cp.set_month(m)
    if not located:
        got = cp.set_location(ev, mv)
        if got != div:
            manifest["errors"].append(f"{key}: division mismatch {got}")
            return False
    tag, mtag, rows = parse_charge_table(cp.html())
    want = f"{MES_TAG[m]}-{y % 100:02d}"
    if norm(mtag or "") != want or not rows:
        # stale postback state: re-walk the location once
        cp.set_location(ev, mv)
        tag, mtag, rows = parse_charge_table(cp.html())
    if not rows:
        manifest["errors"].append(f"{key}: no table")
        return True
    if norm(mtag or "") != want:
        manifest["errors"].append(f"{key}: month tag {mtag} != {want}")
        return True
    if tag and norm(tag) != code:
        manifest["errors"].append(f"{key}: page tag {tag}")
        return True
    for horario, cargo, _ut, val in rows:
        ct, unit = map_charge(horario, cargo)
        writer.writerow([code, div, f"{y}-{m:02d}-01", ct, unit,
                         f"{val:.6f}"])
        manifest["rows"] += 1
    manifest["cells"] += 1
    return True


def _fatal(ex) -> bool:
    """Browser/page-level death: retrying in this session is useless."""
    s = (str(ex) + type(ex).__name__).lower()
    return "crash" in s or "targetclosed" in s or "browser" in s


def scrape(pw, months, tariffs, divmap, writer, manifest):
    """Fresh chromium every few divisions: long ASPX sessions crash
    the page on the Pi, so recycling is cheaper than recovering."""
    BATCH = 4
    seen = set()      # cells already scraped (skip after a recycle)
    for code in tariffs:
        divs = sorted(divmap.items())
        di, fail_streak = 0, 0
        while di < len(divs) and fail_streak < 5:
            start_di = di
            b = pw.chromium.launch()
            try:
                page = b.new_page(locale="es-MX")
                cp = CfePage(page, TARIFF_URLS[code])
                for _n in range(BATCH):
                    if di >= len(divs):
                        break
                    div, (ev, mv) = divs[di]
                    located = False
                    for (y, m) in months:
                        key = f"{code}/{div}/{y}-{m:02d}"
                        if key in seen:
                            continue
                        try:
                            before = manifest["cells"]
                            located = scrape_cell(
                                cp, code, div, ev, mv, y, m, located,
                                writer, manifest)
                            if manifest["cells"] > before:
                                seen.add(key)
                        except Exception as ex:
                            manifest["errors"].append(
                                f"{key}: {ex}"[:200])
                            located = False
                            if _fatal(ex):
                                raise
                            time.sleep(3)
                    print(f"{code} {div}: done", flush=True)
                    di += 1
            except Exception as ex:
                print(f"{code}: browser recycle after error "
                      f"({ex})"[:200], flush=True)
                time.sleep(5)
            finally:
                try:
                    b.close()
                except Exception:
                    pass
            fail_streak = fail_streak + 1 if di == start_di else 0
        if fail_streak >= 5:
            manifest["errors"].append(f"{code}: gave up after "
                                      f"5 fruitless browser restarts")
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--months")
    ap.add_argument("--tariffs", default=",".join(TARIFF_URLS))
    ap.add_argument("--out")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        if args.discover:
            discover_divmap(pw)
            return 0
        if not (args.months and args.out):
            ap.error("--months and --out required (or --discover)")
        if not os.path.exists(DIVMAP_PATH):
            print("no divmap.json — running discovery first")
            discover_divmap(pw)
        divmap = json.load(open(DIVMAP_PATH))
        # discovery also finds composite-division municipalities
        # ("VALLE DE MEXICO NORTE Y CENTRO") — scrape only the 17
        # pure CFE divisions the tariff table is keyed by
        divmap = {k: v for k, v in divmap.items()
                  if k in KNOWN_REGIONS}
        months = month_range(args.months)
        tariffs = [t for t in args.tariffs.split(",") if t]
        bad = [t for t in tariffs if t not in TARIFF_URLS]
        if bad:
            ap.error(f"unknown tariffs: {bad}")
        manifest = {"cells": 0, "rows": 0, "errors": [],
                    "months": [f"{y}-{m:02d}" for y, m in months],
                    "tariffs": tariffs, "divisions": len(divmap)}
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["tariff_code", "region", "month", "charge_type",
                        "unit", "value_mxn"])
            scrape(pw, months, tariffs, divmap, w, manifest)
        json.dump(manifest, open(args.out + ".manifest.json", "w"),
                  indent=1)
        expected = len(months) * len(tariffs) * len(divmap)
        print(f"cells {manifest['cells']}/{expected}, rows "
              f"{manifest['rows']}, errors {len(manifest['errors'])}")
        return 0 if manifest["cells"] == expected else 1


if __name__ == "__main__":
    sys.exit(main())
