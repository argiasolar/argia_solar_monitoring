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


# v273: the app links to NO portal page. Tomasz on his iPhone, 2026-09-29:
# target=_blank did not open Safari (v271), the plant report turned the Home
# Screen app white (v272), and the full plant page is a desktop layout cut off
# at the right. What the phone needs from those pages is on the app's own
# screens instead (the inverter table on each plant screen). Tickets are read
# here and changed on the computer. test_app_view pins "no link out".
READ_ONLY_NOTE = ('<p class="note">' + t("To open, change or add a ticket, use the portal on a computer.",
                                         "Para abrir, cambiar o crear un ticket, usa el portal en una computadora.") + '</p>')


# ------------------------------------------------------ v274 performance
# Tomasz, 2026-09-29: "add tile on main page showing availability and
# performance ... on the plant level we should see the daily performance in %
# so I know if it works properly or lags".
PR_BANDS = (0.75, 0.65)          # PR 30 d: >= 0.75 green, >= 0.65 amber - the portal's bands
AVAIL_BANDS = (0.98, 0.95)       # availability 30 d (IEC 63019) - the portal's bands
TODAY_BANDS = (0.90, 0.70)       # today vs expected - the portal's month-to-date bands
TODAY_MIN_IRR = 50.0             # W/m2: dawn / dusk hours are too small to judge
DAY_HOURS = (6, 20)              # MX hours [from, to): monitoring_gen.WINDOW. pio06 2026-09-29: SLP2 had a
                                 # 3 a.m. irradiance sample - a night value must not become expected energy
TODAY_MIN_EXP_H = 0.10           # expected kWh per kWp (~ one weak morning hour) before a percentage means anything


def tip(inner, en, es, tag="span", cls="") -> str:
    """v276: a figure with its explanation - tap on the phone (JS shows it in
    the tip box), hover on a computer (title). Tomasz, 2026-09-30: 'Add to all
    numbers that are not 100% a tool tip or some mouse over explanation'."""
    c = f"tip {cls}".strip()
    return (f'<{tag} class="{c}" title="{esc(en)}" data-tip-en="{esc(en)}" data-tip-es="{esc(es)}">{inner}</{tag}>')


TIP_TODAY = ("Today vs expected = energy in today's completed daylight hours against measured irradiance x kWp x the "
             "plant's PR baseline, up to the last hour with data (a data gap is not counted as a loss). "
             "Green >= 90%, amber >= 70%.",
             "Hoy vs esperado = energía de las horas completas de hoy contra irradiancia medida x kWp x el PR base de la "
             "planta, hasta la última hora con datos (un corte de datos no cuenta como pérdida). Verde >= 90%, ámbar >= 70%.")
TIP_PR = ("PR 30 days = performance ratio: energy produced / (irradiation x kWp) over the last 30 days, "
          "kWp-weighted for the fleet. Green >= 0.75, amber >= 0.65.",
          "PR 30 días = relación de desempeño: energía producida / (irradiación x kWp) en los últimos 30 días, "
          "ponderado por kWp en la flota. Verde >= 0.75, ámbar >= 0.65.")
TIP_AV = ("Availability 30 days = share of daylight time the inverters were producing (IEC 63019), last 30 days. "
          "Green >= 98%, amber >= 95%.",
          "Disponibilidad 30 días = parte del tiempo con luz en que los inversores produjeron (IEC 63019), últimos 30 días. "
          "Verde >= 98%, ámbar >= 95%.")
TIP_KW = ("Power now = sum of the inverters heard from in the last 30 minutes; '-' when none reported.",
          "Potencia ahora = suma de los inversores que reportaron en los últimos 30 minutos; '-' si ninguno reportó.")
TIP_KWH = ("kWh today = the inverters' own day counters, as last reported.",
           "kWh hoy = los contadores diarios de los inversores, según el último reporte.")
TIP_INV = ("Inverters live = inverters that reported in the last 30 minutes / inverters configured.",
           "Inversores = inversores que reportaron en los últimos 30 minutos / inversores configurados.")
TIP_TEMP = ("Temperature now / today's peak, inside the inverter. Amber >= 65 C, red >= 75 C.",
            "Temperatura ahora / pico de hoy, dentro del inversor. Ámbar >= 65 C, rojo >= 75 C.")
TIP_PEER = ("% of peers = this inverter's kWh per rated kW today vs the median of the plant's other inverters. "
            "Amber < 85%, red < 70%.",
            "% de pares = kWh por kW nominal de este inversor hoy vs la mediana de los demás inversores de la planta. "
            "Ámbar < 85%, rojo < 70%.")
TIP_CAUSE = {
    "unavailability": ("Unavailability = inverters at 0 W in daylight (measured), or no data while the vendor counters show "
                       "no production. A data gap where the counters prove production is not a loss.",
                       "Indisponibilidad = inversores en 0 W con luz (medido), o sin datos mientras los contadores muestran "
                       "que no hubo producción. Un corte de datos con producción probada no es pérdida."),
    "overheating": ("Overheating = derating measured against cooler peer inverters in the same hours.",
                    "Sobrecalentamiento = reducción medida contra inversores pares más frescos en las mismas horas."),
    "underperformance": ("Underperformance = the plant ran but made less than expected, not explained by 0 W or heat "
                         "(soiling, strings, clipping, a slow inverter). Counted only on days the shortfall reaches 10% of "
                         "expected; smaller differences are the normal 4.8% day-to-day scatter of the estimate.",
                         "Bajo desempeño = la planta operó pero produjo menos, sin explicación por 0 W o calor (suciedad, "
                         "strings, recorte, inversor lento). Solo cuenta los días en que la diferencia llega al 10%; "
                         "diferencias menores son la variación normal de 4.8% de la estimación."),
}


TIP_TICKET = ("Lost while open = the losses of each day since the ticket was opened (same method as Losses): a plant "
              "ticket carries the plant's daily loss, an inverter ticket only that inverter's shortfall vs its peers. "
              "Data gaps proved by the meter readings are not counted.",
              "Perdido mientras abierto = las pérdidas de cada día desde que se abrió el ticket (mismo método que Pérdidas): "
              "un ticket de planta lleva la pérdida diaria de la planta, uno de inversor solo el faltante de ese inversor "
              "vs sus pares. Los cortes de datos probados por las lecturas no cuentan.")


def loss_period_tip(L):
    """(EN, ES) for a 30-day loss total: what it includes and what it does not."""
    c, tl, n = L.get("catchup") or 0.0, L.get("tolerance") or 0.0, L.get("days") or 0
    return (f"Sum of the daily losses over {n} days (unavailability + overheating + underperformance) at the PPA tariff. "
            f"Not counted: {c:,.0f} kWh the vendor's lifetime counter proved were produced during data gaps, and "
            f"{tl:,.0f} kWh of day-to-day scatter under 10%. Tap a day below for its calculation.",
            f"Suma de las pérdidas diarias de {n} días (indisponibilidad + sobrecalentamiento + bajo desempeño) a la tarifa PPA. "
            f"No se cuentan: {c:,.0f} kWh que el contador total probó producidos durante cortes de datos, ni "
            f"{tl:,.0f} kWh de variación diaria menor al 10%. Toque un día abajo para ver su cálculo.")


def band(v, bands):
    if v is None:
        return ""
    return "good" if v >= bands[0] else ("warn" if v >= bands[1] else "bad")


def today_vs_expected(hourly_by_sn, irr_by_hour, kwp, pr, now_hour):
    """(ratio, actual kWh, expected kWh, hours) for today so far. Pure.

    Expected per hour = measured irradiance (W/m2) / 1000 x kWp x the plant's
    PR baseline - the dashed 'theoretical' line of the portal's intraday chart.
    Only COMPLETE hours count (before ``now_hour``), only hours with at least
    TODAY_MIN_IRR, and only up to the last hour any inverter reported: a data
    gap at the end is 'no data yet' (the status says so), not lost energy.
    Hours inside that span where an inverter sent nothing count as 0 for it -
    an inverter that stopped IS the lag this number is for."""
    reported = [h for by_h in (hourly_by_sn or {}).values() for h in by_h]
    if not reported or not kwp or not pr:
        return None, None, None, 0
    last = min(max(reported), now_hour - 1)
    hours = [h for h, irr in (irr_by_hour or {}).items()
             if irr is not None and irr >= TODAY_MIN_IRR and h <= last and DAY_HOURS[0] <= h < DAY_HOURS[1]]
    if not hours:
        return None, None, None, 0
    exp = sum(irr_by_hour[h] * kwp * pr / 1000.0 for h in hours)
    act = sum((by_h.get(h) or 0.0) for by_h in hourly_by_sn.values() for h in hours)
    if exp < TODAY_MIN_EXP_H * kwp:
        return None, act, exp, len(hours)
    return act / exp, act, exp, len(hours)


def weighted(plants, key):
    """kWp-weighted mean of a per-plant ratio (the portal's fleet tiles)."""
    w = sum(p["kwp"] for p in plants if p.get(key) is not None and p.get("kwp"))
    return (sum(p[key] * p["kwp"] for p in plants if p.get(key) is not None and p.get("kwp")) / w) if w else None


def pct(v, d=0) -> str:
    return "-" if v is None else f"{100 * v:,.{d}f}%"


def perf_tiles(pr, av, today, today_note_en="", today_note_es="") -> str:
    ten, tes = TIP_TODAY
    if today_note_en:
        ten, tes = f"{today_note_en} {ten}", f"{today_note_es} {tes}"
    return (f'<div class="tiles three">'
            f'<div class="tile t-{band(today, TODAY_BANDS)}">{tip(pct(today), ten, tes, "b")}{t("today vs expected", "hoy vs esperado")}</div>'
            f'<div class="tile t-{band(pr, PR_BANDS)}">{tip("-" if pr is None else f"{pr:.2f}", *TIP_PR, tag="b")}{t("PR 30 days", "PR 30 días")}</div>'
            f'<div class="tile t-{band(av, AVAIL_BANDS)}">{tip(pct(av, 1), *TIP_AV, tag="b")}{t("availability 30 d", "disponibilidad 30 d")}</div>'
            f'</div>')


PERF_NOTE = ('<p class="note">' + t(
    "Today vs expected = energy in the completed hours against irradiance x kWp x the plant's PR baseline "
    "(green >= 90%, amber >= 70%). PR 30 days: green >= 0.75, amber >= 0.65. Availability: green >= 98%, amber >= 95%.",
    "Hoy vs esperado = energía de las horas completas contra irradiancia x kWp x el PR base de la planta "
    "(verde >= 90%, ámbar >= 70%). PR 30 días: verde >= 0.75, ámbar >= 0.65. Disponibilidad: verde >= 98%, ámbar >= 95%.") + '</p>')


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
    act = sum(p["today_act"] for p in plants if p.get("today_pct") is not None)
    exp = sum(p["today_exp"] for p in plants if p.get("today_pct") is not None)
    fleet_today = (act / exp) if exp else None
    fleet_note = ((f"PPA fleet today so far: {act:,.0f} of {exp:,.0f} kWh expected.",
                   f"Flota PPA hoy hasta ahora: {act:,.0f} de {exp:,.0f} kWh esperados.") if exp else ("", ""))
    cards = []
    for p in sorted_plants(plants):
        cards.append(
            f'<a class="row pc st-{esc(p.get("state"))}" href="#p-{esc(p["slug"])}"><span class="dot"></span>'
            f'<div class="grow"><b>{esc(p["name"])}</b>'
            f'<small>{esc(p.get("where") or "")}{" · " if p.get("where") else ""}{esc(p.get("portfolio") or "")}'
            f' · {num(p.get("kwp"))} kWp</small>'
            f'<small class="stt">{t(p.get("state_en") or "", p.get("state_es"))}</small></div>'
            f'<div class="right"><b>{num(p.get("power_kw"))} kW</b><small>{num(p.get("today_kwh"))} kWh</small>'
            + tip(f'{pct(p.get("today_pct"))} {t("today", "hoy")}', *today_tip(p), tag="small",
                  cls=f'tp t-{band(p.get("today_pct"), TODAY_BANDS)}') + '</div></a>')
    return (f'<section class="v" id="v-fleet" data-tab="fleet">'
            f'<p class="kick">{t("PPA fleet now", "Flota PPA ahora")} · {esc(gen_hhmm)} MX</p>'
            f'<p class="big">{num(power)} <span>kW</span></p>'
            f'<div class="tiles"><div class="tile">{tip(num(today), *TIP_KWH, tag="b")}{t("kWh today", "kWh hoy")}</div>'
            f'<a class="tile{" red" if crit else ""}" href="#alerts"><b>{crit} {t("critical", "críticas")}</b>'
            f'{len(alerts) - crit} {t("warnings", "avisos")}</a></div>'
            f'{perf_tiles(weighted(plants, "pr30"), weighted(plants, "avail30"), fleet_today, *fleet_note)}'
            f'<div class="card list">{"".join(cards) or t("No plants.", "Sin plantas.", tag="p", cls="empty")}</div>'
            f'{PERF_NOTE}</section>')


def loss_card(p) -> str:
    L = p.get("loss30") or {}
    if not L or not L.get("days"):
        return (f'<div class="card pad">{t("Lost production: no loss figures for this plant yet.", "Producción perdida: aún sin cifras para esta planta.", tag="p", cls="muted")}</div>')
    ppa = L.get("mxn") is not None
    ptip = loss_period_tip(L)
    head = (f'<p class="loss">{tip(money(L["mxn"]), *ptip)} <span>MXN</span></p>' if ppa
            else f'<p class="loss">{tip(num(L.get("kwh")), *ptip)} <span>kWh</span></p>')
    parts = []
    for key, en, es in CAUSES:
        v = L.get(f"{key}_mxn") if ppa else L.get(key)
        if v and v >= (1 if ppa else 0.5):
            parts.append(f'<li>{t(en, es)}{tip(money(v) if ppa else num(v) + " kWh", *TIP_CAUSE[key], tag="b")}</li>')
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
        val = ("-" if pct is None else f"{pct:,.0f}%") + ("*" if d.get("catchup") else "") + tail
        dtip = d.get("tip") or (f"Actual {num(e)} kWh of {num(x)} kWh expected for this day.",
                                f"Real {num(e)} kWh de {num(x)} kWh esperados para este día.")
        cell = tip(val, *dtip, cls="dv" + (" red" if bad else ""))
        rows.append(f'<div class="drow"><span class="dd">{day_label(d.get("date") or "")}</span>'
                    f'<span class="bar"><i class="{"bad" if bad else ""}" style="width:{w:.0f}%"></i></span>'
                    f'{cell}</div>')
    if not rows:
        return ""
    return (f'<p class="sec">{t("Last 7 days - actual vs expected, and what it cost", "Últimos 7 días - real vs esperado, y lo que costó")}</p>'
            f'<div class="card pad">{"".join(rows)}</div>'
            f'<p class="note">{t("Tap a day for how it was calculated. * = the data link was down; the energy was proved by the next night meter reading.", "Toque un día para ver el cálculo. * = la conexión estaba caída; la energía se probó con la lectura de la noche siguiente.")}</p>')


INV_STATE = {"ok": ("OK", "OK", "ok"), "fault": ("fault", "falla", "warn"),
             "stale": ("no data > 30 min", "sin datos > 30 min", "crit"),
             "silent": ("silent today", "sin datos hoy", "crit")}


def inverters_card(p) -> str:
    """v273: what the full plant page's inverter table told you, phone-sized:
    each inverter's status, power now, kWh today, temperature now / day peak
    and kWh per kW against its peers."""
    rows = []
    for i in p.get("inverters") or []:
        en, es, cls = INV_STATE.get(i.get("state"), INV_STATE["ok"])
        bits = []
        if i.get("peak") is not None or i.get("temp") is not None:
            tn = "-" if i.get("temp") is None else f'{i["temp"]:.0f}'
            tp = "-" if i.get("peak") is None else f'{i["peak"]:.0f}'
            tc = {"bad": "red", "warn": "amber"}.get(i.get("temp_cls"), "")
            bits.append(tip(f"{tn} / {tp} °C", *TIP_TEMP, cls=tc))
        if i.get("peer") is not None:
            pc = {"bad": "red", "warn": "amber"}.get(i.get("peer_cls"), "")
            bits.append(tip(f'{100 * i["peer"]:.0f}% {t("of peers", "de pares")}', *TIP_PEER, cls=pc))
        if i.get("state") == "stale" and i.get("last"):
            bits.append(f'{t("last data", "último dato")} {esc(i["last"])}')
        rows.append(f'<div class="row invr"><div class="grow"><b>{esc(i.get("label"))}</b>'
                    f'<small class="inv">{esc(i.get("sn"))}</small>'
                    f'<span class="pills">{t(en, es, cls="pill " + cls)}</span>'
                    f'<small>{" · ".join(bits)}</small></div>'
                    f'<div class="right">{tip(num(i.get("power_kw"), 1) + " kW", *TIP_KW, tag="b")}'
                    f'{tip(num(i.get("today_kwh"), 1) + " kWh", *TIP_KWH, tag="small")}</div></div>')
    if not rows:
        return ""
    return (f'<p class="sec">{t("Inverters", "Inversores")}</p><div class="card list">{"".join(rows)}</div>'
            f'<p class="note">{t("°C = now / day peak (amber ≥ 65, red ≥ 75). % of peers = kWh per rated kW vs the other inverters (amber < 85%, red < 70%).", "°C = ahora / pico del día (ámbar ≥ 65, rojo ≥ 75). % de pares = kWh por kW nominal vs los demás inversores (ámbar < 85%, rojo < 70%).")}</p>')


def plant_view(p) -> str:
    al = [alert_row(p, a, with_plant=False) for a in (p.get("alerts") or [])]
    alerts = (f'<p class="sec">{t("Open alerts", "Alertas abiertas")}</p><div class="card list">{"".join(al)}</div>' if al else "")
    slug = esc(p["slug"])
    live_txt = f"{num(p.get('inv_live'))}/{num(p.get('inv_total'))}"
    return (f'<section class="v" id="v-p-{slug}" data-tab="fleet" hidden>'
            f'<a class="back" href="#fleet" onclick="return goBack()">{icon("back", 18)}{t("Fleet", "Flota")}</a>'
            f'<h1>{esc(p["name"])}</h1>'
            f'<p class="sub st-{esc(p.get("state"))}"><span class="dot"></span>{t(p.get("state_en") or "", p.get("state_es"))}'
            f' · {esc(p.get("where") or "")} · {esc(p.get("portfolio") or "")} · {num(p.get("kwp"))} kWp</p>'
            f'<div class="tiles three"><div class="tile">{tip(num(p.get("power_kw")), *TIP_KW, tag="b")}{t("kW now", "kW ahora")}</div>'
            f'<div class="tile">{tip(num(p.get("today_kwh")), *TIP_KWH, tag="b")}{t("kWh today", "kWh hoy")}</div>'
            f'<div class="tile">{tip(live_txt, *TIP_INV, tag="b")}{t("inverters live", "inversores")}</div></div>'
            f'{perf_tiles(p.get("pr30"), p.get("avail30"), p.get("today_pct"), *today_tip(p))}'
            f'{today_line(p)}'
            f'{loss_card(p)}{inverters_card(p)}{days_card(p)}{alerts}{PERF_NOTE}'
            f'</section>')


def today_tip(p):
    """(EN, ES) 'Today so far: X of Y kWh expected (N complete hours).'"""
    if p.get("today_exp") is None:
        return ("Not enough daylight yet today to judge.", "Aún no hay suficiente luz hoy para juzgar.")
    return (f"Today so far: {num(p.get('today_act'))} of {num(p.get('today_exp'))} kWh expected "
            f"({p.get('today_hours', 0)} complete hours).",
            f"Hoy hasta ahora: {num(p.get('today_act'))} de {num(p.get('today_exp'))} kWh esperados "
            f"({p.get('today_hours', 0)} horas completas).")


def today_line(p) -> str:
    if p.get("today_exp") is None:
        return ""
    return (f'<p class="note">{t("Today so far", "Hoy hasta ahora")}: {num(p.get("today_act"))} kWh '
            f'{t("of", "de")} {num(p.get("today_exp"))} kWh {t("expected", "esperados")} '
            f'({p.get("today_hours", 0)} {t("complete hours", "horas completas")})</p>')


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
                   f'{tip(money(cause_mxn[k]), *TIP_CAUSE[k], cls="dv")}</div>' for k, en, es in CAUSES)
    rows = []
    for p in ranked:
        L = p["loss30"]
        val = money(L["mxn"]) if L.get("mxn") is not None else f'{num(L.get("kwh"))} kWh'
        rows.append(f'<a class="row" href="#p-{esc(p["slug"])}"><div class="grow"><b>{esc(p["name"])}</b>'
                    f'<small>{num(L.get("kwh"))} kWh {t("lost", "perdidos")}</small></div><div class="right">{tip(val, *loss_period_tip(L), tag="b")}</div></a>')
    return (f'<section class="v" id="v-losses" data-tab="losses" hidden><h1>{t("Losses", "Pérdidas")}</h1>'
            f'<p class="sub">{t(f"Last {days} days, all PPA plants", f"Últimos {days} días, todas las plantas PPA")}</p>'
            f'<div class="tiles"><div class="tile red"><b>{money(tot_mxn)}</b>{t("MXN lost", "MXN perdidos")}</div>'
            f'<div class="tile"><b>{num(tot_kwh)}</b>{t("kWh lost", "kWh perdidos")}</div></div>'
            f'<p class="sec">{t("By cause (MXN)", "Por causa (MXN)")}</p><div class="card pad">{bars}</div>'
            f'<p class="sec">{t("By plant", "Por planta")}</p>'
            f'<div class="card list">{"".join(rows) or t("No loss figures yet.", "Aún sin cifras.", tag="p", cls="empty")}</div>'
            f'</section>')


PRIO_CLS = {"P1": "crit", "P2": "warn", "P3": "p3", "P4": "p3"}


def ticket_row(k) -> str:
    """One open ticket. The ticket number is left out on purpose: it carries
    the plant code (TK-NL1-0001) and the app speaks customer names."""
    lost = (money(k["lost_mxn"]) if k.get("lost_mxn") is not None
            else (f'{num(k["lost_kwh"])} kWh' if k.get("lost_kwh") else "-"))
    sla = f' · {t("over SLA", "fuera de SLA", cls="red")}' if k.get("over_sla") else ""
    who = esc(k["assignee"]) if k.get("assignee") else t("unassigned", "sin asignar")
    inv = f'{esc(k["inverter"])} · ' if k.get("inverter") else ""
    return (f'<div class="row tk">'
            f'<div class="grow"><span class="pills"><span class="pill {PRIO_CLS.get(k.get("priority"), "p3")}">{esc(k.get("priority"))}</span>'
            f'{t(k.get("status_en") or "", k.get("status_es"), cls="pill st")}</span>'
            f'<b>{esc(k["plant"])}</b><span class="what">{esc(k.get("title") or "")}</span>'
            f'<small>{inv}{t("opened", "abierto")} {day_label(k.get("opened") or "")} · {who}{sla}</small></div>'
            f'<div class="right">{tip(lost, *TIP_TICKET, tag="b", cls="red" if (k.get("lost_mxn") or 0) >= 1 else "")}'
            f'<small>{t("lost while open", "perdido abierto")}</small></div></div>')


def tickets_view(tickets, total_mxn=None, total_kwh=None) -> str:
    """v271: the open tickets INSIDE the app (PPA plants), each with what it has
    cost so far - the same figure as the tickets page. Opening one, or a new
    ticket, goes to Safari (the tickets page is where they are edited)."""
    tickets = list(tickets or [])
    over = sum(1 for k in tickets if k.get("over_sla"))
    total = money(total_mxn) if total_mxn is not None else (f"{num(total_kwh)} kWh" if total_kwh else "$0")
    rows = "".join(ticket_row(k) for k in tickets) or t("No open tickets.", "Sin tickets abiertos.", tag="p", cls="empty")
    return (f'<section class="v" id="v-tickets" data-tab="tickets" hidden><h1>{t("Tickets", "Tickets")}</h1>'
            f'<p class="sub">{len(tickets)} {t("open, PPA plants", "abiertos, plantas PPA")}</p>'
            f'<div class="tiles"><div class="tile{" red" if over else ""}"><b>{over}</b>{t("over SLA", "fuera de SLA")}</div>'
            f'<div class="tile red"><b>{total}</b>{t("lost while open (each plant-day once)", "perdido mientras abiertos (cada planta-día una vez)")}</div></div>'
            f'<div class="card list">{rows}</div>'
            f'{READ_ONLY_NOTE}</section>')


def more_view(gen_mx) -> str:
    return (f'<section class="v" id="v-more" data-tab="more" hidden><h1>{t("More", "Más")}</h1>'
            f'<p class="sub" id="who"></p>'
            f'<div class="card pad" id="install">{t("Install on the iPhone: open this page in Safari, tap Share, then Add to Home Screen. The ARGIA icon opens it full screen.", "Instalar en el iPhone: abre esta página en Safari, toca Compartir y luego Agregar a inicio. El icono de ARGIA la abre en pantalla completa.", tag="p")}</div>'
            f'<div class="card list">'
            f'<div class="row"><div class="grow"><b>{t("Language", "Idioma")}</b></div><span class="seg"><button data-l="en" onclick="setLang(\'en\',true)">EN</button><button data-l="es" onclick="setLang(\'es\',true)">ES</button></span></div>'
            f'<div class="row"><div class="grow"><b>{t("Test a notification", "Probar una notificación")}</b>'
            f'<small id="note-out">{t("Shows how a critical alert would arrive on this phone.", "Muestra cómo llegaría una alerta crítica a este teléfono.")}</small></div>'
            f'<button class="btn sm" onclick="testNote()">{t("Send", "Enviar")}</button></div>'
            f'</div><p class="note">{t("Data as of", "Datos al")} {esc(gen_mx)} MX · {t("refreshed every 5 minutes; tap the arrow at the top to reload.", "se actualiza cada 5 minutos; toca la flecha arriba para recargar.")}</p>'
            f'</section>')


def tabbar(n_alerts) -> str:
    badge = f'<i class="badge">{n_alerts}</i>' if n_alerts else ""

    def tab(key, en, es, href=None, extra=""):
        return (f'<a href="{href or "#" + key}" data-tab="{key}">{icon(key)}{extra}{t(en, es)}</a>')
    return ('<nav class="tabbar">' + tab("fleet", "Fleet", "Flota") + tab("alerts", "Alerts", "Alertas", extra=badge)
            + tab("losses", "Losses", "Pérdidas") + tab("tickets", "Tickets", "Tickets")
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
.t-good b,.tp.t-good{color:var(--green)!important}.t-warn b,.tp.t-warn{color:#b26a00!important}.t-bad b,.tp.t-bad{color:var(--red)!important}
.tp{font-weight:600}
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
.pills{display:flex;gap:6px;margin-bottom:3px}.pill.p3{background:#eceef0;color:var(--muted)}.pill.st{background:#e6f7f5;color:var(--teal2)}
.amber{color:#b26a00}.pill.ok{background:#e6f7f5;color:var(--teal2)}.row.invr .pills{margin:3px 0 1px}
.red{color:var(--red)}.tk .right b.red{color:var(--red)}
.muted{color:var(--muted)}
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:5;display:flex;justify-content:space-around;background:rgba(255,255,255,.96);
 -webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);border-top:1px solid var(--line);padding:6px 4px calc(6px + env(safe-area-inset-bottom))}
.tabbar a{position:relative;display:flex;flex-direction:column;align-items:center;gap:2px;font-size:10.5px;color:#8a929c;min-width:60px}
.tabbar a.on{color:var(--teal2);font-weight:600}
.badge{position:absolute;top:-4px;left:50%;margin-left:6px;background:var(--red);color:#fff;font:700 10px/16px sans-serif;min-width:16px;height:16px;border-radius:8px;text-align:center;padding:0 4px;font-style:normal}
[hidden]{display:none!important}
.tip{border-bottom:1px dotted #9aa0a6;cursor:help}
#tipbox{position:fixed;left:12px;right:12px;bottom:calc(76px + env(safe-area-inset-bottom));z-index:20;background:#053b38;color:#fff;
 border-radius:12px;padding:12px 14px;font-size:14px;line-height:1.45;box-shadow:0 8px 24px rgba(0,0,0,.25);max-width:540px;margin:0 auto}
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
function tipShow(el){var b=document.getElementById('tipbox');var es=(document.documentElement.lang==='es');
 b.textContent=(es?el.dataset.tipEs:el.dataset.tipEn)||el.dataset.tipEn;b.hidden=false;}
document.addEventListener('click',function(ev){var el=ev.target.closest?ev.target.closest('.tip'):null;var b=document.getElementById('tipbox');
 if(el){ev.preventDefault();ev.stopPropagation();tipShow(el);return;}
 if(b&&!b.hidden){b.hidden=true;}},true);
window.addEventListener('hashchange',function(){var b=document.getElementById('tipbox');if(b)b.hidden=true;});
window.addEventListener('hashchange',show);
document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible'){if(Date.now()/1000-GEN>300){location.reload();}else{age();}}});
setLang(L()||((navigator.language||'').slice(0,2)==='es'?'es':'en'));show();age();setInterval(age,30000);
if(standalone()){document.getElementById('install').hidden=true;}
if('serviceWorker' in navigator){navigator.serviceWorker.register('/app/sw.js',{scope:'/app/'}).catch(function(){});}
fetch('/session/whoami',{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){
 if(d.name){var w=document.getElementById('who');w.dataset.en='Signed in as '+d.name;w.dataset.es='Sesión de '+d.name;w.textContent=(document.documentElement.lang==='es')?w.dataset.es:w.dataset.en;}
 if(!L()&&d.lang){setLang(d.lang);}}).catch(function(){});
"""


def render(plants, gen_mx: str, gen_epoch: int, tickets=None, tickets_total=(None, None)) -> str:
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
             + alerts_view(plants) + losses_view(plants) + tickets_view(tickets, *tickets_total) + more_view(gen_mx))
    return (head + top + f'<main>{views}</main>' + '<div id="tipbox" role="status" hidden></div>' + tabbar(n_alerts)
            + f'<script>{JS % {"gen": int(gen_epoch)}}</script></body></html>')
