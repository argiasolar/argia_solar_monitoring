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
def plants(rows: Rows) -> Dict[str, dict]:
    """Configured plants keyed by plant_key (active and inactive)."""
    out: Dict[str, dict] = {}
    for r in rows("SELECT plant_key, customer, brand, kwp_dc,"
                  " coalesce(portfolio,''), active,"
                  " coalesce(tariff_mxn_per_kwh,0), coalesce(pr_baseline,0)"
                  " FROM plant ORDER BY plant_key /*tag:plants*/;"):
        if len(r) >= 8:
            out[r[0]] = {"plant_key": r[0], "customer": r[1], "brand": r[2],
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
            if frag in p["customer"].lower() or frag in k.lower()]
    if len(hits) == 1:
        return hits[0]
    vocab = ", ".join(f"{k}={p['customer']}" for k, p in ps.items())
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


def _active_alarms(rows: Rows, plant: Optional[str] = None) -> List[dict]:
    where = f" AND key LIKE {_q('%' + plant + '%')}" if plant else ""
    out = []
    for r in rows("SELECT key, coalesce(severity,''), first_seen::text,"
                  " last_seen::text FROM alert_state WHERE active"
                  f" {where} ORDER BY first_seen /*tag:alarms_active*/;"):
        if len(r) >= 4:
            out.append({"key": r[0], "severity": r[1],
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
        row = {"plant_key": k, "customer": p["customer"], "brand": p["brand"],
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
    return {"plant_key": k, "date_from": a, "date_to": b, "days": days,
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
        out.append({"plant_key": pk, "customer": p.get("customer"),
                    "portfolio": p.get("portfolio"), "kwp_dc": p.get("kwp_dc"),
                    "specific_yield_kwh_per_kwp": _r((v["production_kwh"] or 0) / p["kwp_dc"])
                    if p.get("kwp_dc") else None, **v})
    out.sort(key=lambda x: (x["vs_expected_pct"] is None, x["vs_expected_pct"] or 0))
    return {"date_from": a, "date_to": b, "plants": out,
            "source": {"tables": ["daily_production"], **_freshness(rows)}}


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
    return {"plant_key": k, "date": d, "brand": brand, "inverters": inv,
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
    return {"plant_key": k, "alarms": _active_alarms(rows, k),
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
    for r in rows("SELECT key, coalesce(severity,''), first_seen::text,"
                  " last_seen::text, active FROM alert_state"
                  f" WHERE first_seen::date <= DATE {_q(b)}"
                  f" AND last_seen::date >= DATE {_q(a)} {where}"
                  " ORDER BY first_seen DESC LIMIT 200 /*tag:alarms_range*/;"):
        if len(r) >= 5:
            alarms.append({"key": r[0], "severity": r[1], "first_seen": r[2],
                           "last_seen": r[3], "active": r[4] == "t"})
    pwhere = f" AND t.plant_key = {_q(k)}" if k else ""
    faults = []
    brands = {pk: p["brand"] for pk, p in plants(rows).items()}
    for r in rows(f"SELECT {MX_D}, t.plant_key, coalesce(i.inverter_label, t.inverter_sn),"
                  " fault_code::text, count(*) FROM telemetry t"
                  " LEFT JOIN inverter i ON i.plant_key = t.plant_key"
                  "  AND i.inverter_sn = t.inverter_sn"
                  f" WHERE {MX_D} BETWEEN DATE {_q(a)} AND DATE {_q(b)}"
                  f" AND fault_code IS NOT NULL AND fault_code::text NOT IN ('', '0')"
                  f" {pwhere} GROUP BY 1, 2, 3, 4 ORDER BY 1 DESC, 2, 3"
                  " LIMIT 300 /*tag:faults_range*/;"):
        if len(r) >= 5 and not is_normal_state(brands.get(r[1], ""), r[3]):
            faults.append({"date": r[0], "plant_key": r[1], "inverter": r[2],
                           "fault_code": r[3],
                           "fault": explain_fault(brands.get(r[1], ""), r[3]),
                           "samples": _i(r[4])})
    return {"plant_key": k, "date_from": a, "date_to": b, "alarms": alarms,
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
    return {"plant_key": k, "date_from": a, "date_to": b,
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
}


def run_tool(rows: Rows, name: str, params: Optional[dict]) -> dict:
    """Dispatch one call. Unknown tools and bad inputs come back as
    ``{"error": ...}`` so the model can recover; anything else raises."""
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    allowed = set(next(t for t in TOOLS if t["name"] == name)
                  ["input_schema"]["properties"])
    params = {k: v for k, v in (params or {}).items() if k in allowed}
    try:
        return fn(rows, **params)
    except ToolError as e:
        return {"error": str(e)}
    except TypeError as e:                     # missing required argument
        return {"error": f"bad arguments for {name}: {e}"}


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
