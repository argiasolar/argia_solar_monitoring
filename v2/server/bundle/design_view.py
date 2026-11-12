"""v370 - /monitoring/design/: each PPA plant against its HelioScope as-connected design (internal only).

Tomasz, 12 Nov 2026: the Nov 2026 HelioScope runs are "our baseline for all calculations and monitoring".
Decided the same day: internal only - the customer invoice annex keeps the contract curve, so this page
lives under /monitoring/ (employees) and nothing here writes to the database.

Pure rendering: portal_gen reads daily_production and contract_monthly and passes the rows here; the
figures come from argia/kpi/design_model.py, so the page is unit-tested without a database.
"""
from __future__ import annotations

import calendar
import datetime as dt
import html
from typing import Callable, Dict, List, Optional, Tuple

from portal_chrome import t, tile

from argia.kpi import design_model as DM

SELECT_SQL = ("SELECT plant_key, prod_date::text, energy_kwh, irradiance_kwh_m2 FROM daily_production"
              " WHERE prod_date >= current_date - 400 ORDER BY plant_key, prod_date;")
CONTRACT_SQL = ("SELECT plant_key, year, month, contract_kwh FROM contract_monthly"
                " WHERE contract_kwh IS NOT NULL AND year >= extract(year from current_date)::int - 1;")
MONTHS_SHOWN = 12

METHOD_EN = ("EPI (energy performance index) = actual energy / what the design predicts for the sun that actually "
             "came: the design's kWh per kWh/m2 of that month (on the plane the plant's sensor measures) x the "
             "measured irradiance x ageing (0.55 %/yr from interconnection). It counts days with measured "
             "irradiance; a day whose raw PR is above 1.05 is skipped (its sun was undercounted). In line = within "
             "3 % of the design. 'Of design' compares energy with the design month itself (typical weather), "
             "'Sun' says how that month's measured irradiance compares with the design year, so a month below "
             "design in cloudy weather reads as resource, not as the plant. Internal: the customer invoice annex "
             "keeps the contract curve.")
METHOD_ES = ("EPI (índice de desempeño energético) = energía real / lo que el diseño predice para el sol que "
             "realmente llegó: los kWh por kWh/m2 del diseño en ese mes (en el plano que mide el sensor de la "
             "planta) x la irradiancia medida x envejecimiento (0.55 %/año desde la interconexión). Cuenta los días "
             "con irradiancia medida; un día con PR bruto mayor a 1.05 se omite (su sol se midió de menos). En línea "
             "= dentro de 3 % del diseño. 'Del diseño' compara la energía con el mes de diseño (clima típico), "
             "'Sol' dice cómo fue la irradiancia medida frente al año de diseño, así un mes bajo el diseño por "
             "nubes se lee como recurso, no como la planta. Interno: el anexo de facturación del cliente conserva "
             "la curva del contrato.")
BAND_TXT = {"in line": ("in line", "en línea"), "above": ("above", "arriba"), "below": ("below", "abajo")}


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalise(rows: List[List[str]]) -> Dict[str, List[Tuple[dt.date, Optional[float], Optional[float]]]]:
    """psql rows of SELECT_SQL -> {plant: [(day, energy, irradiance)]}. Pure."""
    out: Dict[str, List] = {}
    for r in rows:
        if len(r) < 4:
            continue
        try:
            d = dt.date.fromisoformat(r[1])
        except ValueError:
            continue
        out.setdefault(r[0], []).append((d, _f(r[2]), _f(r[3])))
    return out


def contracts(rows: List[List[str]]) -> Dict[Tuple[str, int, int], float]:
    out = {}
    for r in rows:
        try:
            out[(r[0], int(r[1]), int(r[2]))] = float(r[3])
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _pct(x: Optional[float], d: int = 1) -> str:
    return " - " if x is None else f"{x * 100:,.{d}f}%"


def epi_cell(e: DM.Epi) -> str:
    """The EPI as a coloured pill: in line green, below amber (red beyond 10 %), above grey. Pure."""
    if e.epi is None:
        return '<span class="muted"> - </span>'
    b = DM.band(e.epi)
    cls = {"in line": "ok", "above": "off"}.get(b, "bad" if e.epi < 0.90 else "warn")
    en, es = BAND_TXT[b]
    tip = (f"{e.actual_kwh:,.0f} kWh actual / {e.expected_kwh:,.0f} kWh design-expected, {e.days} day(s)"
           + (f", {e.skipped} skipped (sun undercounted)" if e.skipped else ""))
    return (f'<span class="pill {cls}" title="{html.escape(tip, quote=True)}">{e.epi * 100:,.1f}% '
            f'{t(en, es)}</span>')


def periods(today: dt.date) -> List[Tuple[str, str, str, dt.date, dt.date]]:
    """(key, EN, ES, first, last) up to yesterday - the last measured day. Pure."""
    y = today - dt.timedelta(days=1)
    m0 = y.replace(day=1)
    pm_last = m0 - dt.timedelta(days=1)
    return [("30d", "Last 30 days", "Últimos 30 días", y - dt.timedelta(days=29), y),
            ("mtd", "This month", "Este mes", m0, y),
            ("pm", pm_last.strftime("%b %Y"), pm_last.strftime("%m/%Y"), pm_last.replace(day=1), pm_last),
            ("ytd", "Year to date", "Año en curso", y.replace(month=1, day=1), y)]


def _between(days, a: dt.date, b: dt.date):
    return [x for x in days if a <= x[0] <= b]


def _operating(d: DM.Design, days, a: dt.date, b: dt.date):
    """Days of [a, b] from interconnection on: commissioning days before it are not the plant. Pure."""
    return _between(days, max(a, d.cod), b)


def month_rows(d: DM.Design, days, today: dt.date, kwp: Optional[float]) -> List[dict]:
    """The last MONTHS_SHOWN months up to yesterday, oldest first. Pure."""
    y = today - dt.timedelta(days=1)
    out = []
    yy, mm = y.year, y.month
    keys = []
    for _ in range(MONTHS_SHOWN):
        keys.append((yy, mm))
        mm -= 1
        if mm == 0:
            yy, mm = yy - 1, 12
    for yy, mm in reversed(keys):
        first = dt.date(yy, mm, 1)
        last = min(dt.date(yy, mm, calendar.monthrange(yy, mm)[1]), y)
        ds = _between(days, first, last)
        if first < d.cod.replace(day=1) or not ds:
            continue
        actual = sum(e for _, e, _ in ds if e is not None)
        elapsed = (last - first).days + 1
        design = d.design_kwh_month(yy, mm) * elapsed / calendar.monthrange(yy, mm)[1]
        meas = [(x, irr) for x, _, irr in ds if irr is not None and irr > 0]
        out.append({"ym": f"{yy}-{mm:02d}", "partial": elapsed < calendar.monthrange(yy, mm)[1],
                    "actual": actual, "design": design, "of_design": actual / design if design else None,
                    "sun": d.resource(yy, mm, sum(i for _, i in meas), len(meas)),
                    "epi": DM.epi(d, ds, kwp)})
    return out


def summary_table(designs: Dict[str, DM.Design], data, contract, plants: Dict[str, dict], today: dt.date,
                  name: Callable[[str], str], link: Callable[[str], str]) -> str:
    ps = periods(today)
    year = (today - dt.timedelta(days=1)).year
    head = ("<tr>" + "".join(f"<th>{t(en, es)}</th>" for en, es in (
        ("Plant", "Planta"), (f"Design {year}, MWh", f"Diseño {year}, MWh"),
        (f"Contract {year}, MWh", f"Contrato {year}, MWh"), ("Design vs contract", "Diseño vs contrato")))
        + "".join(f"<th>EPI · {t(en, es)}</th>" for _, en, es, _, _ in ps) + "</tr>")
    body = []
    ppa = sorted(k for k, p in plants.items() if p.get("portfolio") == "PPA")
    for k in sorted(designs) + [k for k in ppa if k not in designs]:
        nm = f'<a href="{link(k)}">{html.escape(name(k))}</a> <span class="tkey">{k}</span>'
        d = designs.get(k)
        if d is None:
            body.append(f'<tr><td>{nm}</td><td colspan="{3 + len(ps)}" class="muted">'
                        f'{t("Design pending - still measured against the contract curve and the flat expected factor.", "Diseño pendiente - se sigue midiendo contra la curva del contrato y el factor esperado fijo.")}</td></tr>')
            continue
        days = data.get(k, [])
        kwp = plants.get(k, {}).get("kwp")
        ck = {m: contract.get((k, year, m)) for m in range(1, 13)}
        dvc = DM.design_vs_contract(d, year, ck)
        ctot = sum(v for v in ck.values() if v) if all(ck.values()) else None
        cells = "".join(f"<td>{epi_cell(DM.epi(d, _operating(d, days, a, b), kwp))}</td>" for _, _, _, a, b in ps)
        body.append(f'<tr><td>{nm}</td><td class="num">{d.design_kwh_year(year) / 1000:,.1f}</td>'
                    f'<td class="num">{"" if ctot is None else f"{ctot / 1000:,.1f}"}</td>'
                    f'<td class="num">{" - " if dvc is None else f"{(dvc - 1) * 100:+,.1f}%"}</td>{cells}</tr>')
    return f'<table>{head}{"".join(body)}</table>'


def plant_card(k: str, d: DM.Design, days, plants: Dict[str, dict], today: dt.date,
               name: Callable[[str], str]) -> str:
    kwp = plants.get(k, {}).get("kwp")
    facts = [f"{html.escape(d.source)}, {d.report_date}",
             f"{d.kwp_dc:,.2f} kWp DC" + (f" / {d.kwp_ac:,.0f} kW AC" if d.kwp_ac else ""),
             f"{d.annual_kwh / 1000:,.1f} MWh/yr ({d.annual_kwh / d.kwp_dc:,.0f} kWh/kWp)"
             + (f", PR {d.pr * 100:,.1f}%" if d.pr else ""),
             html.escape(d.condition_set or ""), html.escape(d.weather or "")]
    soil = [m.soiling_pct for m in d.months]
    if all(s is not None for s in soil):
        facts.append(t("soiling by month ", "suciedad por mes ") + " ".join(f"{s:g}" for s in soil) + " %")
    facts.append(t("sensor plane: ", "plano del sensor: ") + ("GHI (horizontal)" if d.sensor == "ghi" else "POA"))
    warn = []
    if kwp and abs(kwp - d.kwp_dc) > 0.05:
        warn.append(t(f"The portal counts {kwp:,.2f} kWp, the design {d.kwp_dc:,.2f} kWp.",
                      f"El portal cuenta {kwp:,.2f} kWp, el diseño {d.kwp_dc:,.2f} kWp."))
    if d.caveat:
        warn.append(html.escape(d.caveat))
    if d.losses:
        facts.append(t("losses ", "pérdidas ") + ", ".join(f"{html.escape(n)} {v:g}%" for n, v in d.losses))
    rows = month_rows(d, days, today, kwp)
    head = ("<tr>" + "".join(f"<th>{t(en, es)}</th>" for en, es in (
        ("Month", "Mes"), ("Actual kWh", "kWh real"), ("Design kWh", "kWh diseño"), ("Of design", "Del diseño"),
        ("Sun vs design year", "Sol vs año de diseño"), ("EPI", "EPI"))) + "</tr>")
    trs = "".join(
        f'<tr><td>{r["ym"]}{" *" if r["partial"] else ""}</td><td class="num">{r["actual"]:,.0f}</td>'
        f'<td class="num">{r["design"]:,.0f}</td><td class="num">{_pct(r["of_design"], 0)}</td>'
        f'<td class="num">{_pct(r["sun"], 0)}</td><td>{epi_cell(r["epi"])}</td></tr>' for r in rows)
    note = (f'<p class="note">* {t("month to date; design prorated to the days elapsed", "mes en curso; diseño prorrateado a los días transcurridos")}</p>'
            if any(r["partial"] for r in rows) else "")
    warns = "".join(f'<p class="note" style="color:#b26a00">⚠ {w}</p>' for w in warn)
    return (f'<div class="card" id="design-{k.lower()}"><h2>{html.escape(name(k))} <span class="tkey">{k}</span></h2>'
            f'<p class="note">{" · ".join(x for x in facts if x)}</p>{warns}'
            f'<table>{head}{trs}</table>{note}</div>')


def render(designs: Dict[str, DM.Design], data, contract, plants: Dict[str, dict], today: dt.date,
           name: Callable[[str], str], link: Callable[[str], str]) -> str:
    if not designs:
        return (f'<div class="card"><p>{t("No HelioScope designs are loaded on this server yet: every plant is still measured against the contract curve and the flat expected factor.", "Aún no hay diseños HelioScope cargados en este servidor: todas las plantas se miden contra la curva del contrato y el factor esperado fijo.")}</p></div>')
    y = today - dt.timedelta(days=1)
    tiles = []
    for key, en, es, a, b in periods(today)[:2]:
        es_ = [DM.epi(d, _operating(d, data.get(k, []), a, b), plants.get(k, {}).get("kwp"))
               for k, d in designs.items()]
        act = sum(e.actual_kwh for e in es_)
        exp = sum(e.expected_kwh for e in es_)
        val = f"{act / exp * 100:,.1f}%" if exp else " - "
        tiles.append(tile(f"Fleet EPI, {en.lower()}", f"EPI de la flota, {es.lower()}", val,
                          f"{len(designs)} plants with a design", f"{len(designs)} plantas con diseño",
                          tip=(METHOD_EN, METHOD_ES)))
    inline = sum(1 for k, d in designs.items()
                 if DM.band(DM.epi(d, _operating(d, data.get(k, []), y - dt.timedelta(days=29), y),
                                   plants.get(k, {}).get("kwp")).epi) == "in line")
    tiles.append(tile("In line, last 30 days", "En línea, últimos 30 días", f"{inline} / {len(designs)}",
                      "within 3 % of the design", "dentro de 3 % del diseño"))
    cards = "".join(plant_card(k, designs[k], data.get(k, []), plants, today, name) for k in sorted(designs))
    return (f'<div class="card" style="padding:10px 14px"><p class="note" style="margin:0">'
            f'{t("Internal page: the as-connected HelioScope designs are the O&M baseline. Customer invoice annexes keep the contract curve.", "Página interna: los diseños HelioScope tal como están conectados son la línea base de O&M. Los anexos de facturación de los clientes conservan la curva del contrato.")}'
            f'</p></div>'
            f'<div class="tiles" style="margin:12px 0">{"".join(tiles)}</div>'
            f'<div class="card"><h2>{t("Each plant against its design", "Cada planta contra su diseño")}</h2>'
            f'{summary_table(designs, data, contract, plants, today, name, link)}</div>'
            f'{cards}<div class="card"><p class="note">{t(METHOD_EN, METHOD_ES)}</p></div>')
