"""Setup › CFE & tariffs — the tariff explorer (v212). Pure: rows in,
HTML out. No database access here; ``setup_app.cfe_explorer_card``
feeds it the ``cfe_tariff`` rows (read-only — the table belongs to the
CFE pipeline) and ``cfe_page_gen`` stays the old site's renderer until
that site is decommissioned.

What the card shows (Tomasz 2026-09-05):
  * an at-a-glance freshness pill — the SAME rule as the lightning on the
    report home (``report_gen``): verified through the latest month that
    has all scrapeable tariffs from source ``cfe_scrape``, "up to date"
    when that month is the current one;
  * tiles with the AVERAGE energy price across CFE regions for BASE,
    INTERMEDIA and PUNTA of the selected scheme (GDMTH by default) —
    plus the min–max spread and the change vs the previous month;
  * every scheme at a glance (the same averages per tariff code);
  * the explorer as before — tariff × region → charge × month table,
    with a search box over regions (division name or the states hint),
    tariff codes and charge names.
"""
from __future__ import annotations

import datetime as dt
import html
import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

CHARGE_ORDER = ['SUMINISTRO BASICO', 'TRANSMISION', 'DISTRIBUCION', 'CENACE',
                'SERVICIOS CONEXOS NO MEM', 'ENERGIA BASE', 'ENERGIA INTERMEDIA',
                'ENERGIA SEMIPUNTA', 'ENERGIA PUNTA', 'CAPACIDAD',
                'FACTOR DE CARGA']
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
# the three period prices every offer is built on, in tile order
PERIODS = [('ENERGIA BASE', 'Base', 'Base'),
           ('ENERGIA INTERMEDIA', 'Intermedia', 'Intermedia'),
           ('ENERGIA PUNTA', 'Punta', 'Punta')]
DEFAULT_TARIFF = 'GDMTH'
EXCLUDED_TARIFFS = ('DB1', 'DB2')      # domestic — out of scope (2026-08-27)
VERIFIED_MIN_TARIFFS = 10             # a month counts only when all scrapeable tariffs are in
MONTHS_SHOWN = 12            # verified months in the table (+ seeded ones after)


# ------------------------------------------------------------- dataset
def build_dataset(rows: Iterable[Sequence], months_shown: int = MONTHS_SHOWN,
                  upto: str = '') -> Dict:
    """rows = (tariff_code, region, month 'YYYY-MM[-DD]', charge_type,
    unit, value) → {'data': tariff→region→charge→{YYYY-MM: value},
    'units': charge→unit, 'months': last N months ascending}. With
    ``upto`` (the CFE-verified month) the N-month window counts verified
    months only; seeded months after it ride along at the end."""
    data: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = {}
    units: Dict[str, str] = {}
    months = set()
    for r in rows:
        if len(r) < 6:
            continue
        tc, reg, mo, ch, un, val = (str(x).strip() for x in r[:6])
        if tc in EXCLUDED_TARIFFS or not tc or not reg or not ch:
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        ym = mo[:7]
        months.add(ym)
        data.setdefault(tc, {}).setdefault(reg, {}).setdefault(ch, {})[ym] = v
        if un:
            units[ch] = un
    if upto:
        keep = (sorted(m for m in months if m <= upto)[-months_shown:]
                + sorted(m for m in months if m > upto))
    else:
        keep = sorted(months)[-months_shown:]
    kept = set(keep)
    for tc in data:
        for reg in data[tc]:
            for ch in data[tc][reg]:
                data[tc][reg][ch] = {m: v for m, v in data[tc][reg][ch].items() if m in kept}
    return {'data': data, 'units': units, 'months': keep}


def period_average(data: Dict, tariff: str, charge: str, month: str
                   ) -> Optional[Dict[str, float]]:
    """Average of one charge across the regions that quote it for the
    month: {'avg', 'min', 'max', 'n'} or None when nobody quotes it."""
    vals = []
    for reg, charges in (data.get(tariff) or {}).items():
        v = (charges.get(charge) or {}).get(month)
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return {'avg': sum(vals) / len(vals), 'min': min(vals), 'max': max(vals),
            'n': len(vals)}


def scheme_averages(data: Dict, months: Sequence[str], upto: str = ''
                    ) -> Dict[str, Dict]:
    """{tariff: {'month': latest month with any period price,
    'prev': month before it or None, charge: {'avg','min','max','n',
    'prev_avg'}}} — one entry per tariff, periods as in PERIODS. A
    tariff without period prices (flat schemes) keeps the entry with no
    period keys so the table still lists it. ``upto`` (the CFE-verified
    month) keeps seeded future months out of the averages."""
    out: Dict[str, Dict] = {}
    months = list(months)
    cands = [m for m in months if not upto or m <= upto] or months
    for tc in sorted(data):
        entry: Dict = {'month': None, 'prev': None}
        latest = None
        for m in reversed(cands):
            if any(period_average(data, tc, ch, m) for ch, _, _ in PERIODS):
                latest = m
                break
        if latest is not None:
            i = list(months).index(latest)
            prev = months[i - 1] if i > 0 else None
            entry['month'], entry['prev'] = latest, prev
            for ch, _, _ in PERIODS:
                a = period_average(data, tc, ch, latest)
                if a is None:
                    continue
                p = period_average(data, tc, ch, prev) if prev else None
                a['prev_avg'] = p['avg'] if p else None
                entry[ch] = a
        out[tc] = entry
    return out


# ------------------------------------------------------------ freshness
def verified_through(rows: Iterable[Sequence], min_tariffs: int = VERIFIED_MIN_TARIFFS
                     ) -> str:
    """'YYYY-MM' of the latest month with >= min_tariffs distinct
    tariff codes from the CFE scrape — rows = (month, source,
    tariff_code). Same rule as the report home's lightning."""
    by_month: Dict[str, set] = {}
    for r in rows:
        if len(r) >= 3 and str(r[1]).strip() == 'cfe_scrape':
            by_month.setdefault(str(r[0])[:7], set()).add(str(r[2]).strip())
    ok = [m for m, s in by_month.items() if len(s) >= min_tariffs]
    return max(ok) if ok else ''


def freshness(through: str, today: dt.date, healthy: Optional[bool] = None
              ) -> Tuple[str, str, str]:
    """(tone, en, es) for the pill. tone: good = this month's CFE-verified
    tariffs are loaded; warn = one month behind (CFE publishes early in
    the month) or the pipeline needs a look; bad = older or nothing
    verified."""
    cur = today.strftime('%Y-%m')
    prev = (today.replace(day=1) - dt.timedelta(days=1)).strftime('%Y-%m')
    if not through:
        return ('bad', 'No CFE-verified tariffs loaded yet',
                'Aún no hay tarifas verificadas por CFE')
    if through >= cur:
        if healthy is False:
            return ('warn', f'Up to date — through {through}, but the pipeline needs a look',
                    f'Al día — hasta {through}, pero el flujo necesita revisión')
        return ('good', f'Up to date — CFE-verified through {through}',
                f'Al día — verificado por CFE hasta {through}')
    if through == prev:
        return ('warn', f'{cur} not loaded yet — verified through {through}',
                f'{cur} aún no cargado — verificado hasta {through}')
    return ('bad', f'Out of date — verified through {through} only',
            f'Desactualizado — verificado sólo hasta {through}')


# ---------------------------------------------------------------- html
def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _t(en, es=None) -> str:
    es = en if es is None else es
    return f'<span data-en="{_e(en)}" data-es="{_e(es)}">{_e(en)}</span>'


EXPLORER_CSS = '''
.cfex .ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0 12px}
.cfex .ctl input[type=search]{border:1px solid #d2d7dd;border-radius:8px;padding:8px 12px;font-size:13.5px;min-width:260px;font-family:inherit}
.cfex .ctl select{border:1px solid #d2d7dd;border-radius:8px;padding:8px 10px;font-size:13.5px;background:#fff;font-family:inherit}
.cfex .ctl .btn{padding:8px 13px}
.cfex .tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:8px 0 14px}
.cfex .tile{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:14px 16px;display:flex;flex-direction:column;gap:4px}
.cfex .tile .tlabel{font-size:12px;color:#6b7480;font-weight:600}
.cfex .tile .tval{font-size:28px;font-weight:800;line-height:1;color:#1a1d23}
.cfex .tile .tval .unit{font-size:13px;font-weight:600;color:#6b7480;margin-left:4px}
.cfex .tile .tsub{font-size:12px;color:#6b7480}
.cfex .tile .up{color:#c2554e}.cfex .tile .down{color:#05847d}
.cfex table td,.cfex table th{white-space:nowrap;text-align:right;padding:6px 8px}
.cfex table td:first-child,.cfex table th:first-child{text-align:left}
.cfex .charge td:nth-child(2),.cfex .charge th:nth-child(2){text-align:left;color:#6b7480}
.cfex .charge{font-size:12px}.cfex .charge td,.cfex .charge th{padding:5px 5px}.cfex .charge td:first-child{white-space:normal;max-width:190px}
.cfex #cfe_tbl{overflow-x:auto}
.cfex tr.on td{background:#e6f7f5}
.cfex th.seed{color:#b26a00}
.cfex .schemes tbody tr{cursor:pointer}
.cfex .pill.good{background:#e6f7f5;color:#05847d}.cfex .pill.warn{background:#fff4e0;color:#b26a00}.cfex .pill.bad{background:#fdeaea;color:#c2554e}
@media(max-width:800px){.cfex .tiles{grid-template-columns:1fr}}
@media print{.cfex .ctl{display:none}}
'''


def explorer_html(dataset: Dict, tone: str, fresh_en: str, fresh_es: str,
                  sources_note: str = '', default_tariff: str = DEFAULT_TARIFF,
                  through: str = '') -> str:
    """The whole card. Everything the browser needs is embedded; the
    selection (tariff|region) is remembered in localStorage.argia_cfe as
    on the old /cfe/ page. ``through`` = the CFE-verified month: the
    averages stop there and later (seeded) months are marked. Pure."""
    data, units, months = dataset['data'], dataset['units'], dataset['months']
    tariffs = sorted(data)
    regions = sorted({r for t in data.values() for r in t})
    avgs = scheme_averages(data, months, upto=through)
    default = default_tariff if default_tariff in data else (tariffs[0] if tariffs else '')
    if not tariffs:
        return ('<div class="card cfex"><h2 data-en="CFE tariff explorer" data-es="Explorador de tarifas CFE">'
                'CFE tariff explorer</h2><p class="note" data-en="No tariff rows loaded yet." '
                'data-es="Aún no hay tarifas cargadas.">No tariff rows loaded yet.</p></div>')
    period_cols = ''.join(f'<th>{_t(en, es)}</th>' for _, en, es in PERIODS)
    return f'''<div class="card cfex"><h2 style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
<span data-en="CFE tariff explorer" data-es="Explorador de tarifas CFE">CFE tariff explorer</span>
<span class="pill {tone}" id="cfe_fresh">{_t(fresh_en, fresh_es)}</span></h2>
<p class="note" data-en="Average energy prices across CFE regions for the selected scheme (MXN/kWh, without IVA). Pick a scheme and a region, or search for a state, a region or a charge."
 data-es="Precios promedio de energía entre regiones CFE para el esquema elegido (MXN/kWh, sin IVA). Elige esquema y región, o busca un estado, una región o un cargo.">
Average energy prices across CFE regions for the selected scheme (MXN/kWh, without IVA).</p>
<div class="ctl">
 <input type="search" id="cfe_q" placeholder="Search region, state, tariff or charge…" data-ph-en="Search region, state, tariff or charge…" data-ph-es="Buscar región, estado, tarifa o cargo…" autocomplete="off">
 <select id="cfe_tar"></select>
 <select id="cfe_reg"></select>
 <button class="btn" type="button" onclick="window.print()" data-en="Download PDF" data-es="Descargar PDF">Download PDF</button>
</div>
<div class="tiles" id="cfe_tiles"></div>
<h2 style="margin-top:14px" data-en="Every scheme at a glance — average of all regions, latest month" data-es="Todos los esquemas — promedio de todas las regiones, último mes">Every scheme at a glance — average of all regions, latest month</h2>
<div style="overflow:visible"><table class="schemes" id="cfe_schemes"><thead><tr><th data-en="Scheme" data-es="Esquema">Scheme</th><th data-en="Month" data-es="Mes">Month</th>{period_cols}<th data-en="Regions" data-es="Regiones">Regions</th></tr></thead><tbody></tbody></table></div>
<h2 style="margin-top:14px" id="cfe_ttl"></h2>
<div id="cfe_tbl"></div>
<p class="note">{_e(sources_note)}{' · ' if sources_note else ''}<span data-en="Values exclude IVA. Region = CFE distribution division; hints show typical states. Months marked * are not CFE-verified yet (Master DB seed)."
 data-es="Valores sin IVA. Región = división de distribución CFE; las pistas muestran estados típicos. Los meses con * aún no están verificados por CFE (semilla Master DB).">Values exclude IVA. Region = CFE distribution division; hints show typical states. Months marked * are not CFE-verified yet.</span></p>
<script>
(function(){{
const DATA={json.dumps(data, separators=(',', ':'))};
const UNITS={json.dumps(units)};
const MONTHS={json.dumps(months)};
const HINT={json.dumps(REGION_HINT)};
const ORDER={json.dumps(CHARGE_ORDER)};
const AVG={json.dumps(avgs, separators=(',', ':'))};
const PERIODS={json.dumps([[c, en, es] for c, en, es in PERIODS])};
const TARIFFS={json.dumps(tariffs)};
const REGIONS={json.dumps(regions)};
const DEFAULT={json.dumps(default)};
const THROUGH={json.dumps(through)};
const $=id=>document.getElementById(id);
const lang=()=>{{try{{return localStorage.getItem('argia_lang')||'en'}}catch(e){{return 'en'}}}};
const L=(en,es)=>lang()==='es'?es:en;
const fmt=(v,d)=>v==null?'—':v.toLocaleString('en-US',{{minimumFractionDigits:d,maximumFractionDigits:d}});
function fill(sel,opts,cur,label){{sel.innerHTML=opts.map(o=>'<option value="'+o+'">'+(label?label(o):o)+'</option>').join('');if(opts.includes(cur))sel.value=cur;}}
function tiles(){{
 const t=$('cfe_tar').value, a=AVG[t]||{{}};
 $('cfe_tiles').innerHTML=PERIODS.map(p=>{{const x=a[p[0]];
  if(!x)return '<div class="tile"><div class="tlabel">'+L(p[1],p[2])+' · '+t+'</div><div class="tval">—</div><div class="tsub">'+L('not quoted for this scheme','no aplica a este esquema')+'</div></div>';
  let d='';if(x.prev_avg!=null){{const pc=100*(x.avg-x.prev_avg)/x.prev_avg;d=' · <span class="'+(pc>0.05?'up':pc<-0.05?'down':'')+'">'+(pc>=0?'+':'')+pc.toFixed(1)+'% '+L('vs','vs')+' '+a.prev+'</span>';}}
  return '<div class="tile"><div class="tlabel">'+L(p[1],p[2])+' · '+t+' · '+a.month+'</div><div class="tval">'+fmt(x.avg,4)+'<span class="unit">MXN/kWh</span></div><div class="tsub">'+L('average of','promedio de')+' '+x.n+' '+L('regions','regiones')+' · '+fmt(x.min,4)+' – '+fmt(x.max,4)+d+'</div></div>';}}).join('');
}}
function schemes(){{
 const cur=$('cfe_tar').value;
 $('cfe_schemes').querySelector('tbody').innerHTML=TARIFFS.map(t=>{{const a=AVG[t]||{{}};let n=0;
  const cells=PERIODS.map(p=>{{const x=a[p[0]];if(x)n=Math.max(n,x.n);return '<td>'+(x?fmt(x.avg,4):'—')+'</td>';}}).join('');
  return '<tr data-t="'+t+'"'+(t===cur?' class="on"':'')+'><td><b>'+t+'</b></td><td>'+(a.month||'—')+'</td>'+cells+'<td>'+(n||'—')+'</td></tr>';}}).join('');
 $('cfe_schemes').querySelectorAll('tbody tr').forEach(tr=>tr.onclick=()=>{{$('cfe_tar').value=tr.dataset.t;render();}});
}}
function table(q){{
 const t=$('cfe_tar').value, r=$('cfe_reg').value;
 $('cfe_ttl').textContent=t+' — '+r+(HINT[r]?' ('+HINT[r]+')':'');
 const d=(DATA[t]||{{}})[r]||{{}};
 let keys=ORDER.filter(k=>d[k]).concat(Object.keys(d).filter(k=>!ORDER.includes(k)).sort());
 if(q)keys=keys.filter(k=>k.toLowerCase().includes(q));
 let h='<table class="charge"><tr><th>'+L('Charge','Cargo')+'</th><th>'+L('Unit','Unidad')+'</th>';
 MONTHS.forEach(m=>h+=(THROUGH&&m>THROUGH)?'<th class="seed" title="'+L('not CFE-verified yet — Master DB seed','aún no verificado por CFE — semilla Master DB')+'">'+m+' *</th>':'<th>'+m+'</th>');h+='</tr>';
 keys.forEach(ch=>{{h+='<tr><td>'+ch+'</td><td>'+(UNITS[ch]||'')+'</td>';
  MONTHS.forEach(m=>{{const v=d[ch]?d[ch][m]:null;h+='<td>'+(v==null?'—':v.toLocaleString('en-US',{{maximumFractionDigits:4}}))+'</td>';}});h+='</tr>';}});
 if(!keys.length)h+='<tr><td colspan="'+(MONTHS.length+2)+'" class="note">'+L('no charge matches','ningún cargo coincide')+'</td></tr>';
 $('cfe_tbl').innerHTML=h+'</table>';
}}
function render(){{
 const q=($('cfe_q').value||'').trim().toLowerCase();
 const tarHit=TARIFFS.filter(t=>t.toLowerCase().includes(q));
 const regHit=REGIONS.filter(r=>(r+' '+(HINT[r]||'')).toLowerCase().includes(q));
 const tarCur=$('cfe_tar').value, regCur=$('cfe_reg').value;
 fill($('cfe_tar'),(q&&tarHit.length&&tarHit.length<TARIFFS.length)?tarHit:TARIFFS,tarCur||DEFAULT);
 fill($('cfe_reg'),(q&&regHit.length&&regHit.length<REGIONS.length)?regHit:REGIONS,regCur,r=>r+(HINT[r]?' — '+HINT[r]:''));
 const chargeQ=(q&&!tarHit.length&&!regHit.length)?q:(q&&tarHit.length===TARIFFS.length&&regHit.length===REGIONS.length?q:'');
 try{{localStorage.setItem('argia_cfe',$('cfe_tar').value+'|'+$('cfe_reg').value);}}catch(e){{}}
 tiles();schemes();table(chargeQ);
}}
function init(){{
 fill($('cfe_tar'),TARIFFS,DEFAULT);fill($('cfe_reg'),REGIONS,REGIONS[0],r=>r+(HINT[r]?' — '+HINT[r]:''));
 let s='';try{{s=localStorage.getItem('argia_cfe')||'';}}catch(e){{}}
 if(s.includes('|')){{const p=s.split('|');if(DATA[p[0]])$('cfe_tar').value=p[0];if(REGIONS.includes(p[1]))$('cfe_reg').value=p[1];}}
 const ph=$('cfe_q');ph.placeholder=lang()==='es'?ph.dataset.phEs:ph.dataset.phEn;
 $('cfe_tar').addEventListener('change',render);$('cfe_reg').addEventListener('change',render);
 $('cfe_q').addEventListener('input',render);
 render();
}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}})();
</script></div>'''
