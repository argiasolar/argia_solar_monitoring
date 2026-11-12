"""The HelioScope view of a PPA string layout (v318, moved out of string_layout_build.py in v371).

Shared by the full build (tools/string_layout_build.py, drawings + workbook) and by
tools/string_layout_helio.py, which swaps only the HelioScope design of a layout already on the
server (Tomasz, 12 Nov 2026: "have you replaced the helioscope design inside monitoring for each
plant drawing?" - the Nov 2026 as-connected designs).

Where the markers come from: the drawing's module mask is registered onto the HelioScope module
mask (blue): one affine by ECC, then each module block refined by its own shift (a HelioScope
design can differ locally from the as-built drawing). Below HELIO_MIN_IOU overall overlap or
HELIO_MIN_PAIRED paired blocks no marker is placed and the page says why.
"""
import os
import re
import subprocess

import cv2
import numpy as np
from PIL import Image

MAXW = 1800             # px
HELIO_MIN_IOU = 0.55
HELIO_MIN_PAIRED = 0.9      # share of drawing blocks paired with a Helioscope block
MISFIT_EN = 'Helioscope design: its module layout does not line up with the as-built drawing'
MISFIT_ES = 'Diseño Helioscope: su distribución de módulos no coincide con el plano de obra'


def helio_image(pdf, tmp):
    """The report's 'Detailed Layout' image (the largest image of the last page) and the counts."""
    n = int(re.search(r'Pages:\s+(\d+)', subprocess.run(['pdfinfo', pdf], capture_output=True, text=True).stdout).group(1))
    os.makedirs(tmp, exist_ok=True)
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    subprocess.run(['pdfimages', '-f', str(n), '-l', str(n), '-png', pdf, os.path.join(tmp, 'i')], check=True)
    best = max((os.path.join(tmp, f) for f in os.listdir(tmp)), key=lambda p: Image.open(p).size[0] * Image.open(p).size[1])
    img = cv2.imread(best)
    text = re.sub(r'\s+', ' ', subprocess.run(['pdftotext', pdf, '-'], capture_output=True, text=True).stdout)
    inv_counts = [int(n) for n in re.findall(r'(?:MAX|MAC|SUN\d{4})[^©]{0,45}?(?<![\d,.])(\d+) \([\d.,]+ kW\)', text)]
    strings = (re.search(r'Strings 10 AWG \(Copper\) (\d+)', text)
               or re.search(r'10 AWG \(Copper\) (\d+) \([\d,.]+ m\)', text))     # v371: column order varies
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


def place(img, counts, pk, src, out, shapes, box, px_w, strings, areas):
    """Register the drawing onto the HelioScope image, add 'helio' positions to ``strings`` (in
    place), write <out>/<pk>_helio.jpg. -> (drawing entry, helio areas)"""
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
    im.save(os.path.join(out, name), 'JPEG', quality=82, optimize=True, progressive=True)
    drawing = {'id': 'helio', 'kind': 'helioscope', 'image': name, 'src': src,
               'title_en': 'Helioscope design', 'title_es': 'Diseño Helioscope', 'box': crop, 'px': [im.width, im.height],
               'fit': round(iou, 3), 'paired': round(paired, 3), 'markers': placed, 'counts': counts}
    helio_areas = [{'sn': sn, 'inv': next(a['inv'] for a in areas if a['sn'] == sn), 'd': 'helio', 'polys': p}
                   for sn, p in polys.items()]
    return drawing, helio_areas
