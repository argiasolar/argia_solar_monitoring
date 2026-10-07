"""v317: string layout monitoring - each string's current against its peers.

Tomasz 2026-10-07: "build the monitoring like you did for the Culligan
mock for [the PPA plants], add the detailed interactive picture / layout
under the intraday production graph".

Inputs
* a plant layout (server-only JSON, built from the ARGIA drawings and the
  PPA string configuration workbook - see docs/OPERATIONS.md): every
  monitored string with its inverter serial, channel (``s<n>`` = string
  input n, ``m<n>`` = MPPT n current shared by ``per`` strings), module,
  plane (tilt / azimuth), and where its label sits on the drawing;
* the 5-minute samples of ``string_sample`` (v317) for one MX day, or for
  days before v317 the nightly ``string_daily`` amp-hours (Growatt only).

Method (chosen for honesty)
* Peers = strings with the same module on the same plane (tilt and
  azimuth); a plane with fewer than 3 strings borrows the plant's other
  strings of the same module, then the whole plant. Current, not power:
  strings on one plane see the same light, so their currents match
  whatever the module count; the MPPT voltage is shared anyway.
* 5-minute buckets. A string's ratio = its current / the median current
  of its peers in that bucket. Below 1 A peer median (dawn, dusk, heavy
  cloud) nothing is judged: the light is too low for 0.1 A readings.
* Day index = the string's current summed over the judged buckets where
  it reported / its peers' median summed over the same buckets - a data
  gap on one inverter cannot pull its strings down.
* Classes: ok >= 90 %, low 75-90 %, very low < 75 %, no current (< 0.3 A
  while the peers carry >= 1 A), no data, low light. Display only: no
  alert, no money - a string colour is a pointer for the O&M team.
Pure functions, no I/O.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

BUCKET_MIN = 5
DIM_A = 1.0              # peer median below this -> low light, not judged
ZERO_A = 0.3             # a string under this while its peers carry DIM_A -> no current
OK_RATIO = 0.90
LOW_RATIO = 0.75
MIN_GROUP = 3
MIN_JUDGED = 6           # judged buckets a day index needs (30 min of good light)
NORMAL_DAYS = 30         # v318: a string's own normal = median day index over this many days before
NORMAL_MIN_DAYS = 7
NORMAL_CLIP = (0.5, 1.2)
CHRONIC = 0.80           # a string whose own normal is below this is never shown as ok
CONFS = ('table', 'order', 'assumed', 'none')
CH_RE = re.compile(r'^[sm]([1-9]\d?)$')

# one character per bucket keeps the page small
OK, LOW, VLOW, ZERO, NODATA, DIM = 'g', 'a', 'r', 'z', 'n', 'd'
CLASS_TEXT = {OK: ('ok', 'ok'), LOW: ('low', 'bajo'), VLOW: ('very low', 'muy bajo'),
              ZERO: ('no current', 'sin corriente'), NODATA: ('no data', 'sin datos'),
              DIM: ('low light, not judged', 'poca luz, sin evaluar')}


class LayoutError(ValueError):
    pass


# ------------------------------------------------------------------ layout
def validate(doc: dict, inverters: Optional[Iterable[str]] = None) -> List[str]:
    """Problems with a layout document ([] = usable). ``inverters``: the
    plant's configured serials; a string on an unknown serial is a problem."""
    out = []
    if not isinstance(doc, dict) or not doc.get('plant'):
        return ['not a layout document (no plant)']
    draws = {d.get('id'): d for d in doc.get('drawings') or []}
    for did, d in draws.items():
        box = d.get('box') or []
        if len(box) != 4 or not (box[2] > box[0] and box[3] > box[1]):
            out.append(f'drawing {did}: bad box {box}')
        if not d.get('image'):
            out.append(f'drawing {did}: no image')
    known = set(inverters) if inverters is not None else None
    seen = set()
    for s in doc.get('strings') or []:
        key = (s.get('sn'), s.get('ch'))
        tag = f"{s.get('sn')}/{s.get('ch')}"
        if not CH_RE.match(str(s.get('ch') or '')):
            out.append(f'{tag}: bad channel')
        if key in seen:
            out.append(f'{tag}: twice')
        seen.add(key)
        if known is not None and s.get('sn') not in known:
            out.append(f'{tag}: inverter not configured for {doc["plant"]}')
        if s.get('conf') not in CONFS:
            out.append(f'{tag}: bad confidence {s.get("conf")!r}')
        if str(s.get('ch', '')).startswith('m') and int(s.get('per') or 1) < 1:
            out.append(f'{tag}: bad per')
        for p in s.get('pos') or []:
            d = draws.get(p.get('d'))
            if d is None:
                out.append(f'{tag}: unknown drawing {p.get("d")!r}')
                continue
            x0, y0, x1, y1 = d['box']
            if not (x0 <= p.get('x', -1) <= x1 and y0 <= p.get('y', -1) <= y1):
                out.append(f'{tag}: label outside drawing {p.get("d")}')
    for a in doc.get('areas') or []:
        if a.get('d') not in draws:
            out.append(f'area {a.get("sn")}: unknown drawing')
        if known is not None and a.get('sn') not in known:
            out.append(f'area {a.get("sn")}: inverter not configured')
    if not doc.get('strings'):
        out.append('no strings')
    return out


def load(text: str, inverters: Optional[Iterable[str]] = None) -> dict:
    doc = json.loads(text)
    probs = validate(doc, inverters)
    if probs:
        raise LayoutError('; '.join(probs[:5]) + (f' (+{len(probs) - 5} more)' if len(probs) > 5 else ''))
    return doc


def peer_groups(strings: Sequence[dict]) -> List[str]:
    """Peer group of each string: its plane, widened when too small."""
    def count(key):
        c = {}
        for s in strings:
            k = key(s)
            c[k] = c.get(k, 0) + 1
        return c
    plane = count(lambda s: s.get('group'))
    module = count(lambda s: s.get('module'))
    out = []
    for s in strings:
        if plane[s.get('group')] >= MIN_GROUP:
            out.append(f"plane:{s.get('group')}")
        elif module[s.get('module')] >= MIN_GROUP:
            out.append(f"module:{s.get('module')}")
        else:
            out.append('plant')
    return out


def peer_members(strings: Sequence[dict], groups: Sequence[str]) -> Dict[str, List[int]]:
    """The strings each group's median is taken over: a plane group is its
    plane, a widened group is every string of that module (or the plant)."""
    out = {}
    for g in set(groups):
        kind, _, rest = g.partition(':')
        if kind == 'plane':
            out[g] = [k for k, s in enumerate(strings) if s.get('group') == rest]
        elif kind == 'module':
            out[g] = [k for k, s in enumerate(strings) if s.get('module') == rest]
        else:
            out[g] = list(range(len(strings)))
    return out


# ----------------------------------------------------------------- samples
def channel_current(str_a: Sequence, mppt_a: Sequence, ch: str, per: int = 1) -> Optional[float]:
    n = int(ch[1:])
    arr = str_a if ch[0] == 's' else mppt_a
    if not arr or n > len(arr) or arr[n - 1] is None:
        return None
    v = float(arr[n - 1])
    if v < 0:
        return None
    return v / per if ch[0] == 'm' else v


def bucket_of(ts_mx: dt.datetime) -> int:
    """Minutes since MX midnight, floored to the bucket."""
    m = ts_mx.hour * 60 + ts_mx.minute
    return m - m % BUCKET_MIN


def adjust(r: float, normal: Optional[float]) -> float:
    """v318: the ratio against the string's own normal. A Growatt MAX reads
    the second string of an MPPT pair 10-20 % low at every PPA site
    (string_daily, Sep-Oct 2026): judged against its neighbours alone that
    paints a third of a plant amber for weeks. Against its own normal only a
    change shows."""
    if normal is None:
        return r
    lo, hi = NORMAL_CLIP
    return r / min(max(normal, lo), hi)


def _grade(adj: float, normal: Optional[float]) -> str:
    if adj >= OK_RATIO:
        return LOW if (normal is not None and normal < CHRONIC) else OK
    return LOW if adj >= LOW_RATIO else VLOW


def classify(i: Optional[float], med: Optional[float], normal: Optional[float] = None) -> Tuple[str, Optional[float]]:
    if i is None:
        return NODATA, None
    if med is None or med < DIM_A:
        return DIM, None
    r = i / med
    if i < ZERO_A:
        return ZERO, r
    return _grade(adjust(r, normal), normal), r


def classify_index(idx: Optional[float], judged: int, normal: Optional[float] = None) -> str:
    if judged < MIN_JUDGED:          # it reported, but never in light good enough to judge
        return DIM
    if idx is None:
        return NODATA
    if idx < 0.05:
        return ZERO
    return _grade(adjust(idx, normal), normal)


def series(strings: Sequence[dict], samples: Iterable[Tuple[dt.datetime, str, Sequence, Sequence]]):
    """samples: (ts_mx, inverter_sn, str_a, mppt_a). Returns
    (buckets, cur) - sorted bucket minutes and, per string, its current in
    each bucket (None = no sample / no value). A second sample in the same
    bucket replaces the first (the latest wins)."""
    by_sn: Dict[str, Dict[int, Tuple[Sequence, Sequence]]] = {}
    for ts, sn, sa, ma in samples:
        by_sn.setdefault(sn, {})[bucket_of(ts)] = (sa or (), ma or ())
    buckets = sorted({b for d in by_sn.values() for b in d})
    cur = []
    for s in strings:
        d = by_sn.get(s['sn'], {})
        row = []
        for b in buckets:
            smp = d.get(b)
            row.append(None if smp is None else channel_current(smp[0], smp[1], s['ch'], int(s.get('per') or 1)))
        cur.append(row)
    return buckets, cur


def evaluate(strings: Sequence[dict], buckets: Sequence[int], cur: Sequence[Sequence[Optional[float]]],
             normal: Optional[Sequence[Optional[float]]] = None) -> dict:
    """Classes per string per bucket, the peer medians, the day index.
    ``normal``: each string's own normal day index (see ``normals``)."""
    normal = list(normal) if normal is not None else [None] * len(strings)
    groups = peer_groups(strings)
    gnames = sorted(set(groups))
    members = peer_members(strings, groups)
    med = {}
    for g in gnames:
        idx = members[g]
        row = []
        for j in range(len(buckets)):
            vals = [cur[k][j] for k in idx if cur[k][j] is not None]
            row.append(median(vals) if len(vals) >= 2 else None)
        med[g] = row
    cls, day = [], []
    for k, s in enumerate(strings):
        m = med[groups[k]]
        cls.append(''.join(classify(cur[k][j], m[j], normal[k])[0] for j in range(len(buckets))))
        num = den = 0.0
        judged = 0
        reported = 0
        for j in range(len(buckets)):
            if cur[k][j] is None:
                continue
            reported += 1
            if m[j] is None or m[j] < DIM_A:
                continue
            num += cur[k][j]
            den += m[j]
            judged += 1
        idx = (num / den) if den > 0 else None
        ah = sum(v for v in cur[k] if v is not None) * BUCKET_MIN / 60.0 if reported else None
        day.append({'ah': None if ah is None else round(ah, 1), 'idx': None if idx is None else round(idx, 3),
                    'judged': judged, 'cls': classify_index(idx, judged, normal[k]) if reported else NODATA})
    return {'groups': groups, 'med': med, 'cls': cls, 'day': day, 'normal': normal}


def evaluate_daily(strings: Sequence[dict], q_ah: Dict[Tuple[str, str], float],
                   normal: Optional[Sequence[Optional[float]]] = None) -> List[dict]:
    """A day's amp-hours per string (string_daily: Growatt string inputs; or
    the day sums of string_sample). Index = Ah / median Ah of the peers."""
    normal = list(normal) if normal is not None else [None] * len(strings)
    groups = peer_groups(strings)
    members = peer_members(strings, groups)
    ah = [q_ah.get((s['sn'], s['ch'])) for s in strings]
    out = []
    for k, s in enumerate(strings):
        peers = [ah[j] for j in members[groups[k]] if ah[j] is not None]
        m = median(peers) if len(peers) >= 2 else None
        if ah[k] is None:
            out.append({'ah': None, 'idx': None, 'judged': 0, 'cls': NODATA})
            continue
        idx = ah[k] / m if m else None
        enough = m is not None and m >= DIM_A * 2      # 2 Ah: less light than that is not a day to judge
        out.append({'ah': round(ah[k], 1), 'idx': None if idx is None else round(idx, 3),
                    'judged': MIN_JUDGED if enough else 0,
                    'cls': classify_index(idx, MIN_JUDGED if enough else 0, normal[k])})
    return out


def normals(strings: Sequence[dict], history: Dict[dt.date, Dict[Tuple[str, str], float]], before: dt.date,
            days: int = NORMAL_DAYS, min_days: int = NORMAL_MIN_DAYS) -> List[Optional[float]]:
    """Each string's own normal: the median of its day index (Ah against its
    peers) over the ``days`` days before ``before``, from days with enough
    light; None with fewer than ``min_days`` such days (then it is judged
    against its peers alone)."""
    per = [[] for _ in strings]
    for d, q in history.items():
        if not (before - dt.timedelta(days=days) <= d < before):
            continue
        for k, r in enumerate(evaluate_daily(strings, q)):
            if r['idx'] is not None and r['cls'] not in (DIM, NODATA):
                per[k].append(r['idx'])
    return [round(median(v), 3) if len(v) >= min_days else None for v in per]


def inverter_summary(strings: Sequence[dict], cls: Sequence[str], nb: int) -> Dict[str, dict]:
    """Per inverter and bucket: the class of its median string (for the
    drawing areas) and how many of its strings are low / very low / dead."""
    out = {}
    order = {ZERO: 0, VLOW: 1, LOW: 2, OK: 3}
    for sn in dict.fromkeys(s['sn'] for s in strings):
        ks = [k for k, s in enumerate(strings) if s['sn'] == sn]
        c, bad = [], []
        for j in range(nb):
            cs = [cls[k][j] for k in ks]
            judged = sorted((x for x in cs if x in order), key=order.get)
            if judged:
                c.append(judged[len(judged) // 2])
            elif any(x == DIM for x in cs):
                c.append(DIM)
            else:
                c.append(NODATA)
            bad.append(sum(1 for x in cs if x in (ZERO, VLOW, LOW)))
        out[sn] = {'cls': ''.join(c), 'bad': bad}
    return out


def hhmm(minutes: int) -> str:
    return f'{minutes // 60:02d}:{minutes % 60:02d}'
