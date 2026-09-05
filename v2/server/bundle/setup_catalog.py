"""The admin catalog — pure helpers (v200, phase 4).

The /setup/ panel is a card file: one colour-coded DRAWER per domain,
TABS inside a drawer, and on a tab index CARDS — each card the edit
form (or the read-only view) of exactly one thing. This module holds
the model and every pure piece of it (no Flask, no psql, no I/O) so the
unit tests can exercise what ships; ``setup_app.py`` assembles the
drawers from it.

URLs: nginx proxies ``/setup/`` to the app root, so a drawer lives at
``/setup/<drawer>/`` and its tabs are anchors on that page —
``/setup/finance/#loans``. Nothing about deployment changes.
"""
from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Sequence, Tuple

# key, EN, ES, colour, tabs[(key, EN, ES)]
DRAWERS: List[Dict] = [
    {"key": "people", "en": "People", "es": "Personas", "color": "#2a78d6",
     "sub_en": "Who can sign in, what they can see, what they receive.",
     "sub_es": "Quién entra, qué ve, qué recibe.",
     "tabs": [("users", "Users", "Usuarios"),
              ("access", "Access & areas", "Accesos y áreas"),
              ("mail", "Email subscriptions", "Suscripciones de correo"),
              ("ask", "Ask ARGIA", "Ask ARGIA")]},
    {"key": "plants", "en": "Plants", "es": "Plantas", "color": "#1e8e3e",
     "sub_en": "The fleet as the system knows it — plants, inverters, expectations, maintenance.",
     "sub_es": "La flota como la conoce el sistema — plantas, inversores, expectativas, mantenimiento.",
     "tabs": [("plants", "Plants", "Plantas"),
              ("inverters", "Inverters", "Inversores"),
              ("settings", "Expected production & tariff", "Producción esperada y tarifa"),
              ("maintenance", "Maintenance events", "Eventos de mantenimiento")]},
    {"key": "finance", "en": "Finance", "es": "Finanzas", "color": "#b8860b",
     "sub_en": "The inputs behind the financial report and the invoices. Paid history is immutable.",
     "sub_es": "Los insumos del reporte financiero y las facturas. El historial pagado es inmutable.",
     "tabs": [("loans", "Loans & schedule", "Créditos y calendario"),
              ("om", "O&M costs", "Costos O&M"),
              ("fees", "LaaS fees", "Cuotas LaaS"),
              ("tariffs", "PPA tariffs", "Tarifas PPA"),
              ("baselines", "PR baseline & SLA", "PR base y SLA"),
              ("invoicing", "Invoicing", "Facturación"),
              ("changelog", "Change log", "Bitácora")]},
    {"key": "cfe", "en": "CFE & tariffs", "es": "CFE y tarifas", "color": "#e8710a",
     "sub_en": "The CFE tariff pipeline: what was scraped, what the Engine received.",
     "sub_es": "El flujo de tarifas CFE: qué se descargó, qué recibió el Engine.",
     "tabs": [("status", "Tariff status", "Estado de tarifas"),
              ("push", "Engine push", "Envío al Engine")]},
    {"key": "system", "en": "System", "es": "Sistema", "color": "#5f6368",
     "sub_en": "Jobs, backups, exports, usage — and where every dataset comes from.",
     "sub_es": "Tareas, respaldos, exportaciones, uso — y de dónde viene cada dato.",
     "tabs": [("jobs", "Jobs & timers", "Tareas y temporizadores"),
              ("runs", "Last runs", "Últimas ejecuciones"),
              ("backups", "Backups", "Respaldos"),
              ("exports", "Drive exports", "Exportaciones a Drive"),
              ("usage", "Usage statistics", "Estadísticas de uso"),
              ("sources", "Data sources", "Fuentes de datos")]},
]

DRAWER_KEYS = [d["key"] for d in DRAWERS]


def drawer(key: str) -> Optional[Dict]:
    for d in DRAWERS:
        if d["key"] == key:
            return d
    return None


def drawer_url(key: str, tab: str = "") -> str:
    return f"/setup/{key}/" + (f"#{tab}" if tab else "")


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


# ---------------------------------------------------------------- HTML

CATALOG_CSS = """
.drawers{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;margin:14px 0;}
.drawer{display:block;background:#fff;border:1px solid #e4e7ea;border-radius:10px;padding:0 0 14px;
 text-decoration:none;color:#202124;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.04);}
.drawer:hover{box-shadow:0 6px 18px rgba(0,0,0,.10);}
.drawer .tabstrip{height:10px;}
.drawer h3{margin:12px 16px 4px;font-size:16px;letter-spacing:.04em;}
.drawer p{margin:0 16px 8px;font-size:12.5px;color:#5f6368;}
.drawer ul{margin:0 16px;padding:0 0 0 16px;font-size:12.5px;color:#3c4043;}
.drawer ul li{margin:2px 0;}
.dnav{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 6px;}
.dnav a{padding:5px 12px;border-radius:8px 8px 0 0;border:1px solid #e4e7ea;border-bottom:0;
 background:#eef1f4;color:#3c4043;text-decoration:none;font-size:13px;}
.dnav a.on{background:#fff;font-weight:600;border-top:3px solid var(--dc,#5f6368);}
.tabbar{position:sticky;top:0;z-index:20;background:#f6f7f8;padding:8px 0;border-bottom:1px solid #e4e7ea;
 display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;}
.tabbar a{font-size:12.5px;padding:4px 10px;border-radius:12px;background:#fff;border:1px solid #dadce0;
 color:#3c4043;text-decoration:none;}
.tabbar a:hover{border-color:var(--dc,#5f6368);}
section.tab{scroll-margin-top:56px;}
section.tab>h2.tabh{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--dc,#5f6368);
 margin:22px 0 -6px;}
.kv{font-size:13px;}
.kv td:first-child{color:#5f6368;white-space:nowrap;width:220px;}
.ok{color:#137333;font-weight:600;} .bad{color:#b3261e;font-weight:600;} .warn{color:#b06000;font-weight:600;}
"""


def catalog_home(drawers: Sequence[Dict] = DRAWERS) -> str:
    """The card file: one card per drawer, its tabs listed. Pure."""
    out = ['<div class="drawers">']
    for d in drawers:
        tabs = "".join(
            f'<li><a href="{drawer_url(d["key"], t)}" data-en="{_e(en)}" data-es="{_e(es)}">{_e(en)}</a></li>'
            for t, en, es in d["tabs"])
        out.append(
            f'<div class="drawer" style="--dc:{d["color"]}">'
            f'<div class="tabstrip" style="background:{d["color"]}"></div>'
            f'<h3><a href="{drawer_url(d["key"])}" style="color:inherit;text-decoration:none"'
            f' data-en="{_e(d["en"])}" data-es="{_e(d["es"])}">{_e(d["en"])}</a></h3>'
            f'<p data-en="{_e(d["sub_en"])}" data-es="{_e(d["sub_es"])}">{_e(d["sub_en"])}</p>'
            f'<ul>{tabs}</ul></div>')
    out.append('</div>')
    return "".join(out)


def drawer_nav(active: str, drawers: Sequence[Dict] = DRAWERS) -> str:
    """The drawer tabs across the top of every drawer page. Pure."""
    parts = ['<div class="dnav">']
    for d in drawers:
        cls = ' class="on"' if d["key"] == active else ''
        parts.append(f'<a href="{drawer_url(d["key"])}"{cls} style="--dc:{d["color"]}"'
                     f' data-en="{_e(d["en"])}" data-es="{_e(d["es"])}">{_e(d["en"])}</a>')
    parts.append('</div>')
    return "".join(parts)


def tab_bar(d: Dict) -> str:
    return ('<div class="tabbar" style="--dc:%s">' % d["color"]
            + "".join(f'<a href="#{t}" data-en="{_e(en)}" data-es="{_e(es)}">{_e(en)}</a>'
                      for t, en, es in d["tabs"])
            + '</div>')


def section(d: Dict, tab: str, body: str) -> str:
    """One tab on a drawer page: anchor + heading + its cards. Pure."""
    en, es = next(((en, es) for t, en, es in d["tabs"] if t == tab), (tab, tab))
    return (f'<section class="tab" id="{_e(tab)}" style="--dc:{d["color"]}">'
            f'<h2 class="tabh" data-en="{_e(en)}" data-es="{_e(es)}">{_e(en)}</h2>{body}</section>')


def drawer_page(d: Dict, sections: Sequence[Tuple[str, str]]) -> str:
    """nav + tab bar + sections, in the drawer's tab order. Pure."""
    order = [t for t, _, _ in d["tabs"]]
    by_tab = dict(sections)
    body = "".join(section(d, t, by_tab[t]) for t in order if t in by_tab)
    return drawer_nav(d["key"]) + tab_bar(d) + body


# ---------------------------------------------------------------- parsers (System drawer)

def _span(us: int) -> str:
    """Microseconds -> '4min 8s' / '1h 28min' / '3 weeks 5 days'. Pure."""
    sec = max(int(us) // 1_000_000, 0)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}min" + (f" {sec % 60}s" if sec % 60 else "")
    if sec < 86400:
        return f"{sec // 3600}h" + (f" {(sec % 3600) // 60}min" if (sec % 3600) // 60 else "")
    days = sec // 86400
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''}" + (f" {(sec % 86400) // 3600}h" if (sec % 86400) // 3600 else "")
    weeks, rest = days // 7, days % 7
    return f"{weeks} weeks" + (f" {rest} day{'s' if rest != 1 else ''}" if rest else "")


def parse_timers(text: str, now_us: Optional[int] = None,
                 fmt=None) -> List[Dict[str, str]]:
    """`systemctl list-timers --all --no-pager --output=json 'argia-*'`
    -> rows. systemd's JSON gives next / last as microseconds since the
    epoch (0 / null when a timer is disabled or has never run); 'left'
    and 'passed' are recomputed from now_us because older systemd
    reports 'left' as the absolute time. Times are rendered with fmt
    (default: server local time). Pure."""
    import datetime as _dt
    import json
    import time as _time
    if now_us is None:
        now_us = int(_time.time() * 1_000_000)
    if fmt is None:
        def fmt(us):
            return _dt.datetime.fromtimestamp(us / 1_000_000).strftime("%a %Y-%m-%d %H:%M")
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    rows = []
    for t in data if isinstance(data, list) else []:
        unit = str(t.get("unit") or "")
        if ".timer" not in unit:
            continue
        nxt, last = t.get("next") or 0, t.get("last") or 0
        rows.append({"next": fmt(nxt) if nxt else "",
                     "left": _span(nxt - now_us) if nxt else "",
                     "last": fmt(last) if last else "",
                     "passed": _span(now_us - last) if last else "",
                     "unit": unit, "svc": str(t.get("activates") or "")})
    return rows


SWITCH_META: List[Tuple[str, str, str]] = [
    # env name, what it gates, value that means 'on PostgreSQL / sheet off'
    ("ARGIA_CONFIG_SOURCE", "Plants / Inverters configuration", "pg"),
    ("ARGIA_TELEMETRY_SOURCE", "5-minute telemetry readers (KPI, alerts, dashboard)", "pg"),
    ("ARGIA_KPI_SOURCE", "KPI_Daily readers (alerts, reports, finance, annex)", "pg"),
    ("ARGIA_KPI_WRITE", "kpi_eod writes daily_production (sheet | both | pg)", "pg"),
    ("ARGIA_FINANCE_SOURCE", "Contract, loans, design, maintenance events", "pg"),
    ("ARGIA_INVOICING_SOURCE", "Invoiced history (annex)", "pg"),
    ("ARGIA_ALERTS_SOURCE", "Alerts ledger (alert_ledger)", "pg"),
    ("ARGIA_DASHBOARD_SOURCE", "Dashboard buckets (dashboard_plant / _inverter)", "pg"),
    ("ARGIA_SHEET_TELEMETRY", "Telemetry_Argia sheet write (1 = on)", "0"),
    ("ARGIA_SHEET_OUTBOX", "Report_Outbox sheet write (1 = on)", "0"),
    ("ARGIA_SHEET_JOBLOG", "SyncRuns sheet write (1 = on)", "0"),
]
SWITCH_DEFAULTS = {"ARGIA_SHEET_TELEMETRY": "1", "ARGIA_SHEET_OUTBOX": "1",
                   "ARGIA_SHEET_JOBLOG": "0"}
ID_KEYS = ("GOOGLE_SHEET_ID_V2", "ARGIA_SOLAR_SHEET_ID")


def parse_env_switches(text: str) -> Dict[str, str]:
    """Only the ARGIA_* data-source switches and whether the two sheet
    ids are SET — never a value of anything else (the env file also holds
    secrets). Pure."""
    out: Dict[str, str] = {}
    keys = {k for k, _, _ in SWITCH_META}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip().replace("export ", "")
        if k in keys:
            out[k] = v.strip().strip('"').strip("'")
        elif k in ID_KEYS:
            out[k] = "set" if v.strip().strip('"').strip("'") else "unset"
    for k in ID_KEYS:
        out.setdefault(k, "unset")
    return out


def switch_rows(sw: Dict[str, str]) -> List[Tuple[str, str, str, bool]]:
    """(name, effective value, what it gates, on-PG?) per switch. Pure."""
    rows = []
    for name, what, done in SWITCH_META:
        if name in sw and sw[name] != "":
            eff = sw[name]
        else:
            eff = SWITCH_DEFAULTS.get(name, "sheet") + " (default)"
        rows.append((name, eff, what, eff.split(" ")[0] == done))
    return rows


def sheet_still_needed(sw: Dict[str, str]) -> List[str]:
    """Mirror of argia.core.sheets.sheet_still_needed over the parsed
    switches (the bundle cannot import the package). Pure."""
    reasons = []
    for name, _, done in SWITCH_META:
        v = sw.get(name, SWITCH_DEFAULTS.get(name, "sheet"))
        eff = v.split(" ")[0] if v else SWITCH_DEFAULTS.get(name, "sheet")
        if name.startswith("ARGIA_SHEET_"):
            on = eff not in ("0", "false", "no")
            if on:
                reasons.append(f"{name}=on")
        elif eff != done:
            reasons.append(f"{name}={eff}")
    return reasons


def sources_card_html(sw: Dict[str, str]) -> str:
    """The 'Data sources' card — the Sheets cut, visible. Pure."""
    rows = "".join(
        f'<tr><td><code>{_e(n)}</code></td><td><b class="{"ok" if ok else "warn"}">{_e(v)}</b></td>'
        f'<td>{_e(what)}</td></tr>'
        for n, v, what, ok in switch_rows(sw))
    ids = "".join(
        f'<tr><td><code>{_e(k)}</code></td>'
        f'<td><b class="{"warn" if sw.get(k) == "set" else "ok"}">{_e(sw.get(k, "unset"))}</b></td>'
        f'<td>{"the workbook is still reachable from the server" if sw.get(k) == "set" else "retired"}</td></tr>'
        for k in ID_KEYS)
    need = sheet_still_needed(sw)
    verdict = ('<p class="ok" data-en="Google Sheets: not needed by any job."'
               ' data-es="Google Sheets: ningún trabajo lo necesita.">Google Sheets: not needed by any job.</p>'
               if not need else
               '<p class="warn">Google Sheets still needed by: ' + _e(", ".join(need)) + '</p>')
    return ('<div class="card"><h2 data-en="Data sources" data-es="Fuentes de datos">Data sources</h2>'
            '<p class="note" data-en="Every switch in /root/.argia_env that decides where a job reads or writes. '
            'Values are read from the file; changing one is a one-line edit there (root only)."'
            ' data-es="Cada interruptor de /root/.argia_env que decide de dónde lee o escribe un trabajo. '
            'Se leen del archivo; cambiar uno es editar una línea (solo root).">'
            'Every switch in /root/.argia_env that decides where a job reads or writes.</p>'
            f'<table class="kv"><tr><th>Switch</th><th>Value</th><th>Gates</th></tr>{rows}{ids}</table>'
            f'{verdict}</div>')


def parse_failed_units(text: str) -> List[str]:
    """`systemctl --failed --no-legend --plain` -> argia unit names. Pure."""
    out = []
    for ln in text.splitlines():
        parts = ln.split()
        if parts and parts[0].startswith("argia-"):
            out.append(parts[0])
    return out
