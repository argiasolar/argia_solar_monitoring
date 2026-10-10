"""System Availability as the Prologis MSA defines it, the exclusion
register, the annual analysis, liquidated damages and the bonus (v323).

MSA Schedule A, Availability Guarantee:

    System Availability = 1 - 1 / (H_ttp x kW_np) x SUM over incidents (H_un x kW_un)

* H_ttp: hours of the period with irradiance above 150 W/m2;
* kW_np: the DC nameplate of the system (sum of the module ratings);
* H_un: hours above 150 W/m2 during which a component was unavailable
  because of an equipment fault; kW_un: the DC nameplate behind it;
* calculated on an annual basis (here also monthly, to track it - proposal
  4.7), reported in the Annual Report, due by the end of the calendar
  quarter following each anniversary of the effective date.

Excluded (deemed available): Owner delays, site access delays, manufacturer
delays, utility outages, weather-related underperformance, Force Majeure
(s.20), and ARGIA's redline (a) awaiting Owner approval of a corrective
quote, (b) manufacturer warranty / RMA turnaround, (c) spare parts in
transit ordered without undue delay, (d) DAS / communication outages not
attributable to ARGIA, (e) curtailment or utility instructions. Every
exclusion ARGIA claims is visible to Prologis and stays "claimed" until a
Prologis manager accepts or rejects it; the availability is shown both with
the accepted exclusions only and with every claimed one.

Liquidated damages: Annual Loss = kWh Rate x (Measured Energy / (1 - D) -
Measured Energy), D = guarantee - A when A is below the guarantee; capped
at 20 % of the annual fees (ARGIA's redline). Bonus (ARGIA's redline): 50 %
of the same formula with D = A - 0.98 when A exceeds 98 %, capped at 10 % of
the site's annual fees. The guarantee is one setting (Tomasz, 9 Oct: 98 %);
the MSA text is inconsistent (98.5 % in the clause, 0.98 in the formula,
97 % in ARGIA's redline).

Computation per 5-minute slot: the unavailable DC at a slot is the sum of
the incidents active and not excluded at that slot, capped at kW_np, so
overlapping records can never count a kW twice beyond the plant.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from argia.prologis import metering as M

SCHEMA = """
CREATE TABLE IF NOT EXISTS unavail_incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_code TEXT NOT NULL, equipment_id INTEGER, component TEXT NOT NULL,
  dc_kw REAL NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL DEFAULT '', cause TEXT NOT NULL DEFAULT '',
  ticket_id INTEGER, created_by TEXT NOT NULL, created_utc TEXT NOT NULL, sample INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS exclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_code TEXT NOT NULL DEFAULT '', incident_id INTEGER, category TEXT NOT NULL,
  start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'claimed',
  claimed_by TEXT NOT NULL, claimed_utc TEXT NOT NULL, decided_by TEXT NOT NULL DEFAULT '', decided_utc TEXT NOT NULL DEFAULT '',
  decision_note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS contract_terms (
  site_code TEXT PRIMARY KEY, effective_date TEXT NOT NULL, kwh_rate REAL, annual_fee REAL,
  updated_by TEXT NOT NULL DEFAULT '', updated_utc TEXT NOT NULL DEFAULT '');
"""
CATEGORIES = [("owner", "Owner delay", "Retraso del Propietario"),
              ("access", "Site / tenant access delay", "Retraso de acceso al sitio / inquilino"),
              ("manufacturer", "Manufacturer delay", "Retraso del fabricante"),
              ("utility", "Utility outage", "Corte de la red / CFE"),
              ("weather", "Weather-related underperformance", "Bajo desempeño por clima"),
              ("force_majeure", "Force Majeure (s.20)", "Fuerza mayor (s.20)"),
              ("a_quote", "(a) Awaiting Owner approval of a corrective quote", "(a) Esperando aprobación del Propietario a una cotización"),
              ("b_rma", "(b) Warranty / RMA turnaround", "(b) Garantía / RMA del fabricante"),
              ("c_transit", "(c) Spare parts in transit", "(c) Refacciones en tránsito"),
              ("d_das", "(d) DAS / communication outage not attributable to ARGIA", "(d) Falla DAS / comunicación no atribuible a ARGIA"),
              ("e_curtail", "(e) Curtailment or utility instruction", "(e) Limitación o instrucción de la red")]
CAT_KEYS = [k for k, _, _ in CATEGORIES]
IRR_MIN = 150.0
SLOT_H = M.STEP_MIN / 60.0
BONUS_FROM = 0.98
BONUS_SHARE, BONUS_CAP = 0.5, 0.10
LD_CAP = 0.20

IrrFn = Callable[[object, dt.date], Sequence[Tuple[str, float]]]   # (site, day) -> [(HH:MM, W/m2)]


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    c.commit()


def _audit(c, user: str, ip: str, action: str, target: str = "", detail: str = "") -> None:
    from argia.prologis import store as S
    S.audit(c, user, ip, action, target, detail)


def to_utc(local: dt.datetime) -> str:
    return (local - M.MX_OFFSET).strftime("%Y-%m-%d %H:%M:%S")


def to_local(ts_utc: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts_utc) + M.MX_OFFSET


def sample_irradiance(site, day: dt.date) -> Sequence[Tuple[str, float]]:
    """GHI every 5 minutes from the SAMPLE metering (until the site sensor)."""
    return [(h, ghi) for h, _kw, ghi in M._profile(site.code, site.lat, site.lon, site.kwp, day)]


# ------------------------------------------------------------------ the formula (pure)
def sunny_slots(site, start: dt.datetime, end: dt.datetime, irr: IrrFn = sample_irradiance) -> List[dt.datetime]:
    """Local 5-minute slot starts in [start, end) with irradiance > 150 W/m2."""
    out: List[dt.datetime] = []
    d = start.date()
    while d <= (end - dt.timedelta(seconds=1)).date():
        for hhmm, ghi in irr(site, d):
            if ghi > IRR_MIN:
                t = dt.datetime.combine(d, dt.time(int(hhmm[:2]), int(hhmm[3:])))
                if start <= t < end:
                    out.append(t)
        d += dt.timedelta(days=1)
    return out


@dataclass
class Interval:
    start: dt.datetime            # local
    end: Optional[dt.datetime]    # local, None = still open

    def covers(self, t: dt.datetime) -> bool:
        return self.start <= t and (self.end is None or t < self.end)


@dataclass
class Inc:
    id: int
    dc_kw: float
    span: Interval


@dataclass
class Excl:
    id: int
    incident_id: Optional[int]
    span: Interval


@dataclass
class Result:
    h_ttp: float
    kw_np: float
    gross_kwh_eq: float               # SUM H_un x kW_un before exclusions
    excluded_kwh_eq: float
    net_kwh_eq: float
    per_incident: Dict[int, Tuple[float, float]] = field(default_factory=dict)   # id -> (H_un gross, H_un excluded)

    @property
    def availability(self) -> Optional[float]:
        if self.h_ttp <= 0 or self.kw_np <= 0:
            return None
        return max(0.0, 1.0 - self.net_kwh_eq / (self.h_ttp * self.kw_np))


def compute(kw_np: float, slots: Sequence[dt.datetime], incidents: Sequence[Inc], exclusions: Sequence[Excl]) -> Result:
    """The MSA formula on 5-minute slots (pure). An exclusion with an
    incident_id covers that incident only; without, every incident of the
    site (portfolio-wide ones are passed in for every site)."""
    gross = net = 0.0
    per: Dict[int, List[float]] = {i.id: [0.0, 0.0] for i in incidents}
    for t in slots:
        g_kw = n_kw = 0.0
        for i in incidents:
            if not i.span.covers(t):
                continue
            g_kw += i.dc_kw
            per[i.id][0] += SLOT_H
            if any(e.span.covers(t) and (e.incident_id is None or e.incident_id == i.id) for e in exclusions):
                per[i.id][1] += SLOT_H
            else:
                n_kw += i.dc_kw
        gross += min(g_kw, kw_np) * SLOT_H
        net += min(n_kw, kw_np) * SLOT_H
    return Result(len(slots) * SLOT_H, kw_np, round(gross, 3), round(gross - net, 3), round(net, 3),
                  {k: (round(v[0], 3), round(v[1], 3)) for k, v in per.items()})


def annual_loss(a: float, threshold: float, measured_kwh: float, kwh_rate: float) -> float:
    """kWh Rate x (Measured Energy / (1 - D) - Measured Energy), D = threshold - A (0 when met)."""
    d = threshold - a
    if d <= 0 or measured_kwh <= 0 or kwh_rate <= 0:
        return 0.0
    return kwh_rate * (measured_kwh / (1 - d) - measured_kwh)


def liquidated_damages(a: float, guarantee: float, measured_kwh: float, kwh_rate: float,
                       annual_fee: Optional[float]) -> Tuple[float, float, bool]:
    """(amount, uncapped, capped?) - capped at 20 % of the annual fees."""
    raw = annual_loss(a, guarantee, measured_kwh, kwh_rate)
    if annual_fee is None:
        return raw, raw, False
    cap = LD_CAP * annual_fee
    return min(raw, cap), raw, raw > cap


def bonus(a: float, measured_kwh: float, kwh_rate: float, annual_fee: Optional[float]) -> Tuple[float, float, bool]:
    """50 % of the loss formula with D = A - 0.98 when A > 98 %; capped at
    10 % of the site's annual fees."""
    d = a - BONUS_FROM
    if d <= 0 or measured_kwh <= 0 or kwh_rate <= 0:
        return 0.0, 0.0, False
    raw = BONUS_SHARE * kwh_rate * (measured_kwh / (1 - d) - measured_kwh)
    if annual_fee is None:
        return raw, raw, False
    cap = BONUS_CAP * annual_fee
    return min(raw, cap), raw, raw > cap


def contract_years(effective: dt.date, today: dt.date) -> List[Tuple[dt.date, dt.date]]:
    """[(start, end_exclusive)] of every contract year begun by ``today``."""
    out = []
    k = 0
    while True:
        try:
            a = effective.replace(year=effective.year + k)
            b = effective.replace(year=effective.year + k + 1)
        except ValueError:                       # 29 Feb
            a = dt.date(effective.year + k, 3, 1)
            b = dt.date(effective.year + k + 1, 3, 1)
        if a > today:
            return out
        out.append((a, b))
        k += 1


def analysis_due(anniversary: dt.date) -> dt.date:
    """End of the calendar quarter following the one of the anniversary."""
    q = (anniversary.month - 1) // 3 + 1
    y, nq = (anniversary.year + 1, 1) if q == 4 else (anniversary.year, q + 1)
    end_month = 3 * nq
    nxt = dt.date(y + 1, 1, 1) if end_month == 12 else dt.date(y, end_month + 1, 1)
    return nxt - dt.timedelta(days=1)


# ------------------------------------------------------------------ store
def _date_time(s: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat((s or "").strip().replace("T", " "))
    except ValueError:
        raise ValueError("times as YYYY-MM-DD HH:MM (Mexico City)")


def add_incident(c, site_code: str, component: str, dc_kw: Optional[float], start_local: str, end_local: str, cause: str,
                 by: str, equipment_id: Optional[int] = None, ticket_id: Optional[int] = None, ip: str = "",
                 site_codes: Iterable[str] = (), site_kwp: float = 0.0, sample: bool = False) -> int:
    from argia.prologis import assets as A
    site_code = (site_code or "").upper()
    if site_code not in set(site_codes):
        raise ValueError("unknown site")
    if equipment_id is not None:
        eq = A.equipment_row(c, equipment_id)
        if eq is None or eq["site_code"] != site_code:
            raise ValueError("the equipment is not at that site")
        if dc_kw is None:
            dc_kw = eq["dc_kw"]
        component = component or " ".join(x for x in (eq["tag"], eq["make"], eq["model"], eq["serial"]) if x)
    if dc_kw is None or not 0 < dc_kw <= max(site_kwp, 0.001) * 1.05:
        raise ValueError("DC kW behind the component must be more than 0 and not above the site kWp")
    if not (component or "").strip():
        raise ValueError("name the component (e.g. INV-02)")
    a = _date_time(start_local)
    b = _date_time(end_local) if (end_local or "").strip() else None
    if b is not None and b <= a:
        raise ValueError("the end must be after the start")
    r = c.execute("INSERT INTO unavail_incidents (site_code, equipment_id, component, dc_kw, start_utc, end_utc, cause, ticket_id, created_by, created_utc, sample)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (site_code, equipment_id, component.strip()[:120], float(dc_kw), to_utc(a), to_utc(b) if b else "",
                                                    cause.strip()[:1000], ticket_id, by, now_utc(), 1 if sample else 0))
    c.commit()
    _audit(c, by, ip, "unavail_add", f"{site_code}/inc#{r.lastrowid}", f"{component} {dc_kw} kW {a} - {b or 'open'}")
    return int(r.lastrowid)


def close_incident(c, iid: int, end_local: str, by: str, ip: str = "") -> None:
    r = c.execute("SELECT * FROM unavail_incidents WHERE id=?", (iid,)).fetchone()
    if not r:
        raise ValueError("no such incident")
    if r["end_utc"]:
        raise ValueError("already closed")
    b = _date_time(end_local)
    if b <= to_local(r["start_utc"]):
        raise ValueError("the end must be after the start")
    c.execute("UPDATE unavail_incidents SET end_utc=? WHERE id=?", (to_utc(b), iid))
    c.commit()
    _audit(c, by, ip, "unavail_close", f"inc#{iid}", str(b))


def claim_exclusion(c, site_code: str, category: str, start_local: str, end_local: str, reason: str, by: str,
                    incident_id: Optional[int] = None, ip: str = "", site_codes: Iterable[str] = ()) -> int:
    site_code = (site_code or "").upper()
    if site_code and site_code not in set(site_codes):
        raise ValueError("unknown site")
    if category not in CAT_KEYS:
        raise ValueError("unknown category")
    if not reason.strip():
        raise ValueError("give the reason and the evidence")
    a, b = _date_time(start_local), _date_time(end_local)
    if b <= a:
        raise ValueError("the end must be after the start")
    if incident_id is not None:
        inc = c.execute("SELECT site_code FROM unavail_incidents WHERE id=?", (incident_id,)).fetchone()
        if not inc or (site_code and inc["site_code"] != site_code):
            raise ValueError("the incident is not at that site")
        site_code = inc["site_code"]
    r = c.execute("INSERT INTO exclusions (site_code, incident_id, category, start_utc, end_utc, reason, claimed_by, claimed_utc)"
                  " VALUES (?,?,?,?,?,?,?,?)", (site_code, incident_id, category, to_utc(a), to_utc(b), reason.strip()[:2000], by, now_utc()))
    c.commit()
    _audit(c, by, ip, "exclusion_claim", f"excl#{r.lastrowid}", f"{category} {site_code or 'ALL'} {a} - {b}")
    return int(r.lastrowid)


def decide_exclusion(c, eid: int, accept: bool, by: str, note: str = "", ip: str = "") -> None:
    r = c.execute("SELECT * FROM exclusions WHERE id=?", (eid,)).fetchone()
    if not r:
        raise ValueError("no such exclusion")
    if r["status"] != "claimed":
        raise ValueError("already decided")
    if not accept and not note.strip():
        raise ValueError("a rejection needs a reason")
    c.execute("UPDATE exclusions SET status=?, decided_by=?, decided_utc=?, decision_note=? WHERE id=?",
              ("accepted" if accept else "rejected", by, now_utc(), note.strip()[:1000], eid))
    c.commit()
    _audit(c, by, ip, "exclusion_decide", f"excl#{eid}", ("accepted " if accept else "rejected ") + note[:200])


def save_terms(c, site_code: str, effective: str, kwh_rate: Optional[float], annual_fee: Optional[float], by: str,
               ip: str = "", site_codes: Iterable[str] = ()) -> None:
    if site_code not in set(site_codes):
        raise ValueError("unknown site")
    dt.date.fromisoformat(effective)
    for v, name in ((kwh_rate, "kWh rate"), (annual_fee, "annual fee")):
        if v is not None and not 0 <= v <= 1e9:
            raise ValueError(f"{name} out of range")
    old = c.execute("SELECT * FROM contract_terms WHERE site_code=?", (site_code,)).fetchone()
    c.execute("INSERT OR REPLACE INTO contract_terms (site_code, effective_date, kwh_rate, annual_fee, updated_by, updated_utc) VALUES (?,?,?,?,?,?)",
              (site_code, effective, kwh_rate, annual_fee, by, now_utc()))
    c.commit()
    _audit(c, by, ip, "contract_terms", site_code,
           f"{dict(old) if old else None} -> effective={effective} rate={kwh_rate} fee={annual_fee}"[:1000])


def terms(c) -> Dict[str, sqlite3.Row]:
    return {r["site_code"]: r for r in c.execute("SELECT * FROM contract_terms")}


def _iv(r) -> Interval:
    return Interval(to_local(r["start_utc"]), to_local(r["end_utc"]) if r["end_utc"] else None)


def site_result(c, site, start: dt.datetime, end: dt.datetime, statuses: Sequence[str] = ("accepted",),
                irr: IrrFn = sample_irradiance, kw_np: Optional[float] = None) -> Result:
    """The formula for one site over [start, end) local, counting the
    exclusions whose status is in ``statuses``."""
    s_u, e_u = to_utc(start), to_utc(end)
    incs = [Inc(r["id"], r["dc_kw"], _iv(r)) for r in c.execute(
        "SELECT * FROM unavail_incidents WHERE site_code=? AND start_utc<? AND (end_utc='' OR end_utc>?)", (site.code, e_u, s_u))]
    q = ",".join("?" * len(statuses))
    exs = [Excl(r["id"], r["incident_id"], _iv(r)) for r in c.execute(
        f"SELECT * FROM exclusions WHERE (site_code=? OR site_code='') AND status IN ({q}) AND start_utc<? AND end_utc>?",
        (site.code, *statuses, e_u, s_u))] if statuses else []
    slots = sunny_slots(site, start, end, irr)
    return compute(kw_np if kw_np is not None else site.kwp, slots, incs, exs)


def portfolio(results: Sequence[Result]) -> Optional[float]:
    num = sum(r.net_kwh_eq for r in results)
    den = sum(r.h_ttp * r.kw_np for r in results)
    return None if den <= 0 else max(0.0, 1 - num / den)


def suggestions(c) -> List[Dict]:
    """Exclusion periods the data already proves, for incidents that have
    no exclusion of that category yet: (a) the wait for Prologis's
    approval on the incident's ticket, (b) the supplier's time on a
    warranty claim for the incident's ticket or equipment."""
    out = []
    for inc in c.execute("SELECT * FROM unavail_incidents"):
        have = {r["category"] for r in c.execute("SELECT category FROM exclusions WHERE incident_id=?", (inc["id"],))}
        if inc["ticket_id"] and "a_quote" not in have:
            t = c.execute("SELECT * FROM tickets WHERE id=?", (inc["ticket_id"],)).fetchone()
            if t and t["approval"] in ("approved", "rejected", "pending"):
                ev = c.execute("SELECT ts_utc FROM ticket_events WHERE ticket_id=? AND kind='approval' ORDER BY id LIMIT 1", (t["id"],)).fetchone()
                end = ev["ts_utc"] if ev else ""
                start = max(t["detected_utc"], inc["start_utc"])
                if end and end > start:
                    out.append({"incident": inc["id"], "site": inc["site_code"], "category": "a_quote", "start_utc": start, "end_utc": end,
                                "reason": f"{t['number']}: waiting for Prologis dispatch / quote approval"})
        if "b_rma" not in have:
            q = "SELECT * FROM warranty_claims WHERE submitted_utc<>'' AND (equipment_id=? OR (ticket_id IS NOT NULL AND ticket_id=?))"
            for cl in c.execute(q, (inc["equipment_id"] or -1, inc["ticket_id"] or -1)):
                end = cl["decided_utc"] or cl["closed_utc"]
                if end and end > cl["submitted_utc"]:
                    out.append({"incident": inc["id"], "site": inc["site_code"], "category": "b_rma", "start_utc": cl["submitted_utc"],
                                "end_utc": end, "reason": f"{cl['number']} with {cl['supplier']} (RMA {cl['supplier_ref'] or '-'})"})
    return out
