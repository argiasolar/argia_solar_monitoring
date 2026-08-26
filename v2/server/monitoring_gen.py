#!/usr/bin/env python3
"""Generate the ARGIA live monitoring portal — monitoring.argia.com.mx.

Static regeneration from PostgreSQL every 5 minutes (systemd timer, right
after the telemetry tick). No client-side data fetching: each page embeds
its data, nginx serves it under the argia-area basic auth.

Pages:
  /monitoring/                fleet overview (live tiles, semaphores)
  /monitoring/<key>/          per-plant: power curve, inverter table,
                              7-day reconciliation, links to the report
  /monitoring/ppa/            all-PPA combined view
  /monitoring/recon/          reconciliation board (daily 14d + monthly)

Run:  python3 monitoring_gen.py [outroot]
Deploy: lives in the repo (v2/server/); the server copies it into
/opt/argia/bundle/ next to argia_logo.py after each pull.
"""
import datetime as dt
import html
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from argia_logo import LOGO_URI

OUTROOT = sys.argv[1] if len(sys.argv) > 1 else '/www/hosting/monitoring.argia.com.mx/www'
MX = ZoneInfo('America/Mexico_City')
STALE_MIN = 30          # no tick for this long inside the window -> red
WINDOW = (6, 20)        # MX production hours for the semaphore

REPORT_BASE = 'https://report.argia.com.mx'


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

# ---------------------------------------------------------------- data
PLANTS = {}
for r in q("SELECT plant_key, customer, brand, kwp_dc, coalesce(portfolio,''),"
           " active, pr_baseline FROM plant ORDER BY plant_key;"):
    if len(r) >= 7 and r[5] == 't':
        PLANTS[r[0]] = {'customer': r[1], 'brand': r[2], 'kwp': f(r[3]) or 0,
                        'portfolio': r[4], 'pr': f(r[6]) or 0.80}

# Latest USABLE sample per inverter today. Vendors (Huawei especially)
# intermittently answer with an empty data map — a tick with neither
# power nor energy is a data gap, not "the plant stopped": we show the
# last real sample and let its age drive the staleness pill instead.
LATEST = {}
for r in q("SELECT DISTINCT ON (plant_key, inverter_sn) plant_key,"
           " inverter_sn, coalesce(inverter_label, inverter_sn), status,"
           " power_w, etoday_kwh, temperature_c, coalesce(fault_code::text,''),"
           " to_char(ts_utc AT TIME ZONE 'America/Mexico_City', 'HH24:MI'),"
           " extract(epoch FROM now() - ts_utc)/60"
           " FROM telemetry"
           " WHERE (ts_utc AT TIME ZONE 'America/Mexico_City')::date"
           f" = DATE '{TODAY}'"
           " AND (etoday_kwh IS NOT NULL OR power_w IS NOT NULL)"
           " ORDER BY plant_key, inverter_sn, ts_utc DESC;"):
    if len(r) >= 10:
        LATEST.setdefault(r[0], []).append({
            'sn': r[1], 'label': r[2], 'status': int(f(r[3]) or 0),
            'power_w': f(r[4]), 'etoday': f(r[5]), 'temp': f(r[6]),
            'fault': r[7] if r[7] not in ('', '0') else '',
            'last_mx': r[8], 'age_min': f(r[9]) or 9e9})

# 5-min plant power series today (kW)
SERIES = {}
for r in q("SELECT plant_key,"
           " to_char(date_trunc('minute', ts_utc AT TIME ZONE"
           " 'America/Mexico_City'), 'HH24:MI'),"
           " sum(power_w)/1000.0 FROM telemetry"
           " WHERE (ts_utc AT TIME ZONE 'America/Mexico_City')::date"
           f" = DATE '{TODAY}'"
           " GROUP BY 1, 2 ORDER BY 1, 2;"):
    if len(r) >= 3:
        SERIES.setdefault(r[0], []).append((r[1], f(r[2]) or 0.0))

# per-inverter cumulative etoday per hour -> hourly energy (stacked bars)
_HOUR_MAX = {}
for r in q("SELECT plant_key, inverter_sn,"
           " extract(hour FROM ts_utc AT TIME ZONE"
           " 'America/Mexico_City')::int, max(etoday_kwh)"
           " FROM telemetry"
           " WHERE (ts_utc AT TIME ZONE 'America/Mexico_City')::date"
           f" = DATE '{TODAY}' AND etoday_kwh IS NOT NULL"
           " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;"):
    if len(r) >= 4:
        _HOUR_MAX.setdefault(r[0], {}).setdefault(r[1], {})[int(r[2])] = f(r[3])

HOURLY = {}   # plant -> inverter_sn -> {hour: kwh produced in that hour}
for pk, invs in _HOUR_MAX.items():
    for sn, by_h in invs.items():
        prev = None
        out = {}
        for h in sorted(by_h):
            v = by_h[h]
            if v is None:
                continue
            out[h] = max(0.0, v - prev) if prev is not None else v
            prev = v
        HOURLY.setdefault(pk, {})[sn] = out

# hourly weather context: cloud cover % + irradiance W/m2 (where sensed)
CLOUD_H, IRR_H = {}, {}
for r in q("SELECT plant_key,"
           " extract(hour FROM ts_utc AT TIME ZONE"
           " 'America/Mexico_City')::int,"
           " avg(cloud_cover_pct), avg(irradiance_wm2) FROM telemetry"
           " WHERE (ts_utc AT TIME ZONE 'America/Mexico_City')::date"
           f" = DATE '{TODAY}' GROUP BY 1, 2;"):
    if len(r) >= 4:
        h = int(r[1])
        if f(r[2]) is not None:
            CLOUD_H.setdefault(r[0], {})[h] = f(r[2])
        if f(r[3]) is not None:
            IRR_H.setdefault(r[0], {})[h] = f(r[3])

# vendor day counter (today's snapshot, if tonight's ran) for cross-check
VENDOR_DAY = {r[0]: f(r[1]) for r in q(
    "SELECT plant_key, daily_kwh FROM vendor_counter_snapshot"
    f" WHERE snap_date = DATE '{TODAY}';") if len(r) >= 2}

# daily production, last 14 days (bars on plant page)
DAILY = {}
for r in q("SELECT plant_key, prod_date::text, energy_kwh, expected_kwh"
           " FROM daily_production"
           f" WHERE prod_date >= DATE '{TODAY}' - 14 ORDER BY 1, 2;"):
    if len(r) >= 4:
        DAILY.setdefault(r[0], []).append(
            (r[1], f(r[2]), f(r[3])))

# reconciliation
RECON_D = {}
for r in q("SELECT plant_key, prod_date::text, interval_kwh,"
           " vendor_daily_kwh, kpi_kwh, completeness_pct, variance_pct,"
           " status, coalesce(note,'') FROM reconciliation_daily"
           f" WHERE prod_date >= DATE '{TODAY}' - 14 ORDER BY 2 DESC, 1;"):
    if len(r) >= 9:
        RECON_D.setdefault(r[0], []).append(r[1:])

RECON_M = [r for r in q(
    "SELECT plant_key, to_char(ref_month,'YYYY-MM'), billing_kwh,"
    " billing_basis, status, coalesce(closed_by,''),"
    " coalesce(note,'') FROM reconciliation_monthly"
    " ORDER BY ref_month DESC, plant_key LIMIT 60;") if len(r) >= 7]


# ------------------------------------------------------------- semaphore
def semaphore(pk):
    """(cls, label_en, label_es) for a plant right now."""
    invs = LATEST.get(pk, [])
    in_window = WINDOW[0] <= NOW_MX.hour < WINDOW[1]
    if not invs:
        if in_window:
            return 'bad', 'no data today', 'sin datos hoy'
        return 'off', 'night — no data yet', 'noche — aún sin datos'
    fresh = [i for i in invs if i['age_min'] <= STALE_MIN]
    # A fault is the vendor's own status flag (3) — NOT the raw state
    # string (Huawei's "IS=512,RS=1" is normal grid-connected operation).
    faults = [i for i in invs if i['status'] == 3]
    powered = [i for i in fresh if i['power_w'] is not None]
    zero_power = [i for i in powered if i['power_w'] < 10]
    if in_window:
        if not fresh:
            return 'bad', f'stale > {STALE_MIN} min', f'sin señal > {STALE_MIN} min'
        if faults:
            return 'warn', f'{len(faults)} inverter(s) flag fault', f'{len(faults)} inversor(es) con falla'
        if len(fresh) < len(invs):
            return 'warn', f'{len(invs)-len(fresh)}/{len(invs)} inverters stale', f'{len(invs)-len(fresh)}/{len(invs)} inversores sin señal'
        if powered and len(zero_power) == len(powered) and NOW_MX.hour in range(9, 17):
            return 'warn', 'zero power midday', 'potencia cero a mediodía'
        return 'good', 'all reporting', 'todo reportando'
    return 'off', 'night', 'noche'


# ------------------------------------------------------------------ css
STYLE = f'''
body{{margin:0;font-family:"Segoe UI",system-ui,sans-serif;background:#f6f7f8;color:#202124;font-size:15px;}}
.wrap{{max-width:1180px;margin:0 auto;padding:24px 18px 48px;}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;}}
h1{{font-size:23px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#1c2733;margin:4px 0 0;}}
.sub{{color:#5f6368;font-size:13.5px;margin-top:4px;}}
.logo{{height:26px;width:auto;margin-top:2px;}}
.controls{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0;}}
.btn{{background:#fff;border:1px solid #dadce0;border-radius:8px;padding:6px 13px;font-size:13.5px;color:#202124;cursor:pointer;text-decoration:none;display:inline-block;}}
.btn.primary{{background:#1c2733;color:#fff;border-color:#1c2733;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:14px;margin-top:8px;}}
.tile{{background:#fff;border:1px solid #e4e7ea;border-radius:12px;padding:14px 16px;text-decoration:none;color:inherit;display:block;position:relative;}}
.tile.good{{box-shadow:0 0 0 2px #e6f4ea, 0 0 18px 2px rgba(30,142,62,.25);}}
.tile.warn{{box-shadow:0 0 0 2px #fdf0dc, 0 0 18px 2px rgba(160,92,0,.3);}}
.tile.bad{{box-shadow:0 0 0 2px #fde7e9, 0 0 18px 2px rgba(197,34,49,.3);}}
.tile.off{{opacity:.75;}}
.tname{{font-weight:700;font-size:15.5px;color:#1c2733;}}
.tkey{{color:#80868b;font-size:12px;margin-left:6px;font-weight:400;}}
.trow{{display:flex;justify-content:space-between;margin-top:7px;font-size:13.5px;color:#5f6368;}}
.trow b{{color:#202124;font-weight:600;}}
.pill{{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;background:#e6f4ea;color:#137333;}}
.pill.warn{{background:#fdf0dc;color:#a05c00;}}
.pill.bad{{background:#fde7e9;color:#c5221f;}}
.pill.off{{background:#eceef0;color:#5f6368;}}
.card{{background:#fff;border:1px solid #e4e7ea;border-radius:10px;padding:16px 18px;margin:14px 0;overflow-x:auto;}}
.card h2{{font-size:14.5px;margin:0 0 10px;}}
table{{border-collapse:collapse;width:100%;font-size:13.5px;}}
th,td{{text-align:right;padding:6px 8px;border-bottom:1px solid #eceef0;white-space:nowrap;}}
th{{color:#5f6368;font-size:12px;}}
td:first-child,th:first-child{{text-align:left;}}
.note{{font-size:13px;color:#80868b;}}
.st-PASS{{color:#137333;font-weight:600;}} .st-REVIEW{{color:#a05c00;font-weight:600;}}
.st-FAIL{{color:#c5221f;font-weight:600;}} .st-NO_DATA{{color:#80868b;}}
h2.sect{{font-size:15px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#1c2733;margin:22px 0 4px;}}
.kpis{{display:flex;gap:26px;flex-wrap:wrap;margin:10px 0 2px;}}
.kpi .v{{font-size:27px;font-weight:700;color:#1c2733;}}
.kpi .l{{font-size:12.5px;color:#5f6368;margin-top:2px;}}
@media print{{.controls{{display:none}}body{{background:#fff}}}}
'''

LANGJS = '''
function setLang(l){document.querySelectorAll('[data-en]').forEach(e=>{e.textContent=e.dataset[l]||e.dataset.en;});
try{localStorage.setItem('argia_lang',l);}catch(e){}}
window.addEventListener('DOMContentLoaded',()=>{let l='en';
try{l=localStorage.getItem('argia_lang')||'en';}catch(e){};setLang(l);});
'''


def page(title, body, subtitle=''):
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta http-equiv="refresh" content="300">
<title>{esc(title)} — ARGIA Monitoring</title>
<style>{STYLE}</style></head><body><div class="wrap">
<div class="top"><div><h1>{esc(title)}</h1>
<div class="sub">{subtitle}</div></div>
<img class="logo" src="{LOGO_URI}" alt="ARGIA SOLAR"></div>
{body}
<p class="note" data-en="Auto-refreshes every 5 minutes. Generated {NOW_MX.strftime('%Y-%m-%d %H:%M')} MX from PostgreSQL telemetry on pio06."
 data-es="Se actualiza cada 5 minutos. Generado {NOW_MX.strftime('%Y-%m-%d %H:%M')} MX desde telemetría PostgreSQL en pio06.">
Auto-refreshes every 5 minutes. Generated {NOW_MX.strftime('%Y-%m-%d %H:%M')} MX from PostgreSQL telemetry on pio06.</p>
<script>{LANGJS}</script></div></body></html>'''


def controls(extra=''):
    return (f'<div class="controls"><a class="btn" href="/" data-en="Fleet"'
            ' data-es="Flota">Fleet</a>'
            '<a class="btn" href="/ppa/" data-en="All PPA" data-es="Todo PPA">All PPA</a>'
            '<a class="btn" href="/recon/" data-en="Reconciliation" data-es="Conciliación">Reconciliation</a>'
            f'<a class="btn" href="{REPORT_BASE}/" data-en="Reports ↗" data-es="Reportes ↗">Reports ↗</a>'
            '<button class="btn" onclick="setLang(\'en\')">EN</button>'
            '<button class="btn" onclick="setLang(\'es\')">ES</button>'
            f'{extra}</div>')


def fmt_kwh(v, dash='—'):
    return dash if v is None else f'{v:,.1f}'


def fmt1(v, dash='—'):
    return dash if v is None else f'{v:,.1f}'


def fmt_kw(v, dash='—'):
    return dash if v is None else f'{v/1000.0:,.1f}'


# ------------------------------------------------------- fleet overview
def plant_now(pk):
    invs = LATEST.get(pk, [])
    powers = [i['power_w'] for i in invs if i['power_w'] is not None]
    etodays = [i['etoday'] for i in invs if i['etoday'] is not None]
    power = sum(powers) / 1000.0 if powers else None
    etoday = sum(etodays) if etodays else None
    fresh = [i for i in invs if i['age_min'] <= STALE_MIN]
    return power, etoday, len(fresh), len(invs)


def _tile(pk, meta):
    cls, len_, les = semaphore(pk)
    power, etoday, fresh, total = plant_now(pk)
    return power, etoday, f'''<a class="tile {cls}" href="/{pk.lower()}/">
<div><span class="tname">{esc(meta['customer'])}</span><span class="tkey">{pk} · {meta['kwp']:,.0f} kWp</span></div>
<div class="trow"><span data-en="Power now" data-es="Potencia">Power now</span><b>{fmt1(power)} kW</b></div>
<div class="trow"><span data-en="Today" data-es="Hoy">Today</span><b>{fmt_kwh(etoday)} kWh</b></div>
<div class="trow"><span data-en="Inverters live" data-es="Inversores">Inverters live</span><b>{fresh}/{total}</b></div>
<div class="trow"><span class="pill {cls}" data-en="{esc(len_)}" data-es="{esc(les)}">{esc(len_)}</span></div>
</a>'''


def fleet_page():
    tot_p = tot_e = 0.0
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
        tiles = []
        for pk in keys:
            power, etoday, tile = _tile(pk, PLANTS[pk])
            tot_p += power or 0
            tot_e += etoday or 0
            tiles.append(tile)
        sections.append(
            f'<h2 class="sect" data-en="{label_en}" data-es="{label_es}">'
            f'{label_en}</h2><div class="grid">{"".join(tiles)}</div>')
    kpis = f'''<div class="kpis">
<div class="kpi"><div class="v">{tot_p:,.1f} kW</div><div class="l" data-en="Fleet power right now" data-es="Potencia de flota ahora">Fleet power right now</div></div>
<div class="kpi"><div class="v">{tot_e:,.0f} kWh</div><div class="l" data-en="Fleet energy today" data-es="Energía de flota hoy">Fleet energy today</div></div>
<div class="kpi"><div class="v">{len(PLANTS)}</div><div class="l" data-en="Active plants" data-es="Plantas activas">Active plants</div></div>
</div>'''
    return page('Fleet Monitoring', controls() + kpis + ''.join(sections),
                'Live O&amp;M view · 5-minute data · all vendors')


# ------------------------------------- intraday chart (like v2 dashboard)
def _inv_color(i, n):
    """Green ramp, dark -> light, one shade per inverter."""
    n = max(n, 1)
    light = 26 + int(38 * (i / max(n - 1, 1))) if n > 1 else 38
    return f'hsl(152,42%,{light}%)'


def intraday_svg(pk, kwp, pr):
    """60-min stacked production per inverter + cloud cover % (right axis)
    + theoretical line (irradiance x kWp x PR) where a sensor exists."""
    invs = HOURLY.get(pk, {})
    label_by_sn = {i['sn']: i['label'] for i in LATEST.get(pk, [])}
    sns = sorted(invs, key=lambda s: label_by_sn.get(s, s))
    hours = list(range(6, 21))
    if not sns:
        return ('<p class="note" data-en="No production samples today yet." '
                'data-es="Aún no hay muestras de producción hoy.">'
                'No production samples today yet.</p>')
    stack = {h: [invs[sn].get(h, 0.0) for sn in sns] for h in hours}
    totals = {h: sum(stack[h]) for h in hours}
    theo = {h: (IRR_H.get(pk, {}).get(h) or 0) * kwp * pr / 1000.0
            for h in hours if IRR_H.get(pk, {}).get(h) is not None}
    vmax = max(list(totals.values()) + list(theo.values()) + [1.0]) * 1.12
    cloud = CLOUD_H.get(pk, {})

    W, H, PL, PR_, PB, PT = 940, 260, 52, 46, 30, 14
    plot_w, plot_h = W - PL - PR_, H - PB - PT
    slot = plot_w / len(hours)

    def x(h):
        return PL + (h - 6) * slot

    def y(v):
        return PT + plot_h - (v / vmax) * plot_h

    def yr(pct):
        return PT + plot_h - (pct / 100.0) * plot_h

    parts = []
    for gv in (0.25, 0.5, 0.75, 1.0):
        vy = y(vmax * gv / 1.12)
        parts.append(f'<line x1="{PL}" y1="{vy:.1f}" x2="{W-PR_}" y2="{vy:.1f}" stroke="#eceef0"/>'
                     f'<text x="{PL-6}" y="{vy+4:.1f}" text-anchor="end" font-size="11" fill="#80868b">{vmax*gv/1.12:,.0f}</text>')
    for gp in (0, 50, 100):
        parts.append(f'<text x="{W-PR_+6}" y="{yr(gp)+4:.1f}" font-size="11" fill="#b9bec4">{gp}</text>')
    # stacked bars
    bw = slot * 0.62
    for h in hours:
        x0 = x(h) + (slot - bw) / 2
        acc = 0.0
        for idx, v in enumerate(stack[h]):
            if v <= 0:
                continue
            y1 = y(acc + v)
            y0 = y(acc)
            parts.append(f'<rect x="{x0:.1f}" y="{y1:.1f}" width="{bw:.1f}" '
                         f'height="{max(y0-y1,0.5):.1f}" fill="{_inv_color(idx, len(sns))}"/>')
            acc += v
        parts.append(f'<text x="{x(h)+slot/2:.1f}" y="{H-10}" text-anchor="middle" font-size="11" fill="#80868b">{h:02d}</text>')
    # cloud cover dashed line, right axis
    cpts = [(x(h) + slot / 2, yr(cloud[h])) for h in hours if h in cloud]
    if len(cpts) >= 2:
        d = ' '.join(f'{"M" if i == 0 else "L"}{px:.1f},{py:.1f}'
                     for i, (px, py) in enumerate(cpts))
        parts.append(f'<path d="{d}" fill="none" stroke="#9aa1a8" stroke-width="1.6" stroke-dasharray="5 4"/>')
    # theoretical line (only where irradiance was sensed)
    tpts = [(x(h) + slot / 2, y(theo[h])) for h in sorted(theo)]
    if len(tpts) >= 2:
        d = ' '.join(f'{"M" if i == 0 else "L"}{px:.1f},{py:.1f}'
                     for i, (px, py) in enumerate(tpts))
        parts.append(f'<path d="{d}" fill="none" stroke="#1c2733" stroke-width="1.8" stroke-dasharray="7 4"/>')
    svg = (f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">'
           + ''.join(parts) + '</svg>')
    leg = ['<span class="note" style="margin-right:14px">'
           '<span style="color:#9aa1a8">╌╌</span> '
           '<span data-en="Cloud cover % (right)" data-es="Nubosidad % (der.)">Cloud cover % (right)</span></span>']
    if tpts:
        leg.append('<span class="note" style="margin-right:14px">'
                   '<span style="color:#1c2733">╌╌</span> '
                   '<span data-en="Theoretical (irr × kWp × PR)" data-es="Teórico (irr × kWp × PR)">Theoretical (irr × kWp × PR)</span></span>')
    for idx, sn in enumerate(sns):
        leg.append(f'<span class="note" style="margin-right:12px">'
                   f'<span style="display:inline-block;width:10px;height:10px;'
                   f'background:{_inv_color(idx, len(sns))};border-radius:2px"></span> '
                   f'{esc(label_by_sn.get(sn, sn))}</span>')
    return svg + '<div style="margin-top:6px">' + ''.join(leg) + '</div>'


# ---------------------------------------------------------- power curve
def power_svg(pk, kwp):
    pts = SERIES.get(pk, [])
    W, H, PL, PB = 900, 220, 46, 24
    if not pts:
        return '<p class="note" data-en="No power samples today yet." data-es="Aún no hay muestras de potencia hoy.">No power samples today yet.</p>'
    vmax = max(max(v for _, v in pts), kwp * 0.2, 1.0)
    vmax *= 1.08

    def x(hhmm):
        h, m = int(hhmm[:2]), int(hhmm[3:5])
        frac = (h * 60 + m - 5 * 60) / (17 * 60)   # 05:00..22:00
        return PL + max(0.0, min(1.0, frac)) * (W - PL - 10)

    def y(v):
        return H - PB - (v / vmax) * (H - PB - 16)

    path = ' '.join(f'{"M" if i == 0 else "L"}{x(t):.1f},{y(v):.1f}'
                    for i, (t, v) in enumerate(pts))
    gridlines = []
    for gv in (0.25, 0.5, 0.75, 1.0):
        vy = y(vmax * gv / 1.08)
        gridlines.append(f'<line x1="{PL}" y1="{vy:.1f}" x2="{W-10}" y2="{vy:.1f}" stroke="#eceef0"/>'
                         f'<text x="{PL-6}" y="{vy+4:.1f}" text-anchor="end" font-size="11" fill="#80868b">{vmax*gv/1.08:,.0f}</text>')
    hours = ''.join(f'<text x="{x(f"{h:02d}:00"):.1f}" y="{H-6}" text-anchor="middle" font-size="11" fill="#80868b">{h:02d}</text>'
                    for h in range(6, 22, 2))
    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">'
            + ''.join(gridlines) + hours +
            f'<path d="{path}" fill="none" stroke="#1c2733" stroke-width="2"/></svg>')


def plant_page(pk):
    meta = PLANTS[pk]
    cls, len_, les = semaphore(pk)
    power, etoday, fresh, total = plant_now(pk)
    vend = VENDOR_DAY.get(pk)
    inv_cells = []
    for i in sorted(LATEST.get(pk, []), key=lambda v: v['label']):
        stale = i['age_min'] > STALE_MIN
        # fault = the vendor's normalized status flag; the raw state
        # string is shown as detail, not treated as an alarm by itself.
        fault = i['status'] == 3
        pill_cls = 'bad' if stale else ('warn' if fault else 'good')
        pill_txt = ('stale' if stale else
                    ((i['fault'] or 'fault') if fault else 'OK'))
        detail = i['fault'] if (i['fault'] and not fault) else ''
        temp = '—' if i['temp'] is None else f"{i['temp']:.0f}"
        inv_cells.append(
            f'<tr><td>{esc(i["label"])}</td>'
            f'<td><span class="pill {pill_cls}">{esc(pill_txt)}</span>'
            f'{(" <span class=note>" + esc(detail) + "</span>") if detail else ""}</td>'
            f'<td>{fmt_kw(i["power_w"])}</td><td>{fmt_kwh(i["etoday"])}</td>'
            f'<td>{temp}</td><td>{esc(i["last_mx"])}</td></tr>')
    inv_rows = ''.join(inv_cells)
    recon_rows = ''.join(
        f'<tr><td>{esc(r[0])}</td><td>{fmt_kwh(f(r[1]))}</td>'
        f'<td>{fmt_kwh(f(r[2]))}</td><td>{fmt_kwh(f(r[3]))}</td>'
        f'<td>{"—" if f(r[4]) is None else f"{f(r[4]):.0f}%"}</td>'
        f'<td>{"—" if f(r[5]) is None else f"{f(r[5]):+.2f}%"}</td>'
        f'<td class="st-{esc(r[6])}">{esc(r[6])}</td></tr>'
        for r in RECON_D.get(pk, [])[:7])
    daily_rows = ''.join(
        f'<tr><td>{esc(d)}</td><td>{fmt_kwh(e)}</td><td>{fmt_kwh(x)}</td>'
        f'<td>{"—" if not e or not x else f"{100*e/x:,.0f}%"}</td></tr>'
        for d, e, x in reversed(DAILY.get(pk, [])[-7:]))
    body = controls(
        f'<a class="btn primary" href="{REPORT_BASE}/{pk.lower()}/" '
        'data-en="Open report ↗" data-es="Abrir reporte ↗">Open report ↗</a>')
    body += f'''<div class="kpis">
<div class="kpi"><div class="v">{fmt1(power)} kW</div><div class="l" data-en="Power now" data-es="Potencia ahora">Power now</div></div>
<div class="kpi"><div class="v">{fmt_kwh(etoday)} kWh</div><div class="l" data-en="Energy today (interval)" data-es="Energía hoy (intervalos)">Energy today (interval)</div></div>
<div class="kpi"><div class="v">{fmt_kwh(vend)} kWh</div><div class="l" data-en="Vendor counter (last snapshot)" data-es="Contador del fabricante">Vendor counter (last snapshot)</div></div>
<div class="kpi"><div class="v">{fresh}/{total}</div><div class="l" data-en="Inverters live" data-es="Inversores en línea">Inverters live</div></div>
<div class="kpi"><div class="v"><span class="pill {cls}" data-en="{esc(len_)}" data-es="{esc(les)}">{esc(len_)}</span></div><div class="l">Status</div></div>
</div>
<div class="card"><h2 data-en="Intraday production · 60-min buckets · kWh per inverter" data-es="Producción intradía · bloques de 60 min · kWh por inversor">Intraday production · 60-min buckets · kWh per inverter</h2>{intraday_svg(pk, meta['kwp'], meta['pr'])}</div>
<div class="card"><h2 data-en="Power today (kW, 5-min)" data-es="Potencia hoy (kW, 5 min)">Power today (kW, 5-min)</h2>{power_svg(pk, meta['kwp'])}</div>
<div class="card"><h2 data-en="Inverters — latest sample" data-es="Inversores — última muestra">Inverters — latest sample</h2>
<table><tr><th data-en="Inverter" data-es="Inversor">Inverter</th><th>Status</th><th data-en="Power kW" data-es="Potencia kW">Power kW</th><th>EToday kWh</th><th>°C</th><th data-en="Last seen MX" data-es="Última señal MX">Last seen MX</th></tr>{inv_rows}</table></div>
<div class="card"><h2 data-en="Last 7 days — production" data-es="Últimos 7 días — producción">Last 7 days — production</h2>
<table><tr><th data-en="Date" data-es="Fecha">Date</th><th>kWh</th><th data-en="Expected" data-es="Esperado">Expected</th><th>%</th></tr>{daily_rows}</table></div>
<div class="card"><h2 data-en="Daily reconciliation — interval vs vendor counter" data-es="Conciliación diaria — intervalos vs contador">Daily reconciliation — interval vs vendor counter</h2>
<table><tr><th data-en="Date" data-es="Fecha">Date</th><th data-en="Interval" data-es="Intervalos">Interval</th><th data-en="Vendor" data-es="Fabricante">Vendor</th><th>KPI</th><th data-en="Compl." data-es="Compl.">Compl.</th><th>Δ%</th><th>Status</th></tr>{recon_rows}</table>
<p class="note" data-en="The vendor cumulative counter is the billing control; interval data is analytics. A gap in our collection can never shrink an invoice."
 data-es="El contador acumulado del fabricante es el control de facturación; los intervalos son analítica. Un hueco en nuestra colección nunca reduce una factura.">
The vendor cumulative counter is the billing control; interval data is analytics. A gap in our collection can never shrink an invoice.</p></div>'''
    return page(f'{meta["customer"]}', body,
                f'{pk} · {meta["kwp"]:,.1f} kWp · {esc(meta["brand"])} · live monitoring')


# ----------------------------------------------------------- PPA + recon
def ppa_page():
    rows = []
    tot_p = tot_e = tot_kwp = 0.0
    for pk, meta in sorted(PLANTS.items()):
        if meta['portfolio'] != 'PPA':
            continue
        power, etoday, fresh, total = plant_now(pk)
        cls, len_, les = semaphore(pk)
        tot_p += power or 0
        tot_e += etoday or 0
        tot_kwp += meta['kwp']
        rows.append(f'<tr><td><a href="/{pk.lower()}/">{esc(meta["customer"])}</a> <span class="tkey">{pk}</span></td>'
                    f'<td>{meta["kwp"]:,.0f}</td><td>{fmt1(power)}</td>'
                    f'<td>{fmt_kwh(etoday)}</td><td>{fresh}/{total}</td>'
                    f'<td><span class="pill {cls}">{esc(len_)}</span></td></tr>')
    rows.append(f'<tr style="font-weight:700;background:#fafbfc"><td>TOTAL</td><td>{tot_kwp:,.0f}</td>'
                f'<td>{tot_p:,.1f}</td><td>{tot_e:,.0f}</td><td></td><td></td></tr>')
    body = controls() + ('<div class="card"><h2 data-en="All PPA plants — live" data-es="Todas las plantas PPA — en vivo">All PPA plants — live</h2>'
                         '<table><tr><th data-en="Plant" data-es="Planta">Plant</th><th>kWp</th>'
                         '<th data-en="Power kW" data-es="Potencia kW">Power kW</th>'
                         '<th data-en="Today kWh" data-es="Hoy kWh">Today kWh</th>'
                         '<th data-en="Inverters" data-es="Inversores">Inverters</th><th>Status</th></tr>'
                         + ''.join(rows) + '</table></div>')
    return page('PPA Portfolio — Live', body, 'All PPA plants, one screen')


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
        f'<td class="note">{esc(note[:70])}</td></tr>'
        for pk, m, bill, basis, st, closed, note in RECON_M)
    body = controls() + f'''
<div class="card"><h2 data-en="Monthly close — the invoice gate" data-es="Cierre mensual — la puerta de facturación">Monthly close — the invoice gate</h2>
<table><tr><th data-en="Month" data-es="Mes">Month</th><th data-en="Plant" data-es="Planta">Plant</th><th data-en="Billing kWh" data-es="kWh facturables">Billing kWh</th><th data-en="Basis" data-es="Base">Basis</th><th>Status</th><th data-en="Closed by" data-es="Cerrado por">Closed by</th><th data-en="Note" data-es="Nota">Note</th></tr>
{m_rows or '<tr><td colspan="7" class="note" data-en="No months closed yet — the first close runs on the 1st at 06:10 MX." data-es="Aún no hay cierres — el primero corre el día 1 a las 06:10 MX.">No months closed yet — the first close runs on the 1st at 06:10 MX.</td></tr>'}</table>
<p class="note" data-en="An invoice annex can only be generated for a month whose reconciliation is closed (PASS auto-closes; REVIEW/FAIL need a manual close)."
 data-es="El anexo de factura solo se genera para un mes con conciliación cerrada (PASS cierra solo; REVIEW/FAIL requieren cierre manual).">
An invoice annex can only be generated for a month whose reconciliation is closed (PASS auto-closes; REVIEW/FAIL need a manual close).</p></div>
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
    return p


n = 0
write('index.html', fleet_page()); n += 1
write('ppa/index.html', ppa_page()); n += 1
write('recon/index.html', recon_page()); n += 1
for pk in PLANTS:
    write(f'{pk.lower()}/index.html', plant_page(pk)); n += 1
print(f'monitoring_gen: wrote {n} pages under {OUTROOT}/monitoring '
      f'({len(LATEST)} plants with data today)')
