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
for _cand in (os.path.join(os.path.dirname(os.path.dirname(HERE)), 'scripts'),
              '/root/argia_v2/v2/scripts'):               # invoice_publish (repo checkout on pio06)
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)

OUTROOT = sys.argv[1] if len(sys.argv) > 1 else '/www/hosting/portal.argia.com.mx/www'
sys.argv = sys.argv[:1]                            # the imports must not see our argv

import portal_chrome as C                          # noqa: E402
from argia_client_logos import CLIENT_LOGOS        # noqa: E402
import report_gen as RG                            # noqa: E402  (loads PG)
import monitoring_gen as MG                        # noqa: E402  (loads PG)

t, ti, ico, tile, pill = C.t, C.ti, C.ico, C.tile, C.pill
PPA, CAPEX = RG.PPA, RG.CAPEX


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
    """The portal front door (v209): who you are, where to go — and NO
    fleet data. Designers, sales and office staff land here too."""
    now = MG.NOW_MX
    h = now.hour
    g_en, g_es = (('Good morning', 'Buenos días') if h < 12 else
                  ('Good afternoon', 'Buenas tardes') if h < 19 else ('Good evening', 'Buenas noches'))
    dests = [
        ('report', 'Report', 'Reporte', '/report/', 'Fleet overview, PPA, CAPEX, plant performance, financial.',
         'Resumen de flota, PPA, CAPEX, desempeño por planta, financiero.', ''),
        ('monitor', 'Monitoring', 'Monitoreo', '/monitoring/', 'Live inverters, alerts, temperatures, peers.',
         'Inversores en vivo, alertas, temperaturas, pares.', ''),
        ('map', 'Map', 'Mapa', '/map/', 'The fleet on one map, status and today\'s numbers.',
         'La flota en un mapa, estado y cifras de hoy.', ''),
        ('maint', 'Maintenance', 'Mantenimiento', '/maintenance/', 'Tickets: what ARGIA is doing about each issue.',
         'Tickets: qué hace ARGIA con cada problema.', ''),
        ('engine', 'Engine', 'Engine', '/engine/', 'Sizing and proposals.', 'Dimensionamiento y propuestas.', 'engine.sprinkler.agency'),
        ('ags', 'ARGIA Golden Standard', 'ARGIA Golden Standard', '/ags/', 'The ARGIA design, build and O&M standard.',
         'El estándar ARGIA de diseño, construcción y O&M.', 'sprinkler.agency'),
        ('setup', 'Setup', 'Configuración', '/setup/', 'People, plants, finance, CFE & tariffs, system.',
         'Personas, plantas, finanzas, CFE y tarifas, sistema.', ''),
    ]
    cards = ''.join(f'''
   <a href="{path}" id="tile-{key}" class="card dest" style="padding:22px 24px 18px;display:flex;flex-direction:column;gap:10px;color:var(--ink);min-height:160px">
    <div style="display:flex;align-items:center;justify-content:space-between"><span style="width:44px;height:44px;border-radius:11px;background:#e6f7f5;display:flex;align-items:center;justify-content:center">{ico(key, 24, "#05847d", 1.9)}</span><span style="color:#b6bec8">{ico("arrow", 18)}</span></div>
    <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap"><span style="font-weight:800;font-size:19px;color:var(--deep)">{t(en, es)}</span><span class="mono muted">{html.escape(ext) if ext else path.rstrip("/")}</span></div>
    <div class="tblurb" style="font-size:13.5px;color:var(--ink2)" data-en="{html.escape(ben, quote=True)}" data-es="{html.escape(bes, quote=True)}">{html.escape(ben)}</div>
   </a>''' for key, en, es, path, ben, bes, ext in dests)
    body = f'''
<div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
 <div class="kicker">{now.strftime("%A, %d %B %Y")} · {now.strftime("%H:%M")} MX</div>
 <h1 class="pt" style="font-size:34px">{t(g_en, g_es)}<span id="gname"></span>.</h1>
 <div class="muted" style="font-size:15px">{t("Where would you like to go?", "¿A dónde quieres ir?")}</div>
</div>
<div class="grid g3" style="margin-top:28px;gap:16px">{cards}</div>
<a class="askbar askonly" href="/ask/" style="margin-top:28px;color:#e6f7f5">
 <span style="width:36px;height:36px;border-radius:9px;background:var(--teal);display:flex;align-items:center;justify-content:center;flex:0 0 36px">{ico("ask", 20, "#053b38", 2.2)}</span>
 <span style="flex:1;font-size:14px">{t("Ask ARGIA anything about the fleet", "Pregunta a ARGIA lo que quieras sobre la flota")} — <b style="color:#fff">"{t("Why did Taigene produce less yesterday?", "¿Por qué Taigene produjo menos ayer?")}"</b></span>
 <span class="askin">{t("Ask a question…", "Haz una pregunta…")}<span style="flex:1"></span><span class="mono">Ctrl K</span></span>
</a>
<footer class="pf mono muted" style="padding:32px 0 0"><span>ARGIA · Zapopan, MX</span></footer>'''
    return C.page('Portal', body, None)


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
    te = tx = 0.0
    w_pr = kwp_pr = 0.0
    for k, e, x, pct, pr, cls, en, es in plant_rows(keys, day):
        te += e or 0.0
        tx += x or 0.0
        if pr is not None:
            w_pr += pr * RG.plants[k]['kwp']; kwp_pr += RG.plants[k]['kwp']
        pf = RG.plants[k]['portfolio']
        trs += (f'<tr><td><a href="/report/{C.slug(k)}/" class="lcell"><span class="lbox">{logo(k)}</span>{pn(k, 13.5, True)}</a></td>'
                f'<td><span class="pill {"ok" if pf == "PPA" else "off"}">{pf}</span></td><td class="muted">{html.escape(C.location_of(RG.plants[k]["customer"]))}</td>'
                f'<td class="r">{fmt(e)}</td><td class="r muted">{fmt(x)}</td><td class="r">{fmt(pct) + "%" if pct is not None else "—"}</td>'
                f'<td class="r">{fmt(pr, 2)}</td><td><span class="pill {"ok" if cls == "good" else "crit" if cls == "bad" else cls}">{t(en, es)}</span></td></tr>')
    tpct = (100 * te / tx) if tx else None
    trs += (f'<tr class="total"><td><b>{t("TOTAL", "TOTAL")}</b></td><td></td><td class="muted">{len(keys)} {t("plants", "plantas")}</td>'
            f'<td class="r"><b>{fmt(te)}</b></td><td class="r muted">{fmt(tx)}</td><td class="r"><b>{fmt(tpct) + "%" if tpct is not None else "—"}</b></td>'
            f'<td class="r"><b>{fmt(w_pr / kwp_pr, 2) if kwp_pr else "—"}</b></td><td class="muted" style="font-size:12px">{t("PR kWp-weighted", "PR ponderado por kWp")}</td></tr>')
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
<div style="margin-top:16px">{plant_table(keys, RG.asof)}</div>'''
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



# ------------------------------------------------------------ plant report
def plant_report(k):
    """The plant page on the new chrome — same data, same tiles, same
    range engine as report_gen.plant_page (one implementation: plant_parts)."""
    p = RG.plants[k]
    parts = RG.plant_parts(k)
    pf = p['portfolio']
    head = f'''
<div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap">
 <div class="card" style="width:64px;height:64px;display:flex;align-items:center;justify-content:center;padding:8px">{logo(k, "clogo color")}</div>
 <div style="display:flex;flex-direction:column;gap:2px">
  <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"><h1 class="pt">{html.escape(name(k))}</h1><span class="pill {"ok" if pf == "PPA" else "off"}">{pf}</span></div>
  <div class="muted">{html.escape(C.location_of(p["customer"]))} · {p["kwp"]:,.1f} kWp DC · {html.escape(str(p.get("brand") or ""))} · {t("data", "datos")} → {parts["last_seen"]} · <span class="mono">{k}</span></div>
 </div>
 <div style="flex:1"></div>
 <a class="btn2" href="/monitoring/{C.slug(k)}/">{ico("monitor", 15)} {t("Live monitoring", "Monitoreo en vivo")}</a>
 <button class="btn2 noprint" onclick="window.print()">{ico("print", 15)} {t("PDF · current selection", "PDF · selección actual")}</button>
</div>'''
    # the range bar's own "Live monitoring" button would duplicate the header's
    controls = parts['controls'].replace(f'<a class="btn live" href="/monitoring/{k.lower()}/">', '<a class="btn live" style="display:none" href="#">')
    body = head + controls + parts['tiles'] + parts['warn'] + ''.join(parts['body']) + parts['footer']
    return C.page(name(k), body, 'report', 'plants')

# ------------------------------------------------------------- financial
def financial_report():
    """report_gen.financial_body under the portal chrome — same tiles,
    tables, range engine and one-A4 print rule."""
    head = f'''
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap">
 <div style="display:flex;flex-direction:column;gap:4px">
  <div class="kicker">PPA + LaaS · {t("generated", "generado")} {RG.gen_at} · {t("actuals through", "reales hasta")} {RG.asof}</div>
  <div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap"><h1 class="pt">{t("Financial report", "Reporte financiero")}</h1><span class="rng mono muted" id="hdr_range"></span></div>
 </div>
 <button class="btn2 noprint" onclick="window.print()">{ico("print", 15)} {t("PDF · current selection", "PDF · selección actual")}</button>
</div>'''
    body = RG.financial_body().replace('href="/invoices/"', 'href="/report/invoices/"')
    return C.page('Financial report', head + body, 'report', 'financial')


# -------------------------------------------------------------- invoices
OLD_INVOICES_DIR = '/www/hosting/portal.argia.com.mx/www/invoices'    # v214: a real directory in the portal root


def invoice_records():
    """{ym: {factura_name: (kwh, mxn, status)}} from the invoicing register
    (same SQL as scripts/invoice_publish.all_records, through RG.q so it
    does not depend on the job env)."""
    from invoice_publish import FACTURA_CLIENT
    by_pk = {v[0]: k for k, v in FACTURA_CLIENT.items()}
    out = {}
    for r in RG.q("SELECT plant_key, to_char(ref_month, 'YYYY-MM'), billable_kwh, amount_mxn,"
                  " check_status FROM invoicing WHERE billable_kwh IS NOT NULL;"):
        if len(r) < 5 or r[0] not in by_pk:
            continue
        out.setdefault(r[1], {})[by_pk[r[0]]] = (
            float(r[2]) if r[2] else None, float(r[3]) if r[3] else None, r[4])
    return out


def invoices_page():
    """The invoice annexes index on the portal chrome. The files stay
    where the monthly job writes them; /invoices/ on the portal host is
    a symlink to that folder (deploy step), so the links are absolute."""
    import invoice_publish as IP
    months = IP.scan_months(OLD_INVOICES_DIR)
    body = IP.index_body(months, records=invoice_records(), zips=IP.month_zips(OLD_INVOICES_DIR),
                         base='/invoices/')
    head = f'''
<div style="display:flex;flex-direction:column;gap:4px">
 <div class="kicker">{t("One annex per plant and closed month · the PDF is always in Spanish · new months appear on the 1st after the reconciliation close", "Un anexo por planta y mes cerrado · el PDF siempre en español · cada mes nuevo aparece el día 1 tras el cierre de conciliación")}</div>
 <h1 class="pt">{t("Invoice annexes", "Anexos de facturación")}</h1>
</div>'''
    return C.page('Invoice annexes', head + f'<div class="invbody">{body}</div>', 'report', 'invoices')


# ------------------------------------------------------------------- map
def map_page():
    """monitoring_gen.portfolio_page(skin='portal'): tiles, Leaflet map,
    PVOUT overlay, hover cards, per-plant checkboxes — under the portal
    chrome at /map/. Assets (/portfolio/assets, /monitoring/assets) are
    symlinked into the portal root by the deploy step."""
    head = (f'<div style="display:flex;flex-direction:column;gap:4px;margin-bottom:10px"><div class="kicker">'
            f'{t("Every plant, live · circle area = installed kWp · money is an accrual estimate, sin IVA", "Cada planta, en vivo · área del círculo = kWp instalados · el dinero es una estimación devengada, sin IVA")}'
            f'</div><h1 class="pt">{t("Map", "Mapa")}</h1></div>')
    return C.page('Map', head + MG.portfolio_page(skin='portal'), 'map', refresh=300)


# ------------------------------------------------------ live plant page
def monitoring_plant(k, d):
    """monitoring_gen.plant_page(skin='portal') under the portal chrome:
    photo, banners, KPIs, intraday chart, inverter table (vendor-portal
    links), open alerts, last 7 days, daily reconciliation. Links inside
    the body use the plant code; the portal speaks slugs."""
    parts = MG.plant_page(k, d, skin='portal')
    p = MG.PLANTS[k]
    live = parts['live']
    code_path, slug_path = f'/monitoring/{k.lower()}/', f'/monitoring/{C.slug(k)}/'
    body = parts['body'].replace(code_path, slug_path)
    picker = parts['picker'].replace(code_path, slug_path)
    buttons = parts['buttons'].replace(code_path, slug_path)
    head = f'''
<div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:12px">
 <div style="display:flex;flex-direction:column;gap:2px">
  <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"><h1 class="pt">{html.escape(name(k))}</h1><span class="pill {"ok" if p.get("portfolio") == "PPA" else "off"}">{html.escape(str(p.get("portfolio") or ""))}</span><span class="pill {"ok" if live else "off"}">{t("live", "en vivo") if live else t("archived day", "día archivado")}</span></div>
  <div class="muted">{html.escape(C.location_of(p["customer"]))} · {p["kwp"]:,.1f} kWp · {html.escape(str(p.get("brand") or ""))} · {d} · <span class="mono">{k}</span></div>
 </div>
 <div style="flex:1"></div>
 <div class="monctl" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">{picker}{buttons}<a class="btn" href="/report/{C.slug(k)}/">{ico("report", 15, "#053b38", 2)} {t("Open report", "Abrir reporte")}</a></div>
</div>'''
    extra = '<style>' + C.scoped_css(MG.STYLE, '.monbody') + C.skin_reset('.monbody') + MON_OVERRIDES + '</style>'
    return C.page(name(k), head + f'<div class="monbody">{body}</div>', 'monitoring', '',
                  refresh=(300 if live else 0), extra_head=extra)


def mon_wrap(title_en, title_es, kicker_en, kicker_es, body, on, buttons=''):
    """A monitoring_gen page body (skin='portal') under the portal chrome."""
    head = (f'<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:12px">'
            f'<div style="display:flex;flex-direction:column;gap:4px"><div class="kicker">{t(kicker_en, kicker_es)}</div>'
            f'<h1 class="pt">{t(title_en, title_es)}</h1></div>{buttons}</div>')
    extra = '<style>' + C.scoped_css(MG.STYLE, '.monbody') + C.skin_reset('.monbody') + MON_OVERRIDES + '</style>'
    return C.page(title_en, head + f'<div class="monbody">{body}</div>', 'monitoring', on,
                  refresh=300, extra_head=extra)


def monitoring_performance():
    """/monitoring/performance/ — 30-day PR, PR_STC, availability,
    production vs expected, fleet totals (v213: was only on the old site)."""
    return mon_wrap('Performance', 'Desempeño',
                    '30-day PR · availability · production vs expected',
                    'PR 30 días · disponibilidad · producción vs esperado',
                    MG.performance_page(skin='portal'), 'performance')


def monitoring_recon():
    """/monitoring/recon/ — monthly close (the invoice gate) and the
    daily reconciliation of every plant (v213: was only on the old site)."""
    return mon_wrap('Reconciliation', 'Conciliación',
                    'Four-check energy reconciliation · vendor counters are the billing control',
                    'Conciliación de energía en cuatro pasos · los contadores del fabricante son el control de facturación',
                    MG.recon_page(skin='portal'), 'recon',
                    buttons=f'<a class="btn" href="/report/invoices/">{ico("report", 15, "#053b38", 2)} {t("Invoice annexes", "Anexos de facturación")}</a>')


def signed_out_page():
    body = (f'<div style="max-width:520px;margin:60px auto;text-align:center"><h1 class="pt">{t("You are signed out", "Sesión cerrada")}</h1>'
            f'<p class="muted" style="margin:10px 0 22px">{t("Your session has ended on the server. Nobody can continue as you from this browser.", "Su sesión ha terminado en el servidor. Nadie puede continuar con su cuenta desde este navegador.")}</p>'
            f'<a class="btn" href="/login">{t("Sign in again", "Volver a entrar")}</a></div>')
    return C.page('Signed out', body)


def no_access_page():
    body = (f'<div style="max-width:520px;margin:60px auto;text-align:center"><h1 class="pt">{t("No access", "Sin acceso")}</h1>'
            f'<p class="muted" style="margin:10px 0 22px">{t("This part of the portal is not open to your account. Taking you back to the front door…", "Esta sección no está disponible para su cuenta. Volviendo a la portada…")}</p>'
            f'<a class="btn" href="/">{t("Back to the portal", "Volver al portal")}</a></div>')
    return C.page('No access', body, extra_head='<meta http-equiv="refresh" content="6;url=/">')


MON_OVERRIDES = '''
.monbody .pill.good{background:#e6f7f5;color:#05847d}.monbody .pill.warn{background:#fff4e0;color:#b26a00}.monbody .pill.bad{background:#fdeaea;color:#c2554e}
.monbody .card{border-radius:12px;border-color:#e3e6ea}.monbody .kpi .v{color:#1a1d23}.monbody a{color:#05847d}
.monctl input[type=date]{border:1px solid #d2d7dd;border-radius:8px;padding:8px 10px;font:600 13px inherit;font-family:inherit;background:#fff}
.monctl .btn{background:#fff;color:#41474f;border:1px solid #d2d7dd;font-weight:600;padding:9px 14px}.monctl a.btn:last-child{background:#05b1a9;color:#053b38;border-color:#05b1a9;font-weight:800}
'''


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
 <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="pill crit">{ico("bell", 14, "currentColor", 2.2)} {crit} {t("critical", "críticas")}</span><span class="pill warn">{warn} {t("warnings", "avisos")}</span></div>
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
    write('logged-out.html', signed_out_page()); n += 1
    write('no-access.html', no_access_page()); n += 1
    write('report/index.html', report_overview()); n += 1
    write('report/ppa/index.html', report_overview(PPA, 'ppa', 'PPA plants', 'Plantas PPA')); n += 1
    write('report/capex/index.html', report_overview(CAPEX, 'capex', 'CAPEX plants', 'Plantas CAPEX')); n += 1
    write('report/plants/index.html', plant_cards()); n += 1
    write('monitoring/index.html', monitoring_overview()); n += 1
    write('monitoring/ppa/index.html', monitoring_overview('ppa')); n += 1
    write('monitoring/capex/index.html', monitoring_overview('capex')); n += 1
    write('monitoring/performance/index.html', monitoring_performance()); n += 1
    write('monitoring/recon/index.html', monitoring_recon()); n += 1
    # parity phase: not-yet-rebuilt destinations land on the old site
    write('report/financial/index.html', financial_report()); n += 1
    write('report/invoices/index.html', invoices_page()); n += 1
    write('map/index.html', map_page()); n += 1
    # /setup/ (people, plants, finance, cfe, system, account) and /ask/ are
    # the live apps, proxied by nginx on this host too, with the portal skin
    for k in PPA + CAPEX:
        write(f'report/{C.slug(k)}/index.html', plant_report(k)); n += 1
        # the code is an internal alias: /report/gto1/ -> /report/taigene/
        write(f'report/{k.lower()}/index.html', C.redirect_page(f'/report/{C.slug(k)}/', name(k), name(k))); n += 1
        write(f'monitoring/{C.slug(k)}/index.html', monitoring_plant(k, MG.TODAY)); n += 1
        write(f'monitoring/{k.lower()}/index.html', C.redirect_page(f'/monitoring/{C.slug(k)}/', name(k), name(k))); n += 1
        for d in MG.DATES:
            if d != MG.TODAY:
                write(f'monitoring/{C.slug(k)}/d/{d}.html', monitoring_plant(k, d)); n += 1
    write('engine/index.html', C.redirect_page(C.ENGINE_URL, 'Engine', 'Engine')); n += 1
    write('ags/index.html', C.redirect_page(C.AGS_URL, 'ARGIA Golden Standard', 'ARGIA Golden Standard')); n += 1
    print(f'portal_gen: wrote {n} pages under {OUTROOT}')


if __name__ == '__main__':
    main()
