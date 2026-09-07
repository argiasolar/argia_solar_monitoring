"""portal.argia.com.mx — the shared chrome (v208).

Pure: no PostgreSQL, no files. portal_gen.py imports the data from
report_gen / monitoring_gen and renders through the helpers here, so
the header rule, the naming rule and the design tokens live in ONE
place and are unit-tested without a database.

Rules (Tomasz, 2026-09-05):
* every page has exactly three buttons on the right — Home, Ask ARGIA,
  You — plus the section's sub-tabs; nothing else in the header;
* customers are named by their name (Taigene); the plant code (GTO1)
  is an internal addon, small and grey, never leading;
* nothing from the old site is dropped: bilingual text (data-en /
  data-es + localStorage.argia_lang), the ⓘ tooltips, the flip
  tiles, logo grey→colour on hover, photos, print — all carried over.
"""
from __future__ import annotations

import html
import re
import unicodedata

try:
    from argia_logo import LOGO_URI          # the official wordmark, PNG data URI
except ImportError:                          # never in production; keeps the module pure in odd test paths
    LOGO_URI = ''

PORTAL_HOST = 'portal.argia.com.mx'
ENGINE_URL = 'https://engine.sprinkler.agency/engine'
AGS_URL = 'https://sprinkler.agency/argiagoldenstandard/ARGIA_Golden_Standard_Designer_Training_WHITE.html'

# plant code -> URL slug. Static on purpose: slugs are identifiers
# (links, mails, the auth map) and must not move when a customer
# string in PG is edited. display names still come from PG.
SLUGS = {
    'GTO1': 'taigene', 'GTO2': 'hirschmann', 'MEX1': 'sag', 'MEX2': 'vitalmex',
    'MEX3': 'sms', 'NL1': 'plastic-omnium', 'NL2': 'budenheim', 'QRO1': 'tetra-pak',
    'SLP1': 'quimica-coyoacan', 'SLP2': 'holiday-inn-express', 'TAM1': 'ryder',
    'LOAX1': 'grupo-modelo', 'LGTO1': 'pirelli',
}
CODE_OF_SLUG = {v: k for k, v in SLUGS.items()}

SECTIONS = {
    # section -> (label EN, label ES, sub-tabs [(slug, EN, ES)])
    'report': ('Report', 'Reporte', [
        ('', 'Overview', 'Resumen'), ('ppa', 'PPA', 'PPA'), ('capex', 'CAPEX', 'CAPEX'),
        ('plants', 'Plant performance', 'Desempeño por planta'),
        ('financial', 'Financial', 'Financiero'), ('invoices', 'Invoices', 'Facturas')]),
    'monitoring': ('Monitoring', 'Monitoreo', [
        ('', 'Overview', 'Resumen'), ('ppa', 'PPA', 'PPA'), ('capex', 'CAPEX', 'CAPEX'),
        ('performance', 'Performance', 'Desempeño'), ('recon', 'Reconciliation', 'Conciliación')]),
    'map': ('Map', 'Mapa', []),
    'engine': ('Engine', 'Engine', []),
    'ags': ('ARGIA Golden Standard', 'ARGIA Golden Standard', []),
    'setup': ('Setup', 'Configuración', [
        ('people', 'People', 'Personas'), ('plants', 'Plants', 'Plantas'),
        ('finance', 'Finance', 'Finanzas'), ('cfe', 'CFE & tariffs', 'CFE y tarifas'),
        ('system', 'System', 'Sistema')]),
    'maintenance': ('Maintenance', 'Mantenimiento', [
        ('', 'Open', 'Abiertos'), ('new', 'New ticket', 'Nuevo ticket'), ('resolved', 'Resolved', 'Resueltos')]),
}


# ----------------------------------------------------------------- names
def display_name(customer):
    """Human name, never the code — same rule as monitoring_gen.display_name
    (v177.1). 'TAIGENE PPA roof (Leon, GTO)' -> 'Taigene'; short all-caps
    acronyms (SAG, SMS) survive."""
    s = str(customer or '').split('(')[0].split(',')[0]
    for cut in (' PPA', ' CAPEX', ' roof', ' land'):
        i = s.find(cut)
        if i > 0:
            s = s[:i]
    parts = s.strip().split()
    if len(parts) == 1 and len(parts[0]) <= 3 and parts[0].isupper():
        return parts[0]
    return ' '.join('-'.join(p[:1].upper() + p[1:].lower()
                             for p in w.split('-')) for w in parts)


def location_of(customer):
    """'TAIGENE PPA roof (Leon, GTO)' -> 'Leon, GTO'."""
    m = re.search(r'\(([^)]*)\)', str(customer or ''))
    return m.group(1).strip() if m else ''


def slugify(name):
    s = unicodedata.normalize('NFKD', str(name)).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')
    return s


def slug(code):
    return SLUGS.get(code, slugify(code))


def pname(code, customer, size=14, block=False):
    """Customer name first, code as the small grey addon."""
    name = html.escape(display_name(customer))
    if block:
        return (f'<span class="pnb"><span class="pname" style="font-size:{size}px">{name}</span>'
                f'<span class="pcode">{html.escape(code)}</span></span>')
    return (f'<span class="pname" style="font-size:{size}px">{name}</span>'
            f'<span class="pcode">{html.escape(code)}</span>')


# ------------------------------------------------------------ i18n bits
def t(en, es=None):
    es = en if es is None else es
    return (f'<span data-en="{html.escape(en, quote=True)}" '
            f'data-es="{html.escape(es, quote=True)}">{html.escape(en)}</span>')


def ti(en, es=None):
    """The ⓘ + cream tooltip on a KPI tile (hover or focus)."""
    return ('<span class="ti" tabindex="0" role="note" aria-label="definition">i</span>'
            f'<span class="tipbox">{t(en, es)}</span>')


# ----------------------------------------------------------------- icons
_ICONS = {
    'home': '<path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-6h4v6"/>',
    'ask': '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.2a2.6 2.6 0 0 1 5 .9c0 1.8-2.5 2.1-2.5 3.7"/><circle cx="12" cy="17" r=".7" fill="currentColor"/>',
    'user': '<circle cx="12" cy="8.5" r="3.6"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
    'report': '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    'monitor': '<path d="M3 14h4l3-7 4 10 3-6h4"/>',
    'map': '<path d="M3 6.5 9 4l6 2.5 6-2.5v13.5L15 20l-6-2.5-6 2.5z"/><path d="M9 4v13.5M15 6.5V20"/>',
    'engine': '<circle cx="12" cy="12" r="3.2"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/>',
    'ags': '<path d="M12 3l2.6 5.4 5.9.8-4.3 4.1 1.1 5.9L12 16.4l-5.3 2.8 1.1-5.9-4.3-4.1 5.9-.8z"/>',
    'setup': '<path d="M4 7h10M18 7h2M4 12h4M12 12h8M4 17h12M20 17h0"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="17" r="2"/>',
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8"/>',
    'bell': '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    'arrow': '<path d="M5 12h14M13 6l6 6-6 6"/>',
    'globe': '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    'out': '<path d="M10 4H5v16h5M14 8l4 4-4 4M8 12h10"/>',
    'print': '<path d="M7 8V4h10v4M7 17H4v-6h16v6h-3"/><rect x="7" y="14" width="10" height="6"/>',
    'ext': '<path d="M14 4h6v6M20 4l-9 9M18 13v7H4V6h7"/>',
    'cal': '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    'bolt': '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    'chev': '<path d="m6 9 6 6 6-6"/>',
    'maint': '<path d="M14.5 5.5a4 4 0 0 0-5.3 5.1L4 15.8V20h4.2l5.2-5.2a4 4 0 0 0 5.1-5.3l-2.6 2.6-2.2-.6-.6-2.2z"/>',
}


def ico(name, size=20, color='currentColor', sw=1.8):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f'{_ICONS[name]}</svg>')


# ------------------------------------------------------------------- css
CSS = '''
:root{--teal:#05b1a9;--teal2:#05847d;--deep:#053b38;--bg:#eef0f3;--ink:#1a1d23;--ink2:#41474f;--muted:#6b7480;--line:#e3e6ea;--line2:#d2d7dd;
 /* tokens the report_gen fragments (charts, plant page) paint with */
 --s1:#05b1a9;--s2:#eb6834;--surface:#fff;--border:#e3e6ea;--grid:#eceef0;--axis:#d5d9dd;--warn:#f0a83b;
 --green-bg:#e6f7f5;--green-tx:#05847d;--amber-bg:#fff4e0;--amber-tx:#b26a00;--laas-bg:#efe6fb;--laas-tx:#6b3fb5;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--teal2);text-decoration:none}a:hover{color:var(--deep)}
.wrap{max-width:1280px;margin:0 auto;padding:26px 28px 44px}
.mono{font:600 11px ui-monospace,Menlo,Consolas,monospace;letter-spacing:.02em}
.muted{color:var(--muted)}
.card{background:#fff;border:1px solid var(--line);border-radius:12px}
.kicker{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--teal2);font-weight:800}
h1.pt{font-size:28px;line-height:1.1;margin:0;color:var(--deep);font-weight:800;letter-spacing:-.01em}
h2.ct{font-size:15px;margin:0;font-weight:700}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:999px;font-size:11.5px;font-weight:700;white-space:nowrap}
.ok,.good{background:#e6f7f5;color:var(--teal2)}.warn{background:#fff4e0;color:#b26a00}.crit,.bad{background:#fdeaea;color:#c2554e}.off{background:var(--bg);color:var(--muted)}.laas{background:#efe6fb;color:#6b3fb5}
.pname{font-weight:700;color:var(--ink)}.pcode{font:600 10.5px ui-monospace,monospace;color:#8b95a1;margin-left:6px}
.pnb{display:inline-flex;flex-direction:column}.pnb .pcode{margin:0}
.grid{display:grid;gap:14px}.g5{grid-template-columns:repeat(5,minmax(0,1fr))}.g4{grid-template-columns:repeat(4,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
@media(max-width:1000px){.g5,.g4{grid-template-columns:repeat(2,minmax(0,1fr))}.g3{grid-template-columns:1fr 1fr}}
@media(max-width:640px){.g5,.g4,.g3,.g2{grid-template-columns:1fr}.wrap{padding:18px 14px 32px}}
/* header — the one pattern */
header.ph{background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:40}
.phrow{max-width:1280px;margin:0 auto;padding:0 28px;display:flex;align-items:center;gap:18px;height:60px}
.wm{display:flex;align-items:center;gap:9px;color:var(--deep)}.wm .wmb{width:26px;height:26px;border-radius:7px;background:var(--teal);display:flex;align-items:center;justify-content:center}
.wm .wmt{font-weight:800;font-size:17px;letter-spacing:.16em}.wm .wmlogo{height:26px;width:auto;display:block}
.psec{display:flex;align-items:center;gap:10px}.psec .sep{width:1px;height:22px;background:var(--line)}.psec .pn{font-weight:700;font-size:15px}
.hbtns{display:flex;gap:8px;align-items:center;position:relative;margin-left:auto}
.ib{width:40px;height:40px;border-radius:10px;border:1px solid var(--line2);background:#fff;display:flex;align-items:center;justify-content:center;color:var(--ink2);cursor:pointer;padding:0}
.ib:hover{border-color:var(--teal)}.ib.ask{background:var(--teal);border-color:var(--teal);color:var(--deep)}
/* folder tabs: the active one is the open folder, joined to the page */
.tabs{max-width:1280px;margin:0 auto;padding:10px 28px 0;display:flex;gap:4px;flex-wrap:wrap;align-items:flex-end;overflow:visible}
.tab{padding:9px 16px;font-weight:600;font-size:13.5px;color:var(--muted);background:#f4f6f8;border:1px solid var(--line);border-bottom:0;border-radius:9px 9px 0 0;white-space:nowrap;margin-bottom:-1px}
.tab.on{color:var(--deep);background:var(--bg);border-color:var(--line2);box-shadow:inset 0 3px 0 var(--teal)}.tab:hover{color:var(--deep);background:#eef0f3}
/* user menu */
.umenu{display:none;position:absolute;right:0;top:48px;width:240px;background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 32px rgba(26,29,35,.14);padding:8px;z-index:50}
.umenu.open{display:block}.umenu .uwho{padding:10px 12px 6px;display:flex;flex-direction:column}.umenu .uwho b{font-size:14px}
.umenu a,.umenu button{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:none;border:0;border-radius:7px;padding:9px 12px;font:inherit;font-size:13.5px;color:var(--ink);cursor:pointer}
.umenu a:hover,.umenu button:hover{background:#f1f3f5}.umenu .uout{color:#c2554e}
.umenu .seg{margin-left:auto}.wa{padding:1px 7px;border-radius:9px;font-size:11px;font-weight:600;background:#ecebf6;color:#4f4a94}
.seg{display:inline-flex;border:1px solid var(--line2);border-radius:8px;overflow:hidden}.seg button,.seg a,.seg span{padding:7px 12px;font:600 12.5px inherit;font-family:inherit;color:var(--muted);border:0;border-right:1px solid var(--line);background:#fff;cursor:pointer}
.seg>*:last-child{border-right:0}.seg .active,.seg .on{background:#e6f7f5;color:var(--deep)}
.adminonly,.askonly{display:none}
/* buttons */
.btn{background:var(--teal);color:var(--deep);border:0;border-radius:8px;padding:11px 18px;font-weight:800;font-size:13px;letter-spacing:.03em;cursor:pointer;display:inline-flex;align-items:center;gap:8px;white-space:nowrap;font-family:inherit}
.btn2{background:#fff;color:var(--ink2);border:1px solid var(--line2);border-radius:8px;padding:9px 14px;font-weight:600;font-size:13px;cursor:pointer;display:inline-flex;align-items:center;gap:8px;white-space:nowrap;font-family:inherit}
.btn2:hover{border-color:var(--teal)}
/* tiles */
.tile{position:relative;padding:16px 18px;display:flex;flex-direction:column;gap:6px;background:#fff;border:1px solid var(--line);border-radius:12px}
.tile.good{background:#e6f7f5;border-color:#b8e6e1}.tile.warn{background:#fff4e0;border-color:#f3dcae}.tile.bad{background:#fdeaea;border-color:#f3b9b9}
.tlabel{font-size:12px;color:var(--muted);font-weight:600;display:flex;align-items:center}
.tval{font-size:30px;line-height:1;font-weight:800;letter-spacing:-.01em;color:var(--ink)}.tval .unit{font-size:14px;font-weight:600;color:var(--muted);margin-left:4px}
.tsub{font-size:12.5px;color:var(--muted)}
.ti{display:inline-flex;width:15px;height:15px;border-radius:50%;border:1.5px solid #b6bec8;color:var(--muted);font-weight:800;font-size:10px;align-items:center;justify-content:center;margin-left:6px;cursor:help}
.tipbox{display:none;position:absolute;left:10px;right:10px;top:44px;z-index:30;background:#fffdf4;border:1px solid #e8dfa8;border-radius:8px;padding:10px 12px;font-size:12px;color:#3a4049;box-shadow:0 8px 24px rgba(26,29,35,.12)}
.ti:hover+.tipbox,.ti:focus+.tipbox,.tipbox:hover{display:block}
/* flip tiles: only .haswhy (amber/red with a reason) turn around */
.tile.flip{perspective:800px;background:transparent;border:0;padding:0}
.flipin{position:relative;transform-style:preserve-3d;transition:transform .55s cubic-bezier(.4,.1,.2,1) .12s;min-height:100%}
.tile.haswhy:hover .flipin,.tile.haswhy:focus-within .flipin{transform:rotateY(180deg)}
.face{backface-visibility:hidden;padding:16px 18px;display:flex;flex-direction:column;gap:6px;border:1px solid var(--line);border-radius:12px;background:#fff;min-height:118px}
.face.back{position:absolute;inset:0;transform:rotateY(180deg)}
.face.warn{background:#fff4e0;border-color:#f3dcae}.face.bad{background:#fdeaea;border-color:#f3b9b9}.face.good{background:#e6f7f5;border-color:#b8e6e1}
.bwhy{font-size:12px;color:var(--muted);font-weight:600}.twhy{font-size:13px;font-weight:600;color:#b26a00}.face.bad .twhy{color:#c2554e}
/* logos & photos */
.clogo{height:22px;width:auto;max-width:110px;object-fit:contain;display:block;filter:grayscale(1);opacity:.75;transition:filter .25s,opacity .25s}
.lcell{display:flex;align-items:center;gap:12px;color:var(--ink)}.lcell .lbox{width:84px;flex:0 0 84px;display:flex;align-items:center}.lcell .lbox .clogo{height:18px;max-width:80px}
.pcard:hover .clogo,.clogo.color,tr:hover .clogo{filter:none;opacity:1}
.tphoto{width:100%;height:96px;object-fit:cover;border-radius:8px}
/* tables */
table{border-collapse:collapse;width:100%}th{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:700;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid #f0f2f4;font-size:13.5px;vertical-align:middle}td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.chead{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}
/* SVG classes the report_gen charts paint with (columns_svg, monthly_svg, PLANT_JS) */
svg{max-width:100%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--muted);font-size:12px}.lab{fill:var(--ink2);font-size:12.5px}
.bar{fill:var(--s1)}.bar:hover{opacity:.85}.bar.exp{fill:#c9ced4}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.line.s1{stroke:var(--s1)}.line.s2{stroke:var(--s2)}.line.rev{stroke:#1e8e3e}
.line.wx{stroke:#eab308;stroke-dasharray:5 4;stroke-width:2.5}
.dot{stroke:var(--surface);stroke-width:2}.dot.s1{fill:var(--s1)}.dot.s2{fill:var(--s2)}
.hit{fill:transparent}
/* invoices index (scripts/invoice_publish.index_body) */
.invbody h2{font-size:16px;margin:26px 0 2px}.invbody .msum{color:var(--muted);font-size:13px;margin:2px 0 8px}
.invbody table{background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}.invbody th,.invbody td{padding:9px 12px}
.invbody .btn{background:#fff;color:var(--ink);border:1px solid var(--line2);padding:5px 12px;font-weight:600;font-size:12.5px}.invbody .btn.zip{border-color:var(--teal);color:var(--deep)}
.invbody .mut{color:#9aa0a6;font-size:13px}.invbody .blocked{color:#c2554e;font-size:13px}
tr.total td{border-top:2px solid var(--line2);background:#fafbfd}
/* legacy fragment classes (plant page from report_gen.plant_parts) */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(218px,1fr));gap:12px;margin:16px 0}
.tile.flip .face.front{position:relative}.tile.flip.good .face{background:#e6f7f5;border-color:#b8e6e1}.tile.flip.warn .face{background:#fff4e0;border-color:#f3dcae}.tile.flip.bad .face{background:#fdeaea;border-color:#f3b9b9}
.tile .thero{font-size:27px;font-weight:800}
.card h2{font-size:15px;font-weight:700;margin:0 0 6px}.card>h2,.card>.note,.card>.legend,.card>table,.card>svg,.card>div#dchart{margin-left:20px;margin-right:20px}.card>h2{padding-top:16px}.card>table{width:calc(100% - 40px);margin-bottom:16px}.card>svg,.card>#dchart{margin-bottom:16px;max-width:calc(100% - 40px)}
.card td .pill{font-size:inherit;padding:1px 9px}
.card>.tscroll{margin:0 20px 16px;max-width:calc(100% - 40px)}.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}.tscroll>table{width:100%;margin:0}
.card>details{margin:16px 20px}.card>details summary{cursor:pointer;font-weight:700;font-size:14px;color:var(--ink)}.card>details[open] summary{margin-bottom:6px}.card.audit p{margin:6px 0;line-height:1.55;font-size:13px;color:var(--ink2)}
.note{font-size:12.5px;color:var(--muted);margin:0 0 8px}.legend{display:flex;gap:14px;font-size:12.5px;color:var(--ink2);margin:0 0 6px;flex-wrap:wrap}
.key{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0}.rangebar{gap:18px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:10px 16px}
.rgroup{display:flex;align-items:center;gap:6px}.rangebar .sub{font-size:12px;color:var(--muted);font-weight:600;display:flex;align-items:center;gap:6px}
.rangebar input.btn{background:#fff;color:var(--ink);border:1px solid var(--line2);font-weight:600;padding:7px 10px;font-size:13px}
.rangebar .seg .btn{background:#fff;color:var(--muted);border:1px solid var(--line2);border-radius:0;margin-left:-1px;font-weight:600;padding:8px 12px}
.rangebar .seg .btn:first-child{border-radius:8px 0 0 8px;margin-left:0}.rangebar .seg .btn:last-child{border-radius:0 8px 8px 0}.rangebar .seg .btn.active{background:#e6f7f5;color:var(--deep)}
.rangebar .live{margin-left:auto}.rangebar .rdays{font-family:ui-monospace,monospace}
.pdfrow{text-align:center;margin:22px 0 4px}footer{font-size:12px;color:var(--muted);margin-top:20px;line-height:1.6}
.pill.PPA,.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700}
/* ask bar */
.askbar{padding:16px 20px;display:flex;align-items:center;gap:16px;background:var(--deep);border-color:var(--deep);color:#e6f7f5;border-radius:12px}
.askbar .askin{display:flex;align-items:center;gap:10px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.18);border-radius:9px;padding:10px 14px;width:420px;max-width:40%;color:#9fc9c5}
footer.pf{max-width:1280px;margin:0 auto;padding:0 28px 24px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.legacy{font-size:12px;color:var(--muted);display:inline-flex;align-items:center;gap:6px}
@media print{header.ph,.noprint,.ti,.tipbox,.face.back,footer.pf{display:none!important}body{background:#fff}.wrap{padding:0}}
'''

# --------------------------------------------------------------------- js
JS = r'''
<script>
function argiaMenu(e){e.stopPropagation();const m=document.getElementById('umenu');const b=document.getElementById('ubtn');
 const o=!m.classList.contains('open');m.classList.toggle('open',o);b.setAttribute('aria-expanded',o?'true':'false');}
document.addEventListener('click',()=>{const m=document.getElementById('umenu');if(m)m.classList.remove('open');});
function argiaLogout(){fetch('/logout',{method:'POST',credentials:'same-origin'}).finally(()=>{location.href='/logged-out.html';});}
function setLang(l,save){
 document.querySelectorAll('[data-en]').forEach(e=>{e.textContent=e.dataset[l]||e.dataset.en;});
 document.querySelectorAll('.lang-btn').forEach(b=>b.classList.toggle('active',b.dataset.l===l));
 document.documentElement.lang=l==='es'?'es':'en';
 try{localStorage.setItem('argia_lang',l);}catch(e){}
 if(save){fetch('/session/lang',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({lang:l})}).catch(()=>{});}
}
function argiaFit(){
 /* v224: a table wider than its card scrolls inside the card — the page never scrolls sideways */
 document.querySelectorAll('.wrap table').forEach(t=>{
  if(t.closest('.tscroll')||t.parentElement.closest('table'))return;
  const w=document.createElement('div');w.className='tscroll';t.parentNode.insertBefore(w,t);w.appendChild(t);});
}
window.addEventListener('DOMContentLoaded',()=>{
 argiaFit();
 let stored=null;try{stored=localStorage.getItem('argia_lang');}catch(e){}
 setLang(stored||'en');
 const who=document.getElementById('uwho');
 fetch('/session/whoami',{credentials:'same-origin'}).then(r=>r.ok?r.json():null).then(d=>{
  if(!d||!d.user){return;}
  if(!stored&&d.lang){setLang(d.lang);}
  const g=document.getElementById('gname');if(g){g.textContent=', '+(d.first||d.name||d.user);}
  if(who){who.querySelector('b').textContent=d.name||d.user;
   const s=who.querySelector('.mono');s.textContent=d.user;
   if(d.admin){const a=document.createElement('span');a.className='wa';a.textContent='admin';s.appendChild(document.createTextNode(' '));s.appendChild(a);
    document.querySelectorAll('.adminonly').forEach(x=>x.style.display='');}}
  if(!d.admin){const ts=document.getElementById('tile-setup');if(ts){ts.href='/setup/cfe/';const m=ts.querySelector('.mono');if(m)m.textContent='/setup/cfe';
   const b=ts.querySelector('.tblurb');if(b){b.dataset.en='CFE tariffs and your account.';b.dataset.es='Tarifas CFE y tu cuenta.';b.textContent=b.dataset[localStorage.getItem('argia_lang')||'en']||b.dataset.en;}}}
 }).catch(()=>{});
 fetch('/ask/me',{credentials:'same-origin'}).then(r=>r.ok?r.json():null).then(d=>{
  if(d&&d.allowed)document.querySelectorAll('.askonly').forEach(x=>x.style.display='');}).catch(()=>{});
 document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();location.href='/ask/';}});
});
</script>'''


# ---------------------------------------------------------------- header
def wordmark():
    """The official ARGIA wordmark (v210) — on every page, linking home."""
    if LOGO_URI:
        return f'<a class="wm" href="/" title="Home"><img class="wmlogo" src="{LOGO_URI}" alt="ARGIA SOLAR"></a>'
    return (f'<a class="wm" href="/" title="Home">'
            f'<span class="wmb">{ico("sun", 16, "#053b38", 2.2)}</span><span class="wmt">ARGIA</span></a>')


def user_menu():
    return f'''<div class="umenu" id="umenu" role="menu">
 <div class="uwho"><b>…</b><span class="mono muted"></span></div>
 <a href="/account/">{ico("user", 16)} {t("My account", "Mi cuenta")}</a>
 <div style="display:flex;align-items:center;gap:10px;padding:9px 12px">{ico("globe", 16)} {t("Language", "Idioma")}
  <span class="seg"><button class="lang-btn" data-l="en" onclick="setLang('en',true)">EN</button><button class="lang-btn" data-l="es" onclick="setLang('es',true)">ES</button></span></div>
 <button class="uout" onclick="argiaLogout()">{ico("out", 16)} {t("Log out", "Cerrar sesión")}</button>
</div>'''


def header(section=None, on='', tabs_override=None):
    """The one header. section: key of SECTIONS or None (landing);
    on: sub-tab slug that is active ('' = the section's overview);
    tabs_override: a shorter sub-tab list (v214: a non-admin in Setup
    sees only CFE & tariffs)."""
    sec = ''
    tabs = ''
    if section:
        en, es, subs = SECTIONS[section]
        if tabs_override is not None:
            subs = tabs_override
        sec = (f'<div class="psec"><span class="sep"></span><span class="pn">{t(en, es)}</span>'
               f'<span class="mono muted">{PORTAL_HOST}/{section}</span></div>')
        if subs:
            tabs = '<nav class="tabs">' + ''.join(
                f'<a class="tab{" on" if s == on else ""}" href="/{section}/{s + "/" if s else ""}">{t(ten, tes)}</a>'
                for s, ten, tes in subs) + '</nav>'
    return f'''<header class="ph noprint">
 <div class="phrow">
  {wordmark()}
  {sec}
  <div class="hbtns">
   <a class="ib" href="/" title="Home">{ico("home")}</a>
   <a class="ib ask" href="/ask/" title="Ask ARGIA · Ctrl/⌘ K">{ico("ask", 20, "#053b38", 2)}</a>
   <button class="ib" id="ubtn" onclick="argiaMenu(event)" aria-haspopup="true" aria-expanded="false" title="You"><span id="uwho" hidden><b></b><span class="mono"></span></span>{ico("user")}</button>
   {user_menu()}
  </div>
 </div>
 {tabs}
</header>'''


def page(title, body, section=None, on='', refresh=0, extra_head='', tabs=None):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ''
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow">{meta}'
            f'<title>{html.escape(title)} — ARGIA</title><link rel="icon" href="/favicon.png">'
            f'<style>{CSS}</style>{extra_head}</head><body>'
            f'{header(section, on, tabs)}<div class="wrap">{body}</div>{JS}</body></html>')


def skin_reset(scope):
    """v212: the rules a skinned body (.monbody, .setupbody, .askbody)
    needs AFTER its scoped legacy CSS. The legacy `.card` has its own
    padding and `table{width:100%}`; the portal's `.card>table` adds
    20px side margins on top — 40px of overflow and a pointless
    scrollbar under every table (Tomasz 2026-09-05). Here the card
    keeps the legacy padding, loses the portal margins and the
    scrollbar; wide text cells wrap instead of pushing the width."""
    s = scope
    return (f'{s} .card{{overflow:visible}}'
            f'{s} .card>h2,{s} .card>.note,{s} .card>.legend,{s} .card>table,{s} .card>svg,{s} .card>div,{s} .card>p,{s} .card>.tscroll{{margin-left:0;margin-right:0;max-width:100%}}'
            f'{s} .card>h2{{padding-top:0}}{s} .card>table{{width:100%;table-layout:auto;margin-bottom:6px}}{s} .tscroll>table{{width:100%;table-layout:auto;margin-bottom:6px}}{s} .card>.tscroll{{margin-bottom:6px}}'
            f'{s} .card>svg,{s} .card>#dchart{{margin-bottom:6px}}'
            f'{s} table{{font-size:13px}}{s} th,{s} td{{padding:6px 7px}}'
            f'{s} td .note,{s} td.note{{white-space:normal}}'
            f'{s} td:first-child{{white-space:normal}}'
            f'{s} .kv td:first-child{{white-space:nowrap}}')


def scoped_css(css, scope):
    """Prefix every selector of a stylesheet with `scope` so an app's own
    content rules (table, .btn, input …) cannot fight the portal chrome.
    @media / @supports blocks are recursed into; other @-rules pass through."""
    out, i, n = [], 0, len(css)
    while i < n:
        j = css.find('{', i)
        if j < 0:
            break
        head = css[i:j].strip()
        # matching close brace for this block
        depth, k = 1, j + 1
        while k < n and depth:
            if css[k] == '{':
                depth += 1
            elif css[k] == '}':
                depth -= 1
            k += 1
        inner = css[j + 1:k - 1]
        if head.startswith('@media') or head.startswith('@supports'):
            out.append(f'{head}{{{scoped_css(inner, scope)}}}')
        elif head.startswith('@'):
            out.append(f'{head}{{{inner}}}')
        elif head:
            sels = ','.join(f'{scope} {x.strip()}' for x in head.split(',') if x.strip())
            out.append(f'{sels}{{{inner}}}')
        i = k
    return '\n'.join(out)


# ------------------------------------------------------------ components
def tile(label_en, label_es, value, sub_en='', sub_es='', tip=None, tone='',
         why_en='', why_es=''):
    """KPI tile. `tip` = (en, es) shows the ⓘ tooltip. A tone of warn/bad
    with a `why` becomes a flip tile (front value, back reason)."""
    tp = ti(*tip) if tip else ''
    front = (f'<div class="tlabel">{t(label_en, label_es)}{tp}</div>'
             f'<div class="tval">{value}</div>'
             f'<div class="tsub">{t(sub_en, sub_es) if sub_en else ""}</div>')
    if why_en and tone in ('warn', 'bad'):
        return (f'<div class="tile flip haswhy" tabindex="0"><div class="flipin">'
                f'<div class="face {tone}">{front}</div>'
                f'<div class="face back {tone}"><div class="bwhy">{t("why this color", "por qué este color")}</div>'
                f'<div class="twhy">⚠ {t(why_en, why_es or why_en)}</div></div></div></div>')
    return f'<div class="tile {tone}">{front}</div>'


def pill(cls, en, es=None):
    return f'<span class="pill {cls}">{t(en, es)}</span>'


def redirect_page(to, en, es=None):
    """A sub-tab that still lives on the old site: send the browser
    there, but say so (parity phase — nothing is dropped)."""
    # v219: forward the fragment (#lang=es&slide=281 — Ask ARGIA citations)
    # with a script; the meta refresh is the no-JS fallback and drops it.
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<script>location.replace({to!r}+location.hash);</script>'
            f'<noscript><meta http-equiv="refresh" content="0;url={to}"></noscript>'
            '<meta name="robots" content="noindex,nofollow"><title>ARGIA</title></head>'
            f'<body style="font-family:sans-serif;padding:40px">{t(en, es)} → <a href="{to}">{to}</a></body></html>')
