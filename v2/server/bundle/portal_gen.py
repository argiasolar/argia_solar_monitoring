#!/usr/bin/env python3
"""portal.argia.com.mx generator (v208, parity phase).

Runs on pio06 beside the old site, never touching it: the data and the
proven helpers come from report_gen / monitoring_gen (imported — both
load PostgreSQL at import and only WRITE under __main__), the chrome
comes from portal_chrome. Pages that are not rebuilt yet land on the
old site through a redirect page, so the whole portal is navigable
from day one and every feature can be compared side by side.

    python3 /opt/argia/bundle/portal_gen.py [/www/hosting/portal.argia.com.mx/www]
"""
from __future__ import annotations

import html
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))          # monitoring_gen lives one up

OUTROOT = sys.argv[1] if len(sys.argv) > 1 else '/www/hosting/portal.argia.com.mx/www'
sys.argv = sys.argv[:1]                            # the imports must not see our argv

import portal_chrome as C                          # noqa: E402
from argia_client_logos import CLIENT_LOGOS        # noqa: E402
import report_gen as RG                            # noqa: E402  (loads PG)
import monitoring_gen as MG                        # noqa: E402  (loads PG)

t, ti, ico, tile, pill = C.t, C.ti, C.ico, C.tile, C.pill
PPA, CAPEX = RG.PPA, RG.CAPEX
LEGACY = C.LEGACY


def name(k):
    return C.display_name(RG.plants.get(k, MG.PLANTS.get(k, {})).get('customer', k))


def pn(k, size=14, block=False):
    return C.pname(k, RG.plants.get(k, MG.PLANTS.get(k, {})).get('customer', k), size, block)


def photo(k, thumb=True):
    fn = f'{k.lower()}_t.jpg' if thumb else f'{k.lower()}.jpg'
    if os.path.exists(os.path.join(OUTROOT, 'assets', 'photos', fn)):
        return f'/assets/photos/{fn}'
    return None


def logo(k, cls='clogo'):
    ent = CLIENT_LOGOS.get(k)
    return f'<img class="{cls}" src="{ent[1]}" alt="{html.escape(name(k))}">' if ent else ''


def fmt(v, d=0, dash='—'):
    return dash if v is None else f'{v:,.{d}f}'


# ---------------------------------------------------------------- fleet now
def fleet_now(keys):
    power = energy = 0.0
    online = 0
    for k in keys:
        p, e, fresh, total = MG.plant_now(k)
        power += p or 0.0
        energy += e or 0.0
        if fresh:
            online += 1
    return power, energy, online


def open_alerts():
    crit = warn = 0
    for lst in MG.ALERTS_OPEN.values():
        for a in lst:
            if str(a.get('sev', '')).upper() == 'CRITICAL':
                crit += 1
            else:
                warn += 1
    return crit, warn


# ------------------------------------------------------------------ landing
def landing():
    keys = PPA + CAPEX
    power, energy, online = fleet_now(keys)
    crit, warn = open_alerts()
    life = sum(RG.monthly_kwh.values())
    co2 = sum(v / 1000.0 * RG.co2_factor(m2[:4], k2) for (k2, m2), v in RG.monthly_kwh.items())
    rev = sum(a[2] for a in RG.atoms)
    kwp = sum(RG.plants[k].get('kwp', 0) for k in keys)
    now = MG.NOW_MX
    hour = now.hour
    greet = ('Buenos días' if hour < 12 else 'Buenas tardes' if hour < 19 else 'Buenas noches')
    # what needs attention right now, by NAME
    trouble = []
    for k in keys:
        cls, en, es = MG.semaphore(k)
        if cls in ('bad', 'warn'):
            trouble.append(f'<span class="pill {"crit" if cls == "bad" else "warn"}">{html.escape(name(k))} · {t(en, es)}</span>')
    dests = [
        ('report', 'Report', 'Reporte', '/report/', 'Overview, PPA, CAPEX, plant performance, financial, invoices.',
         'Resumen, PPA, CAPEX, desempeño por planta, financiero, facturas.', f'{RG.asof} · {sum(v for (k2, d2), v in RG.daily.items() if d2 == RG.asof) / 1000:,.1f} MWh'),
        ('monitor', 'Monitoring', 'Monitoreo', '/monitoring/', 'Live inverters, alerts, temperatures, peers — every 5 minutes.',
         'Inversores en vivo, alertas, temperaturas, pares — cada 5 minutos.', f'{online} of {len(keys)} plants online'),
        ('map', 'Map', 'Mapa', '/map/', 'The fleet on one map, status and today\'s numbers on hover.',
         'La flota en un mapa, estado y cifras de hoy al pasar el cursor.', f'{len(keys)} sites'),
        ('engine', 'Engine', 'Engine', '/engine/', 'Sizing and proposals.', 'Dimensionamiento y propuestas.', 'CFE tariffs inside'),
        ('ags', 'Golden Standard', 'Golden Standard', '/ags/', 'The ARGIA build and O&M standard, one reference.',
         'El estándar ARGIA de construcción y O&M, una referencia.', 'reference'),
        ('setup', 'Setup', 'Configuración', '/setup/', 'You, users, plants, finance, CFE & tariffs, system.',
         'Tú, usuarios, plantas, finanzas, CFE y tarifas, sistema.', 'admin'),
    ]
    cards = ''.join(f'''
   <a href="{path}" class="card{" adminonly" if key == "setup" else ""}" style="padding:20px 22px 16px;display:flex;flex-direction:column;gap:10px;color:var(--ink);min-height:150px">
    <div style="display:flex;align-items:center;justify-content:space-between"><span style="width:40px;height:40px;border-radius:10px;background:#e6f7f5;display:flex;align-items:center;justify-content:center">{ico(key if key != "monitor" else "monitor", 22, "#05847d", 1.9)}</span><span style="color:#b6bec8">{ico("arrow", 18)}</span></div>
    <div style="display:flex;align-items:baseline;gap:8px"><span style="font-weight:700;font-size:18px">{t(en, es)}</span><span class="mono muted">{path.rstrip("/")}</span></div>
    <div style="font-size:13px;color:var(--ink2)">{t(ben, bes)}</div>
    <div style="flex:1"></div><span class="pill ok" style="align-self:flex-start">{html.escape(fact)}</span>
   </a>''' for key, en, es, path, ben, bes, fact in dests)
    logos = ''.join(
        f'<a href="/report/{C.slug(k)}/" class="card pcard" style="padding:14px 16px;display:flex;flex-direction:column;gap:8px;color:var(--ink);min-width:0" title="{html.escape(name(k))} · {RG.plants[k]["kwp"]:,.0f} kWp · {RG.plants[k]["portfolio"]}">'
        f'{logo(k)}<span style="font-weight:700;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{html.escape(name(k))}</span>'
        f'<span class="muted" style="font-size:11px">{RG.plants[k]["kwp"]:,.0f} kWp · {RG.plants[k]["portfolio"]}</span></a>'
        for k in keys if k in RG.plants)
    body = f'''
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex-wrap:wrap">
 <div style="display:flex;flex-direction:column;gap:6px">
  <div class="kicker">{now.strftime("%A, %d %B %Y · %H:%M")} MX</div>
  <h1 class="pt" style="font-size:34px">{greet}.</h1>
  <div class="muted" style="font-size:15px">{t("Fleet", "Flota")}: {fmt(power)} kW · {online}/{len(keys)} {t("plants online", "plantas en línea")}</div>
 </div>
 <div style="display:flex;gap:10px;flex-wrap:wrap">{"".join(trouble)}</div>
</div>
<div class="grid g5" style="margin-top:26px">
 {tile("Fleet now", "Flota ahora", f"{fmt(power)} <span class=unit>kW</span>", f"of {kwp:,.0f} kWp · {now.strftime('%H:%M')} MX", f"de {kwp:,.0f} kWp · {now.strftime('%H:%M')} MX")}
 {tile("Today so far", "Hoy hasta ahora", f"{fmt(energy / 1000, 1)} <span class=unit>MWh</span>", "interval telemetry, all plants", "telemetría, todas las plantas")}
 {tile("Clean energy, lifetime", "Energía limpia, acumulada", f"{life / 1e6:,.2f} <span class=unit>GWh</span>", f"CO₂ avoided {co2:,.0f} t", f"CO₂ evitado {co2:,.0f} t")}
 {tile("Revenue generated", "Ingreso generado", f"$ {rev / 1e6:,.1f} <span class=unit>M MXN</span>", "PPA + LaaS · accrued", "PPA + LaaS · devengado")}
 {tile("Open alerts", "Alertas abiertas", f"{crit + warn}", f"{crit} critical · {warn} warnings", f"{crit} críticas · {warn} avisos", tone=("bad" if crit else "warn" if warn else ""))}
</div>
<div style="margin-top:26px;display:flex;flex-direction:column;gap:12px">
 <div class="kicker">{t("Where to", "A dónde")}</div>
 <div class="grid g3" style="gap:16px">{cards}</div>
</div>
<div style="margin-top:26px;display:flex;flex-direction:column;gap:12px">
 <div style="display:flex;align-items:baseline;gap:12px"><span class="kicker">{t("Your plants", "Tus plantas")}</span><span class="muted" style="font-size:12px">{t("logos are grey until you hover — each opens the plant report", "los logos están en gris hasta pasar el cursor — cada uno abre el reporte de la planta")}</span></div>
 <div class="grid" style="grid-template-columns:repeat(6,minmax(0,1fr));gap:10px">{logos}</div>
</div>
<div class="askbar askonly" style="margin-top:26px">
 <span style="width:36px;height:36px;border-radius:9px;background:var(--teal);display:flex;align-items:center;justify-content:center">{ico("ask", 20, "#053b38", 2.2)}</span>
 <span style="flex:1;font-size:14px">{t("Ask ARGIA anything about the fleet", "Pregunta a ARGIA lo que quieras sobre la flota")} — <b style="color:#fff">"{t("Why did Taigene produce less yesterday?", "¿Por qué Taigene produjo menos ayer?")}"</b></span>
 <a class="askin" href="/ask/">{t("Ask a question…", "Haz una pregunta…")}<span style="flex:1"></span><span class="mono">Ctrl K</span></a>
</div>
<footer class="pf mono muted" style="padding:24px 0 0"><span>ARGIA · Zapopan, MX · {RG.gen_at}</span><a href="{LEGACY}/" class="legacy">{ico("ext", 12)} {t("old site (until the switch)", "sitio anterior (hasta el cambio)")}</a></footer>'''
    return C.page('Portal', body, None, refresh=300)


# ------------------------------------------------------------- report pages
def plant_rows(keys, day):
    out = []
    for k in keys:
        rows = {d: (e, x) for d, e, x in MG.DAILY.get(k, [])}
        e, x = rows.get(day, (None, None))
        pr = MG.PERF.get(k, {}).get('pr')
        cls, en, es = MG.semaphore(k)
        pct = (100 * e / x) if (e is not None and x) else None
        out.append((k, e, x, pct, pr, cls, en, es))
    return out


def plant_table(keys, day):
    trs = ''
    for k, e, x, pct, pr, cls, en, es in plant_rows(keys, day):
        pf = RG.plants[k]['portfolio']
        trs += (f'<tr><td><a href="/report/{C.slug(k)}/" style="display:flex;align-items:center;gap:12px;color:var(--ink)">{logo(k)}{pn(k, 13.5, True)}</a></td>'
                f'<td><span class="pill {"ok" if pf == "PPA" else "off"}">{pf}</span></td><td class="muted">{html.escape(C.location_of(RG.plants[k]["customer"]))}</td>'
                f'<td class="r">{fmt(e)}</td><td class="r muted">{fmt(x)}</td><td class="r">{fmt(pct) + "%" if pct is not None else "—"}</td>'
                f'<td class="r">{fmt(pr, 2)}</td><td><span class="pill {"ok" if cls == "good" else "crit" if cls == "bad" else cls}">{t(en, es)}</span></td></tr>')
    return f'''<div class="card" style="overflow:hidden">
 <div class="chead"><h2 class="ct">{t("Plant performance", "Desempeño por planta")} · {day}</h2><span class="muted" style="font-size:12.5px">{t("energy from inverter counters; vendor daily only where higher", "energía de contadores de inversor; diario del proveedor sólo si es mayor")}</span></div>
 <div style="overflow-x:auto"><table><thead><tr><th>{t("Plant", "Planta")}</th><th>{t("Portfolio", "Portafolio")}</th><th>{t("Location", "Ubicación")}</th><th class="r">kWh</th><th class="r">{t("Expected", "Esperado")}</th><th class="r">% plan</th><th class="r">PR 30 d</th><th>{t("Status", "Estado")}</th></tr></thead><tbody>{trs}</tbody></table></div>
</div>'''


def report_overview(keys=None, on='', title_en='Fleet overview', title_es='Resumen de la flota'):
    keys = keys or (PPA + CAPEX)
    life = sum(v for (k2, m2), v in RG.monthly_kwh.items() if k2 in keys)
    co2 = sum(v / 1000.0 * RG.co2_factor(m2[:4], k2) for (k2, m2), v in RG.monthly_kwh.items() if k2 in keys)
    rev = sum(a[2] for a in RG.atoms if a[1] in keys or on == '')
    kwp = sum(RG.plants[k]['kwp'] for k in keys)
    this_m = RG.asof[:7]
    mtd = sum(v for (k2, m2), v in RG.monthly_kwh.items() if m2 == this_m and k2 in keys)
    y12, yfl, cur_exp = RG.year_months_with_flags(list(keys))
    n_ppa = sum(1 for k in keys if RG.plants[k]['portfolio'] == 'PPA')
    body = f'''
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap">
 <div style="display:flex;flex-direction:column;gap:4px"><div class="kicker">{len(keys)} {t("plants", "plantas")} · {t("data", "datos")} {RG.first} → {RG.asof} · {RG.gen_at}</div><h1 class="pt">{t(title_en, title_es)}</h1></div>
 <button class="btn2 noprint" onclick="window.print()">{ico("print", 15)} {t("PDF", "PDF")}</button>
</div>
<div class="grid g5" style="margin-top:20px">
 {tile("Clean energy generated", "Energía limpia generada", f"{life / 1e6:,.2f} <span class=unit>GWh</span>", f"{RG.first} → {RG.asof}", f"{RG.first} → {RG.asof}", tip=("Sum of daily production, inverter counters first (vendor daily only where higher).", "Suma de la producción diaria, contadores de inversor primero (diario del proveedor sólo si es mayor)."))}
 {tile("CO₂ avoided", "CO₂ evitado", f"{co2:,.0f} <span class=unit>t</span>", f"grid factor by year · {RG.CO2_T_PER_MWH} t/MWh current", f"factor de red por año · {RG.CO2_T_PER_MWH} t/MWh actual", tip=("kWh × the national grid factor of that year (a contracted plant factor wins).", "kWh × el factor de red nacional de ese año (gana el factor contratado de la planta)."))}
 {tile("Revenue generated", "Ingreso generado", f"$ {rev / 1e6:,.1f} <span class=unit>M MXN</span>", "PPA + LaaS · accrued", "PPA + LaaS · devengado", tip=("Energy × the plant tariff, plus LaaS fees, accrued by day.", "Energía × tarifa de la planta, más cuotas LaaS, devengado por día."))}
 {tile("Fleet capacity", "Capacidad instalada", f"{kwp / 1000:,.2f} <span class=unit>MWp</span>", f"{n_ppa} PPA · {len(keys) - n_ppa} CAPEX", f"{n_ppa} PPA · {len(keys) - n_ppa} CAPEX")}
 {tile("This month", "Este mes", f"{mtd / 1000:,.0f} <span class=unit>MWh</span>", f"{this_m} → {RG.asof[8:]}", f"{this_m} → {RG.asof[8:]}")}
</div>
<div class="card" style="margin-top:16px;padding:18px 20px;display:flex;flex-direction:column;gap:10px">
 <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap"><h2 class="ct">{t("Monthly production", "Producción mensual")} · {RG.asof[:4]}</h2><span class="muted" style="font-size:12px">MWh · {t("grey = expected (contract / prior year); current month: actual over expected · hover a bar", "gris = esperado (contrato / año anterior); mes en curso: real sobre esperado · pasa el cursor por una barra")}</span></div>
 <div style="overflow-x:auto">{RG.columns_svg(y12, "MWh", scale=1000.0, show_values=True, month_names=True, flags=yfl, cur_expected=cur_exp)}</div>
</div>
<div style="margin-top:16px">{plant_table(keys, RG.asof)}</div>
<div style="margin-top:16px" class="muted">{C.legacy_link("/", "compare with the old landing page", "comparar con la página anterior")}</div>'''
    return C.page('Report', body, 'report', on)


def plant_cards():
    cards = ''
    for k in PPA + CAPEX:
        p = RG.plants[k]
        cls, en, es = MG.semaphore(k)
        pr = MG.PERF.get(k, {}).get('pr')
        cards += f'''
 <a href="/report/{C.slug(k)}/" class="card pcard" style="padding:18px 20px;display:flex;flex-direction:column;gap:8px;color:var(--ink)">
  <div style="display:flex;align-items:center;justify-content:space-between">{logo(k)}<span class="pill {"ok" if cls == "good" else "crit" if cls == "bad" else cls}">{t(en, es)}</span></div>
  <div>{pn(k, 17, True)}</div>
  <div class="muted" style="font-size:12.5px">{p["kwp"]:,.0f} kWp · {html.escape(C.location_of(p["customer"]))} · {p["portfolio"]}</div>
  <div style="display:flex;gap:14px;font-size:12.5px"><span><b>PR 30 d</b> {fmt(pr, 2)}</span><span><b>{t("today", "hoy")}</b> {fmt(MG.plant_now(k)[1])} kWh</span></div>
 </a>'''
    body = f'''<div style="display:flex;flex-direction:column;gap:4px"><div class="kicker">{RG.asof} · {t("each card opens the plant report", "cada tarjeta abre el reporte de la planta")}</div><h1 class="pt">{t("Plant performance", "Desempeño por planta")}</h1></div>
<div class="grid g3" style="margin-top:20px;gap:16px">{cards}</div>
<div style="margin-top:16px" class="muted">{t("Plant pages are still served by the old site in this phase — the link opens them there.", "Las páginas por planta todavía las sirve el sitio anterior en esta fase — el enlace las abre ahí.")}</div>'''
    return C.page('Plant performance', body, 'report', 'plants')


# --------------------------------------------------------- monitoring pages
def mon_tile(k):
    cls, en, es = MG.semaphore(k)
    power, etoday, fresh, total = MG.plant_now(k)
    p = MG.PLANTS[k]
    ph = photo(k)
    media = (f'<img class="tphoto" src="{ph}" alt="" loading="lazy">' if ph else
             f'<div style="height:96px;border-radius:8px;background:#f7f9fb;border:1px dashed var(--line2);display:flex;align-items:center;justify-content:center">{logo(k)}</div>')
    bars = ''.join(f'<span style="height:6px;flex:1;border-radius:3px;background:{"#05b1a9" if i < fresh else "#d2d7dd"}"></span>' for i in range(total or 1))
    dot = {'good': '#05b1a9', 'warn': '#f0a83b', 'bad': '#c2554e'}.get(cls, '#b6bec8')
    glow = f'box-shadow:0 0 0 2px {dot}33;' if cls in ('warn', 'bad') else ''
    maint = f'<span class="pill off">{t("maintenance logged", "mantenimiento registrado")}</span>' if MG.MAINT_TODAY.get(k) else ''
    return f'''
 <a href="/monitoring/{C.slug(k)}/" class="card pcard" style="padding:14px 16px;display:flex;flex-direction:column;gap:10px;color:var(--ink);{glow}">
  {media}
  <div style="display:flex;align-items:center;justify-content:space-between;gap:8px"><span style="display:flex;align-items:center;gap:8px"><span style="width:9px;height:9px;border-radius:50%;background:{dot}"></span>{pn(k, 15)}</span><span class="pill {"ok" if cls == "good" else "crit" if cls == "bad" else cls}">{t(en, es)}</span></div>
  <div class="muted" style="font-size:12.5px">{html.escape(C.location_of(p["customer"]))} · {p["kwp"]:,.0f} kWp {maint}</div>
  <div style="display:flex;align-items:flex-end;justify-content:space-between"><span><span class="tlabel">{t("Power now", "Potencia")}</span><span class="tval" style="font-size:22px">{MG.fmt1(power)} kW</span></span><span style="text-align:right"><span class="tlabel">{t("Today", "Hoy")}</span><b style="font-size:16px">{MG.fmt_kwh(etoday)} kWh</b></span><span class="tlabel" style="gap:4px">{fresh}/{total} {t("live", "en vivo")}</span></div>
  <div style="display:flex;gap:4px">{bars}</div>
 </a>'''


def kpi_row(keys, label_en, label_es):
    power, energy, online = fleet_now(keys)
    w_pr = kwp_pr = w_av = kwp_av = 0.0
    for k in keys:
        pf = MG.PERF.get(k, {})
        kwp = MG.PLANTS[k]['kwp']
        if pf.get('pr') is not None:
            w_pr += pf['pr'] * kwp; kwp_pr += kwp
        if pf.get('avail') is not None:
            w_av += pf['avail'] * kwp; kwp_av += kwp
    pr = w_pr / kwp_pr if kwp_pr else None
    av = w_av / kwp_av if kwp_av else None
    prt = '' if pr is None else ('good' if pr >= 0.75 else 'warn' if pr >= 0.65 else 'bad')
    avt = '' if av is None else ('good' if av >= 0.98 else 'warn' if av >= 0.95 else 'bad')
    mtd_p = sum(MG.MTD.get(k, {}).get('prod') or 0 for k in keys)
    mtd_e = sum(MG.MTD.get(k, {}).get('exp') or 0 for k in keys)
    pct = 100 * mtd_p / mtd_e if mtd_e else None
    return f'''<div class="grid g5" style="gap:12px">
 {tile("Power now", "Potencia ahora", f"{fmt(power)} <span class=unit>kW</span>", f"{online}/{len(keys)} {label_en} plants online", f"{online}/{len(keys)} plantas {label_es} en línea")}
 {tile("Energy today", "Energía hoy", f"{fmt(energy / 1000, 1)} <span class=unit>MWh</span>", "interval telemetry", "telemetría")}
 {tile("Performance · PR 30 d", "Desempeño · PR 30 d", fmt(pr, 3), "kWp-weighted · ≥0.75 green · ≥0.65 amber", "ponderado por kWp · ≥0.75 verde · ≥0.65 ámbar", tone=prt)}
 {tile("Availability · 30 d", "Disponibilidad · 30 d", (fmt(100 * av, 1) + "%") if av is not None else "—", "≥98% green · ≥95% amber (IEC 63019)", "≥98% verde · ≥95% ámbar (IEC 63019)", tone=avt)}
 {tile("Month to date", "Mes a la fecha", (fmt(pct) + "%") if pct is not None else "—", f"{fmt(mtd_p / 1000, 1)} of {fmt(mtd_e / 1000, 1)} MWh expected", f"{fmt(mtd_p / 1000, 1)} de {fmt(mtd_e / 1000, 1)} MWh esperados", tone=("" if pct is None else "good" if pct >= 90 else "warn" if pct >= 70 else "bad"))}
</div>'''


def monitoring_overview(which=''):
    groups = [('PPA', PPA), ('CAPEX', CAPEX)] if not which else [(which.upper(), PPA if which == 'ppa' else CAPEX)]
    power, energy, online = fleet_now(PPA + CAPEX)
    crit, warn = open_alerts()
    secs = ''.join(f'''
<div style="margin-top:22px;display:flex;flex-direction:column;gap:12px">
 <div class="kicker">{g} {t("plants", "plantas")}</div>
 {kpi_row(ks, g, g)}
 <div class="grid g3">{"".join(mon_tile(k) for k in ks)}</div>
</div>''' for g, ks in groups)
    body = f'''
<div style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap">
 <div style="display:flex;flex-direction:column;gap:4px"><div class="kicker">{t("Live", "En vivo")} · {MG.NOW_MX.strftime("%H:%M")} MX · {t("refreshes every 5 min", "se actualiza cada 5 min")}</div><h1 class="pt">{t("Fleet now", "Flota ahora")} — {fmt(power)} kW</h1></div>
 <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="pill crit">{ico("bell", 14, "currentColor", 2.2)} {crit} {t("critical", "críticas")}</span><span class="pill warn">{warn} {t("warnings", "avisos")}</span>{C.legacy_link("/monitoring/", "old monitoring", "monitoreo anterior")}</div>
</div>
{secs}'''
    return C.page('Monitoring', body, 'monitoring', which, refresh=300)


# --------------------------------------------------------------------- io
def write(rel, content):
    p = os.path.join(OUTROOT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write(content)
    os.chmod(p, 0o644)


def main():
    n = 0
    write('index.html', landing()); n += 1
    write('logged-out.html', RG.logged_out_page()); n += 1
    write('no-access.html', RG.no_access_page()); n += 1
    write('report/index.html', report_overview()); n += 1
    write('report/ppa/index.html', report_overview(PPA, 'ppa', 'PPA plants', 'Plantas PPA')); n += 1
    write('report/capex/index.html', report_overview(CAPEX, 'capex', 'CAPEX plants', 'Plantas CAPEX')); n += 1
    write('report/plants/index.html', plant_cards()); n += 1
    write('monitoring/index.html', monitoring_overview()); n += 1
    write('monitoring/ppa/index.html', monitoring_overview('ppa')); n += 1
    write('monitoring/capex/index.html', monitoring_overview('capex')); n += 1
    # parity phase: not-yet-rebuilt destinations land on the old site
    legacy = {
        'report/financial/index.html': ('/financial/', 'Financial report — old site', 'Reporte financiero — sitio anterior'),
        'report/invoices/index.html': ('/invoices/', 'Invoice annexes — old site', 'Anexos — sitio anterior'),
        'map/index.html': ('/portfolio/', 'Portfolio map — old site', 'Mapa — sitio anterior'),
        'setup/index.html': ('/account/', 'Your account — old site', 'Tu cuenta — sitio anterior'),
        'setup/users/index.html': ('/setup/people/', 'Users — old site', 'Usuarios — sitio anterior'),
        'setup/plants/index.html': ('/setup/plants/', 'Plants — old site', 'Plantas — sitio anterior'),
        'setup/finance/index.html': ('/setup/finance/', 'Finance — old site', 'Finanzas — sitio anterior'),
        'setup/cfe/index.html': ('/setup/cfe/', 'CFE & tariffs — old site', 'CFE y tarifas — sitio anterior'),
        'setup/system/index.html': ('/setup/system/', 'System — old site', 'Sistema — sitio anterior'),
        'ask/index.html': ('/ask/', 'Ask ARGIA — old site', 'Pregunta a ARGIA — sitio anterior'),
    }
    for rel, (path, en, es) in legacy.items():
        write(rel, C.redirect_page(LEGACY + path, en, es)); n += 1
    for k in PPA + CAPEX:
        write(f'report/{C.slug(k)}/index.html', C.redirect_page(f'{LEGACY}/{k.lower()}/', f'{name(k)} — old site', f'{name(k)} — sitio anterior')); n += 1
        write(f'monitoring/{C.slug(k)}/index.html', C.redirect_page(f'{LEGACY}/monitoring/{k.lower()}/', f'{name(k)} — old site', f'{name(k)} — sitio anterior')); n += 1
    write('engine/index.html', C.redirect_page('https://engine.sprinkler.agency/', 'Engine', 'Engine')); n += 1
    write('ags/index.html', C.redirect_page('https://sprinkler.agency/', 'Golden Standard', 'Golden Standard')); n += 1
    print(f'portal_gen: wrote {n} pages under {OUTROOT}')


if __name__ == '__main__':
    main()
