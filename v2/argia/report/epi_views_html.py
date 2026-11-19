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


def _pct(x):
    return 'n/a' if x is None else f'{x * 100:.1f} %'


def render(pk: str, p: dict, live: bool) -> str:
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
            f'<div class="note">{t("this day", "este día")}: {_pct(pl.get("epi600_day"))}</div></div>')
    caveat = t('Bands are provisional: the station sensors are not yet validated (they read 7 to 21 % below satellite). '
               'Comparisons inside the plant - inverter with inverter, string with string - use the same sensor and are reliable.',
               'Las bandas son provisionales: los sensores de las estaciones aún no están validados (leen 7 a 21 % menos que el satélite). '
               'Las comparaciones dentro de la planta - inversor contra inversor, cadena contra cadena - usan el mismo sensor y son confiables.')
    seg = ('<div class="evseg">' + ''.join(
        f'<button type="button" data-v="{v}" data-en="{en}" data-es="{es}">{en}</button>'
        for v, en, es in (('p', 'Plant', 'Planta'), ('i', 'Inverters', 'Inversores'), ('s', 'Strings', 'Cadenas'))) + '</div>')
    return f'''<div class="card evcard" id="ev{pk}" data-colors='{_embed(INV_COLORS)}'>
<h2 data-en="Troubleshooting: plant, inverters, strings" data-es="Diagnóstico: planta, inversores, cadenas">Troubleshooting: plant, inverters, strings</h2>
{head}
<div class="evmain"><div class="evchart"><svg class="evsvg" viewBox="0 0 940 330" preserveAspectRatio="none"></svg><div class="evleg"></div></div>
<div class="evside">{seg}<div class="evpanel"></div></div></div>
<div class="evsum"></div>
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
.evmain{display:flex;gap:12px;align-items:flex-start}.evchart{flex:1;min-width:0}.evside{width:230px;flex-shrink:0}
.evsvg{width:100%;height:330px;display:block}.evseg{display:flex;flex-direction:column;gap:4px;margin-bottom:8px}
.evseg button,.evinv button{border:1px solid #d6dbe1;background:#fff;border-radius:6px;padding:5px 8px;text-align:left;cursor:pointer;font:inherit}
.evseg button.on{background:#1c2733;color:#fff;border-color:#1c2733}.evinv button.on{border-color:#1c2733;box-shadow:inset 3px 0 0 #1c2733}.evinv button.off{opacity:.45}
.evpanel{font-size:12.5px}.evpanel table{width:100%;border-collapse:collapse}.evpanel td{padding:3px 2px;border-bottom:1px solid #eceef0}
.evinv{display:flex;flex-direction:column;gap:3px;margin:6px 0}.evsw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.evleg{font-size:12px;color:#5f6368;margin-top:4px}.evleg span{margin-right:12px}
.evsum{margin-top:8px}.evsum svg{width:100%;height:auto}
.evtip{position:fixed;display:none;background:#1c2733;color:#fff;padding:6px 8px;border-radius:6px;font-size:12px;pointer-events:none;z-index:50;max-width:280px}
.evflag{color:#c5221f;font-weight:600}
@media(max-width:820px){.evmain{flex-direction:column}.evside{width:100%}}
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
 function mx(arrs){var m=1;arrs.forEach(function(a){a.forEach(function(v){if(v!=null&&v>m)m=v;});});return m*1.08;}
 function clear(){while(svg.firstChild)svg.removeChild(svg.firstChild);leg.innerHTML='';}
 function legItem(color,label,dash){return '<span><svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="'+color+'" stroke-width="2"'+(dash?' stroke-dasharray="5 3"':'')+'/></svg> '+label+'</span>';}
 function drawPlant(){
  var vmax=mx([P.plant.ac_kw,P.plant.design_kw]), gmax=mx([P.ghi]);
  var Y=function(v){return PT+(H-PT-PB)*(1-v/vmax);}, YG=function(v){return PT+(H-PT-PB)*(1-v/gmax);};
  axes(vmax,'kW',gmax);
  var g=path(P.ghi,YG); if(g)svg.appendChild(el('path',{d:g,fill:'none',stroke:'#f2c14e','stroke-width':1.4,opacity:.8}));
  svg.appendChild(el('path',{d:path(P.plant.design_kw,Y),fill:'none',stroke:'#1c2733','stroke-width':1.6,'stroke-dasharray':'7 4'}));
  svg.appendChild(el('path',{d:path(P.plant.ac_kw,Y),fill:'none',stroke:'#2f6fdb','stroke-width':2.2}));
  leg.innerHTML=legItem('#2f6fdb',tx(['PV generation (AC)','Generación FV (CA)']))+legItem('#1c2733',tx(['Design (EPI model, measured weather)','Diseño (modelo EPI, clima medido)']),1)+legItem('#f2c14e',tx(['Station irradiance (right)','Irradiancia de estación (der.)']));
  panel.innerHTML='<table><tr><td>'+tx(['EPI-600 this day','EPI-600 del día'])+'</td><td><b>'+pct(P.plant.epi600_day)+'</b></td></tr><tr><td>'+tx(['EPI-600 month to date','EPI-600 mes a la fecha'])+'</td><td><b>'+pct(P.plant.epi600_mtd)+'</b></td></tr></table>'+
   '<p class="note" style="margin-top:8px"><b>'+tx(['Battery','Batería'])+'</b>: '+tx(['no battery connected. The panel and the battery series activate on their own when a battery is registered.','sin batería conectada. El panel y la serie de batería se activan solas cuando se registre una batería.'])+'</p>';
  sum.innerHTML='';
 }
 function drawInverters(){
  var all=[];P.inverters.forEach(function(v){if(!hidden[v.inv]){all.push(v.w);all.push(v.design);}});
  var vmax=mx(all.length?all:[[1]]), Y=function(v){return PT+(H-PT-PB)*(1-v/vmax);};
  axes(vmax,'W/kWp');
  P.inverters.forEach(function(v,k){if(hidden[v.inv])return;var c=C[k%C.length];
   v.gaps.forEach(function(g){svg.appendChild(el('rect',{x:X(g.i0)-2,y:PT,width:Math.max(X(g.i1)-X(g.i0)+4,4),height:H-PT-PB,fill:c,opacity:.10}));});
   svg.appendChild(el('path',{d:path(v.design,Y),fill:'none',stroke:c,'stroke-width':1.2,'stroke-dasharray':'5 4',opacity:.75}));
   svg.appendChild(el('path',{d:path(v.w,Y),fill:'none',stroke:c,'stroke-width':2}));
   v.gaps.forEach(function(g){var i=P.t.indexOf(g.at);if(i<0||v.w[i]==null)return;var x=X(i),y=Y(v.w[i]);
    svg.appendChild(el('circle',{cx:x,cy:y,r:4,fill:'none',stroke:c,'stroke-width':2}));
    label(Math.min(x+6,W-PR-170),Math.min(y+16,H-PB-4),v.name+' '+g.pct_peers+'% '+tx(['vs peers','vs pares'])+' '+g.from+'-'+g.to,c);});
  });
  leg.innerHTML=P.inverters.map(function(v,k){return legItem(C[k%C.length],v.name);}).join('')+legItem('#80868b',tx(['design of each inverter','diseño de cada inversor']),1);
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
  var all=[S.hi].concat(S.w), vmax=mx(all), Y=function(v){return PT+(H-PT-PB)*(1-v/vmax);};
  axes(vmax,'W/kWp');
  var up='',dn='';for(var i=0;i<n;i++){if(S.hi[i]==null||S.lo[i]==null)continue;up+=(up?'L':'M')+X(i).toFixed(1)+','+Y(S.hi[i]).toFixed(1);}
  for(var j=n-1;j>=0;j--){if(S.hi[j]==null||S.lo[j]==null)continue;dn+='L'+X(j).toFixed(1)+','+Y(S.lo[j]).toFixed(1);}
  if(up)svg.appendChild(el('path',{d:up+dn+'Z',fill:'#dfe3e8',opacity:.8}));
  var out={};S.out.forEach(function(o){out[o.uid]=o;});
  S.uids.forEach(function(u,k){if(out[u])return;var z=S.zero.indexOf(u)>=0;svg.appendChild(el('path',{d:path(S.w[k],Y),fill:'none',stroke:z?'#5b0f1e':'#aab1b9','stroke-width':z?1.6:.8,'stroke-dasharray':z?'4 3':'',opacity:.85}));});
  svg.appendChild(el('path',{d:path(S.median,Y),fill:'none',stroke:'#1c2733','stroke-width':2.4}));
  S.out.forEach(function(o){var k=S.uids.indexOf(o.uid),c=o.pct<0?'#d64545':'#2f6fdb';
   svg.appendChild(el('path',{d:path(S.w[k],Y),fill:'none',stroke:c,'stroke-width':1.8}));
   var v=S.w[k][o.i];if(v!=null){label(Math.min(X(o.i)+5,W-PR-120),Math.max(Y(v)-6,PT+10),S.labels[k]+' ('+(o.pct>0?'+':'')+o.pct+'%)',c);}});
  leg.innerHTML='<span><svg width="22" height="8"><rect width="22" height="8" fill="#dfe3e8"/></svg> '+tx(['band of the strings','banda de las cadenas'])+'</span>'+legItem('#1c2733',tx(['median of the inverter','mediana del inversor']))+legItem('#d64545',tx(['below the band','bajo la banda']))+legItem('#2f6fdb',tx(['above the band','sobre la banda']))+
   (S.zero.length?legItem('#5b0f1e',tx(['no output','sin salida']),1):'')+(S.mixed_planes?'<span>'+tx(['(strings on different roof planes: colours use each string\'s own design; the band is drawn from the raw values)','(cadenas en planos distintos: los colores usan el diseño propio de cada cadena; la banda se dibuja con los valores medidos)'])+'</span>':'');
  var btns=P.inverters.map(function(v,k){return '<button type="button" data-sel="'+v.inv+'" class="'+(v.inv===sel?'on':'')+'"><span class="evsw" style="background:'+C[k%C.length]+'"></span>'+v.name+' · '+pct(v.epi600_mtd)+'</button>';}).join('');
  var wo=function(u){var k=S.uids.indexOf(u);return S.wo&&S.wo[k]?'<br><span class="note">'+S.wo[k]+'</span>':'';};
  var TK=(P.tk||{}).s||{};
  var tkl=function(u){var c=tkcell(TK[u]);return c?'<div class="evtkrow">'+c+'</div>':'';};
  var lst=S.out.map(function(o){return '<tr><td>'+o.label+wo(o.uid)+tkl(o.uid)+'</td><td>'+(o.pct>0?'+':'')+o.pct+'%</td><td>'+o.minutes+' min</td></tr>';}).join('');
  var zl=S.zero.map(function(u){return '<tr><td class="evflag">'+S.labels[S.uids.indexOf(u)]+wo(u)+tkl(u)+'</td><td colspan="2" class="evflag">'+tx(['no output','sin salida'])+'</td></tr>';}).join('');
  panel.innerHTML='<div class="evinv">'+btns+'</div>'+(lst||zl?'<table>'+zl+lst+'</table>':'<p class="note">'+tx(['All strings inside the band.','Todas las cadenas dentro de la banda.'])+'</p>');
  panel.querySelectorAll('[data-sel]').forEach(function(b){b.onclick=function(){sel=b.dataset.sel;draw();};});
  // summary: one marker per string, EPI-600 month to date
  var m=S.uids.length, w=Math.max(m*56,300), s='<svg viewBox="0 0 '+w+' 56" width="'+w+'" style="max-width:100%;height:auto">';
  S.uids.forEach(function(u,k){var e=S.epi600_mtd[u], x=26+k*56, z=S.zero.indexOf(u)>=0;
   var col=z?'#5b0f1e':e==null?'#a3a8ae':e>=0.97?'#1e8e3e':e>=0.93?'#b26a00':'#c5221f';
   s+=z?'<text x="'+x+'" y="22" text-anchor="middle" font-size="18" fill="'+col+'">✕</text>':'<circle cx="'+x+'" cy="16" r="8" fill="'+col+'"/>';
   s+='<text x="'+x+'" y="38" text-anchor="middle" font-size="10" fill="#5f6368">'+S.labels[k].split(':').pop()+'</text><text x="'+x+'" y="51" text-anchor="middle" font-size="10" fill="#1c2733">'+(z?tx(['no output','sin salida']):(e==null?'n/a':Math.round(e*100)+'%'))+'</text>';});
  sum.innerHTML='<div class="note">'+tx(['Each string, EPI-600 month to date (green 97 % or more, amber 93-97 %, red below 93 %; ✕ = no output this day):','Cada cadena, EPI-600 mes a la fecha (verde 97 % o más, ámbar 93-97 %, rojo bajo 93 %; ✕ = sin salida este día):'])+'</div>'+s+'</svg>';
 }
 function draw(){clear();card.querySelectorAll('.evseg button').forEach(function(b){b.classList.toggle('on',b.dataset.v===view);});
  if(view==='p')drawPlant();else if(view==='i')drawInverters();else drawStrings();}
 card.querySelectorAll('.evseg button').forEach(function(b){b.onclick=function(){view=b.dataset.v;draw();};});
 svg.addEventListener('mousemove',function(ev){var r=svg.getBoundingClientRect(),xr=(ev.clientX-r.left)/r.width*W,i=Math.round((xr-PL)/((W-PL-PR)/Math.max(n-1,1)));
  if(i<0||i>=n){tip.style.display='none';return;}var h='<b>'+P.t[i]+'</b>';
  if(view==='p'){h+='<br>'+tx(['AC ','CA '])+(P.plant.ac_kw[i]==null?'n/a':P.plant.ac_kw[i]+' kW')+'<br>'+tx(['design ','diseño '])+(P.plant.design_kw[i]==null?'n/a':P.plant.design_kw[i]+' kW')+'<br>GHI '+(P.ghi[i]==null?'n/a':P.ghi[i]+' W/m²');}
  else if(view==='i'){P.inverters.forEach(function(v){if(!hidden[v.inv])h+='<br>'+v.name+': '+(v.w[i]==null?'n/a':v.w[i])+' / '+(v.design[i]==null?'n/a':v.design[i])+' W/kWp';});}
  else{var S=P.strings[sel];if(S){h+='<br>'+tx(['median ','mediana '])+(S.median[i]==null?'n/a':S.median[i])+' W/kWp';S.out.forEach(function(o){var k=S.uids.indexOf(o.uid);h+='<br>'+o.label+': '+(S.w[k][i]==null?'n/a':S.w[k][i]);});}}
  tip.innerHTML=h;tip.style.display='block';tip.style.left=(ev.clientX+14)+'px';tip.style.top=(ev.clientY+10)+'px';});
 svg.addEventListener('mouseleave',function(){tip.style.display='none';});
 document.querySelectorAll('.lang-btn,[data-l]').forEach(function(b){b.addEventListener('click',function(){setTimeout(draw,0);});});
 draw();
});
})();
'''


def card(pk: str, p: dict, live: bool = False) -> str:
    p = {k: v for k, v in p.items() if k != 'tallies'}        # the page draws; it does not need the sums
    return render(pk, p, live) + f'<style>{CSS}</style><script>{JS}</script>'


def pending_card() -> str:
    """A past day not computed yet (at most a few per plant are computed per run)."""
    return ('<div class="card"><h2 data-en="Troubleshooting: plant, inverters, strings" '
            'data-es="Diagnóstico: planta, inversores, cadenas">Troubleshooting: plant, inverters, strings</h2>'
            + t('This day is queued for computation and appears within a few generator runs.',
                'Este día está en cola de cálculo y aparece en unas pocas corridas del generador.', 'p') + '</div>')
