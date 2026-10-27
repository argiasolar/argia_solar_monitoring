#!/usr/bin/env python3
"""Onboard a plant from a spec file (v328) - plant, inverters, design.

    onboard_plant.py --spec /root/argia_onboard/GTO3.json            # dry run
    onboard_plant.py --spec /root/argia_onboard/GTO3.json --apply

The spec is customer data, so it lives on the server only (never in
this public repo). It is JSON:

    {"plant":     {<plant column>: value, ...},       # plant_key, customer,
                                                       # brand, site_id, kwp_dc,
                                                       # kwp_ac, lat, lon,
                                                       # portfolio, ... required
     "inverters": [{<inverter column>: value, ...}],   # inverter_sn,
                                                       # inverter_label, rated_kw
     "design":    {"annual_kwh": 1650000,             # optional: monthly
                   "shape_from": "GTO1",               # design rows, the
                   "years": [2026, 2027],              # annual figure spread
                   "from_month": 10}}                  # like a nearby plant's
                                                       # (from_month: first
                                                       # year only)

What it does (one SQL transaction, statement_timeout set):
  * INSERT plant / inverter / contract_monthly rows ON CONFLICT DO
    NOTHING - an existing row is never changed (edit those through
    /setup/, which audits); a re-run is a no-op;
  * before writing, the spec is checked (validate()): known columns
    only, the key fields present, kWp and the inverters' AC/DC ratings
    add up, coordinates inside Mexico, SNs unique - a half-configured
    plant is worse than none, so any problem stops the run.

History comes after, with the existing tools:
  vendor_history_import.py (energy, equal to the vendor's month totals),
  recon_backfill.py (vendor counters + reconciliation),
  irradiance_proxy_backfill.py (irradiance from the shared sensor).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argia.core.config_pg import (BOOL_COLS, DATE_COLS,  # noqa: E402
                                  INVERTERS_HEADER, PLANTS_HEADER)
from argia.core.normalize import growatt_site_ids  # noqa: E402

PLANT_COLUMNS = set(PLANTS_HEADER) | {"investment_mxn", "sla_target"}
INVERTER_COLUMNS = set(INVERTERS_HEADER)
PLANT_REQUIRED = ("plant_key", "customer", "brand", "site_id", "kwp_dc",
                  "kwp_ac", "lat", "lon", "portfolio", "installation_date")
INVERTER_REQUIRED = ("inverter_sn", "inverter_label", "rated_kw")
BRANDS = {"GROWATT", "HUAWEI", "SOLAREDGE", "SMA"}
PORTFOLIOS = {"PPA", "CAPEX"}
KEY_RE = re.compile(r"^[A-Z]{2,4}[0-9]{1,2}$")
# mainland Mexico, generous
LAT_RANGE = (14.0, 33.0)
LON_RANGE = (-118.0, -86.0)
RATING_TOL = 0.01           # inverter ratings must add up to the plant to 1 %
STATEMENT_TIMEOUT = "60s"


# ----------------------------------------------------------------- pure

def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def validate(spec: Dict[str, Any]) -> List[str]:
    """Every problem with the spec, in words. [] = good to write."""
    errs: List[str] = []
    plant = spec.get("plant") or {}
    invs = spec.get("inverters") or []
    unknown = sorted(set(plant) - PLANT_COLUMNS)
    if unknown:
        errs.append(f"plant: unknown column(s) {unknown}")
    for k in PLANT_REQUIRED:
        if plant.get(k) in (None, ""):
            errs.append(f"plant: {k} is required")
    key = str(plant.get("plant_key") or "")
    if key and not KEY_RE.match(key):
        errs.append(f"plant: plant_key {key!r} is not like GTO3")
    brand = str(plant.get("brand") or "").upper()
    if brand and brand not in BRANDS:
        errs.append(f"plant: brand {brand!r} not in {sorted(BRANDS)}")
    if plant.get("portfolio") and plant["portfolio"] not in PORTFOLIOS:
        errs.append(f"plant: portfolio must be one of {sorted(PORTFOLIOS)}")
    if brand == "GROWATT" and plant.get("site_id"):
        ids = growatt_site_ids(plant["site_id"])
        if not ids or not all(re.fullmatch(r"\d{6,12}", i) for i in ids):
            errs.append(f"plant: Growatt site_id {plant['site_id']!r} must be "
                        "plant id(s) of 6-12 digits, comma separated")
    kwp_dc, kwp_ac = _num(plant.get("kwp_dc")), _num(plant.get("kwp_ac"))
    for name, v in (("kwp_dc", kwp_dc), ("kwp_ac", kwp_ac)):
        if plant.get(name) not in (None, "") and (v is None or v <= 0):
            errs.append(f"plant: {name} must be a positive number")
    lat, lon = _num(plant.get("lat")), _num(plant.get("lon"))
    if plant.get("lat") not in (None, "") and plant.get("lon") not in (None, ""):
        if (lat is None or lon is None or not LAT_RANGE[0] <= lat <= LAT_RANGE[1]
                or not LON_RANGE[0] <= lon <= LON_RANGE[1]):
            errs.append(f"plant: lat/lon {plant.get('lat')}, {plant.get('lon')} "
                        "is not in Mexico")
    mc, mw = _num(plant.get("module_count")), _num(plant.get("module_wp"))
    if kwp_dc and mc and mw and abs(mc * mw / 1000.0 - kwp_dc) > 0.005 * kwp_dc:
        errs.append(f"plant: module_count x module_wp = {mc * mw / 1000.0:g} kWp "
                    f"but kwp_dc = {kwp_dc:g}")
    for c in DATE_COLS & set(plant):
        if plant[c] not in (None, "") and not _is_date(plant[c]):
            errs.append(f"plant: {c} {plant[c]!r} is not YYYY-MM-DD")

    if not invs:
        errs.append("inverters: at least one is required")
    sns = []
    for i, inv in enumerate(invs, 1):
        bad = sorted(set(inv) - INVERTER_COLUMNS - {"plant_key"})
        if bad:
            errs.append(f"inverter {i}: unknown column(s) {bad}")
        for k in INVERTER_REQUIRED:
            if inv.get(k) in (None, ""):
                errs.append(f"inverter {i}: {k} is required")
        if inv.get("plant_key") not in (None, key):
            errs.append(f"inverter {i}: plant_key {inv['plant_key']!r} != {key!r}")
        for c in DATE_COLS & set(inv):
            if inv[c] not in (None, "") and not _is_date(inv[c]):
                errs.append(f"inverter {i}: {c} {inv[c]!r} is not YYYY-MM-DD")
        sns.append(str(inv.get("inverter_sn") or ""))
    dup = sorted({s for s in sns if s and sns.count(s) > 1})
    if dup:
        errs.append(f"inverters: duplicate SN(s) {dup}")
    ac = [_num(i.get("rated_kw")) for i in invs]
    if kwp_ac and invs and None not in ac and abs(sum(ac) - kwp_ac) > RATING_TOL * kwp_ac:
        errs.append(f"inverters: rated_kw add up to {sum(ac):g} kW, plant kwp_ac is {kwp_ac:g}")
    dc = [_num(i.get("rated_kw_dc")) for i in invs]
    if kwp_dc and invs and None not in dc and abs(sum(dc) - kwp_dc) > RATING_TOL * kwp_dc:
        errs.append(f"inverters: rated_kw_dc add up to {sum(dc):g} kWp, plant kwp_dc is {kwp_dc:g}")

    design = spec.get("design")
    if design:
        if (_num(design.get("annual_kwh")) or 0) <= 0:
            errs.append("design: annual_kwh must be a positive number")
        if not KEY_RE.match(str(design.get("shape_from") or "")):
            errs.append("design: shape_from must be a plant key")
        fm = design.get("from_month", 1)
        if not isinstance(fm, int) or not 1 <= fm <= 12:
            errs.append("design: from_month must be 1-12")
        ys = design.get("years")
        if (not isinstance(ys, list) or not ys
                or not all(isinstance(y, int) and 2020 <= y <= 2060 for y in ys)
                or ys != sorted(set(ys))):
            errs.append("design: years must be a sorted list like [2026, 2027]")
    return errs


def _is_date(v: Any) -> bool:
    try:
        dt.date.fromisoformat(str(v))
        return True
    except ValueError:
        return False


def _lit(col: str, v: Any) -> str:
    """One SQL literal. Strings are quoted with '' doubled; booleans and
    dates typed for their columns."""
    if v is None or v == "":
        return "NULL"
    if col in BOOL_COLS:
        s = str(v).strip().lower()
        if s in ("true", "1", "yes", "y", "x"):
            return "TRUE"
        if s in ("false", "0", "no", "n"):
            return "FALSE"
        raise ValueError(f"{col}: {v!r} is not a flag")
    if col in DATE_COLS:
        return f"DATE '{dt.date.fromisoformat(str(v)).isoformat()}'"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _insert(table: str, row: Dict[str, Any], conflict: str) -> str:
    cols = list(row)
    return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ("
            + ", ".join(_lit(c, row[c]) for c in cols)
            + f") ON CONFLICT ({conflict}) DO NOTHING;")


def design_months(annual_kwh: float, shape: Dict[int, float],
                  from_month: int = 1) -> Dict[int, float]:
    """{month: kWh} for from_month..12: the annual figure spread like
    ``shape`` (a nearby plant's monthly design, any scale). The twelve
    months add up to annual_kwh; months before from_month are dropped
    (the plant did not exist)."""
    if sorted(shape) != list(range(1, 13)) or any(v <= 0 for v in shape.values()):
        raise ValueError("the shape needs a positive value for each of the 12 months")
    tot = sum(shape.values())
    return {m: round(annual_kwh * shape[m] / tot, 1) for m in range(from_month, 13)}


def design_rows(annual_kwh: float, shape: Dict[int, float], years: Sequence[int],
                from_month: int = 1) -> Dict[tuple, float]:
    """{(year, month): kWh}: every listed year at the full annual figure,
    the first one from ``from_month`` only. Pure."""
    out: Dict[tuple, float] = {}
    for i, y in enumerate(years):
        for m, kwh in design_months(annual_kwh, shape, from_month if i == 0 else 1).items():
            out[(y, m)] = kwh
    return out


def pick_shape(rows: Sequence[Sequence[str]]) -> Dict[int, float]:
    """A donor's monthly design from (year, month, kWh) rows: the latest
    year that has all twelve months with a positive value. {} if none."""
    by_year: Dict[int, Dict[int, float]] = {}
    for r in rows:
        try:
            y, m, v = int(r[0]), int(r[1]), float(r[2])
        except (ValueError, IndexError):
            continue
        if v > 0:
            by_year.setdefault(y, {})[m] = v
    full = [y for y, ms in by_year.items() if sorted(ms) == list(range(1, 13))]
    return by_year[max(full)] if full else {}


def build_sql(spec: Dict[str, Any], design: Optional[Dict[tuple, float]] = None) -> str:
    """The whole onboarding as one transaction. Pure."""
    plant = dict(spec["plant"])
    plant["brand"] = str(plant["brand"]).upper()
    plant.setdefault("active", True)
    key = plant["plant_key"]
    out = ["BEGIN;", f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}';",
           _insert("plant", plant, "plant_key")]
    for inv in spec["inverters"]:
        row = {"plant_key": key, **{k: v for k, v in inv.items() if k != "plant_key"}}
        row.setdefault("active", True)
        out.append(_insert("inverter", row, "plant_key, inverter_sn"))
    for (year, m), kwh in sorted((design or {}).items()):
        out.append(_insert("contract_monthly",
                           {"plant_key": key, "year": year, "month": m,
                            "design_kwh": kwh}, "plant_key, year, month"))
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def differences(spec_plant: Dict[str, Any], stored: Dict[str, str]) -> List[str]:
    """Spec fields whose stored value differs (an existing plant is not
    changed by this script - the list says what /setup/ would have to)."""
    out = []
    for c, v in spec_plant.items():
        s = stored.get(c, "")
        want = "" if v is None else str(v)
        a, b = _num(want), _num(s)
        same = (a is not None and b is not None and abs(a - b) < 1e-6) or \
            want.strip().lower() in (s.strip().lower(),
                                     {"t": "true", "f": "false"}.get(s.strip().lower(), "?"))
        if not same:
            out.append(f"{c}: stored {s!r}, spec {want!r}")
    return out


# ----------------------------------------------------------------- I/O

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="onboard a plant from a spec file")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args(argv)
    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    errs = validate(spec)
    if errs:
        print("SPEC INVALID - nothing written:")
        for e in errs:
            print("  - " + e)
        return 2
    from argia.store.pgq import psql_exec, psql_rows
    key = spec["plant"]["plant_key"]
    st = f"SET statement_timeout='{STATEMENT_TIMEOUT}';"

    design = None
    if spec.get("design"):
        d = spec["design"]
        rows = psql_rows(st + " SELECT year, month, design_kwh FROM contract_monthly"
                         f" WHERE plant_key = '{d['shape_from']}' ORDER BY year, month;")
        shape = pick_shape(rows)
        if not shape:
            print(f"SPEC INVALID - nothing written: {d['shape_from']} has no full year "
                  "of design rows to take the monthly shape from")
            return 2
        design = design_rows(float(d["annual_kwh"]), shape, d["years"], int(d.get("from_month", 1)))
        for y in d["years"]:
            ms = {m: v for (yy, m), v in design.items() if yy == y}
            print(f"design {key} {y}: {sum(ms.values()):,.0f} kWh, months {min(ms)}-{max(ms)}"
                  f" (shape of {d['shape_from']}, annual {float(d['annual_kwh']):,.0f} kWh)")

    cols = sorted(spec["plant"])
    have = psql_rows(st + f" SELECT {', '.join(cols)} FROM plant WHERE plant_key = '{key}';")
    if have:
        diff = differences(spec["plant"], dict(zip(cols, have[0])))
        print(f"{key} already exists - its row is left as stored"
              + ("; differences: " if diff else "; no differences"))
        for x in diff:
            print("  " + x)
    have_inv = {r[0] for r in psql_rows(
        st + f" SELECT inverter_sn FROM inverter WHERE plant_key = '{key}';") if r}
    new_inv = [i["inverter_sn"] for i in spec["inverters"] if i["inverter_sn"] not in have_inv]
    print(f"plant {key}: {'exists' if have else 'NEW'}; inverters new {len(new_inv)} "
          f"of {len(spec['inverters'])}: {', '.join(new_inv) or '-'}")

    sql = build_sql(spec, design)
    if not args.apply:
        print("DRY RUN - the SQL that --apply runs:")
        print(sql)
        return 0
    psql_exec(sql)
    n_inv = psql_rows(st + f" SELECT count(*) FROM inverter WHERE plant_key = '{key}';")
    n_p = psql_rows(st + f" SELECT count(*) FROM plant WHERE plant_key = '{key}';")
    ok = n_p and n_p[0][0] == "1" and n_inv and int(n_inv[0][0]) >= len(spec["inverters"])
    print(f"VERIFY {'OK' if ok else 'FAILED'}: plant rows {n_p[0][0] if n_p else 0}, "
          f"inverter rows {n_inv[0][0] if n_inv else 0}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
