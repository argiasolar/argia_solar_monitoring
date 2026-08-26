"""Vendor cumulative-counter snapshots — parsers + upsert SQL. PURE.

One snapshot row per plant per day: the vendor's own daily / monthly /
lifetime energy counters as THEY report them, captured after generation
ends. This is the immutable audit trail the billing control rides on —
we never depend on querying a vendor's history months later.

Vendor sources:
  GROWATT    getMAXTotalData  -> eToday + eTotal (no monthly counter;
             monthly stays NULL and the close uses lifetime delta)
  HUAWEI     getStationRealKpi -> day_cap + month_power + total_power
  SOLAREDGE  /site/energy DAY + MONTH, /site/overview lifeTimeData
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from argia.core.normalize import safe_float
from argia.vendors.growatt_web_parser import (
    GrowattParseError,
    parse_max_total_data,
)


@dataclass(frozen=True)
class CounterSnapshot:
    plant_key: str
    vendor: str
    snap_date: str                       # ISO YYYY-MM-DD (MX plant-local)
    daily_kwh: Optional[float]
    monthly_kwh: Optional[float]
    lifetime_kwh: Optional[float]
    note: str = ""


# ---------------------------------------------------------------------------
# Growatt — plant-level eToday/eTotal from the getMAXTotalData envelope.
# ---------------------------------------------------------------------------
def growatt_counters(fixture_or_response: Any
                     ) -> Tuple[Optional[float], Optional[float],
                                Optional[float]]:
    """(daily, monthly, lifetime) kWh. Growatt web exposes no monthly
    counter -> monthly is always None."""
    try:
        total = parse_max_total_data(fixture_or_response)
    except GrowattParseError:
        return None, None, None
    if total is None:
        return None, None, None
    return total.e_today_kwh, None, total.e_total_kwh


# ---------------------------------------------------------------------------
# Huawei — one getStationRealKpi item's dataItemMap.
# ---------------------------------------------------------------------------
_H_DAY = ("day_cap", "daily_cap", "day_power")
_H_MONTH = ("month_cap", "month_power", "monthEnergy")
_H_TOTAL = ("total_cap", "total_power", "cumulativeEnergy")


def _pick_float(m: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    for k in keys:
        if k in m and m[k] is not None:
            v = safe_float(m[k])
            if v is not None:
                return v
    return None


def huawei_station_counters(result: Any) -> Dict[str, Tuple[
        Optional[float], Optional[float], Optional[float]]]:
    """{stationCode: (daily, monthly, lifetime) kWh} from a
    getStationRealKpi response. Unsuccessful/malformed -> {}."""
    out: Dict[str, Tuple[Optional[float], Optional[float],
                         Optional[float]]] = {}
    if not isinstance(result, dict) or not result.get("success"):
        return out
    for item in result.get("data") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("stationCode") or "").strip()
        m = item.get("dataItemMap")
        if not code or not isinstance(m, dict):
            continue
        out[code] = (_pick_float(m, _H_DAY), _pick_float(m, _H_MONTH),
                     _pick_float(m, _H_TOTAL))
    return out


# ---------------------------------------------------------------------------
# SolarEdge — /site/energy (unit-aware) and /site/overview lifetime.
# ---------------------------------------------------------------------------
def _unit_to_kwh(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    u = unit.strip().lower()
    if u == "wh" or u == "":
        return value / 1000.0
    if u == "kwh":
        return value
    if u == "mwh":
        return value * 1000.0
    return None  # unknown unit: refuse to guess (AGS-901 R6)


def solaredge_energy_kwh(response: Any) -> Optional[float]:
    """Sum of all values in a /site/{id}/energy response, in kWh.
    Works for timeUnit=DAY (one value) and timeUnit=MONTH alike.
    None when no dated value is present."""
    energy = (response or {}).get("energy") or {}
    unit = str(energy.get("unit", "Wh"))
    total = 0.0
    seen = False
    for entry in energy.get("values") or []:
        if not isinstance(entry, dict):
            continue
        v = safe_float(entry.get("value"))
        if v is None:
            continue
        kwh = _unit_to_kwh(v, unit)
        if kwh is None:
            return None
        total += kwh
        seen = True
    return round(total, 3) if seen else None


def solaredge_lifetime_kwh(response: Any) -> Optional[float]:
    """lifeTimeData.energy from /site/{id}/overview (documented in Wh)."""
    overview = (response or {}).get("overview") or {}
    life = overview.get("lifeTimeData") or {}
    v = safe_float(life.get("energy"))
    return round(v / 1000.0, 3) if v is not None else None


# ---------------------------------------------------------------------------
# Upsert SQL for vendor_counter_snapshot (pio06 PostgreSQL).
# ---------------------------------------------------------------------------
def _lit(v: Optional[float]) -> str:
    return "NULL" if v is None else f"{float(v):.3f}"


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def build_snapshot_upsert_sql(snaps: Sequence[CounterSnapshot]
                              ) -> Optional[str]:
    """One multi-row INSERT ... ON CONFLICT for the snapshots. A later
    capture of the same plant-day overwrites (the last capture of the
    night is the final one). None for an empty batch."""
    rows: List[str] = []
    for s in snaps:
        pk = str(s.plant_key or "").strip().upper()
        if not pk or not s.snap_date:
            continue
        rows.append(
            f"({_txt(pk)},{_txt(s.vendor)},{_txt(s.snap_date)},"
            f"{_lit(s.daily_kwh)},{_lit(s.monthly_kwh)},"
            f"{_lit(s.lifetime_kwh)},{_txt(s.note)})")
    if not rows:
        return None
    return (
        "INSERT INTO vendor_counter_snapshot "
        "(plant_key, vendor, snap_date, daily_kwh, monthly_kwh, "
        "lifetime_kwh, note) VALUES\n" + ",\n".join(rows) +
        "\nON CONFLICT (plant_key, snap_date) DO UPDATE SET "
        "vendor=EXCLUDED.vendor, daily_kwh=EXCLUDED.daily_kwh, "
        "monthly_kwh=EXCLUDED.monthly_kwh, "
        "lifetime_kwh=EXCLUDED.lifetime_kwh, note=EXCLUDED.note, "
        "captured_at=now();"
    )


# ---------------------------------------------------------------------------
# Historical daily series (for backfill — vendors DO let us go back in time).
# ---------------------------------------------------------------------------
def solaredge_daily_series(response: Any) -> Dict[str, Optional[float]]:
    """{'YYYY-MM-DD': kwh} from /site/{id}/energy timeUnit=DAY over a
    range. Dates with a null value map to None (day genuinely unknown)."""
    energy = (response or {}).get("energy") or {}
    unit = str(energy.get("unit", "Wh"))
    out: Dict[str, Optional[float]] = {}
    for entry in energy.get("values") or []:
        if not isinstance(entry, dict):
            continue
        d = str(entry.get("date", ""))[:10]
        if len(d) != 10:
            continue
        v = safe_float(entry.get("value"))
        kwh = _unit_to_kwh(v, unit) if v is not None else None
        out[d] = round(kwh, 3) if kwh is not None else None
    return out


def huawei_daily_series(result: Any) -> Dict[Tuple[str, str], Optional[float]]:
    """{(stationCode, 'YYYY-MM-DD'): kwh} from getKpiStationDay (one call
    covers a whole month). collectTime is epoch ms in station-local time;
    the date is taken from it in UTC-6 (all our plants are MX)."""
    import datetime as _dt
    out: Dict[Tuple[str, str], Optional[float]] = {}
    if not isinstance(result, dict) or not result.get("success"):
        return out
    tz = _dt.timezone(_dt.timedelta(hours=-6))
    for item in result.get("data") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("stationCode") or "").strip()
        ct = safe_float(item.get("collectTime"))
        m = item.get("dataItemMap")
        if not code or ct is None or not isinstance(m, dict):
            continue
        d = _dt.datetime.fromtimestamp(ct / 1000.0, tz).date().isoformat()
        out[(code, d)] = _pick_float(
            m, ("inverter_power", "product_power", "ongrid_power"))
    return out
