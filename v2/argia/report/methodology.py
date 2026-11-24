"""Reporting methodology corrections (v392) - pure helpers for the plant report.

Arturo Gonzalez, "Monitoring portal: reporting methodology corrections" v0.1 (23 Nov 2026),
reviewed against the code on 24 Nov 2026. Tomasz's decisions the same day: keep our rule that
a connectivity gap is not an outage, turn the worked examples into regression tests, PPA plants
first, and only compare (not change) the self-calibrated expected line. What is here:

* R1 - per-inverter availability comes from the SAME evidence rule as the plant value
  (argia/kpi/availability.py, v310/v311): a judged daylight step is UP when the inverter
  produced, DOWN when it read 0 W (or stayed silent after a stop) while its peers reported,
  UNKNOWN when nothing is known (left out, shown as coverage). The plant value is the
  capacity-weighted result of the inverter values over the same steps. Before v392 the
  inverter column counted "status = online" rows, so an inverter online at 0 W for a day and a
  half (Vitalmex inverters 1 and 3, 7-8 Nov) showed 100 %.
* R2 (v393) - the inverter's Coverage is its telemetry coverage: judged steps with a real
  reading (a vendor's repeated last-known value is not a reading), never rounded up.
* R4 - specific yield per inverter divides by its DC kWp from the string register (Annex A)
  when every inverter of the plant has one; otherwise by rated AC kW for all of them, and the
  column says which.
* R7 - the month at the data edge is compared with its contract prorated to the days with
  data, marked partial, and left out of the multi-month total until it closes.

No I/O; report_gen.py feeds them.
"""
from __future__ import annotations

import calendar
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

UNIT_DC = "kWh/kWp"
UNIT_AC = "kWh/kW AC"


# ---------------------------------------------------------------- R4: DC basis
def dc_kwp_by_serial(cfgs: Mapping[str, object]) -> Dict[Tuple[str, str], float]:
    """{(plant_key, serial): DC kWp} from the EPI string register (argia.epi.config.load()).
    Strings whose inverter has no serial on the layout are skipped."""
    out: Dict[Tuple[str, str], float] = {}
    for key, c in cfgs.items():
        sn_of = getattr(c, "sn_of", {}) or {}
        for s in getattr(c, "strings", ()) or ():
            sn = sn_of.get(s.inverter)
            if sn and s.kwp and s.kwp > 0:
                out[(key, sn)] = round(out.get((key, sn), 0.0) + float(s.kwp), 3)
    return out


def yield_basis(plant: str, serials: Iterable[str], dc: Mapping[Tuple[str, str], float],
                ac: Mapping[str, float]) -> Tuple[Dict[str, Optional[float]], str]:
    """({serial: denominator}, unit). DC kWp for every inverter when the register has them all,
    else rated AC kW for all (one plant, one basis - a mix would bias the index)."""
    sns = list(serials)
    if sns and all((dc.get((plant, sn)) or 0) > 0 for sn in sns):
        return {sn: float(dc[(plant, sn)]) for sn in sns}, UNIT_DC
    return {sn: (float(ac[sn]) if (ac.get(sn) or 0) > 0 else None) for sn in sns}, UNIT_AC


# ------------------------------------------------------ R1: inverter availability
def inverter_steps(res, serials: Sequence[str]) -> Dict[str, Tuple[int, int, int, int]]:
    """{serial: (up, down, unknown, fresh)} judged steps of one plant-day from an
    argia.kpi.availability.AvailabilityDay - the evidence behind its plant value. ``fresh``
    (v393, R2) = steps with a real reading from the inverter (a vendor's repeated last value
    does not count)."""
    out = {}
    fresh = getattr(res, "fresh_steps", {}) or {}
    for sn in serials:
        down = int(res.down_steps.get(sn, 0))
        unknown = int(res.unknown_steps.get(sn, 0))
        out[sn] = (max(0, int(res.steps) - down - unknown), down, unknown, int(fresh.get(sn, 0)))
    return out


def availability_window(rows: Iterable[tuple], rated: Mapping[str, float]
                        ) -> Tuple[Dict[str, Tuple[Optional[float], Optional[float]]], Tuple[Optional[float], Optional[float]]]:
    """Per-inverter and plant availability over a window.

    ``rows``: (serial, up, down, unknown[, fresh]) per inverter-day. Returns
    ({serial: (availability, coverage)}, (plant availability, plant coverage)), where
    availability = up / (up + down) and coverage = fresh / all judged steps (v393, R2: the
    telemetry coverage of the device - steps with a real reading; rows without ``fresh``
    fall back to the known share (up + down) / all). The plant values weight each inverter
    by its rated kW, exactly as the daily plant value does. None = nothing known (never 0 %
    and never 100 %)."""
    acc: Dict[str, list] = {}
    for r in rows:
        sn, up, down, unknown = r[0], r[1], r[2], r[3]
        fresh = r[4] if len(r) > 4 and r[4] is not None else int(up or 0) + int(down or 0)
        a = acc.setdefault(sn, [0, 0, 0, 0])
        a[0] += int(up or 0)
        a[1] += int(down or 0)
        a[2] += int(unknown or 0)
        a[3] += int(fresh or 0)
    per: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    w_up = w_known = w_all = w_fresh = 0.0
    for sn, (up, down, unknown, fresh) in acc.items():
        known, total = up + down, up + down + unknown
        per[sn] = (up / known if known else None, min(1.0, fresh / total) if total else None)
        w = float(rated.get(sn) or 0) or 1.0
        w_up += w * up
        w_known += w * known
        w_all += w * total
        w_fresh += w * fresh
    plant = (w_up / w_known if w_known else None, min(1.0, w_fresh / w_all) if w_all else None)
    return per, plant


# ------------------------------------------------------ R7: contract, same period
def month_vs_contract(ym: str, actual_kwh: float, contract_kwh: float, asof: str
                      ) -> Tuple[float, Optional[float], bool]:
    """(contract basis kWh, % of it, partial?) for one month of the report table. The month of
    the data edge ``asof`` (YYYY-MM-DD) is partial unless ``asof`` is its last day: its contract
    is prorated to the days 1..asof, the same rule as the header tile."""
    y, m = int(ym[:4]), int(ym[5:7])
    days = calendar.monthrange(y, m)[1]
    partial = asof[:7] == ym and int(asof[8:10]) < days
    basis = contract_kwh * int(asof[8:10]) / days if partial else contract_kwh
    return basis, (actual_kwh / basis * 100.0 if basis else None), partial


# ------------------------------------------------- R4: index since the last change
def index_since(daily: Mapping[str, Mapping[str, float]], denom: Mapping[str, Optional[float]],
                sn: str, start: str) -> Optional[float]:
    """v394 (R4): the inverter's index against the plant median over the days from ``start``
    (its last known change, e.g. strings repaired) - the peers over the SAME days, so a
    repaired fault stops penalizing it. ``daily``: {serial: {date: kWh}}; ``denom``: kWp per
    serial (DC when known). None when it or the peers have no energy in the window."""
    def sy(s):
        d = denom.get(s)
        e = sum(v for day, v in (daily.get(s) or {}).items() if day >= start)
        return e / d if d and e > 0 else None
    mine = sy(sn)
    peers = sorted(y for y in (sy(s) for s in daily) if y is not None)
    if mine is None or not peers:
        return None
    n = len(peers)
    med = peers[n // 2] if n % 2 else (peers[n // 2 - 1] + peers[n // 2]) / 2
    return mine / med if med else None
