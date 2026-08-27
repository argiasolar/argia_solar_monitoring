#!/usr/bin/env python3
"""Generate /cfe/index.html — CFE industrial tariff explorer.

Reads argia_mont.cfe_tariff, embeds the last 13 months for every tariff ×
region × charge type. Static page, client-side selection, EN/ES, print-PDF.
Run after every cfe_load / monthly scrape:  python3 cfe_page_gen.py [outroot]
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from argia_logo import LOGO_URI

OUTROOT = sys.argv[1] if len(sys.argv) > 1 else '/www/hosting/monitoring.argia.com.mx/www'

CHARGE_ORDER = ['SUMINISTRO BASICO', 'TRANSMISION', 'DISTRIBUCION', 'CENACE',
                'SERVICIOS CONEXOS NO MEM', 'ENERGIA BASE', 'ENERGIA INTERMEDIA',
                'ENERGIA PUNTA', 'CAPACIDAD', 'FACTOR DE CARGA']
REGION_HINT = {
    'BAJIO': 'GTO · AGS · QRO · SLP', 'BAJA CALIFORNIA': 'BC · Mexicali · Tijuana',
    'BAJA CALIFORNIA SUR': 'BCS · La Paz', 'CENTRO OCCIDENTE': 'MICH · COL',
    'CENTRO ORIENTE': 'PUE · TLAX · Toluca', 'CENTRO SUR': 'MOR · GRO · Cuernavaca',
    'GOLFO CENTRO': 'VER centro · Tampico', 'GOLFO NORTE': 'NL · TAMPS · Monterrey',
    'JALISCO': 'JAL · NAY · Guadalajara', 'NORTE': 'CHIH · DGO · Torreón',
    'NOROESTE': 'SON · SIN · Hermosillo', 'ORIENTE': 'VER sur · OAX norte',
    'PENINSULAR': 'YUC · QROO · CAMP · Mérida', 'SURESTE': 'CHIS · TAB · OAX sur',
    'VALLE DE MEXICO CENTRO': 'CDMX centro', 'VALLE DE MEXICO NORTE': 'EDOMEX norte · Tlalnepantla',
    'VALLE DE MEXICO SUR': 'CDMX sur · EDOMEX sur',
}


def q(sql):
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', 'argia_mont',
                        '-t', '-A', '-F', '\t', '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])
    return [ln.split('\t') for ln in r.stdout.strip().splitlines() if ln.strip()]


# industrial/business tariffs only (2026-08-27): DB1/DB2 (domestic)
# are out of scope for offer & savings work and are not shown
months = sorted({r[0] for r in q('SELECT DISTINCT month FROM cfe_tariff;')})[-13:]
mlist = "','".join(months)
data = {}          # tariff -> region -> charge -> {month: value}
units = {}         # charge -> unit
for tc, reg, mo, ch, un, val in q(
        f"SELECT tariff_code, region, month, charge_type, coalesce(unit,''), value_mxn "
        f"FROM cfe_tariff WHERE month IN ('{mlist}')"
        f" AND tariff_code NOT IN ('DB1','DB2');"):
    data.setdefault(tc, {}).setdefault(reg, {}).setdefault(ch, {})[mo[:7]] = float(val)
    units[ch] = un
srcinfo = q("SELECT source, max(month), max(loaded_at)::date FROM cfe_tariff GROUP BY source;")
fresh = ' · '.join(f"{s}: through {m[:7]} (loaded {l})" for s, m, l in srcinfo)
scrape_ok = any(s == 'cfe_scrape' for s, _, _ in srcinfo)
status = ('CFE-verified data present' if scrape_ok
          else 'seeded from ARGIA Master DB — monthly CFE auto-update pending first run')

tariffs = sorted(data)
regions = sorted({r for t in data.values() for r in t})
months_show = [m[:7] for m in months]

page = f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>CFE Tariffs — ARGIA</title>
<style>
body{{margin:0;font-family:"Segoe UI",system-ui,sans-serif;background:#f6f7f8;color:#202124;font-size:15px;}}
.wrap{{max-width:1120px;margin:0 auto;padding:26px 18px 48px;}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;}}
h1{{font-size:23px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#1c2733;margin:4px 0 0;}}
.sub{{color:#5f6368;font-size:13.5px;margin-top:4px;}}
.logo{{height:26px;width:auto;margin-top:2px;}}
.controls{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0;}}
.btn,select{{background:#fff;border:1px solid #dadce0;border-radius:8px;padding:6px 13px;
 font-size:13.5px;color:#202124;cursor:pointer;text-decoration:none;}}
.card{{background:#fff;border:1px solid #e4e7ea;border-radius:10px;padding:16px 18px;margin:14px 0;overflow-x:auto;}}
.card h2{{font-size:14.5px;margin:0 0 8px;}}
table{{border-collapse:collapse;width:100%;font-size:13px;}}
th,td{{text-align:right;padding:6px 8px;border-bottom:1px solid #eceef0;white-space:nowrap;}}
th{{color:#5f6368;font-size:12px;}}
td:first-child,th:first-child{{text-align:left;}}
td:nth-child(2),th:nth-child(2){{text-align:left;color:#5f6368;}}
.note{{font-size:13px;color:#80868b;}}
.pill{{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;
 background:#e6f4ea;color:#137333;}}
.pill.warn{{background:#fdf0dc;color:#a05c00;}}
@media print{{.controls{{display:none}}body{{background:#fff}}}}
</style></head><body><div class="wrap">
<div class="top"><div><h1 data-en="CFE Industrial Tariffs" data-es="Tarifas Industriales CFE">CFE Industrial Tariffs</h1>
<div class="sub" data-en="Source of truth for offer & savings calculations · all charges, last 12+ months"
 data-es="Fuente de verdad para ofertas y cálculo de ahorros · todos los cargos, últimos 12+ meses">
Source of truth for offer & savings calculations · all charges, last 12+ months</div></div>
<img class="logo" src="{LOGO_URI}" alt="ARGIA SOLAR"></div>
<div class="controls">
 <a class="btn" href="../" data-en="Home" data-es="Inicio">Home</a>
 <select id="tar"></select>
 <select id="reg"></select>
 <button class="btn" onclick="setLang('en')">EN</button>
 <button class="btn" onclick="setLang('es')">ES</button>
 <button class="btn" onclick="window.print()" data-en="Download PDF" data-es="Descargar PDF">Download PDF</button>
 <span class="pill{' ' if scrape_ok else ' warn'}">{status}</span>
</div>
<div class="card"><h2 id="ttl"></h2><div id="tbl"></div>
<p class="note">{fresh} · <span data-en="Values exclude IVA. Region = CFE distribution division; hints show typical states."
 data-es="Valores sin IVA. Región = división de distribución CFE; las pistas muestran estados típicos.">Values exclude IVA. Region = CFE distribution division; hints show typical states.</span></p></div>
<script>
const DATA={json.dumps(data, separators=(',', ':'))};
const UNITS={json.dumps(units)};
const MONTHS={json.dumps(months_show)};
const HINT={json.dumps(REGION_HINT)};
const ORDER={json.dumps(CHARGE_ORDER)};
const $=id=>document.getElementById(id);
function fill(sel,opts,fmt){{sel.innerHTML=opts.map(o=>'<option value="'+o+'">'+(fmt?fmt(o):o)+'</option>').join('');}}
function render(){{
 const t=$('tar').value, r=$('reg').value;
 try{{localStorage.setItem('argia_cfe',t+'|'+r);}}catch(e){{}}
 $('ttl').textContent=t+' — '+r;
 const d=(DATA[t]||{{}})[r]||{{}};
 let h='<table><tr><th data-en="Charge" data-es="Cargo">Charge</th><th data-en="Unit" data-es="Unidad">Unit</th>';
 MONTHS.forEach(m=>h+='<th>'+m+'</th>');h+='</tr>';
 const keys=ORDER.filter(k=>d[k]).concat(Object.keys(d).filter(k=>!ORDER.includes(k)).sort());
 keys.forEach(ch=>{{h+='<tr><td>'+ch+'</td><td>'+(UNITS[ch]||'')+'</td>';
  MONTHS.forEach(m=>{{const v=d[ch]?d[ch][m]:null;
   h+='<td>'+(v==null?'—':v.toLocaleString('en-US',{{maximumFractionDigits:4}}))+'</td>';}});
  h+='</tr>';}});
 h+='</table>';
 $('tbl').innerHTML=h;
}}
function setLang(l){{document.querySelectorAll('[data-en]').forEach(e=>{{e.textContent=e.dataset[l]||e.dataset.en;}});
try{{localStorage.setItem('argia_lang',l);}}catch(e){{}}}}
window.addEventListener('DOMContentLoaded',()=>{{
 fill($('tar'),{json.dumps(tariffs)});
 fill($('reg'),{json.dumps(regions)},r=>r+(HINT[r]?' — '+HINT[r]:''));
 let s='';try{{s=localStorage.getItem('argia_cfe')||'';}}catch(e){{}}
 if(s.includes('|')){{const p=s.split('|');
  if(DATA[p[0]])$('tar').value=p[0];
  if({json.dumps(regions)}.includes(p[1]))$('reg').value=p[1];}}
 $('tar').addEventListener('change',render);
 $('reg').addEventListener('change',render);
 let l='en';try{{l=localStorage.getItem('argia_lang')||'en';}}catch(e){{}};setLang(l);
 render();
}});
</script></div></body></html>'''

p = os.path.join(OUTROOT, 'cfe', 'index.html')
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, 'w', encoding='utf-8') as fh:
    fh.write(page)
os.chmod(p, 0o644)
print(f'wrote {p}: {len(page):,} B · {len(tariffs)} tariffs × {len(regions)} regions × {len(months_show)} months')
