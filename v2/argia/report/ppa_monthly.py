"""PPA monthly performance report (v374): one report per month for the whole ARGIA PPA portfolio.

Tomasz, 12 Nov 2026: "a section called PPA monthly performance report where we will be able to see the performance
of argia ppa portfolio ... from production, weather, money, availability, etc perspective. where we can show to
stakeholder how well or how bad we are working, use prologis ideas to setup the report, make it for each month going
into the past, and make it available to download". Decided the same day: a new portal section for ARGIA staff and
invited stakeholders; BOTH yardsticks side by side (the contract curve = the promise and the money, the HelioScope
design for the measured sun = the engineering); PDF + Excel per month; a draft until an operator publishes it
(frozen numbers, day-10 deadline, as the Prologis monthly report of the MSA).

Pure: no database, no files. The loader (scripts/ppa_monthly_gen.py) passes plain rows; ``build`` returns a plain,
JSON-serialisable dict, which is exactly what a published month freezes.

The figures of a plant-month (only days with production data count; a partial month is prorated, never padded):

* actual            = sum of daily energy_kwh (gross production, the billing basis)
* contract          = contract_monthly.contract_kwh x data days / days in month (the promise to the customer)
* design (typical)  = HelioScope Grid kWh of the month x ageing x data days / days in month
* design (sun)      = what the design predicts for the sun that came: per day design.expected_kwh(measured
                      irradiance); a day without a sun reading counts its typical design day
* sun index         = measured irradiance / the design year's irradiance for the same days (1.00 = typical)
* EPI               = actual / design (sun) over the days with both readings (energy performance index)
* availability      = mean daily availability (argia/kpi/availability.py) over the days that have it

The bridge from the promise to the result, in kWh and in MXN at each plant's PPA tariff (the steps add up exactly):

    contract -> + design vs contract -> + weather -> - unavailability -> + plant performance (rest) -> actual

Unavailability is loss_daily.unavailability_kwh (from July 2026); before that, and where the month has no loss
rows, it is part of the plant-performance step. A plant without a HelioScope design yet (Taigene) has one
"not split" step from contract to actual, and says so.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import statistics
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA = 1
DEADLINE_DAY = 10                     # published within ten days of month end (the Prologis MSA rule)
DEADLINES_FROM = "2026-11"            # the report started on 12 Nov 2026: earlier months are history, never "late"
SLA_DEFAULT = 0.98
# verdict bands: (green at or above, amber at or above, else red)
PROMISE_BANDS = (0.98, 0.90)          # actual / contract
ENGINEERING_BANDS = (0.97, 0.93)      # EPI: in line within 3 % (design_model.IN_LINE)
AVAILABILITY_BANDS = (0.98, 0.95)
ABOVE_CONTRACT_NOTE = 1.20            # actual this far above the promise: check the curve covers the plant as built
# kg CO2e per kWh avoided, as report_gen (a drift-guard test keeps the two equal)
CO2_BY_YEAR = {2020: 0.494, 2021: 0.423, 2022: 0.435, 2023: 0.438, 2024: 0.444}
CO2_PLANT_OVERRIDE = {"MEX1": 0.202}
STEPS = ("contract", "design", "weather", "customer", "availability", "performance", "unsplit", "actual")
MX_OFFSET = dt.timedelta(hours=-6)    # Mexico City, no daylight saving since 2022: event times are stored in UTC


# ------------------------------------------------------------------ small helpers
def num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ym_of(d) -> str:
    return str(d)[:7]


def ym_add(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    i = y * 12 + (m - 1) + k
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def days_in(ym: str) -> int:
    return calendar.monthrange(int(ym[:4]), int(ym[5:7]))[1]


def month_range(first: str, last: str) -> List[str]:
    out, m = [], first
    while m <= last:
        out.append(m)
        m = ym_add(m, 1)
    return out


def deadline(ym: str) -> str:
    return f"{ym_add(ym, 1)}-{DEADLINE_DAY:02d}"


def deadline_applies(ym: str) -> bool:
    return ym >= DEADLINES_FROM


def co2_factor(year: int, plant_key: str) -> float:
    if plant_key.upper() in CO2_PLANT_OVERRIDE:
        return CO2_PLANT_OVERRIDE[plant_key.upper()]
    lo, hi = min(CO2_BY_YEAR), max(CO2_BY_YEAR)
    return CO2_BY_YEAR[min(max(int(year), lo), hi)]


def band(x: Optional[float], bands: Tuple[float, float]) -> Optional[str]:
    if x is None:
        return None
    return "good" if x >= bands[0] else "warn" if x >= bands[1] else "bad"


def ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return a / b if a is not None and b else None


def _sum(xs: Iterable[Optional[float]]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) if xs else None


def _r(x: Optional[float], d: int = 1) -> Optional[float]:
    return None if x is None else round(x, d)


# ------------------------------------------------------------------ the context (indexes the loader's rows)
class Context:
    """Indexed inputs. Every argument is a list of plain dicts as psql --csv gives them (strings are fine).

    plants:    plant_key, name, kwp_dc, tariff_mxn_per_kwh, sla_target, cod (optional)
    daily:     plant_key, prod_date, energy_kwh, irradiance_kwh_m2, expected_kwh, availability, avail_coverage,
               cloud_cover_pct
    contract:  plant_key, year, month, contract_kwh, tariff_mxn
    invoices:  plant_key, ref_month, amount_mxn, produced_kwh, billable_kwh, penalty_kwh, check_status
    losses:    plant_key, prod_date, lost_kwh, unavailability_kwh, overheating_kwh, underperformance_kwh,
               excused_kwh, lost_mxn
    events:    plant_key, start_ts, end_ts, category, cost_mxn, deemed_kwh, note
    tickets:   number, plant_key, category, priority, status, created_at, started_at, resolved_at, closed_at, title
    sat:       plant_key, check_date, status, drift_pct
    designs:   {plant_key: argia.kpi.design_model.Design}
    """

    def __init__(self, plants, daily, contract, invoices=(), losses=(), events=(), tickets=(), sat=(), designs=None):
        self.plants = {p["plant_key"]: dict(p) for p in plants}
        self.designs = dict(designs or {})
        self.days: Dict[str, Dict[str, Dict]] = {}
        for r in daily:
            if r["plant_key"] in self.plants:
                self.days.setdefault(r["plant_key"], {})[str(r["prod_date"])[:10]] = r
        self.contract = {(c["plant_key"], int(c["year"]), int(c["month"])): c for c in contract}
        self.invoices = {(i["plant_key"], ym_of(i["ref_month"])): i for i in invoices
                         if num(i.get("produced_kwh")) is not None and i.get("check_status") != "EXPECTED_ONLY"}
        self.losses: Dict[Tuple[str, str], Dict] = {(x["plant_key"], str(x["prod_date"])[:10]): x for x in losses}
        self.events = list(events)
        self.tickets = list(tickets)
        self.sat = list(sat)

    def first_month(self) -> Optional[str]:
        firsts = [min(d) for d in self.days.values() if d]
        return ym_of(min(firsts)) if firsts else None

    def plant_first_day(self, pk: str) -> Optional[str]:
        d = self.days.get(pk)
        return min(d) if d else None


# ------------------------------------------------------------------ one plant, one month
def plant_month(ctx: Context, pk: str, ym: str, today: Optional[str] = None) -> Optional[Dict]:
    """The figures of one plant in one month, or None when the plant has no production day in it."""
    p = ctx.plants[pk]
    y, m = int(ym[:4]), int(ym[5:7])
    dim = days_in(ym)
    rows = [r for d, r in sorted(ctx.days.get(pk, {}).items()) if d[:7] == ym and (today is None or d < today)]
    data = [r for r in rows if num(r.get("energy_kwh")) is not None]
    if not data:
        return None
    des = ctx.designs.get(pk)
    before_cod = des is not None and ym < des.cod.isoformat()[:7]
    if before_cod:
        des = None                    # the design describes the plant as connected: not before its interconnection
    share = len(data) / dim
    actual = sum(num(r["energy_kwh"]) for r in data)
    c = ctx.contract.get((pk, y, m)) or {}
    c_full = num(c.get("contract_kwh"))
    tariff = num(c.get("tariff_mxn")) or num(p.get("tariff_mxn_per_kwh"))
    contract = c_full * share if c_full is not None else None
    sun_days = [(dt.date.fromisoformat(str(r["prod_date"])[:10]), num(r["energy_kwh"]), num(r.get("irradiance_kwh_m2")))
                for r in data]
    sun_ok = [(d, e, i) for d, e, i in sun_days if i is not None and i > 0]
    irr = sum(i for _, _, i in sun_ok) if sun_ok else None
    design_typ = design_sun_full = epi = sun_index = design_sun = actual_sun = None
    if des is not None:
        design_typ = des.design_kwh_month(y, m) * share
        design_sun_full = sum(des.expected_kwh(d, i) if i is not None and i > 0 else des.design_kwh_day(d)
                              for d, _, i in sun_days)
        if sun_ok:
            design_sun = sum(des.expected_kwh(d, i) for d, _, i in sun_ok)
            actual_sun = sum(e for _, e, _ in sun_ok)
            epi = ratio(actual_sun, design_sun)
            sun_index = des.resource(y, m, irr, len(sun_ok))
    av = [num(r.get("availability")) for r in data if num(r.get("availability")) is not None]
    availability = statistics.fmean(av) if av else None
    sla = num(p.get("sla_target")) or SLA_DEFAULT
    cloud = [num(r.get("cloud_cover_pct")) for r in data if num(r.get("cloud_cover_pct")) is not None]
    # losses (loss_daily, from July 2026)
    lrows = [ctx.losses[(pk, str(r["prod_date"])[:10])] for r in data if (pk, str(r["prod_date"])[:10]) in ctx.losses]
    loss = None
    if lrows:
        loss = {k: sum(num(x.get(k)) or 0.0 for x in lrows) for k in
                ("lost_kwh", "unavailability_kwh", "overheating_kwh", "underperformance_kwh", "excused_kwh", "lost_mxn")}
        loss["days"] = len(lrows)
    unavail = loss["unavailability_kwh"] if loss and loss["days"] == len(data) else None
    ev = month_events(ctx, pk, ym)
    cust_days = {d for e in ev if e["category"] == "customer" for d in e["days"]}
    customer = (sum(num(ctx.losses[(pk, d)].get("unavailability_kwh")) or 0.0 for d in cust_days
                    if (pk, d) in ctx.losses) if unavail is not None else None)
    billable = [num(r.get("billable_kwh")) for r in data]
    compensated = sum(max(0.0, (b or 0.0) - num(r["energy_kwh"])) for b, r in zip(billable, data) if b is not None)
    paid_kwh = sum(b if b is not None else num(r["energy_kwh"]) for b, r in zip(billable, data))
    # the bridge (kWh); every step None-safe and the sum exact
    if des is not None and contract is not None:
        steps = {"design": design_typ - contract, "weather": design_sun_full - design_typ}
        rest = actual - design_sun_full
    elif contract is not None:
        steps = {}
        rest = actual - contract
    else:
        steps, rest = {}, None
    if rest is not None:
        if unavail is not None:
            steps["customer"] = -customer
            steps["availability"] = -(unavail - customer)
            rest += unavail
        steps["performance" if des is not None else "unsplit"] = rest
    inv = ctx.invoices.get((pk, ym))
    earned = paid_kwh * tariff if tariff else None
    invoiced = num(inv.get("amount_mxn")) if inv else None
    epis = episodes(rows, sla, ctx, pk)
    sat = [s for s in ctx.sat if s["plant_key"] == pk and str(s["check_date"])[:7] == ym]
    worst_sat = None
    if sat:
        worst_sat = max(sat, key=lambda s: abs(num(s.get("drift_pct")) or 0.0))
    notes = []
    if before_cod:
        notes.append(("Before the design's interconnection date: the plant was not complete, so weather and plant "
                      "performance are not split.",
                      "Antes de la fecha de interconexión del diseño: la planta no estaba completa, así que clima y "
                      "desempeño no se separan."))
    elif des is None:
        notes.append(("No HelioScope design on file yet: weather and plant performance are not split; the stored "
                      "weather model is not used as a yardstick.",
                      "Sin diseño HelioScope todavía: clima y desempeño de la planta no se separan; el modelo de "
                      "clima guardado no se usa como referencia."))
    elif des.caveat:
        notes.append((des.caveat, des.caveat))
    if contract and actual / contract >= ABOVE_CONTRACT_NOTE:
        notes.append((f"Actual is {actual / contract - 1:.0%} above the contract curve: check that the curve covers the "
                      "plant as built (expansions).",
                      f"Lo real está {actual / contract - 1:.0%} arriba de la curva de contrato: revisar que la curva "
                      "cubra la planta como está construida (ampliaciones)."))
    if len(data) < dim and (today is None or ym < today[:7]):
        notes.append((f"Production data on {len(data)} of {dim} days: the yardsticks are prorated to those days.",
                      f"Datos de producción en {len(data)} de {dim} días: las referencias se prorratean a esos días."))
    return {
        "plant_key": pk, "name": p.get("name") or pk, "kwp_dc": num(p.get("kwp_dc")), "tariff": tariff,
        "days": dim, "data_days": len(data), "sun_days": len(sun_ok),
        "actual_kwh": actual, "contract_kwh": contract, "contract_full_kwh": c_full,
        "design_typ_kwh": design_typ, "design_sun_kwh": design_sun_full, "has_design": des is not None,
        "actual_sun_days_kwh": actual_sun, "design_sun_days_kwh": design_sun,
        "design_source": des.source if des is not None else None,
        "irradiance_kwh_m2": irr, "sun_index": sun_index, "epi": epi,
        "promise": ratio(actual, contract),
        "yield_kwh_kwp": ratio(actual, num(p.get("kwp_dc"))),
        "availability": availability, "availability_days": len(av), "sla": sla,
        "cloud_cover_pct": statistics.fmean(cloud) if cloud else None,
        "loss": loss, "bridge": steps,
        "revenue_earned_mxn": earned, "revenue_invoiced_mxn": invoiced,
        "paid_kwh": paid_kwh, "compensated_kwh": compensated,
        "invoice_kwh": num(inv.get("produced_kwh")) if inv else None,
        "penalty_kwh": num(inv.get("penalty_kwh")) if inv else None,
        "budget_mxn": contract * tariff if contract is not None and tariff else None,
        "co2_t": actual * co2_factor(y, pk) / 1000.0,
        "events": ev, "episodes": epis,
        "sensor_check": ({"status": worst_sat.get("status"), "drift_pct": num(worst_sat.get("drift_pct"))}
                         if worst_sat else None),
        "verdict": {"promise": band(ratio(actual, contract), PROMISE_BANDS),
                    "engineering": band(epi, ENGINEERING_BANDS),
                    "availability": band(availability, (sla, sla - 0.03))},
        "notes": notes,
    }


def month_events(ctx: Context, pk: str, ym: str) -> List[Dict]:
    """Logged plant events that overlap the month (customer maintenance, ARGIA interventions)."""
    lo, hi = f"{ym}-01", f"{ym_add(ym, 1)}-01"
    out = []
    for e in ctx.events:
        if e["plant_key"] != pk:
            continue
        s, en = str(e.get("start_ts") or "")[:10], str(e.get("end_ts") or "")[:10]
        if s < hi and (not en or en >= lo):
            d0 = _local_date(e.get("start_ts"))
            d1 = _local_date(e.get("end_ts")) or dt.date.fromisoformat(hi) - dt.timedelta(days=1)
            days = [x for x in _dates(d0.isoformat(), max(d0, d1).isoformat()) if x[:7] == ym] if d0 else []
            out.append({"start": s, "end": en or None, "days": days, "category": e.get("category") or "",
                        "deemed_kwh": num(e.get("deemed_kwh")), "cost_mxn": num(e.get("cost_mxn")),
                        "note": (e.get("note") or "").split(" | ")[0][:200]})
    return out


def _local_date(ts) -> Optional[dt.date]:
    """The Mexico City calendar day of a stored timestamp ('2026-10-24 12:00:00+00' is UTC). Pure."""
    t = str(ts or "").strip().replace(" ", "T")
    if not t:
        return None
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    elif len(t) > 3 and t[-3] in "+-" and t[-2:].isdigit():
        t += ":00"
    try:
        x = dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    if x.tzinfo is not None:
        x = x.astimezone(dt.timezone.utc).replace(tzinfo=None) + MX_OFFSET
    return x.date()


def episodes(rows: Sequence[Mapping], sla: float, ctx: Context, pk: str) -> List[Dict]:
    """Runs of consecutive days with availability below the plant's target (the MSA's 'outages' view)."""
    out, cur = [], None
    for r in rows:
        d, a = str(r["prod_date"])[:10], num(r.get("availability"))
        if a is not None and a < sla:
            if cur and (dt.date.fromisoformat(d) - dt.date.fromisoformat(cur["end"])).days == 1:
                cur["end"], cur["days"] = d, cur["days"] + 1
                cur["min"] = min(cur["min"], a)
            else:
                cur = {"start": d, "end": d, "days": 1, "min": a}
                out.append(cur)
        else:
            cur = None
    for e in out:
        lost = [num(ctx.losses[(pk, x)].get("unavailability_kwh")) for x in _dates(e["start"], e["end"])
                if (pk, x) in ctx.losses]
        e["unavailability_kwh"] = sum(x for x in lost if x is not None) if lost else None
    return out


def _dates(a: str, b: str) -> List[str]:
    d0, d1 = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def ticket_stats(ctx: Context, ym: str, plants: Sequence[str]) -> Dict:
    lo, hi = f"{ym}-01", f"{ym_add(ym, 1)}-01"
    opened, resolved, open_end, response = [], [], [], []
    for t in ctx.tickets:
        if t["plant_key"] not in plants:
            continue
        c, r = str(t.get("created_at") or "")[:19], str(t.get("resolved_at") or t.get("closed_at") or "")[:19]
        if lo <= c[:10] < hi:
            opened.append(t)
            if t.get("started_at"):
                h = (_ts(t["started_at"]) - _ts(c)).total_seconds() / 3600.0
                response.append(max(0.0, h))
        if r and lo <= r[:10] < hi:
            resolved.append(t)
        if c[:10] < hi and (not r or r[:10] >= hi):
            open_end.append(t)
    return {"opened": len(opened), "resolved": len(resolved), "open_at_end": len(open_end),
            "median_response_h": statistics.median(response) if response else None,
            "open_list": [{"number": t["number"], "plant_key": t["plant_key"], "title": (t.get("title") or "")[:120],
                           "priority": t.get("priority") or "", "status": t.get("status") or "",
                           "created": str(t.get("created_at") or "")[:10]} for t in open_end]}


def _ts(s) -> dt.datetime:
    return dt.datetime.fromisoformat(str(s)[:19].replace(" ", "T"))


# ------------------------------------------------------------------ the portfolio
def portfolio(pms: Sequence[Dict]) -> Dict:
    """Totals over the plants of a month. Ratios are of sums (a big plant weighs more), never averages of ratios."""
    S = lambda k: _sum(p.get(k) for p in pms)                                            # noqa: E731
    with_c = [p for p in pms if p["contract_kwh"] is not None]
    dz = [p for p in pms if p["has_design"] and p["epi"] is not None]
    a_sun = sum(p["actual_sun_days_kwh"] for p in dz)
    d_sun = sum(p["design_sun_days_kwh"] for p in dz)
    sun_w = [(p["design_typ_kwh"], p["sun_index"]) for p in pms if p["sun_index"] is not None and p["design_typ_kwh"]]
    av_w = [(p["kwp_dc"] or 0.0, p["availability"]) for p in pms if p["availability"] is not None]
    bridge = {k: _sum(p["bridge"].get(k) for p in with_c) for k in STEPS[1:-1]}
    mxn = {k: _sum(p["bridge"][k] * p["tariff"] for p in with_c if p["bridge"].get(k) is not None and p["tariff"])
           for k in STEPS[1:-1]}
    loss_keys = ("lost_kwh", "unavailability_kwh", "overheating_kwh", "underperformance_kwh", "lost_mxn")
    lp = [p for p in pms if p["loss"]]
    return {
        "plants": len(pms), "kwp_dc": S("kwp_dc"),
        "actual_kwh": S("actual_kwh"),
        "contract_kwh": _sum(p["contract_kwh"] for p in with_c),
        "actual_with_contract_kwh": _sum(p["actual_kwh"] for p in with_c),
        "promise": ratio(_sum(p["actual_kwh"] for p in with_c), _sum(p["contract_kwh"] for p in with_c)),
        "epi": ratio(a_sun, d_sun) if d_sun else None, "epi_plants": len(dz),
        "sun_index": (sum(w * s for w, s in sun_w) / sum(w for w, _ in sun_w)) if sun_w else None,
        "availability": (sum(w * a for w, a in av_w) / sum(w for w, _ in av_w)) if av_w and sum(w for w, _ in av_w) else None,
        "revenue_earned_mxn": S("revenue_earned_mxn"), "revenue_invoiced_mxn": S("revenue_invoiced_mxn"),
        "invoiced_plants": sum(1 for p in pms if p["revenue_invoiced_mxn"] is not None),
        "budget_mxn": S("budget_mxn"), "co2_t": S("co2_t"),
        "bridge_kwh": bridge, "bridge_mxn": mxn,
        "loss": {k: _sum(p["loss"][k] for p in lp) for k in loss_keys} if lp else None,
        "deemed_kwh": _sum(e["deemed_kwh"] for p in pms for e in p["events"]),
        "compensated_kwh": S("compensated_kwh"), "paid_kwh": S("paid_kwh"),
        "customer_events": sum(1 for p in pms for e in p["events"] if e["category"] == "customer"),
        "episodes": sum(len(p["episodes"]) for p in pms),
    }


# ------------------------------------------------------------------ the report
def build(ctx: Context, ym: str, today: str, generated_utc: str = "") -> Dict:
    """The report of month ``ym`` as of ``today`` (YYYY-MM-DD; days from today on are not counted)."""
    plants = sorted(ctx.plants)
    pms = [x for x in (plant_month(ctx, pk, ym, today) for pk in plants) if x]
    port = portfolio(pms)
    first = ctx.first_month() or ym
    trend = []
    for m in month_range(max(first, ym_add(ym, -12)), ym):
        tp = [x for x in (plant_month(ctx, pk, m, today) for pk in plants) if x]
        if tp:
            pt = portfolio(tp)
            trend.append({"month": m, "actual_kwh": pt["actual_kwh"], "contract_kwh": pt["contract_kwh"],
                          "promise": pt["promise"], "epi": pt["epi"], "sun_index": pt["sun_index"],
                          "availability": pt["availability"], "revenue_earned_mxn": pt["revenue_earned_mxn"],
                          "lost_mxn": (pt["loss"] or {}).get("lost_mxn")})
    ytd = _period(ctx, plants, month_range(f"{ym[:4]}-01", ym), today)
    life = _period(ctx, plants, month_range(first, ym), today)
    complete = ym < today[:7]
    rep = {
        "schema": SCHEMA, "month": ym, "as_of": today, "generated_utc": generated_utc, "complete": complete,
        "deadline": deadline(ym), "plants": pms, "portfolio": port, "trend": trend, "ytd": ytd, "lifetime": life,
        "tickets": ticket_stats(ctx, ym, plants), "headline": headline(ym, port, pms, complete),
        "first_month": first,
    }
    return rep


def _period(ctx: Context, plants: Sequence[str], months: Sequence[str], today: str) -> Dict:
    pms = [x for m in months for x in (plant_month(ctx, pk, m, today) for pk in plants) if x]
    if not pms:
        return {}
    port = portfolio(pms)
    return {k: port[k] for k in ("actual_kwh", "contract_kwh", "promise", "epi", "revenue_earned_mxn",
                                 "revenue_invoiced_mxn", "budget_mxn", "co2_t", "loss", "bridge_mxn")} | {
        "from": months[0], "to": months[-1]}


def daily_rows(ctx: Context, ym: str, today: str) -> List[Dict]:
    """Every plant-day of the month with the yardsticks of that day (for the Excel workbook). Pure."""
    out = []
    for pk in sorted(ctx.plants):
        des = ctx.designs.get(pk)
        if des is not None and ym < des.cod.isoformat()[:7]:
            des = None
        y, m = int(ym[:4]), int(ym[5:7])
        c = num((ctx.contract.get((pk, y, m)) or {}).get("contract_kwh"))
        for d, r in sorted(ctx.days.get(pk, {}).items()):
            if d[:7] != ym or d >= today:
                continue
            day = dt.date.fromisoformat(d)
            e, i = num(r.get("energy_kwh")), num(r.get("irradiance_kwh_m2"))
            lo = ctx.losses.get((pk, d)) or {}
            out.append({
                "plant_key": pk, "name": ctx.plants[pk].get("name") or pk, "date": d, "energy_kwh": e,
                "billable_kwh": num(r.get("billable_kwh")), "irradiance_kwh_m2": i,
                "contract_day_kwh": c / days_in(ym) if c is not None else None,
                "design_typ_day_kwh": des.design_kwh_day(day) if des is not None else None,
                "design_sun_kwh": des.expected_kwh(day, i) if des is not None and i else None,
                "availability": num(r.get("availability")), "cloud_cover_pct": num(r.get("cloud_cover_pct")),
                "lost_kwh": num(lo.get("lost_kwh")), "unavailability_kwh": num(lo.get("unavailability_kwh")),
                "overheating_kwh": num(lo.get("overheating_kwh")),
                "underperformance_kwh": num(lo.get("underperformance_kwh")), "lost_mxn": num(lo.get("lost_mxn"))})
    return out


# ------------------------------------------------------------------ the words
def _pct(x: float) -> str:
    return f"{abs(x) * 100:.0f}%"


def headline(ym: str, port: Dict, pms: Sequence[Dict], complete: bool) -> Dict[str, str]:
    """Three plain sentences, EN and ES: the promise, the weather, the engineering. Pure."""
    mon_en = dt.date(int(ym[:4]), int(ym[5:7]), 1).strftime("%B %Y")
    mon_es = f"{MESES[int(ym[5:7]) - 1].capitalize()} {ym[:4]}"
    mtd_en, mtd_es = ("", "") if complete else (" so far", " hasta hoy")
    a, pr = port["actual_kwh"] or 0.0, port["promise"]
    en = [f"{mon_en}{mtd_en}: the PPA portfolio delivered {a / 1000:,.1f} MWh"]
    es = [f"{mon_es}{mtd_es}: el portafolio PPA entregó {a / 1000:,.1f} MWh"]
    if pr is not None:
        en[0] += f", {_pct(pr - 1)} {'above' if pr >= 1 else 'below'} the contract curve."
        es[0] += f", {_pct(pr - 1)} {'arriba' if pr >= 1 else 'abajo'} de la curva de contrato."
    else:
        en[0] += "."
        es[0] += "."
    s = port["sun_index"]
    if s is not None:
        en.append(f"The sun was {_pct(s - 1)} {'stronger' if s >= 1 else 'weaker'} than in a typical {mon_en[:-5]}.")
        es.append(f"El sol fue {_pct(s - 1)} {'más fuerte' if s >= 1 else 'más débil'} que en un {MESES[int(ym[5:7]) - 1]} típico.")
    e = port["epi"]
    if e is not None:
        n = port["epi_plants"]
        en.append(f"For the sun that came, the {n} plants with a design produced {_pct(e - 1)} "
                  f"{'more' if e >= 1 else 'less'} than their HelioScope design.")
        es.append(f"Para el sol que hubo, las {n} plantas con diseño produjeron {_pct(e - 1)} "
                  f"{'más' if e >= 1 else 'menos'} que su diseño HelioScope.")
    loss = port["loss"]
    if loss and loss.get("lost_mxn"):
        en.append(f"Recoverable losses: {loss['lost_kwh'] / 1000:,.1f} MWh, MXN {loss['lost_mxn']:,.0f}.")
        es.append(f"Pérdidas recuperables: {loss['lost_kwh'] / 1000:,.1f} MWh, MXN {loss['lost_mxn']:,.0f}.")
    return {"en": " ".join(en), "es": " ".join(es)}


MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre")


# ------------------------------------------------------------------ freezing
def digest(rep: Mapping) -> str:
    """Hash of the numbers (generated_utc and as_of left out): the same data gives the same hash."""
    body = {k: v for k, v in rep.items() if k not in ("generated_utc", "as_of", "published")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


def freeze(rep: Mapping, by: str, when_utc: str) -> Dict:
    """A published report: the numbers as they are now, who and when, on time or late. Pure."""
    if not rep.get("complete"):
        raise ValueError(f"{rep.get('month')}: the month is not over yet, only a complete month can be published")
    out = json.loads(json.dumps(rep, default=str))
    out["published"] = {"by": by, "utc": when_utc, "digest": digest(rep),
                        "on_time": when_utc[:10] <= rep["deadline"] if deadline_applies(rep["month"]) else None}
    return out
