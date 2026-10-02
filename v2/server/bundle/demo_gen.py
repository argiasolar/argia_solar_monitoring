#!/usr/bin/env python3
"""demo.argia.com.mx generator (v279).

The demo is the portal's Report, Monitoring and Map with the same live
data, shown to prospects under invented names:

* every plant is "ARGIA SOLAR <n>" and every plant is PPA (the four CAPEX
  plants are priced at the fleet's kWh-weighted PPA tariff of each month);
* every customer logo is the ARGIA SOLAR logo (grey, black on mouse-over);
* only the landing page, /report/ (overview, PPA, plant performance, one
  page per plant), /monitoring/ and /map/ exist - no financial report, no
  invoices, no app, no Ask ARGIA, no setup, no account page;
* it is a separate site with its own login (nginx, one shared account in
  /opt/argia/demo/demo.htpasswd) - nothing of the portal login is used, so
  the pages have no user menu and no log-out.

How (one codebase, no fork, no second database):

1. every psql the generators run gets ``search_path=demo,public``: the
   read-only views in ``demo_schema.sql`` (demo.plant, demo.contract_monthly,
   demo.loss_daily) replace the names, the portfolio and the tariffs AT THE
   DATA LAYER, so every tile, chart, tooltip and table agrees;
2. portal_gen / report_gen / monitoring_gen are imported unchanged and a
   handful of presentation settings are overridden (fleet lists, slugs,
   logos, sub-tabs, reference links, header buttons);
3. the pages are written to a staging folder, scrubbed, and **scanned for
   every real customer name** (read from public.plant, the logo table, the
   slug table and the reference links). One hit = nothing is published and
   the job exits 1; the last good demo stays online.

This script only READS PostgreSQL and only WRITES under its own web root.
It has no mail, ntfy, Drive, vendor API or model call - see
tests/unit/test_demo_gen.py::test_demo_gen_has_no_outbound_channel.

    python3 /opt/argia/bundle/demo_gen.py [/www/hosting/demo.argia.com.mx/www]
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

DEMO_HOST = 'demo.argia.com.mx'
DEMO_ROOT = '/www/hosting/demo.argia.com.mx/www'
PORTAL_ROOT = os.environ.get('ARGIA_PORTAL_ROOT', '/www/hosting/portal.argia.com.mx/www')
DB = os.environ.get('ARGIA_PG_DB', 'argia_mont')
DEMO_RE = re.compile(r'^\s*(ARGIA SOLAR \S+)')          # "ARGIA SOLAR 3 (Leon, GTO)" -> "ARGIA SOLAR 3"
LOGO_PATH = '/assets/demo/argia-solar.png'

# static files copied from the portal web root (whitelist - nothing else
# from production is ever copied): site photos, the PVOUT overlay, icons
ASSET_GLOBS = [
    ('assets/photos', r'^[a-z0-9]+(_t)?\.jpg$'),
    ('monitoring/assets', r'^[a-z0-9]+(_t)?\.jpg$'),
    ('portfolio/assets', r'^pvout_mexico\.(png|json)$'),
    ('', r'^(favicon\.png|favicon\.svg|favicon\.ico)$'),
]

# words too generic to be a leak on their own (a real name is still caught
# as the whole phrase)
STOP_WORDS = {'grupo', 'mexico', 'méxico', 'service', 'management', 'solutions', 'express',
              'inn', 'holiday', 'the', 'roof', 'land', 'ppa', 'capex', 'argia', 'solar',
              'modelo', 'model', 'tetra', 'plastic'}   # 'modelo de clima' is Spanish for 'weather model'

# portal wording that only makes sense with CAPEX plants (cosmetic: the
# demo has none). A fix that stops matching is caught by test_demo_site.
TEXT_FIXES = [
    (' · 0 CAPEX', ''),
    (' · blue = PPA, teal = CAPEX', ''),
    (' · azul = PPA, verde = CAPEX', ''),
]

DEMO_CSS = '''
/* demo (v279): the one ARGIA SOLAR logo - grey at rest, black on mouse-over */
.clogo{filter:grayscale(1)!important;opacity:.32;max-width:150px}
.lcell .lbox{width:120px;flex:0 0 120px}.lcell .lbox .clogo{height:12px;max-width:112px}
.pcard:hover .clogo,tr:hover .clogo,.clogo:hover{opacity:1}
.logocard .clogo,.clogo.color{opacity:.32;height:auto!important;width:150px}
.logocard:hover .clogo{opacity:1}
'''


# ------------------------------------------------------------------ pure
def demo_display(customer, fallback):
    """'ARGIA SOLAR 3 (Leon, GTO)' -> 'ARGIA SOLAR 3' (kept upper case);
    anything else goes to the normal rule. Pure."""
    m = DEMO_RE.match(str(customer or ''))
    return m.group(1) if m else fallback(customer)


def slugify(s):
    s = unicodedata.normalize('NFKD', str(s)).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def name_variants(customer):
    """Every way a real customer string can show up on a page:
    'HIRSCHMANN-MEXICO CAPEX roof (San Miguel, GTO)' -> {'HIRSCHMANN-MEXICO',
    'HIRSCHMANN', ...}. The location in brackets is NOT a variant (the demo
    keeps the locations). Pure."""
    s = str(customer or '').split('(')[0]
    for cut in (' PPA', ' CAPEX', ' roof', ' land'):
        i = s.find(cut)
        if i > 0:
            s = s[:i]
    s = s.strip(' ,')
    out = set()
    if s:
        out.add(s)
        for w in re.split(r'[\s\-_/]+', s):
            if len(w) >= 4 and w.lower() not in STOP_WORDS:
                out.add(w)
            elif len(w) == 3 and w.isupper() and len(s.split()) == 1 and w.lower() not in STOP_WORDS:
                out.add(w)                                  # SAG, SMS (whole name only)
    return out


def _pattern(token):
    """Case-insensitive, separators ( space - _ ) interchangeable, never in
    the middle of a word. 3-letter acronyms are matched in capitals only
    (so 'sag' inside 'usage' or 'message' is not a hit). Pure."""
    parts = [re.escape(p) for p in re.split(r'[\s\-_]+', token.strip()) if p]
    body = r'[\s\-_]+'.join(parts)
    flags = 0 if (len(token) <= 3 and token.isupper()) else re.IGNORECASE
    return re.compile(r'(?<![A-Za-z0-9])' + body + r'(?![A-Za-z0-9])', flags)


def build_rules(real):
    """real: {plant_key: [real name strings]} -> [(compiled, plant_key)],
    longest token first so 'PLASTIC OMNIUM' wins over 'OMNIUM'. Pure."""
    seen = {}
    for k, names in real.items():
        for nm in names:
            if not nm:
                continue
            for v in name_variants(nm):
                if len(v) >= 3 and v.lower() not in STOP_WORDS:
                    seen.setdefault(v, k)
    return [(_pattern(tok), seen[tok], tok) for tok in sorted(seen, key=lambda x: (-len(x), x))]


_DATA_URI = re.compile(r'data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+')


def _mask(text):
    """Base64 data URIs are random letters: never scan or rewrite them."""
    keep = []

    def sub(m):
        keep.append(m.group(0))
        return f'@@DATAURI{len(keep) - 1}@@'
    return _DATA_URI.sub(sub, text), keep


def _unmask(text, keep):
    return re.sub(r'@@DATAURI(\d+)@@', lambda m: keep[int(m.group(1))], text)


def scrub(text, rules, demo_names):
    """Replace every real-name hit by the plant's demo name ('argia-solar-3'
    inside a URL-ish slug, 'ARGIA SOLAR 3' elsewhere). Returns (text, n). Pure."""
    body, keep = _mask(text)
    n = 0
    for rx, k, tok in rules:
        dn = demo_names.get(k, 'ARGIA SOLAR')

        def rep(m, dn=dn):
            s = m.group(0)                     # 'taigene' / 'plastic-omnium' are slugs (URLs, file names)
            return slugify(dn) if (s == s.lower() and ' ' not in s) else dn
        body, c = rx.subn(rep, body)
        n += c
    return _unmask(body, keep), n


def leaks(text, rules):
    """[(token, context)] for every real name still in the text. Pure."""
    body, _ = _mask(text)
    out = []
    for rx, _k, tok in rules:
        for m in rx.finditer(body):
            a, b = max(0, m.start() - 40), min(len(body), m.end() + 40)
            out.append((tok, body[a:b].replace('\n', ' ')))
    return out


# the portal links into apps the demo does not have (tickets, setup,
# invoices, the phone app): those links become plain text
ABSENT = ('/maintenance/', '/setup/', '/report/invoices/', '/report/financial/', '/report/capex/',
          '/monitoring/capex/', '/invoices/', '/app/', '/ask/', '/account/', '/finance/', '/projects/')
_ANCHOR = re.compile(r'<a\b([^>]*?)\s+href="(/[^"]*)"([^>]*)>(.*?)</a>', re.S)


def unlink_absent(text):
    """<a href="/maintenance/t/T-0002/">T-0002</a> -> <span>T-0002</span>
    for every target the demo does not serve; a button (class btn*) to such
    a target is removed. Returns (text, n). Pure."""
    n = 0

    def sub(m):
        nonlocal n
        if not m.group(2).startswith(ABSENT):
            return m.group(0)
        n += 1
        attrs = re.sub(r'\s(target|rel|onclick)="[^"]*"', '', m.group(1) + m.group(3))
        if re.search(r'class="[^"]*\bbtn', attrs):
            return ''                                   # a button to nowhere goes away entirely
        return f'<span{attrs}>{m.group(4)}</span>'
    return _ANCHOR.sub(sub, text), n


def demo_number(customer):
    m = re.match(r'^\s*ARGIA SOLAR (\d+)', str(customer or ''))
    return int(m.group(1)) if m else 10 ** 6


# ------------------------------------------------------------- database
READ_ONLY = '-c default_transaction_read_only=on'
DEMO_OPTS = f'-c search_path=demo,public {READ_ONLY}'


def psql_public(sql, opts=READ_ONLY):
    """Rows from psql exactly as the generators run it (runuser + psql),
    with our own PGOPTIONS: the REAL tables by default, read-only always."""
    env = dict(os.environ)
    env['PGOPTIONS'] = opts
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', DB, '-X', '-t', '-A', '-F', '\t', '-c', sql],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])
    return [ln.split('\t') for ln in r.stdout.splitlines() if ln.strip()]


def demo_schema_ok():
    """search_path=demo,public silently falls through to the real tables
    when a demo view is missing - so check the views exist AND that the
    settings really reach psql through runuser (an environment that drops
    PGOPTIONS would show the real names). Refuse to run otherwise."""
    need = ('demo.plant', 'demo.contract_monthly', 'demo.loss_daily')
    rows = psql_public("SELECT current_setting('search_path'), current_setting('default_transaction_read_only'), "
                       + ', '.join(f"to_regclass('{v}') IS NOT NULL" for v in need) + ';', DEMO_OPTS)
    if not rows or len(rows[0]) < 2 + len(need):
        return False
    path, ro, flags = rows[0][0], rows[0][1], rows[0][2:]
    return path.replace(' ', '').startswith('demo,') and ro == 'on' and all(x == 't' for x in flags)


def real_names():
    """{plant_key: [every real name we know]} - from the database, the logo
    table, the slug table and the reference links. Nothing new is stored."""
    real = {}
    for r in psql_public('SELECT plant_key, customer FROM public.plant;'):
        if len(r) >= 2:
            real.setdefault(r[0], []).append(r[1])
    try:
        from argia_client_logos import CLIENT_LOGOS
        for k, v in CLIENT_LOGOS.items():
            real.setdefault(k, []).append(v[0])
    except ImportError:
        pass
    try:
        from portal_chrome import SLUGS
        for k, v in SLUGS.items():
            real.setdefault(k, []).append(v.replace('-', ' '))
    except ImportError:
        pass
    return real


# ------------------------------------------------------------------ build
def copy_assets(src_root, out):
    n = 0
    for sub, rx in ASSET_GLOBS:
        sdir = os.path.join(src_root, sub)
        if not os.path.isdir(sdir):
            continue
        for fn in sorted(os.listdir(sdir)):
            if re.match(rx, fn) and os.path.isfile(os.path.join(sdir, fn)):
                os.makedirs(os.path.join(out, sub), exist_ok=True)
                shutil.copy2(os.path.join(sdir, fn), os.path.join(out, sub, fn))
                n += 1
    import demo_brand
    os.makedirs(os.path.join(out, 'assets', 'demo'), exist_ok=True)
    with open(os.path.join(out, LOGO_PATH.lstrip('/')), 'wb') as fh:
        fh.write(demo_brand.logo_png())
    return n + 1


def configure(PG):
    """Point the imported portal generator at the demo. Every override is
    checked - if the portal code changes shape, the demo fails loudly
    instead of quietly showing something else."""
    C, RG, MG = PG.C, PG.RG, PG.MG
    keys = sorted((k for k in RG.plants if k in MG.PLANTS),            # active plants only
                  key=lambda k: (demo_number(RG.plants[k]['customer']), k))
    assert keys, 'no plants'
    assert all(RG.plants[k]['portfolio'] == 'PPA' for k in keys), 'demo views not in use (a plant is not PPA)'
    # fleet lists: everybody is PPA
    RG.PPA[:] = keys
    RG.CAPEX[:] = []
    PG.PPA, PG.CAPEX = RG.PPA, RG.CAPEX
    if hasattr(RG, 'LAAS'):
        RG.LAAS = []
    # names
    C.display_name = lambda c, _o=C.display_name: demo_display(c, _o)
    MG.display_name = lambda c, _o=MG.display_name: demo_display(c, _o)
    demo_names = {k: C.display_name(RG.plants[k]['customer']) for k in keys}
    C.SLUGS.clear()
    C.SLUGS.update({k: slugify(v) for k, v in demo_names.items()})
    C.CODE_OF_SLUG.clear()
    C.CODE_OF_SLUG.update({v: k for k, v in C.SLUGS.items()})
    # one logo for every plant
    logos = {k: (demo_names[k], LOGO_PATH) for k in keys}
    PG.CLIENT_LOGOS = logos
    if hasattr(RG, 'CLIENT_LOGOS'):
        RG.CLIENT_LOGOS = logos
    # reference pages on argia.com.mx are named after the customer
    MG.REF_LINKS = {}
    # chrome: host, sub-tabs, no Ask / account / app
    C.PORTAL_HOST = DEMO_HOST
    C.SECTIONS = {
        'report': ('Report', 'Reporte', [('', 'Overview', 'Resumen'), ('ppa', 'PPA', 'PPA'),
                                          ('plants', 'Plant performance', 'Desempeño por planta')]),
        'monitoring': ('Monitoring', 'Monitoreo', [s for s in C.SECTIONS['monitoring'][2] if s[0] != 'capex']),
        'map': ('Map', 'Mapa', []),
    }
    # the header: no Ask ARGIA button, no user menu (the demo has its own
    # browser sign-in, no portal session, no account page, no log-out)
    hdr = C.header
    ask_btn = re.compile(r'\s*<a class="ib ask" href="/ask/"[^>]*>.*?</a>', re.S)
    user_btn = re.compile(r'\s*<button class="ib" id="ubtn".*?</button>', re.S)
    sample = hdr('report')
    assert ask_btn.search(sample), 'header changed: the Ask ARGIA button was not found'
    assert user_btn.search(sample), 'header changed: the user button was not found'
    C.user_menu = lambda: ''
    C.header = lambda *a, _h=hdr, **kw: user_btn.sub('', ask_btn.sub('', _h(*a, **kw)))
    ctrl_k = "location.href='/ask/';"
    assert ctrl_k in C.JS, 'chrome JS changed: the Ctrl-K jump to /ask/ was not found'
    C.JS = C.JS.replace(ctrl_k, '')
    C.CSS = C.CSS + DEMO_CSS
    # the overview revenue tile is PPA + LaaS on the portal; the demo has PPA only
    rt = PG.revenue_tile
    PG.revenue_tile = lambda on, rev, rev_ppa, rev_laas, _rt=rt: _rt('ppa', rev_ppa, rev_ppa, 0.0)
    return keys, demo_names


def landing(PG):
    """The demo front door: three tiles, no fleet data, no Ask bar."""
    C, MG, t, ico = PG.C, PG.MG, PG.t, PG.ico
    now = MG.NOW_MX
    h = now.hour
    g_en, g_es = (('Good morning', 'Buenos días') if h < 12 else
                  ('Good afternoon', 'Buenas tardes') if h < 19 else ('Good evening', 'Buenas noches'))
    dests = [
        ('report', 'Report', 'Reporte', '/report/', 'Fleet overview, PPA, plant performance.',
         'Resumen de flota, PPA, desempeño por planta.'),
        ('monitor', 'Monitoring', 'Monitoreo', '/monitoring/', 'Live inverters, alerts, temperatures, peers.',
         'Inversores en vivo, alertas, temperaturas, pares.'),
        ('map', 'Map', 'Mapa', '/map/', 'The fleet on one map, status and today\'s numbers.',
         'La flota en un mapa, estado y cifras de hoy.'),
    ]
    cards = ''.join(f'''
   <a href="{path}" id="tile-{key}" class="card dest" style="padding:22px 24px 18px;display:flex;flex-direction:column;gap:10px;color:var(--ink);min-height:160px">
    <div style="display:flex;align-items:center;justify-content:space-between"><span style="width:44px;height:44px;border-radius:11px;background:#e6f7f5;display:flex;align-items:center;justify-content:center">{ico(key, 24, "#05847d", 1.9)}</span><span style="color:#b6bec8">{ico("arrow", 18)}</span></div>
    <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap"><span style="font-weight:800;font-size:19px;color:var(--deep)">{t(en, es)}</span><span class="mono muted">{path.rstrip("/")}</span></div>
    <div class="tblurb" style="font-size:13.5px;color:var(--ink2)" data-en="{html.escape(ben, quote=True)}" data-es="{html.escape(bes, quote=True)}">{html.escape(ben)}</div>
   </a>''' for key, en, es, path, ben, bes in dests)
    body = f'''
<div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
 <div class="kicker">{now.strftime("%A, %d %B %Y")} · {now.strftime("%H:%M")} MX · DEMO</div>
 <h1 class="pt" style="font-size:34px">{t(g_en, g_es)}<span id="gname"></span>.</h1>
 <div class="muted" style="font-size:15px">{t("Where would you like to go?", "¿A dónde quieres ir?")}</div>
</div>
<div class="grid g3" style="margin-top:28px;gap:16px">{cards}</div>
<footer class="pf mono muted" style="padding:32px 0 0"><span>ARGIA · Zapopan, MX · {DEMO_HOST}</span></footer>'''
    return C.page('Portal', body, None)


def write_pages(PG, keys):
    """The demo's page list - a strict subset of portal_gen.main()."""
    C, MG, name = PG.C, PG.MG, PG.name
    pages = [
        ('index.html', landing(PG)),
        ('report/index.html', PG.report_overview()),
        ('report/ppa/index.html', PG.report_overview(PG.PPA, 'ppa', 'PPA plants', 'Plantas PPA')),
        ('report/plants/index.html', PG.plant_cards()),
        ('monitoring/index.html', PG.monitoring_overview()),
        ('monitoring/ppa/index.html', PG.monitoring_overview('ppa')),
        ('monitoring/performance/index.html', PG.monitoring_performance()),
        ('monitoring/recon/index.html', PG.monitoring_recon()),
        ('monitoring/losses/index.html', PG.monitoring_losses()),
        ('monitoring/recon/reconciliation.csv', MG.recon_csv(MG.RECON_M, MG.RECON_D)),
        ('map/index.html', PG.map_page()),
    ]
    for k in keys:
        s = C.slug(k)
        pages += [
            (f'report/{s}/index.html', PG.plant_report(k)),
            (f'report/{k.lower()}/index.html', C.redirect_page(f'/report/{s}/', name(k), name(k))),
            (f'monitoring/{s}/index.html', PG.monitoring_plant(k, MG.TODAY)),
            (f'monitoring/{k.lower()}/index.html', C.redirect_page(f'/monitoring/{s}/', name(k), name(k))),
        ]
        pages += [(f'monitoring/{s}/d/{d}.html', PG.monitoring_plant(k, d)) for d in MG.DATES if d != MG.TODAY]
    for rel, content in pages:
        PG.write(rel, content)
    return [rel for rel, _ in pages]


def publish(stage, out):
    """Swap the checked staging folder in; the old demo is kept until then."""
    old = out + '.old'
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(out):
        os.replace(out, old)
    os.replace(stage, out)
    shutil.rmtree(old, ignore_errors=True)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    out = os.path.abspath(argv[1] if len(argv) > 1 else DEMO_ROOT)
    stage = out + '.staging'
    # 1) every psql of the generators reads the demo views first
    # and can never write: the session itself is read-only
    os.environ['PGOPTIONS'] = DEMO_OPTS
    if not demo_schema_ok():
        print('demo_gen: REFUSED - demo views missing or search_path/read-only not in effect '
              '(apply demo_schema.sql); nothing published', file=sys.stderr)
        return 2
    # the same import paths portal_gen / monitoring_gen use (bundle, repo checkout)
    sys.path[:0] = [HERE, os.path.dirname(HERE)]
    for cand in ('/root/argia_v2/v2', os.path.dirname(os.path.dirname(HERE)),
                 '/root/argia_v2/v2/scripts', os.path.join(os.path.dirname(os.path.dirname(HERE)), 'scripts')):
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    n_assets = copy_assets(PORTAL_ROOT, stage)
    # 2) the portal generator, unchanged, writing into the staging folder
    # the real names are read BEFORE configure() replaces the slug table
    rules = build_rules(real_names())
    try:
        from argia.alerts import naming as _naming
        _orig_short = _naming.short_customer
        _naming.short_customer = lambda nm, _o=_orig_short: demo_display(nm, _o)
    except ImportError:
        pass
    saved = sys.argv
    sys.argv = [os.path.join(HERE, 'portal_gen.py'), stage]
    try:
        for m in ('portal_gen', 'report_gen', 'monitoring_gen'):
            sys.modules.pop(m, None)
        import portal_gen as PG                        # noqa: E402  (loads PG through the demo views)
    finally:
        sys.argv = saved
    keys, demo_names = configure(PG)
    rels = write_pages(PG, keys)
    # 3) scrub + the leak gate
    scrubbed, found = 0, []
    for rel in rels:
        p = os.path.join(stage, rel)
        with open(p, encoding='utf-8') as fh:
            text = fh.read()
        for a, b in TEXT_FIXES:
            text = text.replace(a, b)
        text, _ = unlink_absent(text)
        text, n = scrub(text, rules, demo_names)
        scrubbed += n
        hits = leaks(text, rules)
        if hits:
            found += [(rel, tok, ctx) for tok, ctx in hits]
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(text)
    if found:
        for rel, tok, ctx in found[:20]:
            print(f'demo_gen: LEAK {rel}: {tok!r} in ...{ctx}...', file=sys.stderr)
        print(f'demo_gen: REFUSED - {len(found)} real name(s) left after the scrub; nothing published', file=sys.stderr)
        shutil.rmtree(stage, ignore_errors=True)
        return 1
    publish(stage, out)
    print(f'demo_gen: wrote {len(rels)} pages + {n_assets} assets under {out} '
          f'({len(keys)} plants, all PPA; {scrubbed} name(s) scrubbed; 0 leaks)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
