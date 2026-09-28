"""v264 - /monitoring/losses/: what under-production cost, by plant and cause.

Pure rendering (no PostgreSQL): portal_gen reads ``loss_daily`` and passes
the rows here, so the page is unit-tested without a database. The method
is argia/analytics/losses.py; the numbers are written nightly by
scripts/loss_daily.py.
"""
from __future__ import annotations

import datetime as dt
import html
from typing import Callable, Dict, List, Optional

from portal_chrome import t, tile

CAUSES = (("unavailability", "Unavailability", "Indisponibilidad"),
          ("overheating", "Overheating", "Sobrecalentamiento"),
          ("underperformance", "Underperformance", "Bajo desempeño"))
PERIODS = (("30d", "Last 30 days", "Últimos 30 días"), ("mtd", "This month", "Este mes"),
           ("7d", "Last 7 days", "Últimos 7 días"), ("1d", "Yesterday", "Ayer"))
BASIS = {"peers": ("peers", "pares"), "weather": ("weather", "clima"),
         "weather-model": ("weather model only", "solo modelo de clima"), "none": ("no data", "sin datos")}
NUM = ("expected_weather_kwh", "expected_peers_kwh", "expected_kwh", "actual_kwh", "lost_kwh",
       "unavailability_kwh", "overheating_kwh", "underperformance_kwh", "excused_kwh", "tariff_mxn", "lost_mxn")


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalise(rows: List[List[str]]) -> List[dict]:
    """psql rows in the column order of ``SELECT_SQL`` -> dicts with floats."""
    out = []
    for r in rows:
        d = dict(zip(("plant_key", "prod_date", "expected_basis", "peers") + NUM, r))
        for k in NUM:
            d[k] = _f(d.get(k))
        out.append(d)
    return out


SELECT_SQL = ("SELECT plant_key, prod_date::text, expected_basis, peers, " + ", ".join(NUM)
              + " FROM loss_daily WHERE prod_date >= current_date - 45 ORDER BY plant_key, prod_date;")


def kwh(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:,.0f}"


def money(v: Optional[float]) -> str:
    return "-" if v is None else f"${v:,.0f}"


def period_rows(rows: List[dict], period: str, today: dt.date) -> List[dict]:
    y = today - dt.timedelta(days=1)
    start = {"1d": y, "7d": y - dt.timedelta(days=6), "30d": y - dt.timedelta(days=29),
             "mtd": y.replace(day=1)}[period]
    return [r for r in rows if start.isoformat() <= r["prod_date"] <= y.isoformat()]


def sums(rows: List[dict]) -> Dict[str, float]:
    s = {k: 0.0 for k in ("expected", "actual", "lost", "lost_mxn") + tuple(c for c, _, _ in CAUSES)
         + tuple(c + "_mxn" for c, _, _ in CAUSES)}
    s["priced"] = 0.0
    for r in rows:
        s["expected"] += r["expected_kwh"] or 0
        s["actual"] += r["actual_kwh"] or 0
        s["lost"] += r["lost_kwh"] or 0
        tar = r["tariff_mxn"]
        for c, _, _ in CAUSES:
            s[c] += r[c + "_kwh"] or 0
            if tar:
                s[c + "_mxn"] += (r[c + "_kwh"] or 0) * tar
        if tar:
            s["priced"] = 1.0
            s["lost_mxn"] += (r["lost_kwh"] or 0) * tar
    return s


def _basis_cell(rows: List[dict]) -> str:
    n: Dict[str, int] = {}
    for r in rows:
        n[r["expected_basis"]] = n.get(r["expected_basis"], 0) + 1
    parts = [t(f"{BASIS.get(b, (b, b))[0]} {c} d", f"{BASIS.get(b, (b, b))[1]} {c} d") for b, c in sorted(n.items())]
    return " · ".join(parts)


def plant_table(rows: List[dict], plants: Dict[str, dict], name: Callable[[str], str],
                link: Callable[[str], str]) -> str:
    head = ("<tr>" + "".join(f"<th>{t(en, es)}</th>" for en, es in (
        ("Plant", "Planta"), ("Expected kWh", "Esperado kWh"), ("Actual kWh", "Real kWh"),
        ("Lost kWh", "Perdido kWh"), ("Lost MXN", "Perdido MXN"))) +
        "".join(f"<th>{t(en + ' MXN', es + ' MXN')}</th>" for _, en, es in CAUSES) +
        f"<th>{t('Expected from', 'Esperado con')}</th></tr>")
    body, fleet = [], []
    by_plant: Dict[str, List[dict]] = {}
    for r in rows:
        by_plant.setdefault(r["plant_key"], []).append(r)
    for section in ("PPA", "CAPEX"):
        keys = [k for k in sorted(by_plant) if plants.get(k, {}).get("portfolio") == section]
        if not keys:
            continue
        body.append(f'<tr><td colspan="9" style="font-weight:700;background:#fafbfc">{section}</td></tr>')
        for k in sorted(keys, key=lambda k: -(sums(by_plant[k])["lost_mxn"] or sums(by_plant[k])["lost"] / 1000)):
            s = sums(by_plant[k])
            fleet.append(s)
            priced = s["priced"] > 0
            mx = (lambda v: money(v)) if priced else (lambda v: '<span class="muted">' + t("kWh only", "solo kWh") + "</span>")
            cls = ' class="st-FAIL"' if s["expected"] and s["lost"] > 0.10 * s["expected"] else (
                ' class="st-REVIEW"' if s["expected"] and s["lost"] > 0.03 * s["expected"] else "")
            cause_cells = "".join(
                f"<td>{mx(s[c + '_mxn']) if priced else kwh(s[c]) + ' kWh'}</td>" for c, _, _ in CAUSES)
            body.append(
                f'<tr><td><a href="{link(k)}">{html.escape(name(k))}</a> <span class="tkey">{k}</span></td>'
                f"<td>{kwh(s['expected'])}</td><td>{kwh(s['actual'])}</td><td{cls}>{kwh(s['lost'])}</td>"
                f"<td{cls}><b>{mx(s['lost_mxn'])}</b></td>{cause_cells}<td>{_basis_cell(by_plant[k])}</td></tr>")
    tot = {k: sum(s[k] for s in fleet) for k in ("expected", "actual", "lost", "lost_mxn")
           + tuple(c + "_mxn" for c, _, _ in CAUSES)}
    body.append('<tr style="font-weight:700;background:#fafbfc">'
                f'<td>{t("FLEET TOTAL (MXN: PPA only)", "TOTAL FLOTA (MXN: solo PPA)")}</td>'
                f"<td>{kwh(tot['expected'])}</td><td>{kwh(tot['actual'])}</td><td>{kwh(tot['lost'])}</td>"
                f"<td>{money(tot['lost_mxn'])}</td>"
                + "".join(f"<td>{money(tot[c + '_mxn'])}</td>" for c, _, _ in CAUSES) + "<td></td></tr>")
    return f"<table>{head}{''.join(body)}</table>"


def daily_detail(k: str, rows: List[dict], name: Callable[[str], str]) -> str:
    if not rows:
        return ""
    s = sums(rows)
    priced = s["priced"] > 0
    head = "<tr>" + "".join(f"<th>{t(en, es)}</th>" for en, es in (
        ("Date", "Fecha"), ("Weather kWh", "Clima kWh"), ("Peers kWh", "Pares kWh"),
        ("Expected kWh", "Esperado kWh"), ("Actual kWh", "Real kWh"), ("Lost kWh", "Perdido kWh"),
        ("Unavailability kWh", "Indisponibilidad kWh"), ("Overheating kWh", "Sobrecalentamiento kWh"),
        ("Underperformance kWh", "Bajo desempeño kWh"), ("Excused kWh", "Justificado kWh"),
        ("Lost MXN", "Perdido MXN"))) + "</tr>"
    trs = []
    for r in sorted(rows, key=lambda r: r["prod_date"], reverse=True):
        bad = r["expected_kwh"] and (r["lost_kwh"] or 0) > 0.10 * r["expected_kwh"]
        trs.append(
            f"<tr><td>{r['prod_date']}</td><td>{kwh(r['expected_weather_kwh'])}</td>"
            f"<td>{kwh(r['expected_peers_kwh'])}</td>"
            f"<td title=\"{html.escape(r['expected_basis'] + (' ' + r['peers'] if r['peers'] else ''))}\">{kwh(r['expected_kwh'])}</td>"
            f"<td>{kwh(r['actual_kwh'])}</td><td{' class=st-FAIL' if bad else ''}>{kwh(r['lost_kwh'])}</td>"
            f"<td>{kwh(r['unavailability_kwh'])}</td><td>{kwh(r['overheating_kwh'])}</td>"
            f"<td>{kwh(r['underperformance_kwh'])}</td><td>{kwh(r['excused_kwh'])}</td>"
            f"<td>{money(r['lost_mxn']) if priced else '-'}</td></tr>")
    title = (f"{html.escape(name(k))} <span class=\"tkey\">{k}</span> - "
             + (t(f"lost {s['lost']:,.0f} kWh = {money(s['lost_mxn'])} MXN in 30 days",
                  f"perdido {s['lost']:,.0f} kWh = {money(s['lost_mxn'])} MXN en 30 días") if priced else
                t(f"lost {s['lost']:,.0f} kWh in 30 days (CAPEX, no tariff)",
                  f"perdido {s['lost']:,.0f} kWh en 30 días (CAPEX, sin tarifa)")))
    return (f'<details id="loss-{k.lower()}" style="margin:8px 0"><summary style="cursor:pointer;font-weight:600">{title}</summary>'
            f'<table style="margin-top:6px">{head}{"".join(trs)}</table></details>')


METHOD_EN = ("Expected = what the plant should have made with 100% availability. First choice: its healthy "
             "neighbours within 60 km (kWh per kWp that day, times this plant's usual ratio to them over the last "
             "90 days). Otherwise: the weather model (measured irradiance) times the plant's usual actual/model "
             "ratio; 'weather model only' means the plant has not met its model in 90 days, so check the sensor "
             "or the design data. Lost = expected minus actual (vendor counters), split in this order: "
             "unavailability (inverters at 0 W in daylight, or silent while the daily counter shows they did not "
             "produce), overheating (derating measured by the thermal check), underperformance (the rest: ran but "
             "made less). Approved customer maintenance is excused: it is billed as deemed energy. MXN = the PPA "
             "tariff of the month; CAPEX plants are shown in kWh because ARGIA does not bill them per kWh.")
METHOD_ES = ("Esperado = lo que la planta debió producir con 100% de disponibilidad. Primero: sus vecinas sanas a "
             "menos de 60 km (kWh por kWp del día, por la relación habitual de esta planta con ellas en los últimos "
             "90 días). Si no hay: el modelo de clima (irradiancia medida) por la relación habitual real/modelo de la "
             "planta; 'solo modelo de clima' significa que la planta no ha alcanzado su modelo en 90 días: revisar el "
             "sensor o los datos de diseño. Perdido = esperado menos real (contadores del fabricante), en este orden: "
             "indisponibilidad (inversores en 0 W con luz, o sin datos mientras el contador diario muestra que no "
             "produjeron), sobrecalentamiento (reducción medida por la revisión térmica), bajo desempeño (el resto: "
             "operó pero produjo menos). El mantenimiento aprobado del cliente se justifica: se factura como energía "
             "considerada. MXN = tarifa PPA del mes; las plantas CAPEX se muestran en kWh porque ARGIA no las factura por kWh.")


def render(rows: List[dict], plants: Dict[str, dict], today: dt.date, name: Callable[[str], str],
           link: Callable[[str], str]) -> str:
    if not rows:
        return (f'<div class="card"><p>{t("No loss figures yet: the nightly calculation has not run.", "Aún no hay cifras de pérdidas: el cálculo nocturno no ha corrido.")}</p></div>')
    tiles = []
    for p, en, es in PERIODS:
        s = sums([r for r in period_rows(rows, p, today)
                  if plants.get(r["plant_key"], {}).get("portfolio") == "PPA"])
        tiles.append(tile(f"Lost, {en.lower()}", f"Perdido, {es.lower()}", money(s["lost_mxn"]) + " <span class=unit>MXN</span>",
                          f"{s['lost']:,.0f} kWh, PPA plants", f"{s['lost']:,.0f} kWh, plantas PPA"))
    buttons = "".join(
        f'<button class="btn2 lossbtn{" on" if i == 0 else ""}" data-p="{p}" onclick="lossPeriod(\'{p}\')">{t(en, es)}</button>'
        for i, (p, en, es) in enumerate(PERIODS))
    tables = "".join(
        f'<div class="lossp" data-p="{p}"{"" if i == 0 else " hidden"}>'
        f'{plant_table(period_rows(rows, p, today), plants, name, link)}</div>'
        for i, (p, _, _) in enumerate(PERIODS))
    by_plant: Dict[str, List[dict]] = {}
    for r in period_rows(rows, "30d", today):
        by_plant.setdefault(r["plant_key"], []).append(r)
    order = sorted(by_plant, key=lambda k: -(sums(by_plant[k])["lost_mxn"] or sums(by_plant[k])["lost"] / 1000))
    details = "".join(daily_detail(k, by_plant[k], name) for k in order)
    css = "<style>.lossbtn.on{background:#053b38;color:#fff;border-color:#053b38}</style>"
    js = ("<script>function lossPeriod(p){document.querySelectorAll('.lossp').forEach(function(e){e.hidden=e.dataset.p!==p;});"
          "document.querySelectorAll('.lossbtn').forEach(function(b){b.classList.toggle('on',b.dataset.p===p);});}</script>")
    return (f'<div class="tiles" style="margin-bottom:12px">{"".join(tiles)}</div>'
            f'<div class="card"><div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">{buttons}</div>{tables}</div>'
            f'<div class="card"><h2>{t("Day by day, last 30 days", "Día por día, últimos 30 días")}</h2>{details}</div>'
            f'<div class="card"><p class="note">{t(METHOD_EN, METHOD_ES)}</p></div>{css}{js}')


def plant_card(k: str, rows: List[dict], today: dt.date, link: str) -> str:
    """The same numbers, short, at the top of a plant's live page."""
    r30 = [r for r in period_rows(rows, "30d", today) if r["plant_key"] == k]
    if not r30:
        return ""
    s, y = sums(r30), sums([r for r in period_rows(rows, "1d", today) if r["plant_key"] == k])
    if s["priced"]:
        line_en = (f"Lost to under-production: yesterday {money(y['lost_mxn'])} MXN ({y['lost']:,.0f} kWh), "
                   f"last 30 days {money(s['lost_mxn'])} MXN ({s['lost']:,.0f} kWh): unavailability "
                   f"{money(s['unavailability_mxn'])}, overheating {money(s['overheating_mxn'])}, "
                   f"underperformance {money(s['underperformance_mxn'])}.")
        line_es = (f"Perdido por baja producción: ayer {money(y['lost_mxn'])} MXN ({y['lost']:,.0f} kWh), "
                   f"últimos 30 días {money(s['lost_mxn'])} MXN ({s['lost']:,.0f} kWh): indisponibilidad "
                   f"{money(s['unavailability_mxn'])}, sobrecalentamiento {money(s['overheating_mxn'])}, "
                   f"bajo desempeño {money(s['underperformance_mxn'])}.")
    else:
        line_en = (f"Lost to under-production: yesterday {y['lost']:,.0f} kWh, last 30 days {s['lost']:,.0f} kWh "
                   f"(unavailability {s['unavailability']:,.0f}, overheating {s['overheating']:,.0f}, "
                   f"underperformance {s['underperformance']:,.0f}); CAPEX, not billed per kWh.")
        line_es = (f"Perdido por baja producción: ayer {y['lost']:,.0f} kWh, últimos 30 días {s['lost']:,.0f} kWh "
                   f"(indisponibilidad {s['unavailability']:,.0f}, sobrecalentamiento {s['overheating']:,.0f}, "
                   f"bajo desempeño {s['underperformance']:,.0f}); CAPEX, no se factura por kWh.")
    more = f' <a href="{link}">{t("Day by day", "Día por día")}</a>' if link else ""
    return f'<div class="card" style="padding:10px 14px">{t(line_en, line_es)}{more}</div>'
