"""v268 - ARGIA on a phone: the installable web app at portal.argia.com.mx/app/.

Tomasz, 2026-09-29: "do it so I can see how it works on my iPhone" and
"use the same single A letter logo that we are using for the website".

What makes it an app rather than a web page, on an iPhone:

* ``apple-mobile-web-app-capable`` + the manifest -> Safari's
  Share > Add to Home Screen gives an icon that opens full screen,
  without the browser bar;
* ``/apple-touch-icon.png`` -> the icon itself: the website's single-letter
  mark (argia.com.mx/images/favicon/safari-pinned-tab.svg, same polygon),
  drawn here with some margin so the rounded corners never cut it.
  That path is already public in auth_core.PUBLIC_EXACT: the phone fetches
  the icon before anybody is signed in;
* a service worker (``/app/sw.js``) -> the phone can show notifications
  (iOS 16.4+, only once the app is on the Home Screen). It caches NOTHING:
  every screen comes from the server, so the phone can never show stale
  numbers as if they were current.

The page is written by portal_gen every run (5 min), from the same data as
the monitoring pages. PPA plants only (Tomasz, 2026-09-29: "only PPAs in the
app, no need for CAPEX"); every plant, open alert and loss figure is real.
The header carries the website's logo (argia_logo, v251) - the single-letter
mark is the Home Screen icon only. No plant codes anywhere (customer names only); every string in
both languages.

Pure rendering: plain dicts in, text/bytes out - no database, no clock.
"""
from __future__ import annotations

import functools
import html
import json
import struct
import zlib

try:
    from argia_logo import LOGO_URI, LOGO_ALT     # the website's logo: ARGIA / Smart Energy Solutions
except ImportError:                               # pragma: no cover - the bundle always has it
    LOGO_URI, LOGO_ALT = '', 'ARGIA'

# ------------------------------------------------------------------ the mark
# The website's single-letter logo, from its safari-pinned-tab.svg
# (viewBox 0 0 23699 23699). The letter IS the polygon - no font involved.
GLYPH = ((9052, 0), (10288, 0), (13173, 0), (14410, 0), (22783, 23699), (18429, 23699),
         (11731, 4200), (5033, 23699), (678, 23699))
GLYPH_BOX = (678, 0, 22783, 23699)            # x0, y0, x1, y1
ICON_BG, ICON_FG = 237, 20                    # the website icon: #ededed ground, #141414 letter
ICON_SIZES = (180, 192, 512)                  # 180 = iPhone Home Screen; 192/512 = manifest (Android)
THEME = "#ffffff"
# v270 (Tomasz): 34px was 'way too big' on the phone. 24px keeps the tagline on a
# retina screen (3 lines of ~6 CSS px = 18 device px each) and halves the header.
LOGO_PX = 24


def mark_svg(size=22, cls="mark"):
    pts = " ".join(f"{x},{y}" for x, y in GLYPH)
    return (f'<svg class="{cls}" width="{size}" height="{size}" viewBox="0 0 23699 23699" aria-hidden="true">'
            f'<polygon fill="currentColor" points="{pts}"/></svg>')


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


@functools.lru_cache(maxsize=None)
def icon_png(size: int, letter_height: float = 0.56, bg: int = ICON_BG, fg: int = ICON_FG) -> bytes:
    """Opaque grayscale PNG, the letter centred at ``letter_height`` of the
    icon. Scanline fill with 4 sub-rows and exact horizontal coverage, so the
    slanted edges are anti-aliased. Pure python: no Pillow on the server."""
    x0, y0, x1, y1 = GLYPH_BOX
    k = size * letter_height / (y1 - y0)
    ox = (size - (x1 - x0) * k) / 2 - x0 * k
    oy = (size - (y1 - y0) * k) / 2 - y0 * k
    pts = [(x * k + ox, y * k + oy) for x, y in GLYPH]
    n, ss = len(pts), 4
    raw = bytearray()
    for row in range(size):
        acc = [0.0] * size
        for s in range(ss):
            y = row + (s + 0.5) / ss
            xs = []
            for i in range(n):
                (xa, ya), (xb, yb) = pts[i], pts[(i + 1) % n]
                if (ya <= y < yb) or (yb <= y < ya):
                    xs.append(xa + (y - ya) * (xb - xa) / (yb - ya))
            xs.sort()
            for a, b in zip(xs[0::2], xs[1::2]):
                a, b = max(0.0, a), min(float(size), b)
                if b <= a:
                    continue
                ia, ib = int(a), int(b)
                if ia == ib:
                    acc[ia] += (b - a) / ss
                    continue
                acc[ia] += (ia + 1 - a) / ss
                for x in range(ia + 1, min(ib, size)):
                    acc[x] += 1.0 / ss
                if ib < size:
                    acc[ib] += (b - ib) / ss
        raw.append(0)                                      # filter: none
        raw.extend(int(round(bg + (fg - bg) * min(1.0, c))) for c in acc)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def manifest() -> str:
    return json.dumps({
        "name": "ARGIA", "short_name": "ARGIA", "lang": "en",
        "start_url": "/app/", "scope": "/", "display": "standalone",
        "background_color": "#ededed", "theme_color": THEME,
        "icons": [{"src": f"/app/icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png"} for s in (192, 512)],
    }, indent=1)


# No fetch listener on purpose: the app never answers from a cache.
SW_JS = """/* ARGIA app service worker (v268). Notifications only - it caches nothing,
   so every screen always comes from the server. */
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = {body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'ARGIA',
    {body: d.body || '', icon: '/apple-touch-icon.png', tag: d.tag || undefined, data: {url: d.url || '/app/#alerts'}}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/app/';
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(cs => {
    for (const c of cs) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    return self.clients.openWindow(url);
  }));
});
"""


# ------------------------------------------------------------------ helpers
def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def t(en, es=None, tag="span", cls="") -> str:
    es = en if es is None else es
    c = f' class="{cls}"' if cls else ""
    return f'<{tag}{c} data-en="{esc(en)}" data-es="{esc(es)}">{esc(en)}</{tag}>'


def num(v, d=0, dash="-") -> str:
    if v is None:
        return dash
    try:
        return f"{float(v):,.{d}f}"
    except (TypeError, ValueError):
        return dash


def money(v, dash="-") -> str:
    return dash if v is None else f"${float(v):,.0f}"


MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def day_label(iso: str) -> str:
    try:
        y, m, d = (int(x) for x in iso[:10].split("-"))
        return t(f"{MONTHS_EN[m-1]} {d}", f"{d} {MONTHS_ES[m-1]}")
    except (ValueError, IndexError):
        return esc(iso)


STATE_ORDER = {"bad": 0, "warn": 1, "good": 2, "off": 3}
CAUSES = (("unavailability", "Unavailability", "Indisponibilidad"),
          ("overheating", "Overheating", "Sobrecalentamiento"),
          ("underperformance", "Underperformance", "Bajo desempeño"))

ICONS = {
    "fleet": '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    "alerts": '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
    "losses": '<path d="M12 2v20M17 6H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
    "tickets": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>',
    "more": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
    "back": '<path d="M15 5l-7 7 7 7"/>',
}


def icon(name, size=22) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


def sorted_plants(plants):
    return sorted(plants, key=lambda p: (STATE_ORDER.get(p.get("state"), 9), str(p.get("name", "")).lower()))


def all_alerts(plants):
    out = []
    for p in plants:
        for a in p.get("alerts") or []:
            out.append((p, a))
    out.sort(key=lambda pa: (0 if str(pa[1].get("sev", "")).upper() == "CRITICAL" else 1, pa[1].get("since") or ""))
    return out


def sev_pill(sev) -> str:
    s = str(sev or "").upper()
    return (t("Critical", "Crítica", cls="pill crit") if s == "CRITICAL"
            else t("Warning", "Aviso", cls="pill warn"))


def alert_row(p, a, with_plant=True) -> str:
    head = f'<b>{esc(p["name"])}</b>' if with_plant else ""
    inv = f'<small class="inv">{esc(a["inverter"])}</small>' if a.get("inverter") else ""
    return (f'<a class="row al" href="#p-{esc(p["slug"])}">{sev_pill(a.get("sev"))}'
            f'<div class="grow">{head}<span class="what">{esc(a.get("text") or a.get("what") or "")}</span>{inv}'
            f'<small>{t("open since", "abierta desde")} {day_label(a.get("since") or "")}</small></div></a>')


# ------------------------------------------------------------------ views
def fleet_view(plants, gen_hhmm) -> str:
    power = sum(p["power_kw"] or 0.0 for p in plants if p.get("power_kw") is not None)
    today = sum(p["today_kwh"] or 0.0 for p in plants if p.get("today_kwh") is not None)
    alerts = all_alerts(plants)
    crit = sum(1 for _, a in alerts if str(a.get("sev", "")).upper() == "CRITICAL")
    cards = []
    for p in sorted_plants(plants):
        cards.append(
            f'<a class="row pc st-{esc(p.get("state"))}" href="#p-{esc(p["slug"])}"><span class="dot"></span>'
            f'<div class="grow"><b>{esc(p["name"])}</b>'
            f'<small>{esc(p.get("where") or "")}{" · " if p.get("where") else ""}{esc(p.get("portfolio") or "")}'
            f' · {num(p.get("kwp"))} kWp</small>'
            f'<small class="stt">{t(p.get("state_en") or "", p.get("state_es"))}</small></div>'
            f'<div class="right"><b>{num(p.get("power_kw"))} kW</b><small>{num(p.get("today_kwh"))} kWh</small></div></a>')
    return (f'<section class="v" id="v-fleet" data-tab="fleet">'
            f'<p class="kick">{t("PPA fleet now", "Flota PPA ahora")} · {esc(gen_hhmm)} MX</p>'
            f'<p class="big">{num(power)} <span>kW</span></p>'
            f'<div class="tiles"><div class="tile"><b>{num(today)}</b>{t("kWh today", "kWh hoy")}</div>'
            f'<a class="tile{" red" if crit else ""}" href="#alerts"><b>{crit} {t("critical", "críticas")}</b>'
            f'{len(alerts) - crit} {t("warnings", "avisos")}</a></div>'
            f'<div class="card list">{"".join(cards) or t("No plants.", "Sin plantas.", tag="p", cls="empty")}</div>'
            f'</section>')


def loss_card(p) -> str:
    L = p.get("loss30") or {}
    if not L or not L.get("days"):
        return (f'<div class="card pad">{t("Lost production: no loss figures for this plant yet.", "Producción perdida: aún sin cifras para esta planta.", tag="p", cls="muted")}</div>')
    ppa = L.get("mxn") is not None
    head = (f'<p class="loss">{money(L["mxn"])} <span>MXN</span></p>' if ppa
            else f'<p class="loss">{num(L.get("kwh"))} <span>kWh</span></p>')
    parts = []
    for key, en, es in CAUSES:
        v = L.get(f"{key}_mxn") if ppa else L.get(key)
        if v and v >= (1 if ppa else 0.5):
            parts.append(f'<li>{t(en, es)}<b>{money(v) if ppa else num(v) + " kWh"}</b></li>')
    extra = "" if ppa else t(" (CAPEX plant: kWh, no tariff)", " (planta CAPEX: kWh, sin tarifa)")
    nd = L["days"]
    return (f'<div class="card pad lossc">{t(f"Lost in the last {nd} days", f"Perdido en los últimos {nd} días", tag="p", cls="kick")}'
            f'{head}<ul>{"".join(parts)}</ul>'
            f'<p class="note">{t("Expected = nearby healthy plants, else the weather model, at 100% availability.", "Esperado = plantas vecinas sanas, si no el modelo de clima, con 100% de disponibilidad.")}{extra}</p></div>')


def days_card(p) -> str:
    rows = []
    for d in (p.get("days") or [])[:7]:
        e, x = d.get("actual"), d.get("expected")
        pct = (100.0 * e / x) if (e is not None and x) else None
        w = 0 if pct is None else max(2, min(100, pct))
        bad = pct is not None and pct < 80
        lost = d.get("lost_mxn") if d.get("lost_mxn") is not None else None
        tail = ""
        if lost is not None and lost >= 1:
            tail = f" · {money(lost)}"
        elif d.get("lost_mxn") is None and (d.get("lost_kwh") or 0) >= 1:
            tail = f" · {num(d['lost_kwh'])} kWh"
        rows.append(f'<div class="drow"><span class="dd">{day_label(d.get("date") or "")}</span>'
                    f'<span class="bar"><i class="{"bad" if bad else ""}" style="width:{w:.0f}%"></i></span>'
                    f'<span class="dv{" red" if bad else ""}">{"-" if pct is None else f"{pct:,.0f}%"}{tail}</span></div>')
    if not rows:
        return ""
    return (f'<p class="sec">{t("Last 7 days - actual vs expected, and what it cost", "Últimos 7 días - real vs esperado, y lo que costó")}</p>'
            f'<div class="card pad">{"".join(rows)}</div>')


def plant_view(p) -> str:
    al = [alert_row(p, a, with_plant=False) for a in (p.get("alerts") or [])]
    alerts = (f'<p class="sec">{t("Open alerts", "Alertas abiertas")}</p><div class="card list">{"".join(al)}</div>' if al else "")
    slug = esc(p["slug"])
    return (f'<section class="v" id="v-p-{slug}" data-tab="fleet" hidden>'
            f'<a class="back" href="#fleet" onclick="return goBack()">{icon("back", 18)}{t("Fleet", "Flota")}</a>'
            f'<h1>{esc(p["name"])}</h1>'
            f'<p class="sub st-{esc(p.get("state"))}"><span class="dot"></span>{t(p.get("state_en") or "", p.get("state_es"))}'
            f' · {esc(p.get("where") or "")} · {esc(p.get("portfolio") or "")} · {num(p.get("kwp"))} kWp</p>'
            f'<div class="tiles three"><div class="tile"><b>{num(p.get("power_kw"))}</b>{t("kW now", "kW ahora")}</div>'
            f'<div class="tile"><b>{num(p.get("today_kwh"))}</b>{t("kWh today", "kWh hoy")}</div>'
            f'<div class="tile"><b>{num(p.get("inv_live"))}/{num(p.get("inv_total"))}</b>{t("inverters live", "inversores")}</div></div>'
            f'{loss_card(p)}{days_card(p)}{alerts}'
            f'<div class="btns"><a class="btn" href="/monitoring/{slug}/">{t("Full plant page", "Página completa")}</a>'
            f'<a class="btn" href="/report/{slug}/">{t("Report", "Reporte")}</a></div>'
            f'</section>')


def alerts_view(plants) -> str:
    rows = [alert_row(p, a) for p, a in all_alerts(plants)]
    body = "".join(rows) or t("No open alerts.", "Sin alertas abiertas.", tag="p", cls="empty")
    return (f'<section class="v" id="v-alerts" data-tab="alerts" hidden><h1>{t("Alerts", "Alertas")}</h1>'
            f'<p class="sub">{len(rows)} {t("open, critical first", "abiertas, críticas primero")}</p>'
            f'<div class="card list">{body}</div></section>')


def losses_view(plants) -> str:
    tot_kwh = tot_mxn = 0.0
    cause_mxn = {k: 0.0 for k, _, _ in CAUSES}
    days = 0
    ranked = []
    for p in plants:
        L = p.get("loss30") or {}
        if not L.get("days"):
            continue
        days = max(days, L["days"])
        tot_kwh += L.get("kwh") or 0.0
        if L.get("mxn") is not None:
            tot_mxn += L["mxn"]
            for k, _, _ in CAUSES:
                cause_mxn[k] += L.get(f"{k}_mxn") or 0.0
        ranked.append(p)
    ranked.sort(key=lambda p: (-(p["loss30"].get("mxn") or 0.0), -(p["loss30"].get("kwh") or 0.0)))
    top = max(cause_mxn.values()) or 1.0
    bars = "".join(f'<div class="drow"><span class="dd wide">{t(en, es)}</span><span class="bar"><i class="bad" style="width:{100*cause_mxn[k]/top:.0f}%"></i></span>'
                   f'<span class="dv">{money(cause_mxn[k])}</span></div>' for k, en, es in CAUSES)
    rows = []
    for p in ranked:
        L = p["loss30"]
        val = money(L["mxn"]) if L.get("mxn") is not None else f'{num(L.get("kwh"))} kWh'
        rows.append(f'<a class="row" href="#p-{esc(p["slug"])}"><div class="grow"><b>{esc(p["name"])}</b>'
                    f'<small>{num(L.get("kwh"))} kWh {t("lost", "perdidos")}</small></div><div class="right"><b>{val}</b></div></a>')
    return (f'<section class="v" id="v-losses" data-tab="losses" hidden><h1>{t("Losses", "Pérdidas")}</h1>'
            f'<p class="sub">{t(f"Last {days} days, all PPA plants", f"Últimos {days} días, todas las plantas PPA")}</p>'
            f'<div class="tiles"><div class="tile red"><b>{money(tot_mxn)}</b>{t("MXN lost", "MXN perdidos")}</div>'
            f'<div class="tile"><b>{num(tot_kwh)}</b>{t("kWh lost", "kWh perdidos")}</div></div>'
            f'<p class="sec">{t("By cause (MXN)", "Por causa (MXN)")}</p><div class="card pad">{bars}</div>'
            f'<p class="sec">{t("By plant", "Por planta")}</p>'
            f'<div class="card list">{"".join(rows) or t("No loss figures yet.", "Aún sin cifras.", tag="p", cls="empty")}</div>'
            f'<div class="btns"><a class="btn" href="/monitoring/losses/">{t("Full losses page", "Página completa de pérdidas")}</a></div>'
            f'</section>')


def more_view(gen_mx) -> str:
    return (f'<section class="v" id="v-more" data-tab="more" hidden><h1>{t("More", "Más")}</h1>'
            f'<p class="sub" id="who"></p>'
            f'<div class="card pad" id="install">{t("Install on the iPhone: open this page in Safari, tap Share, then Add to Home Screen. The ARGIA icon opens it full screen.", "Instalar en el iPhone: abre esta página en Safari, toca Compartir y luego Agregar a inicio. El icono de ARGIA la abre en pantalla completa.", tag="p")}</div>'
            f'<div class="card list">'
            f'<div class="row"><div class="grow"><b>{t("Language", "Idioma")}</b></div><span class="seg"><button data-l="en" onclick="setLang(\'en\',true)">EN</button><button data-l="es" onclick="setLang(\'es\',true)">ES</button></span></div>'
            f'<div class="row"><div class="grow"><b>{t("Test a notification", "Probar una notificación")}</b>'
            f'<small id="note-out">{t("Shows how a critical alert would arrive on this phone.", "Muestra cómo llegaría una alerta crítica a este teléfono.")}</small></div>'
            f'<button class="btn sm" onclick="testNote()">{t("Send", "Enviar")}</button></div>'
            f'<a class="row" href="/maintenance/"><div class="grow"><b>{t("Maintenance tickets", "Tickets de mantenimiento")}</b><small>{t("the full tickets page", "la página completa de tickets")}</small></div></a>'
            f'<a class="row" href="/"><div class="grow"><b>{t("Full portal", "Portal completo")}</b><small>portal.argia.com.mx</small></div></a>'
            f'<a class="row" href="/logout"><div class="grow"><b>{t("Sign out", "Cerrar sesión")}</b></div></a>'
            f'</div><p class="note">{t("Data as of", "Datos al")} {esc(gen_mx)} MX · {t("refreshed every 5 minutes; tap the arrow at the top to reload.", "se actualiza cada 5 minutos; toca la flecha arriba para recargar.")}</p>'
            f'</section>')


def tabbar(n_alerts) -> str:
    badge = f'<i class="badge">{n_alerts}</i>' if n_alerts else ""

    def tab(key, en, es, href=None, extra=""):
        return (f'<a href="{href or "#" + key}" data-tab="{key}">{icon(key)}{extra}{t(en, es)}</a>')
    return ('<nav class="tabbar">' + tab("fleet", "Fleet", "Flota") + tab("alerts", "Alerts", "Alertas", extra=badge)
            + tab("losses", "Losses", "Pérdidas") + tab("tickets", "Tickets", "Tickets", href="/maintenance/")
            + tab("more", "More", "Más") + '</nav>')


CSS = """
:root{--teal:#05b1a9;--teal2:#05847d;--ink:#1a1d23;--muted:#6b7480;--line:#e6e8eb;--bg:#f2f3f5;--red:#d23c3c;--amber:#e39a1b;--green:#1fa870}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 padding:0 0 calc(70px + env(safe-area-inset-bottom))}
a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:5;background:rgba(255,255,255,.94);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);
 border-bottom:1px solid var(--line);padding:calc(8px + env(safe-area-inset-top)) 16px 8px;display:flex;align-items:center;gap:10px}
header .logo{height:__LOGO_PX__px;width:auto;display:block}
header .ttl{font-weight:700;letter-spacing:.14em;font-size:15px}
header .age{margin-left:auto;font-size:12px;color:var(--muted)}
header .age.old{color:var(--red);font-weight:600}
header button{border:0;background:none;color:var(--teal2);padding:6px;margin:-6px -6px -6px 0}
main{max-width:560px;margin:0 auto;padding:14px 16px 8px}
h1{font-size:26px;margin:4px 0 2px}
.kick{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--teal2);margin:4px 0}
.big{font-size:40px;font-weight:700;margin:0 0 10px}.big span{font-size:18px;color:var(--muted);font-weight:600}
.sub{color:var(--muted);font-size:13px;margin:0 0 12px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.sec{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:18px 2px 6px}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}.tiles.three{grid-template-columns:1fr 1fr 1fr}
.tile{background:#fff;border-radius:14px;padding:12px;font-size:12px;color:var(--muted);display:block}
.tile b{display:block;font-size:20px;color:var(--ink)}.tile.red b{color:var(--red)}
.card{background:#fff;border-radius:14px;margin-bottom:12px;overflow:hidden}.card.pad{padding:12px 14px}
.row{display:flex;align-items:center;gap:10px;padding:12px 14px;border-top:1px solid var(--line)}.row:first-child{border-top:0}
.grow{flex:1;min-width:0}.grow b{display:block}.grow small,.right small{display:block;color:var(--muted);font-size:12px}
.right{text-align:right;white-space:nowrap}
.dot{width:10px;height:10px;border-radius:50%;background:#b8bec6;flex:none}
.st-bad .dot{background:var(--red)}.st-warn .dot{background:var(--amber)}.st-good .dot{background:var(--green)}
.st-bad .stt{color:var(--red)}.st-warn .stt{color:#b26a00}
.what{display:block;font-size:14px}.inv{font-family:ui-monospace,Menlo,monospace}
.pill{font-size:11px;font-weight:700;border-radius:6px;padding:2px 6px;flex:none;align-self:flex-start;margin-top:2px}
.pill.crit{background:#fde8e8;color:var(--red)}.pill.warn{background:#fff4e0;color:#b26a00}
.back{display:inline-flex;align-items:center;gap:2px;color:var(--teal2);font-weight:600;margin:0 0 4px -4px}
.lossc{background:#fdeeee}.loss{font-size:30px;font-weight:700;color:var(--red);margin:2px 0 6px}.loss span{font-size:15px}
.lossc ul{list-style:none;margin:0;padding:0}.lossc li{display:flex;justify-content:space-between;font-size:14px;padding:3px 0}
.note{font-size:12px;color:var(--muted);margin:8px 2px}
.drow{display:flex;align-items:center;gap:10px;padding:7px 0;font-size:14px}
.dd{width:56px;font-weight:600}.dd.wide{width:130px;font-weight:500}
.bar{flex:1;height:8px;background:#eceef0;border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--teal)}.bar i.bad{background:var(--red)}
.dv{min-width:92px;text-align:right;font-variant-numeric:tabular-nums}.dv.red{color:var(--red);font-weight:600}
.btns{display:flex;gap:10px;margin:14px 0}
.btn{flex:1;text-align:center;background:#fff;border:1px solid var(--line);border-radius:12px;padding:11px;font:600 14px inherit;color:var(--teal2)}
.btn.sm{flex:none;padding:7px 14px}
.seg button{border:1px solid var(--line);background:#fff;padding:5px 10px;font:600 13px inherit}.seg button:first-child{border-radius:8px 0 0 8px}
.seg button:last-child{border-radius:0 8px 8px 0}.seg button.on{background:var(--teal2);color:#fff;border-color:var(--teal2)}
.empty{color:var(--muted);padding:14px;margin:0}
.muted{color:var(--muted)}
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:5;display:flex;justify-content:space-around;background:rgba(255,255,255,.96);
 -webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);border-top:1px solid var(--line);padding:6px 4px calc(6px + env(safe-area-inset-bottom))}
.tabbar a{position:relative;display:flex;flex-direction:column;align-items:center;gap:2px;font-size:10.5px;color:#8a929c;min-width:60px}
.tabbar a.on{color:var(--teal2);font-weight:600}
.badge{position:absolute;top:-4px;left:50%;margin-left:6px;background:var(--red);color:#fff;font:700 10px/16px sans-serif;min-width:16px;height:16px;border-radius:8px;text-align:center;padding:0 4px;font-style:normal}
[hidden]{display:none!important}
"""

JS = """
var GEN=%(gen)d;
function L(){try{return localStorage.getItem('argia_lang')||'';}catch(e){return '';}}
function setLang(l,save){document.querySelectorAll('[data-en]').forEach(function(e){e.textContent=l==='es'?e.dataset.es:e.dataset.en;});
 document.documentElement.lang=l;document.querySelectorAll('.seg button').forEach(function(b){b.classList.toggle('on',b.dataset.l===l);});
 try{localStorage.setItem('argia_lang',l);}catch(e){}
 if(save){fetch('/session/lang',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({lang:l})}).catch(function(){});}}
function show(){var h=location.hash.slice(1)||'fleet';var el=document.getElementById('v-'+h)||document.getElementById('v-fleet');
 document.querySelectorAll('section.v').forEach(function(s){s.hidden=(s!==el);});
 document.querySelectorAll('.tabbar a[data-tab]').forEach(function(a){a.classList.toggle('on',a.dataset.tab===el.dataset.tab);});
 window.scrollTo(0,0);}
function goBack(){if(history.length>1){history.back();return false;}return true;}
function age(){var m=Math.max(0,Math.round((Date.now()/1000-GEN)/60));var a=document.getElementById('age');var es=(document.documentElement.lang==='es');
 a.textContent=m<1?(es?'ahora':'just now'):(es?('hace '+m+' min'):(m+' min ago'));a.classList.toggle('old',m>20);}
function standalone(){return window.navigator.standalone===true||(window.matchMedia&&matchMedia('(display-mode: standalone)').matches);}
function say(en,es){var o=document.getElementById('note-out');o.textContent=(document.documentElement.lang==='es')?es:en;}
async function testNote(){
 try{
  if(!('serviceWorker' in navigator)){say('This browser cannot show app notifications.','Este navegador no puede mostrar notificaciones.');return;}
  if(!('Notification' in window)){say(standalone()?'Notifications need iOS 16.4 or newer.':'Add ARGIA to the Home Screen first, then open it from the icon and try again.',
    standalone()?'Las notificaciones requieren iOS 16.4 o posterior.':'Primero agrega ARGIA a la pantalla de inicio, ábrela desde el icono y vuelve a intentar.');return;}
  var p=await Notification.requestPermission();
  if(p!=='granted'){say('Notifications are not allowed ('+p+'). Settings > Notifications > ARGIA.','Notificaciones no permitidas ('+p+'). Ajustes > Notificaciones > ARGIA.');return;}
  var reg=await navigator.serviceWorker.ready;
  var es=(document.documentElement.lang==='es');
  await reg.showNotification(es?'ARGIA - prueba':'ARGIA - test',{body:es?'Así llegaría una alerta CRÍTICA. Tócala para abrir las alertas.':'This is how a CRITICAL alert would arrive. Tap it to open the alerts.',
   icon:'/apple-touch-icon.png',tag:'argia-test',data:{url:'/app/#alerts'}});
  say('Sent. Lock the phone or leave the app to see it arrive.','Enviada. Bloquea el teléfono o sal de la app para verla llegar.');
 }catch(e){say('Did not work: '+e,'No funcionó: '+e);}}
window.addEventListener('hashchange',show);
document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible'){if(Date.now()/1000-GEN>300){location.reload();}else{age();}}});
setLang(L()||((navigator.language||'').slice(0,2)==='es'?'es':'en'));show();age();setInterval(age,30000);
if(standalone()){document.getElementById('install').hidden=true;}
if('serviceWorker' in navigator){navigator.serviceWorker.register('/app/sw.js',{scope:'/app/'}).catch(function(){});}
fetch('/session/whoami',{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){
 if(d.name){var w=document.getElementById('who');w.dataset.en='Signed in as '+d.name;w.dataset.es='Sesión de '+d.name;w.textContent=(document.documentElement.lang==='es')?w.dataset.es:w.dataset.en;}
 if(!L()&&d.lang){setLang(d.lang);}}).catch(function(){});
"""


def render(plants, gen_mx: str, gen_epoch: int) -> str:
    """The whole app, one document: every screen is a <section>, the tab bar
    and #hash links switch between them without a round trip."""
    plants = list(plants or [])
    n_alerts = len(all_alerts(plants))
    hhmm = gen_mx[11:16] if len(gen_mx) >= 16 else gen_mx
    head = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<meta name="robots" content="noindex,nofollow">'
            '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-title" content="ARGIA">'
            '<meta name="apple-mobile-web-app-status-bar-style" content="default">'
            f'<meta name="theme-color" content="{THEME}">'
            '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
            '<link rel="icon" href="/favicon.png">'
            '<link rel="manifest" href="/app/manifest.webmanifest" crossorigin="use-credentials">'
            f'<title>ARGIA</title><style>{CSS.replace("__LOGO_PX__", str(LOGO_PX))}</style></head><body>')
    logo = (f'<img class="logo" src="{LOGO_URI}" alt="{esc(LOGO_ALT)}" height="{LOGO_PX}">' if LOGO_URI
            else '<span class="ttl">ARGIA</span>')
    top = (f'<header>{logo}<span class="age" id="age"></span>'
           f'<button type="button" onclick="location.reload()" aria-label="reload">{icon("refresh", 20)}</button></header>')
    views = (fleet_view(plants, hhmm) + "".join(plant_view(p) for p in sorted_plants(plants))
             + alerts_view(plants) + losses_view(plants) + more_view(gen_mx))
    return (head + top + f'<main>{views}</main>' + tabbar(n_alerts)
            + f'<script>{JS % {"gen": int(gen_epoch)}}</script></body></html>')
