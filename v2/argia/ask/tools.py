"""The functions the assistant may call. Read-only, fixed SQL, validated
inputs — the model never composes a query.

Every tool is ``fn(rows, **params) -> dict`` where ``rows(sql)`` returns
``psql -A -t`` style rows (lists of strings, '' for NULL). Production
passes ``argia.store.pgq.psql_rows``; tests pass a fake keyed on the
``/*tag:...*/`` comment each query carries, so a test never depends on
SQL text.

Inputs coming from the model are untrusted: plant names go through
``resolve_plant`` (matched against the plant table, then quoted), dates
must parse as ISO dates, ranges are capped. Anything else raises
``ToolError``, which the agent hands back to the model as the tool
result so it can rephrase or ask.

Numbers returned here are the numbers the answer must quote. The model
does not aggregate raw samples — the SQL does.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

try:                                              # documented vendor states
    from argia.alerts.fault_catalog import explain_fault, is_normal_state
except Exception:                                 # noqa: BLE001
    def explain_fault(vendor: str, raw: str) -> Optional[str]:   # type: ignore
        return raw or None

    def is_normal_state(vendor: str, raw: str) -> bool:          # type: ignore
        return (raw or "").strip() in ("", "0")

Rows = Callable[[str], List[List[str]]]

MX = ZoneInfo("America/Mexico_City")
MX_D = "(ts_utc AT TIME ZONE 'America/Mexico_City')::date"
MAX_RANGE_DAYS = 400
STALE_MIN = 30


class ToolError(ValueError):
    """Bad input from the model — returned to it as the tool result."""


# ----------------------------------------------------------------- helpers
def _f(s: Any) -> Optional[float]:
    try:
        return float(s) if s not in ("", None) else None
    except (TypeError, ValueError):
        return None


def _i(s: Any) -> Optional[int]:
    v = _f(s)
    return int(v) if v is not None else None


def _q(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _r(v: Optional[float], nd: int = 1) -> Optional[float]:
    return None if v is None else round(v, nd)


def _pct(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or not den:
        return None
    return round(100.0 * num / den, 1)


def today_mx() -> dt.date:
    return dt.datetime.now(MX).date()


def _date(s: Any, name: str) -> str:
    """ISO date string or ToolError. Accepts 'today' / 'yesterday'."""
    if s in (None, ""):
        raise ToolError(f"{name} is required (YYYY-MM-DD)")
    s = str(s).strip().lower()
    if s == "today":
        return today_mx().isoformat()
    if s == "yesterday":
        return (today_mx() - dt.timedelta(days=1)).isoformat()
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise ToolError(f"{name} must be YYYY-MM-DD, got {s!r}") from None


def _range(date_from: Any, date_to: Any) -> tuple:
    a, b = _date(date_from, "date_from"), _date(date_to, "date_to")
    if a > b:
        a, b = b, a
    span = (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
    if span > MAX_RANGE_DAYS:
        raise ToolError(f"range too long ({span} days, max {MAX_RANGE_DAYS})")
    return a, b


# ------------------------------------------------------------------ plants
def display_name(customer: Any) -> str:
    """Human name, never the plant code — the same rule the portfolio
    map follows (v177.1: 'do not use the code names like GTO1').
    'TAIGENE PPA roof (Leon, GTO)' -> 'Taigene'; short all-caps
    acronyms (SAG, SMS) survive. Pure; mirrors monitoring_gen."""
    s = str(customer or "").split("(")[0].split(",")[0]
    for cut in (" PPA", " CAPEX", " roof", " land"):
        i = s.find(cut)
        if i > 0:
            s = s[:i]
    parts = s.strip().split()
    if len(parts) == 1 and len(parts[0]) <= 3 and parts[0].isupper():
        return parts[0]
    return " ".join("-".join(p[:1].upper() + p[1:].lower()
                             for p in w.split("-")) for w in parts)


def plants(rows: Rows) -> Dict[str, dict]:
    """Configured plants keyed by plant_key (active and inactive)."""
    out: Dict[str, dict] = {}
    for r in rows("SELECT plant_key, customer, brand, kwp_dc,"
                  " coalesce(portfolio,''), active,"
                  " coalesce(tariff_mxn_per_kwh,0), coalesce(pr_baseline,0)"
                  " FROM plant ORDER BY plant_key /*tag:plants*/;"):
        if len(r) >= 8:
            out[r[0]] = {"plant_key": r[0], "name": display_name(r[1]),
                         "customer": r[1], "brand": r[2],
                         "kwp_dc": _f(r[3]), "portfolio": r[4],
                         "active": r[5] == "t",
                         "tariff_mxn_per_kwh": _f(r[6]) or None,
                         "pr_baseline": _f(r[7]) or None}
    return out


def resolve_plant(rows: Rows, name: Any) -> str:
    """Plant key for a key ('gto1'), a customer name ('Taigene') or a
    unique fragment of one. Raises ToolError with the vocabulary when
    nothing (or more than one plant) matches."""
    if name in (None, ""):
        raise ToolError("plant is required")
    ps = plants(rows)
    key = str(name).strip().upper()
    if key in ps:
        return key
    frag = str(name).strip().lower()
    hits = [k for k, p in ps.items()
            if frag in p["customer"].lower() or frag in p["name"].lower()
            or frag in k.lower()]
    if len(hits) == 1:
        return hits[0]
    vocab = ", ".join(f"{k}={p['name']}" for k, p in ps.items())
    if not hits:
        raise ToolError(f"unknown plant {name!r}. Known plants: {vocab}")
    raise ToolError(f"{name!r} is ambiguous ({', '.join(hits)}). "
                    f"Known plants: {vocab}")


# ----------------------------------------------------------- live queries
def _today_live(rows: Rows, plant: Optional[str] = None) -> Dict[str, dict]:
    """Per plant: today's energy (sum of each inverter's max etoday),
    current kW (latest sample per inverter, if fresh), minutes since the
    last usable sample, inverters reporting today."""
    where = f" AND plant_key = {_q(plant)}" if plant else ""
    out: Dict[str, dict] = {}
    for r in rows(
            "SELECT plant_key, coalesce(sum(e),0), count(*), min(age)"
            " FROM (SELECT plant_key, inverter_sn, max(etoday_kwh) AS e,"
            "   extract(epoch FROM now() - max(ts_utc))/60 AS age"
            "  FROM telemetry"
            f"  WHERE {MX_D} = (now() AT TIME ZONE 'America/Mexico_City')::date"
            "   AND (etoday_kwh IS NOT NULL OR power_w IS NOT NULL)"
            f"  {where} GROUP BY 1, 2) s GROUP BY 1 /*tag:today_energy*/;"):
        if len(r) >= 4:
            out[r[0]] = {"today_kwh": _r(_f(r[1])),
                         "inverters_reporting": _i(r[2]),
                         "last_sample_age_min": _r(_f(r[3]), 0)}
    for r in rows(
            "SELECT plant_key, sum(power_w)/1000.0 FROM ("
            " SELECT DISTINCT ON (plant_key, inverter_sn) plant_key, power_w"
            "  FROM telemetry"
            f" WHERE ts_utc > now() - interval '{STALE_MIN} minutes'"
            f"  AND power_w IS NOT NULL {where}"
            " ORDER BY plant_key, inverter_sn, ts_utc DESC) t"
            " GROUP BY 1 /*tag:now_kw*/;"):
        if len(r) >= 2:
            out.setdefault(r[0], {})["current_kw"] = _r(_f(r[1]))
    return out


def _perf(rows: Rows, date_from: str, date_to: str,
          plant: Optional[str] = None) -> Dict[str, dict]:
    """daily_production aggregated per plant over [date_from, date_to]."""
    where = f" AND plant_key = {_q(plant)}" if plant else ""
    out: Dict[str, dict] = {}
    for r in rows(
            "SELECT plant_key, sum(energy_kwh),"
            " sum(expected_kwh) FILTER (WHERE expected_kwh > 0),"
            " sum(energy_kwh) FILTER (WHERE expected_kwh > 0),"
            " avg(pr), avg(availability), count(*),"
            " count(*) FILTER (WHERE pr IS NOT NULL), sum(irradiance_kwh_m2)"
            " FROM daily_production"
            f" WHERE prod_date BETWEEN DATE {_q(date_from)} AND DATE {_q(date_to)}"
            f" {where} GROUP BY 1 ORDER BY 1 /*tag:perf*/;"):
        if len(r) >= 9:
            exp, prod_on_exp = _f(r[2]), _f(r[3])
            out[r[0]] = {
                "production_kwh": _r(_f(r[1])),
                "expected_kwh": _r(exp),
                "vs_expected_pct": _pct(prod_on_exp, exp),
                "pr": _r(_f(r[4]), 3),
                "availability_pct": _r((_f(r[5]) or 0) * 100 if _f(r[5]) is not None else None),
                "days": _i(r[6]), "days_with_pr": _i(r[7]),
                "irradiance_kwh_m2": _r(_f(r[8]))}
    return out


def _alarm_plant(key: str, ps: Dict[str, dict]) -> Dict[str, Optional[str]]:
    """Alarm keys look like 'plant-stale:GTO2' or
    'inverter-silent:GTO1:SN4'; the second segment is the plant when it
    is one. Infra alarms have none."""
    parts = (key or "").split(":")
    pk = parts[1].upper() if len(parts) > 1 and parts[1].upper() in ps else None
    return {"plant_key": pk, "name": ps[pk]["name"] if pk else None}


def _active_alarms(rows: Rows, plant: Optional[str] = None) -> List[dict]:
    where = f" AND key LIKE {_q('%' + plant + '%')}" if plant else ""
    ps = plants(rows)
    out = []
    for r in rows("SELECT key, coalesce(severity,''), first_seen::text,"
                  " last_seen::text FROM alert_state WHERE active"
                  f" {where} ORDER BY first_seen /*tag:alarms_active*/;"):
        if len(r) >= 4:
            out.append({"key": r[0], **_alarm_plant(r[0], ps), "severity": r[1],
                        "first_seen": r[2], "last_seen": r[3]})
    return out


def _maintenance(rows: Rows, plant: Optional[str], date_from: Optional[str],
                 date_to: Optional[str], open_only: bool) -> List[dict]:
    conds = []
    if plant:
        conds.append(f"plant_key = {_q(plant)}")
    if open_only:
        conds.append("end_ts IS NULL")
    if date_from and date_to:
        conds.append(f"start_ts::date <= DATE {_q(date_to)}"
                     f" AND coalesce(end_ts, now())::date >= DATE {_q(date_from)}")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    out = []
    for r in rows("SELECT id, plant_key, start_ts::text, coalesce(end_ts::text,''),"
                  " category, coalesce(note,''), coalesce(approved_by,'')"
                  f" FROM maintenance_event{where}"
                  " ORDER BY start_ts DESC LIMIT 50 /*tag:maintenance*/;"):
        if len(r) >= 7:
            out.append({"id": _i(r[0]), "plant_key": r[1], "start": r[2],
                        "end": r[3] or None, "category": r[4],
                        "note": r[5], "approved": bool(r[6])})
    return out


def _freshness(rows: Rows) -> dict:
    r = rows("SELECT (SELECT max(ts_utc) FROM telemetry)::text,"
             " (SELECT max(prod_date) FROM daily_production)::text"
             " /*tag:freshness*/;")
    if r and len(r[0]) >= 2:
        return {"telemetry_latest_utc": r[0][0] or None,
                "daily_kpi_latest_date": r[0][1] or None}
    return {}


# ------------------------------------------------------------------- tools
def get_portfolio_overview(rows: Rows) -> dict:
    """Every active plant: live today, month-to-date vs expected, 30-day
    PR/availability, active alarms."""
    ps = plants(rows)
    live = _today_live(rows)
    t = today_mx()
    mtd = _perf(rows, t.replace(day=1).isoformat(), t.isoformat())
    d30 = _perf(rows, (t - dt.timedelta(days=30)).isoformat(), t.isoformat())
    alarms = _active_alarms(rows)
    out = []
    for k, p in ps.items():
        if not p["active"]:
            continue
        row = {"plant_key": k, "name": p["name"], "brand": p["brand"],
               "portfolio": p["portfolio"], "kwp_dc": p["kwp_dc"]}
        row.update(live.get(k, {"today_kwh": None, "current_kw": None,
                                "last_sample_age_min": None}))
        m = mtd.get(k, {})
        row["mtd_kwh"] = m.get("production_kwh")
        row["mtd_expected_kwh"] = m.get("expected_kwh")
        row["mtd_vs_expected_pct"] = m.get("vs_expected_pct")
        d = d30.get(k, {})
        row["pr_30d"] = d.get("pr")
        row["availability_30d_pct"] = d.get("availability_pct")
        row["active_alarms"] = [a["key"] for a in alarms if k in a["key"]]
        out.append(row)
    return {"as_of_mx": dt.datetime.now(MX).isoformat(timespec="minutes"),
            "plants": out,
            "totals": {"today_kwh": _r(sum(x["today_kwh"] or 0 for x in out)),
                       "current_kw": _r(sum(x.get("current_kw") or 0 for x in out)),
                       "active_alarms": len(alarms)},
            "source": {"tables": ["telemetry", "daily_production", "alert_state"],
                       **_freshness(rows)}}


def get_plant_overview(rows: Rows, plant: Any) -> dict:
    """One plant: configuration, live today, MTD, 30-day performance,
    active alarms, open maintenance events."""
    k = resolve_plant(rows, plant)
    p = plants(rows)[k]
    t = today_mx()
    inv = rows("SELECT count(*) FILTER (WHERE active), count(*) FROM inverter"
               f" WHERE plant_key = {_q(k)} /*tag:inverter_count*/;")
    return {"plant": p,
            "inverters_configured_active": _i(inv[0][0]) if inv else None,
            "today": _today_live(rows, k).get(k, {}),
            "month_to_date": _perf(rows, t.replace(day=1).isoformat(),
                                   t.isoformat(), k).get(k, {}),
            "last_30_days": _perf(rows, (t - dt.timedelta(days=30)).isoformat(),
                                  t.isoformat(), k).get(k, {}),
            "active_alarms": _active_alarms(rows, k),
            "open_maintenance": _maintenance(rows, k, None, None, True),
            "source": {"tables": ["plant", "telemetry", "daily_production",
                                  "alert_state", "maintenance_event"],
                       **_freshness(rows)}}


def get_generation(rows: Rows, plant: Any, date_from: Any, date_to: Any) -> dict:
    """Daily production vs expected for one plant over a date range."""
    k = resolve_plant(rows, plant)
    a, b = _range(date_from, date_to)
    days = []
    for r in rows("SELECT prod_date::text, energy_kwh, expected_kwh,"
                  " irradiance_kwh_m2, pr, availability, coalesce(data_class,''),"
                  " coalesce(status_note,'') FROM daily_production"
                  f" WHERE plant_key = {_q(k)}"
                  f" AND prod_date BETWEEN DATE {_q(a)} AND DATE {_q(b)}"
                  " ORDER BY prod_date /*tag:daily_range*/;"):
        if len(r) >= 8:
            e, x = _f(r[1]), _f(r[2])
            days.append({"date": r[0], "energy_kwh": _r(e), "expected_kwh": _r(x),
                         "vs_expected_pct": _pct(e, x) if x else None,
                         "irradiance_kwh_m2": _r(_f(r[3]), 2),
                         "pr": _r(_f(r[4]), 3),
                         "availability_pct": _r((_f(r[5]) or 0) * 100) if _f(r[5]) is not None else None,
                         "data_class": r[6] or None, "note": r[7] or None})
    return {"plant_key": k, "name": plants(rows)[k]["name"],
            "date_from": a, "date_to": b, "days": days,
            "totals": _perf(rows, a, b, k).get(k, {}),
            "note": "expected_kwh is the irradiance-based expectation stamped "
                    "by the daily KPI job; vs_expected only counts days that "
                    "have one.",
            "source": {"tables": ["daily_production"], **_freshness(rows)}}


def get_performance(rows: Rows, date_from: Any, date_to: Any,
                    plant: Any = None) -> dict:
    """PR, availability and production vs expected per plant over a
    range — all plants (worst first) or one. Use it to compare plants
    or periods."""
    a, b = _range(date_from, date_to)
    k = resolve_plant(rows, plant) if plant else None
    ps = plants(rows)
    perf = _perf(rows, a, b, k)
    out = []
    for pk, v in perf.items():
        p = ps.get(pk, {})
        out.append({"plant_key": pk, "name": p.get("name"),
                    "portfolio": p.get("portfolio"), "kwp_dc": p.get("kwp_dc"),
                    "specific_yield_kwh_per_kwp": _r((v["production_kwh"] or 0) / p["kwp_dc"])
                    if p.get("kwp_dc") else None, **v})
    out.sort(key=lambda x: (x["vs_expected_pct"] is None, x["vs_expected_pct"] or 0))
    return {"date_from": a, "date_to": b, "plants": out,
            "totals": _fleet_totals(out),
            "source": {"tables": ["daily_production"], **_freshness(rows)}}


def _fleet_totals(plant_rows: List[dict]) -> dict:
    """The summary row for a plant table: sums for energy, kWp-weighted
    means for PR and availability (a 155 kWp plant must not pull the
    fleet number as hard as an 818 kWp one), count of plants."""
    prod = sum(x.get("production_kwh") or 0 for x in plant_rows)
    exp = sum(x.get("expected_kwh") or 0 for x in plant_rows)
    kwp = sum(x.get("kwp_dc") or 0 for x in plant_rows)

    def wmean(key):
        pairs = [(x[key], x.get("kwp_dc") or 0) for x in plant_rows
                 if x.get(key) is not None and (x.get("kwp_dc") or 0) > 0]
        w = sum(k for _, k in pairs)
        return _r(sum(v * k for v, k in pairs) / w, 3) if w else None
    return {"plants": len(plant_rows), "kwp_dc": _r(kwp), "production_kwh": _r(prod),
            "expected_kwh": _r(exp), "vs_expected_pct": _pct(prod, exp) if exp else None,
            "specific_yield_kwh_per_kwp": _r(prod / kwp) if kwp else None,
            "pr": wmean("pr"), "availability_pct": wmean("availability_pct"),
            "basis": "sums; PR and availability kWp-weighted"}


def get_inverter_performance(rows: Rows, plant: Any, date: Any = "today") -> dict:
    """Per-inverter energy, specific yield, status and fault for one
    plant on one day; flags under-performers and silent inverters."""
    k = resolve_plant(rows, plant)
    d = _date(date, "date")
    brand = plants(rows)[k]["brand"]
    cfg = {}
    for r in rows("SELECT inverter_sn, coalesce(inverter_label, inverter_sn),"
                  f" rated_kw FROM inverter WHERE plant_key = {_q(k)} AND active"
                  " ORDER BY 2 /*tag:inverters*/;"):
        if len(r) >= 3:
            cfg[r[0]] = {"sn": r[0], "label": r[1], "rated_kw": _f(r[2])}
    seen = {}
    for r in rows(
            "SELECT DISTINCT ON (inverter_sn) inverter_sn, e, status,"
            " coalesce(fault_code::text,''), power_w,"
            " to_char(ts_utc AT TIME ZONE 'America/Mexico_City','HH24:MI'), n"
            " FROM (SELECT inverter_sn, status, fault_code, power_w, ts_utc,"
            "   max(etoday_kwh) OVER (PARTITION BY inverter_sn) AS e,"
            "   count(*) OVER (PARTITION BY inverter_sn) AS n"
            f"  FROM telemetry WHERE plant_key = {_q(k)} AND {MX_D} = DATE {_q(d)}"
            "   AND (etoday_kwh IS NOT NULL OR power_w IS NOT NULL)) t"
            " ORDER BY inverter_sn, ts_utc DESC /*tag:inverter_day*/;"):
        if len(r) >= 7:
            seen[r[0]] = {"energy_kwh": _r(_f(r[1])), "status": _i(r[2]),
                          "fault_raw": r[3] if not is_normal_state(brand, r[3]) else "",
                          "power_kw": _r((_f(r[4]) or 0) / 1000, 2) if _f(r[4]) is not None else None,
                          "last_sample_mx": r[5], "samples": _i(r[6])}
    inv = []
    for sn, c in cfg.items():
        s = seen.get(sn)
        row = dict(c)
        if s is None:
            row.update({"energy_kwh": None, "status": None, "fault": None,
                        "power_kw": None, "last_sample_mx": None, "samples": 0,
                        "silent": True})
        else:
            row.update(s)
            row["fault"] = explain_fault(brand, s["fault_raw"]) if s["fault_raw"] else None
            row.pop("fault_raw", None)
            row["silent"] = False
        row["specific_yield_kwh_per_kw"] = (
            _r(row["energy_kwh"] / c["rated_kw"], 2)
            if row["energy_kwh"] is not None and c["rated_kw"] else None)
        inv.append(row)
    for sn in seen:                      # reporting but not configured
        if sn not in cfg:
            s = dict(seen[sn])
            s.update({"sn": sn, "label": sn, "rated_kw": None, "silent": False,
                      "fault": None, "specific_yield_kwh_per_kw": None,
                      "unconfigured": True})
            s.pop("fault_raw", None)
            inv.append(s)
    yields = sorted(x["specific_yield_kwh_per_kw"] for x in inv
                    if x.get("specific_yield_kwh_per_kw") is not None)
    median = yields[len(yields) // 2] if yields else None
    for x in inv:
        y = x.get("specific_yield_kwh_per_kw")
        x["underperforming"] = bool(median and y is not None and y < 0.8 * median)
    return {"plant_key": k, "name": plants(rows)[k]["name"], "date": d,
            "brand": brand, "inverters": inv,
            "median_specific_yield_kwh_per_kw": median,
            "flags": {"silent": [x["label"] for x in inv if x["silent"]],
                      "underperforming": [x["label"] for x in inv if x["underperforming"]],
                      "faulted": [x["label"] for x in inv if x.get("fault")]},
            "note": "underperforming = specific yield below 80% of the plant's "
                    "median that day; silent = configured active but no sample.",
            "source": {"tables": ["inverter", "telemetry"], **_freshness(rows)}}


def get_active_alarms(rows: Rows, plant: Any = None) -> dict:
    """Alarms currently active in the alert engine plus open maintenance
    events (a plant under maintenance is suppressed from alarms)."""
    k = resolve_plant(rows, plant) if plant else None
    return {"plant_key": k, "name": plants(rows)[k]["name"] if k else None,
            "alarms": _active_alarms(rows, k),
            "open_maintenance": _maintenance(rows, k, None, None, True),
            "note": "alarm keys: plant-dark / plant-stale / inverter-silent / "
                    "recon-fail / infra. Vendor fault codes are per inverter — "
                    "see get_inverter_performance.",
            "source": {"tables": ["alert_state", "maintenance_event"],
                       **_freshness(rows)}}


def get_alarm_history(rows: Rows, date_from: Any, date_to: Any,
                      plant: Any = None) -> dict:
    """Alarms raised in a range (active or resolved), vendor fault codes
    seen per inverter per day, and maintenance events in the range."""
    a, b = _range(date_from, date_to)
    k = resolve_plant(rows, plant) if plant else None
    where = f" AND key LIKE {_q('%' + k + '%')}" if k else ""
    alarms = []
    ps = plants(rows)
    for r in rows("SELECT key, coalesce(severity,''), first_seen::text,"
                  " last_seen::text, active FROM alert_state"
                  f" WHERE first_seen::date <= DATE {_q(b)}"
                  f" AND last_seen::date >= DATE {_q(a)} {where}"
                  " ORDER BY first_seen DESC LIMIT 200 /*tag:alarms_range*/;"):
        if len(r) >= 5:
            alarms.append({"key": r[0], **_alarm_plant(r[0], ps), "severity": r[1],
                           "first_seen": r[2], "last_seen": r[3], "active": r[4] == "t"})
    pwhere = f" AND t.plant_key = {_q(k)}" if k else ""
    faults = []
    brands = {pk: p["brand"] for pk, p in ps.items()}
    for r in rows(f"SELECT {MX_D}, t.plant_key, coalesce(i.inverter_label, t.inverter_sn),"
                  " fault_code::text, count(*) FROM telemetry t"
                  " LEFT JOIN inverter i ON i.plant_key = t.plant_key"
                  "  AND i.inverter_sn = t.inverter_sn"
                  f" WHERE {MX_D} BETWEEN DATE {_q(a)} AND DATE {_q(b)}"
                  f" AND fault_code IS NOT NULL AND fault_code::text NOT IN ('', '0')"
                  f" {pwhere} GROUP BY 1, 2, 3, 4 ORDER BY 1 DESC, 2, 3"
                  " LIMIT 300 /*tag:faults_range*/;"):
        if len(r) >= 5 and not is_normal_state(brands.get(r[1], ""), r[3]):
            faults.append({"date": r[0], "plant_key": r[1],
                           "name": ps.get(r[1], {}).get("name"), "inverter": r[2],
                           "fault_code": r[3],
                           "fault": explain_fault(brands.get(r[1], ""), r[3]),
                           "samples": _i(r[4])})
    return {"plant_key": k, "name": plants(rows)[k]["name"] if k else None,
            "date_from": a, "date_to": b, "alarms": alarms,
            "inverter_faults": faults,
            "maintenance": _maintenance(rows, k, a, b, False),
            "source": {"tables": ["alert_state", "telemetry", "maintenance_event"],
                       **_freshness(rows)}}


def get_lost_generation(rows: Rows, plant: Any, date_from: Any, date_to: Any) -> dict:
    """Energy below expectation (expected − actual on days that fell
    short), valued at the plant's PPA tariff, with the maintenance
    events that overlap the range."""
    k = resolve_plant(rows, plant)
    a, b = _range(date_from, date_to)
    p = plants(rows)[k]
    r = rows("SELECT coalesce(sum(expected_kwh - energy_kwh)"
             "  FILTER (WHERE expected_kwh > energy_kwh), 0),"
             " count(*) FILTER (WHERE expected_kwh > energy_kwh),"
             " count(*) FILTER (WHERE expected_kwh > 0), count(*),"
             " coalesce(sum(expected_kwh) FILTER (WHERE expected_kwh > 0),0),"
             " coalesce(sum(energy_kwh) FILTER (WHERE expected_kwh > 0),0)"
             f" FROM daily_production WHERE plant_key = {_q(k)}"
             f" AND prod_date BETWEEN DATE {_q(a)} AND DATE {_q(b)} /*tag:lost*/;")
    row = r[0] if r else ["0", "0", "0", "0", "0", "0"]
    lost = _f(row[0]) or 0.0
    tariff = p["tariff_mxn_per_kwh"]
    worst = []
    for w in rows("SELECT prod_date::text, energy_kwh, expected_kwh,"
                  " coalesce(status_note,'') FROM daily_production"
                  f" WHERE plant_key = {_q(k)} AND expected_kwh > energy_kwh"
                  f" AND prod_date BETWEEN DATE {_q(a)} AND DATE {_q(b)}"
                  " ORDER BY (expected_kwh - energy_kwh) DESC LIMIT 5 /*tag:lost_days*/;"):
        if len(w) >= 4:
            worst.append({"date": w[0], "energy_kwh": _r(_f(w[1])),
                          "expected_kwh": _r(_f(w[2])),
                          "shortfall_kwh": _r((_f(w[2]) or 0) - (_f(w[1]) or 0)),
                          "note": w[3] or None})
    return {"plant_key": k, "name": p["name"], "date_from": a, "date_to": b,
            "lost_kwh": _r(lost), "days_below_expected": _i(row[1]),
            "days_with_expectation": _i(row[2]), "days_in_range": _i(row[3]),
            "expected_kwh": _r(_f(row[4])), "production_kwh": _r(_f(row[5])),
            "tariff_mxn_per_kwh": tariff,
            "lost_mxn": _r(lost * tariff, 0) if tariff else None,
            "worst_days": worst,
            "maintenance": _maintenance(rows, k, a, b, False),
            "note": "shortfall against the irradiance-based expectation; it "
                    "includes soiling, curtailment and data gaps, not only "
                    "inverter downtime. CAPEX plants have no tariff, so no MXN.",
            "source": {"tables": ["daily_production", "plant", "maintenance_event"],
                       **_freshness(rows)}}


def _month_overlap_days(a: str, b: str) -> Dict[str, tuple]:
    """{'YYYY-MM': (days of the range inside that month, days in month)}."""
    out: Dict[str, tuple] = {}
    d, end = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    while d <= end:
        ym = d.isoformat()[:7]
        nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        last = min(end, nxt - dt.timedelta(days=1))
        out[ym] = ((last - d).days + 1, (nxt - dt.timedelta(days=1)).day)
        d = nxt
    return out


def get_revenue(rows: Rows, date_from: Any, date_to: Any, plant: Any = None) -> dict:
    """Accrued PPA revenue: measured energy × the contract tariff of each
    month (contract_monthly.tariff_mxn, else the plant tariff) — the same
    rule as the reports' 'Revenue generated'. Also the contracted
    expectation prorated over the range. PPA plants only; CAPEX plants
    earn nothing for Argia and LaaS fees are not plants."""
    a, b = _range(date_from, date_to)
    k = resolve_plant(rows, plant) if plant else None
    ps = plants(rows)
    if k and ps[k]["portfolio"] != "PPA":
        return {"plant_key": k, "name": ps[k]["name"], "date_from": a, "date_to": b,
                "revenue_mxn": None,
                "note": f"{ps[k]['name']} is a CAPEX plant: the customer owns it, "
                        "Argia has no PPA revenue there."}
    where = f" AND d.plant_key = {_q(k)}" if k else ""
    out = []
    for r in rows(
            "SELECT d.plant_key, sum(d.energy_kwh),"
            " sum(d.energy_kwh * coalesce(nullif(cm.tariff_mxn,0),"
            "   p.tariff_mxn_per_kwh, 0)), count(*),"
            " min(d.prod_date)::text, max(d.prod_date)::text"
            " FROM daily_production d JOIN plant p ON p.plant_key = d.plant_key"
            " LEFT JOIN contract_monthly cm ON cm.plant_key = d.plant_key"
            "  AND cm.year = extract(year FROM d.prod_date)"
            "  AND cm.month = extract(month FROM d.prod_date)"
            f" WHERE p.portfolio = 'PPA' AND p.active"
            f" AND d.prod_date BETWEEN DATE {_q(a)} AND DATE {_q(b)} {where}"
            " GROUP BY 1 ORDER BY 1 /*tag:revenue*/;"):
        if len(r) >= 6:
            out.append({"plant_key": r[0], "name": ps.get(r[0], {}).get("name"),
                        "energy_kwh": _r(_f(r[1])), "revenue_mxn": _r(_f(r[2]), 0),
                        "days_with_data": _i(r[3]), "first_day": r[4], "last_day": r[5]})
    # contracted expectation, prorated by calendar days of the range
    overlap = _month_overlap_days(a, b)
    exp: Dict[str, float] = {}
    exp_kwh: Dict[str, float] = {}
    for r in rows("SELECT plant_key, year, month, coalesce(contract_kwh,0),"
                  " coalesce(tariff_mxn,0) FROM contract_monthly"
                  f" WHERE (year*100+month) BETWEEN {a[:4]}{a[5:7]} AND {b[:4]}{b[5:7]}"
                  f"{(' AND plant_key = ' + _q(k)) if k else ''} /*tag:contract*/;"):
        if len(r) >= 5 and r[0] in ps and ps[r[0]]["portfolio"] == "PPA":
            ym = f"{_i(r[1]):04d}-{_i(r[2]):02d}"
            if ym not in overlap:
                continue
            inr, nm = overlap[ym]
            t = _f(r[4]) or ps[r[0]]["tariff_mxn_per_kwh"] or 0
            kwh = (_f(r[3]) or 0) * inr / nm
            exp_kwh[r[0]] = exp_kwh.get(r[0], 0) + kwh
            exp[r[0]] = exp.get(r[0], 0) + kwh * t
    for row in out:
        row["contract_kwh"] = _r(exp_kwh.get(row["plant_key"]))
        row["contract_revenue_mxn"] = _r(exp.get(row["plant_key"]), 0)
        row["vs_contract_pct"] = _pct(row["revenue_mxn"], exp.get(row["plant_key"]))
    total = _r(sum(x["revenue_mxn"] or 0 for x in out), 0)
    total_exp = _r(sum(exp.values()), 0) if exp else None
    return {"plant_key": k, "date_from": a, "date_to": b, "plants": out,
            "totals": {"energy_kwh": _r(sum(x["energy_kwh"] or 0 for x in out)),
                       "revenue_mxn": total, "contract_revenue_mxn": total_exp,
                       "vs_contract_pct": _pct(total, total_exp)},
            "note": "accrual estimate: measured energy × contract tariff of each "
                    "month, PPA plants only, before IVA, not invoiced amounts. "
                    "LaaS fees (Pirelli) are not included. Days without a daily "
                    "KPI row (e.g. today) are not counted.",
            "source": {"tables": ["daily_production", "contract_monthly", "plant"],
                       **_freshness(rows)}}


# ------------------------------------------------------- reconciliation
def get_reconciliation(rows: Rows, date_from: Any, date_to: Any, plant: Any = None) -> dict:
    """The nightly reconciliation per plant-day: our 5-minute interval
    sum vs the vendor's own counter vs the KPI row, completeness, status
    and the reference the day was healed from. Use for 'is the data
    reliable', 'which days need review', 'was anything corrected'."""
    a, b = _range(date_from, date_to)
    k = resolve_plant(rows, plant) if plant else None
    ps = plants(rows)
    where = f" AND plant_key = {_q(k)}" if k else ""
    days = []
    for r in rows("SELECT plant_key, prod_date::text, interval_kwh, vendor_daily_kwh, kpi_kwh,"
                  " completeness_pct, variance_pct, status, coalesce(note,''),"
                  " reference_kwh, coalesce(reference_basis,'') FROM reconciliation_daily"
                  f" WHERE prod_date BETWEEN DATE {_q(a)} AND DATE {_q(b)}{where}"
                  " ORDER BY prod_date DESC, plant_key /*tag:recon_daily*/;"):
        if len(r) >= 11:
            days.append({"plant_key": r[0], "name": ps.get(r[0], {}).get("name"), "date": r[1],
                         "interval_kwh": _r(_f(r[2])), "vendor_daily_kwh": _r(_f(r[3])),
                         "kpi_kwh": _r(_f(r[4])), "completeness_pct": _r(_f(r[5])),
                         "variance_pct": _r(_f(r[6]), 2), "status": r[7], "note": r[8] or None,
                         "reference_kwh": _r(_f(r[9])), "reference_basis": r[10] or None})
    by_status: Dict[str, int] = {}
    for d in days:
        by_status[d["status"]] = by_status.get(d["status"], 0) + 1
    return {"plant_key": k, "date_from": a, "date_to": b, "days": days[:400],
            "totals": {"plant_days": len(days), "by_status": by_status,
                       "kpi_kwh": _r(sum(d["kpi_kwh"] or 0 for d in days))},
            "note": "PASS = inverter counters and the vendor plant daily agree within 1%; "
                    "REVIEW = a gap on one side (the note says whose); FAIL = >3% apart; "
                    "the inverter counters are the reference, the vendor figure only "
                    "raises, never lowers. Open days are retried nightly for 14 days.",
            "source": {"tables": ["reconciliation_daily"], **_freshness(rows)}}


def get_monthly_close(rows: Rows, month: Any = None) -> dict:
    """The monthly close per plant — billing kWh, basis, status, closed
    by whom — the gate the invoice annexes wait for."""
    ym = None
    if month:
        m = str(month).strip()[:7]
        if len(m) != 7 or m[4] != "-" or not (m[:4] + m[5:]).isdigit():
            raise ToolError(f"month must be YYYY-MM, got {month!r}")
        ym = m
    ps = plants(rows)
    where = f" WHERE to_char(ref_month,'YYYY-MM') = {_q(ym)}" if ym else ""
    out = []
    for r in rows("SELECT plant_key, to_char(ref_month,'YYYY-MM'), billing_kwh, coalesce(billing_basis,''),"
                  " status, coalesce(closed_at::text,''), coalesce(closed_by,''), coalesce(note,''),"
                  " interval_sum_kwh, vendor_daily_sum_kwh, vendor_monthly_kwh, lifetime_delta_kwh,"
                  " completeness_pct"
                  f" FROM reconciliation_monthly{where} ORDER BY ref_month DESC, plant_key"
                  " /*tag:recon_monthly*/ LIMIT 120;"):
        if len(r) >= 8:
            out.append({"plant_key": r[0], "name": ps.get(r[0], {}).get("name"), "month": r[1],
                        "billing_kwh": _r(_f(r[2])), "basis": r[3] or None, "status": r[4],
                        "closed": bool(r[5]), "closed_at": r[5] or None, "closed_by": r[6] or None,
                        "note": r[7] or None,
                        "interval_sum_kwh": _r(_f(r[8])) if len(r) > 8 else None,
                        "vendor_daily_sum_kwh": _r(_f(r[9])) if len(r) > 9 else None,
                        "vendor_monthly_kwh": _r(_f(r[10])) if len(r) > 10 else None,
                        "lifetime_delta_kwh": _r(_f(r[11])) if len(r) > 11 else None,
                        "completeness_pct": _r(_f(r[12])) if len(r) > 12 else None})
    return {"month": ym, "months": out,
            "totals": {"plant_months": len(out), "closed": sum(1 for x in out if x["closed"]),
                       "open": sum(1 for x in out if not x["closed"]),
                       "billing_kwh": _r(sum(x["billing_kwh"] or 0 for x in out))},
            "note": "a PASS month closes automatically; REVIEW/FAIL wait for a manual close; "
                    "closed months are frozen — nothing changes their kWh afterwards.",
            "source": {"tables": ["reconciliation_monthly"], **_freshness(rows)}}


# ------------------------------------------------------------ thermal
def get_thermal_health(rows: Rows, date_from: Any, date_to: Any, plant: Any = None) -> dict:
    """Inverter thermal health over a range: peak temperature, hours
    at/above 65 °C, hot events, deviation from the plant's peers and
    from ambient, suspected thermal derating and the kWh it cost."""
    from argia.analytics import thermal as TH
    a, b = _range(date_from, date_to)
    k = resolve_plant(rows, plant) if plant else None
    ps = plants(rows)
    where = f" AND plant_key = {_q(k)}" if k else ""
    out = []
    for r in rows("SELECT plant_key, inverter_sn, count(*), max(peak_c), sum(minutes_over_65),"
                  " sum(minutes_over_70), sum(events), max(dt_peer_peak_c), max(dt_ambient_peak_c),"
                  " sum(derating_minutes), sum(lost_kwh), count(*) FILTER (WHERE cooling_health='POOR'),"
                  " count(*) FILTER (WHERE cooling_health='WATCH') FROM thermal_daily"
                  f" WHERE prod_date BETWEEN DATE {_q(a)} AND DATE {_q(b)}{where}"
                  " GROUP BY 1, 2 ORDER BY 11 DESC NULLS LAST, 1, 2 /*tag:thermal*/;"):
        if len(r) >= 13:
            out.append({"plant_key": r[0], "name": ps.get(r[0], {}).get("name"), "inverter_sn": r[1],
                        "days": _i(r[2]), "peak_c": _f(r[3]), "hours_over_65": _r((_f(r[4]) or 0) / 60),
                        "hours_over_70": _r((_f(r[5]) or 0) / 60), "events": _i(r[6]),
                        "dt_peer_peak_c": _f(r[7]), "dt_ambient_peak_c": _f(r[8]),
                        "derating_hours": _r((_f(r[9]) or 0) / 60), "lost_kwh": _r(_f(r[10])),
                        "band": TH.band(_f(r[3])), "poor_days": _i(r[11]), "watch_days": _i(r[12])})
    curve = None
    if k:
        bins = TH.merge_bins((float(x[1]), int(x[2]), float(x[3])) for x in rows(
            "SELECT inverter_sn, bin_c, n, ratio_sum FROM thermal_bins"
            f" WHERE plant_key = {_q(k)} AND prod_date BETWEEN DATE {_q(a)} AND DATE {_q(b)} /*tag:thermal_bins*/;")
            if len(x) >= 4)
        curve = TH.derating_curve(bins) if bins else None
    return {"plant_key": k, "date_from": a, "date_to": b, "inverters": out,
            "totals": {"inverters": len(out), "hours_over_65": _r(sum(x["hours_over_65"] or 0 for x in out)),
                       "events": sum(x["events"] or 0 for x in out),
                       "derating_hours": _r(sum(x["derating_hours"] or 0 for x in out)),
                       "lost_kwh": _r(sum(x["lost_kwh"] or 0 for x in out))},
            "derating_curve": curve,
            "note": "ARGIA operational bands on internal inverter temperature (not warranty limits): "
                    "normal <50, watch 50-60, warning 60-65, high 65-70, critical >=70 C. dt_peer = the "
                    "unit minus the median of its plant peers (a cooling problem when >=5, POOR >=10). "
                    "lost_kwh = suspected thermal derating: intervals >=65 C where the unit, >=5 C hotter "
                    "than cooler peers, produced >=3% less per rated kW than they did. The derating curve "
                    "(actual/expected by temperature bin, knee_c) is measured from the fleet's own data. "
                    "Manufacturer manuals require ventilation and state derating on heat without quantifying it.",
            "source": {"tables": ["thermal_daily", "thermal_bins"], **_freshness(rows)}}


# --------------------------------------------------------------- CFE
_PERIODS = ("ENERGIA BASE", "ENERGIA INTERMEDIA", "ENERGIA PUNTA")


def get_cfe_tariffs(rows: Rows, tariff: Any = "GDMTH", region: Any = None,
                    month: Any = None) -> dict:
    """CFE industrial tariff charges for one scheme: every charge for a
    region, or the average / min / max across the 17 regions when no
    region is given. Latest CFE-verified month unless a month is given."""
    tc = str(tariff or "GDMTH").strip().upper()[:12]
    if not tc.replace("-", "").isalnum():
        raise ToolError(f"bad tariff code {tariff!r}")
    reg = str(region).strip().upper()[:40] if region else None
    if month:
        m = str(month).strip()[:7]
        if len(m) != 7 or m[4] != "-":
            raise ToolError(f"month must be YYYY-MM, got {month!r}")
    else:
        latest = rows("SELECT to_char(max(month),'YYYY-MM') FROM cfe_tariff"
                      f" WHERE tariff_code = {_q(tc)} AND source = 'cfe_scrape' /*tag:cfe_latest*/;")
        m = latest[0][0] if latest and latest[0] and latest[0][0] else None
        if not m:
            return {"tariff": tc, "error": f"no CFE-verified rows for tariff {tc}"}
    where = f" AND region = {_q(reg)}" if reg else ""
    charges = []
    for r in rows("SELECT charge_type, coalesce(unit,''), round(avg(value_mxn),4), round(min(value_mxn),4),"
                  " round(max(value_mxn),4), count(*), bool_or(source='cfe_scrape') FROM cfe_tariff"
                  f" WHERE tariff_code = {_q(tc)} AND to_char(month,'YYYY-MM') = {_q(m)}{where}"
                  " GROUP BY 1, 2 ORDER BY 1 /*tag:cfe_charges*/;"):
        if len(r) >= 7:
            charges.append({"charge": r[0], "unit": r[1], "avg": _f(r[2]), "min": _f(r[3]),
                            "max": _f(r[4]), "regions": _i(r[5]), "cfe_verified": r[6] == "t"})
    if not charges:
        return {"tariff": tc, "region": reg, "month": m,
                "error": f"no rows for {tc} {reg or '(all regions)'} in {m}"}
    energy = [c for c in charges if c["charge"] in _PERIODS]
    return {"tariff": tc, "region": reg or "average of all regions", "month": m,
            "charges": charges,
            "totals": {"energy_avg_mxn_per_kwh": _r(sum(c["avg"] for c in energy) / len(energy), 4)
                       if energy else None, "charges": len(charges)},
            "note": "MXN without IVA; region = CFE distribution division; source cfe_scrape = "
                    "CFE-verified, other rows are Master-DB seeds.",
            "source": {"tables": ["cfe_tariff"]}}


# ------------------------------------------------------- knowledge (AGS)
def search_standard(rows: Rows, query: Any, lang: Any = "en", limit: Any = 5) -> dict:
    """Full-text search over the ARGIA Golden Standard slides."""
    from argia.ask import knowledge as K
    q = str(query or "").strip()
    if len(q) < 2:
        raise ToolError("query is required")
    lang = str(lang or "en").lower()
    lang = lang if lang in K.LANGS else "en"
    try:
        lim = max(1, min(int(limit or 5), 10))
    except (TypeError, ValueError):
        lim = 5
    hits = []
    for r in rows(K.search_sql(q, lang, lim)):
        if len(r) >= 4:
            hits.append({"slide": _i(r[0]), "title": r[1], "excerpt": K.excerpt(r[2], q),
                         "rank": _f(r[3]), "ref": f"AGS slide {r[0]}" + (f" — {r[1]}" if r[1] else "")})
    return {"query": q, "lang": lang, "hits": hits,
            "totals": {"hits": len(hits)},
            "note": ("cite the slide as 'ARGIA Golden Standard, slide N — title'; "
                     "the deck is at https://portal.argia.com.mx/ags/" if hits else
                     f"nothing in the standard matches {q!r} — try other words or the other language"),
            "source": {"tables": ["knowledge"], "doc": "AGS"}}


# ---------------------------------------------------------- free SQL
def describe_tables(rows: Rows, tables: Any = None) -> dict:
    from argia.ask import sqltool as S
    lst = None
    if tables:
        lst = [str(t).strip().lower() for t in (tables if isinstance(tables, list) else str(tables).split(","))]
    return S.describe(rows, lst)


def query_database(rows: Rows, sql: Any) -> dict:
    """Run one read-only SELECT (guarded) and return rows by column."""
    from argia.ask import sqltool as S
    try:
        wrapped = S.wrap(str(sql or ""))
    except S.SqlRejected as e:
        raise ToolError(f"query rejected: {e}")
    try:
        raw = rows(wrapped)
    except Exception as e:                       # noqa: BLE001 — SQL error text helps the model
        msg = str(e)
        return {"error": "query failed: " + msg[msg.find("ERROR:"):][:400] if "ERROR:" in msg else msg[:400]}
    data = S.parse_rows(raw)
    # column sums so the answer's TOTAL row comes from here, not from the model
    sums: Dict[str, float] = {}
    for row in data:
        for col, v in row.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                sums[col] = sums.get(col, 0) + v
    return {"rows": data, "totals": {"rows": len(data), "capped": len(data) >= S.MAX_ROWS,
                                     "column_sums": {c: _r(v, 3) for c, v in sums.items()}},
            "note": "read-only query; rows capped at %d — aggregate in SQL rather than paging" % S.MAX_ROWS,
            "source": {"tables": ["(query)"]}}


# --------------------------------------------------------------- registry
_D = {"type": "string", "description": "YYYY-MM-DD, or 'today' / 'yesterday'"}
_P = {"type": "string",
      "description": "plant key (GTO1, MEX2, ...) or customer name"}

TOOLS: List[dict] = [
    {"name": "get_portfolio_overview",
     "description": "All active plants right now: today's kWh and kW, minutes "
                    "since last sample, month-to-date vs expected, 30-day PR and "
                    "availability, active alarm keys. Start here for 'how is the "
                    "fleet', 'anything to worry about', 'which plants are offline'.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_plant_overview",
     "description": "One plant: configuration, live today, month-to-date, 30-day "
                    "performance, active alarms, open maintenance.",
     "input_schema": {"type": "object", "properties": {"plant": _P},
                      "required": ["plant"]}},
    {"name": "get_generation",
     "description": "Daily energy vs expected, irradiance, PR and availability for "
                    "one plant over a date range, with range totals.",
     "input_schema": {"type": "object",
                      "properties": {"plant": _P, "date_from": _D, "date_to": _D},
                      "required": ["plant", "date_from", "date_to"]}},
    {"name": "get_performance",
     "description": "PR, availability, production vs expected and specific yield "
                    "per plant over a range, worst first. Omit plant to rank the "
                    "whole fleet; call twice with two ranges to compare periods.",
     "input_schema": {"type": "object",
                      "properties": {"date_from": _D, "date_to": _D, "plant": _P},
                      "required": ["date_from", "date_to"]}},
    {"name": "get_inverter_performance",
     "description": "Per-inverter energy, specific yield, status, fault and last "
                    "sample for one plant on one day; flags silent and "
                    "under-performing inverters. Use to explain a bad day.",
     "input_schema": {"type": "object",
                      "properties": {"plant": _P, "date": _D},
                      "required": ["plant"]}},
    {"name": "get_active_alarms",
     "description": "Alarms active now in the alert engine and open maintenance "
                    "events, fleet-wide or for one plant.",
     "input_schema": {"type": "object", "properties": {"plant": _P}}},
    {"name": "get_alarm_history",
     "description": "Alarms raised in a date range, vendor fault codes per "
                    "inverter per day, and maintenance events in the range.",
     "input_schema": {"type": "object",
                      "properties": {"date_from": _D, "date_to": _D, "plant": _P},
                      "required": ["date_from", "date_to"]}},
    {"name": "get_revenue",
     "description": "Accrued PPA revenue in MXN (measured energy × contract "
                    "tariff of each month) per PPA plant and in total over a date "
                    "range, with the contracted expectation. Use for 'how much "
                    "money did we make', 'revenue this month', 'ingresos'. CAPEX "
                    "plants have no revenue.",
     "input_schema": {"type": "object",
                      "properties": {"date_from": _D, "date_to": _D, "plant": _P},
                      "required": ["date_from", "date_to"]}},
    {"name": "get_reconciliation",
     "description": "Nightly data reconciliation per plant-day: our interval sum vs "
                    "the vendor counter vs the KPI row, completeness, PASS/REVIEW/FAIL "
                    "status, what the day was healed from. Use for 'is the data "
                    "reliable', 'which days need review', 'was anything corrected'.",
     "input_schema": {"type": "object",
                      "properties": {"date_from": _D, "date_to": _D, "plant": _P},
                      "required": ["date_from", "date_to"]}},
    {"name": "get_monthly_close",
     "description": "Monthly close per plant (billing kWh, basis, PASS/REVIEW/FAIL, closed "
                    "by whom) — the gate invoices wait for. Omit month for the latest months.",
     "input_schema": {"type": "object",
                      "properties": {"month": {"type": "string", "description": "YYYY-MM"}}}},
    {"name": "get_thermal_health",
     "description": "Inverter thermal health over a range: peak internal temperature, hours "
                    ">= 65 C, hot events, deviation from plant peers and ambient, suspected "
                    "thermal derating and the kWh it cost, plus the measured derating curve "
                    "for one plant. Use for 'is the inverter too hot', 'does heat cost us "
                    "energy', 'which inverter needs cooling', 'temperature alarm evidence'.",
     "input_schema": {"type": "object",
                      "properties": {"date_from": _D, "date_to": _D, "plant": _P},
                      "required": ["date_from", "date_to"]}},
    {"name": "get_cfe_tariffs",
     "description": "CFE industrial tariff charges (BASE/INTERMEDIA/PUNTA energy, capacity, "
                    "distribution...) for a scheme such as GDMTH: one region, or the average "
                    "and min/max across all 17 CFE regions. Latest CFE-verified month by default.",
     "input_schema": {"type": "object",
                      "properties": {"tariff": {"type": "string", "description": "GDMTH (default), GDMTO, DIST, DIT, PDBT, GDBT, APBT, APMT, RABT, RAMT"},
                                     "region": {"type": "string", "description": "CFE division, e.g. BAJIO, GOLFO NORTE, JALISCO"},
                                     "month": {"type": "string", "description": "YYYY-MM"}}}},
    {"name": "search_standard",
     "description": "Search the ARGIA Golden Standard (the design, build and O&M standard, "
                    "364 training slides) and get the matching slides with excerpts. Use for "
                    "any 'what does the standard / AGS say', design rules, tolerances, "
                    "requirements, checklists, terminology.",
     "input_schema": {"type": "object",
                      "properties": {"query": {"type": "string", "description": "key words (not a sentence)"},
                                     "lang": {"type": "string", "description": "en (default), es or cz — the deck's language to search"},
                                     "limit": {"type": "integer", "description": "1-10, default 5"}},
                      "required": ["query"]}},
    {"name": "describe_tables",
     "description": "Columns and meaning of the database tables the assistant may query "
                    "with query_database. Call before writing SQL for something no other "
                    "tool covers.",
     "input_schema": {"type": "object",
                      "properties": {"tables": {"type": "string", "description": "comma-separated table names; omit for all"}}}},
    {"name": "query_database",
     "description": "Run ONE read-only SQL SELECT against the monitoring database when no "
                    "other tool answers the question (rows capped at 200, 10 s). Aggregate "
                    "in SQL. Internal users only. Always call describe_tables first.",
     "input_schema": {"type": "object",
                      "properties": {"sql": {"type": "string", "description": "a single SELECT statement"}},
                      "required": ["sql"]}},
    {"name": "get_lost_generation",
     "description": "kWh below expectation for one plant over a range, valued at "
                    "its PPA tariff in MXN, with the worst days and overlapping "
                    "maintenance events.",
     "input_schema": {"type": "object",
                      "properties": {"plant": _P, "date_from": _D, "date_to": _D},
                      "required": ["plant", "date_from", "date_to"]}},
]

DISPATCH: Dict[str, Callable[..., dict]] = {
    "get_portfolio_overview": get_portfolio_overview,
    "get_plant_overview": get_plant_overview,
    "get_generation": get_generation,
    "get_performance": get_performance,
    "get_inverter_performance": get_inverter_performance,
    "get_active_alarms": get_active_alarms,
    "get_alarm_history": get_alarm_history,
    "get_lost_generation": get_lost_generation,
    "get_revenue": get_revenue,
    "get_reconciliation": get_reconciliation,
    "get_monthly_close": get_monthly_close,
    "get_cfe_tariffs": get_cfe_tariffs,
    "get_thermal_health": get_thermal_health,
    "search_standard": search_standard,
    "describe_tables": describe_tables,
    "query_database": query_database,
}

# v215: what a customer-scoped account (a plant owner) never gets —
# fleet money and free SQL stay internal
INTERNAL_ONLY = {"query_database", "describe_tables", "get_revenue", "get_lost_generation",
                 "get_monthly_close", "get_reconciliation"}
_PLANT_LISTS = ("plants", "days", "months", "inverters", "alarms", "maintenance",
                "open_maintenance", "active_alarms", "worst_days", "inverter_faults")


def run_tool(rows: Rows, name: str, params: Optional[dict],
             scope: Optional[set] = None) -> dict:
    """Dispatch one call. Unknown tools and bad inputs come back as
    ``{"error": ...}`` so the model can recover; anything else raises.

    ``scope`` (v215) = the plant keys a customer-scoped account may see,
    None for internal users: internal-only tools are refused, a plant
    outside the scope is refused, and every list of plant rows in the
    result is filtered (totals are dropped — they would leak the fleet).
    """
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    allowed = set(next(t for t in TOOLS if t["name"] == name)
                  ["input_schema"]["properties"])
    params = {k: v for k, v in (params or {}).items() if k in allowed}
    if scope is not None:
        if name in INTERNAL_ONLY:
            return {"error": f"{name} is not available for this account"}
        if params.get("plant"):
            try:
                k = resolve_plant(rows, params["plant"])
            except ToolError as e:
                return {"error": str(e)}
            if k not in scope:
                return {"error": "that plant is not in your account's scope"}
    try:
        out = fn(rows, **params)
    except ToolError as e:
        return {"error": str(e)}
    except TypeError as e:                     # missing required argument
        return {"error": f"bad arguments for {name}: {e}"}
    if scope is not None and isinstance(out, dict):
        out = scope_result(out, scope)
    return out


def scope_result(out: dict, scope: set) -> dict:
    """Keep only rows of plants in scope; totals go (fleet figures)."""
    res = dict(out)
    for key in _PLANT_LISTS:
        v = res.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict) and "plant_key" in v[0]:
            res[key] = [x for x in v if x.get("plant_key") in scope]
    if res.get("plant_key") and res["plant_key"] not in scope:
        return {"error": "that plant is not in your account's scope"}
    res.pop("totals", None)
    return res


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
