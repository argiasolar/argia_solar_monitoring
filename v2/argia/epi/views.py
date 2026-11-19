"""v382: the troubleshooting views of spec v0.7 section 8 (Plant / Inverters / Strings).

Pure: monitoring_gen hands in one plant-day of 5-minute buckets (the same inputs as
argia.epi.engine) and gets back one JSON-able payload that the page draws. Every
rule is a named constant here and is printed in the card's legend:

* specific power, W per kWp: a string's DC (current x voltage of its MPPT / input,
  see engine.Channel) over its Annex A kWp; an inverter's sum over its strings'
  kWp. When an inverter sent no string sample in a bucket, its AC power / 0.985 is
  used instead (the point is marked "from AC").
* design curve of an inverter: the EPI model's expected DC of its strings for the
  measured station irradiance and panel temperature (engine.expected_inverter_w,
  inverter limit included) over the same kWp. No curve where the station sample
  fails the quality checks (engine.station_qc / station_valid).
* gap against design (spec 8.2), judged against the other inverters so that what
  hits the whole plant (cloud over the station, low-sun shading, a sensor error)
  is not flagged on every inverter every afternoon: an inverter's actual/design
  ratio below GAP_SHARE x the median ratio of the plant's inverters, while its
  design is at least GAP_MIN_WKWP, for GAP_MIN_STEPS buckets or more in a row.
  (October 2026: judged against the design alone, every Taigene inverter was
  "flagged" 15:30-17:30 every day; against its peers only real dips remain.)
* strings (spec 8.3): each string's index = its W per kWp / its own design W per
  kWp (so strings on different planes of one inverter compare fairly; Taigene
  INV1-3 have SE and NW strings). Per bucket: the median index of the inverter and
  a band of BAND_LO..BAND_HI x that median (the platform's existing "90 % of its
  neighbours" rule, and its mirror above). The chart draws the raw W per kWp with
  the median and band of the raw values (identical to the index band when the
  inverter has one plane; a frozen station never bends the drawing). A
  string outside the band for OUT_MIN_STEPS buckets with G_poa >= OUT_MIN_GPOA is
  drawn in colour, labelled with its median deviation while outside, and its worst
  deviation with the time. (October 2026 with 400 W/m2: every afternoon's row
  shading between 16:30 and 17:00 flagged half the strings; at 600 the strings
  the month report found low remain.)
* no output (spec 8.5): a string whose mean index is below ZERO_SHARE of the
  inverter's median index over the samples with G_poa >= ZERO_GPOA. Strings with
  no output or no data are then left out of the median and band used for the
  outliers (one dead string of four would move that median by about 15 %).
* EPI-600 / Availability-150 of the day: engine.run_plant over the same buckets;
  month to date: apply_mtd() adds the tallies of the earlier days of the month
  (argia.epi.live keeps them per closed day).
"""
from __future__ import annotations

import datetime as dt
import statistics
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from argia.epi import engine as E
from argia.epi import model as M
from argia.epi import sky

GAP_SHARE = 0.85
GAP_MIN_WKWP = 150.0
GAP_MIN_STEPS = 2
BAND_LO, BAND_HI = 0.90, 1.10
OUT_MIN_STEPS = 6
OUT_MIN_GPOA = 600.0           # the EPI-600 domain: low-sun row shading is not a string fault
ZERO_SHARE = 0.05
ZERO_GPOA = 400.0


def _r(v: Optional[float]) -> Optional[int]:
    return None if v is None else int(round(v))


def _runs(flags: Sequence[bool], min_len: int) -> List[Tuple[int, int]]:
    out, start = [], None
    for i, fl in enumerate(list(flags) + [False]):
        if fl and start is None:
            start = i
        elif not fl and start is not None:
            if i - start >= min_len:
                out.append((start, i - 1))
            start = None
    return out


def day_buckets(day: dt.date, tz: dt.tzinfo, lat: float, lon: float) -> List[int]:
    """The 5-minute buckets of a local day with the sun up (mid-bucket)."""
    d0 = int(dt.datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()) // 300
    return [b for b in range(d0, d0 + 288)
            if sky.sun_position(dt.datetime.fromtimestamp(b * 300 + 150, dt.timezone.utc), lat, lon).zenith < 90]


def build(pm: M.PlantModel, strings: Sequence[M.StringDef], channel_of: Mapping[str, E.Channel],
          sn_of: Mapping[str, str], station: Mapping[int, E.Station], samples: Mapping[Tuple[str, int], E.InvSample],
          ac_power_w: Mapping[Tuple[str, int], float], day: dt.date, tz: dt.tzinfo,
          labels: Optional[Mapping[str, str]] = None, inv_labels: Optional[Mapping[str, str]] = None,
          mtd: Optional[dict] = None, workorder: Optional[Mapping[str, str]] = None) -> dict:
    """One plant-day payload. ``labels``: uid -> work-order label (layout name, MPPT, input);
    ``inv_labels``: inverter -> name on the portal; ``mtd``: tallies of the earlier days of the month."""
    labels = labels or {}
    buckets = day_buckets(day, tz, pm.lat, pm.lon)
    times = [dt.datetime.fromtimestamp(b * 300, dt.timezone.utc).astimezone(tz).strftime('%H:%M') for b in buckets]
    qc = E.station_qc(station, pm.lat, pm.lon)
    by_inv: Dict[str, List[M.StringDef]] = {}
    for s in strings:
        if s.cod <= day:
            by_inv.setdefault(s.inverter, []).append(s)
    ghi, g_poa_by_b, design_by, sdesign = [], {}, {}, {}
    for b in buckets:
        st = station.get(b)
        ok = st is not None and b not in qc and E.station_valid(st)[0]
        ghi.append(_r(st.ghi) if ok else None)
        if ok:
            sun = sky.sun_position(st.ts, pm.lat, pm.lon)
            for inv, strs in by_inv.items():
                tot, per = E.expected_inverter_w(pm, strs, st, sun=sun)
                design_by[(inv, b)] = tot
                for s, g, e_s in per:
                    g_poa_by_b[(s.uid, b)] = g
                    sdesign[(s.uid, b)] = e_s / s.kwp
    plant_ac, plant_exp = [], []
    for b in buckets:
        acs = [ac_power_w.get((sn, b)) for sn in sn_of.values()]
        plant_ac.append(None if all(a is None for a in acs) else round(sum(a for a in acs if a) / 1000.0, 1))
        exps = [design_by.get((inv, b)) for inv in by_inv]
        plant_exp.append(None if any(e is None for e in exps) or not exps
                         else round(sum(exps) * M.INVERTER_EFFICIENCY / 1000.0, 1))
    res = E.run_plant(pm, list(strings), channel_of, sn_of, {b: station[b] for b in buckets if b in station},
                      samples, tz, ac_power_w=ac_power_w)
    day_t = tallies(res)
    inverters, string_views, inv_series = [], {}, {}
    for inv in sorted(by_inv):
        strs = by_inv[inv]
        sn = sn_of.get(inv)
        kwp = sum(s.kwp for s in strs)
        w_inv, w_exp, from_ac = [], [], []
        w_str = {s.uid: [] for s in strs}
        for b in buckets:
            smp = samples.get((sn, b))
            if smp is not None:
                ps = {s.uid: channel_of[s.uid].power_w(smp.str_a, smp.mppt_a, smp.mppt_v) for s in strs}
                known = [p for p in ps.values() if p is not None]
                w_inv.append(_r(sum(known) / kwp) if known else None)
                from_ac.append(False)
                for s in strs:
                    w_str[s.uid].append(_r(ps[s.uid] / s.kwp) if ps[s.uid] is not None else None)
            else:
                pac = ac_power_w.get((sn, b))
                w_inv.append(_r(pac / M.INVERTER_EFFICIENCY / kwp) if pac is not None else None)
                from_ac.append(pac is not None)
                for s in strs:
                    w_str[s.uid].append(None)
            e = design_by.get((inv, b))
            w_exp.append(_r(e / kwp) if e is not None else None)
        inv_series[inv] = (w_inv, w_exp)
        sv = _strings_view(strs, w_str, times, buckets, g_poa_by_b, sdesign, w_exp, labels)
        inverters.append({
            'inv': inv, 'name': (inv_labels or {}).get(inv, inv), 'sn': sn, 'kwp': round(kwp, 2),
            'w': w_inv, 'design': w_exp, 'from_ac': [i for i, x in enumerate(from_ac) if x], 'gaps': [],
            'n_strings': len(strs), 'producing': len(strs) - len(sv['zero']) - len(sv['nodata']),
            'epi600_day': _ratio(day_t['epi600'].get(inv)), 'avail_day': _avail(day_t['avail'].get(inv))})
        sv['wo'] = [(workorder or {}).get(s.uid, '') for s in strs]
        string_views[inv] = sv
    _peer_gaps(inverters, times)
    payload = {'day': day.isoformat(), 't': times, 'ghi': ghi,
            'plant': {'ac_kw': plant_ac, 'design_kw': plant_exp, 'epi600_day': _ratio(day_t['epi600'].get('plant'))},
            'inverters': inverters, 'strings': string_views, 'tallies': day_t,
            'rules': {'gap_share': GAP_SHARE, 'gap_min_wkwp': GAP_MIN_WKWP, 'gap_min_min': GAP_MIN_STEPS * 5,
                      'band': [BAND_LO, BAND_HI], 'out_min_min': OUT_MIN_STEPS * 5, 'out_min_gpoa': OUT_MIN_GPOA,
                      'zero_share': ZERO_SHARE, 'zero_gpoa': ZERO_GPOA}}
    return apply_mtd(payload, mtd)


def apply_mtd(payload: dict, mtd: Optional[dict], mtd_days: Optional[int] = None) -> dict:
    """Set the month-to-date fields of a day payload: its own tallies plus ``mtd`` (the tallies
    of the earlier days of the month). Kept apart from build() so that the payload of a closed
    day can be cached and still show the month-to-date of the moment it is drawn."""
    total = merge_tallies(mtd or {}, payload['tallies'])
    band = lambda t: M.band(_ratio(t), total['steps600']) if t else 'insufficient'
    p6 = total['epi600'].get('plant')
    payload['plant'].update({'epi600_mtd': _ratio(p6), 'steps600_mtd': total['steps600'], 'band_mtd': band(p6),
                             'mtd_days': mtd_days})
    for i in payload['inverters']:
        t6 = total['epi600'].get(i['inv'])
        i.update({'epi600_mtd': _ratio(t6), 'steps600_mtd': t6[2] if t6 else 0, 'band_mtd': band(t6),
                  'avail_mtd': _avail(total['avail'].get(i['inv']))})
        sv = payload['strings'][i['inv']]
        sv['epi600_mtd'] = {u: _ratio(total['epi600'].get(u)) for u in sv['uids']}
    return payload


def _peer_gaps(inverters: List[dict], times: Sequence[str]) -> None:
    """Fill each inverter's 'gaps': its actual/design ratio against the plant's median ratio."""
    n = len(times)
    ratios = {i['inv']: [(i['w'][k] / i['design'][k]) if (i['w'][k] is not None and i['design'][k])
                         else None for k in range(n)] for i in inverters}
    peer = []
    for k in range(n):
        vals = [r[k] for r in ratios.values() if r[k] is not None]
        peer.append(statistics.median(vals) if len(vals) >= 2 else None)
    for i in inverters:
        r = ratios[i['inv']]
        flags = [r[k] is not None and peer[k] and (i['design'][k] or 0) >= GAP_MIN_WKWP and r[k] < GAP_SHARE * peer[k]
                 for k in range(n)]
        for i0, i1 in _runs(flags, GAP_MIN_STEPS):
            worst = min(range(i0, i1 + 1), key=lambda k: r[k] / peer[k])
            i['gaps'].append({'from': times[i0], 'to': times[i1], 'i0': i0, 'i1': i1, 'at': times[worst],
                              'pct_peers': round(100.0 * (r[worst] / peer[worst] - 1)),
                              'pct_design': round(100.0 * (r[worst] - 1))})


def _strings_view(strs, w_str, times, buckets, g_poa_by_b, sdesign, w_inv_design, labels) -> dict:
    n = len(times)
    idx = {s.uid: [(w_str[s.uid][i] / sdesign[(s.uid, b)]) if (w_str[s.uid][i] is not None and sdesign.get((s.uid, b)))
                   else None for i, b in enumerate(buckets)] for s in strs}
    planes = {(s.tilt, s.azimuth) for s in strs}

    def medians(members):
        out = []
        for i in range(n):
            vals = [idx[s.uid][i] for s in members if idx[s.uid][i] is not None]
            out.append(statistics.median(vals) if len(vals) >= 2 else None)
        return out
    # 1) no output and no data, against the median of all strings
    med_all = medians(strs)
    zero, nodata = [], []
    for s in strs:
        ser = idx[s.uid]
        if all(v is None for v in w_str[s.uid]):
            nodata.append(s.uid)
            continue
        sunny = [i for i, b in enumerate(buckets) if (g_poa_by_b.get((s.uid, b)) or 0) >= ZERO_GPOA
                 and ser[i] is not None and med_all[i]]
        if sunny and sum(ser[i] for i in sunny) < ZERO_SHARE * sum(med_all[i] for i in sunny):
            zero.append(s.uid)
    # 2) the reference is the median of the strings that produce: a dead string must not
    #    pull the band down (one dead string of four moves the median by about 15 %)
    live = [s for s in strs if s.uid not in zero and s.uid not in nodata]
    med_i = medians(live)
    # drawing: the raw W per kWp (what was measured; a frozen station never bends it),
    # median and band of the raw values; the classification below uses the index
    med, lo, hi = [], [], []
    for i in range(n):
        vals = [w_str[s.uid][i] for s in live if w_str[s.uid][i] is not None]
        m = statistics.median(vals) if len(vals) >= 2 else None
        med.append(_r(m))
        lo.append(None if m is None else _r(m * BAND_LO))
        hi.append(None if m is None else _r(m * BAND_HI))
    out = []
    for s in live:
        ser = idx[s.uid]
        flags = [ser[i] is not None and bool(med_i[i]) and (g_poa_by_b.get((s.uid, b)) or 0) >= OUT_MIN_GPOA
                 and (ser[i] < med_i[i] * BAND_LO or ser[i] > med_i[i] * BAND_HI) for i, b in enumerate(buckets)]
        if _runs(flags, OUT_MIN_STEPS):
            dev = [ser[i] / med_i[i] - 1 for i in range(n) if flags[i]]
            i_w = max((i for i in range(n) if flags[i]), key=lambda i: abs(ser[i] / med_i[i] - 1))
            out.append({'uid': s.uid, 'label': labels.get(s.uid, s.uid), 'pct': round(100.0 * statistics.median(dev)),
                        'worst': round(100.0 * (ser[i_w] / med_i[i_w] - 1)), 'at': times[i_w], 'i': i_w,
                        'minutes': 5 * sum(flags)})
    return {'uids': [s.uid for s in strs], 'labels': [labels.get(s.uid, s.uid) for s in strs],
            'kwp': [s.kwp for s in strs], 'w': [w_str[s.uid] for s in strs], 'mixed_planes': len(planes) > 1,
            'median': med, 'lo': lo, 'hi': hi, 'out': out, 'zero': zero, 'nodata': nodata}


def _ratio(t) -> Optional[float]:
    return None if not t or t[1] <= 0 else round(t[0] / t[1], 4)


def _avail(a) -> Optional[float]:
    if not a:
        return None
    k = a[0] + a[1]
    return None if k == 0 else round(a[0] / k, 4)


def tallies(res: E.Result) -> dict:
    """The engine result as plain numbers: epi600 {key: [real Wh, expected Wh, n]},
    avail {inv: [up, down, missing]}, steps600 (distinct 5-minute steps)."""
    return {'epi600': {k: [t.real_wh, t.expected_wh, t.n] for k, t in res.epi600.items()},
            'avail': {k: [a.up, a.down, a.missing] for k, a in res.avail.items()},
            'steps600': len(res.steps600)}


def merge_tallies(a: dict, b: dict) -> dict:
    out = {'epi600': {}, 'avail': {}, 'steps600': a.get('steps600', 0) + b.get('steps600', 0)}
    for part in ('epi600', 'avail'):
        for src in (a.get(part, {}), b.get(part, {})):
            for k, v in src.items():
                cur = out[part].setdefault(k, [0] * len(v))
                out[part][k] = [x + y for x, y in zip(cur, v)]
    return out
