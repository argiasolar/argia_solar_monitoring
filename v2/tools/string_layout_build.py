"""Build the server-only PPA string layout files (v317). Run by hand, not a job.

    python tools/string_layout_build.py <drawings dir> <out dir> <Strings.csv>

<drawings dir> holds the ARGIA drawings named as in F below; Strings.csv
is sheet 'Strings' of ARGIA_PPA_String_Configuration.xlsx saved as CSV.
The output (<PK>.json + <PK>_<drawing>.jpg) goes to /opt/argia/layouts/
on the server (customer data: never into the repo). Needs pdfplumber,
Pillow and poppler's pdftoppm.

Inputs: the ARGIA drawings (PDF, 'UBICACION DE MODULOS FV') and
ARGIA_PPA_String_Configuration.xlsx (sheet Strings).
Outputs (OUT dir): <PK>.json + <PK>_<drawing>.jpg per plant.

String positions come from the drawing's own string-number labels
(PDF text, exact), the inverter from the label colour or the drawing
table, the channel from the drawing table or the inverter's input order.
Every mapping carries a confidence: 'table' (the drawing's own table
names the input), 'order' (the n-th string of the inverter is input n,
the drawing gives no input table), 'assumed' (the drawing and the
monitored inputs disagree; placed where most likely, to confirm on site).
"""
import csv
import datetime as dt
import json
import os
import subprocess
import sys

import pdfplumber
from PIL import Image

U = sys.argv[1]
OUT = sys.argv[2]
XLSX_CSV = sys.argv[3]
os.makedirs(OUT, exist_ok=True)
R = 150                 # render dpi
MAXW = 1800             # px, the page scales it down

F = {
    'MEX2': '3f0b601f-VITALMEX_UBICACI_N.pdf',
    'NL1': 'a77689a3-02-PLASTIC-OMNIUM-NL_20250110_V4.0-UBICACI_N.pdf',
    'SLP1': '85eab6bf-ENG_DESIGN_QUIMICA_COYOACAN_SLP_DC_Wiring_Diagram_v2.pdf',
    'SLP2': '6b2fffc6-TURISTICA_ARIZONA_SLP_20240711_V5.0-UBICACI_N.pdf',
    'GTO1': '9d85d5b7-02-TAIGENE_20251103_V7.0-UBICACI_N.pdf',
    'GTO1_V6': '2320fdaf-02-TAIGENE_20241120_V6.0-UBICACI_N_DE_MODULOS_FV.pdf',
}
SRC = {
    'MEX2': 'VITALMEX - Ubicación de módulos FV, rev. V5.0 (07.04.2026)',
    'NL1': 'PLASTIC OMNIUM NL - Ubicación de módulos FV, rev. V4.0 (10.01.2025)',
    'SLP1': 'Química Coyoacán - Recolocación de módulos FV, rev. V1.0 (12.07.2024)',
    'SLP2': 'Turística Arizona SLP - Ubicación de módulos FV, rev. V4.0 (11.07.2024)',
    'GTO1': 'Taigene - Ubicación de módulos FV, rev. V7.0 (05.11.2025, ampliación 234 kWp); inverters 1-4 areas from rev. V6.0 (20.11.2024)',
}
PLANT_NAME = {'Quimica Coyoacan': 'SLP1', 'Turistica Arizona': 'SLP2', 'Taigene': 'GTO1',
              'Plastic Omnium': 'NL1', 'SAG': 'MEX1', 'Vitalmex': 'MEX2'}


def col(c):
    if isinstance(c, (list, tuple)):
        return tuple(round(float(x), 2) for x in c)
    return c


def labels(page, maxy, maxx):
    """Numeric labels as {'n','x','y','col'} (centre, pts); split digits merged."""
    chars = sorted((c for c in page.chars if c['text'].isdigit() and c['top'] < maxy and c['x1'] < maxx),
                   key=lambda c: c['x0'])
    out = []
    for c in chars:
        k = col(c['non_stroking_color'])
        cur = next((w for w in out if w['col'] == k and abs(w['size'] - c['size']) < 0.2
                    and abs(c['top'] - w['last_top']) < 1.5 and 0 < c['x0'] - w['last_x0'] < 0.8 * c['size']), None)
        if cur:
            cur['text'] += c['text']
            cur['x1'] = c['x1']
            cur['last_top'] = c['top']
            cur['last_x0'] = c['x0']
            cur['bottom'] = max(cur['bottom'], c['bottom'])
            continue
        out.append({'text': c['text'], 'x0': c['x0'], 'x1': c['x1'], 'top': c['top'], 'bottom': c['bottom'],
                    'last_top': c['top'], 'last_x0': c['x0'], 'col': k, 'size': c['size']})
    return [{'n': int(w['text']), 'x': (w['x0'] + w['x1']) / 2, 'y': (w['top'] + w['bottom']) / 2,
             'col': w['col'], 'size': round(w['size'], 1)} for w in out]


def render(pdf, crop, out):
    x0, y0, x1, y1 = crop
    s = R / 72.0
    tmp = out + '.tmp'
    subprocess.run(['pdftoppm', '-r', str(R), '-x', str(int(x0 * s)), '-y', str(int(y0 * s)),
                    '-W', str(int((x1 - x0) * s)), '-H', str(int((y1 - y0) * s)), '-png', '-singlefile',
                    pdf, tmp], check=True)
    im = Image.open(tmp + '.png').convert('RGB')
    if im.width > MAXW:
        im = im.resize((MAXW, round(im.height * MAXW / im.width)), Image.LANCZOS)
    im.save(out, 'JPEG', quality=82, optimize=True, progressive=True)
    os.remove(tmp + '.png')
    return im.width, im.height


def xlsx_strings():
    rows = list(csv.DictReader(open(XLSX_CSV, encoding='utf-8')))
    out = {}
    for r in rows:
        pk = PLANT_NAME[r['Plant']]
        tid = r['Telemetry ID'].strip()
        if tid.startswith('Istr'):
            ch = 's' + tid[4:]
        elif tid.startswith('PV'):
            ch = 's' + tid[2:]
        elif tid.startswith('MPPT'):
            ch = 'm' + tid[4:]
        else:
            raise ValueError(tid)
        out[(pk, r['Inverter S/N'].strip(), ch)] = r
    return out


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


USED = set()


def string_rec(x, pk, sn, inv, ch, pts, conf, note=None, per=1):
    """pts: [{'d': drawing id, 'x', 'y', 'cad'}] (empty = not on any drawing)."""
    r = x.get((pk, sn, ch))
    if r is None:
        raise SystemExit(f'{pk} {sn} {ch} ({pts}) is not in the string configuration')
    if (pk, sn, ch) in USED:
        raise SystemExit(f'{pk} {sn} {ch} placed twice')
    USED.add((pk, sn, ch))
    tilt, az = fnum(r['Tilt (deg)']), fnum(r['Azimuth (deg)'])
    mod = r['Module'].split(' (')[0]
    rec = {'sn': sn, 'ch': ch, 'inv': inv, 'mppt': int(float(r['MPPT'])) if r['MPPT'] else None,
           'name': r['Standard name'], 'kwp': fnum(r['kWp']),
           'modules': int(float(r['Modules'])) if fnum(r['Modules']) else None, 'module': mod,
           'tilt': tilt, 'az': az, 'orient': r['Orientation'], 'group': f'{mod}|{tilt:g}|{az:g}',
           'pos': [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in p.items()} for p in pts],
           'conf': conf if pts else 'none'}
    if per > 1:
        rec['per'] = per
    notes = [n for n in (r['Note'], note) if n]
    if notes:
        rec['note'] = '; '.join(notes)
    return rec


def coloured_bbox(page, box):
    """bbox of coloured (non-grey) vector shapes inside box."""
    bx = [1e9, 1e9, -1e9, -1e9]
    for kind in ('rects', 'lines', 'curves'):
        for o in getattr(page, kind):
            c = col(o.get('stroking_color'))
            if not isinstance(c, tuple) or len(c) != 3 or max(c) - min(c) < 0.3:
                continue
            if o['x0'] < box[0] or o['x1'] > box[2] or o['top'] < box[1] or o['bottom'] > box[3]:
                continue
            bx = [min(bx[0], o['x0']), min(bx[1], o['top']), max(bx[2], o['x1']), max(bx[3], o['bottom'])]
    return bx


def crop_of(page, labs, aerial, margin=28):
    bx = coloured_bbox(page, aerial)
    for l in labs:
        bx = [min(bx[0], l['x'] - 8), min(bx[1], l['y'] - 8), max(bx[2], l['x'] + 8), max(bx[3], l['y'] + 8)]
    return [round(max(aerial[0], bx[0] - margin)), round(max(aerial[1], bx[1] - margin)),
            round(min(aerial[2], bx[2] + margin)), round(min(aerial[3], bx[3] + margin))]


def aerial_box(page):
    big = max(page.images, key=lambda i: (i['x1'] - i['x0']) * (i['bottom'] - i['top']))
    return [big['x0'], big['top'], big['x1'], big['bottom']]


# --------------------------------------------------------------- plants
X = xlsx_strings()
SN = {}
for (pk, sn, ch), r in X.items():
    SN[(pk, r['Inverter'])] = sn


def page_of(key):
    return pdfplumber.open(os.path.join(U, F[key])).pages[0]


def draw(pk, did, page, labs, title_en, title_es, extra=None):
    aerial = aerial_box(page)
    crop = crop_of(page, labs + (extra or []), aerial)
    img = f'{pk}_{did}.jpg'
    pw, ph = render(os.path.join(U, F[pk]), crop, os.path.join(OUT, img))
    return {'id': did, 'image': img, 'src': SRC[pk], 'title_en': title_en, 'title_es': title_es,
            'box': crop, 'px': [pw, ph]}


def pt(did, lab):
    return {'d': did, 'x': lab['x'], 'y': lab['y'], 'cad': lab['n']}


def by_n(labs):
    d = {}
    for l in labs:
        if l['n'] in d:
            raise SystemExit(f'label {l["n"]} twice')
        d[l['n']] = l
    return d


def mex2():
    p = page_of('MEX2')
    labs = [l for l in labels(p, 640, 980) if l['size'] == 8.4 and 1 <= l['n'] <= 36]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 37)), sorted(L)
    d = draw('MEX2', 'main', p, labs, 'Roof layout', 'Distribución en techo')
    chans = [1, 2, 4, 5, 7, 8, 10, 11, 13, 14, 16, 17]       # drawing table: Cadena -> Canal PV
    strings = []
    for n in range(1, 37):
        inv = f'INV{(n - 1) // 12 + 1}'
        strings.append(string_rec(X, 'MEX2', SN[('MEX2', inv)], inv, f's{chans[(n - 1) % 12]}',
                                  [pt('main', L[n])], 'table'))
    return [d], strings, []


def nl1():
    p = page_of('NL1')
    labs = [l for l in labels(p, 640, 980) if l['size'] == 4.9 and 1 <= l['n'] <= 49]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 50)), sorted(L)
    cols = {(1.0, 0.0, 0.0): 'INV1', (0.0, 0.0, 1.0): 'INV2', (0.95, 0.4, 0.13): 'INV3', (0.07, 0.4, 0.2): 'INV4'}
    d = draw('NL1', 'main', p, labs, 'Ground-mount layout', 'Distribución en piso')
    strings, k = [], {}
    for n in range(1, 50):
        inv = cols[L[n]['col']]
        if n == 25:          # the drawing's 13th string of inverter 2; monitoring: inverter 3 has 13 inputs
            strings.append(string_rec(X, 'NL1', SN[('NL1', 'INV3')], 'INV3', 's13', [pt('main', L[n])], 'assumed',
                                      'drawing: string 25 on inverter 2 (13 strings), monitored: inverter 2 has '
                                      '12 inputs and inverter 3 has 13; placed as inverter 3 input 13 - confirm on site'))
            continue
        k[inv] = k.get(inv, 0) + 1
        strings.append(string_rec(X, 'NL1', SN[('NL1', inv)], inv, f's{k[inv]}', [pt('main', L[n])], 'order'))
    return [d], strings, ['The 305 kWp expansion (inverters 5 and 6, strings 50-73) is not in the monitoring yet: '
                          'it is added when its inverters report.']


def slp1():
    p = page_of('SLP1')
    labs = [l for l in labels(p, 640, 980) if l['size'] == 5.3 and 1 <= l['n'] <= 18]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 19)), sorted(L)
    d = draw('SLP1', 'main', p, labs, 'Ground-mount layout', 'Distribución en piso')
    note = ('drawing: string 3 on MPPT 2 and strings 8, 9 on MPPT 3; monitored: inputs 3, 4 on MPPT 2 and '
            'input 5 on MPPT 3 - confirm on site')
    maps = {'INV1': {1: 's1', 2: 's2', 3: 's3', 8: 's4', 9: 's5', 13: 's9', 15: 's10', 17: 's11', 7: 's13'},
            'INV2': {4: 's1', 5: 's2', 6: 's3', 11: 's5', 12: 's6', 14: 's9', 16: 's10', 18: 's11', 10: 's13'}}
    strings = []
    for inv, m in maps.items():
        for n, ch in m.items():
            bad = inv == 'INV1' and n in (3, 8, 9)
            strings.append(string_rec(X, 'SLP1', SN[('SLP1', inv)], inv, ch, [pt('main', L[n])],
                                      'assumed' if bad else 'order', note if bad else None))
    return [d], strings, []


def slp2():
    p = page_of('SLP2')
    labs = [l for l in labels(p, 600, 1030) if l['size'] == 4.5 and 1 <= l['n'] <= 14]
    red = by_n([l for l in labs if l['col'] == (1.0, 0.0, 0.0)])
    blue = by_n([l for l in labs if l['col'] == (0.0, 0.0, 1.0)])
    assert sorted(red) == list(range(1, 14)) and sorted(blue) == list(range(1, 15)), (sorted(red), sorted(blue))
    d = draw('SLP2', 'main', p, labs, 'Roof and carport layout', 'Distribución en techo y estacionamiento')
    strings = []
    for inv, L, chans in (('INV1', red, list(range(1, 12)) + [13, 15]), ('INV2', blue, list(range(1, 14)) + [15])):
        for n in sorted(L):
            strings.append(string_rec(X, 'SLP2', SN[('SLP2', inv)], inv, f's{chans[n - 1]}', [pt('main', L[n])], 'order'))
    return [d], strings, []


def grey_origin(page):
    xs = [o for o in page.lines if col(o.get('stroking_color')) == (0.46, 0.46, 0.46)]
    return min(o['x0'] for o in xs), min(o['top'] for o in xs), max(o['x1'] for o in xs), max(o['bottom'] for o in xs)


def gto1():
    p7 = page_of('GTO1')
    labs = [l for l in labels(p7, 560, 1010) if l['size'] == 3.9 and l['col'] == (1.0, 0.0, 0.0) and 1 <= l['n'] <= 20]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 21)), sorted(L)
    # inverters 1-4: their module areas from rev. V6 (one colour per inverter), moved into the V7 frame
    p6 = page_of('GTO1_V6')
    g6, g7 = grey_origin(p6), grey_origin(p7)
    dx, dy = g7[0] - g6[0], g7[1] - g6[1]
    assert abs((g6[2] - g6[0]) - (g7[2] - g7[0])) < 3 and abs((g6[3] - g6[1]) - (g7[3] - g7[1])) < 3, (g6, g7)
    cols = {(1.0, 0.0, 1.0): 'INV1', (0.07, 0.61, 0.28): 'INV2', (1.0, 0.0, 0.0): 'INV3', (0.0, 0.0, 1.0): 'INV4'}
    rects = {v: [] for v in cols.values()}
    for kind in ('rects', 'curves'):
        for o in getattr(p6, kind):
            inv = cols.get(col(o.get('stroking_color')))
            if not inv or not (g6[0] <= o['x0'] and o['x1'] <= g6[2] and g6[1] <= o['top'] and o['bottom'] <= g6[3]):
                continue
            w, h = o['x1'] - o['x0'], o['bottom'] - o['top']
            if w < 2 or h < 2 or w > 80 or h > 80:
                continue
            rects[inv].append([round(o['x0'] + dx, 1), round(o['top'] + dy, 1), round(w, 1), round(h, 1)])
    areas = []
    extra = []
    for inv, rs in rects.items():
        rs = sorted({tuple(r) for r in rs})
        areas.append({'sn': SN[('GTO1', inv)], 'inv': inv, 'd': 'main', 'rects': [list(r) for r in rs]})
        extra += [{'x': r[0], 'y': r[1]} for r in rs]
    d = draw('GTO1', 'main', p7, labs, 'Roof layout (2026, with the 234 kWp expansion)',
             'Distribución en techo (2026, con la ampliación de 234 kWp)', extra)
    strings = []
    for n in range(1, 15):
        strings.append(string_rec(X, 'GTO1', SN[('GTO1', 'INV5')], 'INV5', f's{n}', [pt('main', L[n])], 'table'))
    note6 = ('drawing table: 3 MPPT x 2 strings (strings 15-20); monitoring: 2 combined inputs '
             '(4 strings + 2 strings) - confirm which strings share each input')
    strings.append(string_rec(X, 'GTO1', SN[('GTO1', 'INV6')], 'INV6', 'm1', [pt('main', L[n]) for n in (15, 16, 17, 18)],
                              'assumed', note6, per=4))
    strings.append(string_rec(X, 'GTO1', SN[('GTO1', 'INV6')], 'INV6', 'm2', [pt('main', L[n]) for n in (19, 20)],
                              'assumed', note6, per=2))
    return [d], strings, areas


def unplaced(pk):
    out = []
    for (p, sn, ch), r in sorted(X.items(), key=lambda kv: (kv[0][1], len(kv[0][2]), kv[0][2])):
        if p == pk and (p, sn, ch) not in USED:
            out.append(string_rec(X, pk, sn, r['Inverter'], ch, [], 'none'))
    return out


BUILD = {'MEX2': mex2, 'NL1': nl1, 'SLP1': slp1, 'SLP2': slp2, 'GTO1': gto1, 'MEX1': None}
for pk, fn in BUILD.items():
    drawings, strings, third = fn() if fn else ([], [], [])
    areas = third if pk == 'GTO1' else []
    notes = third if pk == 'NL1' else []
    strings += unplaced(pk)
    doc = {'plant': pk, 'built': dt.date.today().isoformat(), 'version': 1,
           'source': 'ARGIA_PPA_String_Configuration.xlsx (engine v0.3, 2026-10-05) + drawings',
           'drawings': drawings, 'strings': strings, 'areas': areas, 'notes': notes}
    json.dump(doc, open(os.path.join(OUT, f'{pk}.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    n_pos = sum(1 for s in strings if s['pos'])
    print(pk, len(drawings), 'drawing(s)', len(strings), 'channels', n_pos, 'placed',
          {c: sum(1 for s in strings if s['conf'] == c) for c in ('table', 'order', 'assumed', 'none')},
          len(areas), 'areas', [d['px'] for d in drawings])
