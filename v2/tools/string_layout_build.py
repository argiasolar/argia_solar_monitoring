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
  'table' (a table names the input; layout_tables.py reads it), 'order' (string n = input n),
  'assumed' (drawing and monitoring disagree: placed where most likely,
  confirm on site), 'none' (not on the drawing: grid only).
* Inverter areas: the drawing's own modules, by the colour the drawing
  gives each inverter.
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

import pdfplumber
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helio_layout as HL       # noqa: E402
import layout_tables as LT      # noqa: E402

SRC, OUT, STRINGS_CSV, INVERTERS_CSV = sys.argv[1:5]
os.makedirs(OUT, exist_ok=True)
R = 150                 # drawing render dpi
MAXW = HL.MAXW
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
    'GTO1': {'drawing': 'GTO1_drawing_v81.pdf',
             'src': 'Taigene - Ubicación de módulos FV, rev. V8.1 (20.11.2026, all 77 strings numbered)',
             'modules': {(1.0, 0.0, 1.0): 'INV1', (0.07, 0.61, 0.28): 'INV2', (0.58, 0.15, 0.56): 'INV3',
                         (0.8, 0.41, 0.16): 'INV4', BLUE: 'INV5', RED: 'INV6'},
             'helio': 'GTO1_helio_ppa.pdf', 'helio_src': "HelioScope 'PPA Taigene', whole plant 839.28 kWp (A. Gonzalez, 19.11.2026)",
             # the drawing's module counts replace the workbook's (its expansion counts predate the as-built);
             # inverter 6 model from Arturo's answers to the EPI-600 review (21.11.2026)
             'kwp_from_strings': True, 'models': {'INV6': 'Growatt MAX 60KTL3 LV'}, 'line_modules': True,
             'label_circles': False},
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


def line_modules(page, box, colours, avoid=()):
    """Modules drawn as loose lines (no box) of the given colours inside box (layout_tables.line_cells)."""
    hl = []
    for o in page.lines:
        inv = colours.get(col(o.get('stroking_color')))
        w = o['x1'] - o['x0']
        if not inv or o['bottom'] - o['top'] > 0.5 or not 5 <= w <= 40:
            continue
        if box[0] <= o['x0'] and o['x1'] <= box[2] and box[1] <= o['top'] <= box[3]:
            hl.append((o['x0'], o['x1'], o['top'], inv))
    return [c for c in LT.line_cells(hl)
            if not any(abs((c[0] + c[2]) / 2 - a['x']) < 5 and abs((c[1] + c[3]) / 2 - a['y']) < 5 for a in avoid)]


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


def string_rec(pk, sn, inv, ch, pts, conf, note=None, per=1, modules=None):
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
    if modules is not None:                 # the drawing's count (per string) wins over the workbook
        watt = re.search(r'(\d+) W\b', mod)
        rec['modules'] = modules * per
        rec['kwp'] = round(modules * per * int(watt.group(1)) / 1000.0, 2) if watt else rec['kwp']
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


def gto1(page):
    """Taigene rev. V8.1: every string numbered (1-77), a table Cadena -> String / MPPT per inverter."""
    labs = [lab for lab in labels(page, 560, 1026) if lab['size'] == 5.9 and 1 <= lab['n'] <= 77]
    L = by_n(labs)
    assert sorted(L) == list(range(1, 78)), sorted(L)
    text = page.within_bbox((0, 565, page.width - 190, page.height)).extract_text()
    cad = LT.cadenas(text)
    per_inv = LT.split_by_inverter(cad, LT.module_counts(text))
    assert sorted(per_inv) == [1, 2, 3, 4, 5, 6], per_inv
    out = []
    for k in range(1, 6):                       # MAX 124KTL3-X2: 8 MPPT x 2 inputs
        inv = f'INV{k}'
        for n in per_inv[k]:
            panels, st, mp = cad[n]
            ch, conf, note = f's{LT.growatt_input(mp, st)}', 'table', None
            if (inv, n) == ('INV1', 15):
                # table: String 2 - MPPT 8 = input 16, which never carries current; MPPT 8 holds one string
                # and MPPT 2 two (inputs 3 and 4 both about 8 A at noon), the workbook lists inputs 1-15
                ch, conf = 's4', 'assumed'
                note = ('drawing V8.1 table: String 2 - MPPT 8 (input 16, reads 0 A); monitoring: MPPT 2 carries '
                        '2 strings and MPPT 8 one - placed on input 4, confirm on site which string feeds MPPT 2')
            out.append(string_rec('GTO1', SN[('GTO1', inv)], inv, ch, [pt(L[n])], conf, note, modules=panels))
    six = per_inv[6]
    assert six == [72, 73, 74, 75, 76, 77] and len({cad[n][0] for n in six}) == 1, six
    note6 = ('drawing V8.1 table: 4 MPPT (strings 72 and 73 alone, 74-75 and 76-77 in pairs); monitoring: '
             '2 combined inputs (4 strings + 2 strings) - placed by location (west strip 74-77, inner roof 72-73), '
             'confirm on site which strings share each input')
    out.append(string_rec('GTO1', SN[('GTO1', 'INV6')], 'INV6', 'm1', [pt(L[n]) for n in (74, 75, 76, 77)],
                          'assumed', note6, per=4, modules=cad[74][0]))
    out.append(string_rec('GTO1', SN[('GTO1', 'INV6')], 'INV6', 'm2', [pt(L[n]) for n in (72, 73)],
                          'assumed', note6, per=2, modules=cad[72][0]))
    notes = [['Drawing rev. V8.1 numbers all 77 strings. Two places where the drawing and the monitoring '
              'disagree are marked assumed (orange dashed ring): inverter 1 string 15 (the drawing puts it on '
              'MPPT 8 input 16, which never carries current, while the monitoring sees a second string on MPPT 2) '
              'and inverter 6 (the drawing lists 4 MPPT, the inverter reports 2 inputs carrying 4 and 2 strings). '
              "Module counts per string are the drawing's.",
              'El plano rev. V8.1 numera las 77 cadenas. Dos puntos donde el plano y el monitoreo no coinciden '
              'están marcados como supuestos (anillo naranja punteado): inversor 1 cadena 15 (el plano la pone '
              'en el MPPT 8 entrada 16, que nunca lleva corriente, mientras el monitoreo ve una segunda cadena '
              'en el MPPT 2) e inversor 6 (el plano indica 4 MPPT, el inversor reporta 2 entradas con 4 y 2 '
              'cadenas). Los módulos por cadena son los del plano.']]
    return labs, out, notes


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


# ----------------------------------------------------------- Helioscope (v371: tools/helio_layout.py)
def helio_view(pk, shapes, box, px_w, strings, areas):
    cfg = PLANTS[pk]
    img, counts = HL.helio_image(os.path.join(SRC, cfg['helio']), os.path.join(OUT, '_helio_tmp'))
    return HL.place(img, counts, pk, cfg['helio_src'], OUT, shapes, box, px_w, strings, areas)


# ------------------------------------------------------------------- main
def build(pk):
    cfg = PLANTS[pk]
    page = page_of(cfg['drawing'])
    labs, strings, notes = BUILD[pk](page)
    aerial = aerial_box(page)
    circles = labs if cfg.get('label_circles', True) else ()     # V8.1 labels sit on the modules, no circle
    shapes = module_shapes(page, aerial, cfg['modules'], avoid=circles)
    if cfg.get('line_modules'):
        shapes += line_modules(page, aerial, cfg['modules'], avoid=circles)
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
        notes.append([HL.MISFIT_EN + ' '
                      f'(overlap {hd["fit"]:.0%}, blocks paired {hd["paired"]:.0%}), so the strings are only marked on the installation drawing.',
                      HL.MISFIT_ES + ' '
                      f'(traslape {hd["fit"]:.0%}, bloques emparejados {hd["paired"]:.0%}), por eso las cadenas solo se marcan en el plano de instalación.'])
    areas += hareas
    def inv_kwp(r):
        if not cfg.get('kwp_from_strings'):
            return fnum(r['DC capacity (kWp)'])
        return round(sum(s['kwp'] or 0 for s in strings if s['sn'] == r['Serial number'].strip()), 2)
    inverters = [{'sn': r['Serial number'].strip(), 'inv': r['Inverter'],
                  'model': cfg.get('models', {}).get(r['Inverter'], r['Model']),
                  'inputs': int(float(r['Monitored inputs'])) if fnum(r['Monitored inputs']) else None,
                  'kwp': inv_kwp(r), 'planes': r['Planes (tilt / azimuth)'],
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
