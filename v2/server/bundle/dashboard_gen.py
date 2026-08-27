#!/usr/bin/env python3
"""Generate the ARGIA financial/production report page from PostgreSQL.

Runs on pio06:  python3 /opt/argia/dashboard_gen.py
Writes:         /opt/argia/www/index.html  (served by nginx, basic-auth)

Static, self-contained (inline SVG, no external assets). Colors follow the
validated reference dataviz palette; light + dark via prefers-color-scheme.
"""
import subprocess
import datetime as dt
import html

OUT = '/www/hosting/monitoring.argia.com.mx/www/index.html'
DB = 'argia_mont'


def q(sql):
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', DB,
                        '-t', '-A', '-F', '\t', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    return [line.split('\t') for line in r.stdout.strip().splitlines() if line.strip()]


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------- data ----------------
asof = q("SELECT max(prod_date) FROM daily_production;")[0][0]
first = q("SELECT min(prod_date) FROM daily_production;")[0][0]

plants = {r[0]: {'customer': r[1], 'brand': r[2], 'kwp': f(r[3]), 'portfolio': r[4],
                 'tariff': f(r[5])}
          for r in q("SELECT plant_key, customer, brand, kwp_dc, portfolio, "
                     "coalesce(tariff_mxn_per_kwh,0) FROM plant ORDER BY kwp_dc DESC;")}

fleet_kwp = sum(p['kwp'] for p in plants.values())
lifetime = {r[0]: f(r[1]) for r in q(
    "SELECT plant_key, sum(energy_kwh) FROM daily_production GROUP BY plant_key;")}
total_lifetime = sum(lifetime.values())

last30 = {r[0]: f(r[1]) for r in q(
    f"SELECT plant_key, sum(energy_kwh) FROM daily_production "
    f"WHERE prod_date > date '{asof}' - 30 GROUP BY plant_key;")}
total30 = sum(last30.values())

pr30 = {r[0]: f(r[1]) for r in q(
    f"SELECT plant_key, avg(pr) FROM daily_production "
    f"WHERE source='v2' AND pr IS NOT NULL AND prod_date > date '{asof}' - 30 "
    f"GROUP BY plant_key;")}

rev30 = sum(last30.get(k, 0) * p['tariff'] for k, p in plants.items()
            if p['portfolio'] == 'PPA')

monthly = q("SELECT to_char(date_trunc('month', prod_date),'YYYY-MM'), "
            "round(sum(energy_kwh)) FROM daily_production GROUP BY 1 ORDER BY 1;")

income_m = {r[0]: f(r[1]) for r in q("""
  SELECT to_char(make_date(cm.year, cm.month, 1),'YYYY-MM') ym,
         round(sum(dp.kwh * coalesce(cm.tariff_mxn, p.tariff_mxn_per_kwh, 0)))
  FROM (SELECT plant_key, date_trunc('month', prod_date) m, sum(energy_kwh) kwh
        FROM daily_production GROUP BY 1,2) dp
  JOIN plant p ON p.plant_key = dp.plant_key AND p.portfolio = 'PPA'
  LEFT JOIN contract_monthly cm ON cm.plant_key = dp.plant_key
        AND cm.year = extract(year FROM dp.m) AND cm.month = extract(month FROM dp.m)
  GROUP BY 1 ORDER BY 1;""")}

debt_m = {r[0]: f(r[1]) for r in q(
    "SELECT to_char(date_trunc('month', ref_month),'YYYY-MM'), round(sum(payment_mxn)) "
    "FROM loan_schedule GROUP BY 1 ORDER BY 1;")}

months = [m for m, _ in monthly]
fin_months = [m for m in months if m in income_m or m in debt_m]

# ---------------- svg helpers ----------------
def columns_svg(pairs, width=980, height=260, unit='MWh', scale=1000.0):
    """Single-series monthly columns, blue, 4px rounded caps, hover titles."""
    if not pairs:
        return ''
    vals = [f(v) / scale for _, v in pairs]
    vmax = max(vals) or 1
    pad_l, pad_b, pad_t = 46, 30, 14
    W, H = width, height
    plot_w, plot_h = W - pad_l - 8, H - pad_t - pad_b
    n = len(pairs)
    slot = plot_w / n
    bw = min(24, max(6, slot - 2))
    # clean y ticks
    import math
    step = 10 ** math.floor(math.log10(vmax))
    if vmax / step > 5: step *= 2
    if vmax / step < 2: step /= 2
    ticks, t = [], 0.0
    while t <= vmax * 1.001:
        ticks.append(t); t += step
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Monthly production">']
    for t in ticks:
        y = pad_t + plot_h * (1 - t / (ticks[-1] or 1))
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W-8}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" class="tick" text-anchor="end">{int(t):,}</text>')
    top = ticks[-1] or 1
    for k, (m, v) in enumerate(pairs):
        val = f(v) / scale
        h = plot_h * val / top
        x = pad_l + k * slot + (slot - bw) / 2
        y = pad_t + plot_h - h
        r = min(4, bw / 2, h)
        out.append(
            f'<path class="bar" d="M{x:.1f},{y+r:.1f} q0,-{r} {r},-{r} h{bw-2*r:.1f} '
            f'q{r},0 {r},{r} v{max(h-r,0):.1f} h-{bw:.1f} z">'
            f'<title>{m}: {val:,.1f} {unit}</title></path>')
    lab_every = max(1, n // 10)
    for k, (m, _) in enumerate(pairs):
        if k % lab_every == 0:
            x = pad_l + k * slot + slot / 2
            out.append(f'<text x="{x:.1f}" y="{H-8}" class="tick" text-anchor="middle">{m[2:]}</text>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{W-8}" y2="{pad_t+plot_h}" class="axis"/>')
    out.append('</svg>')
    return ''.join(out)


def hbars_svg(rows, width=980, unit='MWh'):
    """Horizontal magnitude bars, one hue, value at tip."""
    if not rows:
        return ''
    vmax = max(v for _, v in rows) or 1
    row_h, pad_l, pad_r = 30, 66, 96
    W = width
    H = len(rows) * row_h + 8
    plot_w = W - pad_l - pad_r
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Lifetime production by plant">']
    for i, (k, v) in enumerate(rows):
        y = 4 + i * row_h
        bw = plot_w * v / vmax
        bh = 20
        r = min(4, bh / 2, bw)
        out.append(f'<text x="{pad_l-8}" y="{y+bh/2+4}" class="lab" text-anchor="end">{html.escape(k)}</text>')
        out.append(
            f'<path class="bar" d="M{pad_l},{y} h{max(bw-r,0):.1f} q{r},0 {r},{r} '
            f'v{bh-2*r:.1f} q0,{r} -{r},{r} h-{max(bw-r,0):.1f} z">'
            f'<title>{k}: {v:,.1f} {unit}</title></path>')
        out.append(f'<text x="{pad_l+bw+8:.1f}" y="{y+bh/2+4}" class="val">{v:,.0f}</text>')
    out.append('</svg>')
    return ''.join(out)


def lines_svg(mlist, s1, s2, name1, name2, width=980, height=260, unit='k MXN', scale=1000.0):
    """Two-series line chart (income vs debt), 2px lines, end labels + legend."""
    if not mlist:
        return ''
    v1 = [f(s1.get(m, 0)) / scale for m in mlist]
    v2 = [f(s2.get(m, 0)) / scale for m in mlist]
    vmax = max(v1 + v2) or 1
    pad_l, pad_b, pad_t, pad_r = 56, 30, 14, 110
    W, H = width, height
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    n = len(mlist)
    def pt(i, val):
        x = pad_l + (plot_w * i / max(n - 1, 1))
        y = pad_t + plot_h * (1 - val / vmax)
        return x, y
    import math
    step = 10 ** math.floor(math.log10(vmax))
    if vmax / step > 5: step *= 2
    ticks, t = [], 0.0
    while t <= vmax * 1.001:
        ticks.append(t); t += step
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{name1} vs {name2}">']
    for t in ticks:
        y = pad_t + plot_h * (1 - t / (ticks[-1] or 1))
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W-pad_r}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" class="tick" text-anchor="end">{int(t):,}</text>')
    for cls, vals, name in (('s1', v1, name1), ('s2', v2, name2)):
        d = ' '.join(f'{"M" if i==0 else "L"}{pt(i,v)[0]:.1f},{pt(i,v)[1]:.1f}' for i, v in enumerate(vals))
        out.append(f'<path class="line {cls}" d="{d}"/>')
        ex, ey = pt(n - 1, vals[-1])
        out.append(f'<circle class="dot {cls}" cx="{ex:.1f}" cy="{ey:.1f}" r="4"/>')
        out.append(f'<text x="{ex+10:.1f}" y="{ey+4:.1f}" class="lab">{name}</text>')
        for i, v in enumerate(vals):
            x, y = pt(i, v)
            out.append(f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="9">'
                       f'<title>{mlist[i]} · {name}: {v:,.0f} {unit}</title></circle>')
    lab_every = max(1, n // 8)
    for i, m in enumerate(mlist):
        if i % lab_every == 0:
            x = pad_l + plot_w * i / max(n - 1, 1)
            out.append(f'<text x="{x:.1f}" y="{H-8}" class="tick" text-anchor="middle">{m[2:]}</text>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{W-pad_r}" y2="{pad_t+plot_h}" class="axis"/>')
    out.append('</svg>')
    return ''.join(out)


# ---------------- page ----------------
gen_at = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
gwh = total_lifetime / 1e6

tiles = f'''
<div class="tiles">
 <div class="tile"><div class="tlabel">Lifetime production</div>
   <div class="thero">{gwh:,.2f} <span class="unit">GWh</span></div>
   <div class="tsub">{first} → {asof}</div></div>
 <div class="tile"><div class="tlabel">Fleet capacity</div>
   <div class="tval">{fleet_kwp/1000:,.2f} <span class="unit">MWp DC</span></div>
   <div class="tsub">{len(plants)} plants · 6 PPA + 4 CAPEX</div></div>
 <div class="tile"><div class="tlabel">Production, last 30 days</div>
   <div class="tval">{total30/1000:,.1f} <span class="unit">MWh</span></div>
   <div class="tsub">to {asof}</div></div>
 <div class="tile"><div class="tlabel">Est. PPA revenue, last 30 days</div>
   <div class="tval">{(f"{rev30/1e6:,.2f}" if rev30 >= 1e6 else f"{rev30:,.0f}")} <span class="unit">{("M MXN" if rev30 >= 1e6 else "MXN")}</span></div>
   <div class="tsub">energy × contract tariff</div></div>
</div>'''

life_rows = sorted(((k, v / 1000) for k, v in lifetime.items()),
                   key=lambda x: -x[1])

table_rows = []
for k in sorted(plants, key=lambda k: -lifetime.get(k, 0)):
    p = plants[k]
    pr = pr30.get(k)
    r30 = last30.get(k, 0) * p['tariff'] if p['portfolio'] == 'PPA' else None
    pr_cell = f"{pr*100:,.1f}%" if pr else "—"
    rev_cell = f"{r30:,.0f}" if r30 is not None else "—"
    table_rows.append(
        f"<tr><td><b>{k}</b></td><td>{html.escape(p['customer'][:34])}</td>"
        f"<td>{p['brand']}</td><td class='num'>{p['kwp']:,.1f}</td>"
        f"<td>{p['portfolio']}</td>"
        f"<td class='num'>{lifetime.get(k,0)/1000:,.1f}</td>"
        f"<td class='num'>{last30.get(k,0)/1000:,.2f}</td>"
        f"<td class='num'>{pr_cell}</td>"
        f"<td class='num'>{rev_cell}</td></tr>")

page = f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>ARGIA — Financial &amp; Production Report</title>
<style>
:root {{ color-scheme: light dark; }}
body {{
  margin:0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background:#f9f9f7; color:#0b0b0b;
  --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --s1:#2a78d6; --s2:#eb6834;
  --border:rgba(11,11,11,0.10);
}}
@media (prefers-color-scheme: dark) {{
 body {{ background:#0d0d0d; color:#fff;
  --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --s1:#3987e5; --s2:#d95926;
  --border:rgba(255,255,255,0.10); }}
}}
.wrap {{ max-width:1040px; margin:0 auto; padding:24px 16px 48px; }}
header h1 {{ font-size:22px; margin:0 0 2px; }}
header .sub {{ color:var(--ink2); font-size:13px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; margin:20px 0; }}
.tile {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }}
.tlabel {{ font-size:12px; color:var(--ink2); }}
.thero {{ font-size:34px; font-weight:600; margin-top:2px; }}
.tval {{ font-size:24px; font-weight:600; margin-top:2px; }}
.unit {{ font-size:13px; font-weight:400; color:var(--ink2); }}
.tsub {{ font-size:11px; color:var(--muted); margin-top:2px; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:10px;
        padding:16px; margin:14px 0; overflow-x:auto; }}
.card h2 {{ font-size:14px; margin:0 0 4px; }}
.card .note {{ font-size:11.5px; color:var(--muted); margin:0 0 10px; }}
svg {{ max-width:100%; height:auto; display:block; }}
.grid {{ stroke:var(--grid); stroke-width:1; }}
.axis {{ stroke:var(--axis); stroke-width:1; }}
.tick {{ fill:var(--muted); font-size:10.5px; }}
.lab  {{ fill:var(--ink2); font-size:11.5px; }}
.val  {{ fill:var(--ink2); font-size:11px; }}
.bar  {{ fill:var(--s1); }}
.bar:hover {{ opacity:.85; }}
.line {{ fill:none; stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
.line.s1 {{ stroke:var(--s1); }} .line.s2 {{ stroke:var(--s2); }}
.dot.s1 {{ fill:var(--s1); }} .dot.s2 {{ fill:var(--s2); }}
.dot {{ stroke:var(--surface); stroke-width:2; }}
.hit {{ fill:transparent; }}
.legend {{ display:flex; gap:16px; font-size:12px; color:var(--ink2); margin:0 0 6px; }}
.key {{ display:inline-block; width:14px; height:3px; border-radius:2px; vertical-align:middle; margin-right:6px; }}
table {{ border-collapse:collapse; width:100%; font-size:12.5px; }}
th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--grid); }}
th {{ color:var(--ink2); font-weight:600; font-size:11.5px; }}
td.num, th.num {{ text-align:right; font-variant-numeric: tabular-nums; }}
footer {{ font-size:11px; color:var(--muted); margin-top:22px; line-height:1.6; }}
</style></head><body><div class="wrap">
<header>
 <h1>ARGIA — Financial &amp; Production Report</h1>
 <div class="sub">Fleet of {len(plants)} plants · {fleet_kwp/1000:,.2f} MWp DC · data {first} → {asof} · generated {gen_at}</div>
</header>
{tiles}
<div class="card">
 <h2>Monthly production — whole fleet</h2>
 <p class="note">MWh per calendar month, v1 history (from {first}) + v2 (from 2026-07-01). Hover a column for the value.</p>
 {columns_svg(monthly)}
</div>
<div class="card">
 <h2>Monthly PPA income vs. debt service</h2>
 <p class="note">Income = monthly energy × contract tariff (PPA plants). Debt service = sum of loan installments due that month (derived from the amortization schedule — never stored). Thousands of MXN.</p>
 <div class="legend"><span><span class="key" style="background:var(--s1)"></span>PPA income (est.)</span>
 <span><span class="key" style="background:var(--s2)"></span>Debt service</span></div>
 {lines_svg(fin_months, income_m, debt_m, 'Income', 'Debt')}
</div>
<div class="card">
 <h2>Lifetime production by plant</h2>
 <p class="note">MWh since each plant first reported.</p>
 {hbars_svg(life_rows)}
</div>
<div class="card">
 <h2>Fleet table</h2>
 <table>
 <tr><th>Plant</th><th>Customer</th><th>Brand</th><th class="num">kWp DC</th><th>Portfolio</th>
     <th class="num">Lifetime MWh</th><th class="num">Last 30d MWh</th><th class="num">PR 30d</th><th class="num">Rev 30d MXN</th></tr>
 {''.join(table_rows)}
 </table>
</div>
<footer>
 Sources: v1 ARGIA_Solar DailyData (2024-02-29 → 2026-06-30, frozen historical record) ·
 v2 Argia_Mont KPI_Daily (2026-07-01 → {asof}) · loans &amp; contracts migrated 1:1 from signed
 amortization tables and ContractData. Income is an estimate (energy × tariff), not invoiced
 amounts. One-time migration to PostgreSQL (argia_mont @ pio06) on 2026-08-25 — live daily
 sync not yet enabled; data ends {asof} until it is.
</footer>
</div></body></html>'''

import os
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, 'w', encoding='utf-8') as fh:
    fh.write(page)
print(f'wrote {OUT}: {len(page):,} bytes; asof={asof}; lifetime={gwh:.2f} GWh; tiles rev30={rev30:,.0f} MXN')
