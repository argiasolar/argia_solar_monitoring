#!/usr/bin/env python3
"""Generate the ARGIA live monitoring portal — monitoring.argia.com.mx.

v109: day archive pages + date picker, PPA cumulative tiles + gauge,
data-completeness banners, maintenance badge, invoice-gate column.
Static regeneration from PostgreSQL every 5 minutes.

Pages:
  /monitoring/                 fleet overview (PPA / CAPEX sections)
  /monitoring/<key>/           per-plant, TODAY (live)
  /monitoring/<key>/d/<date>.html   per-plant, one archived day
  /monitoring/ppa/             all-PPA live + month-to-date + gauge
  /monitoring/recon/           reconciliation board (invoice gate)

Run:  python3 monitoring_gen.py [outroot]
Deploy: repo v2/server/ -> cp to /opt/argia/bundle/ after each pull.
"""
import datetime as dt
import html
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from argia_logo import LOGO_URI

# fault-code catalog from the repo checkout (documented vendor states)
sys.path.insert(0, '/root/argia_v2/v2')
try:
    from argia.alerts.fault_catalog import explain_fault, is_normal_state
except Exception:                                     # noqa: BLE001
    def explain_fault(vendor, raw):
        return raw or None

    def is_normal_state(vendor, raw):
        return (raw or '').strip() in ('', '0')

OUTROOT = sys.argv[1] if len(sys.argv) > 1 else '/www/hosting/monitoring.argia.com.mx/www'
MX = ZoneInfo('America/Mexico_City')
STALE_MIN = 30
WINDOW = (6, 20)
DAY_PAGES = 30          # how many past days get an archive page
# Everything lives on ONE host now (report.argia.com.mx): reports at
# the root, live monitoring under /monitoring/. One basic-auth prompt
# instead of two — browsers cache credentials per hostname, so two
# hostnames could never share a login. monitoring.argia.com.mx 301s
# here. BASE prefixes every monitoring link; REPORT_BASE is now
# same-origin, so the per-plant "Open report" button needs no host.
BASE = os.environ.get('ARGIA_MON_BASE', '/monitoring')
REPORT_BASE = os.environ.get('ARGIA_REPORT_BASE', '')


def q(sql):
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d',
                        'argia_mont', '-t', '-A', '-F', '\t', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])
    return [ln.split('\t') for ln in r.stdout.splitlines() if ln.strip()]


def f(s):
    try:
        return float(s) if s not in ('', None) else None
    except ValueError:
        return None


def esc(s):
    return html.escape(str(s), quote=True)


NOW_MX = dt.datetime.now(MX)
TODAY = NOW_MX.date().isoformat()
MX_D = "(ts_utc AT TIME ZONE 'America/Mexico_City')::date"
MX_H = "extract(hour FROM ts_utc AT TIME ZONE 'America/Mexico_City')::int"

# ---------------------------------------------------------------- plants
PLANTS = {}
for r in q("SELECT plant_key, customer, brand, kwp_dc, coalesce(portfolio,''),"
           " active, pr_baseline FROM plant ORDER BY plant_key;"):
    if len(r) >= 7 and r[5] == 't':
        PLANTS[r[0]] = {'customer': r[1], 'brand': r[2], 'kwp': f(r[3]) or 0,
                        'portfolio': r[4], 'pr': f(r[6]) or 0.80}

# ------------------------------------------- reference links + photos
# Public reference pages on argia.com.mx (NL1 and QRO1 have none yet —
# they simply render without the link/photo, never a broken one).
REF_LINKS = {
    'GTO1': 'https://argia.com.mx/es/references/-guanajuato-taigene',
    'GTO2': 'https://argia.com.mx/es/references/'
            '-san-miguel-de-allende-hirschmann-automotive',
    'MEX1': 'https://argia.com.mx/es/references/-san-pedro-sag-mexico',
    'MEX2': 'https://argia.com.mx/es/references/-cdmx-vitalmex',
    'MEX3': 'https://argia.com.mx/es/references/'
            '-tlalnepantla-service-management-solutions',
    'NL2': 'https://argia.com.mx/es/references/-monterrey-budenheim',
    'SLP1': 'https://argia.com.mx/es/references/coyoacan',
    'SLP2': 'https://argia.com.mx/es/references/holiday-inn',
}

# Vendor monitoring portals — clicking an inverter Status pill opens the
# brand's own portal in a new tab (login is the vendor's, not ours).
VENDOR_PORTAL = {
    'GROWATT': 'https://server.growatt.com/',
    'HUAWEI': 'https://la5.fusionsolar.huawei.com/'
              'pvmswebsite/login/build/index.html',
    'SOLAREDGE': 'https://monitoring.solaredge.com/mfe/auth/',
}


def photo_uri(pk, thumb=False):
    """Site photo (fetched once from the reference page into
    assets/<pk>.jpg in the web root; <pk>_t.jpg is the ~520px thumbnail
    for fleet tiles). Missing file -> None; every page must render fine
    without it."""
    name = f'{pk.lower()}_t.jpg' if thumb else f'{pk.lower()}.jpg'
    p = os.path.join(OUTROOT, 'monitoring', 'assets', name)
    if os.path.exists(p):
        return f'{BASE}/assets/{name}'
    if thumb:                       # thumbnail missing -> try the full one
        return photo_uri(pk, thumb=False)
    return None


# configured ACTIVE inverters — the honest denominator. Counting only
# inverters that answer hides a dead one inside a green plant (GTO2
# Inverter 2, found 2026-08-26).
CONFIG_INV = {}   # plant -> [(sn, label, rated_kw)]
for r in q("SELECT plant_key, inverter_sn,"
           " coalesce(inverter_label, inverter_sn), rated_kw"
           " FROM inverter WHERE active ORDER BY 1, 3;"):
    if len(r) >= 4:
        CONFIG_INV.setdefault(r[0], []).append((r[1], r[2], f(r[3])))

# Dates with REAL collection (newest first, capped). Vendors sometimes
# return rows carrying stale timestamps, which would create hollow
# archive days — a real fleet collection day has hundreds of rows.
DATES = [r[0] for r in q(
    f"SELECT {MX_D} AS d FROM telemetry GROUP BY 1"
    " HAVING count(*) >= 60 ORDER BY d DESC "
    f"LIMIT {DAY_PAGES};") if r and r[0]]
FIRST_DATE = DATES[-1] if DATES else TODAY

# ------------------------------------------------- per-date aggregates
# Latest USABLE sample per inverter per date (empty vendor replies are
# data gaps, not outages — see 2026-08-26 MEX1 incident).
LATEST = {}   # date -> plant -> [inverter dict]
for r in q("SELECT DISTINCT ON (d, plant_key, inverter_sn) * FROM ("
           f" SELECT {MX_D} AS d, plant_key, inverter_sn,"
           "  coalesce(inverter_label, inverter_sn) AS label, status,"
           "  power_w, etoday_kwh, temperature_c,"
           "  coalesce(fault_code::text,'') AS fault,"
           "  to_char(ts_utc AT TIME ZONE 'America/Mexico_City',"
           "   'HH24:MI') AS mx,"
           "  extract(epoch FROM now() - ts_utc)/60 AS age, ts_utc"
           "  FROM telemetry WHERE (etoday_kwh IS NOT NULL"
           "   OR power_w IS NOT NULL)"
           "   AND ts_utc > now() - interval '32 days') t"
           " ORDER BY d, plant_key, inverter_sn, ts_utc DESC;"):
    if len(r) >= 11:
        d = r[0]
        if d not in DATES:
            continue
        LATEST.setdefault(d, {}).setdefault(r[1], []).append({
            'sn': r[2], 'label': r[3], 'status': int(f(r[4]) or 0),
            'power_w': f(r[5]), 'etoday': f(r[6]), 'temp': f(r[7]),
            'fault': r[8] if r[8] not in ('', '0') else '',
            'last_mx': r[9], 'age_min': f(r[10]) or 9e9})

# hourly cumulative maxima -> per-hour energy
_HM = {}      # date -> plant -> sn -> {hour: cum}
for r in q(f"SELECT {MX_D}, plant_key, inverter_sn, {MX_H},"
           " max(etoday_kwh) FROM telemetry WHERE etoday_kwh IS NOT NULL"
           " GROUP BY 1, 2, 3, 4;"):
    if len(r) >= 5 and r[0] in DATES:
        _HM.setdefault(r[0], {}).setdefault(r[1], {}).setdefault(
            r[2], {})[int(r[3])] = f(r[4])

HOURLY = {}   # date -> plant -> sn -> {hour: kwh}
FIRST_TICK = {}  # date -> plant -> first hour with data (banner input)
for d, plants_ in _HM.items():
    for pk, invs in plants_.items():
        for sn, by_h in invs.items():
            prev = None
            out = {}
            for h in sorted(by_h):
                v = by_h[h]
                if v is None:
                    continue
                if prev is not None:
                    out[h] = max(0.0, v - prev)
                elif h <= 7:
                    out[h] = v      # dawn: first cumulative is real
                # else: sampling started mid-day -> catch-up, no bar
                prev = v
            HOURLY.setdefault(d, {}).setdefault(pk, {})[sn] = out
        hrs = [h for i in invs.values() for h in i]
        if hrs:
            FIRST_TICK.setdefault(d, {})[pk] = min(hrs)

CLOUD_H, IRR_H = {}, {}   # date -> plant -> {hour: val}
for r in q(f"SELECT {MX_D}, plant_key, {MX_H}, avg(cloud_cover_pct),"
           " avg(irradiance_wm2) FROM telemetry GROUP BY 1, 2, 3;"):
    if len(r) >= 5 and r[0] in DATES:
        h = int(r[2])
        if f(r[3]) is not None:
            CLOUD_H.setdefault(r[0], {}).setdefault(r[1], {})[h] = f(r[3])
        if f(r[4]) is not None:
            IRR_H.setdefault(r[0], {}).setdefault(r[1], {})[h] = f(r[4])

VENDOR_DAY = {}   # (plant, date) -> kwh
for r in q("SELECT plant_key, snap_date::text, daily_kwh"
           " FROM vendor_counter_snapshot WHERE daily_kwh IS NOT NULL;"):
    if len(r) >= 3:
        VENDOR_DAY[(r[0], r[1])] = f(r[2])

DAILY = {}        # plant -> [(date, energy, expected)]
for r in q("SELECT plant_key, prod_date::text, energy_kwh, expected_kwh"
           " FROM daily_production"
           f" WHERE prod_date >= DATE '{TODAY}' - 31 ORDER BY 1, 2;"):
    if len(r) >= 4:
        DAILY.setdefault(r[0], []).append((r[1], f(r[2]), f(r[3])))

MTD = {}          # plant -> {'prod': kwh, 'exp': kwh}
for r in q("SELECT plant_key, sum(energy_kwh), sum(expected_kwh)"
           " FROM daily_production"
           f" WHERE date_trunc('month', prod_date) ="
           f" date_trunc('month', DATE '{TODAY}') GROUP BY 1;"):
    if len(r) >= 3:
        MTD[r[0]] = {'prod': f(r[1]) or 0.0, 'exp': f(r[2])}

RECON_D = {}      # plant -> [rows newest first]
RECON_BY_PD = {}  # (plant, date) -> (completeness, status)
for r in q("SELECT plant_key, prod_date::text, interval_kwh,"
           " vendor_daily_kwh, kpi_kwh, completeness_pct, variance_pct,"
           " status, coalesce(note,'') FROM reconciliation_daily"
           f" WHERE prod_date >= DATE '{TODAY}' - 31 ORDER BY 2 DESC, 1;"):
    if len(r) >= 9:
        RECON_D.setdefault(r[0], []).append(r[1:])
        RECON_BY_PD[(r[0], r[1])] = (f(r[5]), r[7])

RECON_M = [r for r in q(
    "SELECT plant_key, to_char(ref_month,'YYYY-MM'), billing_kwh,"
    " billing_basis, status, coalesce(closed_by,''),"
    " coalesce(note,'') FROM reconciliation_monthly"
    " ORDER BY ref_month DESC, plant_key LIMIT 60;") if len(r) >= 7]

# performance, last 30 days (Phase F v1): PR, availability, vs expected
PERF = {}
for r in q("SELECT plant_key, round(avg(pr)::numeric, 3),"
           " round(avg(availability)::numeric, 3),"
           " sum(energy_kwh), sum(expected_kwh),"
           " count(*) FILTER (WHERE pr IS NOT NULL),"
           " round(avg(pr_stc)::numeric, 3)"
           " FROM daily_production"
           f" WHERE prod_date >= DATE '{TODAY}' - 30"
           " GROUP BY 1;"):
    if len(r) >= 7:
        PERF[r[0]] = {'pr': f(r[1]), 'avail': f(r[2]), 'prod': f(r[3]),
                      'exp': f(r[4]), 'pr_days': int(f(r[5]) or 0),
                      'pr_stc': f(r[6])}

PR_TREND = {}   # plant -> [(date, pr)] last 30d, for sparklines
for r in q("SELECT plant_key, prod_date::text, pr FROM daily_production"
           f" WHERE prod_date >= DATE '{TODAY}' - 30 AND pr IS NOT NULL"
           " ORDER BY 1, 2;"):
    if len(r) >= 3 and f(r[2]) is not None:
        PR_TREND.setdefault(r[0], []).append((r[1], f(r[2])))


def perf_avail_tiles(keys, grp=''):
    """Two KPI tiles — Performance (30-day PR) and Availability (30d) —
    kWp-weighted across `keys` (a single plant gives its own values).
    Bands match the performance page: PR >=0.75 green / >=0.65 amber;
    availability >=98% green / >=95% amber (IEC 63019). Missing data
    renders an em dash, never 0."""
    w_pr = kwp_pr = w_av = kwp_av = 0.0
    for k in keys:
        p = PERF.get(k, {})
        kwp = PLANTS[k]['kwp']
        if p.get('pr') is not None:
            w_pr += p['pr'] * kwp; kwp_pr += kwp
        if p.get('avail') is not None:
            w_av += p['avail'] * kwp; kwp_av += kwp
    pr = (w_pr / kwp_pr) if kwp_pr else None
    av = (w_av / kwp_av) if kwp_av else None
    pr_cls = ('' if pr is None else
              (' class="st-PASS"' if pr >= 0.75 else
               (' class="st-REVIEW"' if pr >= 0.65 else ' class="st-FAIL"')))
    av_cls = ('' if av is None else
              (' class="st-PASS"' if av >= 0.98 else
               (' class="st-REVIEW"' if av >= 0.95 else ' class="st-FAIL"')))
    g = (grp + ' ') if grp else ''
    return (
        f'<div class="kpi"><div class="v"><span{pr_cls}>'
        f'{"—" if pr is None else f"{pr:.3f}"}</span></div>'
        f'<div class="l" data-en="{g}performance · PR 30d"'
        f' data-es="Desempeño {grp} · PR 30d">{g}performance · PR 30d'
        '</div></div>'
        f'<div class="kpi"><div class="v"><span{av_cls}>'
        f'{"—" if av is None else f"{100 * av:,.1f}%"}</span></div>'
        f'<div class="l" data-en="{g}availability · 30d"'
        f' data-es="Disponibilidad {grp} · 30d">{g}availability · 30d'
        '</div></div>')

# active maintenance events (PG table; portal badge + invoicing input)
MAINT_TODAY = {}
try:
    for r in q("SELECT plant_key, category, coalesce(note,''),"
               " (end_ts IS NULL) FROM maintenance_event"
               " WHERE start_ts <= now()"
               " AND (end_ts IS NULL OR end_ts >= now() - interval '1 day');"):
        if len(r) >= 4:
            MAINT_TODAY.setdefault(r[0], []).append(
                {'category': r[1], 'note': r[2], 'ongoing': r[3] == 't'})
except RuntimeError:
    pass


# ------------------------------------------------------------- semaphore
def semaphore(pk):
    """(cls, label_en, label_es) for a plant RIGHT NOW (today's data)."""
    invs = LATEST.get(TODAY, {}).get(pk, [])
    in_window = WINDOW[0] <= NOW_MX.hour < WINDOW[1]
    if not invs:
        if in_window:
            return 'bad', 'no data today', 'sin datos hoy'
        return 'off', 'night — no data yet', 'noche — aún sin datos'
    fresh = [i for i in invs if i['age_min'] <= STALE_MIN]
    faults = [i for i in invs if i['status'] == 3]
    powered = [i for i in fresh if i['power_w'] is not None]
    zero_power = [i for i in powered if i['power_w'] < 10]
    conf_n = len(CONFIG_INV.get(pk, [])) or len(invs)
    silent = conf_n - len(invs)
    if in_window:
        if not fresh:
            return 'bad', f'stale > {STALE_MIN} min', f'sin señal > {STALE_MIN} min'
        if silent > 0:
            return 'warn', f'{silent}/{conf_n} inverter(s) SILENT today', f'{silent}/{conf_n} inversor(es) SIN DATOS hoy'
        if faults:
            return 'warn', f'{len(faults)} inverter(s) flag fault', f'{len(faults)} inversor(es) con falla'
        if len(fresh) < len(invs):
            return 'warn', f'{len(invs)-len(fresh)}/{len(invs)} inverters stale', f'{len(invs)-len(fresh)}/{len(invs)} inversores sin señal'
        if powered and len(zero_power) == len(powered) and NOW_MX.hour in range(9, 17):
            return 'warn', 'zero power midday', 'potencia cero a mediodía'
        return 'good', 'all reporting', 'todo reportando'
    return 'off', 'night', 'noche'


# ------------------------------------------------------------------ css
STYLE = '''
body{margin:0;font-family:"Segoe UI",system-ui,sans-serif;background:#f6f7f8;color:#202124;font-size:15px;}
.wrap{max-width:1180px;margin:0 auto;padding:24px 18px 48px;}
.top{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;}
h1{font-size:23px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#1c2733;margin:4px 0 0;}
.sub{color:#5f6368;font-size:13.5px;margin-top:4px;}
.logo{height:26px;width:auto;margin-top:2px;}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0;}
.btn{background:#fff;border:1px solid #dadce0;border-radius:8px;padding:6px 13px;font-size:13.5px;color:#202124;cursor:pointer;text-decoration:none;display:inline-block;}
.btn.primary{background:#1c2733;color:#fff;border-color:#1c2733;}
.banner{background:#fdf6e3;border:1px solid #f0e2b6;border-radius:8px;padding:10px 14px;font-size:13.5px;color:#6b5900;margin:10px 0;}
.banner.info{background:#eef3fb;border-color:#d3e0f2;color:#2a4f7c;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:14px;margin-top:8px;}
.tile{background:#fff;border:1px solid #e4e7ea;border-radius:12px;padding:14px 16px;text-decoration:none;color:inherit;display:block;position:relative;}
.tile.good{box-shadow:0 0 0 2px #e6f4ea, 0 0 18px 2px rgba(30,142,62,.25);}
.tile.warn{box-shadow:0 0 0 2px #fdf0dc, 0 0 18px 2px rgba(160,92,0,.3);}
.tile.bad{box-shadow:0 0 0 2px #fde7e9, 0 0 18px 2px rgba(197,34,49,.3);}
.tile.off{opacity:.75;}
.tname{font-weight:700;font-size:15.5px;color:#1c2733;}
.tkey{color:#80868b;font-size:12px;margin-left:6px;font-weight:400;}
.trow{display:flex;justify-content:space-between;margin-top:7px;font-size:13.5px;color:#5f6368;}
.trow b{color:#202124;font-weight:600;}
.pill{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;background:#e6f4ea;color:#137333;}
.pill.warn{background:#fdf0dc;color:#a05c00;}
.pill.bad{background:#fde7e9;color:#c5221f;}
.pill.off{background:#eceef0;color:#5f6368;}
a.statlink{text-decoration:none;}
a.statlink .pill:hover{outline:1px solid #1a73e8;cursor:pointer;}
.card{background:#fff;border:1px solid #e4e7ea;border-radius:10px;padding:16px 18px;margin:14px 0;overflow-x:auto;}
.card h2{font-size:14.5px;margin:0 0 10px;}
table{border-collapse:collapse;width:100%;font-size:13.5px;}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid #eceef0;white-space:nowrap;}
th{color:#5f6368;font-size:12px;}
td:first-child,th:first-child{text-align:left;}
.note{font-size:13px;color:#80868b;}
.st-PASS{color:#137333;font-weight:600;} .st-REVIEW{color:#a05c00;font-weight:600;}
.st-FAIL{color:#c5221f;font-weight:600;} .st-NO_DATA{color:#80868b;}
h2.sect{font-size:15px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#1c2733;margin:22px 0 4px;}
.kpis{display:flex;gap:26px;flex-wrap:wrap;margin:10px 0 2px;align-items:flex-end;}
.kpi .v{font-size:27px;font-weight:700;color:#1c2733;}
.kpi .l{font-size:12.5px;color:#5f6368;margin-top:2px;}
/* gauge tile: column, bottom-aligned so its caption sits on the same
   line as the other KPI labels (svg is display:block, no descender gap) */
.kpi.gauge{display:flex;flex-direction:column;align-items:center;align-self:flex-end;}
.kpi.gauge .l{margin-top:3px;text-align:center;}
input[type=date]{background:#fff;border:1px solid #dadce0;border-radius:8px;padding:5px 10px;font-size:13.5px;font-family:inherit;}
.tphoto{width:100%;height:88px;object-fit:cover;border-radius:10px;margin-bottom:8px;display:block;}
.pphoto{width:100%;max-height:190px;object-fit:cover;border-radius:14px;margin:10px 0 4px;display:block;}
.kgroup{display:flex;gap:26px;align-items:flex-end;padding-right:26px;}
.kgroup+.kgroup{border-left:1px solid #e3e6e9;padding-left:26px;}
.usermenu{margin-left:auto;position:relative;}
.whoami{background:#eef1f4;border:1px solid #dadce0;border-radius:20px;
 padding:5px 12px;font-size:13px;color:#1c2733;font-weight:600;
 text-decoration:none;cursor:pointer;font-family:inherit;}
.whoami:hover{border-color:#b9bec4;}
.whoami .wu{font-weight:400;color:#5f6368;}
.whoami .wa{margin-left:6px;padding:1px 7px;border-radius:9px;font-size:11px;
 font-weight:600;background:#ecebf6;color:#4f4a94;}
.whoami .car{margin-left:6px;color:#5f6368;font-size:10px;}
.umenu{display:none;position:absolute;right:0;top:calc(100% + 6px);z-index:50;
 min-width:210px;background:#fff;border:1px solid #e4e7ea;border-radius:10px;
 box-shadow:0 6px 24px rgba(0,0,0,.12);padding:6px;text-align:left;}
.umenu.open{display:block;}
.umenu a,.umenu button{display:block;width:100%;box-sizing:border-box;text-align:left;
 background:none;border:0;border-radius:7px;padding:8px 10px;font-size:13.5px;
 color:#202124;text-decoration:none;cursor:pointer;font-family:inherit;}
.umenu a:hover,.umenu button:hover{background:#f1f3f5;}
.umenu .umsep{border-top:1px solid #e4e7ea;margin:5px 2px;}
.umenu .umrow{display:flex;gap:6px;padding:4px 6px 2px;}
.umenu .umrow button{border:1px solid #dadce0;text-align:center;padding:5px 0;}
.umenu .umrow button.active{border-color:#2a78d6;box-shadow:inset 0 0 0 1px #2a78d6;}
.umenu .umlabel{font-size:11px;color:#80868b;padding:6px 10px 0;
 text-transform:uppercase;letter-spacing:.08em;}
.adminonly{display:none;}
@media print{.controls{display:none}body{background:#fff}}
'''

LANGJS = '''
function setLang(l){document.querySelectorAll('[data-en]').forEach(e=>{e.textContent=e.dataset[l]||e.dataset.en;});
document.querySelectorAll('.lang-btn').forEach(b=>b.classList.toggle('active',b.dataset.l===l));
try{localStorage.setItem('argia_lang',l);}catch(e){}}
function argiaMenu(ev){ev.stopPropagation();
 const m=document.getElementById('umenu'),b=document.getElementById('whoami');
 const open=m.classList.toggle('open');b.setAttribute('aria-expanded',open);}
document.addEventListener('click',()=>{
 const m=document.getElementById('umenu');
 if(m&&m.classList.contains('open')){m.classList.remove('open');
  document.getElementById('whoami').setAttribute('aria-expanded','false');}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){
 const m=document.getElementById('umenu');if(m)m.classList.remove('open');}});
window.addEventListener('DOMContentLoaded',()=>{let l='en';
try{l=localStorage.getItem('argia_lang')||'en';}catch(e){};setLang(l);});
// Name the signed-in account on every page. nginx re-sends cached
// basic-auth credentials silently, so without this you cannot tell
// which user the page in front of you actually belongs to.
window.addEventListener('DOMContentLoaded',()=>{
 const el=document.getElementById('whoami'); if(!el) return;
 fetch('/account/whoami',{credentials:'same-origin'})
  .then(r=>r.ok?r.json():null)
  .then(d=>{if(!d||!d.user){el.remove();return;}
   el.textContent=d.name;
   if(d.name!==d.user){const s=document.createElement('span');
    s.className='wu';s.textContent=' ('+d.user+')';el.appendChild(s);}
   if(d.admin){const a=document.createElement('span');
    a.className='wa';a.textContent='admin';el.appendChild(a);
    document.querySelectorAll('.adminonly')
     .forEach(x=>x.style.display='inline-flex');}
   const c=document.createElement('span');c.className='car';
   c.textContent='▾';el.appendChild(c);})
  .catch(()=>{el.remove();});});
// straight to the public page — a pre-flight fetch that returns 401
// makes the browser pop its own sign-in dialog (reported 2026-08-28)
function argiaLogout(){location.href='/logged-out.html';}
'''


def page(title, body, subtitle='', refresh=True):
    meta_r = '<meta http-equiv="refresh" content="300">' if refresh else ''
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">{meta_r}
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="shortcut icon" href="/favicon.ico">
<title>{esc(title)} — ARGIA Monitoring</title>
<style>{STYLE}</style></head><body><div class="wrap">
<div class="top"><div><h1>{esc(title)}</h1>
<div class="sub">{subtitle}</div></div>
<a href="/" title="ARGIA reports"><img class="logo" src="{LOGO_URI}" alt="ARGIA SOLAR"></a></div>
{body}
<p class="note" data-en="Generated {NOW_MX.strftime('%Y-%m-%d %H:%M')} MX from PostgreSQL telemetry on pio06.{' Live pages auto-refresh every 5 minutes.' if refresh else ' Archived day — static.'}"
 data-es="Generado {NOW_MX.strftime('%Y-%m-%d %H:%M')} MX desde PostgreSQL en pio06.{' Las páginas en vivo se actualizan cada 5 minutos.' if refresh else ' Día archivado — estático.'}">
Generated {NOW_MX.strftime('%Y-%m-%d %H:%M')} MX from PostgreSQL telemetry on pio06.</p>
<script>{LANGJS}</script></div></body></html>'''


def controls(extra=''):
    # "← Reports" LAST (v152): it leaves this portal, so it belongs
    # at the end of the row next to the person's own menu, not in
    # front of the views this page is actually made of.
    return (f'<div class="controls">'
            f'<a class="btn" href="{BASE}/" data-en="Fleet"'
            ' data-es="Flota">Fleet</a>'
            f'<a class="btn" href="{BASE}/ppa/" data-en="All PPA" data-es="Todo PPA">All PPA</a>'
            f'<a class="btn" href="{BASE}/capex/" data-en="CAPEX" data-es="CAPEX">CAPEX</a>'
            f'<a class="btn" href="{BASE}/performance/" data-en="Performance" data-es="Desempeño">Performance</a>'
            f'<a class="btn" href="{BASE}/recon/" data-en="Reconciliation" data-es="Conciliación">Reconciliation</a>'
            f'{extra}'
            '<a class="btn" href="/" data-en="← Reports"'
            ' data-es="← Reportes">← Reports</a>'
            # identity + language + log out, one menu (filled by whoami)
            '<div class="usermenu">'
            '<button class="whoami" id="whoami" onclick="argiaMenu(event)"'
            ' aria-haspopup="true" aria-expanded="false">…</button>'
            '<div class="umenu" id="umenu">'
            '<a href="/account/" data-en="My account" data-es="Mi cuenta">My account</a>'
            '<div class="umsep"></div>'
            '<div class="umlabel" data-en="Language" data-es="Idioma">Language</div>'
            '<div class="umrow">'
            '<button class="lang-btn" data-l="en" onclick="setLang(\'en\')">EN</button>'
            '<button class="lang-btn" data-l="es" onclick="setLang(\'es\')">ES</button>'
            '</div><div class="umsep"></div>'
            '<button onclick="argiaLogout()" data-en="Log out"'
            ' data-es="Cerrar sesión">Log out</button>'
            '</div></div>'
            '</div>')


def date_picker(pk, current):
    return (f'<input type="date" min="{FIRST_DATE}" max="{TODAY}" '
            f'value="{current}" onchange="location.href='
            f'(this.value==\'{TODAY}\')?\'{BASE}/{pk.lower()}/\''
            f':\'{BASE}/{pk.lower()}/d/\'+this.value+\'.html\'" '
            'title="5-minute data exists from '
            f'{FIRST_DATE} (server collection start)">')


def fmt_kwh(v, dash='—'):
    return dash if v is None else f'{v:,.1f}'


def fmt1(v, dash='—'):
    return dash if v is None else f'{v:,.1f}'


def fmt_kw(v, dash='—'):
    return dash if v is None else f'{v/1000.0:,.1f}'


GAUGE_CX, GAUGE_CY, GAUGE_R = 76.0, 74.0, 58.0
GAUGE_MAX = 130.0          # full sweep of the scale, in %


def gauge_svg(pct, lo=70, hi=90, width=132):
    """Semicircle meter: red<lo, amber lo-hi, green>hi, scale 0-130%.

    The value arc spans at most 180 degrees, so the SVG large-arc flag
    is ALWAYS 0. The old code used ``1 if frac > 0.5``, which made every
    gauge above 65% draw the MAJOR arc the long way round — the broken,
    clipped segments Tomasz reported on /ppa (108%) and /capex (69%) on
    2026-08-28. Regression-tested in tests/unit/test_gauge.py.

    Geometry is sized so the drawing fills the viewBox with no dead
    margin, and the svg is display:block so no inline-text descender
    gap shifts the caption out of line with the other KPI tiles.
    """
    import math
    color = '#c5221f' if pct < lo else ('#e8a13d' if pct < hi else '#1e8e3e')
    frac = max(0.0, min(pct / GAUGE_MAX, 1.0))
    cx, cy, r = GAUGE_CX, GAUGE_CY, GAUGE_R
    a1 = math.pi * (1 - frac)                 # 180 deg (empty) -> 0 (full)
    x1, y1 = cx + r * math.cos(a1), cy - r * math.sin(a1)
    val = (f'<path d="M{cx - r:.1f},{cy:.1f} A{r:.0f},{r:.0f} 0 0 1'
           f' {x1:.1f},{y1:.1f}" fill="none" stroke="{color}"'
           ' stroke-width="12" stroke-linecap="round"/>') if frac > 0.004 else ''
    # The colour-band legend lives in the tile's tooltip, not in the
    # svg: at this scale it rendered ~6px tall and clipped on both
    # edges, and being svg text it was never translated by the ES
    # toggle (which only rewrites data-en/data-es elements).
    return f'''<svg viewBox="0 0 152 84" style="width:{width}px;height:auto;display:block" role="img" aria-label="{pct:,.0f}% of expected ({GAUGE_MAX:.0f}% full scale)">
<path d="M{cx - r:.1f},{cy:.1f} A{r:.0f},{r:.0f} 0 0 1 {cx + r:.1f},{cy:.1f}" fill="none" stroke="#eceef0" stroke-width="12" stroke-linecap="round"/>
{val}
<text x="{cx:.0f}" y="{cy - 6:.0f}" text-anchor="middle" font-size="25" font-weight="700" fill="#1c2733">{pct:,.0f}%</text></svg>'''


GAUGE_TIP = ('&lt;70 % red · 70–90 % amber · &gt;90 % green'
             ' · rojo / ámbar / verde · full scale 130 %')


def completeness_banner(pk, d):
    """Honest data-quality note for a plant-day (item 8)."""
    comp, status = RECON_BY_PD.get((pk, d), (None, None))
    first_h = FIRST_TICK.get(d, {}).get(pk)
    msgs = []
    if d == TODAY and first_h is not None and first_h > 7:
        msgs.append((
            f"Intraday data today starts at ~{first_h:02d}:00 MX — 5-minute "
            "collection on this server began 2026-08-26 ~12:04 MX (Pi→server "
            "migration). Daily TOTALS are still complete: they come from the "
            "inverter's own cumulative counter.",
            f"Los datos intradía de hoy comienzan ~{first_h:02d}:00 MX — la "
            "colección cada 5 min en este servidor inició el 2026-08-26 "
            "~12:04 MX (migración Pi→servidor). Los TOTALES diarios están "
            "completos: provienen del contador acumulado del inversor."))
    elif comp is not None and comp < 95 and d != TODAY:
        msgs.append((
            f"Interval data for this day is {comp:.0f}% complete — the "
            "hourly bars may undercount. The daily total is vendor-counter "
            "verified" + (f" (reconciliation: {status})" if status else "") + ".",
            f"Los datos de intervalos de este día están al {comp:.0f}% — las "
            "barras horarias pueden subestimar. El total diario está "
            "verificado contra el contador del fabricante."))
    if d < FIRST_DATE:
        msgs.append((
            "No 5-minute data exists for this day (before server collection "
            "start); only the daily total is available.",
            "No existen datos de 5 minutos para este día; solo el total "
            "diario está disponible."))
    return ''.join(
        f'<div class="banner" data-en="{esc(en)}" data-es="{esc(es)}">{esc(en)}</div>'
        for en, es in msgs)


# ------------------------------------------------------- fleet overview
def plant_now(pk):
    invs = LATEST.get(TODAY, {}).get(pk, [])
    powers = [i['power_w'] for i in invs if i['power_w'] is not None]
    etodays = [i['etoday'] for i in invs if i['etoday'] is not None]
    power = sum(powers) / 1000.0 if powers else None
    etoday = sum(etodays) if etodays else None
    fresh = [i for i in invs if i['age_min'] <= STALE_MIN]
    total = len(CONFIG_INV.get(pk, [])) or len(invs)
    return power, etoday, len(fresh), total


def _tile(pk, meta):
    cls, len_, les = semaphore(pk)
    power, etoday, fresh, total = plant_now(pk)
    maint = ('<div class="trow"><span class="pill off" data-en="maintenance logged" '
             'data-es="mantenimiento registrado">maintenance logged</span></div>'
             if MAINT_TODAY.get(pk) else '')
    ph = photo_uri(pk, thumb=True)
    photo = (f'<img class="tphoto" src="{ph}" alt="" loading="lazy">'
             if ph else '')
    return power, etoday, f'''<a class="tile {cls}" href="{BASE}/{pk.lower()}/">
{photo}<div><span class="tname">{esc(meta['customer'])}</span><span class="tkey">{pk} · {meta['kwp']:,.0f} kWp</span></div>
<div class="trow"><span data-en="Power now" data-es="Potencia">Power now</span><b>{fmt1(power)} kW</b></div>
<div class="trow"><span data-en="Today" data-es="Hoy">Today</span><b>{fmt_kwh(etoday)} kWh</b></div>
<div class="trow"><span data-en="Inverters live" data-es="Inversores">Inverters live</span><b>{fresh}/{total}</b></div>
<div class="trow"><span class="pill {cls}" data-en="{esc(len_)}" data-es="{esc(les)}">{esc(len_)}</span></div>
{maint}</a>'''


def fleet_page():
    # PPA and CAPEX are different businesses and must never blend: each
    # portfolio gets its OWN summary row directly above its own grid
    # (user feedback 2026-08-26 + 2026-08-27). PPA is internal to Argia;
    # CAPEX plant pages carry client-grade access (see nginx vhost).
    sections = []
    for label_en, label_es, keys in (
            ('PPA plants', 'Plantas PPA',
             [k for k in sorted(PLANTS) if PLANTS[k]['portfolio'] == 'PPA']),
            ('CAPEX plants', 'Plantas CAPEX',
             [k for k in sorted(PLANTS) if PLANTS[k]['portfolio'] == 'CAPEX']),
            ('Other', 'Otras',
             [k for k in sorted(PLANTS)
              if PLANTS[k]['portfolio'] not in ('PPA', 'CAPEX')])):
        if not keys:
            continue
        grp = label_en.split()[0]
        tiles = []
        p_ = e_ = 0.0
        for pk in keys:
            power, etoday, tile = _tile(pk, PLANTS[pk])
            p_ += power or 0
            e_ += etoday or 0
            tiles.append(tile)
        kpis = f'''<div class="kpis">
<div class="kpi"><div class="v">{p_:,.1f} kW</div><div class="l" data-en="{grp} power now" data-es="Potencia {grp} ahora">{grp} power now</div></div>
<div class="kpi"><div class="v">{e_:,.0f} kWh</div><div class="l" data-en="{grp} energy today" data-es="Energía {grp} hoy">{grp} energy today</div></div>
<div class="kpi"><div class="v">{len(keys)}</div><div class="l" data-en="{grp} plants" data-es="Plantas {grp}">{grp} plants</div></div>
{perf_avail_tiles(keys, grp)}
</div>'''
        sections.append(
            f'<h2 class="sect" data-en="{label_en}" data-es="{label_es}">'
            f'{label_en}</h2>{kpis}<div class="grid">{"".join(tiles)}</div>')
    kpis = ''
    banner = ''
    ft = FIRST_TICK.get(TODAY, {})
    if ft and min(ft.values()) > 7:
        banner = ('<div class="banner" data-en="Intraday data today starts late: 5-minute collection moved from the Pi to this server on 2026-08-26 ~12:04 MX. Daily totals stay complete (inverter cumulative counters). From tomorrow charts cover the full day."'
                  ' data-es="Los datos intradía de hoy comienzan tarde: la colección de 5 min migró del Pi a este servidor el 2026-08-26 ~12:04 MX. Los totales diarios están completos (contadores acumulados). Desde mañana las gráficas cubren todo el día.">'
                  'Intraday data today starts late: 5-minute collection moved from the Pi to this server on 2026-08-26 ~12:04 MX. Daily totals stay complete (inverter cumulative counters). From tomorrow charts cover the full day.</div>')
    return page('Fleet Monitoring', controls() + banner + kpis + ''.join(sections),
                'Live O&amp;M view · 5-minute data · all vendors')


# ------------------------------------------------------- intraday chart
def _inv_color(i, n):
    n = max(n, 1)
    light = 26 + int(38 * (i / max(n - 1, 1))) if n > 1 else 38
    return f'hsl(152,42%,{light}%)'


def intraday_svg(pk, kwp, pr, d):
    invs = HOURLY.get(d, {}).get(pk, {})
    label_by_sn = {i['sn']: i['label'] for i in LATEST.get(d, {}).get(pk, [])}
    sns = sorted(invs, key=lambda s: label_by_sn.get(s, s))
    hours = list(range(6, 21))
    if not sns:
        return ('<p class="note" data-en="No 5-minute samples for this day." '
                'data-es="Sin muestras de 5 minutos para este día.">'
                'No 5-minute samples for this day.</p>')
    stack = {h: [invs[sn].get(h, 0.0) for sn in sns] for h in hours}
    totals = {h: sum(stack[h]) for h in hours}
    irr = IRR_H.get(d, {}).get(pk, {})
    theo = {h: irr[h] * kwp * pr / 1000.0 for h in hours if irr.get(h) is not None}
    vmax = max(list(totals.values()) + list(theo.values()) + [1.0]) * 1.12
    cloud = CLOUD_H.get(d, {}).get(pk, {})

    W, H, PL, PR_, PB, PT = 940, 260, 52, 46, 30, 14
    plot_w, plot_h = W - PL - PR_, H - PB - PT
    slot = plot_w / len(hours)
    x = lambda h: PL + (h - 6) * slot
    y = lambda v: PT + plot_h - (v / vmax) * plot_h
    yr = lambda p: PT + plot_h - (p / 100.0) * plot_h

    parts = []
    for gv in (0.25, 0.5, 0.75, 1.0):
        vy = y(vmax * gv / 1.12)
        parts.append(f'<line x1="{PL}" y1="{vy:.1f}" x2="{W-PR_}" y2="{vy:.1f}" stroke="#eceef0"/>'
                     f'<text x="{PL-6}" y="{vy+4:.1f}" text-anchor="end" font-size="11" fill="#80868b">{vmax*gv/1.12:,.0f}</text>')
    for gp in (0, 50, 100):
        parts.append(f'<text x="{W-PR_+6}" y="{yr(gp)+4:.1f}" font-size="11" fill="#b9bec4">{gp}</text>')
    bw = slot * 0.62
    for h in hours:
        x0 = x(h) + (slot - bw) / 2
        acc = 0.0
        for idx, v in enumerate(stack[h]):
            if v <= 0:
                continue
            y1_, y0_ = y(acc + v), y(acc)
            parts.append(f'<rect x="{x0:.1f}" y="{y1_:.1f}" width="{bw:.1f}" '
                         f'height="{max(y0_-y1_,0.5):.1f}" fill="{_inv_color(idx, len(sns))}"/>')
            acc += v
        parts.append(f'<text x="{x(h)+slot/2:.1f}" y="{H-10}" text-anchor="middle" font-size="11" fill="#80868b">{h:02d}</text>')
    cpts = [(x(h) + slot / 2, yr(cloud[h])) for h in hours if h in cloud]
    if len(cpts) >= 2:
        dd = ' '.join(f'{"M" if i == 0 else "L"}{px:.1f},{py:.1f}' for i, (px, py) in enumerate(cpts))
        parts.append(f'<path d="{dd}" fill="none" stroke="#9aa1a8" stroke-width="1.6" stroke-dasharray="5 4"/>')
    tpts = [(x(h) + slot / 2, y(theo[h])) for h in sorted(theo)]
    if len(tpts) >= 2:
        dd = ' '.join(f'{"M" if i == 0 else "L"}{px:.1f},{py:.1f}' for i, (px, py) in enumerate(tpts))
        parts.append(f'<path d="{dd}" fill="none" stroke="#1c2733" stroke-width="1.8" stroke-dasharray="7 4"/>')
    svg = f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">' + ''.join(parts) + '</svg>'
    leg = ['<span class="note" style="margin-right:14px"><span style="color:#9aa1a8">╌╌</span> <span data-en="Cloud cover % (right)" data-es="Nubosidad % (der.)">Cloud cover % (right)</span></span>']
    if tpts:
        leg.append('<span class="note" style="margin-right:14px"><span style="color:#1c2733">╌╌</span> <span data-en="Theoretical (irr × kWp × PR)" data-es="Teórico (irr × kWp × PR)">Theoretical (irr × kWp × PR)</span></span>')
    for idx, sn in enumerate(sns):
        leg.append(f'<span class="note" style="margin-right:12px"><span style="display:inline-block;width:10px;height:10px;background:{_inv_color(idx, len(sns))};border-radius:2px"></span> {esc(label_by_sn.get(sn, sn))}</span>')
    return svg + '<div style="margin-top:6px">' + ''.join(leg) + '</div>'


# ----------------------------------------------------------- plant page
def plant_page(pk, d):
    """One plant, one day. d == TODAY -> live page with live KPIs."""
    meta = PLANTS[pk]
    live = d == TODAY
    invs = LATEST.get(d, {}).get(pk, [])
    etodays = [i['etoday'] for i in invs if i['etoday'] is not None]
    day_kwh = sum(etodays) if etodays else None
    vend = VENDOR_DAY.get((pk, d))
    stored = next((e for dd, e, _x in DAILY.get(pk, []) if dd == d), None)

    inv_cells = []
    portal = VENDOR_PORTAL.get(str(meta.get('brand', '')).upper())
    for i in sorted(invs, key=lambda v: v['label']):
        stale = live and i['age_min'] > STALE_MIN
        fault = i['status'] == 3
        pill_cls = 'bad' if stale else ('warn' if fault else 'good')
        pill_txt = ('stale' if stale else ((i['fault'] or 'fault') if fault else 'OK'))
        # Reason column: the catalog's human explanation; normal
        # operating states stay quiet, unknown codes say so honestly.
        detail = ''
        if i['fault'] and (fault or not is_normal_state(meta['brand'],
                                                        i['fault'])):
            detail = explain_fault(meta['brand'], i['fault']) or ''
        temp = '—' if i['temp'] is None else f"{i['temp']:.0f}"
        pill_html = f'<span class="pill {pill_cls}">{esc(pill_txt)}</span>'
        if portal:
            pill_html = (f'<a class="statlink" href="{esc(portal)}"'
                         f' target="_blank" rel="noopener"'
                         f' title="Open {esc(meta["brand"])} portal">'
                         f'{pill_html}</a>')
        inv_cells.append(
            f'<tr><td>{esc(i["label"])}</td>'
            f'<td>{pill_html}'
            f'{(" <span class=note>" + esc(detail) + "</span>") if detail else ""}</td>'
            f'<td>{fmt_kw(i["power_w"])}</td><td>{fmt_kwh(i["etoday"])}</td>'
            f'<td>{temp}</td><td>{esc(i["last_mx"])}</td></tr>')
    # configured inverters that never answered this day — the dead ones
    seen_sns = {i['sn'] for i in invs}
    for sn, label, rated in CONFIG_INV.get(pk, []):
        if sn in seen_sns:
            continue
        silent_pill = ('<span class="pill bad"'
                       ' data-en="SILENT — no data this day"'
                       ' data-es="SIN DATOS este día">'
                       'SILENT — no data this day</span>')
        if portal:
            silent_pill = (f'<a class="statlink" href="{esc(portal)}"'
                           f' target="_blank" rel="noopener"'
                           f' title="Open {esc(meta["brand"])} portal">'
                           f'{silent_pill}</a>')
        inv_cells.append(
            f'<tr><td>{esc(label)}</td>'
            f'<td>{silent_pill}'
            ' <span class=note data-en="configured active'
            f' ({(rated or 0):,.0f} kW) but never reported" data-es="configurado'
            ' activo pero sin reportar">configured active'
            f' ({(rated or 0):,.0f} kW) but never reported</span></td>'
            '<td>—</td><td>—</td><td>—</td><td>—</td></tr>')
    recon_rows = ''.join(
        f'<tr><td>{esc(r[0])}</td><td>{fmt_kwh(f(r[1]))}</td>'
        f'<td>{fmt_kwh(f(r[2]))}</td><td>{fmt_kwh(f(r[3]))}</td>'
        f'<td>{"—" if f(r[4]) is None else f"{f(r[4]):.0f}%"}</td>'
        f'<td>{"—" if f(r[5]) is None else f"{f(r[5]):+.2f}%"}</td>'
        f'<td class="st-{esc(r[6])}">{esc(r[6])}</td></tr>'
        for r in RECON_D.get(pk, [])[:7])
    daily_rows = ''.join(
        f'<tr><td><a href="{BASE + "/" + pk.lower() + "/" if dd == TODAY else f"{BASE}/{pk.lower()}/d/{dd}.html"}">{esc(dd)}</a></td>'
        f'<td>{fmt_kwh(e)}</td><td>{fmt_kwh(xx)}</td>'
        f'<td>{"—" if not e or not xx else f"{100*e/xx:,.0f}%"}</td></tr>'
        for dd, e, xx in reversed(DAILY.get(pk, [])[-7:]))

    maint = MAINT_TODAY.get(pk, [])
    maint_html = ''.join(
        f'<div class="banner info" data-en="Maintenance logged ({esc(m["category"])}{", ongoing" if m["ongoing"] else ""}): {esc(m["note"] or "no note")} — approved customer events become deemed energy in invoicing."'
        f' data-es="Mantenimiento registrado ({esc(m["category"])}): {esc(m["note"] or "sin nota")} — los eventos de cliente aprobados se facturan como energía compensada.">'
        f'Maintenance logged ({esc(m["category"])}): {esc(m["note"] or "no note")}</div>'
        for m in maint) if live else ''

    if live:
        powers = [i['power_w'] for i in invs if i['power_w'] is not None]
        power = sum(powers) / 1000.0 if powers else None
        fresh = len([i for i in invs if i['age_min'] <= STALE_MIN])
        conf_total = len(CONFIG_INV.get(pk, [])) or len(invs)
        cls, len_, les = semaphore(pk)
        kpis = f'''<div class="kpis">
<div class="kpi"><div class="v">{fmt1(power)} kW</div><div class="l" data-en="Power now" data-es="Potencia ahora">Power now</div></div>
<div class="kpi"><div class="v">{fmt_kwh(day_kwh)} kWh</div><div class="l" data-en="Energy today (interval)" data-es="Energía hoy (intervalos)">Energy today (interval)</div></div>
<div class="kpi"><div class="v">{fmt_kwh(vend)} kWh</div><div class="l" data-en="Vendor counter (last snapshot)" data-es="Contador del fabricante">Vendor counter (last snapshot)</div></div>
<div class="kpi"><div class="v">{fresh}/{conf_total}</div><div class="l" data-en="Inverters live / configured" data-es="Inversores en línea / configurados">Inverters live / configured</div></div>
{perf_avail_tiles([pk])}
<div class="kpi"><div class="v"><span class="pill {cls}" data-en="{esc(len_)}" data-es="{esc(les)}">{esc(len_)}</span></div><div class="l">Status</div></div>
</div>'''
        sub = f'{pk} · {meta["kwp"]:,.1f} kWp · {esc(meta["brand"])} · live monitoring'
    else:
        kpis = f'''<div class="kpis">
<div class="kpi"><div class="v">{fmt_kwh(stored if stored is not None else day_kwh)} kWh</div><div class="l" data-en="Energy this day" data-es="Energía del día">Energy this day</div></div>
<div class="kpi"><div class="v">{fmt_kwh(vend)} kWh</div><div class="l" data-en="Vendor daily counter" data-es="Contador diario del fabricante">Vendor daily counter</div></div>
<div class="kpi"><div class="v">{len(invs)}</div><div class="l" data-en="Inverters with data" data-es="Inversores con datos">Inverters with data</div></div>
</div>'''
        sub = f'{pk} · {meta["kwp"]:,.1f} kWp · {esc(meta["brand"])} · {d} (archived day)'

    ref = REF_LINKS.get(pk)
    ref_btn = (f'<a class="btn" href="{ref}" target="_blank" rel="noopener" '
               'title="Project reference on argia.com.mx" '
               'data-en="⧉ Reference ↗" data-es="⧉ Referencia ↗">'
               '⧉ Reference ↗</a>') if ref else ''
    ph = photo_uri(pk)
    photo = (f'<img class="pphoto" src="{ph}" alt="{esc(meta["customer"])}"'
             ' loading="lazy">') if ph and live else ''
    body = controls(
        date_picker(pk, d)
        + (f'<a class="btn" href="{BASE}/{pk.lower()}/" data-en="Live" data-es="En vivo">Live</a>' if not live else '')
        + ref_btn
        + f'<a class="btn primary" href="{REPORT_BASE}/{pk.lower()}/" '
          'data-en="Open report ↗" data-es="Abrir reporte ↗">Open report ↗</a>')
    body += photo + maint_html + completeness_banner(pk, d) + kpis
    body += f'''
<div class="card"><h2 data-en="Intraday production · 60-min buckets · kWh per inverter" data-es="Producción intradía · bloques de 60 min · kWh por inversor">Intraday production · 60-min buckets · kWh per inverter</h2>{intraday_svg(pk, meta['kwp'], meta['pr'], d)}</div>
<div class="card"><h2 data-en="Inverters — {'latest sample' if live else 'last sample of the day'}" data-es="Inversores — última muestra">Inverters — latest sample</h2>
<table><tr><th data-en="Inverter" data-es="Inversor">Inverter</th><th>Status</th><th data-en="Power kW" data-es="Potencia kW">Power kW</th><th>EDay kWh</th><th>°C</th><th data-en="Time MX" data-es="Hora MX">Time MX</th></tr>{''.join(inv_cells)}</table>
<p class="note" data-en="Status comes from the inverter's own status flag; raw vendor state strings are shown as detail. A full fault-code catalog (human explanations per vendor code) arrives with the alerting phase."
 data-es="El estado proviene de la bandera del propio inversor; las cadenas de estado se muestran como detalle. El catálogo de códigos de falla llega con la fase de alertas.">Status comes from the inverter's own status flag.</p></div>
<div class="card"><h2 data-en="Last 7 days — production (click a date)" data-es="Últimos 7 días — producción (clic en la fecha)">Last 7 days — production (click a date)</h2>
<table><tr><th data-en="Date" data-es="Fecha">Date</th><th>kWh</th><th data-en="Expected" data-es="Esperado">Expected</th><th>%</th></tr>{daily_rows}</table></div>
<div class="card"><h2 data-en="Daily reconciliation — interval vs vendor counter" data-es="Conciliación diaria — intervalos vs contador">Daily reconciliation — interval vs vendor counter</h2>
<table><tr><th data-en="Date" data-es="Fecha">Date</th><th data-en="Interval" data-es="Intervalos">Interval</th><th data-en="Vendor" data-es="Fabricante">Vendor</th><th>KPI</th><th data-en="Compl." data-es="Compl.">Compl.</th><th>Δ%</th><th>Status</th></tr>{recon_rows}</table>
<p class="note" data-en="The vendor cumulative counter is the billing control; interval data is analytics. A gap in our collection can never shrink an invoice."
 data-es="El contador acumulado del fabricante es el control de facturación; los intervalos son analítica.">The vendor cumulative counter is the billing control; interval data is analytics.</p></div>'''
    return page(meta['customer'], body, sub, refresh=live)


# ------------------------------------------------------------- PPA page
def ppa_page():
    ppa = [k for k in sorted(PLANTS) if PLANTS[k]['portfolio'] == 'PPA']
    rows = []
    tot_p = tot_e = tot_kwp = 0.0
    mtd_prod = 0.0
    mtd_exp = 0.0
    exp_known = True
    for pk in ppa:
        meta = PLANTS[pk]
        power, etoday, fresh, total = plant_now(pk)
        cls, len_, les = semaphore(pk)
        tot_p += power or 0
        tot_e += etoday or 0
        tot_kwp += meta['kwp']
        m = MTD.get(pk, {})
        mtd_prod += m.get('prod', 0.0)
        if m.get('exp') is None:
            exp_known = False
        else:
            mtd_exp += m['exp']
        m_exp = m.get('exp')
        m_pct = '—' if not m_exp else f"{100*m['prod']/m_exp:,.0f}%"
        exp_str = '—' if m_exp is None else f'{m_exp:,.0f}'
        rows.append(
            f'<tr><td><a href="{BASE}/{pk.lower()}/">{esc(meta["customer"])}</a> '
            f'<span class="tkey">{pk}</span></td>'
            f'<td>{meta["kwp"]:,.0f}</td><td>{fmt1(power)}</td>'
            f'<td>{fmt_kwh(etoday)}</td>'
            f'<td>{m.get("prod", 0):,.0f}</td>'
            f'<td>{exp_str}</td>'
            f'<td>{m_pct}</td><td>{fresh}/{total}</td>'
            f'<td><span class="pill {cls}">{esc(len_)}</span></td></tr>')
    pct = (100.0 * mtd_prod / mtd_exp) if mtd_exp else None
    rows.append(
        f'<tr style="font-weight:700;background:#fafbfc"><td>TOTAL</td>'
        f'<td>{tot_kwp:,.0f}</td><td>{tot_p:,.1f}</td><td>{tot_e:,.0f}</td>'
        f'<td>{mtd_prod:,.0f}</td><td>{mtd_exp:,.0f}</td>'
        f'<td>{"—" if pct is None else f"{pct:,.0f}%"}</td><td></td><td></td></tr>')
    kpis = f'''<div class="kpis">
<div class="kpi"><div class="v">{tot_p:,.1f} kW</div><div class="l" data-en="PPA power right now" data-es="Potencia PPA ahora">PPA power right now</div></div>
<div class="kpi"><div class="v">{tot_e:,.0f} kWh</div><div class="l" data-en="PPA energy today" data-es="Energía PPA hoy">PPA energy today</div></div>
<div class="kpi"><div class="v">{mtd_prod:,.0f} kWh</div><div class="l" data-en="Production — month to date" data-es="Producción — mes en curso">Production — month to date</div></div>
<div class="kpi"><div class="v">{mtd_exp:,.0f} kWh</div><div class="l" data-en="Expected — month to date{'' if exp_known else ' (partial)'}" data-es="Esperado — mes en curso">Expected — month to date</div></div>
<div class="kpi gauge" title="{GAUGE_TIP}">{gauge_svg(pct) if pct is not None else ''}<div class="l" data-en="Production vs expected (MTD)" data-es="Producción vs esperado (MTD)">Production vs expected (MTD)</div></div>
</div>'''
    note = ('<p class="note" data-en="Month-to-date sums come from vendor-counter-verified daily production. Expected is the sum of daily expected values where the KPI pipeline computed them; days without an expected value are excluded from the ratio."'
            ' data-es="Los acumulados del mes provienen de producción diaria verificada contra el contador del fabricante. El esperado suma los valores diarios disponibles.">'
            'Month-to-date sums come from vendor-counter-verified daily production.</p>')
    body = controls() + kpis + (
        '<div class="card"><h2 data-en="All PPA plants — live + month to date" data-es="Plantas PPA — en vivo + mes en curso">All PPA plants — live + month to date</h2>'
        '<table><tr><th data-en="Plant" data-es="Planta">Plant</th><th>kWp</th>'
        '<th data-en="Power kW" data-es="Potencia kW">Power kW</th>'
        '<th data-en="Today kWh" data-es="Hoy kWh">Today kWh</th>'
        '<th data-en="MTD kWh" data-es="Mes kWh">MTD kWh</th>'
        '<th data-en="MTD expected" data-es="Mes esperado">MTD expected</th>'
        '<th data-en="vs exp." data-es="vs esp.">vs exp.</th>'
        '<th data-en="Inverters" data-es="Inversores">Inverters</th><th>Status</th></tr>'
        + ''.join(rows) + '</table>' + note + '</div>')
    return page('PPA Portfolio — Live', body,
                'All PPA plants · live + cumulative month view')


def capex_page():
    """CAPEX mirror of the PPA page. Deliberately a SEPARATE page:
    CAPEX belongs to the customers and may be shared with them; PPA is
    Argia's own business and stays internal (user rule 2026-08-27)."""
    capex = [k for k in sorted(PLANTS) if PLANTS[k]['portfolio'] == 'CAPEX']
    rows = []
    tot_p = tot_e = tot_kwp = 0.0
    mtd_prod = mtd_exp = 0.0
    for pk in capex:
        meta = PLANTS[pk]
        power, etoday, fresh, total = plant_now(pk)
        cls, len_, les = semaphore(pk)
        tot_p += power or 0
        tot_e += etoday or 0
        tot_kwp += meta['kwp']
        m = MTD.get(pk, {})
        mtd_prod += m.get('prod', 0.0)
        m_exp = m.get('exp')
        if m_exp is not None:
            mtd_exp += m_exp
        m_pct = '—' if not m_exp else f"{100*m.get('prod', 0)/m_exp:,.0f}%"
        rows.append(
            f'<tr><td><a href="{BASE}/{pk.lower()}/">{esc(meta["customer"])}</a> '
            f'<span class="tkey">{pk}</span></td>'
            f'<td>{meta["kwp"]:,.0f}</td><td>{fmt1(power)}</td>'
            f'<td>{fmt_kwh(etoday)}</td>'
            f'<td>{m.get("prod", 0):,.0f}</td>'
            f'<td>{"—" if m_exp is None else f"{m_exp:,.0f}"}</td>'
            f'<td>{m_pct}</td><td>{fresh}/{total}</td>'
            f'<td><span class="pill {cls}">{esc(len_)}</span></td></tr>')
    pct = (100.0 * mtd_prod / mtd_exp) if mtd_exp else None
    rows.append(
        f'<tr style="font-weight:700;background:#fafbfc"><td>TOTAL</td>'
        f'<td>{tot_kwp:,.0f}</td><td>{tot_p:,.1f}</td><td>{tot_e:,.0f}</td>'
        f'<td>{mtd_prod:,.0f}</td><td>{mtd_exp:,.0f}</td>'
        f'<td>{"—" if pct is None else f"{pct:,.0f}%"}</td><td></td><td></td></tr>')
    kpis = f'''<div class="kpis">
<div class="kpi"><div class="v">{tot_p:,.1f} kW</div><div class="l" data-en="CAPEX power right now" data-es="Potencia CAPEX ahora">CAPEX power right now</div></div>
<div class="kpi"><div class="v">{tot_e:,.0f} kWh</div><div class="l" data-en="CAPEX energy today" data-es="Energía CAPEX hoy">CAPEX energy today</div></div>
<div class="kpi"><div class="v">{mtd_prod:,.0f} kWh</div><div class="l" data-en="Production — month to date" data-es="Producción — mes en curso">Production — month to date</div></div>
<div class="kpi"><div class="v">{mtd_exp:,.0f} kWh</div><div class="l" data-en="Expected — month to date" data-es="Esperado — mes en curso">Expected — month to date</div></div>
<div class="kpi gauge" title="{GAUGE_TIP}">{gauge_svg(pct) if pct is not None else ''}<div class="l" data-en="Production vs expected (MTD)" data-es="Producción vs esperado (MTD)">Production vs expected (MTD)</div></div>
</div>'''
    note = ('<p class="note" data-en="CAPEX plants belong to their owners; these figures are the owners\' production. Month-to-date sums come from vendor-counter-verified daily production."'
            ' data-es="Las plantas CAPEX pertenecen a sus dueños; estas cifras son su producción. Los acumulados del mes provienen de producción diaria verificada contra el contador del fabricante.">'
            'CAPEX plants belong to their owners; these figures are the owners\' production.</p>')
    body = controls() + kpis + (
        '<div class="card"><h2 data-en="CAPEX plants — live + month to date" data-es="Plantas CAPEX — en vivo + mes en curso">CAPEX plants — live + month to date</h2>'
        '<table><tr><th data-en="Plant" data-es="Planta">Plant</th><th>kWp</th>'
        '<th data-en="Power kW" data-es="Potencia kW">Power kW</th>'
        '<th data-en="Today kWh" data-es="Hoy kWh">Today kWh</th>'
        '<th data-en="MTD kWh" data-es="Mes kWh">MTD kWh</th>'
        '<th data-en="MTD expected" data-es="Mes esperado">MTD expected</th>'
        '<th data-en="vs exp." data-es="vs esp.">vs exp.</th>'
        '<th data-en="Inverters" data-es="Inversores">Inverters</th><th>Status</th></tr>'
        + ''.join(rows) + '</table>' + note + '</div>')
    return page('CAPEX Portfolio — Live', body,
                'All CAPEX plants · live + cumulative month view')


def pr_sparkline(pk, w=140, h=30):
    pts = [v for _d, v in PR_TREND.get(pk, [])][-30:]
    if len(pts) < 2:
        return '<span class="note">—</span>'
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 0.01
    step = w / (len(pts) - 1)
    d = ' '.join(f'{"M" if i == 0 else "L"}{i*step:.1f},'
                 f'{h - 3 - (v - lo) / rng * (h - 6):.1f}'
                 for i, v in enumerate(pts))
    return (f'<svg viewBox="0 0 {w} {h}" style="width:{w}px;height:{h}px;'
            f'vertical-align:middle"><path d="{d}" fill="none" '
            'stroke="#1e8e3e" stroke-width="1.6"/></svg>')


def performance_page():
    rows = []
    # fleet summary: production/expected are straight sums; PR, PR_STC
    # and availability are kWp-weighted means (a 155 kWp plant must not
    # pull the fleet number as hard as an 818 kWp one)
    t_kwp = t_prod = t_exp = 0.0
    w_pr = w_prstc = w_av = 0.0
    kwp_pr = kwp_prstc = kwp_av = 0.0
    for section, keys in (
            ('PPA', [k for k in sorted(PLANTS)
                     if PLANTS[k]['portfolio'] == 'PPA']),
            ('CAPEX', [k for k in sorted(PLANTS)
                       if PLANTS[k]['portfolio'] == 'CAPEX'])):
        if keys:
            rows.append(f'<tr><td colspan="9" style="font-weight:700;'
                        f'background:#fafbfc">{section}</td></tr>')
        for pk in keys:
            meta = PLANTS[pk]
            p = PERF.get(pk, {})
            pr = p.get('pr')
            av = p.get('avail')
            prod, exp = p.get('prod'), p.get('exp')
            ratio = ('—' if not prod or not exp
                     else f'{100*prod/exp:,.0f}%')
            pr_cls = ('' if pr is None else
                      (' class="st-PASS"' if pr >= 0.75 else
                       (' class="st-REVIEW"' if pr >= 0.65
                        else ' class="st-FAIL"')))
            av_cls = ('' if av is None else
                      (' class="st-PASS"' if av >= 0.98 else
                       (' class="st-REVIEW"' if av >= 0.95
                        else ' class="st-FAIL"')))
            prstc = p.get('pr_stc')
            kwp = meta['kwp']
            t_kwp += kwp
            t_prod += prod or 0
            t_exp += exp or 0
            if pr is not None:
                w_pr += pr * kwp; kwp_pr += kwp
            if prstc is not None:
                w_prstc += prstc * kwp; kwp_prstc += kwp
            if av is not None:
                w_av += av * kwp; kwp_av += kwp
            rows.append(
                f'<tr><td><a href="{BASE}/{pk.lower()}/">{esc(meta["customer"])}'
                f'</a> <span class="tkey">{pk}</span></td>'
                f'<td>{meta["kwp"]:,.0f}</td>'
                f'<td{pr_cls}>{"—" if pr is None else f"{pr:.3f}"}</td>'
                f'<td>{"—" if prstc is None else f"{prstc:.3f}"}</td>'
                f'<td>{pr_sparkline(pk)}</td>'
                f'<td{av_cls}>{"—" if av is None else f"{100*av:,.1f}%"}</td>'
                f'<td>{"—" if prod is None else f"{prod:,.0f}"}</td>'
                f'<td>{"—" if exp is None else f"{exp:,.0f}"}</td>'
                f'<td>{ratio}</td></tr>')
    t_ratio = '—' if not t_prod or not t_exp else f'{100*t_prod/t_exp:,.0f}%'
    rows.append(
        '<tr style="font-weight:700;background:#fafbfc">'
        '<td data-en="FLEET TOTAL / kWp-weighted avg"'
        ' data-es="TOTAL FLOTA / prom. ponderado por kWp">'
        'FLEET TOTAL / kWp-weighted avg</td>'
        f'<td>{t_kwp:,.0f}</td>'
        f'<td>{"—" if not kwp_pr else f"{w_pr/kwp_pr:.3f}"}</td>'
        f'<td>{"—" if not kwp_prstc else f"{w_prstc/kwp_prstc:.3f}"}</td>'
        '<td></td>'
        f'<td>{"—" if not kwp_av else f"{100*w_av/kwp_av:,.1f}%"}</td>'
        f'<td>{t_prod:,.0f}</td><td>{t_exp:,.0f}</td>'
        f'<td>{t_ratio}</td></tr>')
    body = controls() + f'''
<div class="card"><h2 data-en="Performance — last 30 days" data-es="Desempeño — últimos 30 días">Performance — last 30 days</h2>
<table><tr><th data-en="Plant" data-es="Planta">Plant</th><th>kWp</th>
<th data-en="Avg PR" data-es="PR prom.">Avg PR</th>
<th title="temperature-corrected to 25°C cells" data-en="PR_STC" data-es="PR_STC">PR_STC</th>
<th data-en="PR trend (30d)" data-es="Tendencia PR (30d)">PR trend (30d)</th>
<th data-en="Availability" data-es="Disponibilidad">Availability</th>
<th data-en="Production kWh" data-es="Producción kWh">Production kWh</th>
<th data-en="Expected kWh" data-es="Esperado kWh">Expected kWh</th>
<th data-en="vs exp." data-es="vs esp.">vs exp.</th></tr>
{''.join(rows)}</table>
<p class="note" data-en="PR and availability come from the daily KPI pipeline (vendor-counter-verified energy). PR_STC is temperature-corrected to 25°C cells (AGS-701 / IEC 61724-3) using measured, irradiance-weighted module temperature — only computed where a sensor exists, never estimated. Bands: PR green ≥0.75, amber 0.65–0.75; availability green ≥98% (IEC 63019), amber 95–98%. Next: degradation vs the ≤0.4%/yr warranty."
 data-es="PR y disponibilidad provienen del pipeline diario de KPI. PR_STC está corregido a células de 25°C (AGS-701 / IEC 61724-3) con temperatura de módulo medida y ponderada por irradiancia — solo donde hay sensor, nunca estimado. Bandas: PR verde ≥0.75, ámbar 0.65–0.75; disponibilidad verde ≥98% (IEC 63019). Sigue: degradación vs garantía ≤0.4%/año.">
PR and availability come from the daily KPI pipeline.</p></div>'''
    return page('Performance', body,
                '30-day PR · availability · production vs expected')


def recon_page():
    d_rows = []
    for pk in sorted(RECON_D):
        for r in RECON_D[pk][:5]:
            d_rows.append(
                f'<tr><td>{esc(r[0])}</td><td>{pk}</td>'
                f'<td>{fmt_kwh(f(r[1]))}</td><td>{fmt_kwh(f(r[2]))}</td>'
                f'<td>{fmt_kwh(f(r[3]))}</td>'
                f'<td>{"—" if f(r[4]) is None else f"{f(r[4]):.0f}%"}</td>'
                f'<td>{"—" if f(r[5]) is None else f"{f(r[5]):+.2f}%"}</td>'
                f'<td class="st-{esc(r[6])}">{esc(r[6])}</td>'
                f'<td class="note">{esc(r[7][:70])}</td></tr>')
    m_rows = ''.join(
        f'<tr><td>{esc(m)}</td><td>{esc(pk)}</td><td>{fmt_kwh(f(bill))}</td>'
        f'<td>{esc(basis)}</td><td class="st-{esc(st)}">{esc(st)}</td>'
        f'<td>{esc(closed) or "<span class=note>open</span>"}</td>'
        f'<td>{"<span class=pill>invoice unlocked</span>" if closed else "<span class=pill off>locked until close</span>"}</td>'
        f'<td class="note">{esc(note[:60])}</td></tr>'
        for pk, m, bill, basis, st, closed, note in RECON_M)
    body = controls() + f'''
<div class="card"><h2 data-en="Monthly close — the invoice gate" data-es="Cierre mensual — la puerta de facturación">Monthly close — the invoice gate</h2>
<table><tr><th data-en="Month" data-es="Mes">Month</th><th data-en="Plant" data-es="Planta">Plant</th><th data-en="Billing kWh" data-es="kWh facturables">Billing kWh</th><th data-en="Basis" data-es="Base">Basis</th><th>Status</th><th data-en="Closed by" data-es="Cerrado por">Closed by</th><th data-en="Invoice annex" data-es="Anexo de factura">Invoice annex</th><th data-en="Note" data-es="Nota">Note</th></tr>
{m_rows or '<tr><td colspan="8" class="note" data-en="No monthly close yet. The first close (August) runs automatically on Sep 1 at 06:10 MX; each plant-month then appears here with its billing kWh, and the invoice annex unlocks for closed months." data-es="Aún no hay cierre mensual. El primero (agosto) corre el 1 de septiembre a las 06:10 MX; cada planta-mes aparecerá aquí con sus kWh facturables, y el anexo de factura se desbloquea para meses cerrados.">No monthly close yet. The first close (August) runs automatically on Sep 1 at 06:10 MX.</td></tr>'}</table>
<p class="note" data-en="A PASS month closes automatically; REVIEW/FAIL wait for a manual close. Approved customer-maintenance events add deemed energy to billing. The annex generator connects here once the first month is closed."
 data-es="Un mes PASS cierra automáticamente; REVIEW/FAIL esperan cierre manual. Los eventos de mantenimiento de cliente aprobados agregan energía compensada.">
A PASS month closes automatically; REVIEW/FAIL wait for a manual close.</p></div>
<div class="card"><h2 data-en="Daily reconciliation — last days, all plants" data-es="Conciliación diaria — últimos días, todas las plantas">Daily reconciliation — last days, all plants</h2>
<table><tr><th data-en="Date" data-es="Fecha">Date</th><th data-en="Plant" data-es="Planta">Plant</th><th data-en="Interval" data-es="Intervalos">Interval</th><th data-en="Vendor" data-es="Fabricante">Vendor</th><th>KPI</th><th>Compl.</th><th>Δ%</th><th>Status</th><th data-en="Note" data-es="Nota">Note</th></tr>
{''.join(d_rows)}</table></div>'''
    return page('Reconciliation', body,
                'Four-check energy reconciliation · vendor counters are the billing control')


# ----------------------------------------------------------------- write
def write(rel, content):
    p = os.path.join(OUTROOT, 'monitoring', rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write(content)
    os.chmod(p, 0o644)


n = 0
write('index.html', fleet_page()); n += 1
write('ppa/index.html', ppa_page()); n += 1
write('capex/index.html', capex_page()); n += 1
write('performance/index.html', performance_page()); n += 1
write('recon/index.html', recon_page()); n += 1
for pk in PLANTS:
    write(f'{pk.lower()}/index.html', plant_page(pk, TODAY)); n += 1
    for d in DATES:
        if d != TODAY:
            write(f'{pk.lower()}/d/{d}.html', plant_page(pk, d)); n += 1
print(f'monitoring_gen: wrote {n} pages under {OUTROOT}/monitoring '
      f'({len(DATES)} day(s) of 5-min data since {FIRST_DATE})')
