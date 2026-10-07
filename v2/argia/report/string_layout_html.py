"""v317 / v318: the string layout card under the intraday chart of a plant page.

Pure rendering: monitoring_gen reads the layout file and the samples,
argia.analytics.string_layout judges them, this module turns the result
into one self-contained card. No I/O.

v318 (Tomasz, 7 Oct 2026: "double check the number of inverters, map each
inverter's connected strings with separate colours, make sure we have a
good and transparent legend, I am also missing the Helioscope versions, I
do not understand what the picture is showing"):
* two views - Health (the dot's colour = the string against its
  neighbours and its own normal; the ring = its inverter) and Inverters
  (every dot and the inverter's own panels in that inverter's colour);
* two pictures - the installation drawing and the Helioscope design;
* a plain-language line on what a dot is, an inverter table (colour, name
  on the portal and on the drawing, serial, model, strings, kWp), the
  counts from every source side by side, a legend with the thresholds.
"""
from __future__ import annotations

import html
import json
from typing import Dict, List, Optional, Sequence

from argia.analytics import string_layout as SL

COLORS = {SL.OK: '#1e9e5a', SL.LOW: '#e8a23a', SL.VLOW: '#d64545', SL.ZERO: '#5b0f1e',
          SL.NODATA: '#a3a8ae', SL.DIM: '#b8c7d9'}
# inverter colours: none of them green, amber or red (those mean health)
INV_COLORS = ['#2f6fdb', '#9b59d0', '#13a3b5', '#e05aa8', '#a07a3c', '#4a5d73', '#6b8e23', '#c0392b']
CLASS_RULE = {
    SL.OK: ('90 % or more of its neighbours', '90 % o más de sus vecinas'),
    SL.LOW: ('75 to 90 %, or always below 80 %', '75 a 90 %, o siempre bajo 80 %'),
    SL.VLOW: ('below 75 %', 'menos de 75 %'),
    SL.ZERO: ('no current while the neighbours produce (string off, open fuse or connector)',
              'sin corriente mientras las vecinas producen (cadena apagada, fusible o conector abierto)'),
    SL.DIM: ('too little sun to judge (early, late, heavy cloud)', 'muy poco sol para evaluar (temprano, tarde, muy nublado)'),
    SL.NODATA: ('no data from the inverter', 'sin datos del inversor'),
}
CONF_TEXT = {
    'table': ('position and input from the drawing table (or the field form)',
              'posición y entrada según la tabla del plano (o el formato de campo)'),
    'order': ('position from the drawing; input by the inverter order (string n = input n)',
              'posición según el plano; entrada por el orden del inversor (cadena n = entrada n)'),
    'assumed': ('drawing and monitoring disagree - placed where most likely, confirm on site',
                'el plano y el monitoreo no coinciden - ubicada donde es más probable, confirmar en sitio'),
    'none': ('not numbered on the drawing - see the grid', 'sin número en el plano - ver la cuadrícula'),
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
        return [f'same module ({mod}) and roof plane ({tilt}° / {az}°)',
                f'mismo módulo ({mod}) y plano ({tilt}° / {az}°)']
    if kind == 'module':
        return [f'same module ({rest}), every plane of the plant', f'mismo módulo ({rest}), todos los planos de la planta']
    return ['every string of the plant', 'todas las cadenas de la planta']


def _note(n) -> List[str]:
    return list(n) if isinstance(n, (list, tuple)) else [str(n), str(n)]


def payload(pk: str, layout: dict, inv_labels: Dict[str, str], *, live: bool, source: str,
            buckets: Sequence[int] = (), cur: Sequence[Sequence[Optional[float]]] = (),
            ev: Optional[dict] = None, day: Optional[List[dict]] = None,
            age_min: Optional[int] = None, img_url: Optional[Dict[str, str]] = None,
            normal: Optional[Sequence[Optional[float]]] = None, monitored_inverters: Optional[int] = None) -> dict:
    """Everything the card's script needs, compact. source: '5min' (string_sample),
    'daily' (day amp-hours only), 'none'."""
    strings = layout['strings']
    groups = ev['groups'] if ev else SL.peer_groups(strings)
    gnames = sorted(set(groups))
    gi = {g: k for k, g in enumerate(gnames)}
    normal = list(normal) if normal is not None else (ev or {}).get('normal') or [None] * len(strings)
    S = []
    for k, s in enumerate(strings):
        S.append({'sn': s['sn'], 'inv': s.get('inv') or '', 'il': inv_labels.get(s['sn'], s['sn']), 'ch': s['ch'],
                  'mp': s.get('mppt'), 'cad': [p.get('cad') for p in s.get('pos') or [] if p['d'] == 'drawing'],
                  'p': [[p['d'], _r(p['x']), _r(p['y']), p.get('cad')] for p in s.get('pos') or []],
                  'cf': s.get('conf'), 'kwp': s.get('kwp'), 'mod': s.get('module') or '', 'n': s.get('modules'),
                  'or': _orient(s), 'per': int(s.get('per') or 1), 'note': s.get('note') or '',
                  'g': gi[groups[k]], 'nm': normal[k] if k < len(normal) else None})
    day = day if day is not None else (ev['day'] if ev else [{'cls': SL.NODATA} for _ in strings])
    inv = SL.inverter_summary(strings, ev['cls'], len(buckets)) if ev else {}
    inv_day = SL.inverter_summary(strings, [[d['cls']] for d in day], 1)
    io = [sn for sn in inv_labels if any(x['sn'] == sn for x in S)] + \
         [sn for sn in dict.fromkeys(x['sn'] for x in S) if sn not in inv_labels]
    meta = {i.get('sn'): i for i in layout.get('inverters') or []}
    inverters = []
    for k, sn in enumerate(io):
        m = meta.get(sn, {})
        mine = [x for x in S if x['sn'] == sn]
        inverters.append({'sn': sn, 'il': inv_labels.get(sn, sn), 'dn': m.get('inv') or (mine[0]['inv'] if mine else ''),
                          'model': m.get('model') or '', 'kwp': m.get('kwp'), 'col': INV_COLORS[k % len(INV_COLORS)],
                          'n': sum(x['per'] for x in mine), 'inputs': len(mine),
                          'placed': sum(x['per'] for x in mine if any(q[0] == 'drawing' for q in x['p']))})
    chk = dict((layout.get('checks') or {}))
    if monitored_inverters is not None:
        chk.setdefault('inverters', {})
        chk['inverters'] = dict(chk['inverters'], monitoring=monitored_inverters)
    return {
        'pk': pk, 'live': bool(live), 'src': source, 'age': age_min,
        't': [SL.hhmm(b) for b in buckets],
        'd': [{'id': d['id'], 'kind': d.get('kind', 'drawing'), 'img': (img_url or {}).get(d['id'], ''), 'box': d['box'],
               't': [d.get('title_en', ''), d.get('title_es', '')], 'src': d.get('src', ''),
               'mk': d.get('markers', True), 'fit': d.get('fit'), 'pr': d.get('paired')}
              for d in layout.get('drawings') or []],
        's': S,
        'a': [{'sn': a['sn'], 'd': a['d'], 'r': a.get('rects') or [], 'pl': a.get('polys') or []}
              for a in layout.get('areas') or []],
        'i': [[_r(v) for v in row] for row in cur] if ev else [],
        'c': ev['cls'] if ev else [],
        'm': [[_r(v) for v in ev['med'][g]] for g in gnames] if ev else [],
        'gn': [_group_label(g) for g in gnames],
        'dy': [{'ah': d.get('ah'), 'x': d.get('idx'), 'c': d['cls']} for d in day],
        'iv': {sn: {'c': v['cls'], 'b': v['bad'], 'dc': inv_day[sn]['cls'], 'db': inv_day[sn]['bad'][0]}
               for sn, v in inv.items()} if ev else
              {sn: {'c': '', 'b': [], 'dc': inv_day[sn]['cls'], 'db': inv_day[sn]['bad'][0]} for sn in inv_day},
        'io': io, 'inv': inverters, 'chk': chk,
        'notes': [_note(n) for n in layout.get('notes') or []],
        'col': COLORS,
        'cls': {k: list(v) for k, v in SL.CLASS_TEXT.items()},
        'rule': {k: list(v) for k, v in CLASS_RULE.items()},
        'conf': {k: list(v) for k, v in CONF_TEXT.items()},
    }


def _embed(obj) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/')


def t(en: str, es: str, tag: str = 'span', cls: str = '') -> str:
    c = f' class="{cls}"' if cls else ''
    return f'<{tag}{c} data-en="{html.escape(en)}" data-es="{html.escape(es)}">{html.escape(en)}</{tag}>'


def _btn(attr: str, val: str, en: str, es: str) -> str:
    return (f'<button type="button" class="slbtn" data-{attr}="{val}" data-en="{html.escape(en)}" '
            f'data-es="{html.escape(es)}">{html.escape(en)}</button>')


SRC_NOTE = {
    '5min': ('Every 5 minutes the inverters report each string\'s current. A string is compared with its neighbours '
             '(same module, same roof plane) at the same moment and with its own normal over the last 30 days, '
             'so a string that always reads a little lower is not flagged, only a change is.',
             'Cada 5 minutos los inversores reportan la corriente de cada cadena. Se compara con sus vecinas (mismo '
             'módulo, mismo plano) en el mismo momento y con su propio normal de los últimos 30 días: una cadena que '
             'siempre lee un poco menos no se marca, solo un cambio.'),
    'daily': ('This day predates the 5-minute string record: the colours compare each string\'s amp-hours of the '
              'day with its neighbours (Growatt nightly history).',
              'Este día es anterior al registro de cadenas de 5 minutos: los colores comparan los amperios-hora del '
              'día de cada cadena con sus vecinas (historial nocturno de Growatt).'),
    'none': ('No string data for this day: the 5-minute string record starts on 7 Oct 2026 and the Huawei plants have '
             'no earlier string history. The Inverters view still shows the wiring.',
             'Sin datos de cadenas para este día: el registro de 5 minutos empieza el 7 oct 2026 y las plantas Huawei no '
             'tienen historial anterior. La vista Inversores muestra el cableado.'),
}


def render(p: dict) -> str:
    pk = p['pk']
    has_draw = bool(p['d'])
    stale = ''
    if p['live'] and p['src'] == '5min' and p.get('age') is not None and p['age'] > 30:
        age = p['age']
        stale = ('<div class="banner">' + t(f'Latest string sample {age} min ago: the colours show the last known state.',
                                            f'Última muestra de cadenas hace {age} min: los colores muestran el último estado conocido.')
                 + '</div>')
    views = ('<div class="slseg" title="View">' + _btn('v', 'h', 'Health', 'Desempeño')
             + _btn('v', 'i', 'Inverters', 'Inversores') + '</div>')
    pics = ''
    if len(p['d']) > 1:
        pics = '<div class="slseg sltabs">' + ''.join(
            _btn('dr', str(k), d['t'][0], d['t'][1]) for k, d in enumerate(p['d'])) + '</div>'
    timebar = ''
    if p['src'] == '5min':
        n = max(len(p['t']) - 1, 0)
        timebar = ('<div class="slseg slmodes">' + _btn('m', 't', 'At a time', 'A una hora')
                   + _btn('m', 'd', 'Whole day', 'Todo el día') + '</div>'
                   f'<div class="sltime"><input type="range" min="0" max="{n}" value="{n}" aria-label="time">'
                   '<b class="slclock"></b></div>')
    pic = ''
    if has_draw:
        pic = ('<div class="slpic"><img alt="layout" loading="lazy"><svg class="slsvg" preserveAspectRatio="none"></svg></div>'
               '<div class="note slsrc"></div>')
    notes = ''.join(f'<p class="note">{t(*_note(n))}</p>' for n in p.get('notes') or [])
    intro = t('Each numbered dot is one string: a chain of panels wired in series into one inverter input. '
              'The shaded panels show which inverter each part of the plant feeds. Hover or tap a dot for its details.',
              'Cada punto numerado es una cadena: paneles conectados en serie a una entrada del inversor. '
              'Los paneles sombreados muestran a qué inversor alimenta cada parte de la planta. Pase el cursor o toque un punto para ver el detalle.')
    return f'''<div class="card slcard" id="sl{pk}">
<h2 data-en="Strings on the plant layout" data-es="Cadenas en la distribución de la planta">Strings on the plant layout</h2>
<p class="slintro">{intro}</p>
<div class="slchk"></div>
{stale}<div class="slbar">{views}{pics}{timebar}</div>
<div class="slmain"><div class="sldraw">{pic}</div><div class="slside"><div class="sllegend"></div><div class="slsum"></div><div class="sllist"></div></div></div>
<div class="slgrid"></div>
<div class="sltip" role="tooltip"></div>
<p class="note">{t(*SRC_NOTE[p['src']])} {t('A colour is a pointer for the O&M team, not an alarm.', 'Un color es una pista para O&M, no una alarma.')}</p>{notes}
<script type="application/json" class="sldata">{_embed(p)}</script>
</div>'''


CSS = '''
.slcard .slintro{font-size:13.5px;color:#3c4043;margin:0 0 8px;max-width:980px}
.slcard .slchk{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;color:#5f6368;margin-bottom:10px}
.slcard .slchk span.ok b{color:#137333}.slcard .slchk span.bad b{color:#b06000}
.slcard .slbar{display:flex;gap:10px 14px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.slcard .slseg{display:inline-flex;border:1px solid #dadce0;border-radius:8px;overflow:hidden}
.slcard .slbtn{background:#fff;border:0;padding:6px 12px;font:inherit;font-size:13px;color:#3c4043;cursor:pointer}
.slcard .slbtn+.slbtn{border-left:1px solid #dadce0}
.slcard .slbtn.on{background:#1c2733;color:#fff}
.slcard .sltime{display:flex;align-items:center;gap:10px;flex:1;min-width:200px;max-width:420px}
.slcard .sltime input{flex:1;accent-color:#1c2733}
.slcard .slclock{font-variant-numeric:tabular-nums;min-width:48px}
.slcard .slmain{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.slcard .sldraw{flex:3 1 560px;min-width:0}
.slcard .sldraw:empty{display:none}
.slcard .slside{flex:1 1 260px;font-size:13px;min-width:240px}
.slcard .slpic{position:relative;width:100%;border-radius:8px;overflow:hidden;border:1px solid #e4e7ea;background:#f1f3f4}
.slcard .slpic img{display:block;width:100%;height:auto;filter:saturate(.18) contrast(.9) brightness(1.06)}
.slcard .slsvg{position:absolute;inset:0;width:100%;height:100%}
.slcard .slsvg .mk{cursor:pointer}
.slcard .slsvg .mk.hl .dot{stroke:#1a73e8!important;stroke-width:3px}
.slcard .sllegend h4{margin:2px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#5f6368}
.slcard .sllegend table{width:100%;border-collapse:collapse;font-size:12.5px;margin-bottom:10px}
.slcard .sllegend td{padding:3px 4px;border-bottom:1px solid #f0f1f3;text-align:left;white-space:normal;vertical-align:top}
.slcard .sllegend td.r{text-align:right;white-space:nowrap}
.slcard .sw{display:inline-block;width:12px;height:12px;border-radius:50%;vertical-align:-2px;margin-right:6px}
.slcard .sq{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-2px;margin-right:6px}
.slcard .slsum{margin-bottom:8px;font-size:12.5px;color:#3c4043}
.slcard .sllist div{padding:5px 7px;border-radius:6px;cursor:pointer;border:1px solid #eceef0;margin-bottom:4px}
.slcard .sllist div:hover{background:#f6f7f8}
.slcard .slgrid{margin-top:12px;display:flex;flex-direction:column;gap:6px}
.slcard .slinv{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.slcard .slinv .nm{width:170px;font-size:12.5px;color:#3c4043;border-left:5px solid #ccc;padding-left:7px}
.slcard .slinv .mp{display:inline-flex;gap:3px;padding:3px;border:1px solid #eceef0;border-radius:7px}
.slcard .chip{width:24px;height:24px;border-radius:6px;color:#fff;font-size:11px;font-weight:600;display:flex;align-items:center;justify-content:center;cursor:pointer;box-sizing:border-box}
.slcard .chip.hl{outline:3px solid #1a73e8}
.slcard .sltip{position:fixed;z-index:60;display:none;background:#1c2733;color:#fff;border-radius:8px;padding:9px 11px;font-size:12.5px;line-height:1.45;max-width:340px;pointer-events:none;box-shadow:0 6px 24px rgba(0,0,0,.25)}
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
 var nb=P.t.length, mode=(P.src==='5min'&&P.live)?'t':'d', view=(P.src==='none'?'i':'h'), at=nb-1, dr=0, pin=null;
 var IC={}; P.inv.forEach(function(v){IC[v.sn]=v;});
 function nr(x){var m=String(x||'').match(/(\d+)\s*$/);return m?m[1]:'';}
 function dname(sn){var v=IC[sn]||{};if(!v.dn)return '';var d=nr(v.dn);return (d&&d===nr(v.il))?'':(L()?'INVERSOR ':'INVERSOR ')+d;}
 var range=card.querySelector('.sltime input'), clock=card.querySelector('.slclock'), tip=card.querySelector('.sltip');
 function icol(sn){return (IC[sn]||{}).col||'#888';}
 function cls(k){return mode==='t'&&nb?(P.c[k]||'')[at]||'n':(P.dy[k]||{}).c||'n';}
 function hcol(c){return P.col[c]||P.col.n;}
 function num(v,d){return v==null?'-':Number(v).toFixed(d==null?1:d);}
 function pct(v){return v==null?'-':Math.round(100*v)+' %';}
 function sname(s){var l=L();return s.cad.length?((l?'cadena ':'string ')+s.cad.join(', ')):(s.ch[0]==='m'?'MPPT '+s.ch.slice(1):(l?'entrada ':'input ')+s.ch.slice(1));}
 function chtxt(s){var l=L();return s.ch[0]==='m'?('MPPT '+s.ch.slice(1)+(s.per>1?(l?' (compartida por ':' (shared by ')+s.per+(l?' cadenas)':' strings)'):'')):((l?'entrada ':'input ')+s.ch.slice(1)+(s.mp?' · MPPT '+s.mp:''));}
 function ratio(k){var s=P.s[k];if(!(mode==='t'&&nb))return (P.dy[k]||{}).x;var i=(P.i[k]||[])[at],m=(P.m[s.g]||[])[at];return (i!=null&&m)?i/m:null;}
 function tipHtml(k){var s=P.s[k],l=L(),c=cls(k),h=[],iv=IC[s.sn]||{};
  h.push('<div class="h"><span class="sw" style="background:'+icol(s.sn)+'"></span>'+E(s.il)+(dname(s.sn)?' <span class="mut">('+(l?'en el plano: ':'on the drawing: ')+E(dname(s.sn))+')</span>':'')+' · '+sname(s)+'</div>');
  h.push('<div class="mut">'+chtxt(s)+' · '+E(s.sn)+'</div>');
  h.push('<div>'+(s.n?s.n+' x ':'')+E(s.mod)+(s.kwp?' · '+num(s.kwp,2)+' kWp':'')+(s.or?' · '+E(s.or):'')+'</div>');
  if(mode==='t'&&nb){var i=(P.i[k]||[])[at],m=(P.m[s.g]||[])[at];
   h.push('<div>'+(l?'A las ':'At ')+P.t[at]+': <b>'+num(i)+' A</b>'+(s.per>1?(l?' por cadena':' per string'):'')+' · '+(l?'vecinas ':'neighbours ')+num(m)+' A · <b>'+pct(ratio(k))+'</b></div>');}
  var d=P.dy[k]||{};
  if(d.ah!=null||d.x!=null){h.push('<div>'+(l?'Día: ':'Day: ')+(d.ah!=null?num(d.ah)+' Ah':'')+(d.x!=null?' · '+pct(d.x)+(l?' de sus vecinas':' of its neighbours'):'')+'</div>');}
  if(s.nm!=null)h.push('<div class="mut">'+(l?'Su normal (30 días): ':'Its normal (30 days): ')+pct(s.nm)+(l?' de sus vecinas':' of its neighbours')+'</div>');
  h.push('<div><span class="sw" style="background:'+hcol(c)+'"></span>'+E((P.cls[c]||['',''])[l])+'</div>');
  h.push('<div class="mut">'+(l?'Vecinas: ':'Neighbours: ')+E((P.gn[s.g]||['',''])[l])+'</div>');
  h.push('<div class="mut">'+E((P.conf[s.cf]||['',''])[l])+'</div>');
  if(s.note)h.push('<div class="mut">'+E(s.note)+'</div>');
  return h.join('');}
 function areaHtml(sn){var l=L(),iv=IC[sn]||{};
  return '<div class="h"><span class="sw" style="background:'+icol(sn)+'"></span>'+E(iv.il||sn)+(dname(sn)?' <span class="mut">('+(l?'en el plano: ':'on the drawing: ')+E(dname(sn))+')</span>':'')+'</div><div class="mut">'+E(sn)+' · '+E(iv.model||'')+'</div><div>'+(iv.n||0)+(l?' cadenas':' strings')+(iv.kwp?' · '+num(iv.kwp,1)+' kWp':'')+'</div>'+
   (iv.placed<iv.n?'<div class="mut">'+(l?'El plano marca el área del inversor; sus cadenas sin número están en la cuadrícula de abajo.':'The drawing marks this inverter\'s area; its unnumbered strings are in the grid below.')+'</div>':'');}
 function show(ev,htmlTxt){tip.innerHTML=htmlTxt;tip.style.display='block';
  var x=ev.clientX+14,y=ev.clientY+14,w=tip.offsetWidth,hh=tip.offsetHeight;
  if(x+w>window.innerWidth-8)x=ev.clientX-w-14; if(y+hh>window.innerHeight-8)y=ev.clientY-hh-14;
  tip.style.left=Math.max(4,x)+'px';tip.style.top=Math.max(4,y)+'px';}
 function hide(){if(pin==null)tip.style.display='none';}
 function highlight(k,on){card.querySelectorAll('[data-k="'+k+'"]').forEach(function(e){e.classList.toggle('hl',on);});}
 function bindTip(el,k){el.addEventListener('mousemove',function(ev){if(pin==null){show(ev,tipHtml(k));highlight(k,true);}});
  el.addEventListener('mouseleave',function(){highlight(k,false);hide();});
  el.addEventListener('click',function(ev){ev.stopPropagation();pin=(pin===k?null:k);show(ev,tipHtml(k));if(pin==null)hide();});}
 // ------------------------------------------------------------- checks
 function checks(){var l=L(),c=P.chk||{},o=[];
  function row(lbl,obj,keys){if(!obj)return;var vals=keys.filter(function(k){return obj[k[0]]!=null;}).map(function(k){return [k,obj[k[0]]];});
   if(!vals.length)return;var same=vals.every(function(v){return v[1]===vals[0][1];});
   o.push('<span class="'+(same?'ok':'bad')+'">'+lbl+': '+vals.map(function(v){return v[0][l+1]+' <b>'+v[1]+'</b>';}).join(' · ')+(same?' ✓':'')+'</span>');}
  row(l?'Inversores':'Inverters',c.inverters,[['monitoring','monitoring','monitoreo'],['workbook','workbook','configuración'],['drawing','drawing','plano'],['helioscope','Helioscope','Helioscope']]);
  row(l?'Cadenas':'Strings',c.strings,[['workbook','monitored','monitoreadas'],['drawing_labels','numbered on the drawing','numeradas en el plano'],['helioscope','Helioscope design','diseño Helioscope']]);
  card.querySelector('.slchk').innerHTML=o.join('');}
 // ---------------------------------------------------------------- picture
 var svg=card.querySelector('.slsvg'), img=card.querySelector('.slpic img');
 function drawPic(){if(!svg)return;var D=P.d[dr];img.src=D.img;
  var bx=D.box,w=bx[2]-bx[0],h=bx[3]-bx[1];svg.setAttribute('viewBox',bx[0]+' '+bx[1]+' '+w+' '+h);
  while(svg.firstChild)svg.removeChild(svg.firstChild);
  var r=Math.max(w,h)/95;
  P.a.forEach(function(a){if(a.d!==D.id)return;var g=document.createElementNS(NS,'g');g.setAttribute('class','ar');g.dataset.sn=a.sn;
   a.r.forEach(function(q){var e=document.createElementNS(NS,'rect');e.setAttribute('x',q[0]);e.setAttribute('y',q[1]);e.setAttribute('width',q[2]);e.setAttribute('height',q[3]);g.appendChild(e);});
   a.pl.forEach(function(q){var e=document.createElementNS(NS,'polygon');e.setAttribute('points',q.join(' '));g.appendChild(e);});
   g.setAttribute('fill',icol(a.sn));g.style.cursor='pointer';
   g.addEventListener('mousemove',function(ev){if(pin==null)show(ev,areaHtml(a.sn));});g.addEventListener('mouseleave',hide);
   svg.appendChild(g);});
  P.s.forEach(function(s,k){s.p.forEach(function(q){if(q[0]!==D.id)return;
   var g=document.createElementNS(NS,'g');g.setAttribute('class','mk');g.dataset.k=k;
   if(s.cf==='assumed'){var o=document.createElementNS(NS,'circle');o.setAttribute('cx',q[1]);o.setAttribute('cy',q[2]);o.setAttribute('r',r*1.45);
    o.setAttribute('fill','none');o.setAttribute('stroke','#f28c28');o.setAttribute('stroke-width',r*0.32);o.setAttribute('stroke-dasharray',(r*0.55)+' '+(r*0.35));g.appendChild(o);}
   var c=document.createElementNS(NS,'circle');c.setAttribute('class','dot');c.setAttribute('cx',q[1]);c.setAttribute('cy',q[2]);c.setAttribute('r',r);
   c.setAttribute('stroke-width',r*0.34);if(s.cf==='order')c.setAttribute('stroke-dasharray',(r*0.5)+' '+(r*0.3));
   var tx=document.createElementNS(NS,'text');tx.setAttribute('x',q[1]);tx.setAttribute('y',q[2]+r*0.38);tx.setAttribute('text-anchor','middle');
   var lb=q[3]!=null?String(q[3]):s.ch.slice(1);
   tx.setAttribute('font-size',r*(lb.length>1?0.98:1.15));tx.setAttribute('font-weight','700');tx.setAttribute('fill','#fff');tx.setAttribute('pointer-events','none');
   tx.textContent=lb;g.appendChild(c);g.appendChild(tx);bindTip(g,k);svg.appendChild(g);});});
  var l=L();card.querySelector('.slsrc').innerHTML=(D.kind==='helioscope'?(l?'Diseño Helioscope: ':'Helioscope design: '):(l?'Plano: ':'Drawing: '))+E(D.src)+
   (D.kind==='helioscope'&&!D.mk?(' · <b>'+(l?'sin marcas de cadenas: el diseño no coincide con el plano de obra':'no string marks: the design does not line up with the as-built drawing')+'</b>'):'')+
   (D.kind==='helioscope'&&D.mk?(' · '+(l?'cadenas trasladadas desde el plano: ':'strings carried over from the drawing: ')+Math.round(100*(D.pr||0))+(l?' % de los bloques de módulos emparejados':' % of the module blocks matched')):'');
  card.querySelectorAll('.sltabs .slbtn').forEach(function(b){b.classList.toggle('on',+b.dataset.dr===dr);});
  paint();}
 // ------------------------------------------------------------------- grid
 var grid=card.querySelector('.slgrid');
 (function(){var byInv={};P.s.forEach(function(s,k){(byInv[s.sn]=byInv[s.sn]||[]).push(k);});
  (P.io||[]).filter(function(sn){return byInv[sn];}).forEach(function(sn){var row=document.createElement('div');row.className='slinv';
   var iv=IC[sn]||{},nm=document.createElement('div');nm.className='nm';nm.style.borderLeftColor=icol(sn);
   nm.innerHTML='<b>'+E(iv.il||sn)+'</b>'+(dname(sn)?' <span class="note">('+(L()?'plano: ':'drawing: ')+E(dname(sn))+')</span>':'')+'<br><span class="note">'+E(sn)+'</span>';row.appendChild(nm);
   var mp={},mo=[];byInv[sn].forEach(function(k){var m=P.s[k].mp||0;if(!mp[m]){mp[m]=[];mo.push(m);}mp[m].push(k);});
   mo.sort(function(a,b){return a-b;}).forEach(function(m){var box=document.createElement('div');box.className='mp';box.title='MPPT '+m;
    mp[m].forEach(function(k){var c=document.createElement('div');c.className='chip';c.dataset.k=k;var s=P.s[k];
     c.textContent=s.cad.length===1?s.cad[0]:s.ch.slice(1);bindTip(c,k);box.appendChild(c);});row.appendChild(box);});
   grid.appendChild(row);});})();
 document.addEventListener('click',function(){if(pin!=null){pin=null;tip.style.display='none';}});
 // ----------------------------------------------------------------- legend
 function legend(cnt){var l=L(),h=[];
  h.push('<h4>'+(l?'Inversores':'Inverters')+'</h4><table>');
  P.inv.forEach(function(v){h.push('<tr><td><span class="sq" style="background:'+v.col+'"></span><b>'+E(v.il)+'</b>'+(dname(v.sn)?'<br><span class="note">'+(l?'en el plano: ':'on the drawing: ')+E(dname(v.sn))+'</span>':'')+
   '</td><td><span class="note">'+E(v.sn)+'<br>'+E(v.model)+'</span></td><td class="r">'+v.n+(l?' cad.':' str.')+(v.placed!==v.n?'<br><span class="note">'+v.placed+(l?' en plano':' on drawing')+'</span>':'')+
   (v.kwp?'<br><span class="note">'+num(v.kwp,1)+' kWp</span>':'')+'</td></tr>');});
  h.push('</table>');
  if(view==='h'){
   h.push('<h4>'+(l?'Color del punto':'Dot colour')+'</h4><table>');
   ['g','a','r','z','d','n'].forEach(function(c){h.push('<tr><td><span class="sw" style="background:'+hcol(c)+'"></span>'+E(P.cls[c][l])+'</td><td class="note">'+E(P.rule[c][l])+'</td><td class="r">'+(cnt[c]||0)+'</td></tr>');});
   h.push('</table><div class="note" style="margin-bottom:8px">'+(l?'Borde del punto = color de su inversor. Borde punteado = entrada por el orden del inversor; anillo naranja = confirmar en sitio.':'Ring of the dot = its inverter\'s colour. Dashed ring = input by the inverter order; orange ring = confirm on site.')+'</div>');
  } else {
   h.push('<div class="note" style="margin-bottom:8px">'+(l?'Cada punto y cada panel en el color de su inversor. Borde punteado = entrada por el orden del inversor; anillo naranja = confirmar en sitio.':'Every dot and every panel in its inverter\'s colour. Dashed ring = input by the inverter order; orange ring = confirm on site.')+'</div>');
  }
  card.querySelector('.sllegend').innerHTML=h.join('');}
 // ------------------------------------------------------------------ paint
 function paint(){var l=L(),cnt={};checks();
  P.s.forEach(function(s,k){var c=cls(k);cnt[c]=(cnt[c]||0)+1;});
  card.querySelectorAll('[data-k]').forEach(function(e){var k=+e.dataset.k,s=P.s[k],c=cls(k);
   var fill=view==='h'?hcol(c):icol(s.sn), ring=view==='h'?icol(s.sn):'#ffffff';
   if(e.tagName==='g'){var d=e.querySelector('.dot');d.setAttribute('fill',fill);d.setAttribute('stroke',ring);}
   else{e.style.background=fill;e.style.border=view==='h'?('3px solid '+icol(s.sn)):'0';}});
  card.querySelectorAll('.slsvg g.ar').forEach(function(g){g.setAttribute('fill-opacity',view==='h'?'0.16':'0.42');});
  if(clock)clock.textContent=nb?P.t[at]:'';
  var timeOn=view==='h'&&P.src==='5min';
  var tm=card.querySelector('.slmodes');if(tm)tm.style.display=timeOn?'':'none';
  if(range)range.parentNode.style.display=(timeOn&&mode==='t')?'':'none';
  card.querySelectorAll('.slmodes .slbtn').forEach(function(b){b.classList.toggle('on',b.dataset.m===mode);});
  card.querySelectorAll('.slbtn[data-v]').forEach(function(b){b.classList.toggle('on',b.dataset.v===view);});
  legend(cnt);
  var sum=card.querySelector('.slsum'),list=card.querySelector('.sllist');list.innerHTML='';
  if(view!=='h'){sum.innerHTML='';return;}
  var when=(mode==='t'&&nb)?((l?'A las ':'At ')+P.t[at]):(l?'Todo el día':'Whole day');
  var bad=[];P.s.forEach(function(s,k){var c=cls(k);if(c==='z'||c==='r'||c==='a')bad.push(k);});
  sum.innerHTML='<b>'+when+'</b>: '+(cnt.g||0)+(l?' bien, ':' ok, ')+bad.length+(l?' bajas':' low')+(l?' de ':' of ')+P.s.length+(l?' cadenas':' strings')+'.';
  var rank={z:0,r:1,a:2};bad.sort(function(a,b){return rank[cls(a)]-rank[cls(b)]||((ratio(a)||0)-(ratio(b)||0));});
  bad.slice(0,10).forEach(function(k){var s=P.s[k],d=document.createElement('div'),c=cls(k),r=ratio(k);
   d.innerHTML='<span class="sw" style="background:'+hcol(c)+';box-shadow:0 0 0 2px '+icol(s.sn)+'"></span><b>'+E(s.il)+'</b> · '+sname(s)+': '+(r!=null?pct(r)+(l?' de sus vecinas':' of its neighbours'):E(P.cls[c][l]))+(s.nm!=null?' <span class="note">('+(l?'normal ':'normal ')+pct(s.nm)+')</span>':'');
   d.addEventListener('mouseenter',function(){highlight(k,true);});d.addEventListener('mouseleave',function(){highlight(k,false);});
   d.addEventListener('click',function(ev){ev.stopPropagation();pin=k;show(ev,tipHtml(k));});list.appendChild(d);});
  if(bad.length>10){var m=document.createElement('div');m.style.cursor='default';m.textContent='+'+(bad.length-10);list.appendChild(m);}
 }
 if(range){range.addEventListener('input',function(){at=+range.value;paint();});}
 card.querySelectorAll('.slmodes .slbtn').forEach(function(b){b.addEventListener('click',function(){mode=b.dataset.m;paint();});});
 card.querySelectorAll('.slbtn[data-v]').forEach(function(b){b.addEventListener('click',function(){view=b.dataset.v;paint();});});
 card.querySelectorAll('.sltabs .slbtn').forEach(function(b){b.addEventListener('click',function(){dr=+b.dataset.dr;drawPic();});});
 if(svg&&P.d.length)drawPic();else paint();
 document.querySelectorAll('.lang-btn,[data-l]').forEach(function(b){b.addEventListener('click',function(){setTimeout(function(){if(svg&&P.d.length)drawPic();else paint();},0);});});
});
})();
'''


def card(p: dict) -> str:
    """The card with its style and script (one block per page)."""
    return render(p) + f'<style>{CSS}</style><script>{JS}</script>'
