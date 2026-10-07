"""Build the server-only PPA string layout files (v317, v318). Run by hand, not a job.

    python tools/string_layout_build.py <src dir> <out dir> <Strings.csv> <Inverters.csv>

<src dir> holds the ARGIA drawings and Helioscope reports under the names in
PLANTS below (copied from the project folders on Drive); the CSVs are the
sheets 'Strings' and 'Inverters' of ARGIA_PPA_String_Configuration.xlsx.
Output: <PK>.json + <PK>_drawing.jpg + <PK>_helio.jpg per plant, for
/opt/argia/layouts/ on the server (customer data: never into the repo).
Needs pdfplumber, Pillow, numpy, opencv and poppler (pdftoppm, pdfimages).

Where each piece comes from
* String position: the drawing's own string-number label (PDF text, exact).
* Inverter of a string: the drawing table / label colour; at SAG the
  field-filled 'Mapeo de cadenas' form (27 Jul 2026).
* Input (channel) of a string, with a confidence per string:
  'table' (a table names the input), 'order' (string n = input n),
  'assumed' (drawing and monitoring disagree: placed where most likely,
  confirm on site), 'none' (not on the drawing: grid only).
* Inverter areas: the drawing's own modules, by the colour the drawing
  gives each inverter (Taigene inverters 1-4: from rev. V6.0, same frame).
* Helioscope view: the report's 'Detailed Layout' image. The drawing's
  module mask is registered onto the Helioscope module mask (blue): one
  affine by ECC, then each module block refined by its own shift (a
  Helioscope design can differ locally from the as-built drawing). A
  label moves with its block. Below HELIO_MIN_IOU overall overlap no
  marker is placed there and the page says why.
"""
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys

import cv2
import numpy as np
import pdfplumber
from PIL import Image

SRC, OUT, STRINGS_CSV, INVERTERS_CSV = sys.argv[1:5]
os.makedirs(OUT, exist_ok=True)
R = 150                 # drawing render dpi
MAXW = 1800             # px
HELIO_MIN_IOU = 0.55
HELIO_MIN_PAIRED = 0.9      # share of drawing blocks paired with a Helioscope block
PLANT_NAME = {'Quimica Coyoacan': 'SLP1', 'Turistica Arizona': 'SLP2', 'Taigene': 'GTO1',
              'Plastic Omnium': 'NL1', 'SAG': 'MEX1', 'Vitalmex': 'MEX2'}
RED, BLUE, MAGENTA = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 1.0)
PLANTS = {
    'MEX2': {'drawing': 'MEX2_drawing.pdf', 'src': 'VITALMEX - Ubicación de módulos FV, rev. V5.0 (07.04.2026)',
             'modules': {BLUE: 'INV1', RED: 'INV2', (0.0, 0.72, 0.18): 'INV3'},
             'helio': 'MEX2_helio.pdf', 'helio_src': 'Helioscope VITALMEX_610kWp (A. Gonzalez, 07.10.2026)'},
    'NL1': {'drawing': 'NL1_drawing.pdf', 'src': 'PLASTIC OMNIUM NL - Ubicación de módulos FV, rev. V4.0 (10.01.2025)',
            'modules': {RED: 'INV1', (0.0, 0.25, 1.0): 'INV2', (1.0, 0.5, 0.0): 'INV3', (0.36, 0.72, 0.36): 'INV4'},
            'helio': 'NL1_helio_ppa.pdf', 'helio_src': 'Helioscope PLASTIC OMNIUM Canadian 700W Bifacial (A. Gonzalez, 2026)'},
    'SLP1': {'drawing': 'SLP1_drawing.pdf', 'src': 'Química Coyoacán - Recolocación de módulos FV, rev. V1.0 (12.07.2024)',
             'modules': {(0.0, 0.25, 1.0): 'INV1', (1.0, 0.25, 0.0): 'INV2'},
             'helio': 'SLP1_helio_v2.pdf', 'helio_src': 'Helioscope COYOACAN QUIMICA relocation (A. Gonzalez, 09.02.2026)'},
    'SLP2': {'drawing': 'SLP2_drawing.pdf', 'src': 'Turística Arizona SLP - Ubicación de módulos FV, rev. V5.0 (11.07.2024)',
             'modules': {RED: 'INV1', (0.0, 0.25, 1.0): 'INV2'},
             'helio': 'SLP2_helio.pdf', 'helio_src': 'Helioscope TURISTICA ARIZONA PPA (E. Mora, 2024)'},
    'GTO1': {'drawing': 'GTO1_drawing.pdf',
             'src': 'Taigene - Ubicación de módulos FV, rev. V7.0 (05.11.2025, ampliación 234 kWp); inverters 1-4 from rev. V6.0 (20.11.2024)',
             'modules': {RED: 'INV5', (0.36, 0.72, 0.36): 'INV6'},
             'helio': 'GTO1_helio.pdf', 'helio_src': 'Helioscope TAIGENE Proyecto Integro 700 kW AC (A. Gonzalez, 07.10.2026)'},
    'MEX1': {'drawing': 'MEX1_drawing.pdf',
             'src': 'SAG - Ubicación de módulos FV, rev. V6.0 (15.07.2026); inputs from the field form Mapeo de cadenas (27.07.2026)',
             'modules': {RED: 'INV1', BLUE: 'INV2', MAGENTA: 'INV3'},
             'helio': 'MEX1_helio_ppa.pdf', 'helio_src': 'Helioscope SAG MEXICO - HUAWEI expansion (A. Gonzalez, 02.09.2026)'},
}


# ------------------------------------------------------------------ helpers
def col(c):
    if isinstance(c, (list, tuple)):
        return tuple(round(float(x), 2) for x in c)
    return c


def page_of(name):
    return pdfplumber.open(os.path.join(SRC, name)).pages[0]


def labels(page, maxy, maxx):
    """Numeric labels {'n','x','y','col','size'} (centre, pts); split digits merged."""
    chars = sorted((c for c in page.chars if c['text'].isdigit() and c['top'] < maxy and c['x1'] < maxx),
                   key=lambda c: c['x0'])
    out = []
    for c in chars:
        k = col(c['non_stroking_color'])
        cur = next((w for w in out if w['col'] == k and abs(w['size'] - c['size']) < 0.2
                    and abs(c['top'] - w['last_top']) < 1.5 and 0 < c['x0'] - w['last_x0'] < 0.8 * c['size']), None)
        if cur:
            cur['text'] += c['text']
            cur.update(x1=c['x1'], last_top=c['top'], last_x0=c['x0'], bottom=max(cur['bottom'], c['bottom']))
            continue
        out.append({'text': c['text'], 'x0': c['x0'], 'x1': c['x1'], 'top': c['top'], 'bottom': c['bottom'],
                    'last_top': c['top'], 'last_x0': c['x0'], 'col': k, 'size': c['size']})
    return [{'n': int(w['text']), 'x': (w['x0'] + w['x1']) / 2, 'y': (w['top'] + w['bottom']) / 2,
             'col': w['col'], 'size': round(w['size'], 1)} for w in out]


def by_n(labs):
    d = {}
    for lab in labs:
        if lab['n'] in d:
            raise SystemExit(f'label {lab["n"]} twice')
        d[lab['n']] = lab
    return d


def aerial_box(page):
    big = max(page.images, key=lambda i: (i['x1'] - i['x0']) * (i['bottom'] - i['top']))
    return [big['x0'], big['top'], big['x1'], big['bottom']]


def module_shapes(page, box, colours, maxsz=40, avoid=()):
    """Module outlines (x0, y0, x1, y1, inv) of the given stroke colours inside box;
    shapes centred within 5 pt of a label (the label's own circle) are skipped."""
    out = []
    for kind in ('curves', 'rects'):
        for o in getattr(page, kind):
            inv = colours.get(col(o.get('stroking_color')))
            if not inv:
                continue
            w, h = o['x1'] - o['x0'], o['bottom'] - o['top']
            if w < 1 or h < 1 or w > maxsz or h > maxsz:
                continue
            if not (box[0] <= o['x0'] and o['x1'] <= box[2] and box[1] <= o['top'] and o['bottom'] <= box[3]):
                continue
            cx, cy = (o['x0'] + o['x1']) / 2, (o['top'] + o['bottom']) / 2
            if any(abs(cx - a['x']) < 5 and abs(cy - a['y']) < 5 for a in avoid):
                continue
            out.append((o['x0'], o['top'], o['x1'], o['bottom'], inv))
    return out


def crop_of(shapes, labs, aerial, margin=28):
    xs = [s[0] for s in shapes] + [s[2] for s in shapes] + [lab['x'] for lab in labs]
    ys = [s[1] for s in shapes] + [s[3] for s in shapes] + [lab['y'] for lab in labs]
    return [round(max(aerial[0], min(xs) - margin)), round(max(aerial[1], min(ys) - margin)),
            round(min(aerial[2], max(xs) + margin)), round(min(aerial[3], max(ys) + margin))]


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


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_strings():
    out = {}
    for r in csv.DictReader(open(STRINGS_CSV, encoding='utf-8')):
        pk = PLANT_NAME[r['Plant']]
        tid = r['Telemetry ID'].strip()
        ch = {'Istr': 's', 'PV': 's', 'MPPT': 'm'}[re.match(r'[A-Za-z]+', tid).group(0)] + re.sub(r'\D', '', tid)
        out[(pk, r['Inverter S/N'].strip(), ch)] = r
    return out


def read_inverters():
    out = {}
    for r in csv.DictReader(open(INVERTERS_CSV, encoding='utf-8')):
        out.setdefault(PLANT_NAME[r['Plant']], []).append(r)
    return out


X = read_strings()
INVS = read_inverters()
SN = {(pk, r['Inverter']): r['Serial number'].strip() for pk, rows in INVS.items() for r in rows}
USED = set()


def string_rec(pk, sn, inv, ch, pts, conf, note=None, per=1):
    r = X.get((pk, sn, ch))
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


def pt(lab, did='drawing'):
    return {'d': did, 'x': lab['x'], 'y': lab['y'], 'cad': lab['n']}


def unplaced(pk):
    out = []
    for (p, sn, ch), r in sorted(X.items(), key=lambda kv: (kv[0][1], len(kv[0][2]), kv[0][2])):
        if p == pk and (p, sn, ch) not in USED:
            out.append(string_rec(pk, sn, r['Inverter'], ch, [], 'none'))
    return out


# ------------------------------------------------------------- per plant
def mex2(page):
    labs = [lab for lab in labels(page, 640, 980) if lab['size'] == 8.4 and 1 <= lab['n'] <= 36]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 37)), sorted(L)
    chans = [1, 2, 4, 5, 7, 8, 10, 11, 13, 14, 16, 17]       # drawing table: Cadena -> Canal PV
    out = []
    for n in range(1, 37):
        inv = f'INV{(n - 1) // 12 + 1}'
        out.append(string_rec('MEX2', SN[('MEX2', inv)], inv, f's{chans[(n - 1) % 12]}', [pt(L[n])], 'table'))
    return labs, out, []


def nl1(page):
    labs = [lab for lab in labels(page, 640, 980) if lab['size'] == 4.9 and 1 <= lab['n'] <= 49]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 50)), sorted(L)
    cols = {RED: 'INV1', BLUE: 'INV2', (0.95, 0.4, 0.13): 'INV3', (0.07, 0.4, 0.2): 'INV4'}
    out, k = [], {}
    for n in range(1, 50):
        inv = cols[L[n]['col']]
        if n == 25:          # the drawing's 13th string of inverter 2; monitoring: inverter 3 has 13 inputs
            out.append(string_rec('NL1', SN[('NL1', 'INV3')], 'INV3', 's13', [pt(L[n])], 'assumed',
                                  'drawing: string 25 on inverter 2 (13 strings), monitored: inverter 2 has '
                                  '12 inputs and inverter 3 has 13; placed as inverter 3 input 13 - confirm on site'))
            continue
        k[inv] = k.get(inv, 0) + 1
        out.append(string_rec('NL1', SN[('NL1', inv)], inv, f's{k[inv]}', [pt(L[n])], 'order'))
    notes = [['The 305 kWp expansion (inverters 5 and 6, strings 50-73, drawing rev. V3.0 30.09.2026) is not in the '
              'monitoring yet: it is added when its inverters report.',
              'La ampliación de 305 kWp (inversores 5 y 6, cadenas 50-73, plano rev. V3.0 30.09.2026) aún no está '
              'en el monitoreo: se agrega cuando sus inversores reporten.']]
    return labs, out, notes


def slp1(page):
    labs = [lab for lab in labels(page, 640, 980) if lab['size'] == 5.3 and 1 <= lab['n'] <= 18]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 19)), sorted(L)
    note = ('drawing: string 3 on MPPT 2 and strings 8, 9 on MPPT 3; monitored: inputs 3, 4 on MPPT 2 and '
            'input 5 on MPPT 3 - confirm on site')
    maps = {'INV1': {1: 's1', 2: 's2', 3: 's3', 8: 's4', 9: 's5', 13: 's9', 15: 's10', 17: 's11', 7: 's13'},
            'INV2': {4: 's1', 5: 's2', 6: 's3', 11: 's5', 12: 's6', 14: 's9', 16: 's10', 18: 's11', 10: 's13'}}
    out = []
    for inv, m in maps.items():
        for n, ch in m.items():
            bad = inv == 'INV1' and n in (3, 8, 9)
            out.append(string_rec('SLP1', SN[('SLP1', inv)], inv, ch, [pt(L[n])],
                                  'assumed' if bad else 'order', note if bad else None))
    return labs, out, []


def slp2(page):
    labs = [lab for lab in labels(page, 600, 1030) if lab['size'] == 4.5 and 1 <= lab['n'] <= 14]
    red = by_n([lab for lab in labs if lab['col'] == RED])
    blue = by_n([lab for lab in labs if lab['col'] == BLUE])
    assert sorted(red) == list(range(1, 14)) and sorted(blue) == list(range(1, 15)), (sorted(red), sorted(blue))
    out = []
    for inv, L, chans in (('INV1', red, list(range(1, 12)) + [13, 15]), ('INV2', blue, list(range(1, 14)) + [15])):
        for n in sorted(L):
            out.append(string_rec('SLP2', SN[('SLP2', inv)], inv, f's{chans[n - 1]}', [pt(L[n])], 'order'))
    return labs, out, []


# SAG: the field form 'Mapeo de cadenas fotovoltaicas' (ARGIA, 27 Jul 2026): position N -> (inverter, PV input)
SAG_MAPEO = {
    1: (1, 4), 2: (1, 1), 3: (3, 5), 4: (1, 2), 5: (1, 6), 6: (1, 3), 7: (1, 8), 8: (1, 7), 9: (1, 9),
    10: (1, 13), 11: (1, 10), 12: (1, 14), 13: (1, 11), 14: (1, 15), 15: (1, 12), 16: (1, 19), 17: (1, 21),
    18: (1, 20), 19: (2, 3), 20: (2, 6), 21: (2, 2), 22: (2, 5), 23: (2, 1), 24: (2, 4), 25: (2, 14),
    26: (2, 10), 27: (2, 13), 28: (2, 9), 29: (2, 7), 30: (2, 8), 31: (2, 21), 32: (2, 20), 33: (2, 19),
    34: (2, 12), 35: (2, 15), 36: (2, 11), 37: (3, 6), 38: (3, 2), 39: (3, 5), 40: (3, 1), 41: (3, 4),
    42: (3, 14), 43: (3, 10), 44: (3, 13), 45: (3, 9), 46: (3, 7), 47: (3, 8), 48: (3, 20), 49: (3, 19),
    50: (3, 12), 51: (3, 15), 52: (3, 11), 53: (1, 3), 54: (3, 21)}


def mex1(page):
    labs = [lab for lab in labels(page, 680, 1180) if lab['size'] in (6.5, 6.6) and 1 <= lab['n'] <= 54
            and lab['col'] in (RED, BLUE, MAGENTA)]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 55)), sorted(L)
    m = dict(SAG_MAPEO)
    # the form lists inverter 1 PV3 twice (N6, N53) and inverter 3 PV5 twice (N3, N39) and lacks
    # inverter 1 PV5 and inverter 3 PV3: the second of each pair takes the missing input, all four flagged
    m[53] = (1, 5)
    m[3] = (3, 3)
    flagged = {3, 6, 39, 53}
    note = ('the field form lists inverter 1 PV3 twice (positions 6 and 53) and inverter 3 PV5 twice (3 and 39) '
            'and lacks inverter 1 PV5 and inverter 3 PV3; positions 53 and 3 shown on the missing inputs - confirm on site')
    # v318: the first 5-minute string record (7 Oct 2026) shows inverter 2 PV4 at 0 A while its spare PV16
    # carries a string (the PV4 / PV16 exception of 24 Aug), and inverter 3 PV21 (string 21, arc fault in
    # June) at 0 A while its spare PV18 carries one: those two positions are shown on the live inputs
    moved = {24: (2, 16, 4, 'field form: inverter 2 PV4; since the PV4 / PV16 exception of 24 Aug 2026 PV4 reads '
                             '0 A and the spare PV16 carries a string - shown on PV16, confirm on site'),
             54: (3, 18, 21, 'field form: inverter 3 PV21 (string 21, arc fault June 2026); PV21 reads 0 A and the '
                             'spare PV18 carries a string - shown on PV18, confirm on site')}
    for n, (i, new_pv, old_pv, _note) in moved.items():
        sn = SN[('MEX1', f'INV{i}')]
        X[('MEX1', sn, f's{new_pv}')] = dict(X[('MEX1', sn, f's{old_pv}')], **{
            'Telemetry ID': f'PV{new_pv}', 'Standard name': f'SAG_INV{i}_PV{new_pv}', 'Note': ''})
        USED.add(('MEX1', sn, f's{old_pv}'))         # the old input carries no string any more
        m[n] = (i, new_pv)
    flagged |= set(moved)
    drawing_inv = {n: {RED: 1, BLUE: 2, MAGENTA: 3}[lab['col']] for n, lab in L.items()}
    out = []
    for n in range(1, 55):
        i, pv = m[n]
        inv = f'INV{i}'
        extra = None
        if drawing_inv[n] != i:
            extra = f'the drawing colours position {n} as inverter {drawing_inv[n]}; the field form says inverter {i}'
        notes = [x for x in (moved[n][3] if n in moved else (note if n in flagged else None), extra) if x]
        out.append(string_rec('MEX1', SN[('MEX1', inv)], inv, f's{pv}', [pt(L[n])],
                              'assumed' if n in flagged else 'table', '; '.join(notes) or None))
    return labs, out, []


def grey_box(page):
    xs = [o for o in page.lines if col(o.get('stroking_color')) == (0.46, 0.46, 0.46)]
    return min(o['x0'] for o in xs), min(o['top'] for o in xs), max(o['x1'] for o in xs), max(o['bottom'] for o in xs)


def gto1(page):
    labs = [lab for lab in labels(page, 560, 1010) if lab['size'] == 3.9 and lab['col'] == RED and 1 <= lab['n'] <= 20]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 21)), sorted(L)
    out = [string_rec('GTO1', SN[('GTO1', 'INV5')], 'INV5', f's{n}', [pt(L[n])], 'table') for n in range(1, 15)]
    note6 = ('drawing table: 3 MPPT x 2 strings (strings 15-20); monitoring: 2 combined inputs '
             '(4 strings + 2 strings) - confirm which strings share each input')
    out.append(string_rec('GTO1', SN[('GTO1', 'INV6')], 'INV6', 'm1', [pt(L[n]) for n in (15, 16, 17, 18)],
                          'assumed', note6, per=4))
    out.append(string_rec('GTO1', SN[('GTO1', 'INV6')], 'INV6', 'm2', [pt(L[n]) for n in (19, 20)],
                          'assumed', note6, per=2))
    notes = [['Inverters 1-4: the drawings mark each inverter\'s area but number no string, so their strings are '
              'only in the grid below. Drawing V6.0 gives inverter 1 14 strings and inverter 2 15; the monitoring '
              'sees 15 on inverter 1 and 14 on inverter 2 (input 15 of inverter 2 reads about 0 A) - confirm on site.',
              'Inversores 1-4: los planos marcan el área de cada inversor pero no numeran las cadenas, por eso sus '
              'cadenas están solo en la cuadrícula de abajo. El plano V6.0 da 14 cadenas al inversor 1 y 15 al 2; '
              'el monitoreo ve 15 en el inversor 1 y 14 en el 2 (la entrada 15 del inversor 2 lee cerca de 0 A) - '
              'confirmar en sitio.']]
    return labs, out, notes


def gto1_v6_shapes(page7):
    """Inverters 1-4 module outlines from rev. V6.0, moved into the V7.0 frame."""
    p6 = page_of('GTO1_drawing_v6.pdf')
    g6, g7 = grey_box(p6), grey_box(page7)
    dx, dy = g7[0] - g6[0], g7[1] - g6[1]
    assert abs((g6[2] - g6[0]) - (g7[2] - g7[0])) < 3 and abs((g6[3] - g6[1]) - (g7[3] - g7[1])) < 3, (g6, g7)
    cols = {MAGENTA: 'INV1', (0.07, 0.61, 0.28): 'INV2', RED: 'INV3', BLUE: 'INV4'}
    return [(x0 + dx, y0 + dy, x1 + dx, y1 + dy, inv) for x0, y0, x1, y1, inv
            in module_shapes(p6, list(g6), cols, maxsz=80) if (x1 - x0) >= 2 and (y1 - y0) >= 2]


def clusters(shapes, gap=2.0):
    """Module outlines that touch (within gap pt) form one block. -> list of index lists"""
    parent = list(range(len(shapes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    order = sorted(range(len(shapes)), key=lambda i: shapes[i][0])
    for a_pos, i in enumerate(order):
        xi0, yi0, xi1, yi1 = shapes[i][:4]
        for j in order[a_pos + 1:]:
            if shapes[j][0] > xi1 + gap:
                break
            xj0, yj0, xj1, yj1 = shapes[j][:4]
            if yj0 <= yi1 + gap and yi0 <= yj1 + gap:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(shapes)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def sag_blocks_by_label(shapes, labs, strings):
    """SAG: every block (3 x 6 modules) is one string and its number sits at the block's
    lower right corner; the block takes the inverter of that string (the field form)."""
    inv_of = {p['cad']: st['inv'] for st in strings for p in st['pos']}
    out = []
    for idx in clusters(shapes, gap=0.3):
        x0 = min(shapes[i][0] for i in idx)
        x1 = max(shapes[i][2] for i in idx)
        y0 = min(shapes[i][1] for i in idx)
        y1 = max(shapes[i][3] for i in idx)
        if len(idx) <= 24:
            lab = min(labs, key=lambda q: (q['x'] - x1) ** 2 + (q['y'] - y1) ** 2)
            out += [shapes[i][:4] + (inv_of[lab['n']],) for i in idx]
            continue
        # two blocks touch (SAG 23 and 53): each module goes to the first label at or below it
        near = [q for q in labs if x0 - 2 <= q['x'] <= x1 + 12 and y0 - 2 <= q['y'] <= y1 + 12]
        for i in idx:
            cy = (shapes[i][1] + shapes[i][3]) / 2
            below = [q for q in near if q['y'] >= cy - 3] or near
            lab = min(below, key=lambda q: q['y'] - cy)
            out.append(shapes[i][:4] + (inv_of[lab['n']],))
    return out


BUILD = {'MEX2': mex2, 'NL1': nl1, 'SLP1': slp1, 'SLP2': slp2, 'GTO1': gto1, 'MEX1': mex1}


# ----------------------------------------------------------- Helioscope
def helio_image(pdf):
    """The report's 'Detailed Layout' image (the largest image of the last page) and the counts."""
    n = int(re.search(r'Pages:\s+(\d+)', subprocess.run(['pdfinfo', pdf], capture_output=True, text=True).stdout).group(1))
    tmp = os.path.join(OUT, '_helio_tmp')
    os.makedirs(tmp, exist_ok=True)
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    subprocess.run(['pdfimages', '-f', str(n), '-l', str(n), '-png', pdf, os.path.join(tmp, 'i')], check=True)
    best = max((os.path.join(tmp, f) for f in os.listdir(tmp)), key=lambda p: Image.open(p).size[0] * Image.open(p).size[1])
    img = cv2.imread(best)
    text = re.sub(r'\s+', ' ', subprocess.run(['pdftotext', pdf, '-'], capture_output=True, text=True).stdout)
    inv_counts = [int(n) for n in re.findall(r'(?:MAX|MAC|SUN\d{4})[^©]{0,45}?(?<![\d,.])(\d+) \([\d.,]+ kW\)', text)]
    strings = re.search(r'Strings 10 AWG \(Copper\) (\d+)', text)
    return img, {'inverters': sum(inv_counts) if inv_counts else None, 'strings': int(strings.group(1)) if strings else None}


def helio_mask(img):
    b, g, r = [img[:, :, i].astype(int) for i in range(3)]
    return ((b > 110) & (b - r > 50) & (b - g > 40)).astype(np.uint8) * 255


def _blur(m, s):
    return cv2.GaussianBlur(m.astype(np.float32) / 255.0, (0, 0), s)


def ecc_reg(md, mh, W=420, rots=(0, -8, 8, -16, 16, -24, 24), scales=(1.0, 0.85, 1.15)):
    """drawing mask -> Helioscope mask, one affine. -> (F 2x3 full-res, IoU)"""
    sd, sh = W / md.shape[1], W / mh.shape[1]
    a = cv2.resize(md, (W, int(md.shape[0] * sd)), interpolation=cv2.INTER_AREA)
    b = cv2.resize(mh, (W, int(mh.shape[0] * sh)), interpolation=cv2.INTER_AREA)
    Ma, Mb = cv2.moments(a, True), cv2.moments(b, True)
    ca = np.array([Ma['m10'] / Ma['m00'], Ma['m01'] / Ma['m00']])
    cb = np.array([Mb['m10'] / Mb['m00'], Mb['m01'] / Mb['m00']])
    s0 = np.sqrt(Mb['m00'] / Ma['m00'])
    best = None
    for rot in rots:
        for sc in scales:
            th = np.radians(rot)
            c, s = np.cos(th) * s0 * sc, np.sin(th) * s0 * sc
            A = np.array([[c, -s, 0], [s, c, 0]], np.float32)
            A[:, 2] = cb - A[:, :2] @ ca
            ok = True
            for sig in (6, 3):
                try:
                    _cc, A = cv2.findTransformECC(_blur(a, sig), _blur(b, sig), A.copy(), cv2.MOTION_AFFINE,
                                                  (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5), None, 1)
                except cv2.error:
                    ok = False
                    break
            if not ok:
                continue
            w = cv2.warpAffine(a, A, (b.shape[1], b.shape[0]))
            iou = ((w > 127) & (b > 127)).sum() / max(1, ((w > 127) | (b > 127)).sum())
            if best is None or iou > best[0]:
                best = (iou, A)
    if best is None:
        return None, 0.0
    iou, A = best
    F = np.diag([1 / sh, 1 / sh, 1.0]) @ np.vstack([A, [0, 0, 1]]) @ np.diag([sd, sd, 1.0])
    return F[:2], float(iou)


def _blocks(mask, dil, min_area):
    big = cv2.dilate(mask, np.ones((dil, dil), np.uint8)) if dil else mask
    n, lab, st, cen = cv2.connectedComponentsWithStats(big, 8)
    keep = [i for i in range(1, n) if st[i, 4] >= min_area]
    return lab, {i: (float(cen[i][0]), float(cen[i][1]), int(((lab == i) & (mask > 0)).sum())) for i in keep}


def block_match(md, mh, F, dil_d=9, dil_h=5):
    """Pair each drawing module block with a Helioscope block (Hungarian on the
    distance after F), refit one similarity on the pairs, then give every paired
    block its own shift onto its partner. -> (lab, F2, shifts, share of blocks paired)"""
    from scipy.optimize import linear_sum_assignment
    scale = np.sqrt(abs(np.linalg.det(F[:, :2])))
    lab, D = _blocks(md, dil_d, 150)
    _lh, H = _blocks(mh, dil_h, int(150 * scale * scale))
    if not D or not H:
        return lab, F, {}, 0.0
    di, hi = list(D), list(H)
    F2 = F.copy()
    pairs = []
    for _round in range(3):
        P = np.array([[F2[0, 0] * D[i][0] + F2[0, 1] * D[i][1] + F2[0, 2], F2[1, 0] * D[i][0] + F2[1, 1] * D[i][1] + F2[1, 2]]
                      for i in di])
        Q = np.array([[H[j][0], H[j][1]] for j in hi])
        cost = np.linalg.norm(P[:, None, :] - Q[None, :, :], axis=2)
        r, c = linear_sum_assignment(cost)
        pairs = []
        for a, b in zip(r, c):
            area_d, area_h = D[di[a]][2] * scale * scale, H[hi[b]][2]
            size = np.sqrt(max(area_d, area_h))
            if cost[a, b] < 0.9 * size and 0.4 < area_h / max(area_d, 1) < 2.5:
                pairs.append((di[a], hi[b]))
        if len(pairs) >= 3:
            src = np.float32([D[i][:2] for i, _ in pairs])
            dst = np.float32([H[j][:2] for _, j in pairs])
            M, _inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=40)
            if M is not None:
                F2 = M
    shifts = {}
    for i, j in pairs:
        px = F2[0, 0] * D[i][0] + F2[0, 1] * D[i][1] + F2[0, 2]
        py = F2[1, 0] * D[i][0] + F2[1, 1] * D[i][1] + F2[1, 2]
        shifts[i] = (H[j][0] - px, H[j][1] - py)
    return lab, F2, shifts, len(pairs) / len(D)


def block_at(lab, x, y, maxd=60):
    H, W = lab.shape
    xi, yi = int(round(x)), int(round(y))
    if 0 <= xi < W and 0 <= yi < H and lab[yi, xi] > 0:
        return int(lab[yi, xi])
    y0, y1, x0, x1 = max(0, yi - maxd), min(H, yi + maxd), max(0, xi - maxd), min(W, xi + maxd)
    sub = lab[y0:y1, x0:x1]
    ys, xs = np.nonzero(sub)
    if len(xs) == 0:
        return None
    k = int(np.argmin((xs + x0 - x) ** 2 + (ys + y0 - y) ** 2))
    return int(sub[ys[k], xs[k]])


def helio_view(pk, shapes, box, px_w, strings, areas):
    cfg = PLANTS[pk]
    img, counts = helio_image(os.path.join(SRC, cfg['helio']))
    s = px_w / (box[2] - box[0])
    to_px = lambda x, y: ((x - box[0]) * s, (y - box[1]) * s)          # noqa: E731
    md = np.zeros((int(round((box[3] - box[1]) * s)), px_w), np.uint8)
    for x0, y0, x1, y1, _inv in shapes:
        a, b = to_px(x0, y0)
        c, d = to_px(x1, y1)
        cv2.rectangle(md, (int(a), int(b)), (int(c), int(d)), 255, -1)
    mh = helio_mask(img)
    F, iou = ecc_reg(md, mh)
    paired = 0.0
    if F is not None:
        best = None
        for dil in (9, 5, 3):                 # blocks set close together need a finer split
            r = block_match(md, mh, F, dil_d=dil, dil_h=max(3, dil // 2))
            if best is None or r[3] > best[3]:
                best = r
        lab, F, shifts, paired = best
    placed = F is not None and iou >= HELIO_MIN_IOU and paired >= HELIO_MIN_PAIRED
    pts, polys = [], {}
    if placed:

        def move(x, y):
            px, py = to_px(x, y)
            b = block_at(lab, px, py)
            dx, dy = shifts.get(b, (0.0, 0.0)) if b else (0.0, 0.0)
            return (F[0, 0] * px + F[0, 1] * py + F[0, 2] + dx, F[1, 0] * px + F[1, 1] * py + F[1, 2] + dy)
        for st in strings:
            extra = []
            for p in st['pos']:
                if p['d'] == 'drawing':
                    hx, hy = move(p['x'], p['y'])
                    extra.append({'d': 'helio', 'x': round(hx, 1), 'y': round(hy, 1), 'cad': p['cad']})
            st['pos'] += extra
            pts += [(q['x'], q['y']) for q in extra]
        for a in areas:
            ps = []
            for x, y, w, h in a['rects']:
                cx, cy = x + w / 2, y + h / 2
                hx, hy = move(cx, cy)
                corners = []
                for qx, qy in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
                    ax, ay = to_px(qx, qy)
                    cpx, cpy = to_px(cx, cy)
                    corners += [round(hx + F[0, 0] * (ax - cpx) + F[0, 1] * (ay - cpy), 1),
                                round(hy + F[1, 0] * (ax - cpx) + F[1, 1] * (ay - cpy), 1)]
                ps.append(corners)
            polys[a['sn']] = ps
    ys, xs = np.nonzero(mh)
    allx = list(xs) + [p[0] for p in pts]
    ally = list(ys) + [p[1] for p in pts]
    m = 70
    crop = [int(max(0, min(allx) - m)), int(max(0, min(ally) - m)),
            int(min(img.shape[1], max(allx) + m)), int(min(img.shape[0], max(ally) + m))]
    im = Image.fromarray(cv2.cvtColor(img[crop[1]:crop[3], crop[0]:crop[2]], cv2.COLOR_BGR2RGB))
    if im.width > MAXW:
        im = im.resize((MAXW, round(im.height * MAXW / im.width)), Image.LANCZOS)
    name = f'{pk}_helio.jpg'
    im.save(os.path.join(OUT, name), 'JPEG', quality=82, optimize=True, progressive=True)
    drawing = {'id': 'helio', 'kind': 'helioscope', 'image': name, 'src': cfg['helio_src'],
               'title_en': 'Helioscope design', 'title_es': 'Diseño Helioscope', 'box': crop, 'px': [im.width, im.height],
               'fit': round(iou, 3), 'paired': round(paired, 3), 'markers': placed, 'counts': counts}
    helio_areas = [{'sn': sn, 'inv': next(a['inv'] for a in areas if a['sn'] == sn), 'd': 'helio', 'polys': p}
                   for sn, p in polys.items()]
    return drawing, helio_areas


# ------------------------------------------------------------------- main
def build(pk):
    cfg = PLANTS[pk]
    page = page_of(cfg['drawing'])
    labs, strings, notes = BUILD[pk](page)
    aerial = aerial_box(page)
    shapes = module_shapes(page, aerial, cfg['modules'], avoid=labs)
    if pk == 'GTO1':
        shapes += gto1_v6_shapes(page)
    if pk == 'MEX1':
        shapes = sag_blocks_by_label(shapes, labs, strings)
    box = crop_of(shapes, labs, aerial)
    pw, ph = render(os.path.join(SRC, cfg['drawing']), box, os.path.join(OUT, f'{pk}_drawing.jpg'))
    strings += unplaced(pk)
    areas = []
    for inv in sorted({s[4] for s in shapes}):
        rects = [[round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)]
                 for x0, y0, x1, y1, i in shapes if i == inv]
        areas.append({'sn': SN[(pk, inv)], 'inv': inv, 'd': 'drawing', 'rects': rects})
    drawings = [{'id': 'drawing', 'kind': 'drawing', 'image': f'{pk}_drawing.jpg', 'src': cfg['src'],
                 'title_en': 'Installation drawing', 'title_es': 'Plano de instalación', 'box': box, 'px': [pw, ph]}]
    hd, hareas = helio_view(pk, shapes, box, pw, strings, areas)
    drawings.append(hd)
    if not hd['markers']:
        notes.append(['Helioscope design: its module layout does not line up with the as-built drawing '
                      f'(overlap {hd["fit"]:.0%}, blocks paired {hd["paired"]:.0%}), so the strings are only marked on the installation drawing.',
                      'Diseño Helioscope: su distribución de módulos no coincide con el plano de obra '
                      f'(traslape {hd["fit"]:.0%}, bloques emparejados {hd["paired"]:.0%}), por eso las cadenas solo se marcan en el plano de instalación.'])
    areas += hareas
    inverters = [{'sn': r['Serial number'].strip(), 'inv': r['Inverter'], 'model': r['Model'],
                  'inputs': int(float(r['Monitored inputs'])) if fnum(r['Monitored inputs']) else None,
                  'kwp': fnum(r['DC capacity (kWp)']), 'planes': r['Planes (tilt / azimuth)'],
                  'on_drawing': sum(len(s['pos']) and 1 for s in strings if s['sn'] == r['Serial number'].strip()
                                    and any(p['d'] == 'drawing' for p in s['pos']))}
                 for r in INVS[pk]]
    checks = {'inverters': {'workbook': len(INVS[pk]),
                            'drawing': len({a['inv'] for a in areas if a['d'] == 'drawing'}),
                            'helioscope': hd['counts']['inverters']},
              'strings': {'workbook': sum(int(st.get('per') or 1) for st in strings),
                          'drawing_labels': len(labs), 'helioscope': hd['counts']['strings']}}
    doc = {'plant': pk, 'built': dt.date.today().isoformat(), 'version': 2,
           'source': 'ARGIA_PPA_String_Configuration.xlsx (engine v0.3, 2026-10-05) + drawings + Helioscope',
           'inverters': inverters, 'checks': checks, 'drawings': drawings, 'strings': strings,
           'areas': areas, 'notes': notes}
    with open(os.path.join(OUT, f'{pk}.json'), 'w', encoding='utf-8') as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(',', ':'))
    placed = sum(1 for s in strings if any(p['d'] == 'drawing' for p in s['pos']))
    print(f'{pk}: {len(strings)} channels, {placed} on the drawing, areas {[(a["inv"], len(a.get("rects") or a.get("polys"))) for a in areas if a["d"] == "drawing"]}, '
          f'helioscope fit {hd["fit"]:.2f} paired {hd["paired"]:.2f} markers={hd["markers"]} counts={hd["counts"]} checks={checks}')


if __name__ == '__main__':
    for pk in (sys.argv[5].split(',') if len(sys.argv) > 5 else PLANTS):
        build(pk)
