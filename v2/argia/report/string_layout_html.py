"""v317: the string layout card under the intraday chart of a plant page.

Pure rendering: monitoring_gen reads the layout file and the samples,
argia.analytics.string_layout judges them, this module turns the result
into one self-contained card (inline SVG over the drawing image, a
string grid per inverter, a hover card, a time slider). No I/O.
"""
from __future__ import annotations

import html
import json
from typing import Dict, List, Optional, Sequence

from argia.analytics import string_layout as SL

COLORS = {SL.OK: '#1e9e5a', SL.LOW: '#e8a23a', SL.VLOW: '#d64545', SL.ZERO: '#5b0f1e',
          SL.NODATA: '#a3a8ae', SL.DIM: '#b8c7d9'}
CONF_TEXT = {
    'table': ('position and input from the drawing table', 'posición y entrada según la tabla del plano'),
    'order': ('position from the drawing; input by the inverter order (string n = input n)',
              'posición según el plano; entrada por el orden del inversor (cadena n = entrada n)'),
    'assumed': ('drawing and monitoring disagree - placed where most likely, confirm on site',
                'el plano y el monitoreo no coinciden - ubicada donde es más probable, confirmar en sitio'),
    'none': ('not on the drawing', 'no está en el plano'),
}


def _r(v, nd=1):
    return None if v is None else round(float(v), nd)


def _orient(s: dict) -> str:
    if s.get('tilt') is None or s.get('az') is None:
        return ''
    return f"{s['tilt']:g}° / {s['az']:g}°" + (f" {s['orient']}" if s.get('orient') else '')


def _group_label(g: str) -> List[str]:
    kind, _, rest = g.partition(':')
    if kind == 'plane':
        mod, tilt, az = rest.split('|')
        return [f'{mod}, {tilt}° / {az}°', f'{mod}, {tilt}° / {az}°']
    if kind == 'module':
        return [f'{rest}, all planes of the plant', f'{rest}, todos los planos de la planta']
    return ['the whole plant', 'toda la planta']


def payload(pk: str, layout: dict, inv_labels: Dict[str, str], *, live: bool, source: str,
            buckets: Sequence[int] = (), cur: Sequence[Sequence[Optional[float]]] = (),
            ev: Optional[dict] = None, day: Optional[List[dict]] = None,
            age_min: Optional[int] = None, img_url: Optional[Dict[str, str]] = None) -> dict:
    """Everything the card's script needs, compact. source: '5min' (string_sample),
    'daily' (string_daily amp-hours only), 'none'."""
    strings = layout['strings']
    groups = ev['groups'] if ev else SL.peer_groups(strings)
    gnames = sorted(set(groups))
    gi = {g: k for k, g in enumerate(gnames)}
    S = []
    for s in strings:
        S.append({'sn': s['sn'], 'inv': s.get('inv') or '', 'il': inv_labels.get(s['sn'], s['sn']), 'ch': s['ch'],
                  'mp': s.get('mppt'), 'cad': [p.get('cad') for p in s.get('pos') or []],
                  'p': [[p['d'], _r(p['x']), _r(p['y'])] for p in s.get('pos') or []], 'cf': s.get('conf'),
                  'kwp': s.get('kwp'), 'mod': s.get('module') or '', 'n': s.get('modules'), 'or': _orient(s),
                  'per': int(s.get('per') or 1), 'note': s.get('note') or '', 'g': gi[groups[len(S)]]})
    day = day if day is not None else (ev['day'] if ev else [{'cls': SL.NODATA} for _ in strings])
    inv = SL.inverter_summary(strings, ev['cls'], len(buckets)) if ev else {}
    dcls = [d['cls'] for d in day]
    inv_day = SL.inverter_summary(strings, [[c] for c in dcls], 1)
    return {
        'pk': pk, 'live': bool(live), 'src': source, 'age': age_min,
        't': [SL.hhmm(b) for b in buckets],
        'd': [{'id': d['id'], 'img': (img_url or {}).get(d['id'], ''), 'box': d['box'],
               't': [d.get('title_en', ''), d.get('title_es', '')], 'src': d.get('src', '')}
              for d in layout.get('drawings') or []],
        's': S,
        'a': [{'sn': a['sn'], 'd': a['d'], 'r': a['rects']} for a in layout.get('areas') or []],
        'i': [[_r(v) for v in row] for row in cur] if ev else [],
        'c': ev['cls'] if ev else [],
        'm': [[_r(v) for v in ev['med'][g]] for g in gnames] if ev else [],
        'gn': [_group_label(g) for g in gnames],
        'dy': [{'ah': d.get('ah'), 'x': d.get('idx'), 'c': d['cls']} for d in day],
        'iv': {sn: {'c': v['cls'], 'b': v['bad'], 'dc': inv_day[sn]['cls'], 'db': inv_day[sn]['bad'][0]}
               for sn, v in inv.items()} if ev else
              {sn: {'c': '', 'b': [], 'dc': inv_day[sn]['cls'], 'db': inv_day[sn]['bad'][0]} for sn in inv_day},
        'io': [sn for sn in inv_labels if any(x['sn'] == sn for x in S)] + [sn for sn in dict.fromkeys(x['sn'] for x in S) if sn not in inv_labels],
        'notes': layout.get('notes') or [],
        'col': COLORS,
        'cls': {k: list(v) for k, v in SL.CLASS_TEXT.items()},
        'conf': {k: list(v) for k, v in CONF_TEXT.items()},
    }


def _embed(obj) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/')


def t(en: str, es: str, tag: str = 'span', cls: str = '') -> str:
    c = f' class="{cls}"' if cls else ''
    return f'<{tag}{c} data-en="{html.escape(en)}" data-es="{html.escape(es)}">{html.escape(en)}</{tag}>'


def render(p: dict) -> str:
    pk = p['pk']
    uid = f'sl{pk}'
    has_draw = bool(p['d'])
    legend = ''.join(f'<span class="slleg"><i style="background:{COLORS[k]}"></i>{t(*SL.CLASS_TEXT[k])}</span>'
                     for k in (SL.OK, SL.LOW, SL.VLOW, SL.ZERO, SL.DIM, SL.NODATA))
    if p['src'] == '5min':
        bar = (f'<div class="slmodes"><button type="button" class="slbtn" data-m="t" data-en="At a time" data-es="A una hora">At a time</button>'
               f'<button type="button" class="slbtn" data-m="d" data-en="Whole day" data-es="Todo el día">Whole day</button></div>'
               f'<div class="sltime"><input type="range" min="0" max="{max(len(p["t"]) - 1, 0)}" value="{max(len(p["t"]) - 1, 0)}" '
               f'aria-label="time"><b class="slclock"></b></div>')
    else:
        bar = ''
    src_note = {
        '5min': ('Live: every 5 minutes, the current of each string against the median of its peers (same module, same plane) in the same 5 minutes.',
                 'En vivo: cada 5 minutos, la corriente de cada cadena contra la mediana de sus pares (mismo módulo, mismo plano) en esos 5 minutos.'),
        'daily': ('This day predates the 5-minute string record (v317): colours show the day\'s amp-hours of each string against its peers, from the nightly Growatt history.',
                  'Este día es anterior al registro de cadenas de 5 minutos (v317): los colores muestran los amperios-hora del día de cada cadena contra sus pares, del historial nocturno de Growatt.'),
        'none': ('No string data for this day yet. The 5-minute string record starts with v317; Huawei plants have no earlier string history.',
                 'Aún no hay datos de cadenas para este día. El registro de 5 minutos empieza con v317; las plantas Huawei no tienen historial anterior.'),
    }[p['src']]
    stale = ''
    if p['live'] and p['src'] == '5min' and p.get('age') is not None and p['age'] > 30:
        age = p['age']
        stale = ('<div class="banner">' + t(f'Latest string sample {age} min ago: the colours show the last known state.',
                                            f'Última muestra de cadenas hace {age} min: los colores muestran el último estado conocido.')
                 + '</div>')
    draw_html = ''
    if has_draw:
        tabs = ''
        if len(p['d']) > 1:
            tabs = '<div class="sltabs">' + ''.join(
                f'<button type="button" class="slbtn" data-dr="{k}" data-en="{html.escape(d["t"][0])}" data-es="{html.escape(d["t"][1])}">{html.escape(d["t"][0])}</button>'
                for k, d in enumerate(p['d'])) + '</div>'
        draw_html = (f'{tabs}<div class="slpic"><img alt="layout" loading="lazy"><svg class="slsvg" preserveAspectRatio="none"></svg></div>'
                     f'<div class="note slsrc"></div>')
    notes = ''.join(f'<p class="note">{html.escape(n)}</p>' for n in p.get('notes') or [])
    return f'''<div class="card slcard" id="{uid}">
<h2 data-en="String layout · each string's current against its peers" data-es="Distribución de cadenas · la corriente de cada cadena contra sus pares">String layout · each string's current against its peers</h2>
{stale}<div class="slbar">{bar}<div class="sllegend">{legend}</div></div>
<div class="slmain"><div class="sldraw">{draw_html}</div><div class="slside"><div class="slsum"></div><div class="sllist"></div></div></div>
<div class="slgrid"></div>
<div class="sltip" role="tooltip"></div>
<p class="note">{t(*src_note)} {t("Below 1 A peer median (dawn, dusk, heavy cloud) strings are not judged. A colour is a pointer for the O&M team, not an alarm. Ring: solid = input from the drawing table, dashed = input by the inverter order, orange = drawing and monitoring disagree (confirm on site).", "Con mediana de pares bajo 1 A (amanecer, atardecer, nubes densas) no se evalúa. Un color es una pista para O&M, no una alarma. Anillo: sólido = entrada según la tabla del plano, punteado = entrada por el orden del inversor, naranja = el plano y el monitoreo no coinciden (confirmar en sitio).")}</p>{notes}
<script type="application/json" class="sldata">{_embed(p)}</script>
</div>'''


CSS = '''
.slcard .slbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.slcard .slmodes,.slcard .sltabs{display:inline-flex;gap:0;border:1px solid #dadce0;border-radius:8px;overflow:hidden}
.slcard .sltabs{margin-bottom:8px}
.slcard .slbtn{background:#fff;border:0;padding:6px 12px;font:inherit;font-size:13px;color:#3c4043;cursor:pointer}
.slcard .slbtn+.slbtn{border-left:1px solid #dadce0}
.slcard .slbtn.on{background:#1c2733;color:#fff}
.slcard .sltime{display:flex;align-items:center;gap:10px;flex:1;min-width:220px;max-width:460px}
.slcard .sltime input{flex:1;accent-color:#1c2733}
.slcard .slclock{font-variant-numeric:tabular-nums;min-width:48px}
.slcard .sllegend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;color:#5f6368}
.slcard .slleg i{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.slcard .slmain{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.slcard .sldraw{flex:3 1 560px;min-width:0}
.slcard .sldraw:empty{display:none}
.slcard .slside{flex:1 1 220px;font-size:13px}
.slcard .slpic{position:relative;width:100%;border-radius:8px;overflow:hidden;border:1px solid #e4e7ea;background:#f1f3f4}
.slcard .slpic img{display:block;width:100%;height:auto;filter:saturate(.22) contrast(.92) brightness(1.04)}
.slcard .slsvg{position:absolute;inset:0;width:100%;height:100%}
.slcard .slsvg .mk{cursor:pointer}
.slcard .slsvg .mk.hl circle{stroke:#1a73e8;stroke-width:3}
.slcard .slsum{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;margin-bottom:10px}
.slcard .slsum b{text-align:right;font-variant-numeric:tabular-nums}
.slcard .sllist div{padding:5px 7px;border-radius:6px;cursor:pointer;border:1px solid #eceef0;margin-bottom:4px}
.slcard .sllist div:hover{background:#f6f7f8}
.slcard .slgrid{margin-top:12px;display:flex;flex-direction:column;gap:6px}
.slcard .slinv{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.slcard .slinv .nm{width:150px;font-size:12.5px;color:#3c4043}
.slcard .slinv .mp{display:inline-flex;gap:3px;padding:3px;border:1px solid #eceef0;border-radius:7px}
.slcard .chip{width:24px;height:24px;border-radius:6px;color:#fff;font-size:11px;font-weight:600;display:flex;align-items:center;justify-content:center;cursor:pointer}
.slcard .chip.hl{outline:3px solid #1a73e8}
.slcard .sltip{position:fixed;z-index:60;display:none;background:#1c2733;color:#fff;border-radius:8px;padding:9px 11px;font-size:12.5px;line-height:1.45;max-width:330px;pointer-events:none;box-shadow:0 6px 24px rgba(0,0,0,.25)}
.slcard .sltip .h{font-weight:700;margin-bottom:3px}
.slcard .sltip .mut{color:#b9c2cc}
@media(max-width:700px){.slcard .slinv .nm{width:100%}}
'''

JS = r'''
(function(){
document.querySelectorAll('.slcard').forEach(function(card){
 var P=JSON.parse(card.querySelector('.sldata').textContent);
 var L=function(){return (document.documentElement.lang==='es'||(function(){try{return localStorage.getItem('argia_lang')==='es';}catch(e){return false;}})())?1:0;};
 var NS='http://www.w3.org/2000/svg';
 function E(x){return String(x==null?'':x).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
 var nb=P.t.length, mode=(P.src==='5min'&&P.live)?'t':'d', at=nb-1, dr=0, pin=null;
 var range=card.querySelector('.sltime input'), clock=card.querySelector('.slclock');
 var tip=card.querySelector('.sltip');
 function cls(k){return mode==='t'&&nb?(P.c[k]||'')[at]||'n':(P.dy[k]||{}).c||'n';}
 function icls(sn){var v=P.iv[sn]||{};return mode==='t'&&nb?(v.c||'')[at]||'n':(v.dc||'n');}
 function ibad(sn){var v=P.iv[sn]||{};return mode==='t'&&nb?(v.b||[])[at]||0:(v.db||0);}
 function col(c){return P.col[c]||P.col.n;}
 function num(v,d){return v==null?'-':Number(v).toFixed(d==null?1:d);}
 function sname(s){var w=L()?'cadena':'string';return (s.cad.length?(w+' '+s.cad.join(', ')):(s.ch[0]==='m'?'MPPT '+s.ch.slice(1):(L()?'entrada ':'input ')+s.ch.slice(1)));}
 function chtxt(s){return s.ch[0]==='m'?('MPPT '+s.ch.slice(1)+(s.per>1?(L()?' (compartida por ':' (shared by ')+s.per+(L()?' cadenas)':' strings)'):'')):((L()?'entrada ':'input ')+s.ch.slice(1)+(s.mp?' · MPPT '+s.mp:''));}
 function tipHtml(k){var s=P.s[k],l=L(),c=cls(k),h=[];
  h.push('<div class="h">'+E(s.il)+(s.inv?' <span class="mut">('+(l?'plano ':'drawing ')+s.inv+')</span>':'')+' · '+sname(s)+'</div>');
  h.push('<div class="mut">'+chtxt(s)+' · '+E(s.sn)+'</div>');
  h.push('<div>'+(s.n?s.n+' x ':'')+E(s.mod)+(s.kwp?' · '+num(s.kwp,2)+' kWp':'')+(s.or?' · '+E(s.or):'')+'</div>');
  if(mode==='t'&&nb){var i=(P.i[k]||[])[at],m=(P.m[s.g]||[])[at];
   h.push('<div>'+(l?'A las ':'At ')+P.t[at]+': <b>'+num(i)+' A</b>'+(s.per>1?(l?' por cadena':' per string'):'')+' · '+(l?'pares ':'peers ')+num(m)+' A'+(i!=null&&m?(' · <b>'+Math.round(100*i/m)+' %</b>'):'')+'</div>');}
  var d=P.dy[k]||{};
  if(d.ah!=null||d.x!=null){h.push('<div>'+(l?'Día: ':'Day: ')+(d.ah!=null?num(d.ah)+' Ah':'')+(d.x!=null?' · '+Math.round(100*d.x)+(l?' % de sus pares':' % of its peers'):'')+'</div>');}
  h.push('<div><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:'+col(c)+'"></span> '+(P.cls[c]||['',''])[l]+'</div>');
  h.push('<div class="mut">'+(l?'Pares: ':'Peers: ')+E((P.gn[s.g]||['',''])[l])+'</div>');
  h.push('<div class="mut">'+(P.conf[s.cf]||['',''])[l]+'</div>');
  if(s.note)h.push('<div class="mut">'+E(s.note)+'</div>');
  return h.join('');}
 function areaHtml(sn){var l=L(),ks=[];P.s.forEach(function(s,k){if(s.sn===sn)ks.push(k);});var s=P.s[ks[0]];
  var c=icls(sn),b=ibad(sn);
  return '<div class="h">'+E(s.il)+(s.inv?' <span class="mut">('+(l?'plano ':'drawing ')+s.inv+')</span>':'')+'</div><div class="mut">'+E(sn)+' · '+ks.length+(l?' cadenas monitoreadas':' monitored strings')+'</div>'+
   '<div><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:'+col(c)+'"></span> '+(l?'cadena mediana: ':'median string: ')+(P.cls[c]||['',''])[l]+(b?' · <b>'+b+(l?' bajas':' low')+'</b>':'')+'</div>'+
   '<div class="mut">'+(l?'El plano marca el área del inversor, no la de cada cadena: ver la cuadrícula de abajo.':'The drawing marks the inverter\'s area, not each string: see the grid below.')+'</div>';}
 function show(ev,htmlTxt){tip.innerHTML=htmlTxt;tip.style.display='block';
  var x=ev.clientX+14,y=ev.clientY+14,w=tip.offsetWidth,hh=tip.offsetHeight;
  if(x+w>window.innerWidth-8)x=ev.clientX-w-14; if(y+hh>window.innerHeight-8)y=ev.clientY-hh-14;
  tip.style.left=Math.max(4,x)+'px';tip.style.top=Math.max(4,y)+'px';}
 function hide(){if(pin==null)tip.style.display='none';}
 function highlight(k,on){card.querySelectorAll('[data-k="'+k+'"]').forEach(function(e){e.classList.toggle('hl',on);});}
 // ---------------------------------------------------------------- drawing
 var svg=card.querySelector('.slsvg'), img=card.querySelector('.slpic img');
 function drawPic(){if(!svg)return;var D=P.d[dr];img.src=D.img;
  var bx=D.box,w=bx[2]-bx[0],h=bx[3]-bx[1];svg.setAttribute('viewBox',bx[0]+' '+bx[1]+' '+w+' '+h);
  while(svg.firstChild)svg.removeChild(svg.firstChild);
  var r=Math.max(4.5,w/95);
  P.a.forEach(function(a){if(a.d!==D.id)return;var g=document.createElementNS(NS,'g');g.setAttribute('class','ar');
   a.r.forEach(function(q){var e=document.createElementNS(NS,'rect');e.setAttribute('x',q[0]);e.setAttribute('y',q[1]);e.setAttribute('width',q[2]);e.setAttribute('height',q[3]);g.appendChild(e);});
   g.dataset.sn=a.sn;g.style.cursor='pointer';
   g.addEventListener('mousemove',function(ev){if(pin==null)show(ev,areaHtml(a.sn));});g.addEventListener('mouseleave',hide);
   svg.appendChild(g);});
  P.s.forEach(function(s,k){s.p.forEach(function(q,qi){if(q[0]!==D.id)return;
   var g=document.createElementNS(NS,'g');g.setAttribute('class','mk');g.dataset.k=k;
   var c=document.createElementNS(NS,'circle');c.setAttribute('cx',q[1]);c.setAttribute('cy',q[2]);c.setAttribute('r',r);
   c.setAttribute('stroke-width',r*0.28);
   if(s.cf==='order')c.setAttribute('stroke-dasharray',(r*0.5)+' '+(r*0.35));
   var tx=document.createElementNS(NS,'text');tx.setAttribute('x',q[1]);tx.setAttribute('y',q[2]+r*0.36);tx.setAttribute('text-anchor','middle');
   tx.setAttribute('font-size',r*(String(s.cad[qi]).length>1?0.95:1.1));tx.setAttribute('font-weight','700');tx.setAttribute('fill','#fff');tx.setAttribute('pointer-events','none');
   tx.textContent=s.cad[qi]!=null?s.cad[qi]:s.ch.slice(1);
   g.appendChild(c);g.appendChild(tx);
   g.addEventListener('mousemove',function(ev){if(pin==null){show(ev,tipHtml(k));highlight(k,true);}});
   g.addEventListener('mouseleave',function(){highlight(k,false);hide();});
   g.addEventListener('click',function(ev){ev.stopPropagation();pin=(pin===k?null:k);show(ev,tipHtml(k));if(pin==null)hide();});
   svg.appendChild(g);});});
  var D2=P.d[dr];card.querySelector('.slsrc').textContent=(L()?'Plano: ':'Drawing: ')+D2.src;
  card.querySelectorAll('.sltabs .slbtn').forEach(function(b){b.classList.toggle('on',+b.dataset.dr===dr);});
  paint();}
 // ------------------------------------------------------------------- grid
 var grid=card.querySelector('.slgrid');
 (function(){var byInv={},order=[];P.s.forEach(function(s,k){if(!byInv[s.sn]){byInv[s.sn]=[];}byInv[s.sn].push(k);});order=(P.io||[]).filter(function(sn){return byInv[sn];});
  order.forEach(function(sn){var row=document.createElement('div');row.className='slinv';
   var nm=document.createElement('div');nm.className='nm';var s0=P.s[byInv[sn][0]];
   nm.innerHTML='<b>'+E(s0.il)+'</b><br><span class="note">'+E(sn)+'</span>';row.appendChild(nm);
   var mp={},mo=[];byInv[sn].forEach(function(k){var m=P.s[k].mp||0;if(!mp[m]){mp[m]=[];mo.push(m);}mp[m].push(k);});
   mo.sort(function(a,b){return a-b;}).forEach(function(m){var box=document.createElement('div');box.className='mp';box.title='MPPT '+m;
    mp[m].forEach(function(k){var c=document.createElement('div');c.className='chip';c.dataset.k=k;var s=P.s[k];
     c.textContent=s.cad.length===1?s.cad[0]:s.ch.slice(1);
     c.addEventListener('mousemove',function(ev){if(pin==null){show(ev,tipHtml(k));highlight(k,true);}});
     c.addEventListener('mouseleave',function(){highlight(k,false);hide();});
     c.addEventListener('click',function(ev){ev.stopPropagation();pin=(pin===k?null:k);show(ev,tipHtml(k));if(pin==null)hide();});
     box.appendChild(c);});row.appendChild(box);});
   grid.appendChild(row);});})();
 document.addEventListener('click',function(){if(pin!=null){pin=null;tip.style.display='none';}});
 // ------------------------------------------------------------------ paint
 function paint(){var l=L(),cnt={};
  card.querySelectorAll('[data-k]').forEach(function(e){var k=+e.dataset.k,c=cls(k);cnt[c]=(cnt[c]||0)+(e.classList.contains('chip')?1:0);
   if(e.tagName==='g'){var ci=e.querySelector('circle');ci.setAttribute('fill',col(c));
    var cf=P.s[k].cf;ci.setAttribute('stroke',cf==='assumed'?'#f28c28':'#ffffff');}
   else e.style.background=col(c);});
  card.querySelectorAll('.slsvg g.ar').forEach(function(g){var c=icls(g.dataset.sn);g.setAttribute('fill',col(c));g.setAttribute('fill-opacity','0.55');});
  if(clock)clock.textContent=nb?P.t[at]:'';
  if(range)range.style.display=mode==='t'?'':'none';if(clock)clock.style.display=mode==='t'?'':'none';
  card.querySelectorAll('.slmodes .slbtn').forEach(function(b){b.classList.toggle('on',b.dataset.m===mode);});
  var sum=card.querySelector('.slsum'),order=['g','a','r','z','d','n'];
  sum.innerHTML='<span class="note">'+(mode==='t'&&nb?((l?'A las ':'At ')+P.t[at]):(l?'Todo el día':'Whole day'))+'</span><span></span>'+
   order.filter(function(c){return cnt[c];}).map(function(c){return '<span><i style="display:inline-block;width:10px;height:10px;border-radius:50%;background:'+col(c)+';margin-right:6px"></i>'+P.cls[c][l]+'</span><b>'+cnt[c]+'</b>';}).join('');
  var bad=[];P.s.forEach(function(s,k){var c=cls(k);if(c==='z'||c==='r'||c==='a')bad.push(k);});
  var rank={z:0,r:1,a:2};bad.sort(function(a,b){return rank[cls(a)]-rank[cls(b)];});
  var list=card.querySelector('.sllist');list.innerHTML='';
  if(!bad.length){list.innerHTML='<div style="cursor:default">'+(l?'Ninguna cadena baja.':'No string below its peers.')+'</div>';}
  bad.slice(0,10).forEach(function(k){var s=P.s[k],d=document.createElement('div'),c=cls(k);
   var val=mode==='t'&&nb?(function(){var i=(P.i[k]||[])[at],m=(P.m[s.g]||[])[at];return i!=null&&m?Math.round(100*i/m)+' %':'';})():((P.dy[k]||{}).x!=null?Math.round(100*P.dy[k].x)+' %':'');
   d.innerHTML='<i style="display:inline-block;width:10px;height:10px;border-radius:50%;background:'+col(c)+';margin-right:6px"></i><b>'+E(s.il)+'</b> · '+sname(s)+' <span class="note">'+val+'</span>';
   d.addEventListener('mouseenter',function(){highlight(k,true);});d.addEventListener('mouseleave',function(){highlight(k,false);});
   d.addEventListener('click',function(ev){ev.stopPropagation();pin=k;show(ev,tipHtml(k));});
   list.appendChild(d);});
  if(bad.length>10){var m=document.createElement('div');m.style.cursor='default';m.textContent='+'+(bad.length-10);list.appendChild(m);}
 }
 if(range){range.addEventListener('input',function(){at=+range.value;paint();});}
 card.querySelectorAll('.slmodes .slbtn').forEach(function(b){b.addEventListener('click',function(){mode=b.dataset.m;paint();});});
 card.querySelectorAll('.sltabs .slbtn').forEach(function(b){b.addEventListener('click',function(){dr=+b.dataset.dr;drawPic();});});
 if(svg&&P.d.length)drawPic();else paint();
 document.querySelectorAll('.lang-btn,[data-l]').forEach(function(b){b.addEventListener('click',function(){setTimeout(paint,0);});});
});
})();
'''


def card(p: dict) -> str:
    """The card with its style and script (one block per page)."""
    return render(p) + f'<style>{CSS}</style><script>{JS}</script>'
