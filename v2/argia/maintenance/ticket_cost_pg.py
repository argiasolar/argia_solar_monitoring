"""v271 - what open tickets cost, read from PostgreSQL.

Moved out of maint_app.ticket_costs so the tickets page and the phone app
(portal_gen -> /app/) show the SAME figure for the same ticket. The query
runner is passed in (``rows_csv(sql) -> csv text``): maint_app gives its own
(which tests can replace), portal_gen gives pgq.psql_csv. The arithmetic is
argia.maintenance.ticket_cost (pure).

Two queries for the whole list. A missing loss table or telemetry never
breaks a caller: those tickets simply get no figure.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Dict, List, Optional

from argia.maintenance import ticket_cost as TC
from argia.maintenance import tickets as TK


def in_list(keys) -> str:
    return ",".join(TK._txt(k) for k in sorted(set(keys))) or "''"


def _f(v) -> Optional[float]:
    return float(v) if v not in (None, "") else None


def costs_for(tks: List[TK.Ticket], rows_csv: Callable[[str], str],
              today: Optional[dt.date] = None) -> Dict[str, List[TC.DayCost]]:
    """{ticket number: [DayCost]} for the days each ticket has been open."""
    today = today or dt.datetime.now(TC.MX).date()
    wins = {t.number: TC.window(t.created_at, t.resolved_at, t.closed_at, today) for t in tks}
    starts = [w[0] for w in wins.values() if w]
    if not starts:
        return {}
    since = min(starts).isoformat()
    plants = {t.plant_key for t in tks if wins.get(t.number)}
    loss: Dict[str, Dict[str, TC.PlantDay]] = {}
    try:
        for r in TK.rows_from_csv(rows_csv(
                "SELECT plant_key, prod_date::text AS d, lost_kwh, tariff_mxn, unavailability_kwh, overheating_kwh,"
                f" underperformance_kwh FROM loss_daily WHERE prod_date >= DATE '{since}'"
                f" AND plant_key IN ({in_list(plants)});")):
            loss.setdefault(r["plant_key"], {})[r["d"]] = TC.PlantDay(
                _f(r["lost_kwh"]), _f(r["tariff_mxn"]), _f(r["unavailability_kwh"]) or 0.0,
                _f(r["overheating_kwh"]) or 0.0, _f(r["underperformance_kwh"]) or 0.0)
    except Exception:                                    # noqa: BLE001
        return {}
    inv_plants = {t.plant_key for t in tks if t.inverter_sn and wins.get(t.number)}
    per_inv: Dict[tuple, tuple] = {}                     # (plant, sn, day) -> (rated_kw, kwh)
    if inv_plants:
        try:
            for r in TK.rows_from_csv(rows_csv(
                    "WITH inv AS (SELECT plant_key, inverter_sn, (ts_utc AT TIME ZONE 'America/Mexico_City')::date::text AS d,"
                    " max(etoday_kwh) AS kwh FROM telemetry WHERE plant_key IN (" + in_list(inv_plants) + ")"
                    f" AND ts_utc >= (DATE '{since}' AT TIME ZONE 'America/Mexico_City') GROUP BY 1, 2, 3)"
                    " SELECT inv.plant_key, inv.inverter_sn, inv.d, inv.kwh, i.rated_kw FROM inv"
                    " JOIN inverter i ON i.plant_key = inv.plant_key AND i.inverter_sn = inv.inverter_sn WHERE i.active;")):
                per_inv[(r["plant_key"], r["inverter_sn"], r["d"])] = (_f(r["rated_kw"]), _f(r["kwh"]))
        except Exception:                                # noqa: BLE001
            per_inv = {}
    out = {}
    for t in tks:
        days = wins.get(t.number) or []
        inverter = None
        if t.inverter_sn:
            inverter = {}
            for d in days:
                ds = d.isoformat()
                rated, kwh = per_inv.get((t.plant_key, t.inverter_sn, ds), (None, None))
                peers = [k / r for (pk, sn, dd), (r, k) in per_inv.items()
                         if pk == t.plant_key and dd == ds and sn != t.inverter_sn and r and k is not None]
                inverter[ds] = (rated, kwh, peers)
        out[t.number] = TC.ticket_days(days, loss.get(t.plant_key, {}), inverter)
    return out
