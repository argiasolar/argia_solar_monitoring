"""v382: the troubleshooting card of a plant page - spec v0.7 section 8 (Plant / Inverters / Strings).

Pure rendering of the payload built by argia.epi.views: one card, one chart with one
time axis, a panel on the right that switches the level, a summary of every string
(EPI-600 month to date, "no output" as a cross) and the rules in plain words.
No I/O; the page draws the SVG in the browser from the embedded JSON.
"""
from __future__ import annotations

import html
import json

from argia.report.string_layout_html import INV_COLORS

BAND_TEXT = {'green': ('on target', 'en meta'), 'amber': ('watch', 'vigilar'), 'red': ('below', 'bajo'),
             'suspect': ('suspect data', 'dato sospechoso'), 'insufficient': ('insufficient data', 'datos insuficientes')}


def _embed(obj) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/')


def t(en: str, es: str, tag: str = 'span', cls: str = '') -> str:
    c = f' class="{cls}"' if cls else ''
    return f'<{tag}{c} data-en="{html.escape(en)}" data-es="{html.escape(es)}">{html.escape(en)}</{tag}>'


def _days(pl: dict) -> str:
    n, m = pl.get('mtd_days'), pl.get('mtd_days_expected')
    if n is None or m is None:
        return ''
    if n >= m:
        return ', ' + t(f'{n} days', f'{n} días', 'span')
    return ', ' + t(f'{n} of {m} days so far, the rest is being computed',
                    f'{n} de {m} días por ahora, el resto se está calculando', 'span')


EPI_DISPLAY_MAX = 1.0


def cap(x):
    """v399 (Arturo, 24 Nov 2026): an EPI above 100 % is displayed as 100 %. Display only - the
    engine, the cache and every ratio keep the computed value."""
    return None if x is None else min(x, EPI_DISPLAY_MAX)


def capped(p: dict) -> dict:
    """A display copy of a card payload with every EPI-600 figure capped at 100 % (plant, inverters,
    strings, week / month history). Power series (W/kWp) are untouched. Pure."""
    import copy
    q = copy.deepcopy(p)
    pl = q.get('plant') or {}
    for k in ('epi600_day', 'epi600_mtd'):
        if k in pl:
            pl[k] = cap(pl[k])
    for i in q.get('inverters') or []:
        for k in ('epi600_day', 'epi600_mtd', 'epi600_mtd_producing'):
            if k in i:
                i[k] = cap(i[k])
    for sv in (q.get('strings') or {}).values():
        if isinstance(sv.get('epi600_mtd'), dict):
            sv['epi600_mtd'] = {u: cap(v) for u, v in sv['epi600_mtd'].items()}
    h = q.get('hist')
    if h:
        h['plant'] = [cap(v) for v in h.get('plant') or []]
        for part in ('inv', 'str'):
            h[part] = {k: [cap(v) for v in vals] for k, vals in (h.get(part) or {}).items()}
        for name in ('sum', 'sum7'):
            h[name] = {k: cap(v) for k, v in (h.get(name) or {}).items()}
    return q


def _pct(x):
    return 'n/a' if x is None else f'{x * 100:.1f} %'


def day_nav(pk: str, nav: dict) -> str:
    """v390 (Tomasz: "add a date picker to this graph, so the user doesnt have to go up the page in order to
    move days back and forth"): previous day / date / next day / today, in the card's own head line. Every link
    lands back on this card (#ev<pk>). ``nav``: day, today, first (oldest day page), prev / next (the
    neighbouring days that have a page, or None) and base (the plant's page folder, no trailing slash)."""
    base, today = nav['base'], nav['today']

    def url(d):
        return (f'{base}/' if d == today else f'{base}/d/{d}.html') + f'#ev{pk}'

    def arrow(d, sym, en, es, cls):
        if not d:
            return f'<span class="evnb off {cls}" aria-disabled="true">{sym}</span>'
        return (f'<a class="evnb {cls}" href="{url(d)}" title="{en}: {d}" data-en="{sym}" data-es="{sym}" '
                f'data-title-es="{es}: {d}">{sym}</a>')
    out = ('<div class="evnav">' + arrow(nav.get('prev'), '&#9664;', 'Previous day', 'Día anterior', 'evprev')
           + f'<input type="date" class="evdate" value="{nav["day"]}" min="{nav["first"]}" max="{today}" '
             f'data-base="{base}" data-today="{today}" data-card="ev{pk}" aria-label="Day">'
           + arrow(nav.get('next'), '&#9654;', 'Next day', 'Día siguiente', 'evnext'))
    if nav['day'] != today:
        out += f'<a class="evnb evtoday" href="{url(today)}" data-en="Today" data-es="Hoy">Today</a>'
    return out + '</div>'


def render(pk: str, p: dict, live: bool, nav: dict = None) -> str:
    p = capped(p)                          # v399: EPI above 100 % shown as 100 %
    pl = p['plant']
    band = pl.get('band_mtd') or 'insufficient'
    be, bs = BAND_TEXT.get(band, BAND_TEXT['insufficient'])
    r = p['rules']
    rules_en = (f"W per kWp = DC power of the strings / their kWp (Annex A); where an inverter sent no string sample, AC / 0.985 is used. "
                f"Design = the EPI model's expected power for the measured irradiance and panel temperature (station samples that fail "
                f"the quality checks have no design point). An inverter is flagged when its actual/design ratio falls below "
                f"{r['gap_share']:.0%} of the median ratio of the plant's inverters for {r['gap_min_min']} min or more (design at least "
                f"{r['gap_min_wkwp']:.0f} W/kWp), so cloud over the station or low-sun shading on the whole plant is not flagged. "
                f"Strings are compared by their index (W per kWp / their own design, so different roof planes compare fairly): grey band "
                f"= {r['band'][0]:.0%} to {r['band'][1]:.0%} of the inverter's median; a string outside it for {r['out_min_min']} min or more "
                f"with G_poa of {r['out_min_gpoa']:.0f} W/m2 or more is drawn in colour. No output = below {r['zero_share']:.0%} of the "
                f"median with G_poa of {r['zero_gpoa']:.0f} W/m2 or more; such a string is left out of the median and band.")
    rules_es = (f"W por kWp = potencia CC de las cadenas / su kWp (Anexo A); si un inversor no envió muestra de cadenas se usa CA / 0.985. "
                f"Diseño = potencia esperada por el modelo EPI para la irradiancia y temperatura de panel medidas (las muestras de estación "
                f"que no pasan los controles de calidad no tienen punto de diseño). Un inversor se marca cuando su razón real/diseño cae "
                f"bajo {r['gap_share']:.0%} de la mediana de los inversores de la planta durante {r['gap_min_min']} min o más (diseño al menos "
                f"{r['gap_min_wkwp']:.0f} W/kWp): una nube sobre la estación o sombra de sol bajo en toda la planta no se marca. "
                f"Las cadenas se comparan por su índice (W por kWp / su propio diseño, así planos distintos se comparan con justicia): banda gris "
                f"= {r['band'][0]:.0%} a {r['band'][1]:.0%} de la mediana del inversor; una cadena fuera de ella {r['out_min_min']} min o más "
                f"con G_poa de {r['out_min_gpoa']:.0f} W/m2 o más se dibuja en color. Sin salida = bajo {r['zero_share']:.0%} de la "
                f"mediana con G_poa de {r['zero_gpoa']:.0f} W/m2 o más; esa cadena queda fuera de la mediana y la banda.")
    head = (f'<div class="evhead"><div><b>EPI-600</b> {t("month to date", "mes a la fecha")}: <b>{_pct(pl.get("epi600_mtd"))}</b> '
            f'<span class="evband ev-{band}">{t(be, bs)}</span> <span class="note">({pl.get("steps600_mtd", 0)} '
            f'{t("valid 5-min steps", "pasos de 5 min válidos")}{_days(pl)})</span></div>'
            f'<div class="evright">{day_nav(pk, nav) if nav else ""}'
            f'<div class="note">{t("this day", "este día")}: {_pct(pl.get("epi600_day"))}</div></div></div>')
    caveat = t('Bands are provisional: the station sensors are not yet validated (they read 7 to 21 % below satellite). '
               'Comparisons inside the plant - inverter with inverter, string with string - use the same sensor and are reliable.',
               'Las bandas son provisionales: los sensores de las estaciones aún no están validados (leen 7 a 21 % menos que el satélite). '
               'Las comparaciones dentro de la planta - inversor contra inversor, cadena contra cadena - usan el mismo sensor y son confiables.')
    seg = ('<div class="evseg">' + ''.join(
        f'<button type="button" data-v="{v}" data-en="{en}" data-es="{es}">{en}</button>'
        for v, en, es in (('p', 'Plant', 'Planta'), ('i', 'Inverters', 'Inversores'), ('s', 'Strings', 'Cadenas'),
                          ('w', 'Week', 'Semana'), ('m', 'Month', 'Mes'))
        + ((('c', 'Consumption', 'Consumo'),) if p.get('cons') else ())) + '</div>')
    cons = p.get('cons') or {}
    ct = cons.get('today') or {}
    if cons:                                          # v386: consumption in the head line, next to EPI-600
        share = '' if ct.get('share') is None else f" · {t('solar share', 'cobertura solar')} <b>{ct['share'] * 100:.0f} %</b>"
        kwh = 'n/a' if ct.get('cons') is None else f"{ct['cons']:,.0f} kWh"
        lab = t('Consumption so far today', 'Consumo de hoy a la hora') if live else t('Consumption this day', 'Consumo del día')
        head += f'<div class="evhead2 note">{lab}: <b>{kwh}</b>{share}</div>'
    return f'''<div class="card evcard" id="ev{pk}" data-colors='{_embed(INV_COLORS)}'>
<h2 data-en="Troubleshooting: plant, inverters, strings" data-es="Diagnóstico: planta, inversores, cadenas">Troubleshooting: plant, inverters, strings</h2>
{head}
<div class="evmain">{seg}<div class="evchart"><svg class="evsvg" viewBox="0 0 940 330" preserveAspectRatio="none"></svg><div class="evleg"></div></div>
<div class="evpanel"></div></div>
<div class="evsum"></div>
<div class="evby"></div>
<div class="evtip" role="tooltip"></div>
<p class="note">{caveat}</p>
<p class="note">{t(rules_en, rules_es)}</p>
<script type="application/json" class="evdata">{_embed(p)}</script>
</div>'''


CSS = '''
.evcard .evtk{display:inline-block;white-space:nowrap;font-size:12px;padding:2px 8px;border-radius:6px;border:1px solid #c5221f;color:#c5221f;text-decoration:none}
.evcard .evtk:hover{background:#fce8e6}
.evcard .evtk.open{border-color:#1e8e3e;color:#1e8e3e}
.evcard .evtks{margin-top:8px}
.evcard .evtkrow{margin-top:4px}

.evcard .evhead{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;margin:4px 0 8px}
.evband{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:12px}
.ev-green{background:#1e8e3e}.ev-amber{background:#b26a00}.ev-red{background:#c5221f}.ev-suspect{background:#6a3fb5}.ev-insufficient{background:#7a8494}
.evmain{display:block}.evchart{min-width:0}
.evsvg{width:100%;height:330px;display:block}.evseg{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 8px}
.evhead2{margin:-4px 0 8px}
.evright{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.evnav{display:flex;align-items:center;gap:4px}
.evnb{display:inline-block;min-width:30px;text-align:center;border:1px solid #d6dbe1;background:#fff;border-radius:6px;padding:4px 8px;color:#1c2733;text-decoration:none;font-size:13px;line-height:18px}
.evnb:hover{border-color:#1c2733}.evnb.off{opacity:.35;cursor:default}.evnb.off:hover{border-color:#d6dbe1}
.evdate{border:1px solid #d6dbe1;border-radius:6px;padding:3px 6px;font:inherit;font-size:13px}
.evseg button,.evinv button{border:1px solid #d6dbe1;background:#fff;border-radius:6px;padding:5px 8px;text-align:left;cursor:pointer;font:inherit}
.evseg button.on{background:#1c2733;color:#fff;border-color:#1c2733}.evinv button.on{border-color:#1c2733;box-shadow:inset 3px 0 0 #1c2733}.evinv button.off{opacity:.45}
.evpanel{font-size:12.5px;margin-top:10px}.evpanel table{width:100%;border-collapse:collapse}.evpanel td{padding:3px 2px;border-bottom:1px solid #eceef0}
.evinv{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}.evinv button{flex:0 1 250px}
.evtiles{display:flex;flex-wrap:wrap;gap:22px;margin:4px 0 8px}.evtiles b{display:block;font-size:20px;color:#1c2733}.evsw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.evleg{font-size:12px;color:#5f6368;margin-top:4px}.evleg span{margin-right:12px;white-space:nowrap;display:inline-block}
.evcard .evleg svg{display:inline-block;vertical-align:middle}   /* the portal sets svg{display:block} */
.evsum{margin-top:8px}.evsum svg{width:100%;height:auto}
.evby{margin-top:18px;border-top:1px solid #eceef0;padding-top:10px}.evh3{font-size:14px;margin:4px 0 2px}
.evbyinv{margin:12px 0 4px}.evbyh{font-size:13px;color:#3c4043;margin:0 0 2px}.evbysvg{width:100%;height:auto;display:block}
.evtip{position:fixed;display:none;background:#1c2733;color:#fff;padding:6px 8px;border-radius:6px;font-size:12px;pointer-events:none;z-index:50;max-width:280px}
.evflag{color:#c5221f;font-weight:600}
@media(max-width:820px){.evinv button{flex:1 1 100%}}
'''

JS = r'''
(function(){
var L=function(){return (document.documentElement.lang==='es'||(function(){try{return localStorage.getItem('argia_lang')==='es';}catch(e){return false;}})())?1:0;};
function tx(a){return a[L()];}
function el(tag,attrs,txt){var e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(var k in attrs)e.setAttribute(k,attrs[k]);if(txt!=null)e.textContent=txt;return e;}
function pct(x){return x==null?'n/a':(x*100).toFixed(1)+' %';}
// v383: a prefilled ticket, or the open ticket that already covers it
function tkcell(e){if(!e)return '';
 if(e.open)return '<a class="evtk open" href="/maintenance/t/'+encodeURIComponent(e.open)+'/">'+e.open+' '+tx(['open','abierto'])+'</a>';
 return '<a class="evtk" href="'+e.url.replace(/&/g,'&amp;').replace(/"/g,'&quot;')+'">'+tx(['Open ticket','Abrir ticket'])+'</a>';}
document.querySelectorAll('.evcard').forEach(function(card){
 var P=JSON.parse(card.querySelector('.evdata').textContent), C=JSON.parse(card.dataset.colors);
 var svg=card.querySelector('.evsvg'), leg=card.querySelector('.evleg'), panel=card.querySelector('.evpanel'), sum=card.querySelector('.evsum'), tip=card.querySelector('.evtip');
 var view='i', sel=(P.inverters[0]||{}).inv, hidden={};
 // v390: the view and the chosen inverter survive a day change (this tab only)
 var VK='argia_ev_'+card.id;
 try{var st=JSON.parse(sessionStorage.getItem(VK)||'null');if(st){if(card.querySelector('.evseg button[data-v="'+st.v+'"]'))view=st.v;
  if(P.inverters.some(function(x){return x.inv===st.sel;}))sel=st.sel;}}catch(e){}
 function keep(){try{sessionStorage.setItem(VK,JSON.stringify({v:view,sel:sel}));}catch(e){}}
 var dp=card.querySelector('.evdate');
 if(dp)dp.addEventListener('change',function(){var d=dp.value;if(!d||d<dp.min||d>dp.max)return;keep();
  location.href=(d===dp.dataset.today?dp.dataset.base+'/':dp.dataset.base+'/d/'+d+'.html')+'#'+dp.dataset.card;});
 card.querySelectorAll('.evnav a').forEach(function(a){a.addEventListener('click',keep);});
 var W=940,H=330,PL=50,PR=46,PT=24,PB=26, n=P.t.length;
 var X=function(i){return PL+(n>1?i*(W-PL-PR)/(n-1):0);};
 function path(vals,Y){var d='',pen=false;vals.forEach(function(v,i){if(v==null){pen=false;return;}d+=(pen?'L':'M')+X(i).toFixed(1)+','+Y(v).toFixed(1);pen=true;});return d;}
 var placed=[];
 // a label never covers another: moved down (or up near the bottom) in 13 px steps
 function label(x,y,txt,c){var w=txt.length*6.2,k=0;
  while(k<12&&placed.some(function(b){return Math.abs(b.y-y)<13&&x<b.x+b.w&&b.x<x+w;})){y+=(y+13>H-PB-2)?-13*(k+2):13;k++;}
  placed.push({x:x,y:y,w:w});svg.appendChild(el('text',{x:x,y:y,'font-size':11,fill:c},txt));}
 function axes(vmax,unit,right){placed=[];
  for(var k=0;k<=4;k++){var v=vmax*k/4,y=PT+(H-PT-PB)*(1-k/4);svg.appendChild(el('line',{x1:PL,y1:y,x2:W-PR,y2:y,stroke:'#eceef0'}));
   svg.appendChild(el('text',{x:PL-6,y:y+4,'text-anchor':'end','font-size':11,fill:'#80868b'},Math.round(v)));}
  svg.appendChild(el('text',{x:4,y:12,'font-size':11,fill:'#80868b'},unit));
  if(right){for(var k2=0;k2<=4;k2++){var y2=PT+(H-PT-PB)*(1-k2/4);svg.appendChild(el('text',{x:W-PR+6,y:y2+4,'font-size':11,fill:'#b9bec4'},Math.round(right*k2/4)));}
   svg.appendChild(el('text',{x:W-PR-4,y:12,'font-size':11,fill:'#b9bec4'},'W/m²'));}
  P.t.forEach(function(s,i){if(s.slice(3)==='00'){svg.appendChild(el('text',{x:X(i),y:H-8,'text-anchor':'middle','font-size':11,fill:'#80868b'},s));}});
 }
 // v384: the day's sunshine as a yellow area behind the lines, on its own W/m2 scale (right)
 function sun(gmax){var Y=function(v){return PT+(H-PT-PB)*(1-v/gmax);},d='',seg=[];
  function flush(){if(seg.length>1){d+='M'+X(seg[0][0]).toFixed(1)+','+(H-PB)+seg.map(function(q){return 'L'+X(q[0]).toFixed(1)+','+Y(q[1]).toFixed(1);}).join('')+'L'+X(seg[seg.length-1][0]).toFixed(1)+','+(H-PB)+'Z';}seg=[];}
  P.ghi.forEach(function(v,i){if(v==null)flush();else seg.push([i,v]);});flush();
  if(d)svg.appendChild(el('path',{d:d,fill:'#f9d65c',opacity:.28,stroke:'#f2c14e','stroke-width':1}));}
 function sunLeg(){return '<span><svg width="22" height="8"><rect width="22" height="8" fill="#f9d65c" opacity=".5"/></svg> '+tx(['sunshine: station irradiance (right, W/m²)','sol: irradiancia de estación (der., W/m²)'])+'</span>';}
 function mx(arrs){var m=1;arrs.forEach(function(a){a.forEach(function(v){if(v!=null&&v>m)m=v;});});return m*1.08;}
 function clear(){while(svg.firstChild)svg.removeChild(svg.firstChild);leg.innerHTML='';}
 function legItem(color,label,dash){return '<span><svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="'+color+'" stroke-width="2"'+(dash?' stroke-dasharray="5 3"':'')+'/></svg> '+label+'</span>';}
 function drawPlant(){
  var CK=consKw(), vmax=mx([P.plant.ac_kw,P.plant.design_kw].concat(CK?[CK]:[])), gmax=mx([P.ghi]);
  var Y=function(v){return PT+(H-PT-PB)*(1-v/vmax);}, YG=function(v){return PT+(H-PT-PB)*(1-v/gmax);};
  axes(vmax,'kW',gmax); sun(gmax);
  svg.appendChild(el('path',{d:path(P.plant.design_kw,Y),fill:'none',stroke:'#1c2733','stroke-width':1.6,'stroke-dasharray':'7 4'}));
  svg.appendChild(el('path',{d:path(P.plant.ac_kw,Y),fill:'none',stroke:'#2f6fdb','stroke-width':2.2}));
  if(CK)svg.appendChild(el('path',{d:path(CK,Y),fill:'none',stroke:CO,'stroke-width':2.2}));
  leg.innerHTML=legItem('#2f6fdb',tx(['PV generation (AC)','Generación FV (CA)']))+legItem('#1c2733',tx(['Design (EPI model, measured weather)','Diseño (modelo EPI, clima medido)']),1)+
   (CK?legItem(CO,tx(['site consumption (PV + grid import - export)','consumo del sitio (FV + importación - exportación)'])):'')+sunLeg();
  panel.innerHTML=consTiles()+'<table><tr><td>'+tx(['EPI-600 this day','EPI-600 del día'])+'</td><td><b>'+pct(P.plant.epi600_day)+'</b></td></tr><tr><td>'+tx(['EPI-600 month to date','EPI-600 mes a la fecha'])+'</td><td><b>'+pct(P.plant.epi600_mtd)+'</b></td></tr></table>'+
   '<p class="note" style="margin-top:8px"><b>'+tx(['Battery','Batería'])+'</b>: '+tx(['no battery connected. The panel and the battery series activate on their own when a battery is registered.','sin batería conectada. El panel y la serie de batería se activan solas cuando se registre una batería.'])+'</p>';
  sum.innerHTML='';
 }
 // v386: the grid meter (consumption = PV + import - export), only where a meter is validated
 var CO='#e8710a', CN=P.cons||null;
 function consKw(){if(!CN||!CN.grid_kw)return null;var any=false,o=P.plant.ac_kw.map(function(a,i){var g=CN.grid_kw[i];if(g==null)return null;any=true;return Math.round(((a||0)+g)*10)/10;});return any?o:null;}
 function kwh(v){return v==null?'n/a':Math.round(v).toLocaleString('en-US')+' kWh';}
 function sh(v){return v==null?'n/a':Math.round(v*100)+' %';}
 function consTiles(){if(!CN)return '';var d=CN.today||{};
  return '<div class="evtiles"><span><b>'+kwh(d.cons)+'</b>'+tx(['consumption','consumo'])+'</span><span><b>'+sh(d.share)+'</b>'+tx(['solar share','cobertura solar'])+'</span>'+
   '<span><b>'+kwh(d.imp)+'</b>'+tx(['grid import','importación'])+'</span><span><b>'+kwh(d.exp)+'</b>'+tx(['export','exportación'])+'</span>'+
   '<span><b>'+sh(CN.s7)+'</b>'+tx(['solar share, 7 days','cobertura, 7 días'])+'</span><span><b>'+sh(CN.sm)+'</b>'+tx(['solar share, month','cobertura, mes'])+' ('+kwh(CN.cons_m)+')</span></div>'+
   (d.ok===false?'<p class="evflag">'+tx(['This day\'s export is larger than its solar: meter or solar data incomplete, no share shown.','La exportación del día supera la solar: datos incompletos, sin cobertura.'])+'</p>':'');}
 // stacked bars: solar used on site (green) + grid import (grey) up, export (amber) down
 function bars(items,Wb,Hb,every){var L0=50,R0=12,T0=12,B0=24,up=1,dn=0;items.forEach(function(it){var b=it.b;if(b){up=Math.max(up,(b.used||0)+(b.imp||0));dn=Math.max(dn,b.exp||0);}});
  var sp=up+dn,y0=T0+(Hb-T0-B0)*up/sp,sc=(Hb-T0-B0)/sp,sl=(Wb-L0-R0)/Math.max(items.length,1),bw=sl*0.66,s='';
  s+='<line x1="'+L0+'" y1="'+y0.toFixed(1)+'" x2="'+(Wb-R0)+'" y2="'+y0.toFixed(1)+'" stroke="#80868b"/>';
  [0.5,1].forEach(function(f){var y=y0-up*f*sc;s+='<line x1="'+L0+'" y1="'+y.toFixed(1)+'" x2="'+(Wb-R0)+'" y2="'+y.toFixed(1)+'" stroke="#eceef0"/><text x="'+(L0-6)+'" y="'+(y+4).toFixed(1)+'" text-anchor="end" font-size="11" fill="#80868b">'+Math.round(up*f).toLocaleString('en-US')+'</text>';});
  if(dn>0.05*up)s+='<text x="'+(L0-6)+'" y="'+(y0+dn*sc).toFixed(1)+'" text-anchor="end" font-size="11" fill="#80868b">-'+Math.round(dn).toLocaleString('en-US')+'</text>';
  s+='<text x="4" y="11" font-size="11" fill="#80868b">kWh</text>';
  items.forEach(function(it,k){var x=L0+k*sl+(sl-bw)/2,b=it.b;
   if(b){var u=Math.max(b.used||0,0),im=Math.max(b.imp||0,0),ex=Math.max(b.exp||0,0),op=it.dim?' opacity="0.35"':'';
    s+='<g'+op+'><title>'+it.lab+': '+tx(['solar used ','solar consumida '])+Math.round(u)+' kWh, '+tx(['import ','importación '])+Math.round(im)+', '+tx(['export ','exportación '])+Math.round(ex)+(b.share!=null?', '+tx(['share ','cobertura '])+sh(b.share):'')+'</title>'+
     '<rect x="'+x.toFixed(1)+'" y="'+(y0-u*sc).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+(u*sc).toFixed(1)+'" fill="#2e9e5b"/>'+
     '<rect x="'+x.toFixed(1)+'" y="'+(y0-(u+im)*sc).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+(im*sc).toFixed(1)+'" fill="#9aa3ab"/>'+
     '<rect x="'+x.toFixed(1)+'" y="'+y0.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+(ex*sc).toFixed(1)+'" fill="#f0a03a"/></g>';}
   if(k%every===0)s+='<text x="'+(x+bw/2).toFixed(1)+'" y="'+(Hb-7)+'" text-anchor="middle" font-size="10.5" fill="#80868b">'+it.lab+'</text>';});
  return s;}
 function consLeg(){var sw=function(c){return '<svg width="12" height="10"><rect width="10" height="10" rx="2" fill="'+c+'"/></svg> ';};
  return '<span>'+sw('#2e9e5b')+tx(['solar used on site','solar consumida en sitio'])+'</span><span>'+sw('#9aa3ab')+tx(['grid import','importación de la red'])+'</span><span>'+sw('#f0a03a')+tx(['export to the grid (below the axis)','exportación a la red (bajo el eje)'])+'</span>';}
 function drawCons(){if(!CN)return;
  var items=CN.days.map(function(d){return {lab:d.d.slice(5),b:d.cons!=null?d:null,dim:d.ok===false};});
  var g=document.createElementNS('http://www.w3.org/2000/svg','g');g.innerHTML=bars(items,W,H,2);svg.appendChild(g);
  leg.innerHTML='<b>'+tx(['Last 30 days, kWh per day','Últimos 30 días, kWh por día'])+'</b> '+consLeg();
  panel.innerHTML=consTiles()+'<p class="note">'+tx(['Consumption = solar + grid import - export (the plant\'s grid meter read through the inverter vendor\'s portal, solar from the inverters). Solar share = solar used on site / consumption; several days = ratio of the sums. A CFE bill is the reference for billing; this view is for operations.','Consumo = solar + importación - exportación (medidor de red de la planta leído por el portal del fabricante, solar de los inversores). Cobertura solar = solar consumida en sitio / consumo; varios días = cociente de las sumas. El recibo de CFE es la referencia para facturación; esta vista es para operación.'])+'</p>';
  var hs=CN.hours.map(function(h,i){return {lab:(i<10?'0':'')+i,b:h};});
  sum.innerHTML=(CN.hours.some(function(h){return h;})?'<h3 class="evh3">'+tx(['This day by hour, kWh','Este día por hora, kWh'])+'</h3><svg viewBox="0 0 940 210" style="width:100%;height:auto;display:block">'+bars(hs,940,210,2)+'</svg>':'');
 }
 function drawInverters(){
  var all=[];P.inverters.forEach(function(v){if(!hidden[v.inv]){all.push(v.w);all.push(v.design);}});
  var vmax=mx(all.length?all:[[1]]), Y=function(v){return PT+(H-PT-PB)*(1-v/vmax);}, gmax=mx([P.ghi]);
  axes(vmax,'W/kWp',gmax); sun(gmax);
  P.inverters.forEach(function(v,k){if(hidden[v.inv])return;var c=C[k%C.length];
   v.gaps.forEach(function(g){svg.appendChild(el('rect',{x:X(g.i0)-2,y:PT,width:Math.max(X(g.i1)-X(g.i0)+4,4),height:H-PT-PB,fill:c,opacity:.10}));});
   svg.appendChild(el('path',{d:path(v.design,Y),fill:'none',stroke:c,'stroke-width':1.2,'stroke-dasharray':'5 4',opacity:.75}));
   svg.appendChild(el('path',{d:path(v.w,Y),fill:'none',stroke:c,'stroke-width':2}));
   v.gaps.forEach(function(g){var i=P.t.indexOf(g.at);if(i<0||v.w[i]==null)return;var x=X(i),y=Y(v.w[i]);
    svg.appendChild(el('circle',{cx:x,cy:y,r:4,fill:'none',stroke:c,'stroke-width':2}));
    label(Math.min(x+6,W-PR-170),Math.min(y+16,H-PB-4),v.name+' '+g.pct_peers+'% '+tx(['vs peers','vs pares'])+' '+g.from+'-'+g.to,c);});
  });
  leg.innerHTML=P.inverters.map(function(v,k){return legItem(C[k%C.length],v.name);}).join('')+legItem('#80868b',tx(['design of each inverter','diseño de cada inversor']),1)+sunLeg();
  var rows=P.inverters.map(function(v,k){var c=C[k%C.length];
   return '<button type="button" data-inv="'+v.inv+'" class="'+(hidden[v.inv]?'off':'')+'"><span class="evsw" style="background:'+c+'"></span><b>'+v.name+'</b><br>'+
    tx(['strings ','cadenas '])+v.producing+' / '+v.n_strings+' · EPI-600 '+pct(v.epi600_mtd)+' ('+tx(['MTD','mes'])+') · '+pct(v.epi600_day)+' ('+tx(['day','día'])+')<br>Avail-150 '+pct(v.avail_mtd)+
    (v.gaps.length?' · <span class="evflag">'+v.gaps.length+' '+tx(['gap(s)','hueco(s)'])+'</span>':'')+(v.from_ac.length?' · '+tx(['AC used ','CA usada '])+v.from_ac.length+'×':'')+'</button>';}).join('');
  var TI=(P.tk||{}).i||{}, tks=P.inverters.filter(function(v){return TI[v.inv];}).map(function(v){var g=v.gaps.reduce(function(a,b){return b.pct_peers<a.pct_peers?b:a;},v.gaps[0]);
   return '<tr><td><b>'+v.name+'</b> '+g.pct_peers+'% '+tx(['vs peers','vs pares'])+' '+g.from+'-'+g.to+(v.gaps.length>1?' (+'+(v.gaps.length-1)+')':'')+'</td><td>'+tkcell(TI[v.inv])+'</td></tr>';}).join('');
  panel.innerHTML='<div class="note">'+tx(['Click an inverter to hide or show it.','Haga clic en un inversor para ocultarlo o mostrarlo.'])+'</div><div class="evinv">'+rows+'</div>'+(tks?'<table class="evtks">'+tks+'</table>':'');
  panel.querySelectorAll('[data-inv]').forEach(function(b){b.onclick=function(){hidden[b.dataset.inv]=!hidden[b.dataset.inv];draw();};});
  sum.innerHTML='';
 }
 function drawStrings(){
  var S=P.strings[sel]; if(!S){return;}
  var all=[S.hi].concat(S.w), vmax=mx(all), Y=function(v){return PT+(H-PT-PB)*(1-v/vmax);}, gmax=mx([P.ghi]);
  axes(vmax,'W/kWp',gmax); sun(gmax);
  var up='',dn='';for(var i=0;i<n;i++){if(S.hi[i]==null||S.lo[i]==null)continue;up+=(up?'L':'M')+X(i).toFixed(1)+','+Y(S.hi[i]).toFixed(1);}
  for(var j=n-1;j>=0;j--){if(S.hi[j]==null||S.lo[j]==null)continue;dn+='L'+X(j).toFixed(1)+','+Y(S.lo[j]).toFixed(1);}
  if(up)svg.appendChild(el('path',{d:up+dn+'Z',fill:'#dfe3e8',opacity:.8}));
  var out={};S.out.forEach(function(o){out[o.uid]=o;});
  S.uids.forEach(function(u,k){if(out[u])return;var z=S.zero.indexOf(u)>=0;svg.appendChild(el('path',{d:path(S.w[k],Y),fill:'none',stroke:z?'#5b0f1e':'#aab1b9','stroke-width':z?1.6:.8,'stroke-dasharray':z?'4 3':'',opacity:.85}));});
  svg.appendChild(el('path',{d:path(S.median,Y),fill:'none',stroke:'#1c2733','stroke-width':2.4}));
  S.out.forEach(function(o){var k=S.uids.indexOf(o.uid),c=o.pct<0?'#d64545':'#2f6fdb';
   svg.appendChild(el('path',{d:path(S.w[k],Y),fill:'none',stroke:c,'stroke-width':1.8}));
   var v=S.w[k][o.i];if(v!=null){label(Math.min(X(o.i)+5,W-PR-120),Math.max(Y(v)-6,PT+10),S.labels[k]+' ('+(o.pct>0?'+':'')+o.pct+'%)',c);}});
  leg.innerHTML=sunLeg()+'<span><svg width="22" height="8"><rect width="22" height="8" fill="#dfe3e8"/></svg> '+tx(['band of the strings','banda de las cadenas'])+'</span>'+legItem('#1c2733',tx(['median of the inverter','mediana del inversor']))+legItem('#d64545',tx(['below the band','bajo la banda']))+legItem('#2f6fdb',tx(['above the band','sobre la banda']))+
   (S.zero.length?legItem('#5b0f1e',tx(['no output','sin salida']),1):'')+(S.mixed_planes?'<span>'+tx(['(strings on different roof planes: colours use each string\'s own design; the band is drawn from the raw values)','(cadenas en planos distintos: los colores usan el diseño propio de cada cadena; la banda se dibuja con los valores medidos)'])+'</span>':'');
  var btns=P.inverters.map(function(v,k){return '<button type="button" data-sel="'+v.inv+'" class="'+(v.inv===sel?'on':'')+'"><span class="evsw" style="background:'+C[k%C.length]+'"></span>'+v.name+' · '+pct(v.epi600_mtd)+'</button>';}).join('');
  var wo=function(u){var k=S.uids.indexOf(u);return S.wo&&S.wo[k]?'<br><span class="note">'+S.wo[k]+'</span>':'';};
  var TK=(P.tk||{}).s||{};
  var tkl=function(u){var c=tkcell(TK[u]);return c?'<div class="evtkrow">'+c+'</div>':'';};
  var lst=S.out.map(function(o){return '<tr><td>'+o.label+wo(o.uid)+tkl(o.uid)+'</td><td>'+(o.pct>0?'+':'')+o.pct+'%</td><td>'+o.minutes+' min</td></tr>';}).join('');
  var zl=S.zero.map(function(u){return '<tr><td class="evflag">'+S.labels[S.uids.indexOf(u)]+wo(u)+tkl(u)+'</td><td colspan="2" class="evflag">'+tx(['no output','sin salida'])+'</td></tr>';}).join('');
  panel.innerHTML='<div class="evinv">'+btns+'</div>'+(lst||zl?'<table>'+zl+lst+'</table>':'<p class="note">'+tx(['All strings inside the band.','Todas las cadenas dentro de la banda.'])+'</p>');
  panel.querySelectorAll('[data-sel]').forEach(function(b){b.onclick=function(){sel=b.dataset.sel;keep();draw();};});
  sum.innerHTML='';
 }
 // v384: EPI-600 month to date by string, one chart per inverter (Arturo's mock-up, spec section 8.4)
 function byStrings(){var box=card.querySelector('.evby');if(!box)return;var out=['<h3 class="evh3">'+tx(['EPI-600 by string and inverter, month to date','EPI-600 por cadena e inversor, mes a la fecha'])+'</h3>'+
   '<p class="note">'+tx(['Each dot is one string; the band behind it is the target band. A cross is a string with no output this month.','Cada punto es una cadena; la banda detrás es la banda objetivo. Una cruz es una cadena sin salida este mes.'])+'</p>'];
  P.inverters.forEach(function(v){var S=P.strings[v.inv];if(!S)return;var m=S.uids.length;
   var vals=S.uids.filter(function(u){return S.idle_mtd.indexOf(u)<0;}).map(function(u){return S.epi600_mtd[u];});
   var top=Math.max(1.0,(Math.ceil(Math.max.apply(null,vals.concat([1]))*100)+2)/100), bot=Math.min(0.85,(Math.floor(Math.min.apply(null,vals.concat([0.85]))*100)-2)/100);
   var Wc=Math.max(1400,m*46+120),Hc=170,L=46,R=110,T=10,B=26,Yc=function(e){return T+(Hc-T-B)*(1-(Math.min(Math.max(e,bot),top)-bot)/(top-bot));},Xc=function(k){return L+(k+0.5)*(Wc-L-R)/m;};
   var s='<svg viewBox="0 0 '+Wc+' '+Hc+'" class="evbysvg" preserveAspectRatio="xMidYMid meet">';
   [[0.97,top,'#e6f4ea',tx(['green: 97 % or more','verde: 97 % o más']),'#1e8e3e'],[0.93,0.97,'#fef7e0',tx(['amber: 93 to 97 %','ámbar: 93 a 97 %']),'#b26a00'],[bot,0.93,'#fce8e6',tx(['red: below 93 %','rojo: bajo 93 %']),'#c5221f']].forEach(function(b){
    if(b[1]<=bot)return;var y0=Yc(b[1]),y1=Yc(Math.max(b[0],bot));s+='<rect x="'+L+'" y="'+y0.toFixed(1)+'" width="'+(Wc-L)+'" height="'+(y1-y0).toFixed(1)+'" fill="'+b[2]+'"/><text x="'+(Wc-6)+'" y="'+(y0+13).toFixed(1)+'" text-anchor="end" font-size="10.5" fill="'+b[4]+'">'+b[3]+'</text>';});
   [bot,0.93,0.97,1.0].concat(top>1.0?[top]:[]).forEach(function(e){s+='<text x="'+(L-6)+'" y="'+(Yc(e)+4).toFixed(1)+'" text-anchor="end" font-size="10" fill="#80868b">'+Math.round(e*100)+'</text>';});
   s+='<text x="12" y="'+((Hc-B+T)/2)+'" font-size="10" fill="#80868b" transform="rotate(-90 12 '+((Hc-B+T)/2)+')" text-anchor="middle">EPI-600 (%)</text>';
   S.uids.forEach(function(u,k){var e=S.epi600_mtd[u],x=Xc(k),idle=S.idle_mtd.indexOf(u)>=0,lab=S.labels[k].split(':').pop();
    s+='<text x="'+x.toFixed(1)+'" y="'+(Hc-8)+'" text-anchor="middle" font-size="10" fill="#5f6368">'+lab+'</text>';
    if(idle){var y=Yc(bot)-8-(k>0&&S.idle_mtd.indexOf(S.uids[k-1])>=0&&(k%2)?13:0);s+='<text x="'+x.toFixed(1)+'" y="'+(y+5).toFixed(1)+'" text-anchor="middle" font-size="15" font-weight="700" fill="#c5221f">✕</text><text x="'+x.toFixed(1)+'" y="'+(y-10).toFixed(1)+'" text-anchor="middle" font-size="9.5" fill="#c5221f">'+(e==null?tx(['no data','sin datos']):tx(['no output','sin salida']))+'</text>';return;}
    var col=e>=0.97?'#1e8e3e':e>=0.93?'#e8a317':'#d93025',y=Yc(e);
    s+='<line x1="'+x.toFixed(1)+'" y1="'+(Hc-B)+'" x2="'+x.toFixed(1)+'" y2="'+y.toFixed(1)+'" stroke="#dadce0"/><circle cx="'+x.toFixed(1)+'" cy="'+y.toFixed(1)+'" r="5.5" fill="'+col+'"><title>'+S.labels[k]+(S.wo&&S.wo[k]?' · '+S.wo[k]:'')+': '+(e*100).toFixed(1)+' %</title></circle>'+
     '<text x="'+x.toFixed(1)+'" y="'+(y-9).toFixed(1)+'" text-anchor="middle" font-size="10" fill="#3c4043">'+Math.round(e*100)+'</text>';});
   s+='</svg>';
   out.push('<div class="evbyinv"><div class="evbyh"><b>'+v.name+'</b> &nbsp;|&nbsp; '+v.producing_mtd+' '+tx(['of','de'])+' '+m+' '+tx(['strings producing','cadenas produciendo'])+' &nbsp;|&nbsp; EPI-600 <b>'+pct(v.epi600_mtd)+'</b> ('+pct(v.epi600_mtd_producing)+' '+tx(['on producing strings','en cadenas que producen'])+')</div>'+s+'</div>');});
  box.innerHTML=out.join('');}
 // v384: week (7 days) and month (30 days) - daily EPI-600 per inverter over the target bands, the day's
 // insolation as yellow bars (right, kWh/m2), and a strings x days map; a day not computed yet is empty
 function drawPeriod(nd){var Hh=P.hist;if(!Hh){panel.innerHTML='<p class="note">'+tx(['No history yet.','Aún sin historial.'])+'</p>';return;}
  var a=Math.max(0,Hh.days.length-nd), D=Hh.days.slice(a), m=D.length, sl=function(arr){return arr.slice(a);};
  var Xd=function(i){return PL+(i+0.5)*(W-PL-PR)/m;}, bw=(W-PL-PR)/m;
  var vals=[];sl(Hh.plant).forEach(function(v){if(v!=null)vals.push(v);});P.inverters.forEach(function(v){sl(Hh.inv[v.inv]||[]).forEach(function(e){if(e!=null)vals.push(e);});});
  var lo=Math.min(0.85,(Math.floor(Math.min.apply(null,vals.concat([0.85]))*100)-2)/100), hi=Math.max(1.05,(Math.ceil(Math.max.apply(null,vals.concat([1.0]))*100)+2)/100);
  var Y=function(e){return PT+(H-PT-PB)*(1-(Math.min(Math.max(e,lo),hi)-lo)/(hi-lo));};
  var ins=sl(Hh.insol), imax=Math.max.apply(null,ins.filter(function(v){return v!=null;}).concat([1]))*1.15, YI=function(v){return PT+(H-PT-PB)*(1-v/imax);};
  [[0.97,hi,'#e6f4ea'],[0.93,0.97,'#fef7e0'],[lo,0.93,'#fce8e6']].forEach(function(b){svg.appendChild(el('rect',{x:PL,y:Y(b[1]),width:W-PL-PR,height:Y(b[0])-Y(b[1]),fill:b[2]}));});
  ins.forEach(function(v,i){if(v==null)return;svg.appendChild(el('rect',{x:Xd(i)-bw*0.32,y:YI(v),width:bw*0.64,height:H-PB-YI(v),fill:'#f9d65c',opacity:.45}));});
  [lo,0.93,0.97,1.0,hi].forEach(function(e){var y=Y(e);svg.appendChild(el('line',{x1:PL,x2:W-PR,y1:y,y2:y,stroke:'#e3e6ea'}));svg.appendChild(el('text',{x:PL-6,y:y+4,'text-anchor':'end','font-size':11,fill:'#80868b'},Math.round(e*100)));});
  for(var k2=0;k2<=4;k2++){var v2=imax*k2/4;svg.appendChild(el('text',{x:W-PR+6,y:YI(v2)+4,'font-size':11,fill:'#b9a04e'},v2.toFixed(1)));}
  svg.appendChild(el('text',{x:4,y:12,'font-size':11,fill:'#80868b'},'EPI-600 %'));svg.appendChild(el('text',{x:W-PR-4,y:12,'font-size':11,fill:'#b9a04e'},'kWh/m²'));
  D.forEach(function(d,i){if(m<=10||i%2===0||i===m-1)svg.appendChild(el('text',{x:Xd(i),y:H-8,'text-anchor':'middle','font-size':10.5,fill:'#80868b'},(m<=10?d.slice(5):d.slice(8))));});
  function pl(arr,c,wd,dash){var dd='',pen=false;arr.forEach(function(v,i){if(v==null){pen=false;return;}dd+=(pen?'L':'M')+Xd(i).toFixed(1)+','+Y(v).toFixed(1);pen=true;});
   if(dd)svg.appendChild(el('path',{d:dd,fill:'none',stroke:c,'stroke-width':wd,'stroke-dasharray':dash||''}));
   arr.forEach(function(v,i){if(v!=null)svg.appendChild(el('circle',{cx:Xd(i),cy:Y(v),r:wd>2?3.2:2.4,fill:c}));});}
  P.inverters.forEach(function(v,k){if(!hidden[v.inv])pl(sl(Hh.inv[v.inv]||[]),C[k%C.length],1.6);});
  pl(sl(Hh.plant),'#1c2733',2.6);
  leg.innerHTML=legItem('#1c2733',tx(['plant','planta']))+P.inverters.map(function(v,k){return legItem(C[k%C.length],v.name);}).join('')+
   '<span><svg width="22" height="8"><rect width="22" height="8" fill="#f9d65c" opacity=".6"/></svg> '+tx(['sunshine: insolation of the day (right)','sol: insolación del día (der.)'])+'</span>';
  // period figures: a ratio of the sums of the days shown (not an average of daily ratios)
  var rows=P.inverters.map(function(v,k){var arr=sl(Hh.inv[v.inv]||[]),red=arr.filter(function(e){return e!=null&&e<0.93;}).length,ok=arr.filter(function(e){return e!=null;}).length;
   var ps=(nd===7?Hh.sum7:Hh.sum)||{};
   return '<button type="button" data-inv="'+v.inv+'" class="'+(hidden[v.inv]?'off':'')+'"><span class="evsw" style="background:'+C[k%C.length]+'"></span><b>'+v.name+'</b><br>'+
    'EPI-600 '+(nd===7?tx(['week ','semana ']):tx(['30 days ','30 días ']))+'<b>'+pct(ps[v.inv])+'</b> · '+tx(['days below 93 %: ','días bajo 93 %: '])+red+' '+tx(['of','de'])+' '+ok+'</button>';}).join('');
  var got=D.length-sl(Hh.plant).filter(function(e){return e==null;}).length;
  var pp=(nd===7?Hh.sum7:Hh.sum)||{};
  panel.innerHTML='<table><tr><td>'+tx(['Plant EPI-600, ','EPI-600 de la planta, '])+(nd===7?tx(['last 7 days','últimos 7 días']):tx(['last 30 days','últimos 30 días']))+'</td><td><b>'+pct(pp.plant)+'</b></td></tr></table><div class="note">'+tx(['Days with a value: ','Días con valor: '])+got+' '+tx(['of','de'])+' '+m+'. '+tx(['A day without a value had no valid sample at 600 W/m² or more, or is still being computed.','Un día sin valor no tuvo muestra válida con 600 W/m² o más, o aún se está calculando.'])+'</div><div class="evinv">'+rows+'</div>';
  panel.querySelectorAll('[data-inv]').forEach(function(b){b.onclick=function(){hidden[b.dataset.inv]=!hidden[b.dataset.inv];draw();};});
  // strings x days map, every inverter
  var cw=Math.max(10,Math.min(28,Math.floor(820/m))), s='<div class="note">'+tx(['Each string by day: daily EPI-600 (green 97 % or more, amber 93-97 %, red below 93 %, grey no value, dark red below 5 % = no output). A whole column in red is the whole plant that day (clouds over the station, a sensor) - a row that stays red is the string.','Cada cadena por día: EPI-600 diario (verde 97 % o más, ámbar 93-97 %, rojo bajo 93 %, gris sin valor, rojo oscuro bajo 5 % = sin salida). Una columna completa en rojo es toda la planta ese día (nubes sobre la estación, un sensor) - una fila que sigue en rojo es la cadena.'])+'</div>';
  P.inverters.forEach(function(v){var S=P.strings[v.inv];if(!S)return;var Wm=110+m*cw,Hm=14+S.uids.length*13;
   var g='<svg viewBox="0 0 '+Wm+' '+Hm+'" width="'+Wm+'" style="max-width:100%;height:auto;display:block;margin:6px 0 2px"><text x="0" y="10" font-size="11" font-weight="700" fill="#1c2733">'+v.name+'</text>';
   S.uids.forEach(function(u,r){var arr=sl(Hh.str[u]||[]),y=14+r*13;g+='<text x="104" y="'+(y+10)+'" text-anchor="end" font-size="9.5" fill="#5f6368">'+S.labels[r].split(':').pop()+'</text>';
    arr.forEach(function(e,i){var c=e==null?'#e8eaed':e<0.05?'#5b0f1e':e>=0.97?'#34a853':e>=0.93?'#f4b400':'#ea4335';
     g+='<rect x="'+(110+i*cw)+'" y="'+y+'" width="'+(cw-1.5)+'" height="11.5" rx="2" fill="'+c+'"><title>'+S.labels[r]+' '+D[i]+': '+(e==null?'n/a':(e*100).toFixed(1)+' %')+'</title></rect>';});});
   s+=g+'</svg>';});
  sum.innerHTML=s;
 }
 function draw(){clear();card.querySelectorAll('.evseg button').forEach(function(b){b.classList.toggle('on',b.dataset.v===view);});
  if(view==='p')drawPlant();else if(view==='i')drawInverters();else if(view==='s')drawStrings();else if(view==='c')drawCons();else drawPeriod(view==='w'?7:30);}
 card.querySelectorAll('.evseg button').forEach(function(b){b.onclick=function(){view=b.dataset.v;keep();draw();};});
 byStrings();
 svg.addEventListener('mousemove',function(ev){var r=svg.getBoundingClientRect(),xr=(ev.clientX-r.left)/r.width*W;
  if(view==='c'){tip.style.display='none';return;}
  if(view==='w'||view==='m'){var Hh=P.hist;if(!Hh){return;}var nd=view==='w'?7:30,a0=Math.max(0,Hh.days.length-nd),m=Hh.days.length-a0,j=Math.floor((xr-PL)/((W-PL-PR)/m));
   if(j<0||j>=m){tip.style.display='none';return;}var q=a0+j,hh='<b>'+Hh.days[q]+'</b><br>'+tx(['insolation ','insolación '])+(Hh.insol[q]==null?'n/a':Hh.insol[q]+' kWh/m²')+'<br>'+tx(['plant ','planta '])+pct(Hh.plant[q]);
   P.inverters.forEach(function(v){if(!hidden[v.inv])hh+='<br>'+v.name+': '+pct((Hh.inv[v.inv]||[])[q]);});
   tip.innerHTML=hh;tip.style.display='block';tip.style.left=(ev.clientX+14)+'px';tip.style.top=(ev.clientY+10)+'px';return;}
  var i=Math.round((xr-PL)/((W-PL-PR)/Math.max(n-1,1)));
  if(i<0||i>=n){tip.style.display='none';return;}var h='<b>'+P.t[i]+'</b>';
  if(view==='p'){h+='<br>'+tx(['AC ','CA '])+(P.plant.ac_kw[i]==null?'n/a':P.plant.ac_kw[i]+' kW')+'<br>'+tx(['design ','diseño '])+(P.plant.design_kw[i]==null?'n/a':P.plant.design_kw[i]+' kW')+'<br>GHI '+(P.ghi[i]==null?'n/a':P.ghi[i]+' W/m²');
   var ck=consKw();if(ck&&ck[i]!=null){var gk=CN.grid_kw[i];h+='<br>'+tx(['consumption ','consumo '])+ck[i]+' kW<br>'+(gk>=0?tx(['grid import ','importación '])+gk:tx(['export ','exportación '])+(-gk))+' kW';}}
  else if(view==='i'){P.inverters.forEach(function(v){if(!hidden[v.inv])h+='<br>'+v.name+': '+(v.w[i]==null?'n/a':v.w[i])+' / '+(v.design[i]==null?'n/a':v.design[i])+' W/kWp';});}
  else{var S=P.strings[sel];if(S){h+='<br>'+tx(['median ','mediana '])+(S.median[i]==null?'n/a':S.median[i])+' W/kWp';S.out.forEach(function(o){var k=S.uids.indexOf(o.uid);h+='<br>'+o.label+': '+(S.w[k][i]==null?'n/a':S.w[k][i]);});}}
  tip.innerHTML=h;tip.style.display='block';tip.style.left=(ev.clientX+14)+'px';tip.style.top=(ev.clientY+10)+'px';});
 svg.addEventListener('mouseleave',function(){tip.style.display='none';});
 document.querySelectorAll('.lang-btn,[data-l]').forEach(function(b){b.addEventListener('click',function(){setTimeout(draw,0);});});
 draw();
});
})();
'''


def card(pk: str, p: dict, live: bool = False, nav: dict = None) -> str:
    p = {k: v for k, v in p.items() if k != 'tallies'}        # the page draws; it does not need the sums
    return render(pk, p, live, nav) + f'<style>{CSS}</style><script>{JS}</script>'


def pending_card() -> str:
    """A past day not computed yet (at most a few per plant are computed per run)."""
    return ('<div class="card"><h2 data-en="Troubleshooting: plant, inverters, strings" '
            'data-es="Diagnóstico: planta, inversores, cadenas">Troubleshooting: plant, inverters, strings</h2>'
            + t('This day is queued for computation and appears within a few generator runs.',
                'Este día está en cola de cálculo y aparece en unas pocas corridas del generador.', 'p') + '</div>')
