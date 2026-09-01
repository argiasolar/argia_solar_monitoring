"""Onboard TAM1 — Ryder Nuevo Laredo (CAPEX carport, Growatt).

Facts, verified live 2026-09-01 against Growatt plant 10902835 and the
uploaded sale contract (14.04.2025) / helioscope offer (07.01.2025):

  * 510 x Canadian Solar CS7N-715TB-AG bifacial  = 364.65 kWp DC
    (the offer simulated 700 W modules / 357 kWp; the installed plant
    registers 364,650 W — same count, 715 W modules)
  * 4 x Growatt 75 kW (MID/MAX family)           = 300 kW AC
    JNMAE7D006 / JNMAE5X00K / JNMAE7D007 / JNMAE5X00L,
    datalogger DYDDF6K04H (ShineMaster, 5-min), CHNT smart meter
  * site: Carretera Nuevo Laredo - Piedras Negras km 7.5, Las Sandias,
    88110 Nuevo Laredo, Tamaulipas (27.525437, -99.566937)
  * producing since 2026-05-13; CFE interconnection in process
  * investment 7,004,698 MXN sin IVA

Idempotent, dry-run by default. --apply then:
  1. appends TAM1 to the Plants / Inverters / Contract_Monthly tabs
     (skipped wherever the plant is already present),
  2. upserts the PG plant / inverter / contract_monthly rows,
  3. backfills daily_production + vendor_counter_snapshot +
     reconciliation_daily from Growatt's plant-level month chart
     (/panel/max/getMAXMonthChart — verified plant-level: identical for
     every inverter SN; sum since May within 0.2% of the four lifetime
     counters), 2026-05-13 .. yesterday — never today's partial day,
  4. prints a per-month PG-vs-vendor verification table.

Design baseline: helioscope monthly generation scaled by the installed
DC ratio 364.65/357 (same module count, bigger modules).

Usage: onboard_tam1.py [--apply]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG = logging.getLogger("argia.onboard_tam1")

PLANT_KEY = "TAM1"
GROWATT_PLANT_ID = "10902835"
FIRST_PRODUCTION = "2026-05-13"

# helioscope offer monthly grid energy (kWh) at 357 kWp DC
HELIOSCOPE_357 = {
    1: 30615.3, 2: 32380.4, 3: 46332.8, 4: 51495.9,
    5: 57996.0, 6: 60025.2, 7: 61713.6, 8: 58151.6,
    9: 45692.6, 10: 38434.5, 11: 28991.7, 12: 25660.2,
}
KWP_DC = 364.65
KWP_OFFER = 357.0
DC_SCALE = KWP_DC / KWP_OFFER

PLANT_FIELDS = {
    "plant_key": PLANT_KEY,
    "customer": "RYDER (Nuevo Laredo, TAM)",
    "brand": "GROWATT",
    "site_id": GROWATT_PLANT_ID,
    "kwp_dc": KWP_DC,
    "kwp_ac": 300,
    "lat": 27.525437,
    "lon": -99.566937,
    "expected_factor": 0.75,
    "pr_target": 0.9,
    "installation_date": "2026-05-12",
    "secret_api_name": "GROWATT_API_TOKEN",
    # no local irradiance sensor: linked to NL1's Monterrey env sensor
    # (same pattern as NL2) until the site gets its own
    "weather_plant_id": "10078094",
    "datalogger_sn": "DYD0DXH00M",
    "datalogger_addr": 33,
    "active": "TRUE",
    "module_count": 510,
    "module_wp": 715,
    "tilt_deg": 7,
    "azimuth_deg": 180,
    "notes": ("Carport. Producing since 2026-05-13; CFE interconnection "
              "in process. Irradiance proxied from NL1 (Monterrey) - "
              "no on-site sensor."),
    "pr_stc_model": 0.87,
    "gamma_pmax": -0.0029,          # CS7N-715TB-AG datasheet
    "monitoring_class": "B",
    "billing_scheme": "net_metering",
    "module_model": "CS7N-715TB-AG",
    "pr_baseline": 0.85,
    "om_cost_monthly_mxn": 3000,
    "portfolio": "CAPEX",
    "show_dashboard": "FALSE",
    "show_daily_report": "FALSE",
    "show_financial": "FALSE",
    "client_channel": "ryder",
}

INVERTERS = [
    ("JNMAE7D006", "Inversor 1"),
    ("JNMAE5X00K", "Inversor 2"),
    ("JNMAE7D007", "Inversor 3"),
    ("JNMAE5X00L", "Inversor 4"),
]
INVERTER_RATED_KW = 75
INVERTER_RATED_KW_DC = 91.2          # 510x715W over 4 units

INVESTMENT_MXN = 7004698             # offer total sin IVA


# ----------------- pure builders (unit-tested) -----------------

def design_kwh(month: int) -> float:
    """Helioscope monthly energy scaled to the installed 715 W modules."""
    return round(HELIOSCOPE_357[month] * DC_SCALE, 1)


def sheet_row(header: Sequence[str], fields: Dict[str, object]) -> List:
    """Order ``fields`` by the live tab header; unknown columns blank.
    Raises if a field has nowhere to go — a silent drop would register
    a half-configured plant."""
    hdr = [str(h or "").strip() for h in header]
    missing = [k for k in fields if k not in hdr]
    if missing:
        raise ValueError("tab header misses column(s): %s" % missing)
    return [fields.get(col, "") for col in hdr]


def inverter_fields(sn: str, label: str) -> Dict[str, object]:
    return {
        "plant_key": PLANT_KEY,
        "inverter_sn": sn,
        "inverter_label": label,
        "rated_kw": INVERTER_RATED_KW,
        "active": "TRUE",
        "rated_kw_dc": INVERTER_RATED_KW_DC,
        "phase": "P1",
        "date_producing": FIRST_PRODUCTION,
        "in_service_today": "TRUE",
    }


def contract_rows_2026() -> List[List]:
    """Contract_Monthly tab rows (design baseline only, no tariff):
    producing months of 2026."""
    return [[PLANT_KEY, 2026, m, design_kwh(m), "", "", "", ""]
            for m in range(5, 13)]


def month_days(ym: str, energies: Sequence[Optional[float]],
               first_production: str = FIRST_PRODUCTION,
               today_iso: Optional[str] = None) -> List[Tuple[str, float]]:
    """(date_iso, kwh) for one month-chart array.

    Days before first production are dropped (the plant did not exist);
    zero days AFTER first production are kept — a real outage must show
    as 0, not as a hole. Today's partial day is never emitted.
    """
    out = []
    for i, e in enumerate(energies, start=1):
        d = "%s-%02d" % (ym, i)
        try:
            dt.date.fromisoformat(d)
        except ValueError:
            continue                     # chart arrays are padded to 31
        if d < first_production:
            continue
        if today_iso and d >= today_iso:
            continue
        out.append((d, round(float(e or 0.0), 1)))
    return out


def daily_production_sql(days: Sequence[Tuple[str, float]]) -> str:
    """INSERT .. ON CONFLICT DO NOTHING — a backfill must never
    overwrite what the live pipeline already stored."""
    note = ("onboarding backfill 2026-09-01: Growatt plant-level month "
            "chart; billable=energy (CAPEX)")
    values = ",\n".join(
        "('%s',DATE '%s',%.1f,%.1f,'vendor-backfill','%s')"
        % (PLANT_KEY, d, kwh, kwh, note) for d, kwh in days)
    return ("INSERT INTO daily_production (plant_key, prod_date,"
            " energy_kwh, billable_kwh, source, status_note) VALUES\n"
            + values + "\nON CONFLICT (plant_key, prod_date) DO NOTHING;")


def snapshot_sql(days: Sequence[Tuple[str, float]]) -> str:
    values = ",\n".join(
        "('%s','GROWATT',DATE '%s',%.1f,'onboarding month-chart backfill')"
        % (PLANT_KEY, d, kwh) for d, kwh in days)
    return ("INSERT INTO vendor_counter_snapshot (plant_key, vendor,"
            " snap_date, daily_kwh, note) VALUES\n" + values
            + "\nON CONFLICT (plant_key, snap_date) DO NOTHING;")


def reconciliation_sql(days: Sequence[Tuple[str, float]]) -> str:
    values = ",\n".join(
        "('%s',DATE '%s',%.1f,%.1f,'OK','onboarding backfill "
        "(plant-level month chart)')" % (PLANT_KEY, d, kwh, kwh)
        for d, kwh in days)
    return ("INSERT INTO reconciliation_daily (plant_key, prod_date,"
            " vendor_daily_kwh, kpi_kwh, status, note) VALUES\n" + values
            + "\nON CONFLICT (plant_key, prod_date) DO NOTHING;")


def plant_sql() -> str:
    f = PLANT_FIELDS
    return (
        "INSERT INTO plant (plant_key, customer, brand, site_id, kwp_dc,"
        " kwp_ac, lat, lon, portfolio, pr_baseline, active,"
        " om_cost_monthly_mxn, investment_mxn) VALUES"
        " ('%s','%s','GROWATT','%s',%s,%s,%s,%s,'CAPEX',%s,TRUE,%s,%s)"
        " ON CONFLICT (plant_key) DO UPDATE SET"
        " customer=EXCLUDED.customer, brand=EXCLUDED.brand,"
        " site_id=EXCLUDED.site_id, kwp_dc=EXCLUDED.kwp_dc,"
        " kwp_ac=EXCLUDED.kwp_ac, lat=EXCLUDED.lat, lon=EXCLUDED.lon,"
        " portfolio=EXCLUDED.portfolio,"
        " pr_baseline=EXCLUDED.pr_baseline, active=TRUE,"
        " om_cost_monthly_mxn=EXCLUDED.om_cost_monthly_mxn,"
        " investment_mxn=EXCLUDED.investment_mxn;"
        % (PLANT_KEY, f["customer"], GROWATT_PLANT_ID, f["kwp_dc"],
           f["kwp_ac"], f["lat"], f["lon"], f["pr_baseline"],
           f["om_cost_monthly_mxn"], INVESTMENT_MXN))


def inverter_table_sql() -> str:
    values = ",\n".join(
        "('%s','%s','%s',%d,'P1',DATE '%s',TRUE)"
        % (PLANT_KEY, sn, label, INVERTER_RATED_KW, FIRST_PRODUCTION)
        for sn, label in INVERTERS)
    return ("INSERT INTO inverter (plant_key, inverter_sn,"
            " inverter_label, rated_kw, phase, date_producing, active)"
            " VALUES\n" + values
            + "\nON CONFLICT (plant_key, inverter_sn) DO NOTHING;")


def contract_table_sql() -> str:
    values = ",\n".join(
        "('%s',2026,%d,%.1f)" % (PLANT_KEY, m, design_kwh(m))
        for m in range(5, 13))
    return ("INSERT INTO contract_monthly (plant_key, year, month,"
            " design_kwh) VALUES\n" + values
            + "\nON CONFLICT (plant_key, year, month) DO UPDATE SET"
            " design_kwh=EXCLUDED.design_kwh;")


# ----------------- vendor fetch -----------------

def fetch_month_chart(client, ym: str) -> List[Optional[float]]:
    r = client._post("/panel/max/getMAXMonthChart",
                     {"maxSn": INVERTERS[0][0],
                      "plantId": GROWATT_PLANT_ID, "date": ym})
    raw = r.get("response", {}).get("_raw_text", "")
    obj = json.loads(raw)
    if obj.get("result") != 1:
        raise RuntimeError("month chart %s: %s" % (ym, raw[:200]))
    return obj["obj"]["energy"]


def months_between(first: str, last: str) -> List[str]:
    y0, m0 = int(first[:4]), int(first[5:7])
    y1, m1 = int(last[:4]), int(last[5:7])
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# ----------------- apply -----------------

def ensure_sheet_rows(sheets, apply: bool) -> None:
    plants = sheets.read_range("Plants", "A1:AZ")
    have = {str(r[0]).strip() for r in plants[1:] if r}
    if PLANT_KEY in have:
        LOG.info("Plants tab: %s already present — skipped", PLANT_KEY)
    else:
        row = sheet_row(plants[0], PLANT_FIELDS)
        LOG.info("Plants tab: appending %s", PLANT_KEY)
        if apply:
            sheets.append_rows("Plants", [row])

    inv = sheets.read_range("Inverters", "A1:Z")
    have_inv = {(str(r[0]).strip(), str(r[1]).strip())
                for r in inv[1:] if r and len(r) > 1}
    rows = [sheet_row(inv[0], inverter_fields(sn, label))
            for sn, label in INVERTERS
            if (PLANT_KEY, sn) not in have_inv]
    LOG.info("Inverters tab: appending %d row(s)", len(rows))
    if apply and rows:
        sheets.append_rows("Inverters", rows)

    con = sheets.read_range("Contract_Monthly", "A1:H")
    have_con = {(str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip())
                for r in con[1:] if r and len(r) > 2}
    rows = [r for r in contract_rows_2026()
            if (r[0], str(r[1]), str(r[2])) not in have_con]
    LOG.info("Contract_Monthly tab: appending %d row(s)", len(rows))
    if apply and rows:
        sheets.append_rows("Contract_Monthly", rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")

    from argia.core.sheets import SheetsClient
    from argia.core.time_utils import now_mx
    from argia.store import pg_mirror
    from argia.store.pgq import psql_exec, psql_rows
    from argia.vendors.growatt_web import GrowattWebClient

    today = now_mx().date().isoformat()

    # 1) sheet
    sheets = SheetsClient(
        sheet_id=os.environ.get("GOOGLE_SHEET_ID_V2", "").strip())
    ensure_sheet_rows(sheets, args.apply)

    if not pg_mirror.enabled():
        LOG.warning("ARGIA_PG_MIRROR not enabled — PG steps skipped")
        return 0

    # 2) PG config rows
    for sql in (plant_sql(), inverter_table_sql(), contract_table_sql()):
        if args.apply:
            psql_exec(sql)
    LOG.info("PG plant/inverter/contract_monthly upserts %s",
             "applied" if args.apply else "planned (dry-run)")

    # 3) history backfill from the plant-level month chart
    client = GrowattWebClient(username=os.environ["GROWATT_USERNAME"],
                              password=os.environ["GROWATT_PASSWORD"])
    client.login()
    days: List[Tuple[str, float]] = []
    chart_month_sum: Dict[str, float] = {}
    for ym in months_between(FIRST_PRODUCTION[:7], today[:7]):
        energies = fetch_month_chart(client, ym)
        got = month_days(ym, energies, today_iso=today)
        days.extend(got)
        chart_month_sum[ym] = round(sum(k for _, k in got), 1)
        LOG.info("month chart %s: %d day(s), %.1f kWh", ym, len(got),
                 chart_month_sum[ym])
    if args.apply and days:
        psql_exec(daily_production_sql(days))
        psql_exec(snapshot_sql(days))
        psql_exec(reconciliation_sql(days))

    # 4) verification: PG must now agree with the vendor chart
    if args.apply:
        rows = psql_rows(
            "SELECT to_char(prod_date,'YYYY-MM'), round(sum(energy_kwh)"
            "::numeric,1), count(*) FROM daily_production"
            f" WHERE plant_key='{PLANT_KEY}' GROUP BY 1 ORDER BY 1;")
        bad = 0
        for ym, kwh, n in rows:
            want = chart_month_sum.get(ym)
            ok = want is not None and abs(float(kwh) - want) < 0.5
            if not ok:
                bad += 1
            LOG.info("VERIFY %s: pg=%s kWh (%s d) vendor=%s -> %s",
                     ym, kwh, n, want, "OK" if ok else "MISMATCH")
        if bad:
            LOG.error("verification FAILED for %d month(s)", bad)
            return 1
        LOG.info("verification passed: PG matches the vendor chart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
