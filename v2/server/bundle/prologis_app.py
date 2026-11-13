#!/usr/bin/env python3
"""ARGIA for Prologis - the customer platform at prologis.argia.com.mx (v292).

Runs on 127.0.0.1:8520 behind nginx (TLS). Its own login - separate from
the ARGIA portal: own user database, scrypt passwords, mandatory TOTP
second factor, roles, idle timeout, CSRF tokens on every form, an audit
log of every login, change, upload and download, and a data export.

    /                 overview: live portfolio, map, alerts, tickets, projects
    /map/             every site on a satellite map
    /sites/           all sites, live; /sites/<code>/ one site
    /tickets/         O&M tickets on the MSA response-time classes, and service orders
    /shop/            service shop: order cleaning, thermography, repairs... (v293)
    /admin/services/  services and prices, published or draft (admin)
    /assets/          equipment register, warranty claims, spare parts and the
                      quarterly inventory report (v320)
    /alarms/          alarm engine results, triage in 4 business hours, daily
                      review log, mail outbox and settings (v321)
    /reports/         monthly O&M report per site and portfolio (page, print,
                      spreadsheet), HSE register, design yield (v322)
    /availability/    MSA availability, incident and exclusion registers, annual
                      analysis with liquidated damages and bonus (v323)
    /projects/        constructions and onboarding pipeline
    /docs/            documentation library per site and folder
    /security/        how the platform protects Prologis data
    /admin/users/     user management (admin)   /audit/  audit log
    /export.zip       full data export (manager, admin)
    /about            the product manual (v331): no platform login, but nginx asks for
                      the readers' own HTTP Basic login (v333). A single self-contained
                      HTML file kept server-only (it carries the offer's prices).
    /maintenance/     preventive maintenance and cleaning plan: year planner, month
                      calendar, tasks, iCalendar export (v333)
    /lighting/        the LED programme (Prologis Essentials): overview by city and park with a
                      live CO2 counter, every project, customers with their logos, one page per
                      project with a satellite picture, and the buildings of 5,000 m2+ not yet on
                      LED; server-only data LIGHT_DIR/lighting.json - no file, no menu entry (v345)

Metering is labelled SAMPLE until Prologis grants SolarEdge access
(argia.prologis.metering). Customer data (registry, database, files,
logo) lives only under /opt/argia/prologis on the server.

    python3 prologis_app.py                     serve
    python3 prologis_app.py --create-admin USER "Full name" EMAIL
    python3 prologis_app.py --seed-sample-tickets
    python3 prologis_app.py --seed-sample-handover   sample handover checklists (never overwrites real rows)
    python3 prologis_app.py --clear-sample-handover  remove them
    python3 prologis_app.py --seed-catalog FILE  add missing services from a JSON list (never overwrites)
    python3 prologis_app.py --seed-parts FILE    add missing spare parts and minimums (never overwrites)
    python3 prologis_app.py --alarm-run [ISO]    one alarm engine pass (the 5-minute timer; ISO = MX time, tests)
    python3 prologis_app.py --scan-uploads       malware-scan the pending uploads (the 5-minute timer, v324)
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
import re
import unicodedata
import sys
import time
import zipfile
from typing import Dict, List, Optional, Tuple

from flask import Flask, Response, abort, g, redirect, request, send_file

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get("ARGIA_V2_DIR", "/root/argia_v2/v2"))

import argia_logo                                         # noqa: E402
import prologis_ui as UI                                  # noqa: E402
from plain_text import plain                              # noqa: E402
from argia.prologis import alarms as AL                   # noqa: E402
from argia.prologis import assets as A                    # noqa: E402
from argia.prologis import availability as AV             # noqa: E402
from argia.prologis import catalog as CAT                 # noqa: E402
from argia.prologis import handover as HO                # noqa: E402
from argia.prologis import metering as M                  # noqa: E402
from argia.prologis import monthly as MR                  # noqa: E402
from argia.prologis import registry as R                  # noqa: E402
from argia.prologis import scan as SC                     # noqa: E402
from argia.prologis import warranty as WR                 # noqa: E402
from argia.prologis import lightops as LO                 # noqa: E402
from argia.prologis import essentials as ES               # noqa: E402
from argia.prologis import sla as SLA                     # noqa: E402
from argia.prologis import store as S                     # noqa: E402
from argia.prologis import maintplan as PM            # noqa: E402  (v333)
from argia.prologis import lighting as LT                 # noqa: E402  (v345)
from argia.prologis import totp as TOTP                   # noqa: E402
from argia.prologis import xlsx as XL                     # noqa: E402
from argia.prologis import dd as DD                       # noqa: E402  (v363)
from argia.prologis import sitepins as SP                 # noqa: E402  (v369)

DATA_DIR = os.environ.get("ARGIA_PL_DIR", "/opt/argia/prologis")
FILES_DIR = os.environ.get("ARGIA_PL_FILES", os.path.join(DATA_DIR, "files"))
BRAND_DIR = os.path.join(DATA_DIR, "brand")
# v294: prologis_logo_white.png is the reversed logo (white wordmark, as on the
# Prologis buildings) for the dark green header; the colour logo stays the fallback
BRAND_FILES = ("prologis_logo.png", "prologis_logo_white.png", "prologis_mark.png")
# v363: the client's web font (a client.json pair adds the @font-face), served from here because the
# CSP allows fonts from this site only
BRAND_FONTS = {"client_font.woff2": "font/woff2"}
COOKIE = "pl_sid"
SECURE_COOKIE = os.environ.get("ARGIA_PL_INSECURE_COOKIE", "") != "1"
MAX_UPLOAD = 50 * 1024 * 1024
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".dwg", ".dxf", ".xlsx", ".xls", ".csv", ".docx",
               ".doc", ".pptx", ".zip", ".txt", ".kmz", ".mp4", ".heic", ".mov"}      # v356: .mov - phone videos of a failed fixture
PUBLIC = {"login", "login_post", "healthz", "brand", "lang", "favicon", "about"}
# v331: the public product manual (Tomasz, 28 Oct: "available to anyone without a
# need to login"). Server-only file, never in the public repo; the page is one
# self-contained document (images and fonts embedded), so its own CSP allows
# nothing from the network at all.
ABOUT_FILE = os.environ.get("ARGIA_PL_ABOUT", os.path.join(DATA_DIR, "public", "about.html"))
ABOUT_CSP = ("default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; "
             "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# v334: one picture per site (aerial view or roof photo), server-only:
# PHOTO_DIR/<SITE CODE>.jpg, shown in the map popup to signed-in users.
PHOTO_DIR = os.environ.get("ARGIA_PL_PHOTO_DIR", os.path.join(DATA_DIR, "photos"))

# v333: the solar potential layer of the map - a server-only picture (pvout.png)
# and its description (pvout.json: bounds [[south, west], [north, east]], the
# colour stops, labels, attribution), made once from PVGIS / Global Solar Atlas
# data for the customer's region. No files = no layer.
MAP_DIR = os.environ.get("ARGIA_PL_MAP_DIR", os.path.join(DATA_DIR, "map"))

# v337: the same platform for another client (irritec.argia.com.mx). A
# server-only client.json next to the data says who the client is and how the
# pages word things: {"name": "Irritec", "replace": [["from", "to"], ...]}.
# Every page (not the /about manual, not downloads) gets the pairs applied in
# order, then "Prologis" -> name. No file = the Prologis platform unchanged.
CLIENT_FILE = os.environ.get("ARGIA_PL_CLIENT", os.path.join(DATA_DIR, "client.json"))
_CLIENT_CACHE: Dict[str, object] = {"mtime": None, "cfg": None}


def client_cfg() -> Optional[Dict]:
    """The client description, or None (no file, or one that does not make
    sense - then the pages stay exactly as they are). Re-read when it changes."""
    try:
        mt = os.path.getmtime(CLIENT_FILE)
    except OSError:
        _CLIENT_CACHE.update(mtime=None, cfg=None)
        return None
    if _CLIENT_CACHE["mtime"] == mt:
        return _CLIENT_CACHE["cfg"]
    cfg = None
    try:
        with open(CLIENT_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        name = str(d.get("name") or "").strip()
        pairs = d.get("replace") or []
        ok = (re.match(r"^[A-Za-z0-9 .&'-]{2,40}$", name) and isinstance(pairs, list) and len(pairs) <= 500
              and all(isinstance(x, list) and len(x) == 2 and all(isinstance(y, str) for y in x) and x[0] for x in pairs))
        if ok:
            cfg = {"name": name, "replace": [(a, b) for a, b in pairs]}
    except (OSError, ValueError, TypeError):
        cfg = None
    _CLIENT_CACHE.update(mtime=mt, cfg=cfg)
    return cfg


def client_text(html: str) -> str:
    """Pure apart from the cached file: the page in the client's words."""
    cfg = client_cfg()
    if not cfg:
        return html
    for a, b in cfg["replace"]:
        html = html.replace(a, b)
    if cfg["name"] != "Prologis":
        html = html.replace("PROLOGIS", cfg["name"].upper()).replace("Prologis", cfg["name"])
    return html
# v300: the browser-tab icon - the Prologis globe (server-only files, first
# one present wins; none = 404 and the browser shows its default)
FAVICONS = {"favicon.png": ("prologis_favicon.png", "prologis_mark.png"),
            "favicon.ico": ("prologis_favicon.png", "prologis_mark.png"),
            "apple-touch-icon.png": ("prologis_touch.png", "prologis_mark.png")}
ICON_LINKS = ('<link rel="icon" type="image/png" href="/favicon.png">'
              '<link rel="apple-touch-icon" href="/apple-touch-icon.png">')
SAMPLE_PILL = ' <span class="pill s-info">SAMPLE</span>'

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD + 1024 * 1024


# ------------------------------------------------------------------ plumbing
def _secret() -> bytes:
    p = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(p):
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(os.urandom(32))
        os.chmod(p, 0o600)
    with open(p, "rb") as fh:
        return fh.read()


def db():
    if "db" not in g:
        g.db = S.connect()
    return g.db


@app.teardown_appcontext
def _close(_exc):
    c = g.pop("db", None)
    if c is not None:
        c.close()


def reg() -> R.Registry:
    """The site registry with the pins placed by hand in the portal (v369)."""
    rg = R.load()
    try:
        return R.apply_pins(rg, SP.pins(db()))
    except RuntimeError:                               # outside a request (no database handle): the file as is
        return rg


def ip() -> str:
    return (request.headers.get("X-Real-IP") or request.remote_addr or "")[:64]


def now_mx() -> dt.datetime:
    return (dt.datetime.now(dt.timezone.utc) + M.MX_OFFSET).replace(tzinfo=None)


def csrf_token() -> str:
    sid = request.cookies.get(COOKIE, "")
    return hmac.new(_secret(), ("csrf|" + sid).encode(), hashlib.sha256).hexdigest()[:32]


def csrf_field() -> str:
    return f'<input type="hidden" name="csrf" value="{csrf_token()}">'


def t() -> UI.T:
    lang = (g.get("user") or {}).get("lang") if g.get("user") else None
    return UI.T(request.cookies.get("pl_lang") or lang or "en")


# v376 (Tomasz, 13 Nov 2026, mrp.argia.com.mx): "turn off the security that requires authenticator for this
# moment". A dated, per-instance exception: ARGIA_PL_MFA_OFF_UNTIL=YYYY-MM-DD in that instance's unit only. Until
# that day (inclusive, Mexico City) a correct password signs in without the authenticator; the day after, MFA is
# required again by itself. Every such sign-in is audited as login_ok_no_mfa. Prologis and Irritec never set it.
MFA_OFF_UNTIL = (os.environ.get("ARGIA_PL_MFA_OFF_UNTIL") or "").strip()


def mfa_off(today: Optional[dt.date] = None, until: Optional[str] = None) -> bool:
    u = MFA_OFF_UNTIL if until is None else until
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", u or ""):
        return False
    try:
        end = dt.date.fromisoformat(u)
    except ValueError:
        return False
    today = today or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)).date()
    return today <= end


@app.before_request
def _guard():
    g.user = None
    if request.endpoint in PUBLIC or request.endpoint is None:
        return None
    sid = request.cookies.get(COOKIE, "")
    row = S.session(db(), sid, time.time())
    if not row:
        return redirect("/login?next=" + request.path)
    g.user = dict(row)
    if request.method == "POST" and request.endpoint not in ("logout",):
        if not hmac.compare_digest(request.form.get("csrf", ""), csrf_token()):
            abort(400)
    if not row["mfa_ok"] and not mfa_off() and request.endpoint not in ("mfa", "mfa_post", "logout"):
        return redirect("/mfa")
    if (row["mfa_ok"] or mfa_off()) and row["must_change"] and request.endpoint not in ("password", "password_post", "logout"):
        return redirect("/password")
    return None


@app.after_request
def _headers(r):
    if r.mimetype == "text/html" and not r.direct_passthrough:
        r.set_data(client_text(plain(r.get_data(as_text=True))))
    r.headers.setdefault("Cache-Control", "no-store")
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "same-origin"
    gimg = " https://maps.googleapis.com" if str(request.endpoint or "").startswith(("lighting", "dd")) and gmaps_key() else ""
    r.headers["Content-Security-Policy"] = ABOUT_CSP if request.endpoint == "about" else (
        "default-src 'self'; img-src 'self' data: https://server.arcgisonline.com https://cdnjs.cloudflare.com" + gimg + "; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; frame-ancestors 'none'")
    return r


def need(perm: str) -> None:
    if not S.can(g.user["role"], perm):
        abort(403)


def _logo() -> str:
    """The Prologis logo for a dark background: the reversed file when the
    server has it, else the colour logo on a white chip."""
    return _brand_img("prologis_logo_white.png", "pl rev", "Prologis") or _brand_img("prologis_logo.png", "pl", "Prologis")


def _brand_img(name: str, cls: str, alt: str) -> str:
    return (f'<img class="{cls}" src="/brand/{name}" alt="{UI.e(alt)}">'
            if os.path.exists(os.path.join(BRAND_DIR, name)) else "")


def page(title: str, body: str, on: str = "", sample: bool = False, wide: bool = True) -> Response:
    tt = t()
    u = g.user or {}
    nav = [("/", tt("Overview", "Resumen"), "home"), ("/map/", tt("Map", "Mapa"), "map"),
           ("/sites/", tt("Sites", "Sitios"), "sites"), ("/tickets/", tt("Tickets", "Tickets"), "tickets"),
           ("/maintenance/", tt("Maintenance", "Mantenimiento"), "maint"), ("/shop/", tt("Services", "Servicios"), "shop"), ("/assets/", tt("Assets", "Activos"), "assets"),
           ("/alarms/", tt("Alarms", "Alarmas"), "alarms"), ("/reports/", tt("Reports", "Reportes"), "reports"),
           ("/projects/", tt("Projects", "Proyectos"), "projects"), ("/docs/", tt("Documents", "Documentos"), "docs")]
    if lighting_ds() is not None:                       # v345: only where the server has the LED programme data
        nav.insert(nav.index(next(x for x in nav if x[2] == "projects")), ("/lighting/", tt("Lighting", "Iluminación"), "lighting"))
    if dd_pf() is not None:                             # v363: only where the server has a due diligence file
        nav.insert(2, ("/dd/", tt("Due diligence", "Due diligence"), "dd"))
        if any(p.lighting for p in dd_pf().props):     # v364: the LED part, where the file has industrial lighting
            nav.insert(3, ("/dd/lighting/", tt("Lighting", "Iluminación"), "ddlight"))
    links = "".join(f'<a href="{h}" class="{"on" if k == on else ""}">{UI.e(lbl)}</a>' for h, lbl, k in nav)
    other = "es" if tt.lang == "en" else "en"
    # v338: the account links fold into one menu under the user's name, so the main menu fits on one line
    users_link = (f'<a href="/admin/users/" class="{"on" if on == "users" else ""}">{tt("Users", "Usuarios")}</a>'
                  if S.can(u.get("role", ""), "users") else "")
    who = (f'<div class="who"><a href="/lang/{other}" title="{tt("Español", "English")}">{other.upper()}</a>'
           f'<details class="um"><summary>{UI.e(u.get("name") or u.get("username", ""))}</summary><div class="menu">{users_link}'
           f'<a href="/security/" class="{"on" if on == "security" else ""}">{tt("Security", "Seguridad")}</a>'
           f'<a href="/logout">{tt("Sign out", "Salir")}</a></div></details></div>')
    banner = (f'<div class="sample">{tt("SAMPLE DATA - simulated production until Prologis grants ARGIA access to the SolarEdge / Hark accounts. Sites, sizes and locations are real.", "DATOS DE MUESTRA - producción simulada hasta que Prologis otorgue a ARGIA acceso a SolarEdge / Hark. Sitios, tamaños y ubicaciones son reales.")}</div>'
              if sample else "")
    pl = _logo()
    html_ = f"""<!doctype html><html lang="{tt.lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>{UI.e(title)} - ARGIA for Prologis</title>{ICON_LINKS}<style>{UI.CSS}</style></head><body>
<div class="top"><div class="stripe"></div><div class="topin">
<a class="brand" href="/">{pl or UI.stripe_svg(26) + ' PROLOGIS'}<span class="x">×</span>
<img class="ar" src="{argia_logo.MARK_URI}" alt="ARGIA"><span class="tag">{tt("O&M platform", "Plataforma O&M")}</span></a>
<nav class="main">{links}</nav>{who}</div></div>{banner}
<main>{body}</main>{SORT_JS if '<table class="t' in body else ""}
<div class="foot"><span>ARGIA for Prologis · {tt("operated by", "operado por")} ARGIA - Smart Energy Solutions</span>
<span>{tt("Times in Mexico City time", "Horas en tiempo de Ciudad de México")}</span><a href="/security/">{tt("Security & data", "Seguridad y datos")}</a>{privacy_link(tt)}</div>
</body></html>"""
    return Response(html_, mimetype="text/html")


def privacy_link(tt) -> str:
    """v324: the privacy notice (published on argia.com.mx) - shown when the admin set its address."""
    try:
        url = AL.setting(db(), "privacy_url")
    except Exception:                        # noqa: BLE001  (a page must render even if the store is busy)
        url = ""
    return f'<a href="{UI.e(url)}" rel="noopener" target="_blank">{tt("Privacy notice", "Aviso de privacidad")}</a>' if url else ""


def flash(msg: str, err: bool = False) -> str:
    return f'<div class="flash{" err" if err else ""}">{UI.e(msg)}</div>' if msg else ""


# ------------------------------------------------------------------ public
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/brand/<name>")
def brand(name):
    if name not in BRAND_FILES and name not in BRAND_FONTS:
        abort(404)
    p = os.path.join(BRAND_DIR, name)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype=BRAND_FONTS.get(name, "image/png"), max_age=86400)


@app.get("/favicon.png", endpoint="favicon")
@app.get("/favicon.ico", endpoint="favicon")
@app.get("/apple-touch-icon.png", endpoint="favicon")
def favicon():
    for name in FAVICONS[request.path.lstrip("/")]:
        p = os.path.join(BRAND_DIR, name)
        if os.path.exists(p):
            return send_file(p, mimetype="image/png", max_age=86400)
    abort(404)


@app.get("/about", endpoint="about")
@app.get("/about/", endpoint="about")
def about():
    """The product manual for anyone with the link (v331). No session is
    read or set; the file is sent byte for byte (no rewriting)."""
    if not os.path.isfile(ABOUT_FILE):
        abort(404)
    r = send_file(ABOUT_FILE, mimetype="text/html", max_age=0, conditional=True)
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/lang/<code>")
def lang(code):
    r = redirect(request.referrer or "/")
    r.set_cookie("pl_lang", "es" if code == "es" else "en", max_age=365 * 86400, samesite="Lax", secure=SECURE_COOKIE)
    return r


def _login_page(msg: str = "", status: int = 200) -> Response:
    tt = UI.T(request.cookies.get("pl_lang", "en"))
    pl = _logo()
    nxt = UI.e(request.args.get("next", "/"))
    html_ = f"""<!doctype html><html lang="{tt.lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Sign in - ARGIA for Prologis</title>{ICON_LINKS}<style>{UI.CSS}
.login .art .brandrow{{display:flex;align-items:center;gap:14px}} .login .art img.pl{{height:34px;background:#fff;border-radius:8px;padding:4px 10px}}
.login .art img.pl.rev{{height:38px;background:none;padding:0;border-radius:0}}
</style></head><body><div class="login"><div class="art"><div class="brandrow">{pl or UI.stripe_svg(34)}<span style="opacity:.6">×</span>
<img src="{argia_logo.MARK_URI}" alt="ARGIA" style="height:26px;filter:brightness(0) invert(1)"></div>
<div><div class="kick" style="color:#9fe3dc">{tt("O&M platform - Mexico", "Plataforma O&M - México")}</div>
<h1>{tt("Every rooftop. Every kilowatt. One view.", "Cada techo. Cada kilowatt. Una vista.")}</h1>
<p style="color:#cfe6e3;max-width:520px">{tt("Live production, response-time tickets on your MSA classes, construction pipeline and documentation for the Prologis rooftop solar portfolio, operated by ARGIA.", "Producción en vivo, tickets con los tiempos de respuesta de su MSA, avance de construcción y documentación del portafolio solar de Prologis, operado por ARGIA.")}</p></div>
<div class="small" style="color:#9fc9c4">{tt("Private platform prepared for Prologis by ARGIA - Smart Energy Solutions. Authorised users only; every access is logged.", "Plataforma privada preparada para Prologis por ARGIA - Smart Energy Solutions. Solo usuarios autorizados; todo acceso queda registrado.")}</div></div>
<div class="box"><form method="post" action="/login"><div class="kick">{tt("Sign in", "Iniciar sesión")}</div>
<h1 class="pt" style="margin-bottom:14px">ARGIA for Prologis</h1>{flash(msg, True)}
<input type="hidden" name="next" value="{nxt}">
<label>{tt("User", "Usuario")}</label><input name="username" autocomplete="username" required autofocus>
<label>{tt("Password", "Contraseña")}</label><input name="password" type="password" autocomplete="current-password" required>
<div style="margin-top:16px"><button class="btn" type="submit">{tt("Continue", "Continuar")}</button></div>
<p class="muted small" style="margin-top:18px">{tt(f"For now (until {MFA_OFF_UNTIL}) the password alone signs you in." if mfa_off() else "A second factor (authenticator app) is required after the password.", f"Por ahora (hasta el {MFA_OFF_UNTIL}) basta la contraseña." if mfa_off() else "Después de la contraseña se requiere un segundo factor (app autenticadora).")}
<a href="/lang/{"es" if tt.lang == "en" else "en"}">{"Español" if tt.lang == "en" else "English"}</a> {privacy_link(tt)}</p></form></div></div></body></html>"""
    return Response(html_, status=status, mimetype="text/html")


@app.get("/login")
def login():
    return _login_page()


@app.post("/login")
def login_post():
    c = db()
    u = (request.form.get("username") or "").strip().lower()
    pw = request.form.get("password") or ""
    now = time.time()
    for key in (f"u:{u}", f"ip:{ip()}"):
        left = S.locked_for(c, key, now)
        if left:
            S.audit(c, u, ip(), "login_locked", u)
            return _login_page(f"Too many attempts. Try again in {left} s. / Demasiados intentos. Intente en {left} s.", 429)
    row = S.user(c, u)
    if not row or row["disabled"] or not S.check_pw(row["pw"], pw):
        S.note_fail(c, f"u:{u}", now)
        S.note_fail(c, f"ip:{ip()}", now)
        S.audit(c, u, ip(), "login_fail", u)
        return _login_page("Wrong user or password. / Usuario o contraseña incorrectos.", 401)
    S.clear_fail(c, f"u:{u}")
    nxt = request.form.get("next") or "/"
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
    if mfa_off():                                 # v376: the instance's dated exception
        sid = S.new_session(c, u, ip(), now, mfa_ok=True)
        c.execute("UPDATE users SET last_login_utc=? WHERE username=?", (S.now_utc(), u))
        c.commit()
        S.audit(c, u, ip(), "login_ok_no_mfa", u, f"MFA exception until {MFA_OFF_UNTIL}")
        r = redirect(nxt)
        r.set_cookie(COOKIE, sid, httponly=True, secure=SECURE_COOKIE, samesite="Lax")
        return r
    sid = S.new_session(c, u, ip(), now, mfa_ok=False)
    S.audit(c, u, ip(), "login_password_ok", u)
    r = redirect("/mfa?next=" + nxt)
    r.set_cookie(COOKIE, sid, httponly=True, secure=SECURE_COOKIE, samesite="Lax")
    return r


@app.get("/mfa")
def mfa(msg: str = ""):
    tt = t()
    c = db()
    u = S.user(c, g.user["username"])
    secret = u["totp"]
    setup = ""
    if not secret:
        pend = request.cookies.get("pl_totp_new") or TOTP.new_secret()
        uri = TOTP.uri(pend, u["username"])
        setup = f"""<p>{tt("Scan this code with Microsoft Authenticator, Google Authenticator or 1Password, then type the 6-digit code.", "Escanee este código con Microsoft Authenticator, Google Authenticator o 1Password y escriba el código de 6 dígitos.")}</p>
<div id="qr" style="background:#fff;padding:10px;display:inline-block;border:1px solid var(--line);border-radius:12px"></div>
<p class="small muted">{tt("Or enter the key manually", "O ingrese la clave manualmente")}: <span class="once" style="font-size:13px">{UI.e(pend)}</span></p>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>new QRCode(document.getElementById('qr'),{{text:{json.dumps(uri)},width:180,height:180}});</script>
<input type="hidden" name="pending" value="{UI.e(pend)}">"""
    body = f"""<div class="card form" style="margin:30px auto"><div class="kick">{tt("Second factor", "Segundo factor")}</div>
<h1 class="pt">{tt("Set up your authenticator" if not secret else "Enter your code", "Configure su autenticador" if not secret else "Ingrese su código")}</h1>
{flash(msg, True)}<form method="post" action="/mfa">{csrf_field()}{setup}
<input type="hidden" name="next" value="{UI.e(request.args.get("next", "/"))}">
<label>{tt("6-digit code", "Código de 6 dígitos")}</label><input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required autofocus style="max-width:180px;font-size:20px;letter-spacing:.2em">
<div style="margin-top:14px"><button class="btn">{tt("Verify", "Verificar")}</button> <a class="btn ghost" href="/logout">{tt("Cancel", "Cancelar")}</a></div></form></div>"""
    r = page(tt("Second factor", "Segundo factor"), body)
    if not secret and "pend" in locals():
        r.set_cookie("pl_totp_new", pend, httponly=True, secure=SECURE_COOKIE, samesite="Strict", max_age=900)
    return r


@app.post("/mfa")
def mfa_post():
    c = db()
    u = S.user(c, g.user["username"])
    code = request.form.get("code", "")
    secret = u["totp"] or request.cookies.get("pl_totp_new", "")
    if not secret or (not u["totp"] and request.form.get("pending") != secret):
        return mfa("Start again. / Empiece de nuevo.")
    ok, step = TOTP.verify(secret, code, last_step=u["totp_step"])
    if not ok:
        S.audit(c, u["username"], ip(), "mfa_fail", u["username"])
        S.note_fail(c, f"u:{u['username']}", time.time())
        if S.locked_for(c, f"u:{u['username']}", time.time()):
            S.end_session(c, request.cookies.get(COOKIE, ""))
            return redirect("/login")
        return mfa("Wrong code. / Código incorrecto.")
    first = not u["totp"]
    c.execute("UPDATE users SET totp=?, totp_step=?, last_login_utc=? WHERE username=?",
              (secret, step, S.now_utc(), u["username"]))
    c.commit()
    S.mark_mfa(c, request.cookies.get(COOKIE, ""))
    S.audit(c, u["username"], ip(), "mfa_enrolled" if first else "login_ok", u["username"])
    nxt = request.form.get("next") or "/"
    r = redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
    r.delete_cookie("pl_totp_new")
    return r


@app.get("/password")
def password(msg: str = ""):
    tt = t()
    body = f"""<div class="card form" style="margin:30px auto"><div class="kick">{tt("Account", "Cuenta")}</div>
<h1 class="pt">{tt("Choose your password", "Elija su contraseña")}</h1>{flash(msg, True)}
<p class="muted small">{tt("At least 12 characters. It replaces the one-time password you received.", "Mínimo 12 caracteres. Sustituye la contraseña temporal que recibió.")}</p>
<form method="post" action="/password">{csrf_field()}<label>{tt("New password", "Nueva contraseña")}</label><input type="password" name="pw1" autocomplete="new-password" required>
<label>{tt("Repeat", "Repetir")}</label><input type="password" name="pw2" autocomplete="new-password" required>
<div style="margin-top:14px"><button class="btn">{tt("Save", "Guardar")}</button></div></form></div>"""
    return page(tt("Password", "Contraseña"), body)


@app.post("/password")
def password_post():
    p1, p2 = request.form.get("pw1", ""), request.form.get("pw2", "")
    if p1 != p2:
        return password("The two passwords differ. / Las contraseñas no coinciden.")
    prob = S.password_problem(p1, g.user["username"])
    if prob:
        return password(prob)
    S.change_password(db(), g.user["username"], p1, ip())
    return redirect("/")


@app.get("/logout")
def logout():
    c = db()
    S.audit(c, (g.user or {}).get("username", ""), ip(), "logout")
    S.end_session(c, request.cookies.get(COOKIE, ""))
    r = redirect("/login")
    r.delete_cookie(COOKIE)
    return r


# ------------------------------------------------------------------ data helpers
def live_all(rg: R.Registry, now: dt.datetime) -> Dict[str, M.LiveState]:
    op = rg.operating
    return {s.code: M.live(s, now, i, len(op)) for i, s in enumerate(op)} | \
        {s.code: M.live(s, now) for s in rg.sites if not s.operating}


def open_tickets(c) -> List:
    q = ",".join("?" * len(S.OPEN))
    return c.execute(f"SELECT * FROM tickets WHERE kind='incident' AND status IN ({q}) ORDER BY id DESC", S.OPEN).fetchall()


def sla_of(c, tk, now_utc: dt.datetime) -> tuple:
    return MR.ticket_clock(c, tk, now_utc)        # v322: one clock for the pages and the monthly report


def sla_pill(cls, used, st, tt) -> str:
    if st == "not_started":
        return f'<span class="pill s-info">{tt("Awaiting dispatch approval", "Esperando aprobación")}</span>'
    if st == "due":
        return f'<span class="pill s-off">{tt("Next site visit", "Próxima visita")}</span>'
    if st == "met":
        return f'<span class="pill s-ok">{tt("Met", "Cumplido")} · {used:.1f} h</span>'
    if st == "breached":
        return f'<span class="pill s-bad">{tt("Breached", "Incumplido")} · {used:.1f} h</span>'
    left = (cls.hours or 0) - used
    return f'<span class="pill {"s-warn" if left < 6 else "s-lime"}">{tt("Due in", "Vence en")} {max(0, left):.1f} h</span>'


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def mx_time(ts_utc: str) -> str:
    if not ts_utc:
        return "-"
    return (dt.datetime.fromisoformat(ts_utc) + M.MX_OFFSET).strftime("%d %b %Y %H:%M")


MAP_COL = {"producing": UI.LIME, "night": "#7f8c8a", "inverter_fault": UI.RED, "comm_loss": UI.AMBER, "pre_pto": UI.SKY}


def site_photo(code: str) -> Optional[str]:
    """The site's picture file, or None. Only registry codes reach the disk,
    so a URL can never name another file."""
    site = reg().site(code)
    if not site or not re.match(r"^[A-Za-z0-9_-]{1,40}$", site.code):
        return None
    f = os.path.join(PHOTO_DIR, site.code + ".jpg")
    return f if os.path.isfile(f) else None


@app.get("/sites/<code>/photo.jpg")
def site_photo_file(code):
    f = site_photo(code)
    if not f:
        abort(404)
    return send_file(f, mimetype="image/jpeg", max_age=86400)


def _map_points(rg: R.Registry, lv: Dict[str, M.LiveState], projects: bool) -> List[Dict]:
    pts = []
    for s in rg.sites:
        st = lv[s.code]
        pts.append({"lat": s.lat, "lon": s.lon, "c": MAP_COL.get(st.status, "#999"), "code": s.code, "name": s.name,
                    "kwp": s.kwp, "kw": st.kw_now, "kwh": st.kwh_today, "st": st.status, "approx": s.geo_approx,
                    "url": f"/sites/{s.code}/", "g": "op" if s.operating else "pre", "sub": s.city,
                    "ph": f"/sites/{s.code}/photo.jpg" if site_photo(s.code) else ""})
    if projects:
        for p in rg.projects:
            if p.lat is not None and not rg.site(p.site_code):
                pts.append({"lat": p.lat, "lon": p.lon, "c": UI.DEEP, "code": p.id, "name": p.name, "kwp": p.kwp or 0,
                            "kw": None, "kwh": 0, "st": "project:" + p.stage, "approx": True, "url": "/projects/",
                            "g": "epc", "sub": p.kind})
    return pts


def _legend_groups(tt) -> List[tuple]:
    return [("op", tt("Operating sites", "Sitios en operación"), UI.TEAL), ("pre", tt("Before PTO", "Antes de PTO"), UI.SKY),
            ("epc", tt("EPC projects", "Proyectos EPC"), UI.DEEP)]


def pvout_layer() -> Optional[Dict]:
    """The solar potential layer description, or None (no files, or a
    description that does not make sense - then the map simply has no layer)."""
    try:
        if not os.path.isfile(os.path.join(MAP_DIR, "pvout.png")):
            return None
        with open(os.path.join(MAP_DIR, "pvout.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        (s_, w_), (n_, e_) = d["bounds"]
        if not (-90 <= float(s_) < float(n_) <= 90 and -180 <= float(w_) < float(e_) <= 180):
            return None
        stops = [[float(v), str(c)] for v, c in d.get("stops") or []]
        if len(stops) < 2 or any(not re.match(r"^#[0-9a-fA-F]{6}$", c) for _v, c in stops):
            return None
        return {"bounds": [[float(s_), float(w_)], [float(n_), float(e_)]], "stops": stops,
                "unit": str(d.get("unit", "kWh/kWp")), "label_en": str(d.get("label_en", "Solar potential")),
                "label_es": str(d.get("label_es", "Potencial solar")), "note_en": str(d.get("note_en", "")),
                "note_es": str(d.get("note_es", "")), "attribution": str(d.get("attribution", ""))}
    except (OSError, ValueError, KeyError, TypeError):
        return None


@app.get("/map/pvout.png")
def map_pvout():
    if pvout_layer() is None:
        abort(404)
    return send_file(os.path.join(MAP_DIR, "pvout.png"), mimetype="image/png", max_age=86400)


def map_block(rg: R.Registry, lv: Dict[str, M.LiveState], h_cls: str = "", projects: bool = True, mode: str = "list",
              extra: Optional[Dict[str, Dict]] = None) -> str:
    """The Leaflet map with a legend of every plant, name and size.
    mode "list": the legend only names them (overview). mode "select": each
    plant and each group has a tick box; unticked plants leave the map and
    the tiles (see map_page) total what stays ticked. The choice is kept in
    this browser (localStorage, per-viewer convenience only)."""
    tt = t()
    pts = _map_points(rg, lv, projects)
    for p in pts:                                       # v333: the popup card's numbers (map page)
        if extra and p["code"] in extra:
            p["x"] = extra[p["code"]]
    sel = mode == "select"
    pv = pvout_layer() if sel else None
    groups = ""
    for g_key, g_name, g_col in _legend_groups(tt):
        rows = [p for p in pts if p["g"] == g_key]
        if not rows:
            continue
        g_kwp = sum(p["kwp"] for p in rows)
        items = ""
        for p in sorted(rows, key=lambda x: x["name"]):
            box = f'<input type="checkbox" class="ptog" data-k="{UI.e(p["code"])}" checked>' if sel else ""
            size = f'{UI.num(p["kwp"], 0)} kWp' if p["kwp"] else tt("size to confirm", "tamaño por confirmar")
            sub = " · ".join(x for x in (p["code"], p["sub"], size) if x)
            items += (f'<label class="lrow">{box}<span class="ldot" style="background:{p["c"]}"></span>'
                      f'<span><a href="{p["url"]}"><b>{UI.e(p["name"])}</b></a><span class="lsub">{UI.e(sub)}</span></span></label>')
        gbox = f'<input type="checkbox" class="gtog" data-g="{g_key}" checked> ' if sel else ""
        groups += (f'<div><div class="lgh" style="color:{g_col}"><label class="lrow" style="padding:0">{gbox}{UI.e(g_name)}</label>'
                   f'<span class="muted small">{len(rows)} · {UI.num(g_kwp, 0)} kWp</span></div><div class="lgrid">{items}</div></div>')
    note = (f'<p class="muted small" style="margin:8px 0 0">{tt("Tick or untick sites or a whole group - the tiles above total the selection, and the choice is remembered in this browser.", "Marque o desmarque sitios o un grupo completo - los mosaicos de arriba suman la selección, y la elección se recuerda en este navegador.")}</p>'
            if sel else "")
    legend = f'<div class="plegend{" sel" if sel else ""}">{groups}</div>{note}'
    pv_legend = ""
    if pv:
        lo, hi = pv["stops"][0][0], pv["stops"][-1][0]
        grad = ", ".join(f'{c} {(v - lo) / (hi - lo) * 100:.0f}%' for v, c in pv["stops"])
        pv_legend = (f'<div class="small pvleg" style="margin-top:8px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
                     f'<b>{UI.e(tt(pv["label_en"], pv["label_es"]))}</b><span class="muted">{UI.num(lo, 0)}</span>'
                     f'<span style="display:inline-block;width:220px;height:10px;border-radius:5px;background:linear-gradient(90deg,{grad})"></span>'
                     f'<span class="muted">{UI.num(hi, 0)} {UI.e(pv["unit"])}</span>'
                     f'<span class="muted">{UI.e(tt(pv["note_en"], pv["note_es"]))} · {tt("switch it on or off with the layer button on the map", "actívela o desactívela con el botón de capas del mapa")}</span></div>')
    status = (f'<div class="legend small" style="margin-top:8px"><span><i style="background:{UI.LIME}"></i>{tt("Producing", "Produciendo")}</span>'
              f'<span><i style="background:{UI.RED}"></i>{tt("Inverter fault", "Falla de inversor")}</span><span><i style="background:{UI.AMBER}"></i>{tt("Communication loss", "Sin comunicación")}</span>'
              f'<span><i style="background:#7f8c8a"></i>{tt("Night", "Noche")}</span><span><i style="background:{UI.SKY}"></i>{tt("Before PTO", "Antes de PTO")}</span>'
              + (f'<span><i style="background:{UI.DEEP}"></i>{tt("EPC project", "Proyecto EPC")}</span>' if projects else "") + "</div>" + pv_legend)
    js_sel = ""
    if sel:
        js_sel = """var KEY='pl_map_hide_v1',HID={};try{var s=localStorage.getItem(KEY);HID=s?JSON.parse(s)||{}:{};}catch(e){HID={};}
function fit(){var b=[];P.forEach(function(p){if(!HID[p.code]&&p.g!=='epc')b.push([p.lat,p.lon]);});
if(!b.length)P.forEach(function(p){if(!HID[p.code])b.push([p.lat,p.lon]);});if(b.length)m.fitBounds(b,{padding:[30,30],maxZoom:13});}
function apply(k,hide){var cb=document.querySelector('.ptog[data-k="'+k+'"]');if(cb)cb.checked=!hide;
if(MK[k]){if(hide)m.removeLayer(MK[k]);else MK[k].addTo(m);}if(hide)HID[k]=1;else delete HID[k];}
function refresh(){try{localStorage.setItem(KEY,JSON.stringify(HID));}catch(e){}
document.querySelectorAll('.gtog').forEach(function(g){var grp=P.filter(function(p){return p.g===g.dataset.g;});
var on=grp.filter(function(p){return !HID[p.code];}).length;g.checked=on===grp.length&&on>0;g.indeterminate=on>0&&on<grp.length;});
if(window.plTiles)window.plTiles(HID);fit();}
P.forEach(function(p){if(HID[p.code])apply(p.code,true);});
document.querySelectorAll('.ptog').forEach(function(cb){cb.addEventListener('change',function(){apply(cb.dataset.k,!cb.checked);refresh();});});
document.querySelectorAll('.gtog').forEach(function(g){g.addEventListener('change',function(){
P.forEach(function(p){if(p.g===g.dataset.g)apply(p.code,!g.checked);});refresh();});});refresh();"""
    else:
        js_sel = "fitAll();"
    pv_js = ""
    if pv:
        # off until the reader switches it on (at site zoom it would only tint the streets).
        # Leaflet shows the layer name and the attribution as HTML: both go in as plain text
        # (&copy; in the file is fine, it is decoded first), and nothing can close the script
        pv_js = (f"OV[{json.dumps(UI.e(html.unescape(tt(pv['label_en'], pv['label_es']))))}]=L.imageOverlay('/map/pvout.png',{json.dumps(pv['bounds'])},"
                 f"{{opacity:.55,attribution:{json.dumps(UI.e(html.unescape(pv['attribution'])))}}});").replace("</", "<\\/")
    lb = json.dumps({"now": tt("Generating now", "Generando ahora"), "today": tt("Today", "Hoy"), "e30": tt("Last 30 days", "Últimos 30 días"),
                     "pr": tt("Performance ratio 30 d", "Performance ratio 30 d"), "av": tt("Availability 30 d", "Disponibilidad 30 d"),
                     "tk": tt("Open tickets", "Tickets abiertos"), "open": tt("Open the site", "Abrir el sitio"),
                     "approx": tt("approximate location", "ubicación aproximada"),
                     "st": {"producing": tt("producing", "produciendo"), "night": tt("night", "noche"),
                            "inverter_fault": tt("inverter fault", "falla de inversor"), "comm_loss": tt("communication loss", "sin comunicación")}},
                    ensure_ascii=False)
    return f"""<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>.leaflet-control-layers label{{display:flex;align-items:center;gap:6px;margin:2px 0;font-weight:500;font-size:12.5px;color:#1d2a35}}.leaflet-control-layers input{{width:auto;margin:0;padding:0;height:auto}}.leaflet-control-layers-separator{{margin:5px 0}}
.mpop img.ph{{display:block;width:100%;height:120px;object-fit:cover;border-radius:6px;margin:0 0 8px;background:#eef0f2}}.mpop table{{border-collapse:collapse;margin:6px 0;width:100%}}.mpop td{{padding:2px 0;border-bottom:1px solid #eef0f2;font-size:12.5px}}.mpop td.n{{text-align:right;padding-left:14px}}</style>
<div id="map" class="{h_cls}"></div><script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
{status}{legend}
<script>(function(){{var P={json.dumps(pts)};var m=L.map('map',{{scrollWheelZoom:false}});var MK={{}};
var st=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri',maxZoom:19}}).addTo(m);
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri, Maxar, Earthstar Geographics',maxZoom:19}});
var OV={{}};{pv_js}
L.control.layers({{'Streets':st,'Satellite':sat}},OV).addTo(m);
var LB={lb};
function n0(v,d){{return Number(v).toLocaleString('en-US',{{minimumFractionDigits:d||0,maximumFractionDigits:d||0}});}}
function esc(s){{return String(s).replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
function pop(p){{var r=function(k,v){{return '<tr><td>'+k+'</td><td class="n"><b>'+v+'</b></td></tr>';}},t='';
 if(p.kw!=null){{t+=r(LB.now,n0(p.kw)+' kW')+r(LB.today,n0(p.kwh)+' kWh');}}
 if(p.x){{t+=r(LB.e30,n0(p.x.e30/1000,1)+' MWh')+r(LB.pr,(p.x.pr*100).toFixed(1)+' %')+r(LB.av,(p.x.av*100).toFixed(2)+' %')+r(LB.tk,p.x.tk);}}
 return '<div class="mpop">'+(p.ph?'<img class="ph" src="'+p.ph+'" alt="">':'')+'<b>'+esc(p.name)+'</b><div class="small muted">'+esc(p.code)+' · '+n0(p.kwp)+' kWp · '+esc(LB.st[p.st]||p.st.replace('_',' '))+'</div>'
  +(t?'<table>'+t+'</table>':'')+(p.approx?'<div class="small muted"><i>'+LB.approx+'</i></div>':'')+'<a href="'+p.url+'">'+LB.open+' ›</a></div>';}}
P.forEach(function(p){{var r=Math.max(7,Math.min(16,Math.sqrt(p.kwp||100)/2.2));
var mk=L.circleMarker([p.lat,p.lon],{{radius:r,color:'#fff',weight:2,fillColor:p.c,fillOpacity:.95}}).addTo(m);MK[p.code]=mk;
mk.bindPopup(pop(p),{{minWidth:240,maxWidth:280}});}});
function fitAll(){{var b=[];P.forEach(function(p){{if(p.g!=='epc')b.push([p.lat,p.lon]);}});if(!b.length)P.forEach(function(p){{b.push([p.lat,p.lon]);}});
if(b.length)m.fitBounds(b,{{padding:[30,30],maxZoom:13}});}}
{js_sel}}})();</script>"""



def greeting(hour: int, tt) -> str:
    """v335: 'Good afternoon' / 'Buenas tardes' (plural in Spanish). Pure."""
    if hour < 12:
        return tt("Good morning", "Buenos días")
    if hour < 19:
        return tt("Good afternoon", "Buenas tardes")
    return tt("Good evening", "Buenas noches")


# ------------------------------------------------------------------ pages
@app.get("/")
def home():
    tt = t()
    rg = reg()
    now = now_mx()
    k = M.portfolio_kpis(rg.sites, now)
    lv = live_all(rg, now)
    c = db()
    tks = open_tickets(c)
    nowu = utc_now()
    alerts = [(s, lv[s.code]) for s in rg.operating if lv[s.code].status in ("inverter_fault", "comm_loss")]
    al = "".join(f'<tr><td><a href="/sites/{s.code}/"><b>{UI.e(s.name)}</b></a><div class="small muted">{s.code}</div></td>'
                 f'<td>{UI.status_pill(x.status, tt)}</td><td class="n">{UI.num(x.kw_now)} kW</td>'
                 f'<td><a class="btn sm" href="/tickets/new?site={s.code}&kind={x.status}">{tt("Open ticket", "Abrir ticket")}</a></td></tr>'
                 for s, x in alerts) or f'<tr><td class="muted">{tt("No active alerts - every operating site is reporting.", "Sin alertas activas - todos los sitios reportan.")}</td></tr>'
    trows = ""
    for tk in tks[:6]:
        cls, used, st = sla_of(c, tk, nowu)
        s = rg.site(tk["site_code"])
        trows += (f'<tr><td class="nw"><a href="/tickets/{tk["number"]}/"><b>{tk["number"]}</b></a></td><td>{UI.e(tk["title"])}<div class="small muted">{UI.e(s.name if s else tk["site_code"])}</div></td>'
                  f'<td><span class="pill s-off">{cls.priority}</span></td><td>{sla_pill(cls, used, st, tt)}</td></tr>')
    trows = trows or f'<tr><td class="muted">{tt("No open tickets.", "Sin tickets abiertos.")}</td></tr>'
    # the portfolio's today curve
    agg: Dict[str, float] = {}
    exp: Dict[str, float] = {}
    for i, s in enumerate(rg.operating):
        r = M.day_result(s.code, s.lat, s.lon, s.kwp, now.date(), upto=now.time(), index=i, n_sites=len(rg.operating))
        for hhmm, v in r.series:
            agg[hhmm] = agg.get(hhmm, 0.0) + (v or 0.0)
        full = M.day_result(s.code, s.lat, s.lon, s.kwp, now.date(), index=i, n_sites=len(rg.operating))
        for (hhmm, _kw), (_h2, _v, ghi) in zip(full.series, M._profile(s.code, s.lat, s.lon, s.kwp, now.date())):
            exp[hhmm] = exp.get(hhmm, 0.0) + s.kwp * M.PR_TARGET * (M.clear_sky_ghi(M.sun_elevation(s.lat, s.lon, dt.datetime.combine(now.date(), dt.time(int(hhmm[:2]), int(hhmm[3:]))))) / 1000) * 0.92
    curve = UI.area_chart(sorted(agg.items()), sorted(exp.items())[::3], unit="kW")
    projs = ""
    st_map = S.project_state(c)
    for p in rg.projects[:6]:
        stage = (st_map.get(p.id)["stage"] if p.id in st_map else p.stage)
        idx = R.STAGES.index(stage) if stage in R.STAGES else 0
        bars = "".join(f'<span class="{"done" if j < idx else ("now" if j == idx else "")}"></span>' for j in range(len(R.STAGES)))
        projs += f'<div style="margin-bottom:10px"><b>{UI.e(p.name)}</b> <span class="small muted">{UI.e(p.kind)} · {UI.e(stage)}</span><div class="stages">{bars}</div></div>'
    an = asset_counts(c)
    assets_card = (f'<div class="card" style="margin-top:16px"><h2>{tt("Assets", "Activos")}<span class="r"><a href="/assets/">{tt("Register", "Registro")} ›</a></span></h2>'
                   + _denied_banner(c, tt)
                   + f'<div class="legend small"><span><b>{an["components"]}</b>&nbsp;{tt("components registered", "componentes registrados")}</span>'
                   f'<span><b>{an["expiring"]}</b>&nbsp;{tt("warranties expire within 90 days", "garantías vencen en 90 días")}</span>'
                   f'<span><b>{an["cal_overdue"]}</b>&nbsp;{tt("calibrations overdue", "calibraciones vencidas")}</span>'
                   f'<span><b>{an["claims_open"]}</b>&nbsp;<a href="/assets/claims/">{tt("open warranty claims", "reclamos de garantía abiertos")}</a></span>'
                   f'<span><b>{an["below_min"]}</b>&nbsp;<a href="/assets/spares/">{tt("spare parts below minimum", "refacciones bajo mínimo")}</a></span></div></div>')
    body = f"""<div class="hero"><div class="glow"></div><div class="k">{UI.e(rg.portfolio.get("market", "Mexico City"))} · {tt("Rooftop solar portfolio", "Portafolio solar en techos")}</div>
<h1>{greeting(now.hour, tt)}{", " + UI.e((g.user or {}).get("name", "").split(" ")[0]) if (g.user or {}).get("name") else ""}.</h1>
<div class="sub"><span class="live-dot"></span>{tt("Live", "En vivo")} · {now.strftime("%d %b %Y %H:%M")} · {k["operating"]} {tt("operating sites", "sitios operando")} · {UI.num(k["kwp"] / 1000, 2)} MWp {tt("under ARGIA care", "a cargo de ARGIA")}</div>
<div class="kpis"><div class="kpi"><div class="l">{tt("Power now", "Potencia ahora")}</div><div class="v" id="kw">{UI.num(k["kw_now"] / 1000, 2)}<small>MW</small></div><div class="d">{tt("of", "de")} {UI.num(k["kwp_operating"] / 1000, 2)} MWp</div></div>
<div class="kpi"><div class="l">{tt("Energy today", "Energía hoy")}</div><div class="v" id="kwh">{UI.num(k["kwh_today"] / 1000, 2)}<small>MWh</small></div><div class="d">{tt("so far", "hasta ahora")}</div></div>
<div class="kpi"><div class="l">{tt("Last 30 days", "Últimos 30 días")}</div><div class="v">{UI.num(k["mwh_30d"], 0)}<small>MWh</small></div><div class="d">{UI.num(k["co2_t_30d"], 0)} t CO₂ {tt("avoided", "evitadas")}</div></div>
<div class="kpi"><div class="l">{tt("Availability 30 d", "Disponibilidad 30 d")}</div><div class="v">{k["availability_30d"] * 100:.2f}<small>%</small></div><div class="d">{tt("MSA method, >150 W/m²", "Método MSA, >150 W/m²")}</div></div>
<div class="kpi"><div class="l">{tt("Performance ratio", "Performance ratio")}</div><div class="v">{k["pr_30d"] * 100:.1f}<small>%</small></div><div class="d">{tt("30-day, weighted", "30 días, ponderado")}</div></div>
<div class="kpi"><div class="l">{tt("Open tickets", "Tickets abiertos")}</div><div class="v">{len(tks)}</div><div class="d">{k["alerts"]} {tt("live alerts", "alertas en vivo")} · {len(S.open_orders(c))} {tt("service orders", "órdenes de servicio")}</div></div></div></div>
<div class="grid g2"><div class="card"><h2>{tt("Portfolio power today", "Potencia del portafolio hoy")}<span class="r muted">{tt("dashed: clear-sky expectation", "punteado: esperado cielo despejado")}</span></h2>{curve}</div>
<div class="card"><h2>{tt("Live alerts", "Alertas en vivo")}<span class="r"><a href="/alarms/">{tt("Alarms", "Alarmas")} ›</a> · <a href="/sites/">{tt("All sites", "Todos los sitios")} ›</a></span></h2><table class="t">{al}</table></div></div>
<div class="grid g2"><div class="card"><h2>{tt("Where your energy is made", "Dónde se produce su energía")}<span class="r"><a href="/map/">{tt("Full map", "Mapa completo")} ›</a></span></h2>{map_block(rg, lv, "mini-map", projects=False)}</div>
<div><div class="card"><h2>{tt("Open tickets", "Tickets abiertos")}<span class="r"><a href="/tickets/">{tt("All", "Todos")} ›</a></span></h2><table class="t">{trows}</table></div>
<div class="card" style="margin-top:16px"><h2>{tt("Construction pipeline", "Avance de construcción")}<span class="r"><a href="/projects/">{tt("Projects", "Proyectos")} ›</a></span></h2>{projs or tt("No projects.", "Sin proyectos.")}</div></div></div>
{assets_card}
<script>setTimeout(function(){{location.reload()}},300000);</script>"""
    return page(tt("Overview", "Resumen"), body, "home", sample=True)


def _sel_tile(tid: str, label: str, value: str, unit: str, sub: str) -> str:
    return (f'<div class="kpi"><div class="l">{label}</div><div class="v"><span id="{tid}">{value}</span><small>{unit}</small></div>'
            f'<div class="d" id="{tid}_d">{sub}</div></div>')


@app.get("/map/")
def map_page():
    tt = t()
    rg = reg()
    now = now_mx()
    lv = live_all(rg, now)
    rows = M.site_stats(rg.sites, now)
    tk = {}
    for x in open_tickets(db()):
        tk[x["site_code"]] = tk.get(x["site_code"], 0) + 1
    for r in rows:
        r["tk"] = tk.get(r["code"], 0)
    k = M.selection_kpis(rows)
    n_tk = sum(r["tk"] for r in rows)
    of, op_txt, sites_txt, avoided, alerts_txt = (tt("of", "de"), tt("operating", "en operación"), tt("of", "de"),
                                                   tt("avoided", "evitadas"), tt("live alerts", "alertas en vivo"))
    tiles = ('<div class="hero tiles"><div class="kpis">'
             + _sel_tile("st_n", tt("Selected sites", "Sitios seleccionados"), str(k["sites"]), "",
                         f'{sites_txt} {len(rg.sites)} · {UI.num(k["kwp"] / 1000, 2)} MWp')
             + _sel_tile("st_kw", tt("Power now", "Potencia ahora"), UI.num(k["kw_now"] / 1000, 2), "MW",
                         f'{of} {UI.num(k["kwp_operating"] / 1000, 2)} MWp {op_txt}')
             + _sel_tile("st_kwh", tt("Energy today", "Energía hoy"), UI.num(k["kwh_today"] / 1000, 2), "MWh", tt("so far", "hasta ahora"))
             + _sel_tile("st_e30", tt("Last 30 days", "Últimos 30 días"), UI.num(k["mwh_30d"], 0), "MWh", f'{UI.num(k["co2_t_30d"], 0)} t CO₂ {avoided}')
             + _sel_tile("st_av", tt("Availability 30 d", "Disponibilidad 30 d"), f'{k["availability_30d"] * 100:.2f}', "%", tt("kWp-weighted", "ponderada por kWp"))
             + _sel_tile("st_pr", tt("Performance ratio", "Performance ratio"), f'{k["pr_30d"] * 100:.1f}', "%", tt("30-day, weighted", "30 días, ponderado"))
             + _sel_tile("st_tk", tt("Open tickets", "Tickets abiertos"), str(n_tk), "", f'{k["alerts"]} {alerts_txt}')
             + "</div></div>")
    labels = json.dumps({"of": sites_txt, "total": len(rg.sites), "op": f"MWp {op_txt}", "co2": f"t CO₂ {avoided}", "alerts": alerts_txt})
    script = f"""<script>{M.SELECTION_JS}
(function(){{var R={json.dumps(rows)},LB={labels};
function f(v,d){{return v.toLocaleString('en-US',{{minimumFractionDigits:d,maximumFractionDigits:d}});}}
function set(id,v){{var e=document.getElementById(id);if(e)e.textContent=v;}}
window.plTiles=function(H){{var S=R.filter(function(r){{return !H[r.code];}}),k=plSel(S);
var tkn=S.reduce(function(a,r){{return a+r.tk;}},0);
set('st_n',k.sites);set('st_n_d',LB.of+' '+LB.total+' · '+f(k.kwp/1000,2)+' MWp');
set('st_kw',f(k.kw_now/1000,2));set('st_kw_d',LB.of+' '+f(k.kwp_operating/1000,2)+' '+LB.op);
set('st_kwh',f(k.kwh_today/1000,2));set('st_e30',f(k.mwh_30d,0));set('st_e30_d',f(k.co2_t_30d,0)+' '+LB.co2);
set('st_av',k.operating?f(k.availability_30d*100,2):'-');set('st_pr',k.operating?f(k.pr_30d*100,1):'-');
set('st_tk',tkn);set('st_tk_d',k.alerts+' '+LB.alerts);}};}})();</script>"""
    body = (f'<div class="kick">{tt("Portfolio map", "Mapa del portafolio")}</div><h1 class="pt">{len(rg.sites)} {tt("rooftops", "techos")} · {UI.num(rg.kwp_total / 1000, 2)} MWp</h1>'
            f'<p class="muted small">{tt("Locations are approximate (industrial park level) until exact coordinates are captured at onboarding.", "Ubicaciones aproximadas (a nivel parque industrial) hasta registrar coordenadas exactas en el onboarding.")}</p>'
            + tiles + script + '<div style="margin-top:14px">' + map_block(rg, lv, mode="select", extra={
                r["code"]: {"e30": r["e30"], "pr": (r["e30"] / r["irr_kwp"]) if r["irr_kwp"] else 0.0, "av": r["av"], "tk": r["tk"]}
                for r in rows if r["op"]}) + "</div>")
    return page(tt("Map", "Mapa"), body, "map", sample=True)


@app.get("/sites/")
def sites():
    tt = t()
    rg = reg()
    now = now_mx()
    lv = live_all(rg, now)
    rows = ""
    op = rg.operating
    yday = now.date() - dt.timedelta(days=1)
    for s in sorted(rg.sites, key=lambda x: (not x.operating, x.market or "MEXICO", x.park, x.code)):
        x = lv[s.code]
        if s.operating:
            i = op.index(s)
            h = M.history(s, yday, 30, i, len(op))
            e30 = sum(d.kwh for d in h)
            irr = sum(d.irr_kwh_m2 for d in h)
            pr = e30 / (s.kwp * irr) if irr else None
            av = sum(d.availability for d in h) / len(h)
            extra = f'<td class="n">{UI.num(e30 / 1000, 1)}</td><td class="n">{UI.num(e30 / s.kwp / 30, 2)}</td><td class="n">{pr * 100:.1f}%</td><td class="n">{av * 100:.2f}%</td>'
        else:
            extra = f'<td class="n" colspan="4"><span class="muted small">PTO {UI.e(s.pto)}</span></td>'
        sub = " · ".join(x for x in (s.code, UI.e(s.city), UI.e(s.customer)) if x)
        need_ = "".join(f' <span class="pill s-lime" title="{UI.e(tt(*SCOPE_LBL[k]))}">{UI.e(tt(*SCOPE_LBL[k]))[:1]}</span>' for k in R.SCOPES if k in s.scope)
        rows += (f'<tr><td><a href="/sites/{s.code}/"><b>{UI.e(s.name)}</b></a>{need_}<div class="small muted">{sub}</div></td>'
                 f'<td>{UI.status_pill(x.status, tt)}</td><td class="n">{UI.num(s.kwp, 0)}</td><td class="n">{UI.num(x.kw_now)}</td>'
                 f'<td class="n">{UI.num(x.kwh_today)}</td>{extra}</tr>')
    body = f"""<div class="kick">{tt("Sites", "Sitios")}</div><h1 class="pt">{tt("Every rooftop, live", "Cada techo, en vivo")}</h1>{_sites_tabs("list", tt)}
<div class="card" style="margin-top:12px;overflow-x:auto"><table class="t"><tr><th>{tt("Site", "Sitio")}</th><th>{tt("Status", "Estado")}</th><th class="n">kWp</th><th class="n">kW {tt("now", "ahora")}</th>
<th class="n">kWh {tt("today", "hoy")}</th><th class="n">MWh 30 d</th><th class="n">{tt("Yield", "Rendimiento")} kWh/kWp·d</th><th class="n">PR 30 d</th><th class="n">{tt("Avail.", "Disp.")} 30 d</th></tr>{rows}</table></div>"""
    return page(tt("Sites", "Sitios"), body, "sites", sample=True)


@app.get("/sites/<code>/")
def site_page(code):
    tt = t()
    rg = reg()
    s = rg.site(code) or abort(404)
    now = now_mx()
    op = rg.operating
    i = op.index(s) if s in op else 0
    x = M.live(s, now, i, len(op))
    c = db()
    tks = c.execute("SELECT * FROM tickets WHERE site_code=? OR id IN (SELECT ticket_id FROM order_lines WHERE site_code=?) "
                    "ORDER BY id DESC LIMIT 8", (s.code, s.code)).fetchall()
    docs = c.execute("SELECT * FROM documents WHERE site_code=? AND deleted=0 ORDER BY id DESC LIMIT 8", (s.code,)).fetchall()
    if s.operating:
        r = M.day_result(s.code, s.lat, s.lon, s.kwp, now.date(), upto=now.time(), index=i, n_sites=len(op))
        h = M.history(s, now.date() - dt.timedelta(days=1), 30, i, len(op))
        curve = UI.area_chart(r.series, unit="kW")
        bars = UI.bar_chart([(d.day.isoformat(), d.kwh, d.expected_kwh) for d in h], w=1300, h=280)
        e30 = sum(d.kwh for d in h)
        av = sum(d.availability for d in h) / len(h)
        irr = sum(d.irr_kwh_m2 for d in h)
        donuts = (f'<div style="display:flex;gap:18px;flex-wrap:wrap;align-items:center">{UI.donut(av, UI.TEAL, label=f"{av * 100:.1f}%")}<div><b>{tt("Availability", "Disponibilidad")}</b><div class="small muted">30 d · MSA</div></div>'
                  f'{UI.donut(e30 / (s.kwp * irr) if irr else 0, UI.SKY, label=f"{(e30 / (s.kwp * irr) if irr else 0) * 100:.0f}%")}<div><b>PR</b><div class="small muted">30 d</div></div></div>')
        live_cards = f"""<div class="grid g2"><div class="card"><h2>{tt("Power today", "Potencia hoy")}<span class="r">{UI.status_pill(x.status, tt)}</span></h2>{curve}
<div class="small muted">{tt("Last reading", "Última lectura")} {x.last_seen or "-"} · {UI.num(x.kwh_today)} kWh {tt("today", "hoy")}</div></div>
<div class="card"><h2>{tt("Health", "Salud")}</h2>{donuts}<p class="small muted" style="margin-top:12px">{tt("Specific yield 30 d", "Rendimiento específico 30 d")}: <b>{UI.num(e30 / s.kwp, 0)} kWh/kWp</b> · {UI.num(e30 / 1000, 1)} MWh</p></div></div>
<div class="card" style="margin-top:16px"><h2>{tt("Last 30 days", "Últimos 30 días")}<span class="r muted">{tt("bar: actual · line: expected (weather-adjusted) · amber: below 85%", "barra: real · línea: esperado (ajustado por clima) · ámbar: bajo 85%")}</span></h2>{bars}</div>"""
    else:
        live_cards = f'<div class="card" style="margin-top:16px"><h2>{tt("Not yet in operation", "Aún no en operación")}</h2><p>{tt("Permission to Operate planned", "Permiso de operación planeado")}: <b>{UI.e(s.pto)}</b>. {tt("Monitoring starts at the ARGIA onboarding inspection.", "El monitoreo inicia con la inspección de onboarding de ARGIA.")}</p></div>'
    st_lbl = dict((k, tt(en, es)) for k, en, es in S.TICKET_STATUSES + CAT.ORDER_STATUSES)
    trs = "".join(f'<tr><td><a href="/tickets/{tk["number"]}/">{tk["number"]}</a></td><td>{UI.e(tk["title"])}</td><td>{UI.e(st_lbl.get(tk["status"], tk["status"]))}</td></tr>' for tk in tks) \
        or f'<tr><td class="muted">{tt("No tickets.", "Sin tickets.")}</td></tr>'
    drs = "".join(f'<tr><td><a href="/docs/{d["id"]}/download">{UI.e(d["name"])}</a></td><td class="small muted">{UI.e(d["folder"])}</td></tr>' for d in docs) \
        or f'<tr><td class="muted">{tt("No documents yet.", "Sin documentos aún.")}</td></tr>'
    eqs = A.equipment(c, s.code)
    by_cat: Dict[str, int] = {}
    for r_ in eqs:
        by_cat[r_["category"]] = by_cat.get(r_["category"], 0) + int(r_["qty"])
    exp_n = sum(1 for r_ in eqs if A.warranty_state(r_["warranty_until"], today_mx()) in ("expiring", "expired"))
    eq_html = ('<div class="legend small">' + "".join(f'<span><b>{q}</b>&nbsp;{UI.e(_lbl(A.CATEGORIES, k, tt))}</span>' for k, q in sorted(by_cat.items()))
               + (f'<span class="pill s-warn">{exp_n} {tt("warranties expiring or expired", "garantías por vencer o vencidas")}</span>' if exp_n else "") + "</div>"
               if eqs else f'<p class="small muted">{tt("Not registered yet - filled at onboarding.", "Aún sin registrar - se llena en el onboarding.")}</p>')
    body = f"""<div class="kick">{UI.e(s.park)} · {UI.e(s.city)}</div><h1 class="pt">{UI.e(s.name)}</h1>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 4px"><span class="chip">{s.code}</span><span class="chip">{UI.num(s.kwp, 1)} kWp</span><span class="chip">PTO {UI.e(s.pto)}</span>
<span class="chip">{UI.e(s.monitoring)}</span><span class="chip">{UI.e(s.address)}</span>{_loc_chip(s, tt)}</div>{_service_card(s, tt)}{live_cards}
<div class="grid g2e"><div class="card"><h2>{tt("Tickets", "Tickets")}<span class="r"><a class="btn sm ghost" href="/shop/">{tt("Order a service", "Pedir servicio")}</a> <a class="btn sm" href="/tickets/new?site={s.code}">{tt("New", "Nuevo")}</a></span></h2><table class="t">{trs}</table></div>
<div class="card"><h2>{tt("Documents", "Documentos")}<span class="r"><a href="/docs/?site={s.code}">{tt("Folder", "Carpeta")} ›</a></span></h2><table class="t">{drs}</table></div></div>
<div class="card" style="margin-top:16px"><h2>{tt("Equipment", "Equipos")}<span class="r"><a href="/assets/?site={s.code}">{tt("Register", "Registro")} ›</a></span></h2>{eq_html}</div>
{_ho_card(c, s, tt)}
{_location_card(s, tt)}"""
    return page(s.name, body, "sites", sample=s.operating)


# ------------------------------------------------------------- v369: Prologis PM package, pins and site pictures
SCOPE_LBL = {"pm": ("Preventive maintenance", "Mantenimiento preventivo"), "cleaning": ("Cleaning", "Limpieza"),
             "thermography": ("Thermography", "Termografía")}


def _service_card(s: R.Site, tt) -> str:
    """What Prologis's PM package says about the site: tenant, export scheme, since when it runs, the services
    Prologis asks for and the last cleaning. Nothing when the registry has none of it."""
    if not (s.customer or s.scope or s.scheme or s.in_operation or s.last_cleaning):
        return ""
    facts = []
    if s.customer:
        facts.append((tt("Tenant", "Inquilino"), UI.e(s.customer)))
    if s.solar_id:
        facts.append((tt("Solar ID", "ID solar"), UI.e(s.solar_id)))
    if s.scheme:
        facts.append((tt("Interconnection", "Interconexión"), UI.e(s.scheme)))
    if s.in_operation:
        facts.append((tt("In operation since", "En operación desde"), UI.e(s.in_operation)))
    if s.last_cleaning:
        m = s.months_since_cleaning(today_mx())
        facts.append((tt("Last cleaning", "Última limpieza"), f"{UI.e(s.last_cleaning)} · {m} {tt('months ago', 'meses')}"))
    else:
        facts.append((tt("Last cleaning", "Última limpieza"), tt("not recorded", "sin registro")))
    if s.pl_priority is not None:
        facts.append((tt("Prologis priority score", "Puntaje de prioridad Prologis"), f"{s.pl_priority:g}"))
    chips = "".join(f'<span class="pill s-lime">{UI.e(tt(*SCOPE_LBL[k]))}</span>' for k in R.SCOPES if k in s.scope) \
        or f'<span class="small muted">{tt("No service requested yet", "Aún sin servicios solicitados")}</span>'
    rows = "".join(f'<tr><td class="muted">{k}</td><td><b>{v}</b></td></tr>' for k, v in facts)
    order = (f'<a class="btn sm" href="/shop/">{tt("Order these services", "Pedir estos servicios")}</a>'
             if s.scope and S.can(g.user["role"], "order") else "")
    return (f'<div class="card" style="margin-top:16px"><h2>{tt("Maintenance requested by Prologis", "Mantenimiento solicitado por Prologis")}'
            f'<span class="r">{order}</span></h2><div class="legend" style="gap:8px;margin-bottom:10px">{chips}</div>'
            f'<table class="t">{rows}</table><p class="small muted" style="margin-top:8px">'
            f'{tt("Source: Prologis PM package, November 2026.", "Fuente: paquete de mantenimiento de Prologis, noviembre 2026.")}</p></div>')


def _loc_chip(s: R.Site, tt) -> str:
    if s.geo_approx:
        return f'<a class="chip" href="#location" style="border-color:#e0a100">📍 {tt("Approximate location - place the pin", "Ubicación aproximada - coloque el pin")}</a>'
    return f'<a class="chip" href="#location">📍 {tt("Location and picture", "Ubicación e imagen")}</a>'


def _photo_v(code: str) -> str:
    f = site_photo(code)
    if not f:
        return ""
    st = os.stat(f)
    return f"{st.st_mtime_ns:x}{st.st_size:x}"[-12:]


def _location_card(s: R.Site, tt) -> str:
    """The site on a map you can zoom and pan; people allowed to (Prologis managers, ARGIA) drag the pin to the
    building, save it, and save the satellite picture at the pin or upload their own."""
    can_pin = S.can(g.user["role"], "site_pin")
    ver = _photo_v(s.code)
    if s.pinned_by:
        cap = tt(f"Pin placed by hand by {s.pinned_by}", f"Pin colocado a mano por {s.pinned_by}")
    elif not s.geo_approx:
        cap = tt("The pin marks the building", "El pin marca la nave") + (f" ({UI.e(s.geo_note)})" if s.geo_note else "")
    else:
        cap = tt("Approximate location - place the pin on the building and save it", "Ubicación aproximada - coloque el pin sobre la nave y guárdelo") \
            + (f" ({UI.e(s.geo_note)})" if s.geo_note else "")
    cfg = json.dumps({"lat": s.lat, "lon": s.lon, "z": 18 if s.geo_approx else 19, "pin": True})
    edit = photo_forms = ""
    if can_pin:
        reset = (f'<button class="btn sm ghost" name="reset" value="1" formnovalidate>{tt("Back to the registry point", "Volver al punto del registro")}</button>'
                 if s.pinned_by else "")
        edit = (f'<form method="post" action="/sites/{s.code}/pin" class="pinbar" id="pinf">{csrf_field()}'
                f'<input type="hidden" name="lat"><input type="hidden" name="lon">'
                f'<button type="button" class="btn sm" id="pinedit">📍 {tt("Move pin", "Mover pin")}</button>'
                f'<span id="pinhelp" class="small muted" hidden>{tt("Drag the pin, or click the building on the map.", "Arrastre el pin o haga clic en la nave en el mapa.")}</span>'
                f'<label class="small" id="pinpic" hidden><input type="checkbox" name="photo" value="1" checked> {tt("also save the satellite picture here", "guardar también la imagen satelital aquí")}</label>'
                f'<button class="btn sm" id="pinsave" hidden>{tt("Save position", "Guardar posición")}</button>'
                f'<button type="button" class="btn sm ghost" id="pincancel" hidden>{tt("Cancel", "Cancelar")}</button>{reset}</form>')
        photo_forms = (f'<form method="post" action="/sites/{s.code}/photo" class="pinbar">{csrf_field()}<input type="hidden" name="src" value="map">'
                       f'<button class="btn sm ghost">🛰 {tt("Save satellite picture at the pin", "Guardar imagen satelital en el pin")}</button></form>'
                       f'<form method="post" action="/sites/{s.code}/photo" enctype="multipart/form-data" class="pinbar">{csrf_field()}'
                       f'<input type="hidden" name="src" value="upload"><input type="file" name="file" accept=".jpg,.jpeg,.png" required>'
                       f'<button class="btn sm ghost">{tt("Upload a picture", "Subir una imagen")}</button></form>')
    pic = (f'<img src="/sites/{s.code}/photo.jpg?v={ver}" alt="{UI.e(s.name)}" style="width:100%;border-radius:12px;display:block">'
           if ver else f'<div class="small muted" style="padding:30px 10px;text-align:center;border:1px dashed var(--line);border-radius:12px">'
           f'{tt("No picture yet.", "Aún sin imagen.")} {tt("Place the pin on the building, then save the satellite picture or upload one.", "Coloque el pin sobre la nave, luego guarde la imagen satelital o suba una.") if can_pin else ""}</div>')
    icon = UI.MAP_PIN.replace('class="satpin"', 'class="lpin"').replace("'", "&#39;")
    msg = {"saved": tt("Position saved.", "Posición guardada."), "reset": tt("Back to the registry point.", "De vuelta al punto del registro."),
           "photo": tt("Picture saved.", "Imagen guardada."), "nophoto": tt("Position saved, but no satellite picture could be fetched - try again or upload one.",
                                                                              "Posición guardada, pero no se pudo obtener la imagen satelital - intente de nuevo o suba una.")
           }.get(request.args.get("pin", ""), "")
    return f"""<div class="card" style="margin-top:16px" id="location"><h2>{tt("Location and picture", "Ubicación e imagen")}
<span class="r"><a href="{UI.e(LT.google_maps_url(s.lat, s.lon))}" target="_blank" rel="noopener">{tt("Open in Google Maps (satellite)", "Abrir en Google Maps (satélite)")} ›</a></span></h2>
{f'<p class="small" style="color:var(--teal)"><b>{msg}</b></p>' if msg else ""}
<div class="grid g2"><div><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<div id="pmap" class="pmap"></div>{edit}<div class="small muted" style="margin-top:6px">{UI.e(cap)} · Esri World Imagery</div></div>
<div>{pic}{photo_forms}</div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>(function(){{var C={cfg};var m=L.map('pmap',{{scrollWheelZoom:true,maxZoom:21}}).setView([C.lat,C.lon],C.z);
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri, Maxar, Earthstar Geographics',maxNativeZoom:19,maxZoom:21}}).addTo(m);
var st=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri',maxNativeZoom:19,maxZoom:21}});
L.control.layers({{'{tt("Satellite", "Satélite")}':sat,'{tt("Streets", "Calles")}':st}}).addTo(m);L.control.scale({{imperial:false}}).addTo(m);
var ic=L.divIcon({{html:'{icon}',className:'lpinwrap',iconSize:[34,46],iconAnchor:[17,46]}});
var mk=L.marker([C.lat,C.lon],{{icon:ic,keyboard:false}}).addTo(m);
var f=document.getElementById('pinf');if(!f)return;var b=document.getElementById('pinedit'),sv=document.getElementById('pinsave'),cn=document.getElementById('pincancel'),hp=document.getElementById('pinhelp'),pp=document.getElementById('pinpic'),on=false;
function put(ll){{mk.setLatLng(ll);f.lat.value=ll.lat.toFixed(6);f.lon.value=ll.lng.toFixed(6);sv.hidden=false;pp.hidden=false;}}
b.addEventListener('click',function(){{on=true;b.hidden=true;hp.hidden=false;cn.hidden=false;m.getContainer().classList.add('pinning');
mk.dragging.enable();mk.on('dragend',function(){{put(mk.getLatLng());}});}});
m.on('click',function(e){{if(on)put(e.latlng);}});
cn.addEventListener('click',function(){{location.reload();}});}})();</script></div>"""


def _gkey_safe() -> str:
    try:
        return gmaps_key()
    except Exception:                                  # noqa: BLE001
        return ""


@app.post("/sites/<code>/pin")
def site_pin(code):
    """v369: save the building's position dragged on the map (and, ticked by default, the satellite picture
    there), or go back to the registry's point."""
    need("site_pin")
    raw = R.load().site(code) or abort(404)
    c = db()
    if request.form.get("reset"):
        SP.clear_pin(c, raw.code, g.user["username"], ip())
        return redirect(f"/sites/{raw.code}/?pin=reset#location")
    try:
        lat, lon = SP.set_pin(c, raw.code, request.form.get("lat"), request.form.get("lon"), (raw.lat, raw.lon),
                              g.user["username"], ip())
    except SP.Invalid as ex:
        return Response(f"Position not saved: {html.escape(str(ex))}.", status=400, mimetype="text/plain")
    if request.form.get("photo"):
        try:
            data, src = SP.fetch_satellite(lat, lon, _gkey_safe())
            SP.save_photo(PHOTO_DIR, raw.code, data)
            S.audit(c, g.user["username"], ip(), "site_photo", raw.code, f"satellite ({src}) at {lat},{lon}")
        except SP.Invalid:
            return redirect(f"/sites/{raw.code}/?pin=nophoto#location")
        return redirect(f"/sites/{raw.code}/?pin=photo#location")
    return redirect(f"/sites/{raw.code}/?pin=saved#location")


@app.post("/sites/<code>/photo")
def site_photo_save(code):
    """v369: the site picture - the satellite view at the current pin, or a JPEG/PNG uploaded by hand."""
    need("site_pin")
    s = reg().site(code) or abort(404)
    c = db()
    try:
        if request.form.get("src") == "upload":
            f = request.files.get("file")
            if not f:
                raise SP.Invalid("no file")
            data = SP.as_jpeg(f.read(SP.MAX_PHOTO + 1))
            how = f"upload {os.path.basename(f.filename or '')[:60]}"
        else:
            data, src = SP.fetch_satellite(s.lat, s.lon, _gkey_safe())
            how = f"satellite ({src}) at {s.lat},{s.lon}"
        SP.save_photo(PHOTO_DIR, s.code, data)
    except SP.Invalid as ex:
        return Response(f"Picture not saved: {html.escape(str(ex))}.", status=400, mimetype="text/plain")
    S.audit(c, g.user["username"], ip(), "site_photo", s.code, how)
    return redirect(f"/sites/{s.code}/?pin=photo#location")


# ------------------------------------------------------------- v338: site handover from Prologis to ARGIA
def _sites_tabs(on: str, tt) -> str:
    tabs = [("list", "/sites/", tt("Sites", "Sitios")), ("handover", "/sites/handover/", tt("Handover", "Entrega"))]
    return '<div class="tabs">' + "".join(f'<a class="tab{" on" if k == on else ""}" href="{h}">{UI.e(lbl)}</a>' for k, h, lbl in tabs) + "</div>"


def _ho_bar(pct: float) -> str:
    return f'<div class="hbar" title="{pct * 100:.0f}%"><i style="width:{pct * 100:.1f}%"></i></div>'


def _ho_stage(stage: str, tt) -> str:
    en, es, cls = HO.STAGES[stage]
    return f'<span class="pill {cls}">{UI.e(tt(en, es))}</span>'


def _ho_card(c, s, tt) -> str:
    sm = HO.summary(HO.items(c, s.code), today_mx())
    cats = "".join(f'<span><b>{d}/{n}</b>&nbsp;{UI.e(tt(en, es))}</span>' for (cat, en, es) in HO.CATEGORIES for d, n in [sm["by_cat"][cat]])
    warn = ((f' <span class="pill s-bad">{sm["blocked"]} {tt("blocked", "bloqueados")}</span>' if sm["blocked"] else "")
            + (f' <span class="pill s-warn">{sm["overdue"]} {tt("overdue", "vencidos")}</span>' if sm["overdue"] else ""))
    return (f'<div class="card" style="margin-top:16px"><h2>{tt("Handover from Prologis", "Entrega de Prologis")} {_ho_stage(sm["stage"], tt)}{warn}'
            f'<span class="r"><a href="/sites/{s.code}/handover">{tt("Checklist", "Lista")} ›</a></span></h2>'
            f'<div style="display:flex;gap:14px;align-items:center"><div style="flex:1">{_ho_bar(sm["pct"])}</div><b>{sm["pct"] * 100:.0f}%</b>'
            f'<span class="small muted">{sm["done"]}/{sm["total"]}</span></div><div class="legend small" style="margin-top:8px">{cats}</div></div>')


@app.get("/sites/handover/")
def handover_portfolio():
    tt = t()
    rg, c = reg(), db()
    today = today_mx()
    rows, n_stage, sample, blocked, overdue = "", {k: 0 for k in HO.STAGES}, False, 0, 0
    for s in sorted(rg.sites, key=lambda x: (not x.operating, x.park, x.code)):
        sm = HO.summary(HO.items(c, s.code), today)
        n_stage[sm["stage"]] += 1
        sample = sample or sm["sample"]
        blocked += sm["blocked"]
        overdue += sm["overdue"]
        cells = "".join(
            f'<td class="n"><span class="pill {"s-ok" if n and d == n else ("s-info" if d else "s-off")}">{d}/{n}</span></td>'
            for cat, *_ in HO.CATEGORIES for d, n in [sm["by_cat"][cat]])
        flags = ((f' <span class="pill s-bad">{sm["blocked"]}&nbsp;{tt("blocked", "bloq.")}</span>' if sm["blocked"] else "")
                 + (f' <span class="pill s-warn">{sm["overdue"]}&nbsp;{tt("overdue", "vencidos")}</span>' if sm["overdue"] else ""))
        rows += (f'<tr><td><a href="/sites/{s.code}/handover"><b>{UI.e(s.name)}</b></a><div class="small muted">{s.code} · {UI.e(s.city)}</div></td>'
                 f'<td>{_ho_stage(sm["stage"], tt)}{flags}</td><td style="min-width:150px">{_ho_bar(sm["pct"])}</td>'
                 f'<td class="n"><b>{sm["pct"] * 100:.0f}%</b></td>{cells}<td class="small muted nw">{UI.e(sm["last"][:10])}</td></tr>')
    heads = "".join(f'<th class="n">{UI.e(tt(en, es))}</th>' for _, en, es in HO.CATEGORIES)
    tiles = "".join(f'<div class="kpi"><div class="l">{UI.e(tt(en, es))}</div><div class="v">{n_stage[k]}</div></div>' for k, (en, es, _) in HO.STAGES.items())
    tiles += (f'<div class="kpi"><div class="l">{tt("Blocked items", "Puntos bloqueados")}</div><div class="v">{blocked}</div></div>'
              f'<div class="kpi"><div class="l">{tt("Overdue items", "Puntos vencidos")}</div><div class="v">{overdue}</div></div>')
    note = (f'<p class="small muted" style="margin-top:8px"><span class="pill s-warn">{tt("SAMPLE", "MUESTRA")}</span> '
            f'{tt("Sample handover data, to show the module. Any real update replaces it.", "Datos de entrega de muestra, para mostrar el módulo. Cualquier actualización real los reemplaza.")}</p>' if sample else "")
    body = f"""<div class="kick">{tt("Sites", "Sitios")}</div><h1 class="pt">{tt("Handover from Prologis", "Entrega de Prologis")}</h1>{_sites_tabs("handover", tt)}
<p class="muted" style="max-width:980px">{tt("Where each site stands in the takeover: documents received, joint walkthrough, measurements, data access and the signed protocols. Open a site for its checklist.", "Dónde está cada sitio en la entrega: documentos recibidos, recorrido conjunto, mediciones, acceso a datos y actas firmadas. Abra un sitio para ver su lista.")}</p>{note}
<div class="hero" style="padding:16px 20px"><div class="kpis" style="margin-top:0">{tiles}</div></div>
<div class="card" style="margin-top:16px;overflow-x:auto"><table class="t"><tr><th>{tt("Site", "Sitio")}</th><th>{tt("Stage", "Etapa")}</th><th>{tt("Progress", "Avance")}</th><th class="n">%</th>{heads}<th>{tt("Last update", "Última actualización")}</th></tr>{rows}</table></div>"""
    return page(tt("Handover", "Entrega"), body, "sites")


@app.get("/sites/<code>/handover")
def handover_site(code, msg: str = "", err: bool = False):
    tt = t()
    rg, c = reg(), db()
    s = rg.site(code) or abort(404)
    today = today_mx()
    its = HO.items(c, s.code)
    sm = HO.summary(its, today)
    edit = S.can(g.user["role"], "handover_edit")
    opts = lambda cur: "".join(f'<option value="{k}"{" selected" if k == cur else ""}>{UI.e(tt(en, es))}</option>' for k, (en, es, _) in HO.STATUSES.items())
    cards = ""
    for cat, cen, ces in HO.CATEGORIES:
        d, n = sm["by_cat"][cat]
        trs = ""
        for i in (x for x in its if x["cat"] == cat):
            en, es, cls = HO.STATUSES[i["status"]]
            late = i["status"] not in (HO.DONE, HO.NA) and i["due"] and i["due"] < today.isoformat()
            late_pill = f' <span class="pill s-warn">{tt("overdue", "vencido")}</span>'
            who = tt(*HO.WHO[i["who"]])
            meta = (f'<div class="small muted">{UI.e(i["updated_by"])} · {UI.e(i["updated_utc"][:16])}'
                    f'{" · " + tt("sample", "muestra") if i["sample"] else ""}</div>' if i["updated_utc"] else "")
            if edit:
                ctl = (f'<form method="post" action="/sites/{s.code}/handover" class="hof" id="f-{i["key"]}">{csrf_field()}'
                       f'<input type="hidden" name="item" value="{i["key"]}"><select name="status" aria-label="{tt("Status", "Estado")}">{opts(i["status"])}</select>'
                       f'<input type="date" name="due" value="{UI.e(i["due"])}" aria-label="{tt("Due", "Fecha límite")}">'
                       f'<input type="text" name="note" maxlength="{HO.NOTE_MAX}" value="{UI.e(i["note"])}" placeholder="{tt("Note", "Nota")}" aria-label="{tt("Note", "Nota")}">'
                       f'<button class="btn sm" type="submit">{tt("Save", "Guardar")}</button></form>')
            else:
                ctl = (f'<span class="small">{UI.e(i["due"])}</span> <span class="small muted">{UI.e(i["note"])}</span>')
            trs += (f'<tr id="i-{i["key"]}"><td>{UI.e(tt(i["en"], i["es"]))}{meta}</td><td class="nw small">{UI.e(who)}</td>'
                    f'<td class="nw"><span class="pill {cls}">{UI.e(tt(en, es))}</span>{late_pill if late else ""}</td><td>{ctl}</td></tr>')
        link = (f'<span class="r"><a href="/docs/?site={s.code}">{tt("Document folder", "Carpeta de documentos")} ›</a></span>' if cat == "docs" else "")
        cards += (f'<div class="card" style="margin-top:16px"><h2>{UI.e(tt(cen, ces))} <span class="pill {"s-ok" if n and d == n else "s-info" if d else "s-off"}">{d}/{n}</span>{link}</h2>'
                  f'<div style="overflow-x:auto"><table class="t hot"><colgroup><col style="width:34%"><col style="width:8%"><col style="width:13%"><col></colgroup>'
                  f'<tr><th>{tt("Item", "Punto")}</th><th>{tt("Delivers", "Entrega")}</th><th>{tt("Status", "Estado")}</th>'
                  f'<th>{tt("Due date and note", "Fecha límite y nota")}</th></tr>{trs}</table></div></div>')
    flash = f'<div class="flash{" err" if err else ""}">{UI.e(msg)}</div>' if msg else ""
    body = f"""<div class="kick"><a href="/sites/handover/">{tt("Handover", "Entrega")}</a> · {UI.e(s.park)}</div><h1 class="pt">{UI.e(s.name)}</h1>{flash}
<div class="card" style="margin-top:12px"><div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">{_ho_stage(sm["stage"], tt)}
<div style="flex:1;min-width:200px">{_ho_bar(sm["pct"])}</div><b style="font-size:20px">{sm["pct"] * 100:.0f}%</b><span class="muted">{sm["done"]}/{sm["total"]} {tt("items done", "puntos hechos")}</span>
{f'<span class="pill s-bad">{sm["blocked"]} {tt("blocked", "bloqueados")}</span>' if sm["blocked"] else ""}{f'<span class="pill s-warn">{sm["overdue"]} {tt("overdue", "vencidos")}</span>' if sm["overdue"] else ""}
<a class="btn sm ghost" href="/sites/{s.code}/">{tt("Site page", "Página del sitio")} ›</a></div>
<p class="small muted" style="margin:10px 0 0">{tt("Prologis delivers the documents, the data access and the spare parts; ARGIA records the walkthrough and the measurements; both sides agree the punch list and sign the protocol. Every change is in the audit log.", "Prologis entrega los documentos, el acceso a datos y las refacciones; ARGIA registra el recorrido y las mediciones; ambas partes acuerdan los pendientes y firman el acta. Cada cambio queda en la bitácora de auditoría.")}</p></div>{cards}"""
    return page(f'{tt("Handover", "Entrega")} · {s.name}', body, "sites")


@app.post("/sites/<code>/handover")
def handover_set(code):
    need("handover_edit")
    rg, c = reg(), db()
    s = rg.site(code) or abort(404)
    f = request.form
    try:
        HO.set_item(c, s.code, f.get("item", ""), f.get("status", ""), f.get("due", ""), f.get("note", ""),
                    g.user["username"], [x.code for x in rg.sites])
    except (HO.HandoverError, ValueError) as e:
        return handover_site(code, str(e), True)
    S.audit(c, g.user["username"], ip(), "handover_item", f"{s.code}/{f.get('item')}", f"{f.get('status')} due={f.get('due', '')}")
    return redirect(f"/sites/{s.code}/handover#i-{f.get('item')}")


# ------------------------------------------------------------------ tickets
@app.get("/tickets/")
def tickets():
    tt = t()
    rg = reg()
    c = db()
    show = request.args.get("show", "open")
    kind = "order" if request.args.get("kind") == "order" else "incident"
    nowu = utc_now()
    results = [sla_of(c, tk, nowu)[2] for tk in c.execute("SELECT * FROM tickets WHERE kind='incident'").fetchall()]
    comp = SLA.compliance(results)
    n_open, n_orders = len(open_tickets(c)), len(S.open_orders(c))
    rows = ""
    if kind == "incident":
        rows_db = open_tickets(c) if show == "open" else c.execute("SELECT * FROM tickets WHERE kind='incident' ORDER BY id DESC").fetchall()
        for tk in rows_db:
            cls, used, st = sla_of(c, tk, nowu)
            s = rg.site(tk["site_code"])
            stl = dict((k, tt(en, es)) for k, en, es in S.TICKET_STATUSES).get(tk["status"], tk["status"])
            appr = {"pending": f'<span class="pill s-warn">{tt("Approval pending", "Aprobación pendiente")}</span>',
                    "approved": f'<span class="pill s-ok">{tt("Approved", "Aprobado")}</span>',
                    "rejected": f'<span class="pill s-bad">{tt("Rejected", "Rechazado")}</span>'}.get(tk["approval"], "")
            rows += (f'<tr><td class="nw"><a href="/tickets/{tk["number"]}/"><b>{tk["number"]}</b></a>{SAMPLE_PILL if tk["sample"] else ""}</td>'
                     f'<td>{UI.e(tk["title"])}<div class="small muted">{UI.e(s.name if s else tk["site_code"])}</div></td>'
                     f'<td><span class="pill s-off">{cls.priority}</span> <span class="small">{UI.e(tt(cls.en, cls.es))}</span></td>'
                     f'<td>{stl} {appr}</td><td>{sla_pill(cls, used, st, tt)}</td><td class="small muted">{mx_time(tk["detected_utc"])}</td></tr>')
        head = (f'<tr><th>#</th><th>{tt("Issue", "Asunto")}</th><th>{tt("MSA class", "Clase MSA")}</th><th>{tt("Status", "Estado")}</th>'
                f'<th>{tt("Response clock", "Reloj de respuesta")}</th><th>{tt("Detected", "Detectado")}</th></tr>')
    else:
        rows_db = S.open_orders(c) if show == "open" else c.execute("SELECT * FROM tickets WHERE kind='order' ORDER BY id DESC").fetchall()
        for tk in rows_db:
            lines = S.as_lines(S.order_lines(c, tk["id"]))
            tot = CAT.totals(lines)
            sites = sorted({ln.site_code for ln in lines if ln.site_code})
            where = (rg.site(sites[0]).name if len(sites) == 1 and rg.site(sites[0]) else
                     (f'{len(sites)} {tt("sites", "sitios")}' if sites else tt("Portfolio", "Portafolio")))
            when = tk["scheduled_date"] or tk["target_date"] or "-"
            when_note = "" if tk["scheduled_date"] else ' <span class="small muted">' + tt("earliest", "más temprana") + "</span>"
            rows += (f'<tr><td class="nw"><a href="/tickets/{tk["number"]}/"><b>{tk["number"]}</b></a></td>'
                     f'<td>{UI.e(tk["title"])}<div class="small muted">{UI.e(where)} · {tot["lines"]} {tt("lines", "líneas")}{" · PO " + UI.e(tk["po_number"]) if tk["po_number"] else ""}</div></td>'
                     f'<td>{order_pill(tk, tt)}</td><td class="n nw">{_mxn(tot["total"])}{"*" if tot["on_quote"] else ""}</td>'
                     f'<td class="nw">{UI.e(when)}{when_note}</td>'
                     f'<td class="small muted">{mx_time(tk["created_utc"])}</td></tr>')
        head = (f'<tr><th>#</th><th>{tt("Order", "Pedido")}</th><th>{tt("Status", "Estado")}</th><th class="n">{tt("Total incl. IVA", "Total con IVA")}</th>'
                f'<th>{tt("Date", "Fecha")}</th><th>{tt("Ordered", "Pedido")}</th></tr>')
    rows = rows or f'<tr><td class="muted" colspan="6">{tt("Nothing here.", "Nada aquí.")}</td></tr>'
    new = f'<a class="btn" href="/tickets/new">{tt("New ticket", "Nuevo ticket")}</a>' if S.can(g.user["role"], "ticket_new") else ""
    shop_btn = f'<a class="btn ghost" href="/shop/">{tt("Order a service", "Pedir un servicio")}</a>'
    other = "all" if show == "open" else "open"
    toggle = tt("Show all" if show == "open" else "Open only", "Ver todos" if show == "open" else "Solo abiertos")
    tabs = (f'<div class="tabs"><a class="tab{" on" if kind == "incident" else ""}" href="/tickets/">{tt("Incidents", "Incidencias")} <b>{n_open}</b></a>'
            f'<a class="tab{" on" if kind == "order" else ""}" href="/tickets/?kind=order">{tt("Service orders", "Órdenes de servicio")} <b>{n_orders}</b></a></div>')
    body = f"""<div style="display:flex;align-items:end;gap:12px;flex-wrap:wrap"><div><div class="kick">{tt("Operation & maintenance", "Operación y mantenimiento")}</div>
<h1 class="pt">{tt("Tickets", "Tickets")}</h1></div><div style="margin-left:auto;display:flex;gap:8px">{new}{shop_btn}
<a class="btn ghost" href="?show={other}{"&kind=order" if kind == "order" else ""}">{toggle}</a></div></div>
<div class="grid g3"><div class="card"><h2>{tt("Response-time compliance", "Cumplimiento de tiempo de respuesta")}</h2><div style="font-size:30px;font-weight:800;color:var(--deep)">{"-" if comp is None else f"{comp * 100:.0f}%"}</div><div class="small muted">{tt("closed clocks that met the MSA deadline", "relojes cerrados dentro del plazo del MSA")}</div></div>
<div class="card"><h2>{tt("Open", "Abiertos")}</h2><div style="font-size:30px;font-weight:800;color:var(--deep)">{n_open} <small style="font-size:14px;color:var(--muted)">+ {n_orders} {tt("orders", "pedidos")}</small></div><div class="small muted">{tt("incidents: new, responded, in progress or waiting", "incidencias: nuevas, atendidas, en curso o en espera")}</div></div>
<div class="card"><h2>{tt("How the clock works", "Cómo corre el reloj")}</h2><div class="small">{tt("Classes and deadlines are the MSA's (Schedule A). The clock starts at detection, or at Prologis's dispatch approval where the MSA requires it, and stops while waiting on Prologis or site access.", "Clases y plazos del MSA (Anexo A). El reloj inicia en la detección, o en la aprobación de despacho de Prologis cuando el MSA lo exige, y se detiene mientras se espera a Prologis o al acceso.")}</div></div></div>
{tabs}<div class="card" style="margin-top:10px;overflow-x:auto"><table class="t">{head}{rows}</table></div>"""
    return page(tt("Tickets", "Tickets"), body, "tickets")


@app.get("/tickets/new")
def ticket_new(msg: str = ""):
    need("ticket_new")
    tt = t()
    rg = reg()
    pre = request.args.get("site", "")
    kind = request.args.get("kind", "")
    sug = SLA.suggest(None, comm_loss=(kind == "comm_loss")) if kind else "OTHER"
    if kind == "inverter_fault":
        s = rg.site(pre)
        sug = SLA.suggest(s.kwp * M.AC_RATIO * 0.33 if s else 50)
    opts = "".join(f'<option value="{s.code}" {"selected" if s.code == pre else ""}>{UI.e(s.name)} ({s.code})</option>' for s in rg.sites)
    copts = "".join(f'<option value="{k.code}" {"selected" if k.code == sug else ""}>{k.priority} · {UI.e(tt(k.en, k.es))}</option>' for k in SLA.CLASSES)
    title = {"inverter_fault": tt("Inverter fault - production loss", "Falla de inversor - pérdida de producción"),
             "comm_loss": tt("Communication loss", "Pérdida de comunicación")}.get(kind, "")
    body = f"""<div class="card form"><div class="kick">{tt("New ticket", "Nuevo ticket")}</div><h1 class="pt">{tt("Report an issue", "Reportar un asunto")}</h1>{flash(msg, True)}
<form method="post" action="/tickets/new">{csrf_field()}<label>{tt("Site", "Sitio")}</label><select name="site" required>{opts}</select>
<label>{tt("Title", "Título")}</label><input name="title" value="{UI.e(title)}" required maxlength="160">
<label>{tt("MSA response class", "Clase de respuesta MSA")}</label><select name="sla">{copts}</select>
<div class="row"><div><label>{tt("kW affected (optional)", "kW afectados (opcional)")}</label><input name="kw" inputmode="decimal"></div>
<div><label>{tt("Cost estimate MXN (corrective work)", "Estimado MXN (correctivo)")}</label><input name="est" inputmode="decimal"></div></div>
<label><input type="checkbox" name="approval" value="1" style="width:auto"> {tt("Needs Prologis approval before dispatch (unscheduled / corrective work)", "Requiere aprobación de Prologis antes del despacho (correctivo / no programado)")}</label>
<label>{tt("Description", "Descripción")}</label><textarea name="desc"></textarea>
<div style="margin-top:14px"><button class="btn">{tt("Open ticket", "Abrir ticket")}</button></div></form></div>"""
    return page(tt("New ticket", "Nuevo ticket"), body, "tickets")


def _float(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", "")) if str(v or "").strip() else None
    except ValueError:
        return None


@app.post("/tickets/new")
def ticket_new_post():
    need("ticket_new")
    rg = reg()
    s = rg.site(request.form.get("site", ""))
    cls = request.form.get("sla", "OTHER")
    title = (request.form.get("title") or "").strip()
    if not s or cls not in SLA.BY_CODE or not title:
        return ticket_new("Site, class and title are required. / Sitio, clase y título son obligatorios.")
    tk = S.create_ticket(db(), s.code, title, request.form.get("desc", ""), cls, g.user["username"],
                         kw_lost=_float(request.form.get("kw")), estimate_mxn=_float(request.form.get("est")),
                         needs_approval=bool(request.form.get("approval")) and SLA.BY_CODE[cls].approval_needed, ip=ip())
    return redirect(f"/tickets/{tk['number']}/")


def _ticket(number: str):
    tk = db().execute("SELECT * FROM tickets WHERE number=?", (number,)).fetchone()
    if not tk:
        abort(404)
    return tk


@app.get("/tickets/<number>/")
def ticket_page(number):
    tk = _ticket(number)
    if tk["kind"] == "order":
        return order_page(tk)
    tt = t()
    c = db()
    rg = reg()
    s = rg.site(tk["site_code"])
    cls, used, st = sla_of(c, tk, utc_now())
    evs = S.ticket_events(c, tk["id"])
    files = {d["id"]: d for d in c.execute("SELECT * FROM documents WHERE ticket_id=? AND deleted=0", (tk["id"],))}
    tl = ""
    for ev in evs:
        m = json.loads(ev["meta"] or "{}")
        what = {"created": tt("opened the ticket", "abrió el ticket"), "comment": tt("commented", "comentó"),
                "status": f'{tt("moved it to", "lo pasó a")} <b>{UI.e(m.get("to", ""))}</b>',
                "approval": tt("approved dispatch" if m.get("approved") else "rejected dispatch", "aprobó el despacho" if m.get("approved") else "rechazó el despacho"),
                "file": tt("attached a file", "adjuntó un archivo")}.get(ev["kind"], ev["kind"])
        att = ""
        if ev["kind"] == "file" and m.get("doc") in files:
            d = files[m["doc"]]
            att = f'<div><a class="chip" href="/docs/{d["id"]}/download">📎 {UI.e(d["name"])}</a></div>'
        body_html = ("<div>" + UI.e(ev["body"]) + "</div>") if ev["body"] else ""
        tl += (f'<div class="ev"><div class="small muted">{mx_time(ev["ts_utc"])} · <b>{UI.e(ev["username"])}</b> {what}</div>'
               f'{body_html}{att}</div>')
    role = g.user["role"]
    acts = ""
    if S.can(role, "ticket_work"):
        btns = "".join(f'<button class="btn sm ghost" name="to" value="{n}">{UI.e(dict((k, tt(en, es)) for k, en, es in S.TICKET_STATUSES)[n])}</button> '
                       for n in S.TRANSITIONS.get(tk["status"], ()))
        acts += f'<form method="post" action="/tickets/{number}/status">{csrf_field()}<label>{tt("Move to", "Mover a")}</label><input name="note" placeholder="{tt("note (optional)", "nota (opcional)")}"><div style="margin-top:8px">{btns}</div></form>'
    est_txt = (" - MXN " + UI.num(tk["estimate_mxn"])) if tk["estimate_mxn"] else ""
    if tk["approval"] == "pending" and S.can(role, "ticket_approve"):
        acts += (f'<form method="post" action="/tickets/{number}/approve" style="margin-top:12px">{csrf_field()}<label>{tt("Prologis dispatch approval", "Aprobación de despacho Prologis")}'
                 f'{est_txt}</label><input name="note" placeholder="PO / {tt("comment", "comentario")}">'
                 f'<div style="margin-top:8px"><button class="btn sm" name="ok" value="1">{tt("Approve", "Aprobar")}</button> <button class="btn sm danger" name="ok" value="0">{tt("Reject", "Rechazar")}</button></div></form>')
    if S.can(role, "ticket_work"):
        acts += (f'<form method="post" action="/tickets/{number}/eta" style="margin-top:12px;display:flex;gap:6px;align-items:end;flex-wrap:wrap">{csrf_field()}'
                 f'<div><label>{tt("Estimated return to service", "Regreso estimado a servicio")}</label><input name="eta" value="{UI.e(tk["eta_date"])}" placeholder="YYYY-MM-DD" style="width:140px"></div>'
                 f'<button class="btn sm ghost">{tt("Set", "Fijar")}</button></form>')
    acts += (f'<form method="post" action="/tickets/{number}/comment" enctype="multipart/form-data" style="margin-top:12px">{csrf_field()}<label>{tt("Comment", "Comentario")}</label>'
             f'<textarea name="body"></textarea><input type="file" name="file" style="margin-top:6px"><div style="margin-top:8px"><button class="btn sm">{tt("Add", "Agregar")}</button></div></form>')
    chips = (('<span class="chip">' + UI.num(tk["kw_lost"]) + ' kW</span>') if tk["kw_lost"] else "") + \
        (('<span class="chip">' + tt("Back in service by", "En servicio para") + ' ' + UI.e(tk["eta_date"]) + '</span>') if tk["eta_date"] else "") + \
        (('<span class="chip">MXN ' + UI.num(tk["estimate_mxn"]) + '</span>') if tk["estimate_mxn"] else "")
    body = f"""<div class="kick">{UI.e(s.name if s else tk["site_code"])} · {tk["number"]}{" · SAMPLE" if tk["sample"] else ""}</div><h1 class="pt">{UI.e(tk["title"])}</h1>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0"><span class="chip">{cls.priority} · {UI.e(tt(cls.en, cls.es))}</span><span class="chip">{UI.e(dict((k, tt(en, es)) for k, en, es in S.TICKET_STATUSES)[tk["status"]])}</span>{sla_pill(cls, used, st, tt)}
{chips}</div>
<div class="grid g2"><div class="card"><h2>{tt("Timeline", "Historial")}</h2><div class="tl">{tl}</div></div><div class="card"><h2>{tt("Actions", "Acciones")}</h2>
<div class="small muted">{tt("Detected", "Detectado")} {mx_time(tk["detected_utc"])} · {tt("Responded", "Atendido")} {mx_time(tk["responded_utc"])} · {tt("Resolved", "Resuelto")} {mx_time(tk["resolved_utc"])}</div>{acts}</div></div>"""
    return page(tk["number"], body, "tickets")


@app.post("/tickets/<number>/status")
def ticket_status(number):
    tk = _ticket(number)
    if tk["kind"] == "order":
        need("order")
        try:
            S.set_order_status(db(), tk, request.form.get("to", ""), g.user["username"], g.user["role"],
                               request.form.get("note", ""), (request.form.get("scheduled") or "").strip(), ip())
        except PermissionError:
            abort(403)
        except ValueError:
            abort(400)
        return redirect(f"/tickets/{number}/")
    need("ticket_work")
    try:
        S.set_status(db(), tk, request.form.get("to", ""), g.user["username"], request.form.get("note", ""), ip())
    except ValueError:
        abort(400)
    return redirect(f"/tickets/{number}/")


@app.post("/tickets/<number>/approve")
def ticket_approve(number):
    need("ticket_approve")
    try:
        S.set_approval(db(), _ticket(number), request.form.get("ok") == "1", g.user["username"], request.form.get("note", ""), ip())
    except ValueError:
        abort(400)
    return redirect(f"/tickets/{number}/")


def _upload(f, site: str, folder: str, ticket_id: Optional[int] = None) -> Optional[int]:
    if not f or not f.filename:
        return None
    name = os.path.basename(f.filename).replace("\\", "_")[:180]
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        abort(415)
    data = f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        abort(413)
    kind = SC.executable_kind(data)
    if kind:                                  # v324: never store an executable, whatever its name
        S.audit(db(), g.user["username"], ip(), "upload_refused", name, kind)
        abort(415)
    return S.add_document(db(), FILES_DIR, site, folder, name, data, mimetypes.guess_type(name)[0] or "application/octet-stream",
                          g.user["username"], ticket_id=ticket_id, ip=ip())


@app.post("/tickets/<number>/comment")
def ticket_comment(number):
    need("comment")
    tk = _ticket(number)
    c = db()
    did = _upload(request.files.get("file"), tk["site_code"], "ticket", tk["id"]) if request.files.get("file") else None
    body = (request.form.get("body") or "").strip()
    if body:
        S.add_event(c, tk["id"], g.user["username"], "comment", body)
        S.audit(c, g.user["username"], ip(), "ticket_comment", number)
    if did:
        S.add_event(c, tk["id"], g.user["username"], "file", "", {"doc": did})
    return redirect(f"/tickets/{number}/")


# ------------------------------------------------------------------ service shop (v293)
def order_label(st: str, tt) -> str:
    return dict((k, tt(en, es)) for k, en, es in CAT.ORDER_STATUSES).get(st, st)


def order_pill(tk, tt) -> str:
    st = tk["status"]
    cls = {"ORDERED": "s-info", "CONFIRMED": "s-lime", "SCHEDULED": "s-lime", "IN_PROGRESS": "s-warn",
           "COMPLETED": "s-ok", "INVOICED": "s-ok", "CANCELLED": "s-off"}.get(st, "s-off")
    out = f'<span class="pill {cls}">{UI.e(order_label(st, tt))}</span>'
    if st == "ORDERED":
        out += {"quote_needed": f' <span class="pill s-warn">{tt("ARGIA preparing quote", "ARGIA cotizando")}</span>',
                "pending": f' <span class="pill s-warn">{tt("Quote awaiting Prologis", "Cotización por aceptar")}</span>',
                "rejected": f' <span class="pill s-bad">{tt("Quote declined", "Cotización rechazada")}</span>'}.get(tk["approval"], "")
    return out


def _mxn(v) -> str:
    return "-" if v is None else "MXN " + UI.num(v, 2)


def _item_name(it, tt) -> str:
    return tt(it.name_en, it.name_es or it.name_en)


def _cart_count() -> int:
    return len(S.cart(db(), g.user["username"])) if g.get("user") else 0


def _shop_head(tt, sub: str) -> str:
    n = _cart_count()
    cart_btn = (f'<a class="btn" href="/shop/cart">{tt("Cart", "Carrito")} <span class="cartn">{n}</span></a>'
                if S.can(g.user["role"], "order") else "")
    admin_btn = (f'<a class="btn ghost" href="/admin/services/">{tt("Manage services & prices", "Gestionar servicios y precios")}</a>'
                 if S.can(g.user["role"], "catalog_edit") else "")
    return (f'<div class="shophero"><div><div class="kick" style="color:#9fe3dc">{tt("Service shop", "Tienda de servicios")}</div>'
            f'<h1>{tt("Order O&M services for your rooftops", "Pida servicios O&M para sus techos")}</h1><div class="sub">{sub}</div></div>'
            f'<div class="acts">{admin_btn}{cart_btn}</div></div>')


@app.get("/shop/")
def shop():
    tt = t()
    c = db()
    items = S.catalog(c)
    cat = request.args.get("cat", "")
    tabs = [("", tt("All services", "Todos"))] + [(k, tt(en, es)) for k, en, es in CAT.CATEGORIES
                                                 if k != "rates" and any(i.category == k and i.orderable for i in items)]
    chips = "".join(f'<a class="tab{" on" if k == cat else ""}" href="/shop/{"?cat=" + k if k else ""}">{UI.e(lbl)}</a>' for k, lbl in tabs)
    cards = ""
    for k, en, es in CAT.CATEGORIES:
        if k == "rates" or (cat and k != cat):
            continue
        grp = [i for i in items if i.category == k and i.orderable]
        if not grp:
            continue
        cards += f'<h2 class="cath">{UI.e(tt(en, es))}</h2><div class="shopgrid">'
        for it in grp:
            inc = tt(it.includes_en, it.includes_es or it.includes_en)
            inc_html = "".join(f"<li>{UI.e(x.strip())}</li>" for x in inc.split(";") if x.strip())[:2000]
            basis = (f'<span class="pill s-ok">{tt("Contract price", "Precio de contrato")}</span>' if it.basis == "contract"
                     else (f'<span class="pill s-info">{tt("Quoted per job", "Cotizado por trabajo")}</span>' if it.on_quote else ""))
            cards += (f'<a class="svc" href="/shop/{it.code}"><div class="cat">{UI.e(tt(en, es))}</div><h3>{UI.e(_item_name(it, tt))}</h3>'
                      f'<p>{UI.e(tt(it.desc_en, it.desc_es or it.desc_en))}</p>{"<ul>" + inc_html + "</ul>" if inc_html else ""}'
                      f'<div class="foot2"><div class="price">{UI.e(CAT.price_text(it, tt))}</div><div class="small muted">{tt("Lead time", "Plazo")} {it.lead_days} {tt("days", "días")}</div>{basis}</div></a>')
        cards += "</div>"
    rates = [i for i in items if not i.orderable]
    rate_rows = "".join(f'<tr><td><b>{UI.e(_item_name(i, tt))}</b><div class="small muted">{UI.e(tt(i.desc_en, i.desc_es or i.desc_en))}</div></td>'
                        f'<td class="n nw">{UI.e(CAT.price_text(i, tt))}</td></tr>' for i in rates)
    rate_card = (f'<div class="card" style="margin-top:22px"><h2>{UI.stripe_svg(16)} {tt("Rate card & additional costs", "Tarifas y costos adicionales")}'
                 f'<span class="r muted">{tt("prices in MXN before IVA", "precios en MXN antes de IVA")}</span></h2><table class="t">{rate_rows}</table></div>') if rates and not cat else ""
    empty = "" if cards else f'<div class="card" style="margin-top:16px">{tt("No services published yet.", "Aún no hay servicios publicados.")}</div>'
    sub = tt("Fixed contract prices where the MSA sets them, a quote within 2 business days for the rest. Every order becomes a tracked service order.",
             "Precios fijos de contrato donde el MSA los define y cotización en 2 días hábiles para lo demás. Cada pedido se vuelve una orden de servicio con seguimiento.")
    body = _shop_head(tt, sub) + f'<div class="tabs">{chips}</div>' + cards + empty + rate_card
    return page(tt("Service shop", "Tienda de servicios"), body, "shop")


@app.get("/shop/<code>")
def shop_item(code, msg: str = ""):
    tt = t()
    c = db()
    it = S.catalog_item(c, code)
    if not it or not it.published or not it.active or not it.orderable:
        abort(404)
    rg = reg()
    can_order = S.can(g.user["role"], "order")
    inc = tt(it.includes_en, it.includes_es or it.includes_en)
    inc_html = "".join(f"<li>{UI.e(x.strip())}</li>" for x in inc.split(";") if x.strip())
    auto = it.unit in CAT.AUTO_QTY
    unit_en, unit_es = CAT.UNITS[it.unit]
    boxes = ""
    for s in sorted(rg.sites, key=lambda x: (not x.operating, x.name)):
        boxes += (f'<label class="site"><input type="checkbox" name="site" value="{s.code}" data-kwp="{s.kwp}"{"" if s.operating else " data-pre=1"}> '
                  f'<span><b>{UI.e(s.name)}</b><span class="small muted"> · {UI.num(s.kwp, 0)} kWp{"" if s.operating else " · PTO " + UI.e(s.pto)}</span></span></label>')
    qty_html = "" if auto else (f'<label>{tt("Quantity", "Cantidad")} ({UI.e(tt(unit_en, unit_es))}) {tt("for each selected site", "por cada sitio")}</label>'
                                f'<input name="qty" id="qty" inputmode="decimal" value="1" required>')
    price_js = "null" if it.price_mxn is None else repr(float(it.price_mxn))
    form = ""
    if can_order:
        form = f"""<form method="post" action="/shop/add" id="of">{csrf_field()}<input type="hidden" name="item" value="{it.code}">
<label>{tt("Sites", "Sitios")} <a href="#" id="allop" class="small" style="margin-left:8px">{tt("select all operating", "todos los operando")}</a> <a href="#" id="none" class="small" style="margin-left:6px">{tt("clear", "limpiar")}</a></label>
<div class="sitebox">{boxes}</div>{qty_html}
<label>{tt("Note for the crew (optional)", "Nota para la cuadrilla (opcional)")}</label><input name="note" maxlength="300">
<div class="est"><div><div class="small muted">{tt("Estimate before IVA", "Estimado antes de IVA")}</div><div class="big" id="est">-</div><div class="small muted" id="estd"></div></div>
<button class="btn">{tt("Add to cart", "Agregar al carrito")}</button></div></form>
<script>(function(){{var P={price_js},U='{it.unit}',f=document.getElementById('of');function upd(){{var b=f.querySelectorAll('input[name=site]:checked'),n=b.length,k=0;b.forEach(function(x){{k+=parseFloat(x.dataset.kwp)}});
var q=document.getElementById('qty');q=q?parseFloat(q.value.replace(',',''))||0:0;var e=document.getElementById('est'),d=document.getElementById('estd');
if(P===null){{e.textContent='{tt("On quote", "Bajo cotización")}';d.textContent=n+' {tt("site(s)", "sitio(s)")}';return}}
var v=U==='kwp'?P*k:(U==='site'?P*n:P*q*Math.max(1,n));e.textContent='MXN '+v.toLocaleString('en-US',{{maximumFractionDigits:0}});
d.textContent=n+' {tt("site(s)", "sitio(s)")}'+(U==='kwp'?' · '+k.toLocaleString('en-US',{{maximumFractionDigits:0}})+' kWp':'')}}
f.addEventListener('change',upd);f.addEventListener('input',upd);
document.getElementById('allop').onclick=function(ev){{ev.preventDefault();f.querySelectorAll('input[name=site]').forEach(function(x){{x.checked=!x.dataset.pre}});upd()}};
document.getElementById('none').onclick=function(ev){{ev.preventDefault();f.querySelectorAll('input[name=site]').forEach(function(x){{x.checked=false}});upd()}};upd()}})();</script>"""
    else:
        form = f'<p class="muted">{tt("Your role can browse the shop; a Prologis manager places orders.", "Su rol puede consultar la tienda; un gerente de Prologis realiza pedidos.")}</p>'
    cat_lbl = dict((k, tt(en, es)) for k, en, es in CAT.CATEGORIES).get(it.category, it.category)
    basis = (tt("Contract price (MSA Billing Map).", "Precio de contrato (Billing Map del MSA).") if it.basis == "contract"
             else (tt("Priced per job: ARGIA sends the quote within 2 business days; nothing starts before Prologis accepts it.",
                      "Se cotiza por trabajo: ARGIA envía la cotización en 2 días hábiles; nada inicia antes de que Prologis la acepte.") if it.on_quote
                   else tt("Catalogue price.", "Precio de catálogo.")))
    body = f"""<div class="kick"><a href="/shop/">{tt("Service shop", "Tienda de servicios")}</a> · {UI.e(cat_lbl)}</div><h1 class="pt">{UI.e(_item_name(it, tt))}</h1>{flash(msg, True)}
<div class="grid g2"><div class="card"><h2>{tt("Order", "Pedido")}</h2>{form}</div>
<div class="card"><div class="price" style="font-size:24px">{UI.e(CAT.price_text(it, tt))}</div><div class="small muted">{UI.e(basis)}</div>
<p>{UI.e(tt(it.desc_en, it.desc_es or it.desc_en))}</p>{"<h2 style='margin-top:14px'>" + tt("Included", "Incluye") + "</h2><ul class='inc'>" + inc_html + "</ul>" if inc_html else ""}
<div class="chip">{tt("Lead time", "Plazo")}: {it.lead_days} {tt("days", "días")}</div> <div class="chip">{it.code}</div></div></div>"""
    return page(_item_name(it, tt), body, "shop")


@app.post("/shop/add")
def shop_add():
    need("order")
    c = db()
    it = S.catalog_item(c, request.form.get("item", ""))
    if not it or not it.published or not it.active or not it.orderable:
        abort(404)
    rg = reg()
    sites = [rg.site(x) for x in request.form.getlist("site")]
    if any(s is None for s in sites):
        abort(400)
    try:
        lines = CAT.build_lines(it, sites, _float(request.form.get("qty")))
    except ValueError as ex:
        return shop_item(it.code, str(ex))
    S.cart_add(c, g.user["username"], lines, request.form.get("note", ""))
    return redirect("/shop/cart")


@app.get("/shop/cart")
def shop_cart(msg: str = ""):
    need("order")
    tt = t()
    c = db()
    rg = reg()
    rows, lines = "", []
    for r, ln in S._cart_pairs(c, g.user["username"]):
        lines.append(ln)
        it = S.catalog_item(c, ln.item_code)
        s = rg.site(ln.site_code)
        site_txt = UI.e(s.name) if s else tt("Portfolio", "Portafolio")
        unit_en, unit_es = CAT.UNITS[ln.unit]
        note = f'<div class="small muted">{UI.e(r["note"])}</div>' if r["note"] else ""
        rows += (f'<tr><td><b>{UI.e(_item_name(it, tt))}</b>{note}</td><td>{site_txt}</td><td class="n">{UI.num(ln.qty, 2 if ln.qty % 1 else 0)} <span class="small muted">{UI.e(tt(unit_en, unit_es).replace("per ", "").replace("por ", ""))}</span></td>'
                 f'<td class="n">{_mxn(ln.unit_price) if ln.unit_price is not None else tt("On quote", "Bajo cotización")}</td><td class="n"><b>{_mxn(ln.total) if ln.total is not None else "-"}</b></td>'
                 f'<td><form method="post" action="/shop/cart/remove">{csrf_field()}<button class="btn sm ghost" name="id" value="{r["id"]}">{tt("Remove", "Quitar")}</button></form></td></tr>')
    if not lines:
        body = (_shop_head(tt, tt("Your cart is empty.", "Su carrito está vacío.")) + flash(msg, True) +
                f'<div class="card" style="margin-top:16px"><a class="btn" href="/shop/">{tt("Browse services", "Ver servicios")}</a></div>')
        return page(tt("Cart", "Carrito"), body, "shop")
    tot = CAT.totals(lines)
    target = CAT.target_date(now_mx().date(), lines)
    quote_note = (f'<div class="flash" style="margin-top:12px">{tot["on_quote"]} {tt("line(s) on quote: ARGIA prices them within 2 business days and the order waits for your acceptance.", "línea(s) bajo cotización: ARGIA las cotiza en 2 días hábiles y el pedido espera su aceptación.")}</div>'
                  if tot["on_quote"] else "")
    body = _shop_head(tt, tt("Review and place the order. It becomes a service order you can follow in Tickets.", "Revise y confirme. Se convierte en una orden de servicio que puede seguir en Tickets.")) + f"""{flash(msg, True)}
<div class="grid g2"><div class="card" style="overflow-x:auto"><h2>{tt("Cart", "Carrito")}</h2><table class="t"><tr><th>{tt("Service", "Servicio")}</th><th>{tt("Site", "Sitio")}</th><th class="n">{tt("Qty", "Cant.")}</th><th class="n">{tt("Unit price", "Precio unitario")}</th><th class="n">{tt("Amount", "Importe")}</th><th></th></tr>{rows}</table>
<table class="t tot"><tr><td>{tt("Subtotal", "Subtotal")}</td><td class="n">{_mxn(tot["subtotal"])}</td></tr><tr><td>IVA 16%</td><td class="n">{_mxn(tot["iva"])}</td></tr>
<tr><td><b>{tt("Total", "Total")}</b></td><td class="n"><b>{_mxn(tot["total"])}</b></td></tr></table>{quote_note}</div>
<div class="card"><h2>{tt("Place order", "Confirmar pedido")}</h2><form method="post" action="/shop/checkout">{csrf_field()}
<label>{tt("Order title", "Título del pedido")}</label><input name="title" maxlength="160" value="{tt("Service order", "Orden de servicio")} {now_mx():%d %b %Y}">
<div class="row"><div><label>{tt("Prologis PO number", "Número de OC Prologis")}</label><input name="po" maxlength="60"></div>
<div><label>{tt("Preferred date", "Fecha preferida")}</label><input type="date" name="preferred" min="{target.isoformat()}"></div></div>
<div class="small muted" style="margin-top:6px">{tt("Earliest date with the services' lead time", "Fecha más temprana según plazos")}: <b>{target:%d %b %Y}</b></div>
<label>{tt("Site access, contacts, notes", "Acceso, contactos, notas")}</label><textarea name="notes"></textarea>
<div style="margin-top:14px"><button class="btn">{tt("Place order", "Confirmar pedido")}</button></div></form></div></div>"""
    return page(tt("Cart", "Carrito"), body, "shop")


@app.post("/shop/cart/remove")
def shop_cart_remove():
    need("order")
    try:
        S.cart_remove(db(), g.user["username"], int(request.form.get("id", "0")))
    except ValueError:
        abort(400)
    return redirect("/shop/cart")


@app.post("/shop/checkout")
def shop_checkout():
    need("order")
    pref = (request.form.get("preferred") or "").strip()
    try:
        dt.date.fromisoformat(pref) if pref else None
    except ValueError:
        return shop_cart("Preferred date must be a date (YYYY-MM-DD). / La fecha preferida debe ser una fecha (AAAA-MM-DD).")
    try:
        tk = S.place_order(db(), g.user["username"], request.form.get("title", ""), request.form.get("po", ""), pref,
                           request.form.get("notes", ""), ip(), today=now_mx().date(), role=g.user["role"])
    except ValueError as ex:
        return shop_cart(str(ex))
    return redirect(f"/tickets/{tk['number']}/")


def order_page(tk):
    tt = t()
    c = db()
    rg = reg()
    number = tk["number"]
    role = g.user["role"]
    rows_db = S.order_lines(c, tk["id"])
    lines = S.as_lines(rows_db)
    tot = CAT.totals(lines)
    may_quote = S.can(role, "order_work") and tk["status"] == "ORDERED"
    rows = ""
    for r in rows_db:
        s = rg.site(r["site_code"])
        unit_en, unit_es = CAT.UNITS.get(r["unit"], (r["unit"], r["unit"]))
        price = _mxn(r["unit_price"]) if r["unit_price"] is not None else f'<span class="pill s-warn">{tt("On quote", "Bajo cotización")}</span>'
        cat_it = S.catalog_item(c, r["item_code"])
        if may_quote and (cat_it is None or cat_it.on_quote):
            val = "" if r["unit_price"] is None else f'{r["unit_price"]:.2f}'
            price = (f'<form method="post" action="/tickets/{number}/quote" class="qf">{csrf_field()}<input type="hidden" name="line" value="{r["id"]}">'
                     f'<input name="price" value="{val}" inputmode="decimal" placeholder="MXN" required><button class="btn sm">{tt("Set", "Fijar")}</button></form>')
        amount = "-" if r["unit_price"] is None else _mxn(round(r["unit_price"] * r["qty"], 2))
        note = f'<div class="small muted">{UI.e(r["note"])}</div>' if r["note"] else ""
        rows += (f'<tr><td><b>{UI.e(r["name"])}</b>{note}</td><td>{UI.e(s.name) if s else tt("Portfolio", "Portafolio")}</td>'
                 f'<td class="n">{UI.num(r["qty"], 2 if r["qty"] % 1 else 0)} <span class="small muted">{UI.e(tt(unit_en, unit_es).replace("per ", "").replace("por ", ""))}</span></td>'
                 f'<td class="n">{price}</td><td class="n"><b>{amount}</b></td></tr>')
    steps = [k for k, _, _ in CAT.ORDER_STATUSES if k != "CANCELLED"]
    cur = steps.index(tk["status"]) if tk["status"] in steps else -1
    stepper = "".join(f'<div class="st{" done" if i < cur else (" now" if i == cur else "")}"><i></i>{UI.e(order_label(k, tt))}</div>' for i, k in enumerate(steps))
    if tk["status"] == "CANCELLED":
        stepper = f'<div class="pill s-off">{tt("Cancelled", "Cancelado")}</div>'
    acts = ""
    allowed = [n for n in CAT.ORDER_TRANSITIONS.get(tk["status"], ())
               if (S.can(role, "order_work") or (S.can(role, "order") and n in CAT.CUSTOMER_MAY))
               and not (n == "CONFIRMED" and tk["approval"] not in ("not_required", "approved"))]
    verbs = {"CONFIRMED": tt("Confirm order", "Confirmar pedido"), "SCHEDULED": tt("Schedule", "Programar"),
             "IN_PROGRESS": tt("Start work", "Iniciar trabajo"), "COMPLETED": tt("Mark completed", "Marcar completado"),
             "INVOICED": tt("Mark invoiced", "Marcar facturado"), "CANCELLED": tt("Cancel order", "Cancelar pedido")}
    if allowed:
        btns = "".join(f'<button class="btn sm {"danger" if n == "CANCELLED" else ""}" name="to" value="{n}">{UI.e(verbs[n])}</button> ' for n in allowed)
        sched = (f'<label>{tt("Scheduled date (for Scheduled)", "Fecha programada (para Programado)")}</label><input type="date" name="scheduled" value="{UI.e(tk["scheduled_date"])}">'
                 if "SCHEDULED" in allowed else "")
        acts += (f'<form method="post" action="/tickets/{number}/status">{csrf_field()}<label>{tt("Move order to", "Mover pedido a")}</label>'
                 f'<input name="note" placeholder="{tt("note (optional)", "nota (opcional)")}">{sched}<div style="margin-top:8px">{btns}</div></form>')
    if tk["approval"] == "pending" and S.can(role, "ticket_approve"):
        acts += (f'<form method="post" action="/tickets/{number}/approve" style="margin-top:12px">{csrf_field()}<label>{tt("Accept the quote", "Aceptar la cotización")} - {_mxn(tot["total"])} {tt("incl. IVA", "con IVA")}</label>'
                 f'<input name="note" placeholder="PO / {tt("comment", "comentario")}"><div style="margin-top:8px"><button class="btn sm" name="ok" value="1">{tt("Accept", "Aceptar")}</button> '
                 f'<button class="btn sm danger" name="ok" value="0">{tt("Decline", "Rechazar")}</button></div></form>')
    acts += (f'<form method="post" action="/tickets/{number}/comment" enctype="multipart/form-data" style="margin-top:12px">{csrf_field()}<label>{tt("Comment", "Comentario")}</label>'
             f'<textarea name="body"></textarea><input type="file" name="file" style="margin-top:6px"><div style="margin-top:8px"><button class="btn sm">{tt("Add", "Agregar")}</button></div></form>')
    files = {d["id"]: d for d in c.execute("SELECT * FROM documents WHERE ticket_id=? AND deleted=0", (tk["id"],))}
    tl = ""
    for ev in S.ticket_events(c, tk["id"]):
        m = json.loads(ev["meta"] or "{}")
        what = {"created": tt("placed the order", "realizó el pedido"), "comment": tt("commented", "comentó"),
                "status": f'{tt("moved it to", "lo pasó a")} <b>{UI.e(order_label(m.get("to", ""), tt))}</b>' + (f' ({UI.e(m["scheduled"])})' if m.get("scheduled") else ""),
                "approval": tt("accepted the quote" if m.get("approved") else "declined the quote", "aceptó la cotización" if m.get("approved") else "rechazó la cotización"),
                "quote": tt("priced a line", "cotizó una línea"), "file": tt("attached a file", "adjuntó un archivo")}.get(ev["kind"], ev["kind"])
        att = ""
        if ev["kind"] == "file" and m.get("doc") in files:
            d = files[m["doc"]]
            att = f'<div><a class="chip" href="/docs/{d["id"]}/download">{UI.e(d["name"])}</a></div>'
        body_html = ("<div>" + UI.e(ev["body"]) + "</div>") if ev["body"] else ""
        tl += f'<div class="ev"><div class="small muted">{mx_time(ev["ts_utc"])} · <b>{UI.e(ev["username"])}</b> {what}</div>{body_html}{att}</div>'
    dates = [(tt("Ordered", "Pedido"), mx_time(tk["created_utc"])), (tt("PO", "OC"), tk["po_number"] or "-"),
             (tt("Preferred", "Preferida"), tk["preferred_date"] or "-"), (tt("Earliest", "Más temprana"), tk["target_date"] or "-"),
             (tt("Scheduled", "Programada"), tk["scheduled_date"] or "-"), (tt("Completed", "Completado"), mx_time(tk["resolved_utc"]))]
    facts = "".join(f'<div><div class="small muted">{UI.e(a)}</div><b>{UI.e(b)}</b></div>' for a, b in dates)
    quote_txt = (f'<div class="small muted" style="margin-top:6px">{tot["on_quote"]} {tt("line(s) waiting for the ARGIA quote", "línea(s) esperando cotización de ARGIA")}</div>' if tot["on_quote"] else "")
    body = f"""<div class="kick"><a href="/tickets/?kind=order">{tt("Service orders", "Órdenes de servicio")}</a> · {number}</div><h1 class="pt">{UI.e(tk["title"])}</h1>
<div style="margin:8px 0">{order_pill(tk, tt)}</div><div class="stepper">{stepper}</div>
<div class="grid g2"><div><div class="card" style="overflow-x:auto"><h2>{tt("Order lines", "Líneas del pedido")}</h2><table class="t"><tr><th>{tt("Service", "Servicio")}</th><th>{tt("Site", "Sitio")}</th><th class="n">{tt("Qty", "Cant.")}</th><th class="n">{tt("Unit price", "Precio unitario")}</th><th class="n">{tt("Amount", "Importe")}</th></tr>{rows}</table>
<table class="t tot"><tr><td>{tt("Subtotal", "Subtotal")}</td><td class="n">{_mxn(tot["subtotal"])}</td></tr><tr><td>IVA 16%</td><td class="n">{_mxn(tot["iva"])}</td></tr><tr><td><b>{tt("Total", "Total")}</b></td><td class="n"><b>{_mxn(tot["total"])}</b></td></tr></table>{quote_txt}</div>
<div class="card" style="margin-top:16px"><h2>{tt("Timeline", "Historial")}</h2><div class="tl">{tl}</div></div></div>
<div class="card"><h2>{tt("Order details", "Detalles")}</h2><div class="facts">{facts}</div>{"<p class='small'>" + UI.e(tk["description"]) + "</p>" if tk["description"] else ""}<h2 style="margin-top:16px">{tt("Actions", "Acciones")}</h2>{acts}</div></div>"""
    return page(number, body, "tickets")


@app.post("/tickets/<number>/quote")
def ticket_quote(number):
    need("order_work")
    tk = _ticket(number)
    price = _float(request.form.get("price"))
    try:
        if tk["kind"] != "order" or price is None:
            raise ValueError("bad quote")
        S.price_quote_line(db(), tk, int(request.form.get("line", "0")), price, g.user["username"], ip())
    except ValueError:
        abort(400)
    return redirect(f"/tickets/{number}/")


# ------------------------------------------------------------------ catalogue admin (v293)
@app.get("/admin/services/")
def services_admin():
    need("catalog_edit")
    tt = t()
    saved = request.args.get("saved", "")
    msg = f"{saved} saved. / {saved} guardado." if saved else ""
    items = S.catalog(db(), include_drafts=True, include_removed=True)
    rows = ""
    for k, en, es in CAT.CATEGORIES:
        grp = [i for i in items if i.category == k]
        if not grp:
            continue
        rows += f'<tr><th colspan="7" class="grp">{UI.e(tt(en, es))}</th></tr>'
        for i in grp:
            state = (f'<span class="pill s-off">{tt("Removed", "Retirado")}</span>' if not i.active else
                     (f'<span class="pill s-ok">{tt("Published", "Publicado")}</span>' if i.published else f'<span class="pill s-warn">{tt("Draft", "Borrador")}</span>'))
            kind = tt("Orderable", "Pedible") if i.orderable else tt("Rate card", "Tarifa")
            rows += (f'<tr><td class="nw"><a href="/admin/services/{i.code}"><b>{i.code}</b></a></td><td>{UI.e(i.name_en)}<div class="small muted">{UI.e(i.name_es)}</div></td>'
                     f'<td class="n nw">{UI.e(CAT.price_text(i))}</td><td>{UI.e(i.basis)}</td><td>{kind}</td><td class="n">{i.lead_days} d</td><td>{state}</td></tr>')
    pub = sum(1 for i in items if i.published and i.active)
    drafts = sum(1 for i in items if not i.published and i.active)
    body = f"""<div style="display:flex;align-items:end;gap:12px;flex-wrap:wrap"><div><div class="kick">{tt("Administration", "Administración")}</div>
<h1 class="pt">{tt("Services & prices", "Servicios y precios")}</h1><div class="small muted">{pub} {tt("published", "publicados")} · {drafts} {tt("drafts (only admins see them)", "borradores (solo administradores)")} · {tt("every change is in the audit log", "cada cambio queda en la bitácora")}</div></div>
<div style="margin-left:auto;display:flex;gap:8px"><a class="btn ghost" href="/shop/">{tt("Open the shop", "Ver la tienda")}</a><a class="btn" href="/admin/services/new">{tt("Add service", "Agregar servicio")}</a></div></div>{flash(msg)}
<div class="card" style="margin-top:16px;overflow-x:auto"><table class="t"><tr><th>{tt("Code", "Código")}</th><th>{tt("Service", "Servicio")}</th><th class="n">{tt("Price (MXN, before IVA)", "Precio (MXN, sin IVA)")}</th><th>{tt("Basis", "Base")}</th><th>{tt("Type", "Tipo")}</th><th class="n">{tt("Lead", "Plazo")}</th><th>{tt("State", "Estado")}</th></tr>{rows}</table></div>"""
    return page(tt("Services & prices", "Servicios y precios"), body, "users")


@app.get("/admin/services/<code>")
def service_edit(code, msg: str = "", draft: Optional[CAT.Item] = None):
    need("catalog_edit")
    tt = t()
    new = code == "new"
    it = draft or (CAT.Item("", "cleaning", "", "", "site", basis="quote") if new else S.catalog_item(db(), code))
    if it is None:
        abort(404)
    copts = "".join(f'<option value="{k}"{" selected" if k == it.category else ""}>{UI.e(tt(en, es))}</option>' for k, en, es in CAT.CATEGORIES)
    uopts = "".join(f'<option value="{k}"{" selected" if k == it.unit else ""}>{UI.e(tt(en, es))}</option>' for k, (en, es) in CAT.UNITS.items())
    bopts = "".join(f'<option value="{k}"{" selected" if k == it.basis else ""}>{lbl}</option>' for k, lbl in
                    (("contract", tt("Contract price (MSA)", "Precio de contrato (MSA)")), ("catalog", tt("ARGIA list price", "Precio de lista ARGIA")), ("quote", tt("Quote per job (no price)", "Cotización por trabajo (sin precio)"))))

    def chk(name, on, label):
        return f'<label class="ck"><input type="checkbox" name="{name}" value="1"{" checked" if on else ""}> {label}</label>'

    price = "" if it.price_mxn is None else f"{it.price_mxn:g}"
    code_in = (f'<input name="code" value="{UI.e(it.code)}" required maxlength="30" pattern="[A-Za-z0-9_-]+">' if new
               else f'<input value="{UI.e(it.code)}" disabled><input type="hidden" name="code" value="{UI.e(it.code)}">')
    remove = ("" if new or not it.active else
              f'<form method="post" action="/admin/services/{UI.e(it.code)}/remove" style="margin-top:14px">{csrf_field()}<button class="btn sm danger">{tt("Remove from shop", "Retirar de la tienda")}</button>'
              f' <span class="small muted">{tt("past orders keep their lines", "los pedidos anteriores conservan sus líneas")}</span></form>')
    body = f"""<div class="card form" style="max-width:860px"><div class="kick"><a href="/admin/services/">{tt("Services & prices", "Servicios y precios")}</a></div>
<h1 class="pt">{tt("New service", "Nuevo servicio") if new else UI.e(it.name_en)}</h1>{flash(msg, True)}
<form method="post" action="/admin/services/save">{csrf_field()}<input type="hidden" name="is_new" value="{"1" if new else ""}">
<div class="row"><div><label>{tt("Code", "Código")}</label>{code_in}</div><div><label>{tt("Category", "Categoría")}</label><select name="category">{copts}</select></div></div>
<div class="row"><div><label>{tt("Name (English)", "Nombre (inglés)")}</label><input name="name_en" value="{UI.e(it.name_en)}" required maxlength="120"></div>
<div><label>{tt("Name (Spanish)", "Nombre (español)")}</label><input name="name_es" value="{UI.e(it.name_es)}" maxlength="120"></div></div>
<div class="row"><div><label>{tt("Price MXN before IVA (empty = on quote; % for a mark-up)", "Precio MXN sin IVA (vacío = cotización; % para margen)")}</label><input name="price_mxn" value="{price}" inputmode="decimal"></div>
<div><label>{tt("Unit", "Unidad")}</label><select name="unit">{uopts}</select></div></div>
<div class="row"><div><label>{tt("Lead time (days)", "Plazo (días)")}</label><input name="lead_days" value="{it.lead_days}" inputmode="numeric"></div>
<div><label>{tt("Price basis", "Base del precio")}</label><select name="basis">{bopts}</select></div></div>
<div class="row"><div><label>{tt("Description (English)", "Descripción (inglés)")}</label><textarea name="desc_en">{UI.e(it.desc_en)}</textarea></div>
<div><label>{tt("Description (Spanish)", "Descripción (español)")}</label><textarea name="desc_es">{UI.e(it.desc_es)}</textarea></div></div>
<div class="row"><div><label>{tt("Included, separated by ; (English)", "Incluye, separado por ; (inglés)")}</label><textarea name="includes_en">{UI.e(it.includes_en)}</textarea></div>
<div><label>{tt("Included, separated by ; (Spanish)", "Incluye, separado por ; (español)")}</label><textarea name="includes_es">{UI.e(it.includes_es)}</textarea></div></div>
<div class="row"><div><label>{tt("Sort order", "Orden")}</label><input name="sort" value="{it.sort}" inputmode="numeric"></div><div style="padding-top:22px">
{chk("orderable", it.orderable, tt("Orderable in the shop (off = rate card line)", "Pedible en la tienda (no = línea de tarifas)"))}
{chk("published", it.published, tt("Published (Prologis sees it)", "Publicado (Prologis lo ve)"))}
{chk("active", it.active, tt("Active", "Activo"))}</div></div>
<div style="margin-top:14px"><button class="btn">{tt("Save", "Guardar")}</button> <a class="btn ghost" href="/admin/services/">{tt("Back", "Volver")}</a></div></form>{remove}</div>"""
    return page(tt("Services & prices", "Servicios y precios"), body, "users")


@app.post("/admin/services/save")
def service_save():
    need("catalog_edit")
    f = request.form
    d = {k: f.get(k, "") for k in ("code", "category", "name_en", "name_es", "unit", "desc_en", "desc_es", "includes_en", "includes_es", "basis")}
    d["name_es"] = d["name_es"] or d["name_en"]
    d.update(orderable=bool(f.get("orderable")), published=bool(f.get("published")), active=bool(f.get("active")))
    p, lead, srt = _float(f.get("price_mxn")), (f.get("lead_days") or "10").strip(), (f.get("sort") or "100").strip()
    if (f.get("price_mxn") or "").strip() and p is None:
        return service_edit("new" if f.get("is_new") else d["code"], "Price must be a number. / El precio debe ser un número.")
    if not lead.isdigit() or not srt.lstrip("-").isdigit():
        return service_edit("new" if f.get("is_new") else d["code"], "Lead time and sort must be whole numbers. / Plazo y orden deben ser enteros.")
    d.update(price_mxn=p, lead_days=int(lead), sort=int(srt))
    if not d["code"].strip():
        return service_edit("new", "Code is required. / El código es obligatorio.")
    it = CAT.from_dict(d)
    c = db()
    if f.get("is_new") and S.catalog_item(c, it.code):
        return service_edit("new", f"{it.code} already exists. / {it.code} ya existe.", it)
    try:
        S.save_item(c, it, g.user["username"], ip())
    except ValueError as ex:
        return service_edit("new" if f.get("is_new") else it.code, str(ex), it)
    return redirect(f"/admin/services/?saved={it.code}")


@app.post("/admin/services/<code>/remove")
def service_remove(code):
    need("catalog_edit")
    c = db()
    if not S.catalog_item(c, code):
        abort(404)
    S.remove_item(c, code, g.user["username"], ip())
    return redirect("/admin/services/")


# ------------------------------------------------------------------ assets (v320)
# Equipment register, warranty claims and spare parts (MSA Schedule A, Data &
# Reporting and Maintenance & Repairs; proposal 4.6). Everyone signed in sees
# them (Owner visibility); ARGIA operators and admins change them; a Prologis
# manager acknowledges a denied warranty claim.
def today_mx() -> dt.date:
    return now_mx().date()


def _lbl(pairs, key: str, tt) -> str:
    return dict((k, tt(en, es)) for k, en, es in pairs).get(key, key)


def _site_name(rg: R.Registry, code: str) -> str:
    s = rg.site(code)
    return s.name if s else code


WARR_PILL = {"active": "s-ok", "expiring": "s-warn", "expired": "s-bad", "none": "s-off"}
CAL_PILL = {"ok": "s-ok", "due_soon": "s-warn", "overdue": "s-bad", "unknown": "s-warn", "n/a": ""}
CLAIM_PILL = {"DRAFT": "s-off", "SUBMITTED": "s-info", "APPROVED": "s-ok", "DENIED": "s-bad", "CLOSED": "s-off"}


def warranty_pill(until: str, tt) -> str:
    st = A.warranty_state(until, today_mx())
    txt = {"active": tt("Warranty to", "Garantía a"), "expiring": tt("Expires", "Vence"),
           "expired": tt("Expired", "Vencida"), "none": tt("No warranty date", "Sin fecha de garantía")}[st]
    return f'<span class="pill {WARR_PILL[st]}">{txt}{" " + UI.e(until) if until else ""}</span>'


def calib_pill(category: str, due: str, tt) -> str:
    st = A.calib_state(category, due, today_mx())
    if st == "n/a":
        return ""
    txt = {"ok": tt("Calibrated to", "Calibrado a"), "due_soon": tt("Calibration due", "Calibración vence"),
           "overdue": tt("Calibration overdue", "Calibración vencida"), "unknown": tt("Calibration date missing", "Falta fecha de calibración")}[st]
    return f'<span class="pill {CAL_PILL[st]}">{txt}{" " + UI.e(due) if due else ""}</span>'


def asset_counts(c) -> Dict[str, int]:
    """The numbers the overview, the tabs and the alerts share."""
    today = today_mx()
    eq = A.equipment(c)
    w = [A.warranty_state(r["warranty_until"], today) for r in eq]
    cal = [A.calib_state(r["category"], r["calib_due"], today) for r in eq]
    cls = A.claims(c)
    bal = A.balances(A.all_moves(c))
    below = {p for (p, loc), mn in A.minimums(c).items() if bal.get((p, loc), 0.0) + 1e-9 < mn}
    return {"components": len(eq), "expiring": w.count("expiring"), "expired": w.count("expired"),
            "cal_overdue": cal.count("overdue"), "cal_soon": cal.count("due_soon") + cal.count("unknown"),
            "claims_open": sum(1 for x in cls if x["status"] in A.CLAIM_OPEN),
            "denied_unack": sum(1 for x in cls if A.needs_owner_ack(c, x)), "below_min": len(below)}


def _assets_tabs(on: str, tt, n: Dict[str, int]) -> str:
    tabs = [("eq", "/assets/", tt("Equipment register", "Registro de equipos"), n["components"]),
            ("claims", "/assets/claims/", tt("Warranty claims", "Reclamos de garantía"), n["claims_open"]),
            ("spares", "/assets/spares/", tt("Spare parts", "Refacciones"), n["below_min"]),
            ("report", "/assets/spares/report", tt("Quarterly inventory report", "Reporte trimestral de inventario"), None)]
    return '<div class="tabs">' + "".join(f'<a class="tab{" on" if k == on else ""}" href="{h}">{UI.e(lbl)}{"" if v is None else f" <b>{v}</b>"}</a>'
                                         for k, h, lbl, v in tabs) + "</div>"


def _assets_head(tt, title: str, sub: str = "") -> str:
    return (f'<div class="kick">{tt("Assets", "Activos")}</div><h1 class="pt">{UI.e(title)}</h1>'
            + (f'<p class="muted small">{sub}</p>' if sub else ""))


def _denied_banner(c, tt) -> str:
    n = sum(1 for x in A.claims(c) if A.needs_owner_ack(c, x))
    if not n:
        return ""
    return (f'<div class="flash err"><b>{n} {tt("warranty claim(s) denied by the supplier", "reclamo(s) de garantía rechazado(s) por el proveedor")}</b> - '
            f'{tt("Prologis is notified here until a Prologis manager acknowledges each one.", "Prologis queda notificado aquí hasta que un gerente de Prologis acuse cada uno.")} '
            f'<a href="/assets/claims/">{tt("Open the claims", "Ver los reclamos")} ›</a></div>')


def _ticket_id(c, number: str) -> Optional[int]:
    number = (number or "").strip().upper()
    if not number:
        return None
    r = c.execute("SELECT id FROM tickets WHERE number=?", (number,)).fetchone()
    if not r:
        raise ValueError(f"no ticket {number}")
    return int(r["id"])


def _ticket_no(c, tid) -> str:
    if not tid:
        return ""
    r = c.execute("SELECT number FROM tickets WHERE id=?", (tid,)).fetchone()
    return r["number"] if r else ""


@app.get("/assets/")
def assets_page(msg: str = "", err: bool = False):
    tt = t()
    rg = reg()
    c = db()
    site = (request.args.get("site") or "").upper()
    cat = request.args.get("cat", "")
    show_removed = request.args.get("removed") == "1"
    rows = A.equipment(c, site, cat if cat in A.CAT_KEYS else "", include_removed=show_removed)
    n = asset_counts(c)
    edit = S.can(g.user["role"], "assets_edit")
    tr = ""
    for r in rows:
        mm = " ".join(x for x in (r["make"], r["model"]) if x)
        st = "" if r["status"] == "in_service" else f' <span class="pill {"s-bad" if r["status"] == "fault" else "s-off"}">{UI.e(_lbl(A.EQ_STATUSES, r["status"], tt))}</span>'
        ed = f'<a class="btn sm ghost" href="/assets/eq/{r["id"]}">{tt("Edit", "Editar")}</a>' if edit else ""
        tr += (f'<tr><td class="small muted">#{r["id"]}</td><td><b>{UI.e(_site_name(rg, r["site_code"]))}</b><div class="small muted">{UI.e(r["site_code"])}</div></td>'
               f'<td>{UI.e(r["tag"] or "-")}<div class="small muted">{UI.e(_lbl(A.CATEGORIES, r["category"], tt))}</div></td>'
               f'<td>{UI.e(mm or "-")}<div class="small muted">{UI.e(r["serial"] or "")}</div></td>'
               f'<td class="n">{r["qty"]}</td><td class="n">{UI.num(r["dc_kw"], 2) if r["dc_kw"] is not None else "-"}</td>'
               f'<td>{warranty_pill(r["warranty_until"], tt)}{calib_pill(r["category"], r["calib_due"], tt)}{st}</td><td>{ed}</td></tr>')
    tr = tr or f'<tr><td class="muted" colspan="8">{tt("No equipment registered yet. The register is filled at onboarding from the as-built and commissioning documents (CSV import), and kept current after every repair.", "Aún no hay equipos registrados. El registro se llena en el onboarding con los planos as-built y la puesta en marcha (importación CSV) y se actualiza tras cada reparación.")}</td></tr>'
    sopts = "".join(f'<option value="{s.code}" {"selected" if s.code == site else ""}>{UI.e(s.name)}</option>' for s in rg.sites)
    copts = "".join(f'<option value="{k}" {"selected" if k == cat else ""}>{UI.e(tt(en, es))}</option>' for k, en, es in A.CATEGORIES)
    dc = ""
    if site and rg.site(site):
        chk = A.dc_check(A.equipment(c, site), rg.site(site).kwp)
        dc = (f'<p class="small">{tt("Registered module DC", "DC de módulos registrado")}: <b>{UI.num(chk[0], 1)} kW</b> = '
              f'<b>{chk[1] * 100:.1f}%</b> {tt("of the site kWp", "del kWp del sitio")} ({UI.num(rg.site(site).kwp, 1)})'
              + ("" if abs(chk[1] - 1) <= 0.02 else f' <span class="pill s-warn">{tt("check: differs by more than 2%", "revisar: difiere más de 2%")}</span>') + "</p>"
              if chk else f'<p class="small muted">{tt("No module DC registered for this site yet - the availability calculation needs it.", "Aún sin DC de módulos para este sitio - el cálculo de disponibilidad lo necesita.")}</p>')
    btns = (f'<a class="btn" href="/assets/eq/new">{tt("Add equipment", "Agregar equipo")}</a> <a class="btn ghost" href="/assets/import">{tt("Import CSV", "Importar CSV")}</a> ' if edit else "") + \
        f'<a class="btn ghost" href="/assets/equipment.csv">{tt("Download CSV", "Descargar CSV")}</a>'
    kp = (f'<div class="grid g3">'
          f'<div class="card"><h2>{tt("Warranties", "Garantías")}</h2><div style="font-size:26px;font-weight:800;color:var(--deep)">{n["expiring"]} <small style="font-size:13px;color:var(--muted)">{tt("expire within 90 days", "vencen en 90 días")}</small></div><div class="small muted">{n["expired"]} {tt("expired", "vencidas")}</div></div>'
          f'<div class="card"><h2>{tt("Meter and sensor calibration", "Calibración de medidores y sensores")}</h2><div style="font-size:26px;font-weight:800;color:var(--deep)">{n["cal_overdue"]} <small style="font-size:13px;color:var(--muted)">{tt("overdue", "vencidas")}</small></div><div class="small muted">{n["cal_soon"]} {tt("due within 30 days or without a date", "vencen en 30 días o sin fecha")}</div></div>'
          f'<div class="card"><h2>{tt("Open warranty claims", "Reclamos abiertos")}</h2><div style="font-size:26px;font-weight:800;color:var(--deep)">{n["claims_open"]}</div><div class="small muted">{n["denied_unack"]} {tt("denied, awaiting Prologis acknowledgement", "rechazados, esperando acuse de Prologis")}</div></div></div>')
    body = (_assets_head(tt, tt("Equipment register", "Registro de equipos"),
                         tt("Serial numbers, warranties and calibration of every component, per site (MSA Schedule A, Data & Reporting).",
                            "Números de serie, garantías y calibración de cada componente, por sitio (MSA Anexo A, Datos y Reportes)."))
            + flash(msg, err) + _denied_banner(c, tt) + _assets_tabs("eq", tt, n) + kp
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-bottom:10px">'
            f'<form method="get" style="display:flex;gap:8px;flex-wrap:wrap"><select name="site" style="width:auto" onchange="this.form.submit()"><option value="">{tt("All sites", "Todos los sitios")}</option>{sopts}</select>'
            f'<select name="cat" style="width:auto" onchange="this.form.submit()"><option value="">{tt("All categories", "Todas las categorías")}</option>{copts}</select>'
            f'<label class="small" style="display:flex;gap:4px;align-items:center"><input type="checkbox" name="removed" value="1" style="width:auto" {"checked" if show_removed else ""} onchange="this.form.submit()">{tt("show removed", "ver retirados")}</label></form>'
            f'<div style="margin-left:auto">{btns}</div></div>{dc}'
            f'<table class="t"><tr><th>#</th><th>{tt("Site", "Sitio")}</th><th>{tt("Tag / category", "Etiqueta / categoría")}</th><th>{tt("Make, model / serial", "Marca, modelo / serie")}</th>'
            f'<th class="n">{tt("Qty", "Cant.")}</th><th class="n">DC kW</th><th>{tt("Warranty / calibration / status", "Garantía / calibración / estado")}</th><th></th></tr>{tr}</table></div>')
    return page(tt("Assets", "Activos"), body, "assets")


def _eq_form(tt, rg, r: Optional[Dict] = None, msg: str = "") -> Response:
    r = r or {}
    eid = r.get("id")
    sopts = "".join(f'<option value="{s.code}" {"selected" if s.code == r.get("site_code") else ""}>{UI.e(s.name)} ({s.code})</option>' for s in rg.sites)
    copts = "".join(f'<option value="{k}" {"selected" if k == r.get("category") else ""}>{UI.e(tt(en, es))}</option>' for k, en, es in A.CATEGORIES)
    stopts = "".join(f'<option value="{k}" {"selected" if k == (r.get("status") or "in_service") else ""}>{UI.e(tt(en, es))}</option>' for k, en, es in A.EQ_STATUSES)

    def v(k):
        x = r.get(k)
        return "" if x is None else UI.e(str(x))
    body = f"""<div class="card form"><div class="kick">{tt("Equipment register", "Registro de equipos")}</div><h1 class="pt">{tt("Edit equipment", "Editar equipo") + f" #{eid}" if eid else tt("Add equipment", "Agregar equipo")}</h1>{flash(msg, True)}
<form method="post" action="/assets/eq/save">{csrf_field()}<input type="hidden" name="id" value="{eid or ""}">
<div class="row"><div><label>{tt("Site", "Sitio")}</label><select name="site_code" required>{sopts}</select></div><div><label>{tt("Category", "Categoría")}</label><select name="category">{copts}</select></div></div>
<div class="row"><div><label>{tt("Tag (as on the as-built, e.g. INV-01)", "Etiqueta (como en el as-built, ej. INV-01)")}</label><input name="tag" value="{v("tag")}" maxlength="60"></div><div><label>{tt("Status", "Estado")}</label><select name="status">{stopts}</select></div></div>
<div class="row"><div><label>{tt("Make", "Marca")}</label><input name="make" value="{v("make")}" maxlength="60"></div><div><label>{tt("Model", "Modelo")}</label><input name="model" value="{v("model")}" maxlength="80"></div></div>
<div class="row"><div><label>{tt("Serial number (one unit per row)", "Número de serie (una unidad por fila)")}</label><input name="serial" value="{v("serial")}" maxlength="80"></div><div><label>{tt("Quantity (rows without serial, e.g. modules)", "Cantidad (filas sin serie, ej. módulos)")}</label><input name="qty" value="{v("qty") or 1}" inputmode="numeric"></div></div>
<div class="row"><div><label>{tt("DC nameplate behind it, kW", "DC nominal detrás, kW")}</label><input name="dc_kw" value="{v("dc_kw")}" inputmode="decimal"></div><div><label>AC kW</label><input name="ac_kw" value="{v("ac_kw")}" inputmode="decimal"></div></div>
<div class="row"><div><label>{tt("Installed (YYYY-MM-DD)", "Instalado (AAAA-MM-DD)")}</label><input name="installed" value="{v("installed")}"></div><div><label>{tt("Warranty holder (manufacturer, EPC)", "Garante (fabricante, EPC)")}</label><input name="warranty_by" value="{v("warranty_by")}" maxlength="80"></div></div>
<div class="row"><div><label>{tt("Warranty until (YYYY-MM-DD)", "Garantía hasta (AAAA-MM-DD)")}</label><input name="warranty_until" value="{v("warranty_until")}"></div><div><label>{tt("Next calibration (meters, sensors)", "Próxima calibración (medidores, sensores)")}</label><input name="calib_due" value="{v("calib_due")}"></div></div>
<label>{tt("Notes (flash test file, known serial defect, ...)", "Notas (flash test, defecto de serie conocido, ...)")}</label><textarea name="notes">{v("notes")}</textarea>
<div style="margin-top:14px"><button class="btn">{tt("Save", "Guardar")}</button> <a class="btn ghost" href="/assets/">{tt("Cancel", "Cancelar")}</a></div></form></div>"""
    return page(tt("Equipment", "Equipo"), body, "assets")


@app.get("/assets/eq/<eid>")
def assets_eq_form(eid):
    need("assets_edit")
    tt = t()
    rg = reg()
    if eid == "new":
        return _eq_form(tt, rg, {"site_code": request.args.get("site", "").upper()})
    if not eid.isdigit():
        abort(404)
    r = A.equipment_row(db(), int(eid)) or abort(404)
    return _eq_form(tt, rg, dict(r))


@app.post("/assets/eq/save")
def assets_eq_save():
    need("assets_edit")
    tt = t()
    rg = reg()
    f = request.form
    eid = int(f["id"]) if (f.get("id") or "").isdigit() else None
    d = {k: f.get(k, "") for k in A.EQ_FIELDS}
    try:
        A.save_equipment(db(), d, [s.code for s in rg.sites], g.user["username"], ip(), eid=eid)
    except ValueError as ex:
        return _eq_form(tt, rg, {**d, "id": eid}, str(ex))
    return redirect(f"/assets/?site={d['site_code'].upper()}")


@app.get("/assets/import")
def assets_import(msg: str = "", errors: Optional[List[str]] = None):
    need("assets_edit")
    tt = t()
    errs = "".join(f"<li>{UI.e(x)}</li>" for x in (errors or [])[:200])
    more = f"<li>... {len(errors) - 200} {tt('more', 'más')}</li>" if errors and len(errors) > 200 else ""
    body = f"""<div class="card form"><div class="kick">{tt("Equipment register", "Registro de equipos")}</div><h1 class="pt">{tt("Import from CSV", "Importar desde CSV")}</h1>{flash(msg, bool(errors))}
{"<ul class='small'>" + errs + more + "</ul>" if errs else ""}
<p class="small">{tt("One row per component. Columns (header row, any order)", "Una fila por componente. Columnas (fila de encabezado, cualquier orden)")}: <code>{", ".join(A.IMPORT_COLUMNS)}</code>.
{tt("site and category are required; dates as YYYY-MM-DD; a row with a serial number is one unit. All or nothing: if any row has a problem, nothing is imported and every problem is listed. A serial number already in the register is refused, never overwritten.", "site y category son obligatorias; fechas AAAA-MM-DD; una fila con número de serie es una unidad. Todo o nada: si alguna fila tiene un problema no se importa nada y se listan todos. Un número de serie ya registrado se rechaza, nunca se sobrescribe.")}
<a href="/assets/import_template.csv">{tt("Template", "Plantilla")}</a></p>
<form method="post" action="/assets/import" enctype="multipart/form-data">{csrf_field()}<input type="file" name="file" accept=".csv" required>
<div style="margin-top:12px"><button class="btn">{tt("Check and import", "Revisar e importar")}</button> <a class="btn ghost" href="/assets/">{tt("Cancel", "Cancelar")}</a></div></form></div>"""
    return page(tt("Import", "Importar"), body, "assets")


@app.get("/assets/import_template.csv")
def assets_import_template():
    need("assets_edit")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(A.IMPORT_COLUMNS)
    w.writerow(["SITECODE", "inverter", "INV-01", "SolarEdge", "SE100K", "7E1234567-89", "1", "120.5", "100", "2025-03-01",
                "SolarEdge", "2037-03-01", "", "example row - delete"])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=equipment_template.csv"})


@app.post("/assets/import")
def assets_import_post():
    need("assets_edit")
    f = request.files.get("file")
    if not f or not f.filename:
        return assets_import("Choose a CSV file. / Elija un archivo CSV.", ["no file"])
    raw = f.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        abort(413)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    c = db()
    res = A.parse_import(text, [s.code for s in reg().sites], A.existing_serials(c))
    if res.errors:
        S.audit(c, g.user["username"], ip(), "equipment_import_refused", f.filename[:100], f"{len(res.errors)} problems")
        return assets_import(f"Nothing imported: {len(res.errors)} problem(s). / No se importó nada: {len(res.errors)} problema(s).", res.errors)
    n = A.import_equipment(c, res, g.user["username"], ip())
    return assets_page(f"{n} rows imported. / {n} filas importadas.")


@app.get("/assets/equipment.csv")
def assets_equipment_csv():
    c = db()
    rows = A.equipment(c, include_removed=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id"] + list(A.EQ_FIELDS) + ["replaced_by", "created_by", "created_utc", "updated_by", "updated_utc"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[k] for k in cols])
    S.audit(c, g.user["username"], ip(), "equipment_export", "equipment.csv", f"{len(rows)} rows")
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=prologis_equipment.csv"})


# ---- warranty claims
@app.get("/assets/claims/")
def claims_page(msg: str = "", err: bool = False):
    tt = t()
    rg = reg()
    c = db()
    show = request.args.get("show", "open")
    rows = A.claims(c, open_only=(show == "open"))
    n = asset_counts(c)
    nowu = utc_now()
    tr = ""
    for cl in rows:
        ack = f' <span class="pill s-bad">{tt("Prologis to acknowledge", "Pendiente acuse Prologis")}</span>' if A.needs_owner_ack(c, cl) else ""
        td = A.turnaround_days(cl, nowu)
        comp = (_mxn(cl["compensation_mxn"]) + (f' <span class="small muted">{tt("passed through", "transferido")} {UI.e(cl["passed_through"])}</span>' if cl["passed_through"]
                                                 else f' <span class="pill s-warn">{tt("to pass through", "por transferir")}</span>')) if cl["compensation_mxn"] else "-"
        tr += (f'<tr><td class="nw"><a href="/assets/claims/{cl["number"]}/"><b>{cl["number"]}</b></a></td>'
               f'<td>{UI.e(cl["supplier"])}<div class="small muted">{UI.e(_site_name(rg, cl["site_code"]))}{" · " + UI.e(cl["supplier_ref"]) if cl["supplier_ref"] else ""}</div></td>'
               f'<td><span class="pill {CLAIM_PILL[cl["status"]]}">{UI.e(_lbl(A.CLAIM_STATUSES, cl["status"], tt))}</span>{ack}</td>'
               f'<td class="n">{"-" if td is None else f"{td:.0f} d"}</td><td class="n nw">{comp}</td><td class="small muted">{mx_time(cl["opened_utc"])}</td></tr>')
    tr = tr or f'<tr><td class="muted" colspan="6">{tt("No warranty claims.", "Sin reclamos de garantía.")}</td></tr>'
    form = ""
    if S.can(g.user["role"], "assets_edit"):
        sopts = "".join(f'<option value="{s.code}">{UI.e(s.name)} ({s.code})</option>' for s in rg.sites)
        form = (f'<div class="card form" style="margin-top:16px"><h2>{tt("New warranty claim", "Nuevo reclamo de garantía")}</h2><form method="post" action="/assets/claims/new">{csrf_field()}'
                f'<div class="row"><div><label>{tt("Site", "Sitio")}</label><select name="site">{sopts}</select></div><div><label>{tt("Supplier (manufacturer, EPC)", "Proveedor (fabricante, EPC)")}</label><input name="supplier" required maxlength="80"></div></div>'
                f'<div class="row"><div><label>{tt("Equipment # (from the register, optional)", "Equipo # (del registro, opcional)")}</label><input name="equipment" inputmode="numeric"></div><div><label>{tt("Ticket (e.g. PL-0001, optional)", "Ticket (ej. PL-0001, opcional)")}</label><input name="ticket"></div></div>'
                f'<label>{tt("Fault and evidence", "Falla y evidencia")}</label><textarea name="fault"></textarea><div style="margin-top:10px"><button class="btn">{tt("Open claim", "Abrir reclamo")}</button></div></form></div>')
    other = "all" if show == "open" else "open"
    body = (_assets_head(tt, tt("Warranty claims", "Reclamos de garantía"),
                         tt("ARGIA prepares and follows every claim with the manufacturer or EPC; Prologis sees each step, every denial is flagged until a Prologis manager acknowledges it, and compensation is passed through.",
                            "ARGIA prepara y da seguimiento a cada reclamo con el fabricante o EPC; Prologis ve cada paso, todo rechazo queda marcado hasta que un gerente de Prologis lo acusa, y la compensación se transfiere."))
            + flash(msg, err) + _denied_banner(c, tt) + _assets_tabs("claims", tt, n)
            + f'<div class="card" style="margin-top:10px;overflow-x:auto"><div style="text-align:right;margin-bottom:8px"><a class="btn sm ghost" href="?show={other}">{tt("Show all" if show == "open" else "Open only", "Ver todos" if show == "open" else "Solo abiertos")}</a></div>'
            f'<table class="t"><tr><th>#</th><th>{tt("Supplier / site", "Proveedor / sitio")}</th><th>{tt("Status", "Estado")}</th><th class="n">{tt("With supplier", "Con proveedor")}</th>'
            f'<th class="n">{tt("Compensation", "Compensación")}</th><th>{tt("Opened", "Abierto")}</th></tr>{tr}</table></div>{form}')
    return page(tt("Warranty claims", "Reclamos de garantía"), body, "assets")


@app.post("/assets/claims/new")
def claims_new():
    need("assets_edit")
    c = db()
    f = request.form
    site = (f.get("site") or "").upper()
    if not reg().site(site):
        abort(400)
    try:
        eq = int(f["equipment"]) if (f.get("equipment") or "").strip().isdigit() else None
        cl = A.open_claim(c, site, f.get("supplier", ""), f.get("fault", ""), g.user["username"], equipment_id=eq,
                          ticket_id=_ticket_id(c, f.get("ticket", "")), ip=ip())
    except ValueError as ex:
        return claims_page(f"Not opened: {ex}", True)
    return redirect(f"/assets/claims/{cl['number']}/")


def _claim(number: str):
    cl = A.claim(db(), number)
    if not cl:
        abort(404)
    return cl


@app.get("/assets/claims/<number>/")
def claim_page(number, msg: str = ""):
    tt = t()
    rg = reg()
    c = db()
    cl = _claim(number)
    eq = A.equipment_row(c, cl["equipment_id"]) if cl["equipment_id"] else None
    tl = ""
    for ev in A.claim_events(c, cl["id"]):
        m = json.loads(ev["meta"] or "{}")
        what = {"created": tt("opened the claim", "abrió el reclamo"), "comment": tt("commented", "comentó"),
                "status": f'{tt("moved it to", "lo pasó a")} <b>{UI.e(_lbl(A.CLAIM_STATUSES, m.get("to", ""), tt))}</b>',
                "owner_ack": f'<b>{tt("acknowledged the denial for Prologis", "acusó el rechazo por Prologis")}</b>',
                "pass_through": f'{tt("passed the compensation through on", "transfirió la compensación el")} {UI.e(m.get("day", ""))}'}.get(ev["kind"], UI.e(ev["kind"]))
        tl += (f'<div class="ev"><div class="small muted">{mx_time(ev["ts_utc"])} · <b>{UI.e(ev["username"])}</b> {what}</div>'
               + (f"<div>{UI.e(ev['body'])}</div>" if ev["body"] else "") + "</div>")
    role = g.user["role"]
    acts = ""
    if S.can(role, "assets_edit"):
        btns = "".join(f'<button class="btn sm ghost" name="to" value="{x}">{UI.e(_lbl(A.CLAIM_STATUSES, x, tt))}</button> ' for x in A.CLAIM_TRANSITIONS[cl["status"]])
        if btns:
            acts += (f'<form method="post" action="/assets/claims/{number}/status">{csrf_field()}<label>{tt("Move to", "Mover a")}</label>'
                     f'<input name="note" placeholder="{tt("note - the supplier reason is required for a denial", "nota - el motivo del proveedor es obligatorio en un rechazo")}">'
                     f'<div class="row"><div><label>{tt("Supplier RMA / case number", "Número RMA / caso del proveedor")}</label><input name="ref" value="{UI.e(cl["supplier_ref"])}"></div>'
                     f'<div><label>{tt("Compensation received, MXN (if approved)", "Compensación recibida, MXN (si se aprueba)")}</label><input name="comp" inputmode="decimal"></div></div>'
                     f'<div style="margin-top:8px">{btns}</div></form>')
        if cl["compensation_mxn"] and not cl["passed_through"]:
            acts += (f'<form method="post" action="/assets/claims/{number}/pass" style="margin-top:12px">{csrf_field()}<label>{tt("Compensation passed through to Prologis on (YYYY-MM-DD)", "Compensación transferida a Prologis el (AAAA-MM-DD)")}</label>'
                     f'<input name="day" value="{today_mx().isoformat()}"><input name="note" placeholder="{tt("credit note / invoice", "nota de crédito / factura")}"><div style="margin-top:8px"><button class="btn sm">{tt("Record", "Registrar")}</button></div></form>')
    if A.needs_owner_ack(c, cl) and S.can(role, "assets_ack"):
        acts += (f'<form method="post" action="/assets/claims/{number}/ack" style="margin-top:12px">{csrf_field()}<label>{tt("Prologis acknowledgement of the denial", "Acuse de Prologis del rechazo")}</label>'
                 f'<input name="note" placeholder="{tt("comment (optional)", "comentario (opcional)")}"><div style="margin-top:8px"><button class="btn sm danger">{tt("Acknowledge", "Acusar recibo")}</button></div></form>')
    acts += (f'<form method="post" action="/assets/claims/{number}/comment" style="margin-top:12px">{csrf_field()}<label>{tt("Comment / correspondence", "Comentario / correspondencia")}</label>'
             f'<textarea name="body"></textarea><div style="margin-top:8px"><button class="btn sm">{tt("Add", "Agregar")}</button></div></form>')
    td = A.turnaround_days(cl, utc_now())
    eq_txt = (f'<span class="chip">#{eq["id"]} {UI.e(eq["tag"] or "")} {UI.e(eq["make"])} {UI.e(eq["model"])} {UI.e(eq["serial"])}</span>' if eq else "")
    tk = _ticket_no(c, cl["ticket_id"])
    chips = (f'<span class="pill {CLAIM_PILL[cl["status"]]}">{UI.e(_lbl(A.CLAIM_STATUSES, cl["status"], tt))}</span> {eq_txt}'
             + (f' <a class="chip" href="/tickets/{tk}/">{tk}</a>' if tk else "")
             + (f' <span class="chip">RMA {UI.e(cl["supplier_ref"])}</span>' if cl["supplier_ref"] else "")
             + (f' <span class="chip">{tt("with supplier", "con proveedor")} {td:.0f} d</span>' if td is not None else "")
             + (f' <span class="chip">{_mxn(cl["compensation_mxn"])}</span>' if cl["compensation_mxn"] else ""))
    ack = (f'<p class="small">{tt("Denial acknowledged by", "Rechazo acusado por")} <b>{UI.e(cl["owner_ack_by"])}</b> {mx_time(cl["owner_ack_utc"])}</p>' if cl["owner_ack_utc"] else "")
    body = f"""<div class="kick">{UI.e(_site_name(rg, cl["site_code"]))} · {cl["number"]}</div><h1 class="pt">{tt("Warranty claim", "Reclamo de garantía")} · {UI.e(cl["supplier"])}</h1>{flash(msg, True)}
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">{chips}</div>{ack}
<div class="grid g2"><div class="card"><h2>{tt("Fault", "Falla")}</h2><p>{UI.e(cl["fault"]) or "-"}</p><h2>{tt("Timeline", "Historial")}</h2><div class="tl">{tl}</div></div>
<div class="card"><h2>{tt("Actions", "Acciones")}</h2><div class="small muted">{tt("Opened", "Abierto")} {mx_time(cl["opened_utc"])} · {tt("Submitted", "Enviado")} {mx_time(cl["submitted_utc"])} · {tt("Decided", "Resuelto")} {mx_time(cl["decided_utc"])} · {tt("Closed", "Cerrado")} {mx_time(cl["closed_utc"])}</div>{acts}</div></div>"""
    return page(cl["number"], body, "assets")


@app.post("/assets/claims/<number>/status")
def claim_status(number):
    need("assets_edit")
    cl = _claim(number)
    comp = _float(request.form.get("comp"))
    try:
        A.set_claim_status(db(), cl, request.form.get("to", ""), g.user["username"], request.form.get("note", ""),
                           request.form.get("ref", ""), comp if request.form.get("to") == "APPROVED" else None, ip())
    except ValueError as ex:
        return claim_page(number, str(ex))
    if request.form.get("to") == "DENIED":                 # v321: "notify Owner immediately" - also by e-mail
        c = db()
        site = _site_name(reg(), cl["site_code"])
        AL.queue(c, "claim_denied", number, f"[ARGIA for Prologis] Warranty claim {number} denied - {site}",
                 f"The supplier {cl['supplier']} denied warranty claim {number} for {site}.\n"
                 f"Reason given: {request.form.get('note', '').strip()}\n\n"
                 f"Please acknowledge it on the platform: https://prologis.argia.com.mx/assets/claims/{number}/\n",
                 AL.recipients(c, "owner"), False)
    return redirect(f"/assets/claims/{number}/")


@app.post("/assets/claims/<number>/ack")
def claim_ack(number):
    need("assets_ack")
    try:
        A.ack_denial(db(), _claim(number), g.user["username"], request.form.get("note", ""), ip())
    except ValueError as ex:
        return claim_page(number, str(ex))
    return redirect(f"/assets/claims/{number}/")


@app.post("/assets/claims/<number>/pass")
def claim_pass(number):
    need("assets_edit")
    try:
        A.record_pass_through(db(), _claim(number), (request.form.get("day") or "").strip(), g.user["username"],
                              request.form.get("note", ""), ip())
    except ValueError as ex:
        return claim_page(number, str(ex))
    return redirect(f"/assets/claims/{number}/")


@app.post("/assets/claims/<number>/comment")
def claim_comment(number):
    need("comment")
    cl = _claim(number)
    body = (request.form.get("body") or "").strip()
    if body:
        c = db()
        A.claim_event(c, cl["id"], g.user["username"], "comment", body)
        S.audit(c, g.user["username"], ip(), "claim_comment", number)
    return redirect(f"/assets/claims/{number}/")


# ---- spare parts
def _part_name(p, tt) -> str:
    return tt(p["name_en"], p["name_es"] or p["name_en"])


@app.get("/assets/spares/")
def spares_page(msg: str = "", err: bool = False):
    tt = t()
    rg = reg()
    c = db()
    n = asset_counts(c)
    locs = A.locations(c)
    pts = A.parts(c)
    moves = A.all_moves(c)
    bal = A.balances(moves)
    mins = A.minimums(c)
    ser = A.serial_locations(moves)
    head = "".join(f'<th class="n">{UI.e(lc["code"])}<div class="small muted" style="font-weight:400">{UI.e(lc["name"])}</div></th>' for lc in locs)
    tr = ""
    for p in pts:
        cells = ""
        for lc in locs:
            q = bal.get((p["code"], lc["code"]), 0.0)
            mn = mins.get((p["code"], lc["code"]))
            low = mn is not None and q + 1e-9 < mn
            cells += (f'<td class="n"><b style="color:{"var(--red, #b2443c)" if low else "inherit"}">{q:g}</b>'
                      + (f'<span class="small muted"> / {mn:g}</span>' if mn is not None else "") + "</td>")
        sl = sorted(s for (pc, s), loc in ser.items() if pc == p["code"] and loc)
        sers = f'<div class="small muted">{UI.e(", ".join(sl[:12]))}{" ..." if len(sl) > 12 else ""}</div>' if sl else ""
        own = f'<span class="pill s-off">{UI.e(p["owner"])}</span>'
        tr += (f'<tr><td><b>{UI.e(p["code"])}</b> {own}<div>{UI.e(_part_name(p, tt))}</div><div class="small muted">{UI.e(" · ".join(x for x in (" ".join(y for y in (p["make"], p["model"]) if y), tt("by serial", "por serie") if p["serialized"] else "", p["unit"]) if x))}</div>{sers}</td>{cells}</tr>')
    tr = tr or f'<tr><td class="muted" colspan="{len(locs) + 1}">{tt("No spare parts defined yet.", "Aún no hay refacciones definidas.")}</td></tr>'
    recent = ""
    for m in list(reversed(moves))[:40]:
        where = f'{m["from_loc"] or "-"} › {m["to_loc"] or (m["site_code"] and _site_name(rg, m["site_code"])) or "-"}'
        tk = _ticket_no(c, m["ticket_id"])
        dmg = f' <span class="pill s-bad">{tt("damaged", "dañado")}</span>' if m["condition"] == "damaged" else ""
        recent += (f'<tr><td class="small">{UI.e(m["day"])}</td><td>{UI.e(_lbl(A.MOVE_KINDS, m["kind"], tt))}{dmg}</td><td><b>{UI.e(m["part_code"])}</b>'
                   f'{" · " + UI.e(m["serial"]) if m["serial"] else ""}</td><td class="n">{m["qty"]:g}</td><td class="small">{UI.e(where)}</td>'
                   f'<td class="small">{f"<a href=/tickets/{tk}/>{tk}</a>" if tk else ""} {UI.e(m["note"])}</td><td class="small muted">{UI.e(m["username"])}</td></tr>')
    recent = recent or f'<tr><td class="muted" colspan="7">{tt("No movements yet.", "Sin movimientos aún.")}</td></tr>'
    forms = ""
    if S.can(g.user["role"], "assets_edit"):
        popts = "".join(f'<option value="{p["code"]}">{UI.e(p["code"])} - {UI.e(_part_name(p, tt))}</option>' for p in pts)
        lopts = "".join(f'<option value="{lc["code"]}">{UI.e(lc["code"])}</option>' for lc in locs)
        kopts = "".join(f'<option value="{k}">{UI.e(tt(en, es))}</option>' for k, en, es in A.MOVE_KINDS)
        sopts = "".join(f'<option value="{s.code}">{UI.e(s.name)} ({s.code})</option>' for s in rg.sites)
        catopts = "".join(f'<option value="{k}">{UI.e(tt(en, es))}</option>' for k, en, es in A.CATEGORIES)
        oopts = "".join(f'<option value="{k}">{UI.e(tt(en, es))}</option>' for k, en, es in A.OWNERS)
        forms = f"""<div class="grid g2e" style="margin-top:16px"><div class="card"><h2>{tt("Record a movement", "Registrar movimiento")}</h2>
<form method="post" action="/assets/spares/move">{csrf_field()}<div class="row"><div><label>{tt("Movement", "Movimiento")}</label><select name="kind">{kopts}</select></div><div><label>{tt("Part", "Refacción")}</label><select name="part">{popts}</select></div></div>
<div class="row"><div><label>{tt("From location", "Desde")}</label><select name="from"><option value=""></option>{lopts}</select></div><div><label>{tt("To location", "Hacia")}</label><select name="to"><option value=""></option>{lopts}</select></div></div>
<div class="row"><div><label>{tt("Quantity", "Cantidad")}</label><input name="qty" value="1" inputmode="decimal"></div><div><label>{tt("Serial (serialized parts)", "Serie (refacciones por serie)")}</label><input name="serial"></div></div>
<div class="row"><div><label>{tt("Site (issue / return)", "Sitio (salida / devolución)")}</label><select name="site"><option value=""></option>{sopts}</select></div><div><label>{tt("Ticket (e.g. PL-0001)", "Ticket (ej. PL-0001)")}</label><input name="ticket"></div></div>
<div class="row"><div><label>{tt("Replaces equipment # (issue)", "Reemplaza equipo # (salida)")}</label><input name="replaces" inputmode="numeric"></div><div><label>{tt("Date", "Fecha")}</label><input name="day" value="{today_mx().isoformat()}"></div></div>
<label><input type="checkbox" name="damaged" value="1" style="width:auto"> {tt("Received damaged (report to the EPC and Prologis)", "Recibido dañado (reportar al EPC y a Prologis)")}</label>
<label>{tt("Note (inspection result, reason for an adjustment or scrapping)", "Nota (resultado de inspección, motivo de ajuste o baja)")}</label><input name="note" maxlength="1000">
<div style="margin-top:10px"><button class="btn">{tt("Record", "Registrar")}</button></div></form></div>
<div class="card"><h2>{tt("Parts, minimums and locations", "Refacciones, mínimos y ubicaciones")}</h2>
<form method="post" action="/assets/spares/part">{csrf_field()}<div class="row"><div><label>{tt("Code", "Código")}</label><input name="code" required></div><div><label>{tt("Category", "Categoría")}</label><select name="category">{catopts}</select></div></div>
<div class="row"><div><label>{tt("Name (EN)", "Nombre (EN)")}</label><input name="name_en" required></div><div><label>{tt("Name (ES)", "Nombre (ES)")}</label><input name="name_es"></div></div>
<div class="row"><div><label>{tt("Make", "Marca")}</label><input name="make"></div><div><label>{tt("Model", "Modelo")}</label><input name="model"></div></div>
<div class="row"><div><label>{tt("Owner", "Propietario")}</label><select name="owner">{oopts}</select></div><div><label>{tt("Unit", "Unidad")}</label><input name="unit" value="pc"></div></div>
<label><input type="checkbox" name="serialized" value="1" style="width:auto"> {tt("Tracked by serial number", "Por número de serie")}</label>
<div style="margin-top:8px"><button class="btn sm">{tt("Save part (same code = edit)", "Guardar (mismo código = editar)")}</button></div></form>
<form method="post" action="/assets/spares/min" style="margin-top:14px">{csrf_field()}<div class="row"><div><label>{tt("Minimum stock: part", "Mínimo: refacción")}</label><select name="part">{popts}</select></div><div><label>{tt("at location", "en ubicación")}</label><select name="loc">{lopts}</select></div></div>
<label>{tt("Minimum quantity", "Cantidad mínima")}</label><input name="min" inputmode="decimal"><div style="margin-top:8px"><button class="btn sm">{tt("Set minimum", "Fijar mínimo")}</button></div></form>
<form method="post" action="/assets/spares/location" style="margin-top:14px">{csrf_field()}<div class="row"><div><label>{tt("New location code", "Código de ubicación")}</label><input name="code"></div><div><label>{tt("Name", "Nombre")}</label><input name="name"></div></div>
<div style="margin-top:8px"><button class="btn sm ghost">{tt("Add location", "Agregar ubicación")}</button></div></form></div></div>"""
    body = (_assets_head(tt, tt("Spare parts", "Refacciones"),
                         tt("Critical spares in ARGIA's warehouse (never on site), serialized and tracked. Stock is the sum of recorded movements; shown as on hand / minimum.",
                            "Refacciones críticas en el almacén de ARGIA (nunca en sitio), por serie y rastreadas. El inventario es la suma de los movimientos registrados; se muestra existencia / mínimo."))
            + flash(msg, err) + _assets_tabs("spares", tt, n)
            + f'<div class="card" style="margin-top:10px;overflow-x:auto"><table class="t"><tr><th>{tt("Part", "Refacción")}</th>{head}</tr>{tr}</table></div>{forms}'
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Latest movements", "Últimos movimientos")}</h2><table class="t"><tr><th>{tt("Date", "Fecha")}</th><th>{tt("Movement", "Movimiento")}</th>'
            f'<th>{tt("Part", "Refacción")}</th><th class="n">{tt("Qty", "Cant.")}</th><th>{tt("From › to", "De › a")}</th><th>{tt("Ticket / note", "Ticket / nota")}</th><th>{tt("By", "Por")}</th></tr>{recent}</table></div>')
    return page(tt("Spare parts", "Refacciones"), body, "assets")


@app.post("/assets/spares/move")
def spares_move():
    need("assets_edit")
    c = db()
    f = request.form
    try:
        rep = int(f["replaces"]) if (f.get("replaces") or "").strip().isdigit() else None
        A.move(c, f.get("kind", ""), f.get("part", ""), _float(f.get("qty")) or 0, g.user["username"], f.get("from", ""), f.get("to", ""),
               f.get("serial", ""), f.get("site", ""), _ticket_id(c, f.get("ticket", "")), f.get("note", ""),
               "damaged" if f.get("damaged") else "ok", (f.get("day") or "").strip() or None, [s.code for s in reg().sites],
               rep, ip(), today=today_mx())
    except ValueError as ex:
        return spares_page(f"Not recorded: {ex}", True)
    return spares_page("Recorded. / Registrado.")


@app.post("/assets/spares/part")
def spares_part():
    need("assets_edit")
    try:
        A.save_part(db(), {k: request.form.get(k, "") for k in A.PART_FIELDS if k != "active"} | {"active": "1"}, g.user["username"], ip())
    except ValueError as ex:
        return spares_page(f"Not saved: {ex}", True)
    return spares_page("Saved. / Guardado.")


@app.post("/assets/spares/min")
def spares_min():
    need("assets_edit")
    q = _float(request.form.get("min"))
    try:
        if q is None:
            raise ValueError("give a minimum")
        A.set_min(db(), request.form.get("part", ""), request.form.get("loc", ""), q, g.user["username"], ip())
    except ValueError as ex:
        return spares_page(f"Not saved: {ex}", True)
    return spares_page("Saved. / Guardado.")


@app.post("/assets/spares/location")
def spares_location():
    need("assets_edit")
    try:
        A.add_location(db(), request.form.get("code", ""), request.form.get("name", ""), g.user["username"], ip=ip())
    except ValueError as ex:
        return spares_page(f"Not added: {ex}", True)
    return spares_page("Added. / Agregada.")


def _quarter_tables(c, q: str):
    pts = A.parts(c, include_inactive=True)
    locs = A.locations(c, include_inactive=True)
    rows, rest, inq = A.quarter_report(A.all_moves(c), [p["code"] for p in pts], [(lc["code"], bool(lc["transit"])) for lc in locs],
                                       A.minimums(c), q)
    return {p["code"]: p for p in pts}, rows, rest, inq


IN_KINDS = ("RECEIPT", "RETURN", "TRANSFER", "ADJUST_IN")


def _signed(v: float, sign: str) -> str:
    return f"{sign}{v:g}" if v else "0"

OUT_KINDS = ("ISSUE", "TRANSFER", "ADJUST_OUT", "SCRAP")


@app.get("/assets/spares/report")
def spares_report():
    tt = t()
    rg = reg()
    c = db()
    q = (request.args.get("q") or A.quarter_of(today_mx())).upper()
    try:
        start, end = A.quarter_bounds(q)
    except ValueError:
        abort(400)
    pmap, rows, rest, inq = _quarter_tables(c, q)
    fmt = request.args.get("fmt", "")
    hdr = ["part", "name", "owner", "location", "opening"] + [f"in_{k.lower()}" for k in IN_KINDS] + [f"out_{k.lower()}" for k in OUT_KINDS] + ["closing", "minimum", "below_minimum"]

    def line(r):
        p = pmap.get(r.part)
        return ([r.part, p["name_en"] if p else "", p["owner"] if p else "", r.loc, r.opening] + [r.ins.get(k, 0.0) for k in IN_KINDS]
                + [r.outs.get(k, 0.0) for k in OUT_KINDS] + [r.closing, "" if r.min_qty is None else r.min_qty, "yes" if r.below_min else ""])
    if fmt in ("xlsx", "csv"):
        mv = [["date", "movement", "part", "serial", "qty", "from", "to", "site", "ticket", "condition", "note", "by"]] + \
             [[m["day"], m["kind"], m["part_code"], m["serial"], m["qty"], m["from_loc"], m["to_loc"], m["site_code"],
               _ticket_no(c, m["ticket_id"]), m["condition"], m["note"], m["username"]] for m in inq]
        rs = [["part", "name", "minimum_total", "closing_in_warehouses", "in_transit", "recommended_order"]] + \
             [[x.part, pmap[x.part]["name_en"] if x.part in pmap else "", x.min_total, x.closing_total, x.transit, x.order_qty] for x in rest]
        S.audit(c, g.user["username"], ip(), "inventory_report_export", q, fmt)
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(hdr)
            w.writerows(line(r) for r in rows)
            return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=prologis_inventory_{q}.csv"})
        data = XL.workbook([("Stock " + q, [hdr] + [line(r) for r in rows]), ("Movements", mv), ("Restocking", rs)])
        return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename=prologis_inventory_{q}.xlsx"})
    n = asset_counts(c)
    qs = []
    d = today_mx()
    for _ in range(6):
        qs.append(A.quarter_of(d))
        d = A.quarter_bounds(A.quarter_of(d))[0] - dt.timedelta(days=1)
    qopts = "".join(f'<option {"selected" if x == q else ""}>{x}</option>' for x in qs)
    sr = ""
    low_pill = '<span class="pill s-bad">' + tt("below minimum", "bajo mínimo") + "</span>"
    for r in rows:
        p = pmap.get(r.part)
        sr += (f'<tr><td><b>{UI.e(r.part)}</b><div class="small muted">{UI.e(_part_name(p, tt) if p else "")}</div></td><td>{UI.e(r.loc)}</td><td class="n">{r.opening:g}</td>'
               f'<td class="n">{_signed(sum(r.ins.values()), "+")}</td><td class="n">{_signed(sum(r.outs.values()), "-")}</td><td class="n"><b>{r.closing:g}</b></td>'
               f'<td class="n">{"-" if r.min_qty is None else f"{r.min_qty:g}"}</td><td>{low_pill if r.below_min else ""}</td></tr>')
    sr = sr or f'<tr><td class="muted" colspan="8">{tt("No stock and no movements in this quarter.", "Sin inventario ni movimientos en este trimestre.")}</td></tr>'
    rr = "".join(f'<tr><td><b>{UI.e(x.part)}</b></td><td class="n">{x.min_total:g}</td><td class="n">{x.closing_total:g}</td><td class="n">{x.transit:g}</td>'
                 f'<td class="n"><b>{x.order_qty:g}</b></td></tr>' for x in rest) or f'<tr><td class="muted" colspan="5">{tt("No minimums set.", "Sin mínimos fijados.")}</td></tr>'
    by_kind: Dict[str, float] = {}
    for m in inq:
        by_kind[m["kind"]] = by_kind.get(m["kind"], 0.0) + m["qty"]
    mk = " · ".join(f'{UI.e(_lbl(A.MOVE_KINDS, k, tt))} <b>{v:g}</b>' for k, v in sorted(by_kind.items())) or tt("none", "ninguno")
    issued = "".join(f'<tr><td class="small">{UI.e(m["day"])}</td><td><b>{UI.e(m["part_code"])}</b>{" · " + UI.e(m["serial"]) if m["serial"] else ""}</td><td class="n">{m["qty"]:g}</td>'
                     f'<td>{UI.e(_site_name(rg, m["site_code"]))}</td><td class="small">{_ticket_no(c, m["ticket_id"])}</td></tr>' for m in inq if m["kind"] == "ISSUE") \
        or f'<tr><td class="muted" colspan="5">{tt("No parts issued to sites.", "Sin salidas a sitios.")}</td></tr>'
    damaged = [m for m in inq if m["condition"] == "damaged"]
    dm = ("".join(f'<li>{UI.e(m["day"])} · {UI.e(m["part_code"])} {m["qty"]:g} {UI.e(m["serial"])} - {UI.e(m["note"])}</li>' for m in damaged))
    body = (_assets_head(tt, f'{tt("Quarterly inventory report", "Reporte trimestral de inventario")} · {q}',
                         f'{start.isoformat()} - {end.isoformat()}. ' + tt("Opening + in - out = closing for every part and location; restocking brings each part back to its minimum, counting what is in transit.",
                                                                         "Inicial + entradas - salidas = final por refacción y ubicación; la reposición regresa cada refacción a su mínimo, contando lo que está en tránsito."))
            + _assets_tabs("report", tt, n)
            + f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0"><form method="get"><select name="q" onchange="this.form.submit()">{qopts}</select></form>'
            f'<a class="btn ghost" href="?q={q}&fmt=xlsx">{tt("Download spreadsheet", "Descargar hoja de cálculo")}</a> <a class="btn ghost" href="?q={q}&fmt=csv">CSV</a></div>'
            f'<div class="card"><h2>{tt("Movements in the quarter", "Movimientos del trimestre")}</h2><p>{mk}</p>'
            + (f'<p class="small"><b>{tt("Received damaged", "Recibido dañado")}:</b> {tt("counted in stock until scrapped or returned to the supplier.", "cuenta en el inventario hasta darlo de baja o devolverlo al proveedor.")}</p><ul class="small">{dm}</ul>' if damaged else "") + "</div>"
            f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Stock by part and location", "Inventario por refacción y ubicación")}</h2><table class="t"><tr><th>{tt("Part", "Refacción")}</th><th>{tt("Location", "Ubicación")}</th>'
            f'<th class="n">{tt("Opening", "Inicial")}</th><th class="n">{tt("In", "Entradas")}</th><th class="n">{tt("Out", "Salidas")}</th><th class="n">{tt("Closing", "Final")}</th><th class="n">{tt("Minimum", "Mínimo")}</th><th></th></tr>{sr}</table></div>'
            f'<div class="grid g2e" style="margin-top:16px"><div class="card"><h2>{tt("Restocking recommendation", "Recomendación de reposición")}</h2><table class="t"><tr><th>{tt("Part", "Refacción")}</th><th class="n">{tt("Minimum", "Mínimo")}</th>'
            f'<th class="n">{tt("In warehouses", "En almacenes")}</th><th class="n">{tt("In transit", "En tránsito")}</th><th class="n">{tt("Order", "Pedir")}</th></tr>{rr}</table></div>'
            f'<div class="card"><h2>{tt("Issued to sites", "Salidas a sitios")}</h2><table class="t">{issued}</table></div></div>')
    return page(tt("Inventory report", "Reporte de inventario"), body, "assets")


# ------------------------------------------------------------------ alarms (v321)
# Alarm engine results, triage within 4 business hours, the daily review log,
# the mail outbox and the alarm mail settings (MSA Schedule A, Monitoring;
# proposal 4.1). The engine itself runs from the 5-minute timer
# (prologis_app.py --alarm-run).
SEV_PILL = {"critical": "s-bad", "warning": "s-warn"}
REVIEW_PILL = {"on_time": "s-ok", "late": "s-warn", "missing": "s-bad", "today": "s-info", "before_start": "s-off"}
OUTBOX_PILL = {"sent": "s-ok", "dry_run": "s-info", "sample": "s-off", "pending": "s-warn", "failed": "s-bad", "no_recipient": "s-warn"}


def _dur(a_utc: str, b_utc: Optional[str] = None) -> str:
    a = dt.datetime.fromisoformat(a_utc)
    b = dt.datetime.fromisoformat(b_utc) if b_utc else utc_now()
    m = max(0, int((b - a).total_seconds() // 60))
    return f"{m // 1440} d {m % 1440 // 60} h" if m >= 1440 else (f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min")


def triage_pill(a, tt) -> str:
    st = AL.triage_state(a, utc_now())
    if st == "auto":
        return f'<span class="pill s-off">{tt("Cleared by itself", "Se resolvió sola")}</span>'
    if a["triaged_utc"]:
        lbl = dict((k, tt(en, es)) for k, en, es in AL.TRIAGE_ACTIONS).get(a["triage"], a["triage"])
        return f'<span class="pill {"s-ok" if st == "met" else "s-bad"}">{UI.e(lbl)}{"" if st == "met" else " · " + tt("late", "tarde")}</span>'
    due = AL.triage_due_local(a["detected_utc"])
    if st == "breached":
        return f'<span class="pill s-bad">{tt("Triage overdue since", "Clasificación vencida desde")} {due:%d %b %H:%M}</span>'
    left = AL.business_hours_between(now_mx(), due)
    return f'<span class="pill {"s-warn" if left < 1 else "s-lime"}">{tt("Triage due in", "Clasificar en")} {left:.1f} {tt("business h", "h hábiles")}</span>'


def alarm_counts(c) -> Dict[str, int]:
    nowu = utc_now()
    op = AL.open_alarms(c)
    st = AL.review_status(c, today_mx(), 30)
    return {"open": len(op), "critical": sum(1 for a in op if a["severity"] == "critical"),
            "overdue": sum(1 for a in c.execute("SELECT * FROM alarms WHERE triaged_utc=''") if AL.triage_state(a, nowu) == "breached"),
            "review_missing": sum(1 for _, s in st if s == "missing")}


def _alarm_tabs(on: str, tt, n: Dict[str, int]) -> str:
    role = g.user["role"]
    tabs = [("alarms", "/alarms/", tt("Alarms", "Alarmas"), n["open"]),
            ("review", "/alarms/review", tt("Daily review", "Revisión diaria"), n["review_missing"] or None)]
    if S.can(role, "alarms_work"):
        tabs.append(("outbox", "/alarms/outbox", tt("Mail outbox", "Bandeja de salida"), None))
    return '<div class="tabs">' + "".join(f'<a class="tab{" on" if k == on else ""}" href="{h}">{UI.e(lbl)}{"" if v is None else f" <b>{v}</b>"}</a>'
                                         for k, h, lbl, v in tabs) + "</div>"


def _mode_banner(c, tt) -> str:
    if AL.setting(c, "mail_mode") == "live":
        return f'<div class="flash">{tt("E-mail: LIVE - critical alarms are mailed at once, the digest at 07:30. Alarms from SAMPLE data are never mailed.", "Correo: EN VIVO - las alarmas críticas se envían al momento, el resumen a las 07:30. Las alarmas de datos de MUESTRA nunca se envían.")}</div>'
    return f'<div class="flash err">{tt("E-mail: DRY-RUN - every message is written to the outbox and shown here, nothing is sent. It switches to live when the SolarEdge / Hark data is connected.", "Correo: PRUEBA - cada mensaje se escribe en la bandeja de salida y se muestra aquí, no se envía nada. Pasa a vivo cuando se conecten los datos de SolarEdge / Hark.")}</div>'


def _alarm_rows(c, rg, rows, tt, work: bool) -> str:
    out = ""
    kl = dict((k, tt(en, es)) for k, en, es in AL.KINDS)
    for a in rows:
        cls = SLA.BY_CODE.get(a["msa_class"], SLA.BY_CODE["OTHER"])
        tk = _ticket_no(c, a["ticket_id"])
        form = ""
        if work and not a["triaged_utc"]:
            aopts = "".join(f'<option value="{k}">{UI.e(tt(en, es))}</option>' for k, en, es in AL.TRIAGE_ACTIONS)
            form = (f'<form method="post" action="/alarms/{a["id"]}/triage" style="display:flex;gap:4px;flex-wrap:wrap;margin-top:6px">{csrf_field()}'
                    f'<select name="action" style="width:auto">{aopts}</select><input name="ticket" placeholder="PL-0001" style="width:90px">'
                    f'<input name="note" placeholder="{tt("note / reason", "nota / motivo")}" style="width:170px"><button class="btn sm">{tt("Triage", "Clasificar")}</button></form>')
        out += (f'<tr id="a{a["id"]}"><td class="small muted">#{a["id"]}{SAMPLE_PILL if a["sample"] else ""}</td>'
                f'<td><b>{UI.e(_site_name(rg, a["site_code"]))}</b><div class="small muted">{UI.e(a["site_code"])}</div></td>'
                f'<td>{UI.e(kl.get(a["kind"], a["kind"]))}<div class="small muted">{UI.e(a["detail"])}</div></td>'
                f'<td><span class="pill {SEV_PILL.get(a["severity"], "s-off")}">{UI.e(tt("Critical" if a["severity"] == "critical" else "Warning", "Crítica" if a["severity"] == "critical" else "Advertencia"))}</span>'
                f'<div class="small muted">{cls.priority} · {UI.e(tt(cls.en, cls.es))}</div></td>'
                f'<td class="small">{mx_time(a["detected_utc"])}<div class="muted">{_dur(a["detected_utc"], a["cleared_utc"] or None)}'
                f'{" · " + tt("cleared", "resuelta") + " " + mx_time(a["cleared_utc"]) if a["cleared_utc"] else ""}</div></td>'
                f'<td>{triage_pill(a, tt)}{f" <a class=chip href=/tickets/{tk}/>{tk}</a>" if tk else ""}'
                f'{"<div class=small>" + UI.e(a["triage_note"]) + "</div>" if a["triage_note"] else ""}{form}</td></tr>')
    return out


@app.get("/alarms/")
def alarms_page(msg: str = "", err: bool = False):
    tt = t()
    rg = reg()
    c = db()
    n = alarm_counts(c)
    work = S.can(g.user["role"], "alarms_work")
    head = (f'<tr><th>#</th><th>{tt("Site", "Sitio")}</th><th>{tt("Alarm", "Alarma")}</th><th>{tt("Severity / MSA class", "Severidad / clase MSA")}</th>'
            f'<th>{tt("Detected / duration", "Detectada / duración")}</th><th>{tt("Triage (4 business hours)", "Clasificación (4 horas hábiles)")}</th></tr>')
    op = _alarm_rows(c, rg, AL.open_alarms(c), tt, work) or f'<tr><td class="muted" colspan="6">{tt("No open alarms.", "Sin alarmas abiertas.")}</td></tr>'
    since = (utc_now() - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    cl = _alarm_rows(c, rg, c.execute("SELECT * FROM alarms WHERE cleared_utc<>'' AND cleared_utc>=? ORDER BY id DESC LIMIT 100", (since,)).fetchall(), tt, work) \
        or f'<tr><td class="muted" colspan="6">{tt("Nothing cleared in the last 7 days.", "Ninguna resuelta en los últimos 7 días.")}</td></tr>'
    res = [AL.triage_state(a, utc_now()) for a in c.execute("SELECT * FROM alarms WHERE detected_utc>=?", ((utc_now() - dt.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),))]
    closed = [x for x in res if x in ("met", "breached")]
    comp = f'{sum(1 for x in closed if x == "met") / len(closed) * 100:.0f}%' if closed else "-"
    rc = AL.review_compliance(AL.review_status(c, today_mx(), 30))
    me = S.user(c, g.user["username"])
    popts = "".join(f'<option value="{k}" {"selected" if k == (me["alarm_mail"] or "") else ""}>{UI.e(tt(en, es))}</option>' for k, en, es in AL.MAIL_PREFS)
    mine = (f'<div class="card" style="margin-top:16px"><h2>{tt("My alarm e-mails", "Mis correos de alarmas")}</h2>'
            f'<form method="post" action="/alarms/me" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">{csrf_field()}<select name="pref" style="width:auto">{popts}</select>'
            f'<button class="btn sm">{tt("Save", "Guardar")}</button><span class="small muted">{tt("to", "a")} {UI.e(me["email"] or tt("(no e-mail on your user - ask an administrator)", "(su usuario no tiene correo - pida a un administrador)"))}</span></form></div>')
    sett = ""
    if S.can(g.user["role"], "alarms_admin"):
        mode = AL.setting(c, "mail_mode")
        mo = "".join(f'<option value="{k}" {"selected" if k == mode else ""}>{k}</option>' for k in AL.MAIL_MODES)
        sett = (f'<div class="card form" style="margin-top:16px"><h2>{tt("Alarm mail settings", "Configuración de correos de alarmas")}</h2><form method="post" action="/alarms/settings">{csrf_field()}'
                f'<label>{tt("ARGIA desk addresses (critical alarms at any hour and the daily digest)", "Direcciones de la mesa ARGIA (alarmas críticas a cualquier hora y el resumen diario)")}</label>'
                f'<input name="desk_emails" value="{UI.e(AL.setting(c, "desk_emails"))}" placeholder="monitoring@argia.com.mx">'
                f'<label>{tt("Mode", "Modo")}</label><select name="mail_mode">{mo}</select>'
                f'<div style="margin-top:10px"><button class="btn sm">{tt("Save", "Guardar")}</button></div></form></div>')
    kp = (f'<div class="grid g3">'
          f'<div class="card"><h2>{tt("Open alarms", "Alarmas abiertas")}</h2><div style="font-size:26px;font-weight:800;color:var(--deep)">{n["open"]} <small style="font-size:13px;color:var(--muted)">{n["critical"]} {tt("critical", "críticas")}</small></div><div class="small muted">{n["overdue"]} {tt("waiting for triage beyond 4 business hours", "sin clasificar tras 4 horas hábiles")}</div></div>'
          f'<div class="card"><h2>{tt("Triage within 4 business hours", "Clasificación en 4 horas hábiles")}</h2><div style="font-size:26px;font-weight:800;color:var(--deep)">{comp}</div><div class="small muted">{tt("last 30 days; Mon-Fri 09:00-18:00, public holidays off", "últimos 30 días; lun-vie 09:00-18:00, sin días feriados")}</div></div>'
          f'<div class="card"><h2>{tt("Daily performance review", "Revisión diaria de desempeño")}</h2><div style="font-size:26px;font-weight:800;color:var(--deep)">{"-" if rc is None else f"{rc * 100:.0f}%"}</div><div class="small muted">{n["review_missing"]} {tt("day(s) missed in 30 days", "día(s) sin revisión en 30 días")}</div></div></div>')
    body = (f'<div class="kick">{tt("Monitoring", "Monitoreo")}</div><h1 class="pt">{tt("Alarms", "Alarmas")}</h1>'
            f'<p class="muted small">{tt("Every operating site is checked every 5 minutes: production loss, communication loss, meter, weather sensor and data logger. Classes and deadlines are the MSA response-time table; a ticket opened from an alarm keeps the alarm time as its detection time.", "Cada sitio en operación se revisa cada 5 minutos: pérdida de producción, de comunicación, medidor, sensor meteorológico y datalogger. Clases y plazos de la tabla de tiempos de respuesta del MSA; un ticket abierto desde una alarma conserva la hora de la alarma como detección.")}</p>'
            + flash(msg, err) + _mode_banner(c, tt) + _alarm_tabs("alarms", tt, n) + kp
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Open", "Abiertas")}</h2><table class="t">{head}{op}</table></div>'
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Cleared in the last 7 days", "Resueltas en los últimos 7 días")}</h2><table class="t">{head}{cl}</table></div>'
            + mine + sett)
    return page(tt("Alarms", "Alarmas"), body, "alarms", sample=True)


@app.post("/alarms/<int:aid>/triage")
def alarm_triage(aid):
    need("alarms_work")
    c = db()
    a = AL.alarm(c, aid) or abort(404)
    f = request.form
    try:
        num = AL.triage(c, a, f.get("action", ""), g.user["username"], f.get("note", ""), f.get("ticket", ""), ip=ip())
    except ValueError as ex:
        return alarms_page(f"Not triaged: {ex}", True)
    return alarms_page(f"Alarm #{aid} triaged" + (f" - ticket {num}" if num else "") + ".")


@app.post("/alarms/me")
def alarm_me():
    try:
        AL.set_mail_pref(db(), g.user["username"], request.form.get("pref", ""), ip())
    except ValueError:
        abort(400)
    return alarms_page("Saved. / Guardado.")


@app.post("/alarms/settings")
def alarm_settings():
    need("alarms_admin")
    c = db()
    try:
        AL.set_setting(c, "desk_emails", request.form.get("desk_emails", ""), g.user["username"], ip())
        AL.set_setting(c, "mail_mode", request.form.get("mail_mode", "dry_run"), g.user["username"], ip())
    except ValueError as ex:
        return alarms_page(f"Not saved: {ex}", True)
    return alarms_page("Saved. / Guardado.")


def review_evidence(rg, day: dt.date) -> List[Dict]:
    """Per operating site for one day: energy vs expected, PR, availability
    and data completeness (SAMPLE until live data) - what the reviewer
    looked at, stored with the review."""
    op = rg.operating
    upto = now_mx().time() if day == today_mx() else None
    out = []
    for i, s in enumerate(op):
        r = M.day_result(s.code, s.lat, s.lon, s.kwp, day, upto=upto, index=i, n_sites=len(op))
        comp = sum(1 for _, v in r.series if v is not None) / max(1, len(r.series))
        out.append({"site": s.code, "name": s.name, "kwh": r.kwh, "expected_kwh": r.expected_kwh,
                    "ratio": round(r.kwh / r.expected_kwh, 3) if r.expected_kwh else None, "pr": r.pr,
                    "availability": r.availability, "completeness": round(comp, 3), "source": M.source_for(s)})
    return out


@app.get("/alarms/review")
def review_page(msg: str = "", err: bool = False):
    tt = t()
    rg = reg()
    c = db()
    n = alarm_counts(c)
    today = today_mx()
    st = AL.review_status(c, today, 30)
    lbl = {"on_time": tt("Reviewed", "Revisado"), "late": tt("Reviewed late", "Revisado tarde"), "missing": tt("Missed", "Sin revisión"),
           "today": tt("To do today", "Pendiente hoy"), "before_start": tt("Before the log started", "Antes del inicio")}
    rows = {r["day"]: r for r in c.execute("SELECT * FROM daily_review")}
    tr = ""
    for d, s in st:
        if s == "before_start":
            continue
        r = rows.get(d)
        tr += (f'<tr><td class="nw">{d}</td><td><span class="pill {REVIEW_PILL[s]}">{UI.e(lbl[s])}</span></td>'
               f'<td>{UI.e(r["username"]) if r else ""}</td><td class="small">{UI.e(r["findings"]) if r else ""}</td></tr>')
    day = request.args.get("day") or (today - dt.timedelta(days=1)).isoformat()
    try:
        dday = dt.date.fromisoformat(day)
    except ValueError:
        abort(400)
    form = ""
    if S.can(g.user["role"], "alarms_work"):
        open_days = [d for d, s in st if s in ("missing", "today") and (today - dt.date.fromisoformat(d)).days <= AL.REVIEW_GRACE_DAYS]
        if not AL.setting(c, "review_start"):
            open_days = [today.isoformat(), (today - dt.timedelta(days=1)).isoformat()]
        dopts = "".join(f'<option {"selected" if d == day else ""}>{d}</option>' for d in open_days)
        ev = review_evidence(rg, dday)
        er = ""
        for x in ev:
            ratio = "-" if x["ratio"] is None else f'{x["ratio"] * 100:.0f}%'
            pr = "-" if x["pr"] is None else f'{x["pr"] * 100:.1f}%'
            er += (f'<tr><td><b>{UI.e(x["name"])}</b></td><td class="n">{UI.num(x["kwh"])}</td><td class="n">{UI.num(x["expected_kwh"])}</td>'
                   f'<td class="n">{ratio}</td><td class="n">{pr}</td><td class="n">{x["availability"] * 100:.1f}%</td>'
                   f'<td class="n">{x["completeness"] * 100:.0f}%</td></tr>')
        form = (f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Record the review", "Registrar la revisión")}</h2>'
                + (f'<form method="get" style="margin-bottom:8px"><select name="day" style="width:auto" onchange="this.form.submit()">{dopts}</select></form>' if open_days else
                   f'<p class="small muted">{tt("Every day in the last 3 days is reviewed.", "Todos los días de los últimos 3 están revisados.")}</p>')
                + f'<table class="t small"><tr><th>{tt("Site", "Sitio")}</th><th class="n">kWh</th><th class="n">{tt("Expected", "Esperado")}</th><th class="n">%</th><th class="n">PR</th>'
                f'<th class="n">{tt("Avail.", "Disp.")}</th><th class="n">{tt("Data", "Datos")}</th></tr>{er}</table>'
                + (f'<form method="post" action="/alarms/review">{csrf_field()}<input type="hidden" name="day" value="{UI.e(day)}">'
                   f'<label>{tt("Findings at inverter level (or: no findings)", "Hallazgos a nivel inversor (o: sin hallazgos)")}</label><textarea name="findings" required></textarea>'
                   f'<div style="margin-top:10px"><button class="btn">{tt("Reviewed", "Revisado")} {UI.e(day)}</button></div></form>' if day in open_days else "")
                + "</div>")
    rc = AL.review_compliance(st)
    body = (f'<div class="kick">{tt("Monitoring", "Monitoreo")}</div><h1 class="pt">{tt("Daily performance review", "Revisión diaria de desempeño")}</h1>'
            f'<p class="muted small">{tt("MSA Schedule A: on a daily basis, review and analyze system performance at the inverter level. One entry per calendar day, with the numbers that were reviewed; up to 3 days late is accepted and marked late.", "MSA Anexo A: revisar y analizar a diario el desempeño a nivel inversor. Una entrada por día calendario, con las cifras revisadas; hasta 3 días tarde se acepta y se marca tarde.")}'
            f' {tt("On time, last 30 days", "A tiempo, últimos 30 días")}: <b>{"-" if rc is None else f"{rc * 100:.0f}%"}</b></p>'
            + flash(msg, err) + _alarm_tabs("review", tt, n) + form
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><table class="t"><tr><th>{tt("Day", "Día")}</th><th>{tt("Status", "Estado")}</th><th>{tt("By", "Por")}</th><th>{tt("Findings", "Hallazgos")}</th></tr>'
            + (tr or f'<tr><td class="muted" colspan="4">{tt("The log starts with the first recorded review.", "La bitácora inicia con la primera revisión registrada.")}</td></tr>') + '</table></div>')
    return page(tt("Daily review", "Revisión diaria"), body, "alarms", sample=True)


@app.post("/alarms/review")
def review_post():
    need("alarms_work")
    day = (request.form.get("day") or "").strip()
    try:
        d = dt.date.fromisoformat(day)
        ev = review_evidence(reg(), d)
        AL.save_review(db(), day, g.user["username"], request.form.get("findings", ""), len(ev), {"sites": ev}, today_mx(), ip())
    except ValueError as ex:
        return review_page(f"Not recorded: {ex}", True)
    return review_page(f"Review of {day} recorded. / Revisión de {day} registrada.")


@app.get("/alarms/outbox")
def outbox_page():
    need("alarms_work")
    tt = t()
    c = db()
    n = alarm_counts(c)
    rows = c.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 100").fetchall()
    stl = {"sent": tt("Sent", "Enviado"), "dry_run": tt("Dry-run, not sent", "Prueba, no enviado"), "sample": tt("Sample data, never sent", "Datos de muestra, nunca se envía"),
           "pending": tt("Waiting to send", "Por enviar"), "failed": tt("Failed", "Falló"), "no_recipient": tt("No recipient", "Sin destinatario")}
    tr = "".join(f'<tr><td class="small nw">{mx_time(m["created_utc"])}</td><td><span class="pill {OUTBOX_PILL.get(m["status"], "s-off")}">{UI.e(stl.get(m["status"], m["status"]))}</span></td>'
                 f'<td><details><summary>{UI.e(m["subject"])}</summary><pre class="small" style="white-space:pre-wrap">{UI.e(m["body"])}</pre></details>'
                 f'<div class="small muted">{UI.e(m["to_addrs"] or "-")}</div></td></tr>' for m in rows) \
        or f'<tr><td class="muted" colspan="3">{tt("No messages yet.", "Sin mensajes aún.")}</td></tr>'
    body = (f'<div class="kick">{tt("Monitoring", "Monitoreo")}</div><h1 class="pt">{tt("Mail outbox", "Bandeja de salida")}</h1>'
            + _mode_banner(c, tt) + _alarm_tabs("outbox", tt, n)
            + f'<div class="card" style="margin-top:10px;overflow-x:auto"><table class="t"><tr><th>{tt("Created", "Creado")}</th><th>{tt("Status", "Estado")}</th><th>{tt("Message", "Mensaje")}</th></tr>{tr}</table></div>')
    return page(tt("Mail outbox", "Bandeja de salida"), body, "alarms")


# ------------------------------------------------------------------ monthly report (v322)
# MSA Schedule A, Data & Reporting: monthly report per site and for the
# portfolio within 10 days of month end; proposal 4.4: also as spreadsheet,
# availability, HSE. Drafts are ARGIA's; Prologis sees published reports,
# whose numbers are frozen at publication.
DL_PILL = {"met": "s-ok", "late": "s-bad", "due": "s-warn", "overdue": "s-bad", "open": "s-off"}


def _pct(v, d: int = 1) -> str:
    return "-" if v is None else f"{v * 100:.{d}f}%"


def _ratio(a, b) -> Optional[float]:
    return (a / b) if (a is not None and b) else None


def _dl_label(st: str, tt) -> str:
    return {"met": tt("Published on time", "Publicado a tiempo"), "late": tt("Published late", "Publicado tarde"),
            "due": tt("Due", "Por publicar"), "overdue": tt("Overdue", "Vencido"), "open": tt("Month running", "Mes en curso")}[st]


def _months(today: dt.date, n: int = 12) -> List[str]:
    out, d = [], today
    for _ in range(n):
        out.append(f"{d.year}-{d.month:02d}")
        d = d.replace(day=1) - dt.timedelta(days=1)
    return out


def _reports_head(tt, title: str, sub: str = "") -> str:
    tabs = [("/reports/", tt("Monthly reports", "Reportes mensuales")), ("/availability/", tt("Availability", "Disponibilidad")),
            ("/availability/annual", tt("Annual analysis", "Análisis anual")), ("/reports/hse", tt("HSE register", "Registro SSMA")),
            ("/reports/design", tt("Design yield", "Producción de diseño"))]
    cur = request.path
    t_ = '<div class="tabs">' + "".join(f'<a class="tab{" on" if h == cur else ""}" href="{h}">{UI.e(lbl)}</a>' for h, lbl in tabs) + "</div>"
    return (f'<div class="kick">{tt("Reports", "Reportes")}</div><h1 class="pt">{UI.e(title)}</h1>'
            + (f'<p class="muted small">{sub}</p>' if sub else "") + t_)


@app.get("/reports/")
def reports_page(msg: str = "", err: bool = False):
    tt = t()
    c = db()
    today = today_mx()
    work = S.can(g.user["role"], "alarms_work")
    tr = ""
    for m in _months(today):
        p = MR.published(c, m)
        st = MR.deadline_state(m, p["published_utc"] if p else "", today)
        state = (f'<span class="pill s-ok">{tt("Published", "Publicado")} {mx_time(p["published_utc"])}</span>' if p
                 else f'<span class="pill s-off">{tt("Draft (ARGIA only)", "Borrador (solo ARGIA)")}</span>')
        link = (f'<a href="/reports/{m}/"><b>{m}</b></a>' if (p or work) else f"<b>{m}</b>")
        xl = f' <a class="small" href="/reports/{m}/report.xlsx">{tt("spreadsheet", "hoja de cálculo")}</a>' if (p or work) else ""
        tr += (f'<tr><td>{link}{xl}</td><td>{state}</td><td class="nw">{MR.deadline(m):%d %b %Y}</td>'
               f'<td><span class="pill {DL_PILL[st]}">{UI.e(_dl_label(st, tt))}</span></td></tr>')
    g_ = float(AL.setting(c, "availability_guarantee") or 0.98)
    gform = ""
    if S.can(g.user["role"], "alarms_admin"):
        gform = (f'<form method="post" action="/reports/guarantee" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">{csrf_field()}'
                 f'<span class="small">{tt("Availability guarantee", "Garantía de disponibilidad")}</span><input name="g" value="{g_:g}" style="width:90px">'
                 f'<button class="btn sm ghost">{tt("Save", "Guardar")}</button></form>')
    body = (_reports_head(tt, tt("Monthly reports", "Reportes mensuales"),
                          tt("One report per month for each site and the portfolio, published within 10 days of month end (MSA Schedule A). Published numbers are frozen; drafts are visible to ARGIA only.",
                             "Un reporte por mes para cada sitio y el portafolio, publicado dentro de 10 días tras el cierre (MSA Anexo A). Las cifras publicadas quedan fijas; los borradores solo los ve ARGIA."))
            + flash(msg, err)
            + f'<div class="card" style="margin-top:10px;overflow-x:auto"><table class="t"><tr><th>{tt("Month", "Mes")}</th><th>{tt("Status", "Estado")}</th>'
            f'<th>{tt("Deadline", "Fecha límite")}</th><th>{tt("10-day rule", "Regla de 10 días")}</th></tr>{tr}</table>'
            f'<p class="small muted" style="margin-top:8px">{tt("Availability guarantee used in the reports", "Garantía de disponibilidad usada en los reportes")}: <b>{g_ * 100:g}%</b></p>{gform}</div>')
    return page(tt("Reports", "Reportes"), body, "reports")


@app.post("/reports/guarantee")
def reports_guarantee():
    need("alarms_admin")
    try:
        AL.set_setting(db(), "availability_guarantee", (request.form.get("g") or "").strip(), g.user["username"], ip())
    except ValueError as ex:
        return reports_page(f"Not saved: {ex}", True)
    return reports_page("Saved. / Guardado.")


def _report_or_404(month: str):
    try:
        MR.month_bounds(month)
    except ValueError:
        abort(404)
    c = db()
    d, frozen = MR.report_data(c, reg(), month, today_mx())
    if not frozen and not S.can(g.user["role"], "alarms_work"):
        abort(404)                                    # drafts are ARGIA's
    return d, frozen


def _kpi(label: str, value: str, sub: str = "") -> str:
    return f'<div class="kpi"><div class="l">{label}</div><div class="v">{value}</div><div class="d">{sub}</div></div>'


def _report_tables(d: Dict, tt, rg, site: str = "") -> str:
    keep = (lambda x: x == site) if site else (lambda x: True)
    out = [o for o in d["outages"] if keep(o["site"])]
    orows = "".join(f'<tr><td class="nw"><a href="/tickets/{o["number"]}/">{o["number"]}</a>{SAMPLE_PILL if o["sample"] else ""}</td>'
                    f'<td>{UI.e(_site_name(rg, o["site"]))}<div class="small">{UI.e(o["title"])}</div><div class="small muted">{UI.e(o["reason"])}</div></td>'
                    f'<td class="small">{mx_time(o["detected"])}</td><td class="n">{o["days"]:.1f}</td><td class="n">{UI.num(o["kw_lost"]) if o["kw_lost"] else "-"}</td>'
                    f'<td class="small">{UI.e(o["eta"] or ("-" if o["resolved"] else tt("not set", "sin fecha")))}{" · " + tt("back", "restablecido") + " " + mx_time(o["resolved"]) if o["resolved"] else ""}</td>'
                    f'<td class="small">{UI.e(o["action"])}</td></tr>' for o in out) \
        or f'<tr><td class="muted" colspan="7">{tt("No outage lasted more than 3 days.", "Ninguna falla duró más de 3 días.")}</td></tr>'
    wos = [w for w in d["work_orders"] if keep(w["site"])]
    stl = {"met": tt("Met", "Cumplido"), "breached": tt("Breached", "Incumplido"), "running": tt("Running", "En curso"),
           "due": tt("Next site visit", "Próxima visita"), "not_started": tt("Awaiting approval", "Esperando aprobación")}
    wrows = ""
    for w in wos:
        dl_txt = "-" if w["deadline_h"] is None else f'{w["deadline_h"]:g} h'
        hr_txt = "-" if w["hours"] is None else f'{w["hours"]:.1f} h'
        pc = "s-ok" if w["clock"] == "met" else ("s-bad" if w["clock"] == "breached" else "s-off")
        wrows += (f'<tr><td class="nw"><a href="/tickets/{w["number"]}/">{w["number"]}</a>{SAMPLE_PILL if w["sample"] else ""}</td><td>{UI.e(_site_name(rg, w["site"]))}<div class="small">{UI.e(w["title"])}</div></td>'
                  f'<td class="small">{w["priority"]} · {UI.e(w["class"])}</td><td class="n">{dl_txt}</td><td class="n">{hr_txt}</td>'
                  f'<td><span class="pill {pc}">{UI.e(stl.get(w["clock"], w["clock"]))}</span></td></tr>')
    wrows = wrows or f'<tr><td class="muted" colspan="6">{tt("No work orders this month.", "Sin órdenes de trabajo este mes.")}</td></tr>'
    logs = [x for x in d["log"] if keep(x["site"])]
    lrows = "".join(f'<tr><td class="small nw">{mx_time(x["ts"])}</td><td class="small">{UI.e(_site_name(rg, x["site"]))}</td>'
                    f'<td class="small">{UI.e(x["type"])}{(" · " + UI.e(x["severity"])) if x["severity"] else ""}{SAMPLE_PILL if x["sample"] else ""}</td><td class="small">{UI.e(x["text"])}</td></tr>'
                    for x in logs[:300]) or f'<tr><td class="muted" colspan="4">{tt("Nothing logged.", "Sin registros.")}</td></tr>'
    more = f'<p class="small muted">{len(logs) - 300} {tt("more lines in the spreadsheet", "líneas más en la hoja de cálculo")}</p>' if len(logs) > 300 else ""
    hs = [h for h in d["hse"] if keep(h["site"]) or not h["site"]]
    hk = dict((k, tt(en, es)) for k, en, es in MR.HSE_KINDS)
    ok24 = '<span class="pill s-ok">24 h</span>'
    late24 = '<span class="pill s-bad">&gt; 24 h</span>'
    hrows = ""
    for h in hs:
        flag = "" if h["on_time"] is None else (ok24 if h["on_time"] else late24)
        hrows += (f'<tr><td class="small nw">{mx_time(h["occurred"])}</td><td class="small">{UI.e(_site_name(rg, h["site"]) if h["site"] else "-")}</td>'
                  f'<td>{UI.e(hk.get(h["kind"], h["kind"]))}<div class="small">{UI.e(h["description"])}</div></td><td>{flag}</td></tr>')
    hrows = hrows or f'<tr><td class="muted" colspan="4">{tt("No HSE events.", "Sin eventos SSMA.")}</td></tr>'
    return (f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Outages longer than 3 days", "Fallas de más de 3 días")}</h2><table class="t"><tr><th>#</th><th>{tt("Site / reason", "Sitio / causa")}</th>'
            f'<th>{tt("Since", "Desde")}</th><th class="n">{tt("Days", "Días")}</th><th class="n">kW</th><th>{tt("Estimated return to service", "Regreso estimado a servicio")}</th><th>{tt("Corrective action", "Acción correctiva")}</th></tr>{orows}</table></div>'
            f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Work orders and response time", "Órdenes de trabajo y tiempo de respuesta")}</h2><table class="t"><tr><th>#</th><th>{tt("Site", "Sitio")}</th>'
            f'<th>{tt("MSA class", "Clase MSA")}</th><th class="n">{tt("Deadline", "Plazo")}</th><th class="n">{tt("Response", "Respuesta")}</th><th></th></tr>{wrows}</table></div>'
            f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Alarms and O&M log", "Bitácora de alarmas y O&M")}</h2><table class="t">{lrows}</table>{more}</div>'
            f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Health, safety and environment", "Salud, seguridad y medio ambiente")}</h2><table class="t">{hrows}</table></div>')


@app.get("/reports/<month>/")
def report_page(month, msg: str = "", err: bool = False):
    tt = t()
    rg = reg()
    c = db()
    d, frozen = _report_or_404(month)
    p = d["portfolio"]
    pub = MR.published(c, month)
    today = today_mx()
    st = MR.deadline_state(month, pub["published_utc"] if pub else "", today)
    status = (f'<span class="pill s-ok">{tt("Published", "Publicado")} {mx_time(pub["published_utc"])} · {UI.e(pub["published_by"])}</span>' if pub
              else f'<span class="pill s-warn">{tt("DRAFT - ARGIA only", "BORRADOR - solo ARGIA")}</span>')
    dl = f'<span class="pill {DL_PILL[st]}">{UI.e(_dl_label(st, tt))} · {tt("deadline", "límite")} {MR.deadline(month):%d %b %Y}</span>'
    msa = p.get("msa_availability")
    av_ok = msa is not None and msa + 1e-9 >= d["guarantee"]
    pend = p.get("exclusions_pending", 0)
    tiles = ('<div class="hero tiles"><div class="kpis">'
             + _kpi(tt("Actual energy", "Energía real"), f'{UI.num(p["kwh"] / 1000, 1)}<small>MWh</small>', f'{p["days"]} {tt("of", "de")} {p["days_in_month"]} {tt("days", "días")}')
             + _kpi(tt("vs expected (weather adjusted)", "vs esperado (ajustado por clima)"), _pct(_ratio(p["kwh"], p["expected_kwh"])), f'{UI.num(p["expected_kwh"] / 1000, 1)} MWh')
             + _kpi(tt("vs design (PVsyst / Helioscope)", "vs diseño (PVsyst / Helioscope)"), _pct(_ratio(p["kwh"] if p["design_sites"] == p["sites"] else None, p["design_kwh"])),
                    (f'{p["design_sites"]} {tt("of", "de")} {p["sites"]} {tt("sites provided", "sitios con dato")}'))
             + _kpi(tt("Insolation", "Insolación"), f'{p["irr_kwh_m2"]:.1f}<small>kWh/m²</small>', tt("kWp-weighted", "ponderada por kWp"))
             + _kpi("PR", _pct(p["pr"]), "")
             + _kpi(tt("MSA availability", "Disponibilidad MSA"), _pct(msa, 2),
                    f'{tt("guarantee", "garantía")} {d["guarantee"] * 100:g}% · ' + (tt("met", "cumplida") if av_ok else tt("below", "debajo"))
                    + (f' · {pend} {tt("exclusion(s) awaiting Prologis", "exclusión(es) esperando a Prologis")}' if pend else ""))
             + _kpi(tt("Response time met", "Tiempo de respuesta cumplido"), f'{d["response_met"]}/{d["response_closed"]}', tt("work orders", "órdenes"))
             + _kpi(tt("Data completeness", "Completitud de datos"), _pct(p["completeness"], 1), "")
             + "</div></div>")
    srows = ""
    for x in d["sites"]:
        srows += (f'<tr><td><a href="/reports/{month}/{x["code"]}/"><b>{UI.e(x["name"])}</b></a><div class="small muted">{x["code"]}</div></td><td class="n">{UI.num(x["kwp"], 0)}</td>'
                  f'<td class="n">{UI.num(x["kwh"])}</td><td class="n">{UI.num(x["expected_kwh"])}</td><td class="n">{"-" if x["design_kwh"] is None else UI.num(x["design_kwh"])}</td>'
                  f'<td class="n">{_pct(_ratio(x["kwh"], x["expected_kwh"]), 0)}</td><td class="n">{x["irr_kwh_m2"]:.1f}</td><td class="n">{_pct(x["pr"])}</td>'
                  f'<td class="n">{_pct(x.get("msa_availability"), 2)}</td><td class="n">{_pct(x["completeness"], 0)}</td></tr>')
    chart = UI.bar_chart([(k, a, e) for k, a, e in d["daily"]], w=1300, h=260)
    notes = ""
    if pub:
        notes = f'<div class="card" style="margin-top:16px"><h2>{tt("Summary", "Resumen")}</h2><p style="white-space:pre-wrap">{UI.e(pub["notes"])}</p></div>'
    elif S.can(g.user["role"], "alarms_work"):
        can_pub = today > MR.month_bounds(month)[1]
        notes = (f'<div class="card form" style="margin-top:16px"><h2>{tt("Publish to Prologis", "Publicar a Prologis")}</h2>'
                 + (f'<form method="post" action="/reports/{month}/publish">{csrf_field()}<label>{tt("Summary for Prologis (findings, actions, open items)", "Resumen para Prologis (hallazgos, acciones, pendientes)")}</label>'
                    f'<textarea name="notes" required></textarea><div style="margin-top:10px"><button class="btn">{tt("Publish - the numbers are frozen", "Publicar - las cifras quedan fijas")}</button></div></form>'
                    if can_pub else f'<p class="small muted">{tt("Publishing opens when the month is over.", "La publicación abre cuando termina el mes.")}</p>') + "</div>")
    oi = d["open_items"]
    open_html = "".join(f'<li><a href="/tickets/{x["number"]}/">{x["number"]}</a> {UI.e(x["title"])} <span class="small muted">{UI.e(_site_name(rg, x["site"]))} · {UI.e(x["status"])}</span></li>'
                        for x in oi["incidents"] + oi["orders"]) or f'<li class="muted">{tt("Nothing open.", "Nada abierto.")}</li>'
    body = (_reports_head(tt, f'{tt("Monthly O&M report", "Reporte mensual de O&M")} · {month}',
                          f'{d["first_day"]} - {d["last_day"]} · {tt("generated", "generado")} {mx_time(d["generated_utc"])}')
            + flash(msg, err)
            + f'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0">{status} {dl}'
            f'<a class="btn ghost" style="margin-left:auto" href="/reports/{month}/report.xlsx">{tt("Spreadsheet", "Hoja de cálculo")}</a>'
            f'<a class="btn ghost" href="javascript:window.print()">{tt("Print / PDF", "Imprimir / PDF")}</a></div>'
            + tiles + notes
            + f'<div class="card" style="margin-top:16px"><h2>{tt("Portfolio energy per day", "Energía del portafolio por día")}<span class="r muted">{tt("bar: actual · line: expected (weather adjusted)", "barra: real · línea: esperado (ajustado por clima)")}</span></h2>{chart}</div>'
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Sites", "Sitios")}</h2><table class="t"><tr><th>{tt("Site", "Sitio")}</th><th class="n">kWp</th>'
            f'<th class="n">{tt("Actual", "Real")} kWh</th><th class="n">{tt("Expected", "Esperado")} kWh</th><th class="n">{tt("Design", "Diseño")} kWh</th><th class="n">{tt("vs exp.", "vs esp.")}</th>'
            f'<th class="n">kWh/m²</th><th class="n">PR</th><th class="n">{tt("MSA avail.", "Disp. MSA")}</th><th class="n">{tt("Data", "Datos")}</th></tr>{srows}</table>'
            f'<p class="small muted">{tt("MSA availability: irradiance above 150 W/m², weighted by the DC behind each unavailable component, from the incident register with the exclusions Prologis accepted.", "Disponibilidad MSA: irradiancia sobre 150 W/m², ponderada por el DC de cada componente no disponible, del registro de incidentes con las exclusiones aceptadas por Prologis.")} <a href="/availability/?m={month}">{tt("Details", "Detalle")} ›</a></p></div>'
            + _report_tables(d, tt, rg)
            + f'<div class="card" style="margin-top:16px"><h2>{tt("Open corrective items and service orders", "Correctivos y órdenes de servicio abiertos")}</h2><ul class="small">{open_html}</ul></div>')
    return page(f'{tt("Report", "Reporte")} {month}', body, "reports", sample=d["source"] == "sample")


@app.get("/reports/<month>/<code>/")
def report_site_page(month, code):
    tt = t()
    rg = reg()
    d, _frozen = _report_or_404(month)
    x = next((s for s in d["sites"] if s["code"] == code.upper()), None) or abort(404)
    tiles = ('<div class="hero tiles"><div class="kpis">'
             + _kpi(tt("Actual energy", "Energía real"), f'{UI.num(x["kwh"] / 1000, 1)}<small>MWh</small>', f'{UI.num(x["kwp"], 1)} kWp')
             + _kpi(tt("vs expected", "vs esperado"), _pct(_ratio(x["kwh"], x["expected_kwh"])), f'{UI.num(x["expected_kwh"])} kWh')
             + _kpi(tt("vs design", "vs diseño"), _pct(_ratio(x["kwh"], x["design_kwh"])), "-" if x["design_kwh"] is None else f'{UI.num(x["design_kwh"])} kWh')
             + _kpi(tt("Insolation", "Insolación"), f'{x["irr_kwh_m2"]:.1f}<small>kWh/m²</small>', "")
             + _kpi("PR", _pct(x["pr"]), "") + _kpi(tt("MSA availability", "Disponibilidad MSA"), _pct(x.get("msa_availability"), 2), "")
             + _kpi(tt("Data completeness", "Completitud de datos"), _pct(x["completeness"], 0), "") + "</div></div>")
    chart = UI.bar_chart([(dd, a, e) for dd, a, e, _i in x["daily"]], w=1300, h=260)
    body = (_reports_head(tt, f'{UI.e(x["name"])} · {month}', f'<a href="/reports/{month}/">‹ {tt("Portfolio report", "Reporte del portafolio")}</a>')
            + tiles + f'<div class="card" style="margin-top:16px"><h2>{tt("Energy per day", "Energía por día")}</h2>{chart}</div>' + _report_tables(d, tt, rg, x["code"]))
    return page(f'{x["name"]} {month}', body, "reports", sample=x["source"] == "sample")


@app.get("/reports/<month>/report.xlsx")
def report_xlsx(month):
    d, frozen = _report_or_404(month)
    names = {s.code: s.name for s in reg().sites}
    data = XL.workbook(MR.workbook_sheets(d, names))
    S.audit(db(), g.user["username"], ip(), "monthly_report_export", month, "published" if frozen else "draft")
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=prologis_om_report_{month}{'' if frozen else '_DRAFT'}.xlsx"})


@app.post("/reports/<month>/publish")
def report_publish(month):
    need("alarms_work")
    c = db()
    try:
        MR.month_bounds(month)
        d = MR.build(c, reg(), month, today_mx())
        MR.publish(c, month, d, request.form.get("notes", ""), g.user["username"], today_mx(), ip())
    except ValueError as ex:
        return report_page(month, f"Not published: {ex}", True)
    return report_page(month, "Published. / Publicado.")


# ---- HSE register
@app.get("/reports/hse")
def hse_page(msg: str = "", err: bool = False):
    tt = t()
    rg = reg()
    c = db()
    hk = dict((k, tt(en, es)) for k, en, es in MR.HSE_KINDS)
    tr = ""
    for e in c.execute("SELECT * FROM hse_events ORDER BY occurred_utc DESC LIMIT 200"):
        ot = MR.hse_on_time(e)
        pill = "" if ot is None else (f'<span class="pill s-ok">{tt("reported within 24 h", "reportado en 24 h")}</span>' if ot and e["reported_utc"]
                                      else (f'<span class="pill s-warn">{tt("to report within 24 h", "reportar en 24 h")}</span>' if ot else f'<span class="pill s-bad">{tt("not reported within 24 h", "no reportado en 24 h")}</span>'))
        tr += (f'<tr><td class="small nw">{mx_time(e["occurred_utc"])}</td><td>{UI.e(_site_name(rg, e["site_code"]) if e["site_code"] else "-")}</td>'
               f'<td>{UI.e(hk.get(e["kind"], e["kind"]))}<div class="small">{UI.e(e["description"])}</div><div class="small muted">{UI.e(e["actions"])}</div></td>'
               f'<td>{pill}<div class="small muted">{mx_time(e["reported_utc"]) if e["reported_utc"] else ""}{" · Safety Mojo " + UI.e(e["mojo_ref"]) if e["mojo_ref"] else ""}</div></td></tr>')
    tr = tr or f'<tr><td class="muted" colspan="4">{tt("No events recorded.", "Sin eventos registrados.")}</td></tr>'
    form = ""
    if S.can(g.user["role"], "alarms_work"):
        sopts = "".join(f'<option value="{s.code}">{UI.e(s.name)}</option>' for s in rg.sites)
        kopts = "".join(f'<option value="{k}">{UI.e(tt(en, es))}</option>' for k, en, es in MR.HSE_KINDS)
        form = (f'<div class="card form" style="margin-top:16px"><h2>{tt("Record an event", "Registrar un evento")}</h2><form method="post" action="/reports/hse">{csrf_field()}'
                f'<div class="row"><div><label>{tt("Site", "Sitio")}</label><select name="site"><option value="">{tt("Not at a site", "Fuera de sitio")}</option>{sopts}</select></div>'
                f'<div><label>{tt("Kind", "Tipo")}</label><select name="kind">{kopts}</select></div></div>'
                f'<div class="row"><div><label>{tt("Occurred (YYYY-MM-DD HH:MM, Mexico City)", "Ocurrió (AAAA-MM-DD HH:MM, Cd. de México)")}</label><input name="occurred" required></div>'
                f'<div><label>{tt("Reported to Prologis (empty if not yet)", "Reportado a Prologis (vacío si aún no)")}</label><input name="reported"></div></div>'
                f'<label>{tt("What happened", "Qué pasó")}</label><textarea name="description" required></textarea><label>{tt("Actions taken", "Acciones tomadas")}</label><textarea name="actions"></textarea>'
                f'<label>{tt("Safety Mojo reference", "Referencia Safety Mojo")}</label><input name="mojo"><div style="margin-top:10px"><button class="btn">{tt("Record", "Registrar")}</button></div></form></div>')
    body = (_reports_head(tt, tt("HSE register", "Registro SSMA"),
                          tt("Every incident, first aid case and near miss is reported in writing to Prologis within 24 hours (MSA Exhibit F) and goes into the monthly report.",
                             "Todo incidente, caso de primeros auxilios y casi accidente se reporta por escrito a Prologis en 24 horas (MSA Anexo F) y entra al reporte mensual."))
            + flash(msg, err) + f'<div class="card" style="margin-top:10px;overflow-x:auto"><table class="t">{tr}</table></div>' + form)
    return page(tt("HSE register", "Registro SSMA"), body, "reports")


@app.post("/reports/hse")
def hse_post():
    need("alarms_work")
    f = request.form
    try:
        MR.add_hse(db(), (f.get("occurred") or "").strip(), (f.get("site") or "").strip(), f.get("kind", ""), f.get("description", ""),
                   f.get("actions", ""), g.user["username"], (f.get("reported") or "").strip(), f.get("mojo", ""), ip(),
                   [s.code for s in reg().sites], now_mx())
    except ValueError as ex:
        return hse_page(f"Not recorded: {ex}", True)
    return hse_page("Recorded. / Registrado.")


# ---- design yield
@app.get("/reports/design")
def design_page(msg: str = "", err: bool = False, errors: Optional[List[str]] = None):
    tt = t()
    rg = reg()
    c = db()
    dm = MR.design_map(c)
    head = "".join(f"<th class='n'>{i}</th>" for i in range(1, 13))
    tr = ""
    for s in rg.sites:
        vals = [dm.get((s.code, i)) for i in range(1, 13)]
        tot = sum(v for v in vals if v is not None)
        tr += (f'<tr><td><b>{UI.e(s.name)}</b><div class="small muted">{s.code} · {UI.num(s.kwp, 0)} kWp</div></td>'
               + "".join(f'<td class="n small">{"-" if v is None else UI.num(v)}</td>' for v in vals)
               + f'<td class="n"><b>{UI.num(tot) if tot else "-"}</b>{"" if not tot else f"<div class=small>{tot / s.kwp:,.0f} kWh/kWp</div>"}</td></tr>')
    form = ""
    if S.can(g.user["role"], "alarms_work"):
        errs = "".join(f"<li>{UI.e(x)}</li>" for x in (errors or [])[:100])
        form = (f'<div class="card form" style="margin-top:16px"><h2>{tt("Load from CSV", "Cargar desde CSV")}</h2>{"<ul class=small>" + errs + "</ul>" if errs else ""}'
                f'<p class="small">{tt("Columns", "Columnas")}: <code>site,m1,...,m12</code> (kWh). {tt("All or nothing; empty cells mean not provided; a value replaces the stored one.", "Todo o nada; celdas vacías = sin dato; un valor reemplaza al guardado.")} '
                f'<a href="/reports/design_template.csv">{tt("Template", "Plantilla")}</a></p>'
                f'<form method="post" action="/reports/design" enctype="multipart/form-data">{csrf_field()}<label>{tt("Source (study name and version)", "Fuente (estudio y versión)")}</label><input name="source" required>'
                f'<input type="file" name="file" accept=".csv" required style="margin-top:8px"><div style="margin-top:10px"><button class="btn">{tt("Check and load", "Revisar y cargar")}</button></div></form></div>')
    body = (_reports_head(tt, tt("Design yield", "Producción de diseño"),
                          tt("Estimated monthly energy per site from the PVsyst / Helioscope studies - the 'estimated' line of the monthly report. Sites without it show 'not provided'.",
                             "Energía mensual estimada por sitio de los estudios PVsyst / Helioscope - la línea 'estimada' del reporte mensual. Los sitios sin dato muestran 'sin dato'."))
            + flash(msg, err) + f'<div class="card" style="margin-top:10px;overflow-x:auto"><table class="t"><tr><th>{tt("Site", "Sitio")}</th>{head}<th class="n">{tt("Year", "Año")}</th></tr>{tr}</table></div>' + form)
    return page(tt("Design yield", "Producción de diseño"), body, "reports")


@app.get("/reports/design_template.csv")
def design_template():
    need("alarms_work")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["site"] + [f"m{i}" for i in range(1, 13)])
    for s in reg().sites:
        w.writerow([s.code] + [""] * 12)
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=design_yield_template.csv"})


@app.post("/reports/design")
def design_post():
    need("alarms_work")
    f = request.files.get("file")
    source = (request.form.get("source") or "").strip()
    if not f or not f.filename or not source:
        return design_page("Choose a file and name the source. / Elija archivo y fuente.", True)
    raw = f.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        abort(413)
    rows, errs = MR.parse_design(raw.decode("utf-8-sig", errors="replace"), [s.code for s in reg().sites])
    if errs:
        return design_page(f"Nothing loaded: {len(errs)} problem(s). / No se cargó nada.", True, errs)
    n = MR.save_design(db(), rows, source, g.user["username"], ip())
    return design_page(f"{n} values loaded. / {n} valores cargados.")


@app.post("/tickets/<number>/eta")
def ticket_eta(number):
    need("ticket_work")
    tk = _ticket(number)
    v = (request.form.get("eta") or "").strip()
    if v:
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            abort(400)
    c = db()
    c.execute("UPDATE tickets SET eta_date=? WHERE id=?", (v, tk["id"]))
    c.commit()
    S.add_event(c, tk["id"], g.user["username"], "comment", f"Estimated return to service: {v or 'cleared'}")
    S.audit(c, g.user["username"], ip(), "ticket_eta", number, v)
    return redirect(f"/tickets/{number}/")


# ------------------------------------------------------------------ MSA availability (v323)
# The availability formula of MSA Schedule A on the incident register, the
# exclusion register (claimed by ARGIA, accepted or rejected by Prologis),
# the annual analysis with liquidated damages and the bonus.
EXCL_PILL = {"claimed": "s-warn", "accepted": "s-ok", "rejected": "s-bad"}


def _month_window(m: str) -> tuple:
    a, b = MR.month_bounds(m)
    last = min(b, today_mx() - dt.timedelta(days=1))
    return dt.datetime.combine(a, dt.time()), dt.datetime.combine(max(last, a - dt.timedelta(days=1)) + dt.timedelta(days=1), dt.time())


def _lt(ts_utc: str) -> str:
    return "-" if not ts_utc else f"{AV.to_local(ts_utc):%d %b %Y %H:%M}"


@app.get("/availability/")
def availability_page(msg: str = "", err: bool = False):
    tt = t()
    rg = reg()
    c = db()
    m = request.values.get("m") or f"{today_mx():%Y-%m}"       # forms post the month back, so the page stays on it
    try:
        start, end = _month_window(m)
    except ValueError:
        abort(400)
    g_ = float(AL.setting(c, "availability_guarantee") or 0.98)
    rows, acc, cl = "", [], []
    for s in rg.operating:
        ra = AV.site_result(c, s, start, end, ("accepted",))
        rc_ = AV.site_result(c, s, start, end, ("accepted", "claimed"))
        acc.append(ra)
        cl.append(rc_)
        a = ra.availability
        low = a is not None and a + 1e-9 < g_
        rows += (f'<tr><td><b>{UI.e(s.name)}</b><div class="small muted">{s.code}</div></td><td class="n">{ra.h_ttp:,.1f}</td><td class="n">{UI.num(ra.kw_np, 1)}</td>'
                 f'<td class="n">{UI.num(ra.gross_kwh_eq, 1)}</td><td class="n">{UI.num(ra.excluded_kwh_eq, 1)}</td>'
                 f'<td class="n"><b style="color:{"var(--red, #b2443c)" if low else "inherit"}">{_pct(a, 2)}</b></td><td class="n">{_pct(rc_.availability, 2)}</td></tr>')
    pa, pc = AV.portfolio(acc), AV.portfolio(cl)
    incs = c.execute("SELECT * FROM unavail_incidents WHERE start_utc<? AND (end_utc='' OR end_utc>?) ORDER BY start_utc DESC",
                     (AV.to_utc(end), AV.to_utc(start))).fetchall()
    work = S.can(g.user["role"], "alarms_work")
    ir = ""
    for r in incs:
        tk = _ticket_no(c, r["ticket_id"])
        close = ""
        if work and not r["end_utc"]:
            close = (f'<form method="post" action="/availability/incident/{r["id"]}/close" style="display:flex;gap:4px;margin-top:4px">{csrf_field()}<input type="hidden" name="m" value="{UI.e(m)}">'
                     f'<input name="end" placeholder="YYYY-MM-DD HH:MM" style="width:150px"><button class="btn sm ghost">{tt("Close", "Cerrar")}</button></form>')
        ir += (f'<tr><td class="small muted">#{r["id"]}{SAMPLE_PILL if r["sample"] else ""}</td><td>{UI.e(_site_name(rg, r["site_code"]))}<div class="small">{UI.e(r["component"])}</div></td>'
               f'<td class="n">{UI.num(r["dc_kw"], 1)}</td><td class="small">{_lt(r["start_utc"])}<br>{_lt(r["end_utc"]) if r["end_utc"] else tt("open", "abierto")}</td>'
               f'<td class="small">{UI.e(r["cause"])}{f" <a href=/tickets/{tk}/>{tk}</a>" if tk else ""}{close}</td></tr>')
    ir = ir or f'<tr><td class="muted" colspan="5">{tt("No unavailability recorded in this period.", "Sin indisponibilidad registrada en este periodo.")}</td></tr>'
    exs = c.execute("SELECT * FROM exclusions WHERE start_utc<? AND end_utc>? ORDER BY id DESC", (AV.to_utc(end), AV.to_utc(start))).fetchall()
    cat = dict((k, tt(en, es)) for k, en, es in AV.CATEGORIES)
    decide = S.can(g.user["role"], "exclusion_decide")
    er = ""
    for x in exs:
        st = {"claimed": tt("Claimed by ARGIA, awaiting Prologis", "Reclamada por ARGIA, esperando a Prologis"), "accepted": tt("Accepted", "Aceptada"),
              "rejected": tt("Rejected", "Rechazada")}[x["status"]]
        act = ""
        if decide and x["status"] == "claimed":
            act = (f'<form method="post" action="/availability/exclusion/{x["id"]}/decide" style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px">{csrf_field()}<input type="hidden" name="m" value="{UI.e(m)}">'
                   f'<input name="note" placeholder="{tt("comment (required to reject)", "comentario (obligatorio para rechazar)")}" style="width:200px">'
                   f'<button class="btn sm" name="ok" value="1">{tt("Accept", "Aceptar")}</button><button class="btn sm danger" name="ok" value="0">{tt("Reject", "Rechazar")}</button></form>')
        dec = f'<div class="small muted">{UI.e(x["decided_by"])} {_lt(x["decided_utc"])} {UI.e(x["decision_note"])}</div>' if x["decided_by"] else ""
        er += (f'<tr><td class="small muted">#{x["id"]}</td><td>{UI.e(_site_name(rg, x["site_code"]) if x["site_code"] else tt("All sites", "Todos los sitios"))}'
               f'{"<div class=small>" + tt("incident", "incidente") + " #" + str(x["incident_id"]) + "</div>" if x["incident_id"] else ""}</td>'
               f'<td>{UI.e(cat.get(x["category"], x["category"]))}<div class="small">{UI.e(x["reason"])}</div></td>'
               f'<td class="small">{_lt(x["start_utc"])}<br>{_lt(x["end_utc"])}</td><td><span class="pill {EXCL_PILL[x["status"]]}">{UI.e(st)}</span>{dec}{act}</td></tr>')
    er = er or f'<tr><td class="muted" colspan="5">{tt("No exclusions in this period.", "Sin exclusiones en este periodo.")}</td></tr>'
    forms = ""
    if work:
        sopts = "".join(f'<option value="{s.code}">{UI.e(s.name)}</option>' for s in rg.operating)
        copts = "".join(f'<option value="{k}">{UI.e(tt(en, es))}</option>' for k, en, es in AV.CATEGORIES)
        sug = ""
        for x in AV.suggestions(c):
            sug += (f'<li class="small">{UI.e(_site_name(rg, x["site"]))} · {tt("incident", "incidente")} #{x["incident"]} · {UI.e(cat[x["category"]])}: {_lt(x["start_utc"])} - {_lt(x["end_utc"])} · {UI.e(x["reason"])}'
                    f'<form method="post" action="/availability/exclusion" style="display:inline">{csrf_field()}<input type="hidden" name="m" value="{UI.e(m)}"><input type="hidden" name="incident" value="{x["incident"]}">'
                    f'<input type="hidden" name="category" value="{x["category"]}"><input type="hidden" name="start" value="{AV.to_local(x["start_utc"]):%Y-%m-%d %H:%M}">'
                    f'<input type="hidden" name="end" value="{AV.to_local(x["end_utc"]):%Y-%m-%d %H:%M}"><input type="hidden" name="reason" value="{UI.e(x["reason"])}">'
                    f' <button class="btn sm ghost">{tt("Claim", "Reclamar")}</button></form></li>')
        forms = (f'<div class="grid g2e" style="margin-top:16px"><div class="card"><h2>{tt("Record unavailability", "Registrar indisponibilidad")}</h2>'
                 f'<form method="post" action="/availability/incident">{csrf_field()}<input type="hidden" name="m" value="{UI.e(m)}"><div class="row"><div><label>{tt("Site", "Sitio")}</label><select name="site">{sopts}</select></div>'
                 f'<div><label>{tt("Equipment # (DC kW from the register)", "Equipo # (DC kW del registro)")}</label><input name="equipment" inputmode="numeric"></div></div>'
                 f'<div class="row"><div><label>{tt("Component", "Componente")}</label><input name="component" placeholder="INV-02"></div><div><label>{tt("DC kW behind it", "DC kW detrás")}</label><input name="dc_kw" inputmode="decimal"></div></div>'
                 f'<div class="row"><div><label>{tt("From (YYYY-MM-DD HH:MM)", "Desde (AAAA-MM-DD HH:MM)")}</label><input name="start" required></div><div><label>{tt("To (empty = still down)", "Hasta (vacío = sigue)")}</label><input name="end"></div></div>'
                 f'<div class="row"><div><label>{tt("Ticket (e.g. PL-0001)", "Ticket (ej. PL-0001)")}</label><input name="ticket"></div><div><label>{tt("Cause (equipment fault)", "Causa (falla de equipo)")}</label><input name="cause"></div></div>'
                 f'<div style="margin-top:10px"><button class="btn">{tt("Record", "Registrar")}</button></div></form></div>'
                 f'<div class="card"><h2>{tt("Claim an exclusion", "Reclamar una exclusión")}</h2><form method="post" action="/availability/exclusion">{csrf_field()}<input type="hidden" name="m" value="{UI.e(m)}">'
                 f'<div class="row"><div><label>{tt("Site", "Sitio")}</label><select name="site"><option value="">{tt("All sites", "Todos los sitios")}</option>{sopts}</select></div>'
                 f'<div><label>{tt("Incident # (optional)", "Incidente # (opcional)")}</label><input name="incident" inputmode="numeric"></div></div>'
                 f'<label>{tt("Category", "Categoría")}</label><select name="category">{copts}</select>'
                 f'<div class="row"><div><label>{tt("From", "Desde")}</label><input name="start" required></div><div><label>{tt("To", "Hasta")}</label><input name="end" required></div></div>'
                 f'<label>{tt("Reason and evidence", "Motivo y evidencia")}</label><textarea name="reason" required></textarea><div style="margin-top:10px"><button class="btn">{tt("Claim", "Reclamar")}</button></div></form>'
                 + (f'<h2 style="margin-top:14px">{tt("Proven by the data", "Probadas por los datos")}</h2><ul>{sug}</ul>' if sug else "") + "</div></div>")
    months = "".join(f'<option {"selected" if x == m else ""}>{x}</option>' for x in _months(today_mx()))
    g_txt = tt("guarantee", "garantía") + f" {g_ * 100:g}%"
    body = (_reports_head(tt, tt("MSA availability", "Disponibilidad MSA"),
                          tt("A = 1 - (1 / (H_ttp x kW_np)) x SUM(H_un x kW_un): hours above 150 W/m², weighted by the DC nameplate behind each unavailable component. Exclusions claimed by ARGIA count only once Prologis accepts them; the second column shows the result if every claimed exclusion were accepted.",
                             "A = 1 - (1 / (H_ttp x kW_np)) x SUMA(H_un x kW_un): horas sobre 150 W/m², ponderadas por el DC nominal de cada componente no disponible. Las exclusiones que reclama ARGIA cuentan solo cuando Prologis las acepta; la segunda columna muestra el resultado si se aceptaran todas."))
            + flash(msg, err)
            + f'<form method="get" style="margin:10px 0"><select name="m" style="width:auto" onchange="this.form.submit()">{months}</select></form>'
            + f'<div class="hero tiles"><div class="kpis">{_kpi(tt("Portfolio, accepted exclusions", "Portafolio, exclusiones aceptadas"), _pct(pa, 3), g_txt)}'
            f'{_kpi(tt("Incl. exclusions awaiting Prologis", "Incl. exclusiones esperando a Prologis"), _pct(pc, 3), "")}'
            f'{_kpi(tt("Incidents in the period", "Incidentes en el periodo"), str(len(incs)), "")}{_kpi(tt("Exclusions awaiting Prologis", "Exclusiones esperando a Prologis"), str(sum(1 for x in exs if x["status"] == "claimed")), "")}</div></div>'
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><table class="t"><tr><th>{tt("Site", "Sitio")}</th><th class="n">H_ttp</th><th class="n">kW_np</th>'
            f'<th class="n">{tt("Unavailable", "No disponible")} kWh-eq</th><th class="n">{tt("Excluded", "Excluido")} kWh-eq</th><th class="n">{tt("Availability", "Disponibilidad")}</th>'
            f'<th class="n">{tt("Incl. claimed", "Incl. reclamadas")}</th></tr>{rows}</table>'
            f'<p class="small muted">{tt("Irradiance and kW_np", "Irradiancia y kW_np")}: {tt("SAMPLE irradiance until the site sensors are connected; kW_np = the site DC kWp.", "irradiancia de MUESTRA hasta conectar los sensores; kW_np = kWp DC del sitio.")}</p></div>'
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Unavailability incidents", "Incidentes de indisponibilidad")}</h2><table class="t"><tr><th>#</th><th>{tt("Site / component", "Sitio / componente")}</th>'
            f'<th class="n">DC kW</th><th>{tt("From / to", "Desde / hasta")}</th><th>{tt("Cause", "Causa")}</th></tr>{ir}</table></div>'
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Exclusion register", "Registro de exclusiones")}</h2><table class="t"><tr><th>#</th><th>{tt("Where", "Dónde")}</th>'
            f'<th>{tt("Category / reason", "Categoría / motivo")}</th><th>{tt("From / to", "Desde / hasta")}</th><th>{tt("Status", "Estado")}</th></tr>{er}</table></div>' + forms)
    return page(tt("Availability", "Disponibilidad"), body, "reports", sample=True)


def _float_or_none(v):
    v = (v or "").strip()
    return float(v.replace(",", "")) if v else None


@app.post("/availability/incident")
def availability_incident():
    need("alarms_work")
    c = db()
    f = request.form
    rg = reg()
    s = rg.site(f.get("site", ""))
    try:
        eq = int(f["equipment"]) if (f.get("equipment") or "").strip().isdigit() else None
        AV.add_incident(c, f.get("site", ""), f.get("component", ""), _float_or_none(f.get("dc_kw")), f.get("start", ""), f.get("end", ""),
                        f.get("cause", ""), g.user["username"], eq, _ticket_id(c, f.get("ticket", "")), ip(),
                        [x.code for x in rg.operating], s.kwp if s else 0.0)
    except ValueError as ex:
        return availability_page(f"Not recorded: {ex}", True)
    return availability_page("Recorded. / Registrado.")


@app.post("/availability/incident/<int:iid>/close")
def availability_close(iid):
    need("alarms_work")
    try:
        AV.close_incident(db(), iid, request.form.get("end", ""), g.user["username"], ip())
    except ValueError as ex:
        return availability_page(f"Not closed: {ex}", True)
    return availability_page("Closed. / Cerrado.")


@app.post("/availability/exclusion")
def availability_exclusion():
    need("alarms_work")
    f = request.form
    try:
        inc = int(f["incident"]) if (f.get("incident") or "").strip().isdigit() else None
        AV.claim_exclusion(db(), f.get("site", ""), f.get("category", ""), f.get("start", ""), f.get("end", ""), f.get("reason", ""),
                           g.user["username"], inc, ip(), [x.code for x in reg().operating])
    except ValueError as ex:
        return availability_page(f"Not claimed: {ex}", True)
    return availability_page("Claimed - waiting for Prologis. / Reclamada - esperando a Prologis.")


@app.post("/availability/exclusion/<int:eid>/decide")
def availability_decide(eid):
    need("exclusion_decide")
    try:
        AV.decide_exclusion(db(), eid, request.form.get("ok") == "1", g.user["username"], request.form.get("note", ""), ip())
    except ValueError as ex:
        return availability_page(f"Not saved: {ex}", True)
    return availability_page("Saved. / Guardado.")


@app.get("/availability/annual")
def availability_annual(msg: str = "", err: bool = False):
    tt = t()
    rg = reg()
    c = db()
    today = today_mx()
    g_ = float(AL.setting(c, "availability_guarantee") or 0.98)
    tm = AV.terms(c)
    rows = ""
    op = rg.operating
    for i, s in enumerate(op):
        tr_ = tm.get(s.code)
        if not tr_:
            rows += f'<tr><td><b>{UI.e(s.name)}</b><div class="small muted">{s.code}</div></td><td colspan="7" class="muted small">{tt("Contract terms not set (effective date, kWh rate, annual fee).", "Términos del contrato sin fijar (fecha efectiva, tarifa kWh, cuota anual).")}</td></tr>'
            continue
        eff = dt.date.fromisoformat(tr_["effective_date"])
        for a, b in [y for y in AV.contract_years(eff, today) if AV.analysis_due(y[1]) >= today]:   # running, or analysis not yet due
            upto = min(b, today)
            sa, sb = dt.datetime.combine(a, dt.time()), dt.datetime.combine(upto, dt.time())
            ra = AV.site_result(c, s, sa, sb, ("accepted",))
            rc_ = AV.site_result(c, s, sa, sb, ("accepted", "claimed"))
            days = (upto - a).days
            me = sum(d.kwh for d in M.history(s, upto - dt.timedelta(days=1), days, i, len(op))) if days > 0 else 0.0
            av = ra.availability
            ld = bo = (0.0, 0.0, False)
            if av is not None and tr_["kwh_rate"]:
                ld = AV.liquidated_damages(av, g_, me, tr_["kwh_rate"], tr_["annual_fee"])
                bo = AV.bonus(av, me, tr_["kwh_rate"], tr_["annual_fee"])
            done = b <= today
            due = AV.analysis_due(b)
            rate_txt = "-" if not tr_["kwh_rate"] else f'{tr_["kwh_rate"]:.2f}'
            rows += (f'<tr><td><b>{UI.e(s.name)}</b><div class="small muted">{s.code}</div></td><td class="small nw">{a:%d %b %Y} - {b - dt.timedelta(days=1):%d %b %Y}'
                     f'<div class="muted">{tt("complete", "completo") if done else tt("running", "en curso") + f" ({days} d)"}</div></td>'
                     f'<td class="n"><b>{_pct(av, 2)}</b><div class="small muted">{_pct(rc_.availability, 2)}</div></td><td class="n">{UI.num(me)}</td>'
                     f'<td class="n">{rate_txt}</td>'
                     f'<td class="n">{_mxn(ld[0]) if ld[0] else "-"}{" <span class=small>(" + tt("capped", "tope") + ")</span>" if ld[2] else ""}</td>'
                     f'<td class="n">{_mxn(bo[0]) if bo[0] else "-"}{" <span class=small>(" + tt("capped", "tope") + ")</span>" if bo[2] else ""}</td>'
                     f'<td class="small nw">{due:%d %b %Y}</td></tr>')
    form = ""
    if S.can(g.user["role"], "contract_edit"):
        sopts = "".join(f'<option value="{s.code}">{UI.e(s.name)}</option>' for s in op)
        form = (f'<div class="card form" style="margin-top:16px"><h2>{tt("Contract terms per site", "Términos del contrato por sitio")}</h2>'
                f'<form method="post" action="/availability/terms">{csrf_field()}<div class="row"><div><label>{tt("Site", "Sitio")}</label><select name="site">{sopts}</select></div>'
                f'<div><label>{tt("Effective date (YYYY-MM-DD)", "Fecha efectiva (AAAA-MM-DD)")}</label><input name="effective" required></div></div>'
                f'<div class="row"><div><label>{tt("kWh rate for the availability guaranty (MXN/kWh, Addendum A)", "Tarifa kWh de la garantía de disponibilidad (MXN/kWh, Adenda A)")}</label><input name="rate" inputmode="decimal"></div>'
                f'<div><label>{tt("Annual fees for the site (MXN, for the caps)", "Cuotas anuales del sitio (MXN, para los topes)")}</label><input name="fee" inputmode="decimal"></div></div>'
                f'<div style="margin-top:10px"><button class="btn">{tt("Save", "Guardar")}</button></div></form></div>')
    body = (_reports_head(tt, tt("Annual availability analysis", "Análisis anual de disponibilidad"),
                          tt(f"Per contract year from each site's effective date; due by the end of the calendar quarter after each anniversary. Liquidated damages when below the {g_ * 100:g}% guarantee: kWh rate x (energy / (1 - D) - energy), capped at 20% of the annual fees; bonus above 98%: half of the same formula, capped at 10%. Amounts are indicative until the MSA is signed.",
                             f"Por año de contrato desde la fecha efectiva de cada sitio; se entrega al cierre del trimestre siguiente a cada aniversario. Penalización bajo la garantía de {g_ * 100:g}%: tarifa kWh x (energía / (1 - D) - energía), tope 20% de las cuotas anuales; bono sobre 98%: la mitad de la misma fórmula, tope 10%. Montos indicativos hasta firmar el MSA."))
            + flash(msg, err)
            + f'<div class="card" style="margin-top:10px;overflow-x:auto"><table class="t"><tr><th>{tt("Site", "Sitio")}</th><th>{tt("Contract year", "Año de contrato")}</th>'
            f'<th class="n">{tt("Availability (incl. claimed)", "Disponibilidad (incl. reclamadas)")}</th><th class="n">{tt("Measured kWh", "kWh medidos")}</th><th class="n">MXN/kWh</th>'
            f'<th class="n">{tt("Liquidated damages", "Penalización")}</th><th class="n">{tt("Bonus", "Bono")}</th><th>{tt("Analysis due", "Análisis vence")}</th></tr>{rows}</table></div>' + form)
    return page(tt("Annual analysis", "Análisis anual"), body, "reports", sample=True)


@app.post("/availability/terms")
def availability_terms():
    need("contract_edit")
    f = request.form
    try:
        AV.save_terms(db(), (f.get("site") or "").upper(), (f.get("effective") or "").strip(), _float_or_none(f.get("rate")),
                      _float_or_none(f.get("fee")), g.user["username"], ip(), [x.code for x in reg().operating])
    except ValueError as ex:
        return availability_annual(f"Not saved: {ex}", True)
    return availability_annual("Saved. / Guardado.")


# ------------------------------------------------------------------ projects
@app.get("/projects/")
def projects():
    tt = t()
    rg = reg()
    c = db()
    stm = S.project_state(c)
    cards = ""
    edit = S.can(g.user["role"], "project_edit")
    for p in rg.projects:
        o = stm.get(p.id)
        stage = (o["stage"] if o else p.stage) or p.stage
        nxt, nd, notes = (o["next"], o["next_date"], o["notes"]) if o else (p.next, p.next_date, p.notes)
        idx = R.STAGES.index(stage) if stage in R.STAGES else 0
        steps = "".join(f'<div style="flex:1;text-align:center"><div style="height:8px;border-radius:4px;background:{UI.TEAL if j < idx else (UI.LIME if j == idx else "#e4ebea")}"></div>'
                        f'<div class="small pstep" style="margin-top:4px;color:{"var(--deep)" if j == idx else "var(--muted)"};font-weight:{800 if j == idx else 500}">{UI.e(tt(st, R.STAGES_ES[st]))}</div></div>'
                        for j, st in enumerate(R.STAGES))
        form = ""
        if edit:
            so = "".join(f'<option {"selected" if x == stage else ""}>{x}</option>' for x in R.STAGES)
            form = (f'<details style="margin-top:10px"><summary class="small">{tt("Update", "Actualizar")}</summary><form method="post" action="/projects/{UI.e(p.id)}">{csrf_field()}'
                    f'<div class="row"><div><label>{tt("Stage", "Etapa")}</label><select name="stage">{so}</select></div><div><label>{tt("Next milestone date", "Fecha próximo hito")}</label><input name="next_date" value="{UI.e(nd)}"></div></div>'
                    f'<label>{tt("Next milestone", "Próximo hito")}</label><input name="next" value="{UI.e(nxt)}"><label>{tt("Notes", "Notas")}</label><textarea name="notes">{UI.e(notes)}</textarea>'
                    f'<div style="margin-top:8px"><button class="btn sm">{tt("Save", "Guardar")}</button></div></form></details>')
        kwp_txt = (" · " + UI.num(p.kwp, 0) + " kWp") if p.kwp else ""
        notes_html = ('<p class="small" style="margin:10px 0 0">' + UI.e(notes) + '</p>') if notes else ""
        cards += f"""<div class="card"><div style="display:flex;gap:10px;align-items:start;flex-wrap:wrap"><div><div class="kick">{UI.e(p.kind)} · {UI.e(p.city)}</div>
<h2 style="margin:2px 0 0;font-size:18px">{UI.e(p.name)}</h2><div class="small muted">{UI.e(p.id)}{kwp_txt}</div></div>
<div style="margin-left:auto;text-align:right"><div class="small muted">{tt("Next", "Siguiente")}</div><b>{UI.e(nxt or "-")}</b><div class="small">{UI.e(nd)}</div></div></div>
<div style="display:flex;gap:4px;margin-top:12px">{steps}</div>{notes_html}{form}</div>"""
    body = (f'<div class="kick">{tt("Constructions & onboarding", "Construcciones y onboarding")}</div><h1 class="pt">{tt("Projects", "Proyectos")}</h1>'
            f'<p class="muted small">{tt("EPC projects ARGIA engineers for Prologis, and the sites that join O&M at their Permission to Operate.", "Proyectos EPC que ARGIA diseña para Prologis y los sitios que entran a O&M con su permiso de operación.")}</p>'
            f'<div class="grid g2e">{cards}</div>')
    return page(tt("Projects", "Proyectos"), body, "projects")


@app.post("/projects/<pid>")
def project_save(pid):
    need("project_edit")
    rg = reg()
    if not any(p.id == pid for p in rg.projects):
        abort(404)
    stage = request.form.get("stage", "")
    if stage not in R.STAGES:
        abort(400)
    S.save_project(db(), pid, stage, request.form.get("next", ""), request.form.get("next_date", ""),
                   request.form.get("notes", ""), g.user["username"], ip())
    return redirect("/projects/")


# ------------------------------------------------------------------ documents
def scan_pill(d, tt) -> str:
    st = d["scan_status"]
    if st == "clean":
        return ""
    lbl = {"pending": tt("being scanned", "en análisis"), "infected": tt("quarantined", "en cuarentena"), "error": tt("scan failed", "análisis falló")}.get(st, st)
    return f' <span class="pill {"s-warn" if st == "pending" else "s-bad"}">{UI.e(lbl)}</span>'


@app.get("/docs/")
def docs():
    tt = t()
    rg = reg()
    c = db()
    site = (request.args.get("site") or "").upper()
    q = "SELECT * FROM documents WHERE deleted=0" + (" AND site_code=?" if site else "") + " ORDER BY folder, name"
    rows = c.execute(q, (site,) if site else ()).fetchall()
    by: Dict[str, List] = {}
    for d in rows:
        by.setdefault(d["folder"], []).append(d)
    folders = ""
    for key, en, es in S.DOC_FOLDERS + [("ticket", "Ticket attachments", "Adjuntos de tickets")]:
        items = by.get(key, [])
        lis = "".join(f'<tr><td><a href="/docs/{d["id"]}/download">{UI.e(d["name"])}</a>{scan_pill(d, tt)}<div class="small muted">{UI.e(d["site_code"])} · {d["size"] / 1024:,.0f} KB · {UI.e(d["uploaded_by"])} · {mx_time(d["uploaded_utc"])}</div></td></tr>' for d in items)
        empty_row = '<tr><td class="muted">' + tt("Empty", "Vacía") + '</td></tr>'
        folders += f'<div class="card"><h2>📁 {UI.e(tt(en, es))}<span class="r muted">{len(items)}</span></h2><table class="t">{lis or empty_row}</table></div>'
    sopts = "".join(f'<option value="{s.code}" {"selected" if s.code == site else ""}>{UI.e(s.name)}</option>' for s in rg.sites)
    up = ""
    if S.can(g.user["role"], "doc_upload"):
        fo = "".join(f'<option value="{k}">{UI.e(tt(en, es))}</option>' for k, en, es in S.DOC_FOLDERS)
        up = (f'<div class="card" style="margin-top:16px"><h2>{tt("Upload", "Subir")}</h2><form method="post" action="/docs/upload" enctype="multipart/form-data">{csrf_field()}'
              f'<div class="row"><div><label>{tt("Site", "Sitio")}</label><select name="site"><option value="">{tt("Portfolio-wide", "Todo el portafolio")}</option>{sopts}</select></div>'
              f'<div><label>{tt("Folder", "Carpeta")}</label><select name="folder">{fo}</select></div></div><label>{tt("File (max 50 MB)", "Archivo (máx 50 MB)")}</label><input type="file" name="file" required>'
              f'<div style="margin-top:10px"><button class="btn">{tt("Upload", "Subir")}</button></div></form></div>')
    body = (f'<div class="kick">{tt("Documentation", "Documentación")}</div><h1 class="pt">{tt("Document library", "Biblioteca de documentos")}</h1>'
            f'<form method="get" style="max-width:420px;margin:10px 0"><select name="site" onchange="this.form.submit()"><option value="">{tt("All sites", "Todos los sitios")}</option>{sopts}</select></form>'
            f'<p class="small muted">{tt("Every upload and download is recorded in the audit log. Files travel encrypted (TLS) to ARGIA&#39;s Prologis-only file store; executables are refused and every file is scanned for malware before anyone can download it.", "Cada carga y descarga queda en la bitácora. Los archivos viajan cifrados (TLS) al almacén exclusivo de Prologis en ARGIA; los ejecutables se rechazan y cada archivo se analiza contra malware antes de poder descargarse.")}</p>'
            f'<div class="grid g2e">{folders}</div>{up}')
    return page(tt("Documents", "Documentos"), body, "docs")


@app.post("/docs/upload")
def docs_upload():
    need("doc_upload")
    folder = request.form.get("folder", "")
    site = (request.form.get("site") or "").upper()
    if folder not in S.FOLDER_KEYS or (site and not reg().site(site)):
        abort(400)
    _upload(request.files.get("file"), site, folder)
    return redirect("/docs/" + (f"?site={site}" if site else ""))


@app.get("/docs/<int:doc_id>/download")
def docs_download(doc_id):
    c = db()
    d = c.execute("SELECT * FROM documents WHERE id=? AND deleted=0", (doc_id,)).fetchone()
    if not d:
        abort(404)
    if d["scan_status"] != "clean":           # v324: fail closed until the malware scan passed it
        tt = t()
        why = {"pending": tt("This file is waiting for the malware scan (every 5 minutes). Try again shortly.", "Este archivo espera el análisis antimalware (cada 5 minutos). Intente de nuevo en breve."),
               "infected": tt("This file was quarantined: the malware scan found", "Este archivo fue puesto en cuarentena: el análisis encontró") + " " + d["scan_detail"],
               "error": tt("The malware scan could not read this file; ARGIA has been notified.", "El análisis no pudo leer este archivo; ARGIA fue notificado.")}.get(d["scan_status"], "")
        S.audit(c, g.user["username"], ip(), "doc_download_blocked", f"{d['site_code']}/{d['folder']}/{d['name']}", d["scan_status"])
        r = page(tt("Documents", "Documentos"), f'<div class="card form">{flash(why, True)}<a class="btn ghost" href="/docs/">{tt("Back", "Volver")}</a></div>', "docs")
        r.status_code = 409
        return r
    p = S.doc_path(FILES_DIR, d)
    if not os.path.exists(p):
        abort(404)
    S.audit(c, g.user["username"], ip(), "doc_download", f"{d['site_code']}/{d['folder']}/{d['name']}")
    return send_file(p, mimetype=d["mime"] or "application/octet-stream", as_attachment=True, download_name=d["name"])


# ------------------------------------------------------------------ admin
@app.get("/admin/users/")
def users_page(msg: str = "", once: str = ""):
    need("users")
    tt = t()
    c = db()
    rows = ""
    for u in c.execute("SELECT * FROM users ORDER BY org, username"):
        ro = "".join(f'<option value="{r}" {"selected" if r == u["role"] else ""}>{UI.e(tt(*S.ROLE_LABEL[r]))}</option>' for r in S.ROLES)
        mfa_pill = ('<span class="pill s-ok">MFA</span>' if u["totp"]
                    else '<span class="pill s-warn">' + tt("MFA pending", "MFA pendiente") + '</span>')
        dis_pill = (' <span class="pill s-bad">' + tt("Disabled", "Desactivado") + '</span>') if u["disabled"] else ""
        rows += (f'<tr><td><b>{UI.e(u["name"] or u["username"])}</b><div class="small muted">{UI.e(u["username"])} · {UI.e(u["email"])}</div></td><td>{UI.e(u["org"])}</td>'
                 f'<td><form method="post" action="/admin/users/{UI.e(u["username"])}">{csrf_field()}<select name="role" onchange="this.form.submit()">{ro}</select><input type="hidden" name="op" value="role"></form></td>'
                 f'<td>{mfa_pill}'
                 f'{dis_pill}</td><td class="small muted">{mx_time(u["last_login_utc"])}</td>'
                 f'<td><form method="post" action="/admin/users/{UI.e(u["username"])}" style="display:flex;gap:4px;flex-wrap:wrap">{csrf_field()}'
                 f'<button class="btn sm ghost" name="op" value="reset_pw">{tt("New password", "Nueva contraseña")}</button><button class="btn sm ghost" name="op" value="reset_mfa">{tt("Reset MFA", "Reiniciar MFA")}</button>'
                 f'<button class="btn sm {"ghost" if u["disabled"] else "danger"}" name="op" value="{"enable" if u["disabled"] else "disable"}">{tt("Enable" if u["disabled"] else "Disable", "Activar" if u["disabled"] else "Desactivar")}</button></form></td></tr>')
    ro_new = "".join(f'<option value="{r}">{UI.e(tt(*S.ROLE_LABEL[r]))}</option>' for r in S.ROLES)
    once_html = (f'<div class="flash"><b>{tt("One-time password - shown only now. Hand it over by a separate channel; the user must change it and enrol MFA at first sign-in.", "Contraseña de un solo uso - solo se muestra ahora. Entréguela por otro canal; el usuario debe cambiarla y activar MFA al entrar.")}</b><br><span class="once">{UI.e(once)}</span></div>' if once else "")
    perms = "".join(f'<tr><td>{UI.e(p)}</td>' + "".join(f'<td>{"✔" if r in S.PERMS[p] else ""}</td>' for r in S.ROLES) + '</tr>' for p in S.PERMS)
    body = f"""<div class="kick">{tt("Administration", "Administración")}</div><h1 class="pt">{tt("Users & access", "Usuarios y acceso")}</h1>{flash(msg)}{once_html}
<div class="card" style="overflow-x:auto"><table class="t"><tr><th>{tt("User", "Usuario")}</th><th>{tt("Organisation", "Organización")}</th><th>{tt("Role", "Rol")}</th><th>{tt("Security", "Seguridad")}</th><th>{tt("Last sign-in", "Último acceso")}</th><th></th></tr>{rows}</table></div>
<div class="grid g2e"><div class="card"><h2>{tt("Add user", "Agregar usuario")}</h2><form method="post" action="/admin/users/">{csrf_field()}
<div class="row"><div><label>{tt("User name", "Usuario")}</label><input name="username" required></div><div><label>{tt("Full name", "Nombre")}</label><input name="name" required></div></div>
<div class="row"><div><label>Email</label><input name="email" type="email"></div><div><label>{tt("Organisation", "Organización")}</label><select name="org"><option>Prologis</option><option>ARGIA</option></select></div></div>
<label>{tt("Role", "Rol")}</label><select name="role">{ro_new}</select><div style="margin-top:12px"><button class="btn">{tt("Create", "Crear")}</button></div></form></div>
<div class="card"><h2>{tt("What each role can do", "Qué puede hacer cada rol")}</h2><table class="t small"><tr><th></th>{"".join(f"<th>{UI.e(tt(*S.ROLE_LABEL[r]))}</th>" for r in S.ROLES)}</tr>{perms}</table>
<p class="small muted"><a href="/audit/">{tt("Audit log", "Bitácora de auditoría")} ›</a></p></div></div>"""
    return page(tt("Users", "Usuarios"), body, "users")


@app.post("/admin/users/")
def users_create():
    need("users")
    f = request.form
    try:
        pw = S.create_user(db(), f.get("username", ""), f.get("name", ""), f.get("email", ""), f.get("org", "Prologis"),
                           f.get("role", "viewer"), g.user["username"], ip())
    except ValueError as ex:
        return users_page(f"Not created: {ex}")
    return users_page("User created. / Usuario creado.", once=pw)


@app.post("/admin/users/<username>")
def users_edit(username):
    need("users")
    c = db()
    if not S.user(c, username):
        abort(404)
    op = request.form.get("op", "")
    me = g.user["username"]
    if username == me and op in ("disable", "role"):
        return users_page("You cannot change your own role or disable yourself. / No puede cambiar su propio rol ni desactivarse.")
    if op == "role":
        S.set_role(c, username, request.form.get("role", ""), me, ip())
    elif op == "disable":
        S.set_disabled(c, username, True, me, ip())
    elif op == "enable":
        S.set_disabled(c, username, False, me, ip())
    elif op == "reset_mfa":
        S.reset_mfa(c, username, me, ip())
    elif op == "reset_pw":
        return users_page(f"New password for {username}.", once=S.reset_password(c, username, me, ip()))
    else:
        abort(400)
    return users_page("Saved. / Guardado.")


@app.get("/audit/")
def audit_page():
    need("audit")
    tt = t()
    c = db()
    rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 5000").fetchall()
    if request.args.get("csv"):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts_utc", "user", "ip", "action", "target", "detail"])
        for r in rows:
            w.writerow([r["ts_utc"], r["username"], r["ip"], r["action"], r["target"], r["detail"]])
        S.audit(c, g.user["username"], ip(), "audit_export")
        return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=prologis_audit.csv"})
    tr = "".join(f'<tr><td class="small">{mx_time(r["ts_utc"])}</td><td>{UI.e(r["username"])}</td><td class="small muted">{UI.e(r["ip"])}</td><td><b>{UI.e(r["action"])}</b></td><td class="small">{UI.e(r["target"])}</td><td class="small muted">{UI.e(r["detail"])}</td></tr>' for r in rows[:300])
    body = (f'<div style="display:flex;align-items:end;gap:10px"><div><div class="kick">{tt("Security", "Seguridad")}</div><h1 class="pt">{tt("Audit log", "Bitácora de auditoría")}</h1></div>'
            f'<a class="btn ghost" style="margin-left:auto" href="?csv=1">{tt("Download CSV", "Descargar CSV")}</a></div>'
            f'<div class="card" style="margin-top:12px;overflow-x:auto"><table class="t"><tr><th>{tt("When (MX)", "Cuándo (MX)")}</th><th>{tt("User", "Usuario")}</th><th>IP</th><th>{tt("Action", "Acción")}</th><th>{tt("Target", "Objeto")}</th><th>{tt("Detail", "Detalle")}</th></tr>{tr}</table></div>')
    return page(tt("Audit log", "Bitácora"), body, "users")


@app.get("/export.zip")
def export_zip():
    need("export")
    rg = reg()
    c = db()
    buf = io.BytesIO()
    now = now_mx()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name, header, rows):
            s = io.StringIO()
            w = csv.writer(s)
            w.writerow(header)
            w.writerows(rows)
            z.writestr(name, s.getvalue())
        put("sites.csv", ["code", "name", "park", "city", "state", "address", "kwp", "pto", "status", "lat", "lon", "location_approximate"],
            [[s.code, s.name, s.park, s.city, s.state, s.address, s.kwp, s.pto, s.status, s.lat, s.lon, s.geo_approx] for s in rg.sites])
        op = rg.operating
        daily = []
        for i, s in enumerate(op):
            for d in M.history(s, now.date() - dt.timedelta(days=1), 90, i, len(op)):
                daily.append([s.code, d.day.isoformat(), d.kwh, d.expected_kwh, d.irr_kwh_m2, d.pr, d.availability, M.source_for(s)])
        put("daily_energy_90d.csv", ["site", "date", "kwh", "expected_kwh", "irradiation_kwh_m2", "pr", "availability", "source"], daily)
        for name, q in (("tickets.csv", "SELECT * FROM tickets"), ("ticket_events.csv", "SELECT * FROM ticket_events"),
                        ("documents.csv", "SELECT id,site_code,folder,name,size,sha256,mime,ticket_id,uploaded_by,uploaded_utc FROM documents WHERE deleted=0"),
                        ("order_lines.csv", "SELECT * FROM order_lines"),
                        ("catalog.csv", "SELECT * FROM catalog WHERE published=1"),
                        ("equipment.csv", "SELECT * FROM equipment"), ("warranty_claims.csv", "SELECT * FROM warranty_claims"),
                        ("warranty_claim_events.csv", "SELECT * FROM claim_events"), ("spare_parts.csv", "SELECT * FROM parts"),
                        ("stock_locations.csv", "SELECT * FROM stock_locations"), ("stock_minimums.csv", "SELECT * FROM stock_min"),
                        ("stock_movements.csv", "SELECT * FROM stock_moves"),
                        ("audit.csv", "SELECT * FROM audit"), ("users.csv", "SELECT username,name,email,org,role,disabled,created_utc,last_login_utc FROM users")):
            cur = c.execute(q)
            put(name, [d[0] for d in cur.description], [list(r) for r in cur.fetchall()])
        z.writestr("README.txt", "ARGIA for Prologis - data export\nGenerated " + now.isoformat(timespec="minutes") +
                   " Mexico City time.\nsource=sample: simulated until SolarEdge access is granted.\nDocuments: files are available one by one in /docs/, or as a full archive on request.\n")
    S.audit(c, g.user["username"], ip(), "data_export", "export.zip", f"{len(buf.getvalue())} bytes")
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=prologis_export_{now:%Y%m%d}.zip"})


@app.get("/security/")
def security_page():
    tt = t()
    items = [
        (tt("Separate platform", "Plataforma separada"), tt("Own login, own user database and own file store, apart from every other ARGIA customer.", "Acceso, base de usuarios y almacén de archivos propios, separados de cualquier otro cliente de ARGIA.")),
        ((tt("Sign-in (temporary exception)", "Acceso (excepción temporal)"), tt(f"Until {MFA_OFF_UNTIL} a password alone signs in; from the next day an authenticator code (TOTP) is required again for every user. Every sign-in is logged.", f"Hasta el {MFA_OFF_UNTIL} basta la contraseña; desde el día siguiente se vuelve a exigir el código de app autenticadora (TOTP) a cada usuario. Todo acceso queda registrado."))
         if mfa_off() else
         (tt("Two-factor sign-in", "Acceso con dos factores"), tt("Password (scrypt-hashed, 12+ characters) plus an authenticator code (TOTP) for every user.", "Contraseña (scrypt, 12+ caracteres) más código de app autenticadora (TOTP) para cada usuario."))),
        (tt("Least privilege", "Mínimo privilegio"), tt("Four roles; Prologis managers approve dispatches, ARGIA operators work tickets, only administrators manage users.", "Cuatro roles; los gerentes de Prologis aprueban despachos, los operadores de ARGIA trabajan tickets, solo administradores gestionan usuarios.")),
        (tt("Encryption in transit", "Cifrado en tránsito"), tt("TLS 1.2 / 1.3 only, HSTS, strict content-security policy.", "Solo TLS 1.2 / 1.3, HSTS, política de contenido estricta.")),
        (tt("Every action logged", "Todo queda registrado"), tt("Sign-ins, failures, uploads, downloads, ticket and user changes - downloadable by Prologis managers.", "Accesos, fallos, cargas, descargas, cambios de tickets y usuarios - descargable por gerentes de Prologis.")),
        (tt("Your data, exportable", "Sus datos, exportables"), tt("One click exports sites, energy, tickets, documents index, users and audit log as CSV.", "Un clic exporta sitios, energía, tickets, índice de documentos, usuarios y bitácora en CSV.")),
        (tt("Brute-force protection", "Protección contra fuerza bruta"), tt("Lock-out after 5 failed attempts per user or address; idle sessions end after 8 hours.", "Bloqueo tras 5 intentos fallidos por usuario o dirección; sesiones inactivas terminan a las 8 horas.")),
        (tt("Tested releases", "Versiones probadas"), tt("Every release passes 5,000+ automated tests before it reaches the server; the server is checked against the code every morning.", "Cada versión pasa más de 5,000 pruebas automáticas antes de llegar al servidor; el servidor se compara con el código cada mañana.")),
        (tt("Malware-checked uploads", "Cargas revisadas contra malware"), tt("Executables are refused; every uploaded file is scanned with ClamAV and cannot be downloaded until it passes; infected files are quarantined.", "Los ejecutables se rechazan; cada archivo se analiza con ClamAV y no se puede descargar hasta pasar el análisis; los infectados van a cuarentena.")),
        (tt("Access logs kept 12 months", "Registros de acceso por 12 meses"), tt("Web server access logs of this platform are kept for 12 months; the audit log is never deleted automatically.", "Los registros de acceso del servidor web de esta plataforma se guardan 12 meses; la bitácora de auditoría nunca se borra automáticamente.")),
        (tt("Known software, checked", "Software conocido y revisado"), tt("A software bill of materials (CycloneDX) is produced every week and dependencies are checked for known vulnerabilities.", "Cada semana se genera una lista de componentes de software (CycloneDX) y se revisan las dependencias contra vulnerabilidades conocidas.")),
    ]
    if os.path.ismount(DATA_DIR):            # v324: claimed only while it is true on this server
        items.insert(4, (tt("Encryption at rest", "Cifrado en reposo"), tt("The platform's database and files live on an encrypted volume whose key is not stored on the server; backups are encrypted for two offline keys.", "La base de datos y los archivos de la plataforma están en un volumen cifrado cuya llave no se guarda en el servidor; los respaldos se cifran para dos llaves fuera de línea.")))
    cards = "".join(f'<div class="card"><h2>{UI.stripe_svg(16)} {UI.e(a)}</h2><div class="small">{UI.e(b)}</div></div>' for a, b in items)
    c = db()
    admin = ""
    if S.can(g.user["role"], "alarms_admin"):
        st = dict(c.execute("SELECT scan_status, count(*) FROM documents WHERE deleted=0 GROUP BY scan_status").fetchall())
        admin = (f'<div class="card form" style="margin-top:16px"><h2>{tt("Administration", "Administración")}</h2>'
                 f'<p class="small">{tt("Uploads by malware-scan status", "Cargas por estado del análisis")}: {UI.e(", ".join(f"{k} {v}" for k, v in sorted(st.items())) or "-")}</p>'
                 f'<form method="post" action="/security/privacy">{csrf_field()}<label>{tt("Privacy notice address (https://, on argia.com.mx)", "Dirección del aviso de privacidad (https://, en argia.com.mx)")}</label>'
                 f'<input name="url" value="{UI.e(AL.setting(c, "privacy_url"))}" placeholder="https://argia.com.mx/aviso-de-privacidad"><div style="margin-top:8px"><button class="btn sm">{tt("Save", "Guardar")}</button></div></form></div>')
    pl = privacy_link(tt)
    body = (f'<div class="kick">{tt("Trust", "Confianza")}</div><h1 class="pt">{tt("How we protect Prologis data", "Cómo protegemos los datos de Prologis")}</h1>'
            + (f'<p class="small">{pl}</p>' if pl else "") + f'<div class="grid g2e">{cards}</div>'
            + (f'<p style="margin-top:16px"><a class="btn" href="/export.zip">{tt("Export all data", "Exportar todos los datos")}</a> <a class="btn ghost" href="/audit/">{tt("Audit log", "Bitácora")}</a></p>' if S.can(g.user["role"], "export") else "")
            + admin)
    return page(tt("Security", "Seguridad"), body)


@app.post("/security/privacy")
def security_privacy():
    need("alarms_admin")
    try:
        AL.set_setting(db(), "privacy_url", request.form.get("url", ""), g.user["username"], ip())
    except ValueError as ex:
        r = page("Security", f'<div class="card form">{flash(str(ex), True)}<a class="btn ghost" href="/security/">Back / Volver</a></div>')
        r.status_code = 400
        return r
    return redirect("/security/")


# ------------------------------------------------------------------ CLI
def _seed_sample_tickets() -> None:
    rg = reg()
    c = S.connect()
    if c.execute("SELECT count(*) FROM tickets WHERE sample=1").fetchone()[0]:
        print("sample tickets already present")
        return
    op = rg.operating
    base = utc_now()
    plan = [
        (op[0], "Inverter 2 offline - 1/3 of the array not producing", "OUT_100_500", 205.0, 18500.0, True, 30, ["RESPONDED"]),
        (op[min(3, len(op) - 1)], "No data from the Hark logger since 13:00", "COMM_1", None, None, False, 20, []),
        (op[min(6, len(op) - 1)], "Annual preventive maintenance 2027 - scheduling", "OTHER", None, None, False, 70, ["RESPONDED", "IN_PROGRESS"]),
        (op[min(9, len(op) - 1)], "Damaged module found in thermography (hot spot)", "STRING_25", 0.6, 4200.0, True, 200, ["RESPONDED", "IN_PROGRESS", "RESOLVED"]),
    ]
    for s, title, cls, kw, est, appr, hours_ago, steps in plan:
        det = (base - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        tk = S.create_ticket(c, s.code, title, "Sample ticket to show the workflow. Delete or close at go-live.", cls, "argia-sample",
                             detected_utc=det, kw_lost=kw, estimate_mxn=est, needs_approval=appr, sample=True)
        if appr:
            S.set_approval(c, tk, True, "argia-sample", "Sample approval",
                           ts=(base - dt.timedelta(hours=hours_ago - 1)).strftime("%Y-%m-%d %H:%M:%S"))
            tk = c.execute("SELECT * FROM tickets WHERE id=?", (tk["id"],)).fetchone()
        for k, stp in enumerate(steps):
            ts = (base - dt.timedelta(hours=hours_ago) + dt.timedelta(hours=2 + 6 * k)).strftime("%Y-%m-%d %H:%M:%S")
            S.set_status(c, tk, stp, "argia-sample", "", ts=ts)
            tk = c.execute("SELECT * FROM tickets WHERE id=?", (tk["id"],)).fetchone()
    print(f"seeded {len(plan)} sample tickets")


# ------------------------------------------------------------- v333: maintenance plan
PM_ABBR = {"cleaning": "CL", "thermography": "TH", "inspection": "IN", "electrical": "EL", "inverter": "INV", "roof": "RF", "other": "OT"}
PM_CSS = """<style>
.grid.g4{grid-template-columns:repeat(4,minmax(0,1fr))}@media(max-width:980px){.grid.g4{grid-template-columns:1fr 1fr}}
.pmlegend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;margin:10px 0 0}.pmlegend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-2px}
.pmc{display:inline-flex;align-items:center;gap:3px;font:700 10.5px/1 ui-monospace,Menlo,Consolas,monospace;color:#fff;border-radius:5px;padding:4px 6px;margin:2px;white-space:nowrap}
.pmc.due{background:#fff!important;border:1.5px solid currentColor}.pmc.done{opacity:.55}.pmc.overdue{box-shadow:0 0 0 2px #d64545}
.pmyear{border-collapse:collapse;width:100%;min-width:980px}.pmyear th,.pmyear td{border:1px solid #e6e9ec;padding:6px;vertical-align:top}
.pmyear th{background:#f6f7f8;font-size:12px;text-align:center}.pmyear td.site{min-width:190px;background:#fbfbfc}.pmyear td.now{background:#f1fbf9}
.pmcal{border-collapse:collapse;width:100%;table-layout:fixed;min-width:760px}.pmcal th{font-size:12px;color:var(--muted);padding:6px;text-align:left}
.pmcal td{border:1px solid #e6e9ec;height:108px;vertical-align:top;padding:6px}.pmcal td.out{background:#f7f8f9}.pmcal td.today{box-shadow:inset 0 0 0 2px var(--teal,#05b1a9)}
.pmcal .d{font-size:12px;font-weight:700;color:var(--muted);margin-bottom:4px}.pmcal .ev{display:block;font-size:11.5px;line-height:1.3;border-radius:5px;padding:3px 6px;margin-bottom:3px;color:#fff;overflow-wrap:anywhere}
.pmcal .ev.due{background:#fff!important;border:1.5px solid currentColor}.pmcal .ev.done{opacity:.55;text-decoration:line-through}.pmcal .ev.overdue{box-shadow:0 0 0 2px #d64545}
</style>"""


def _pm_label(kind: str, tt) -> str:
    en, es, _c = PM.KINDS.get(kind, PM.KINDS["other"])
    return tt(en, es)


def _pm_status(st: str, tt) -> str:
    cls = {"done": "s-ok", "planned": "s-info", "overdue": "s-bad", "due": "s-off"}[st]
    return f'<span class="pill {cls}">{UI.e(tt(*PM.STATUS_TEXT[st]))}</span>'


def _pm_tabs(on: str, tt, today: dt.date) -> str:
    tabs = [("year", "/maintenance/", tt("Year plan", "Plan anual")),
            ("month", f"/maintenance/month?m={today:%Y-%m}", tt("Calendar", "Calendario")),
            ("tasks", "/maintenance/tasks", tt("Tasks and rhythm", "Tareas y frecuencia"))]
    return ('<div class="tabs">' + "".join(f'<a class="tab{" on" if k == on else ""}" href="{h}">{UI.e(lbl)}</a>' for k, h, lbl in tabs)
            + f'<a class="tab" href="/maintenance/plan.ics" style="margin-left:auto">{tt("Add to my calendar (.ics)", "Agregar a mi calendario (.ics)")}</a></div>')


def _pm_legend(tt) -> str:
    kinds = "".join(f'<span><i style="background:{c}"></i>{UI.e(tt(en, es))} <b class="muted">{PM_ABBR[k]}</b></span>'
                    for k, (en, es, c) in PM.KINDS.items())
    marks = (f'<span><span class="pmc due" style="color:#4a5d73">AB</span> {tt("due", "pendiente")}</span>'
             f'<span><span class="pmc" style="background:#4a5d73">AB</span> {tt("planned", "programado")}</span>'
             f'<span><span class="pmc done" style="background:#4a5d73">AB ✓</span> {tt("done", "hecho")}</span>'
             f'<span><span class="pmc overdue" style="background:#4a5d73">AB</span> {tt("overdue", "vencido")}</span>')
    return f'<div class="pmlegend">{kinds}</div><div class="pmlegend">{marks}</div>'


def _pm_chip(o, tt, site_name: str, cls: str = "pmc") -> str:
    col = PM.KINDS.get(o.kind, PM.KINDS["other"])[2]
    style = f"color:{col}" if o.status == PM.DUE else f"background:{col}"
    tip = f"{PM.title_of(o.title, o.kind, tt.lang)} · {site_name} · {tt('due', 'vence')} {o.due:%d %b %Y}" + (f" · {tt('planned', 'programado')} {o.planned:%d %b}" if o.planned else "") \
        + (f" · {tt('done', 'hecho')} {o.done:%d %b}" if o.done else "")
    label = PM_ABBR.get(o.kind, "OT") + (" ✓" if o.status == PM.DONE else "")
    return f'<span class="{cls} {o.status}" style="{style}" title="{UI.e(tip)}">{UI.e(label)}</span>'


def _pm_tiles(sm: Dict[str, int], tt) -> str:
    def tile(lbl, v, sub, bad=False):
        return (f'<div class="card"><h2>{lbl}</h2><div style="font-size:28px;font-weight:800;color:{"#b42318" if bad and v else "var(--deep)"}">{v}</div>'
                f'<div class="small muted">{sub}</div></div>')
    return ('<div class="grid g4">' + tile(tt("Overdue", "Vencidas"), sm["overdue"], tt("due date passed, not done", "fecha vencida, sin hacer"), True)
            + tile(tt("Next 30 days", "Próximos 30 días"), sm["next30"], tt("due or planned", "pendientes o programadas"))
            + tile(tt("Planned", "Programadas"), sm["planned"], tt("with a date agreed with the site", "con fecha acordada con el sitio"))
            + tile(tt("Done this year", "Hechas este año"), sm["done_year"], tt("with the date it was done", "con la fecha en que se hizo")) + "</div>")


def _pm_head(tt, title: str, sub: str) -> str:
    return (f'<div class="kick">{tt("Maintenance", "Mantenimiento")}</div><h1 class="pt">{UI.e(title)}</h1>'
            f'<p class="muted small">{sub}</p>')


def _pm_all(c, rg, start: dt.date, end: dt.date, today: dt.date):
    return PM.occurrences(PM.tasks(c), PM.visits(c), [s.code for s in rg.operating], start, end, today)


@app.get("/maintenance/")
def maint_year(msg: str = "", err: bool = False):
    tt = t()
    rg, c = reg(), db()
    today = now_mx().date()
    try:
        year = int(request.args.get("y") or today.year)
    except ValueError:
        year = today.year
    year = min(max(year, today.year - 5), today.year + 5)
    occ = _pm_all(c, rg, dt.date(year, 1, 1), dt.date(year, 12, 31), today)
    occ_year = [o for o in occ if o.day.year == year]
    sm = PM.summary(_pm_all(c, rg, dt.date(today.year, 1, 1), today + dt.timedelta(days=60), today), today)
    months = [dt.date(year, m, 1) for m in range(1, 13)]
    mnames = [tt(d.strftime("%b"), ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")[d.month - 1]) for d in months]
    head = "".join(f'<th>{UI.e(n)}</th>' for n in mnames)
    rows = ""
    for s_ in rg.operating:
        cells = ""
        for d in months:
            here = [o for o in occ_year if o.site_code == s_.code and o.day.month == d.month]
            now_c = " now" if (d.year, d.month) == (today.year, today.month) else ""
            cells += f'<td class="{now_c.strip()}">' + "".join(_pm_chip(o, tt, s_.name) for o in here) + "</td>"
        rows += f'<tr><td class="site"><a href="/sites/{s_.code}/"><b>{UI.e(s_.name)}</b></a><div class="small muted">{s_.code} · {UI.num(s_.kwp, 0)} kWp</div></td>{cells}</tr>'
    if not rows:
        rows = f'<tr><td colspan="13" class="muted">{tt("No operating site yet.", "Aún no hay sitios en operación.")}</td></tr>'
    if not PM.tasks(c, active_only=True):
        rows += (f'<tr><td colspan="13" class="muted">{tt("No plan yet: the O&M team adds the recurring tasks (cleaning, thermography, inspections, inverter service) under Tasks and rhythm.", "Aún sin plan: el equipo de O&M agrega las tareas recurrentes (limpieza, termografía, inspecciones, servicio de inversores) en Tareas y frecuencia.")}</td></tr>')
    nav = (f'<div style="display:flex;gap:8px;align-items:center;margin:0 0 10px"><a class="btn sm ghost" href="/maintenance/?y={year - 1}">‹ {year - 1}</a>'
           f'<b style="font-size:16px">{year}</b><a class="btn sm ghost" href="/maintenance/?y={year + 1}">{year + 1} ›</a></div>')
    # the next 60 days: what to prepare, with the O&M team's actions
    edit = S.can(g.user["role"], "pm_edit")
    soon = [o for o in _pm_all(c, rg, today - dt.timedelta(days=400), today + dt.timedelta(days=60), today) if o.status != PM.DONE]
    ag = ""
    for o in soon[:60]:
        sname = _site_name(rg, o.site_code)
        act = ""
        if edit:
            act = (f'<form method="post" action="/maintenance/visit" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">{csrf_field()}'
                   f'<input type="hidden" name="task" value="{o.task_id}"><input type="hidden" name="site" value="{UI.e(o.site_code)}">'
                   f'<input type="hidden" name="due" value="{o.due.isoformat()}"><input type="hidden" name="back" value="/maintenance/?y={year}">'
                   f'<input type="date" name="planned" value="{o.planned.isoformat() if o.planned else ""}" style="width:auto" title="{tt("planned date", "fecha programada")}">'
                   f'<button class="btn sm ghost" name="act" value="plan">{tt("Plan", "Programar")}</button>'
                   f'<button class="btn sm" name="act" value="done">{tt("Done today", "Hecho hoy")}</button></form>')
        ag += (f'<tr><td class="nw"><b>{o.day:%d %b %Y}</b><div class="small muted">{tt("due", "vence")} {o.due:%d %b}</div></td>'
               f'<td><b>{UI.e(sname)}</b><div class="small muted">{UI.e(o.site_code)}</div></td>'
               f'<td>{_pm_chip(o, tt, sname)} {UI.e(PM.title_of(o.title, o.kind, tt.lang))}<div class="small muted">{(UI.e(_pm_label(o.kind, tt)) + " · ") if o.title else ""}{o.duration_days} {tt("day(s)", "día(s)")}</div></td>'
               f'<td>{_pm_status(o.status, tt)}</td><td>{act}</td></tr>')
    ag = ag or f'<tr><td colspan="5" class="muted">{tt("Nothing due in the next 60 days.", "Nada pendiente en los próximos 60 días.")}</td></tr>'
    body = (PM_CSS + _pm_head(tt, tt("Preventive maintenance and cleaning", "Mantenimiento preventivo y limpieza"),
                              tt("Every recurring job per rooftop on one plan: module cleaning, thermography, inspections, electrical tests, inverter service. The O&M team plans the date with the site and records when it is done; the visit report goes to the documents.",
                                 "Cada trabajo recurrente por techo en un plan: limpieza de módulos, termografía, inspecciones, pruebas eléctricas, servicio de inversores. El equipo de O&M programa la fecha con el sitio y registra cuándo se hizo; el reporte de la visita va a los documentos."))
            + flash(msg, err) + _pm_tabs("year", tt, today) + _pm_tiles(sm, tt)
            + f'<div class="card" style="margin-top:16px"><h2>{tt("Year plan", "Plan anual")}</h2>{nav}<div style="overflow-x:auto"><table class="pmyear"><tr><th>{tt("Site", "Sitio")}</th>{head}</tr>{rows}</table></div>{_pm_legend(tt)}</div>'
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><h2>{tt("Next 60 days and overdue", "Próximos 60 días y vencidas")}</h2>'
            f'<table class="t"><tr><th>{tt("Date", "Fecha")}</th><th>{tt("Site", "Sitio")}</th><th>{tt("Task", "Tarea")}</th><th>{tt("Status", "Estado")}</th><th></th></tr>{ag}</table></div>')
    return page(tt("Maintenance", "Mantenimiento"), body, "maint")


@app.get("/maintenance/month")
def maint_month():
    tt = t()
    rg, c = reg(), db()
    today = now_mx().date()
    try:
        y, m = (int(x) for x in (request.args.get("m") or f"{today:%Y-%m}").split("-"))
        first = dt.date(y, m, 1)
    except ValueError:
        first = today.replace(day=1)
    weeks = PM.month_grid(first.year, first.month)
    start, end = first, PM.add_months(first, 1) - dt.timedelta(days=1)
    occ = [o for o in _pm_all(c, rg, start, end, today) if start <= o.day <= end]
    by_day: Dict[dt.date, list] = {}
    for o in occ:
        by_day.setdefault(o.day, []).append(o)
    dn = [tt(*x) for x in (("Mon", "lun"), ("Tue", "mar"), ("Wed", "mié"), ("Thu", "jue"), ("Fri", "vie"), ("Sat", "sáb"), ("Sun", "dom"))]
    trs = ""
    for w in weeks:
        trs += "<tr>"
        for d in w:
            if d is None:
                trs += '<td class="out"></td>'
                continue
            evs = "".join(_pm_chip(o, tt, _site_name(rg, o.site_code), "ev").replace(
                f'>{UI.e(PM_ABBR.get(o.kind, "OT") + (" ✓" if o.status == PM.DONE else ""))}<',
                f'>{UI.e(PM_ABBR.get(o.kind, "OT"))} · {UI.e(_site_name(rg, o.site_code))}<') for o in by_day.get(d, []))
            trs += f'<td class="{"today" if d == today else ""}"><div class="d">{d.day}</div>{evs}</td>'
        trs += "</tr>"
    mname = tt(first.strftime("%B %Y"), f'{("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")[first.month - 1]} {first.year}')
    prev_, next_ = PM.add_months(first, -1), PM.add_months(first, 1)
    nav = (f'<div style="display:flex;gap:8px;align-items:center;margin:0 0 10px"><a class="btn sm ghost" href="/maintenance/month?m={prev_:%Y-%m}">‹</a>'
           f'<b style="font-size:16px">{UI.e(mname)}</b><a class="btn sm ghost" href="/maintenance/month?m={next_:%Y-%m}">›</a>'
           f'<span class="small muted" style="margin-left:auto">{len(occ)} {tt("jobs this month", "trabajos este mes")}</span></div>')
    body = (PM_CSS + _pm_head(tt, tt("Calendar", "Calendario"), tt("The jobs of the month on the day they are planned (or due, while no date is agreed). Hover a job for its details.", "Los trabajos del mes en el día programado (o de vencimiento, mientras no haya fecha acordada). Pase el cursor sobre un trabajo para ver el detalle."))
            + _pm_tabs("month", tt, today)
            + f'<div class="card" style="margin-top:16px">{nav}<div style="overflow-x:auto"><table class="pmcal"><tr>{"".join(f"<th>{UI.e(x)}</th>" for x in dn)}</tr>{trs}</table></div>{_pm_legend(tt)}</div>')
    return page(tt("Maintenance", "Mantenimiento"), body, "maint")


@app.get("/maintenance/tasks")
def maint_tasks(msg: str = "", err: bool = False):
    tt = t()
    rg, c = reg(), db()
    today = now_mx().date()
    edit = S.can(g.user["role"], "pm_edit")
    trs = ""
    for r in PM.tasks(c):
        nxt = [o for o in PM.occurrences([dict(r, active=1)], PM.visits(c), [s.code for s in rg.operating], today, today + dt.timedelta(days=800), today)
               if o.status != PM.DONE]
        nd = min((o.day for o in nxt), default=None)
        tog = ""
        if edit:
            tog = (f'<form method="post" action="/maintenance/tasks/{r["id"]}/active">{csrf_field()}<input type="hidden" name="on" value="{0 if r["active"] else 1}">'
                   f'<button class="btn sm ghost">{tt("Pause", "Pausar") if r["active"] else tt("Resume", "Reanudar")}</button></form>')
        col = PM.KINDS.get(r["kind"], PM.KINDS["other"])[2]
        trs += (f'<tr{" class=muted" if not r["active"] else ""}><td><span class="pmc" style="background:{col}">{PM_ABBR.get(r["kind"], "OT")}</span> <b>{UI.e(PM.title_of(r["title"], r["kind"], tt.lang))}</b>'
                f'<div class="small muted">{" · ".join(UI.e(x) for x in ((_pm_label(r["kind"], tt) if r["title"] else ""), r["notes"]) if x)}</div></td>'
                f'<td>{UI.e(_site_name(rg, r["site_code"])) if r["site_code"] else tt("Every operating site", "Todos los sitios en operación")}</td>'
                f'<td>{tt("every", "cada")} {r["every_months"]} {tt("months", "meses")}</td><td class="nw">{r["first_due"]}</td>'
                f'<td class="nw">{nd.isoformat() if nd and r["active"] else "-"}</td><td class="n">{r["duration_days"]}</td><td>{tog}</td></tr>')
    trs = trs or f'<tr><td colspan="7" class="muted">{tt("No task yet.", "Aún no hay tareas.")}</td></tr>'
    form = ""
    if edit:
        kopts = "".join(f'<option value="{k}">{UI.e(tt(en, es))}</option>' for k, (en, es, _c) in PM.KINDS.items())
        sopts = f'<option value="">{tt("Every operating site", "Todos los sitios en operación")}</option>' + "".join(
            f'<option value="{s_.code}">{UI.e(s_.name)}</option>' for s_ in rg.sites)
        fopts = "".join(f'<option value="{n}" {"selected" if n == 6 else ""}>{n}</option>' for n in PM.FREQUENCIES)
        form = (f'<div class="card" style="margin-top:16px"><h2>{tt("Add a recurring task", "Agregar una tarea recurrente")}</h2>'
                f'<form method="post" action="/maintenance/tasks" class="form">{csrf_field()}<div class="row">'
                f'<div><label>{tt("Kind", "Tipo")}</label><select name="kind">{kopts}</select></div>'
                f'<div><label>{tt("Title (optional)", "Título (opcional)")}</label><input name="title" maxlength="120"></div></div><div class="row">'
                f'<div><label>{tt("Site", "Sitio")}</label><select name="site">{sopts}</select></div>'
                f'<div><label>{tt("Every (months)", "Cada (meses)")}</label><select name="every">{fopts}</select></div>'
                f'<div><label>{tt("First due", "Primer vencimiento")}</label><input type="date" name="first" required></div>'
                f'<div><label>{tt("Days on site", "Días en sitio")}</label><input type="number" name="days" min="1" max="30" value="1"></div></div>'
                f'<label>{tt("Notes", "Notas")}</label><input name="notes" maxlength="500">'
                f'<div style="margin-top:12px"><button class="btn">{tt("Add", "Agregar")}</button></div></form></div>')
    body = (PM_CSS + _pm_head(tt, tt("Tasks and rhythm", "Tareas y frecuencia"), tt("The recurring jobs the plan is made of. Pausing a task keeps its history; the plan rebuilds itself from these rows.", "Los trabajos recurrentes que forman el plan. Pausar una tarea conserva su historial; el plan se reconstruye a partir de estas filas."))
            + flash(msg, err) + _pm_tabs("tasks", tt, today)
            + f'<div class="card" style="margin-top:16px;overflow-x:auto"><table class="t"><tr><th>{tt("Task", "Tarea")}</th><th>{tt("Site", "Sitio")}</th><th>{tt("Rhythm", "Frecuencia")}</th>'
            f'<th>{tt("First due", "Primer vencimiento")}</th><th>{tt("Next", "Siguiente")}</th><th class="n">{tt("Days", "Días")}</th><th></th></tr>{trs}</table></div>' + form)
    return page(tt("Maintenance", "Mantenimiento"), body, "maint")


@app.post("/maintenance/tasks")
def maint_task_add():
    need("pm_edit")
    tt = t()
    rg, c = reg(), db()
    f = request.form
    try:
        tid = PM.add_task(c, f.get("site", ""), f.get("kind", ""), f.get("title", ""), int(f.get("every") or 0), f.get("first", ""),
                          int(f.get("days") or 1), f.get("notes", ""), g.user["username"], [s_.code for s_ in rg.sites])
    except (PM.PlanError, ValueError) as e:
        return maint_tasks(str(e), True)
    S.audit(c, g.user["username"], ip(), "pm_task_add", str(tid), f"{f.get('kind')} every {f.get('every')} m from {f.get('first')} site={f.get('site') or 'all'}")
    return maint_tasks(tt("Task added - it is on the plan now.", "Tarea agregada - ya está en el plan."))


@app.post("/maintenance/tasks/<int:tid>/active")
def maint_task_active(tid: int):
    need("pm_edit")
    c = db()
    on = request.form.get("on") == "1"
    try:
        PM.set_active(c, tid, on)
    except PM.PlanError:
        abort(404)
    S.audit(c, g.user["username"], ip(), "pm_task_" + ("resume" if on else "pause"), str(tid))
    return redirect("/maintenance/tasks")


@app.post("/maintenance/visit")
def maint_visit():
    need("pm_edit")
    c = db()
    f = request.form
    today = now_mx().date()
    back = f.get("back", "/maintenance/")
    if not back.startswith("/maintenance/"):
        back = "/maintenance/"
    act = f.get("act", "plan")
    try:
        if act == "done":
            PM.record(c, int(f.get("task") or 0), f.get("site", ""), f.get("due", ""), g.user["username"], done=today.isoformat(), today=today)
        else:
            PM.record(c, int(f.get("task") or 0), f.get("site", ""), f.get("due", ""), g.user["username"], planned=f.get("planned", ""), today=today)
    except (PM.PlanError, ValueError) as e:
        return maint_year(str(e), True)
    S.audit(c, g.user["username"], ip(), "pm_" + ("done" if act == "done" else "plan"), f"{f.get('task')}/{f.get('site')}/{f.get('due')}",
            f.get("planned", "") if act != "done" else today.isoformat())
    return redirect(back)


@app.get("/maintenance/plan.ics")
def maint_ics():
    tt = t()
    rg, c = reg(), db()
    today = now_mx().date()
    occ = _pm_all(c, rg, today - dt.timedelta(days=31), today + dt.timedelta(days=366), today)
    body = PM.ics(occ, {s_.code: s_.name for s_ in rg.sites}, request.host or "prologis.argia.com.mx", tt.lang)
    S.audit(c, g.user["username"], ip(), "pm_ics", "plan.ics", f"{len(occ)} events")
    return Response(body, mimetype="text/calendar", headers={"Content-Disposition": 'attachment; filename="maintenance-plan.ics"'})


# ------------------------------------------------------------- v345: LED lighting (Prologis Essentials)
# Tomasz, 5 Nov 2026: the LED programme ARGIA delivers for Prologis since 2019 on this platform - by city,
# park and customer, every building in a table, statistics, a CO2 counter, a satellite picture per plant,
# the customers' logos, and the Prologis buildings of 5,000 m2+ still not on LED. Data: the server-only
# LIGHT_DIR/lighting.json (+ logos/<key>.png), written on Tomasz's laptop; no file = no menu entry, 404.
LIGHT_DIR = os.environ.get("ARGIA_PL_LIGHTING_DIR", os.path.join(DATA_DIR, "lighting"))
_LIGHT_CACHE: Dict[str, object] = {"mtime": None, "ds": None, "err": None}


def lighting_ds() -> Optional[LT.Dataset]:
    """The dataset, re-read when the file changes. A broken file keeps the last good one (and says why in
    the log); no file at all = None."""
    path = os.path.join(LIGHT_DIR, "lighting.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        _LIGHT_CACHE.update(mtime=None, ds=None, err=None)
        return None
    if _LIGHT_CACHE["mtime"] == mt:
        return _LIGHT_CACHE["ds"]
    try:
        _LIGHT_CACHE.update(mtime=mt, ds=LT.load(path), err=None)
    except (OSError, ValueError, TypeError, KeyError) as ex:
        _LIGHT_CACHE.update(mtime=mt, err=str(ex))
        print(f"lighting.json not loaded: {ex}", file=sys.stderr)
    return _LIGHT_CACHE["ds"]


_DOCS_CACHE: Dict = {"mtime": None, "by": {}}
DOC_TYPES = [("audit", "Audit", "Auditoría"), ("study", "Lighting study", "Estudio lumínico"), ("proforma", "Proforma", "Proforma"),
             ("wo", "Work order", "Orden de trabajo"), ("po", "Prologis PO", "OC Prologis"), ("handover", "Handover", "Entrega")]
_DOC_PATH_RE = re.compile(r"^[^<>:\"|?*\x00-\x1f\\]+$")


def project_docs() -> Dict[int, List[Dict]]:
    """v357: the project documents the portal serves - lighting/docs.json (built on the laptop) lists them, the files sit
    under lighting/docs/ on the server. A broken list keeps the last good one; no list = no documents card."""
    path = os.path.join(LIGHT_DIR, "docs.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        _DOCS_CACHE.update(mtime=None, by={})
        return {}
    if _DOCS_CACHE["mtime"] != mt:
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("schema") != 1:
                raise ValueError("docs.json: unknown schema")
            by: Dict[int, List[Dict]] = {}
            kinds = {k for k, _, _ in DOC_TYPES}
            for f in d.get("files", []):
                rel = str(f.get("path", ""))
                if f.get("type") not in kinds or not _DOC_PATH_RE.match(rel) or ".." in rel.split("/") or rel.startswith("/"):
                    raise ValueError(f"docs.json: bad entry {rel!r}")
                by.setdefault(int(f["project"]), []).append({"type": f["type"], "date": f.get("date"), "name": str(f.get("name") or rel.rsplit("/", 1)[-1]),
                                                             "path": rel, "size": int(f.get("size") or 0)})
            _DOCS_CACHE.update(mtime=mt, by=by)
        except (OSError, ValueError, TypeError, KeyError) as ex:
            _DOCS_CACHE.update(mtime=mt)
            print(f"docs.json not loaded: {ex}", file=sys.stderr)
    return _DOCS_CACHE["by"]


def _docs_card(p: LT.Project, tt) -> str:
    files = project_docs().get(p.id, [])
    if not files:
        return ""
    icons = {"audit": "🔎", "study": "💡", "proforma": "🧾", "wo": "📝", "po": "📑", "handover": "✅"}
    label = {k: tt(en, es) for k, en, es in DOC_TYPES}

    def size(n: int) -> str:
        return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} kB"
    rows = "".join(f'<a class="docf" href="/lighting/p/{p.id}/doc/{i}" target="_blank" rel="noopener" title="{UI.e(f["name"])}">'
                   f'<span class="docic">{icons[f["type"]]}</span><span><b>{UI.e(label[f["type"]])}</b>'
                   f'<small>{UI.e(f["date"] or "")}{" · " if f["date"] else ""}{size(f["size"])} · {UI.e(f["name"].rsplit(".", 1)[-1].upper())}</small></span>'
                   f'<span class="docdl" title="{tt("Download", "Descargar")}" onclick="event.preventDefault();location.href=this.parentNode.href+\'?dl=1\'">⬇</span></a>'
                   for i, f in enumerate(files))
    return (f'<div class="card" style="margin-top:16px"><h2>{tt("Project documents", "Documentos del proyecto")}<span class="r muted">{len(files)}</span></h2>'
            f'<p class="small muted" style="margin-top:0">{tt("The latest version of each document; open it in the browser or download it. No Google Drive access needed.", "La última versión de cada documento; ábralo en el navegador o descárguelo. No se necesita acceso a Google Drive.")}</p>'
            f'<div class="docgrid">{rows}</div></div>')


_WARRANTY_CACHE: Dict = {"mtime": None, "reg": None}


def warranty_reg() -> Optional[WR.Register]:
    """The warranty claims register (v354), re-read when the file changes; a broken file keeps the last good one."""
    path = os.path.join(LIGHT_DIR, "warranty.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        _WARRANTY_CACHE.update(mtime=None, reg=None)
        return None
    if _WARRANTY_CACHE["mtime"] != mt:
        try:
            _WARRANTY_CACHE.update(mtime=mt, reg=WR.load(path))
        except (OSError, ValueError, TypeError, KeyError) as ex:
            _WARRANTY_CACHE.update(mtime=mt)
            print(f"warranty.json not loaded: {ex}", file=sys.stderr)
    return _WARRANTY_CACHE["reg"]


GMAPS_KEY_FILE = os.path.join(LIGHT_DIR, "google_maps_key")      # v346: server-only, one line, browser key restricted by referrer
_GKEY_RE = re.compile(r"^[A-Za-z0-9_-]{30,60}$")


def gmaps_key() -> str:
    """The Google Maps browser key, or "" (no file or not a key) - then Esri imagery is used."""
    try:
        with open(GMAPS_KEY_FILE, encoding="utf-8") as fh:
            k = fh.read().strip()
    except OSError:
        return ""
    return k if _GKEY_RE.match(k) else ""


def sat_photo(lat: float, lon: float, building: bool, w: int = 640, h: int = 360) -> str:
    """A satellite picture URL: Google Static Maps when the server has a key, else Esri World Imagery."""
    k = gmaps_key()
    if k:
        z = 19 if building else 17
        return (f"https://maps.googleapis.com/maps/api/staticmap?center={lat:.6f},{lon:.6f}&zoom={z}"
                f"&size={w}x{h}&scale=2&maptype=satellite&key={k}")
    return LT.satellite_url(lat, lon, w, h, half_m=110 if building else 230)


def _lds() -> LT.Dataset:
    need("view")
    ds = lighting_ds()
    if ds is None:
        abort(404)
    return LO.apply_pins(ds, LO.pins(db()))           # v360: pins moved by hand in the portal win over the data file


def _can_pin() -> bool:
    return S.can(g.user["role"], "light_request") or S.can(g.user["role"], "light_work")


def _plant_map(p: LT.Project, tt, moved) -> str:
    """v360: the plant on a map you can zoom and pan (Esri satellite / streets, the tiles the CSP allows); managers and
    ARGIA can drag the pin to the right building and save it, or go back to the data file's point."""
    zoom = 19 if p.geo == "building" else 17 if p.geo == "park" else 12      # as the pictures before (v353)
    pin = p.geo in ("building", "park")
    if moved:
        when = (dt.datetime.strptime(moved["ts_utc"], "%Y-%m-%d %H:%M:%S") + M.MX_OFFSET).strftime("%Y-%m-%d")    # Mexico City date
        cap = tt(f"Pin placed by hand by {moved['username']} on {when}", f"Pin colocado a mano por {moved['username']} el {when}")
    else:
        cap = (tt("The pin marks the building", "El pin marca la nave") if p.geo == "building"
               else tt("The pin marks the park (the building's exact position is not surveyed yet)", "El pin marca el parque (la posición exacta de la nave aún no está levantada)")
               if p.geo == "park" else tt("Only the city is known for this project, so there is no pin", "Para este proyecto solo se conoce la ciudad, por eso no hay pin"))
    cfg = json.dumps({"lat": p.lat, "lon": p.lon, "z": zoom, "pin": pin})
    edit = ""
    if _can_pin():
        reset = (f'<button class="btn sm ghost" name="reset" value="1" formnovalidate>{tt("Back to the data file", "Volver al dato del archivo")}</button>' if moved else "")
        edit = (f'<form method="post" action="/lighting/p/{p.id}/pin" class="pinbar" id="pinf">{csrf_field()}'
                f'<input type="hidden" name="lat"><input type="hidden" name="lon">'
                f'<button type="button" class="btn sm" id="pinedit">📍 {tt("Move pin", "Mover pin") if pin else tt("Place pin", "Colocar pin")}</button>'
                f'<span id="pinhelp" class="small muted" hidden>{tt("Drag the pin, or click the building on the map.", "Arrastre el pin o haga clic en la nave en el mapa.")}</span>'
                f'<button class="btn sm" id="pinsave" hidden>{tt("Save position", "Guardar posición")}</button>'
                f'<button type="button" class="btn sm ghost" id="pincancel" hidden>{tt("Cancel", "Cancelar")}</button>{reset}</form>')
    icon = UI.MAP_PIN.replace('class="satpin"', 'class="lpin"').replace("'", "&#39;")
    return f"""<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<div id="pmap" class="pmap"></div>{edit}
<div class="small muted" style="margin-top:6px">{UI.e(cap)} · Esri World Imagery · <a href="{UI.e(LT.google_maps_url(p.lat, p.lon))}" target="_blank" rel="noopener">{tt("Open in Google Maps (satellite)", "Abrir en Google Maps (satélite)")} ›</a></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>(function(){{var C={cfg};var m=L.map('pmap',{{scrollWheelZoom:true,maxZoom:21}}).setView([C.lat,C.lon],C.z);
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri, Maxar, Earthstar Geographics',maxNativeZoom:19,maxZoom:21}}).addTo(m);
var st=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri',maxNativeZoom:19,maxZoom:21}});
L.control.layers({{'{tt("Satellite", "Satélite")}':sat,'{tt("Streets", "Calles")}':st}}).addTo(m);L.control.scale({{imperial:false}}).addTo(m);
var ic=L.divIcon({{html:'{icon}',className:'lpinwrap',iconSize:[34,46],iconAnchor:[17,46]}});
var mk=C.pin?L.marker([C.lat,C.lon],{{icon:ic,keyboard:false}}).addTo(m):null;
var f=document.getElementById('pinf');if(!f)return;var b=document.getElementById('pinedit'),sv=document.getElementById('pinsave'),cn=document.getElementById('pincancel'),hp=document.getElementById('pinhelp'),on=false;
function put(ll){{if(!mk){{mk=L.marker(ll,{{icon:ic,keyboard:false}}).addTo(m);}}else mk.setLatLng(ll);f.lat.value=ll.lat.toFixed(6);f.lon.value=ll.lng.toFixed(6);sv.hidden=false;}}
b.addEventListener('click',function(){{on=true;b.hidden=true;hp.hidden=false;cn.hidden=false;m.getContainer().classList.add('pinning');
if(!mk)put(m.getCenter());mk.dragging.enable();mk.on('dragend',function(){{put(mk.getLatLng());}});}});
m.on('click',function(e){{if(on){{put(e.latlng);mk.dragging.enable();}}}});
cn.addEventListener('click',function(){{location.reload();}});}})();</script>"""


def _mk(key: str, tt) -> str:
    return UI.e(tt(*LT.MARKETS.get(key, (key, key))))


def _ltabs(on: str, tt, n_pipe: int) -> str:
    tabs = [("/lighting/", tt("Overview", "Resumen"), "home"), ("/lighting/buildings/", tt("All buildings", "Todas las naves"), "list"),
            ("/lighting/customers/", tt("Customers", "Clientes"), "cust"),
            ("/lighting/pipeline/", tt("Not yet on LED", "Aún sin LED") + f"<b>{n_pipe}</b>", "pipe")]
    reg = warranty_reg()
    if reg is not None:
        n_open = sum(1 for c in reg.claims if c.status == "open") + len(LO.claims(db(), open_only=True))
        tabs.append(("/lighting/warranty/", tt("Warranty", "Garantías") + (f"<b>{n_open}</b>" if n_open else ""), "warr"))
    tabs.append(("/lighting/calendar/", tt("Calendar", "Calendario"), "cal"))
    return '<div class="tabs">' + "".join(f'<a class="tab{" on" if k == on else ""}" href="{h}">{lbl}</a>' for h, lbl, k in tabs) + "</div>"


def _logo_src(c) -> str:
    """Logo URL with a version from the file's size and time: a replaced logo shows at once although the
    browser keeps logos for a day (v350 - Tomasz still saw the old green tiles after the files were swapped)."""
    try:
        st = os.stat(os.path.join(LIGHT_DIR, "logos", c.logo))
        v = f"{st.st_mtime_ns:x}{st.st_size:x}"[-12:]
    except OSError:
        v = "0"
    return f"/lighting/logo/{UI.e(c.key)}.png?v={v}"


SORT_JS = ("<script>(function(){"
           # v361: one comparison for every column - empty cells ('-') last in both directions (two empties made the
           # old comparison throw, so the browser dropped the whole sort: 'sorting does not work in all columns'),
           # numbers with units are numbers ('0 % LED', '48 months', '$1,234', '18,000 m²'), ISO dates and codes
           # ('2025-339') compare as text in natural order
           "function key(c){if(!c)return[2,0];var s=(c.dataset.v!==undefined?c.dataset.v:c.textContent).replace(/\\s+/g,' ').trim();"
           "if(!s||/^[-\\u2013]$/.test(s))return[2,0];if(/^\\d{4}-\\d{2}-\\d{2}/.test(s))return[1,s];"
           "var m=s.replace(/,/g,'').match(/^(-?)\\$?(-?\\d+(?:\\.\\d+)?)\\s*([^\\d]*)$/);if(m)return[0,(m[1]?-1:1)*parseFloat(m[2])];return[1,s];}"
           "function cmp(x,y,asc){if(x[0]===2||y[0]===2)return(x[0]===2)-(y[0]===2);if(x[0]!==y[0])return asc?x[0]-y[0]:y[0]-x[0];"
           "var d=x[0]===0?x[1]-y[1]:x[1].localeCompare(y[1],'es',{sensitivity:'base',numeric:true});return asc?d:-d;}"
           # columns by position on screen: a merged cell (colspan) counts as empty for the columns it covers
           "function wid(r){var w=0;[].forEach.call(r.cells,function(c){w+=c.colSpan||1;});return w;}"
           "function at(r,vi){var x=0;for(var i=0;i<r.cells.length;i++){if(x===vi)return r.cells[i];x+=r.cells[i].colSpan||1;if(x>vi)return null;}return null;}"
           # every data table with a header row; a row that is one cell across the table (a group title, 'nothing to
           # show') keeps the table as it is unless the table says sortable
           "document.querySelectorAll('table.t').forEach(function(t){if(t.classList.contains('tot')||t.classList.contains('nosort')||t.rows.length<3)return;"
           "var h=t.rows[0];if(![].every.call(h.cells,function(c){return c.tagName==='TH';}))return;var W=wid(h);"
           "function ok(r){return r.cells.length>1&&wid(r)===W;}"
           "var odd=[].slice.call(t.rows,1).some(function(r){return !ok(r);});if(odd&&!t.classList.contains('sortable'))return;"
           "var vi=0;[].forEach.call(h.cells,function(th){var ci=vi;vi+=th.colSpan||1;if(!th.textContent.trim())return;th.classList.add('srt');th.tabIndex=0;th.setAttribute('aria-sort','none');"
           "function go(){var asc=th.getAttribute('aria-sort')!=='ascending';"
           "[].forEach.call(h.cells,function(x){if(x.classList.contains('srt'))x.setAttribute('aria-sort','none');});"
           "th.setAttribute('aria-sort',asc?'ascending':'descending');"
           "var rows=[].slice.call(t.rows,1).filter(ok);"
           "rows.sort(function(a,b){return cmp(key(at(a,ci)),key(at(b,ci)),asc);});"
           "rows.forEach(function(r){r.parentNode.appendChild(r);});}"
           "th.addEventListener('click',go);th.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}});});});})();</script>")


def _hscroll(inner: str, cls: str, tt, ident: str = "") -> str:
    """Two rows that scroll sideways: swipe on a phone; wheel, trackpad or the arrow buttons on a desktop (v351)."""
    idattr = f' id="{ident}"' if ident else ""
    return (f'<div class="lscroll"><button type="button" class="lsb" data-d="-1" aria-label="{tt("Scroll left", "Desplazar a la izquierda")}">‹</button>'
            f'<div class="{cls} hrow"{idattr}>{inner}</div>'
            f'<button type="button" class="lsb" data-d="1" aria-label="{tt("Scroll right", "Desplazar a la derecha")}">›</button></div>')


HSCROLL_JS = ("<script>document.querySelectorAll('.lscroll').forEach(function(w){var g=w.querySelector('.hrow'),b=w.querySelectorAll('.lsb');"
              "function upd(){b[0].disabled=g.scrollLeft<4;b[1].disabled=g.scrollLeft+g.clientWidth>=g.scrollWidth-4;}"
              "b.forEach(function(x){x.addEventListener('click',function(){g.scrollBy({left:+x.dataset.d*g.clientWidth*0.9,behavior:'smooth'});});});"
              "g.addEventListener('scroll',upd,{passive:true});window.addEventListener('resize',upd);g.addEventListener('hupd',upd);upd();});</script>")


def _q(*parts: str) -> str:
    """Search key: lower case, no accents ("Sincronía" is found by "sincronia")."""
    s = " ".join(p for p in parts if p)
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn").lower()


def _clogo(ds: LT.Dataset, p_or_key, name: str = "", big: bool = False, size: str = "") -> str:
    """The customer's logo in full colour (server-only PNG) or a monogram badge."""
    key = p_or_key.group if isinstance(p_or_key, LT.Project) else p_or_key
    name = name or (p_or_key.customer if isinstance(p_or_key, LT.Project) else key) or ""
    c = ds.customers.get(key) if key else None
    cls = ("clogo big" if big else ("clogo " + size).strip()) + " plain"
    if c and c.logo:
        return f'<span class="{cls}"><img src="{_logo_src(c)}" alt="{UI.e(c.name)}" loading="lazy"></span>'
    words = [w for w in re.split(r"[^A-Za-z0-9&+]+", name) if w]
    init = "".join(w[0] for w in words[:2]).upper() or "-"
    if isinstance(p_or_key, LT.Project) and p_or_key.kind != "tenant":     # vacant, exterior, common areas, stock: Prologis's own (v351, v353)
        img = _brand_img("prologis_logo.png", "", "Prologis")
        if img:
            return f'<span class="{cls}">{img}</span>'
    if isinstance(p_or_key, LT.Project) and p_or_key.kind != "tenant":      # no brand file on the server: grey initials
        return f'<span class="{cls} mono" style="background:#9aa4ad">{UI.e(init)}</span>'
    hue = int(hashlib.md5(name.encode()).hexdigest()[:4], 16) % 360
    return f'<span class="{cls} mono" style="background:hsl({hue} 30% 36%)">{UI.e(init)}</span>'


def _lmap(points: List[Dict], height: str = "520px", sat: bool = False) -> str:
    """Leaflet map (streets / satellite, Esri - the tiles the CSP allows), one circle per point, popup with a link."""
    data = json.dumps(points, ensure_ascii=False).replace("</", "<\\/")
    base = "sat" if sat else "st"
    return f"""<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<div id="map" style="height:{height}"></div><script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>(function(){{var P={data};var m=L.map('map',{{scrollWheelZoom:true}});
var st=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri',maxZoom:19}});
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri, Maxar, Earthstar Geographics',maxZoom:19}});
{base}.addTo(m);L.control.layers({{'Streets':st,'Satellite':sat}}).addTo(m);
var manual=false;m.getContainer().addEventListener('click',function(e){{if(e.target.closest('.leaflet-control-layers-base'))manual=true;}},true);
function auto(){{if(manual)return;var z=m.getZoom();if(z>=14&&m.hasLayer(st)){{m.removeLayer(st);sat.addTo(m);}}
else if(z<14&&m.hasLayer(sat)&&'{base}'==='st'){{m.removeLayer(sat);st.addTo(m);}}}}m.on('zoomend',auto);
function esc(s){{return String(s).replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
var b=[];P.forEach(function(p){{b.push([p.lat,p.lon]);var mk=L.circleMarker([p.lat,p.lon],{{radius:p.r||7,color:'#fff',weight:1.5,fillColor:p.c,fillOpacity:.9}}).addTo(m)
.bindPopup((p.ph?'<img src="'+esc(p.ph)+'" referrerpolicy="origin" alt="" style="display:block;width:240px;height:135px;object-fit:cover;border-radius:6px;margin-bottom:6px;background:#dfe6e4">':'')
+'<b>'+esc(p.t)+'</b><div class="small muted">'+esc(p.s)+'</div>'+(p.u?'<a href="'+p.u+'">'+esc(p.l||'Open')+' ›</a>':''),{{minWidth:240}});
mk.on('click',function(){{if(m.getZoom()<15)m.flyTo([p.lat,p.lon],16,{{duration:.8}});}});}});
if(b.length>1)m.fitBounds(b,{{padding:[30,30],maxZoom:15}});else if(b.length)m.setView(b[0],16);}})();</script>"""


def _park_points(ds: LT.Dataset, projects: List[LT.Project], tt) -> List[Dict]:
    pts = []
    for key, ps in LT.group_by([p for p in projects if p.lat is not None], LT.park_key).items():
        t_ = LT.totals(ps, today_mx())
        pts.append({"lat": sum(p.lat for p in ps) / len(ps), "lon": sum(p.lon for p in ps) / len(ps),
                    "c": UI.TEAL, "r": max(6, min(22, (t_.fixtures or 50) ** 0.5 / 2.4)),
                    "t": f"{ps[0].park} ({tt(*LT.MARKETS[ps[0].market])})",
                    "s": " · ".join(x for x in (f"{t_.projects} " + (tt("project", "proyecto") if t_.projects == 1 else tt("projects", "proyectos")),
                                                 f"{UI.num(t_.fixtures)} {tt('fixtures', 'luminarias')}",
                                                 f"{UI.num(t_.area_m2)} m²" if t_.area_m2 else "") if x),
                    "u": f"/lighting/buildings/?pk={key}", "l": tt("Open the park", "Abrir el parque"),
                    "ph": sat_photo(sum(p.lat for p in ps) / len(ps), sum(p.lon for p in ps) / len(ps), False, 480, 270)})
    return pts


def _tbl_summary(rows, tt, link_key: str, label, head: str = "", city=None) -> str:
    out = ""
    for k, t_ in rows:
        out += (f'<tr><td><a href="/lighting/buildings/?{link_key}={UI.e(k)}">{UI.e(label(k))}</a></td>'
                + (f'<td>{UI.e(city(k))}</td>' if city else "") + f'<td class="n">{t_.projects}</td>'
                f'<td class="n">{UI.num(t_.fixtures)}</td><td class="n">{UI.num(t_.area_m2)}</td><td class="n">{UI.num(t_.mwh_saved)}</td>'
                f'<td class="n">{UI.num(t_.co2_t_year)}</td></tr>')
    return (f'<div style="overflow-x:auto"><table class="t sortable"><tr><th>{UI.e(head)}</th>' + (f'<th>{tt("City", "Ciudad")}</th>' if city else "")
            + f'<th class="n">{tt("Projects", "Proyectos")}</th><th class="n">{tt("Fixtures", "Luminarias")}</th>'
            f'<th class="n">m²</th><th class="n">MWh/{tt("yr", "año")}</th><th class="n">t CO₂e/{tt("yr", "año")}</th></tr>{out}</table></div>')


def _warranty_until(p: LT.Project, tt) -> str:
    """5 years after the handover (v354); says whether it has ended. A planned handover gives no date yet."""
    reg = warranty_reg()
    years = reg.years if reg else 5
    end = WR.warranty_until(p.handover_date, years) if p.handover_date and not p.handover_planned else None
    if not end:
        return "-"
    gone = end < today_mx().isoformat()
    return f'{UI.e(end)} <span class="small muted">({tt("ended", "terminó") if gone else tt(f"{years} years from handover", f"{years} años desde la entrega")})</span>'


def _claim_head(tt, with_place: bool = True) -> str:
    place = (f'<th>{tt("Prologis building code", "Código Prologis")}</th><th>{tt("Park", "Parque")}</th><th>{tt("City", "Ciudad")}</th>'
             f'<th>{tt("Customer", "Cliente")}</th>') if with_place else ""
    return (f'<tr><th>{tt("Claim", "Reclamo")}</th><th>{tt("Reported", "Reportado")}</th>{place}<th class="n">{tt("Fixtures", "Luminarias")}</th>'
            f'<th>{tt("Model", "Modelo")}</th><th>{tt("Failure", "Falla")}</th><th>{tt("Closed", "Cerrado")}</th>'
            f'<th class="n">{tt("Days", "Días")}</th><th>{tt("Status", "Estado")}</th></tr>')


def _claim_row(c: WR.Claim, tt, today: dt.date, with_place: bool = True) -> str:
    st = WR.STATUSES[c.status]
    days = c.days_open(today)
    href = ""
    ds = lighting_ds()
    if ds is not None and c.code:                 # the customer's project in that building, else every project there
        cands = [p for p in ds.projects if p.yardi == c.code]
        own = [p for p in cands if c.group and p.group == c.group]
        pick = own if len(own) == 1 else (cands if len(cands) == 1 else [])
        href = f"/lighting/p/{pick[0].id}" if pick else (f"/lighting/buildings/?q={UI.e(c.code)}" if cands else "")
    code = f'<a href="{href}">{UI.e(c.code)}</a>' if href else UI.e(c.code or "-")
    place = (f'<td class="nw">{code}</td><td>{UI.e(c.park or "-")}</td><td>{_mk(c.market, tt) if c.market else "-"}</td>'
             f'<td>{UI.e(c.customer or "-")}</td>') if with_place else ""
    return (f'<tr><td class="nw">{UI.e(c.ticket or "-")}</td><td class="nw">{UI.e(c.reported or "-")}</td>{place}'
            f'<td class="n">{UI.num(c.qty) if c.qty else "-"}</td><td class="small">{UI.e(", ".join(c.models) or "-")}</td>'
            f'<td class="small">{UI.e(c.cause or "-")}</td><td class="nw">{UI.e(c.closed or "-")}</td>'
            f'<td class="n">{days if days is not None else "-"}{"" if c.status == "closed" or days is None else "+"}</td>'
            f'<td><span class="pill" style="background:{st[2]}22;color:#14211f">{UI.e(tt(*st[:2]))}</span></td></tr>')


@app.get("/lighting/")
def lighting_home():
    ds = _lds()
    tt, today = t(), today_mx()
    T_ = LT.totals(ds.projects, today)
    pipe = LT.pipeline(ds)
    deliv = ds.delivered
    by_m = LT.summary_rows(LT.group_by(deliv, lambda p: p.market), today)
    by_p = LT.summary_rows(LT.group_by(deliv, LT.park_key), today)
    pname = {LT.park_key(p): p.park for p in deliv}
    pcity = {LT.park_key(p): tt(*LT.MARKETS[p.market]) for p in deliv}
    ser = LT.year_series(ds.projects, today)
    views = [("projects", tt("Projects", "Proyectos"), tt("projects", "proyectos"), 1, UI.TEAL),
             ("fixtures", tt("LED fixtures", "Luminarias LED"), tt("fixtures", "luminarias"), 1, UI.SKY),
             ("area_m2", tt("Floor area", "Superficie"), "m²", 1, UI.DEEP),
             ("mwh", tt("Energy saved", "Energía ahorrada"), f"MWh/{tt('yr', 'año')}", 1, UI.LIME),
             ("co2_cum", tt("CO₂e avoided, cumulative", "CO₂e evitado, acumulado"), "t CO₂e", 1, "#3c4b49")]
    ctabs = "".join(f'<button type="button" class="tab{" on" if i == 0 else ""}" data-ch="{k}">{UI.e(lbl)}</button>' for i, (k, lbl, _, _, _) in enumerate(views))
    cpanes = "".join(f'<div class="chpane" data-ch="{k}"{"" if i == 0 else " hidden"}>'
                     + UI.bar_chart([(str(y), v, None) for y, v in ser.get(k, [])], unit=unit, h=210, color=col) + "</div>"
                     for i, (k, _, unit, _, col) in enumerate(views))
    chart = (f'<div class="tabs chtabs" style="margin:0 0 8px">{ctabs}</div>{cpanes}'
             f'<p class="small muted" style="margin:4px 0 0">{tt("By year of handover (or of the signed work order). CO₂e: avoided each year by everything handed over until then, at that year&#39;s grid factor; *current year to date.", "Por año de entrega (o de la orden de trabajo firmada). CO₂e: evitado cada año por todo lo entregado hasta entonces, con el factor de red de ese año; *año en curso a la fecha.")}</p>'
             '<script>document.querySelectorAll(".chtabs .tab").forEach(function(b){b.addEventListener("click",function(){'
             'document.querySelectorAll(".chtabs .tab").forEach(function(x){x.classList.toggle("on",x===b);});'
             'document.querySelectorAll(".chpane").forEach(function(p){p.hidden=p.dataset.ch!==b.dataset.ch;});});});</script>')
    cust = LT.summary_rows(LT.group_by([p for p in deliv if p.group], lambda p: p.group), today)   # all of them, 2 scrolling rows
    logos = "".join(f'<a class="ltile" href="/lighting/buildings/?c={UI.e(k)}" title="{UI.e(ds.customers[k].name if k in ds.customers else k)}">'
                    f'{_clogo(ds, k, ds.customers[k].name if k in ds.customers else k, size="mid")}<span>{UI.e(ds.customers[k].name if k in ds.customers else k)}</span>'
                    f'<small>{t_.projects} · {UI.num(t_.fixtures)} {tt("fixtures", "luminarias")}</small></a>' for k, t_ in cust)
    cnt = {k: sum(1 for s in pipe if s.status == k) for k in LT.BSTATUS}
    pot = LT.potential(pipe, LT.intensity(ds), today.year)
    since = f"{tt('since', 'desde')} {T_.first_year}" if T_.first_year else ""
    kp = [(tt("Projects delivered", "Proyectos entregados"), UI.num(T_.projects), "", f"{UI.num(T_.buildings)} {tt('buildings', 'naves')} {since}"),
          (tt("LED fixtures installed", "Luminarias LED instaladas"), UI.num(T_.fixtures), "",
           (f'+ {UI.num(T_.stock_fixtures)} {tt("as replacement stock", "como stock de reemplazo")}' if T_.stock_fixtures else "")),
          (tt("Floor area relit", "Superficie iluminada"), UI.num(T_.area_m2 / 1e6, 2), "M m²", ""),
          (tt("Energy saved", "Energía ahorrada"), UI.num(T_.mwh_saved / 1000, 1), f"GWh/{tt('yr', 'año')}", tt("design, from the lighting studies", "de diseño, según estudios lumínicos")),
          (tt("CO₂e avoided", "CO₂e evitado"), UI.num(T_.co2_t_year), f"t/{tt('yr', 'año')}", UI.e(co2reg_label())),
          (tt("Parks · markets", "Parques · mercados"), f"{T_.parks} · {T_.markets}", "", f"{T_.customers} {tt('customers', 'clientes')}")]
    kpis = "".join(f'<div class="kpi"><div class="l">{l}</div><div class="v">{v}<small>{u}</small></div><div class="d">{d}</div></div>' for l, v, u, d in kp)
    counter = (f'<div class="kpi lcount"><div class="l"><span class="live-dot"></span>{tt("CO₂e avoided since handover", "CO₂e evitado desde la entrega")}</div>'
               f'<div class="v"><span id="co2c" data-v="{T_.co2_t_since:.4f}" data-r="{T_.co2_rate_t_s:.10f}">{UI.num(T_.co2_t_since, 3)}</span><small>t</small></div>'
               f'<div class="d">{tt("each year at that year&#39;s grid factor; counting live", "cada año con su factor de red; contando en vivo")}</div></div>')
    js = """<script>(function(){var el=document.getElementById('co2c');if(!el)return;var v0=parseFloat(el.dataset.v),r=parseFloat(el.dataset.r),t0=Date.now();
setInterval(function(){el.textContent=(v0+r*(Date.now()-t0)/1000).toLocaleString('en-US',{minimumFractionDigits:3,maximumFractionDigits:3});},250);})();</script>"""
    body = f"""{_ltabs("home", tt, len(pipe))}
<section class="hero"><div class="glow"></div><div class="k">{tt("LED lighting · Prologis Essentials", "Iluminación LED · Prologis Essentials")}</div>
<h1>{tt("Prologis buildings relit with LED, one view.", "Naves de Prologis con iluminación LED, en una vista.")}</h1>
<div class="sub">{tt("ARGIA converts Prologis buildings across Mexico to LED: audit, lighting study, proforma, work order, installation and handover.", "ARGIA convierte naves de Prologis en todo México a LED: auditoría, estudio lumínico, proforma, orden de trabajo, instalación y entrega.")}</div>
<div class="kpis">{counter}{kpis}</div></section>
<div class="grid g2 lg"><div class="card"><h2 style="flex-wrap:wrap">{tt("Where", "Dónde")}<span class="r muted" style="font-weight:600">{tt("one circle per park, size = LED fixtures", "un círculo por parque, tamaño = luminarias")}</span></h2>{_lmap(_park_points(ds, deliv, tt))}</div>
<div class="card"><h2>{tt("By city", "Por ciudad")}</h2>{_tbl_summary(by_m, tt, "m", lambda k: tt(*LT.MARKETS[k]), tt("City", "Ciudad"))}
<h2 style="margin-top:18px">{tt("Year by year", "Año por año")}</h2>{chart}</div></div>
<div class="grid g2e lg"><div class="card"><h2>{tt("By park", "Por parque")}<span class="r muted">{len(by_p)} {tt("parks", "parques")}</span></h2>
<div class="sitebox" style="max-height:440px">{_tbl_summary(by_p, tt, "pk", lambda k: pname.get(k, k), tt("Park", "Parque"), city=lambda k: pcity.get(k, ""))}</div></div>
<div class="card"><h2>{tt("Still to convert", "Por convertir")}<a class="r" href="/lighting/pipeline/">{tt("Open the list", "Abrir la lista")} ›</a></h2>
<p class="small muted" style="margin-top:0">{tt("Prologis buildings of 5,000 m² and more (Prologis property database) that are not, or only partly, on LED according to ARGIA's records.", "Naves de Prologis de 5,000 m² o más (base de propiedades de Prologis) que según los registros de ARGIA no están, o solo en parte, en LED.")}</p>
<table class="t">{"".join(f'<tr><td><span class="pill" style="background:{LT.BSTATUS[k][2]}22;color:#14211f">{UI.e(tt(*LT.BSTATUS[k][:2]))}</span></td><td class="n"><b>{cnt[k]}</b></td></tr>' for k in ("in_progress", "audited", "partial", "not_started"))}
<tr><td>{tt("Floor area to convert", "Superficie por convertir")}</td><td class="n"><b>{UI.num(pot["m2"])} m²</b></td></tr>
<tr><td>{tt("Potential (estimate)", "Potencial (estimado)")}</td><td class="n"><b>{UI.num(pot["mwh"])} MWh/{tt("yr", "año")} · {UI.num(pot["co2_t"])} t CO₂e/{tt("yr", "año")}</b></td></tr></table></div></div>
<div class="card" style="margin-top:16px"><h2>{tt("Customers", "Clientes")}<a class="r" href="/lighting/customers/">{tt("All customers", "Todos los clientes")} ›</a></h2>{_hscroll(logos, "lgrid2", tt, "lgrid2")}{HSCROLL_JS}</div>
<p class="small muted">{tt("Savings are design values from ARGIA's lighting studies (annual kWh before vs after), not metered. Positions are park-level unless the building was surveyed. Data as of", "Los ahorros son valores de diseño de los estudios lumínicos de ARGIA (kWh anuales antes y después), no medidos. Las posiciones son por parque salvo levantamiento. Datos al")} {UI.e(ds.generated)}.</p>{js}"""
    return page(tt("LED lighting", "Iluminación LED"), body, on="lighting")


def co2reg_label() -> str:
    from argia.core import co2 as _co2
    return _co2.label()


def _filter(ds: LT.Dataset) -> tuple:
    a = request.args
    q = (a.get("q") or "").strip().lower()[:80]
    ps = ds.projects
    if a.get("m"):
        ps = [p for p in ps if p.market == a.get("m")]
    if a.get("pk"):
        ps = [p for p in ps if LT.park_key(p) == a.get("pk")]
    if a.get("c"):
        ps = [p for p in ps if p.group == a.get("c")]
    if a.get("s"):
        ps = [p for p in ps if p.status == a.get("s")]
    if a.get("y", "").isdigit():
        ps = [p for p in ps if p.year == int(a.get("y"))]
    if q:
        ps = [p for p in ps if q in " ".join(str(x or "") for x in (p.customer, p.park, p.bldg, p.yardi, p.wo_number, p.po_number)).lower()]
    return ps, {k: a.get(k, "") for k in ("q", "m", "pk", "c", "s", "y")}


@app.get("/lighting/buildings/")
def lighting_list():
    ds = _lds()
    tt, today = t(), today_mx()
    ps, f = _filter(ds)
    ps = sorted(ps, key=lambda p: (-(p.year or 0), p.market, p.park, p.bldg or ""))
    T_ = LT.totals(ps, today)
    parks = sorted({(LT.park_key(p), f"{p.park} · {tt(*LT.MARKETS[p.market])}") for p in ds.projects}, key=lambda x: x[1])
    custs = sorted({(p.group, ds.customers[p.group].name if p.group in ds.customers else p.customer) for p in ds.projects if p.group}, key=lambda x: x[1].lower())
    years = sorted({p.year for p in ds.projects if p.year}, reverse=True)

    def sel(name, opts, cur, all_lbl):
        return (f'<select name="{name}" onchange="this.form.submit()"><option value="">{all_lbl}</option>'
                + "".join(f'<option value="{UI.e(v)}"{" selected" if str(v) == cur else ""}>{UI.e(lbl)}</option>' for v, lbl in opts) + "</select>")
    form = (f'<form class="lfilt" method="get"><input name="q" value="{UI.e(f["q"])}" placeholder="{tt("Search customer, park, building code, WO, PO", "Buscar cliente, parque, código, OT, OC")}">'
            + sel("m", [(k, tt(*LT.MARKETS[k])) for k in LT.MARKETS if any(p.market == k for p in ds.projects)], f["m"], tt("All cities", "Todas las ciudades"))
            + sel("pk", parks, f["pk"], tt("All parks", "Todos los parques"))
            + sel("c", custs, f["c"], tt("All customers", "Todos los clientes"))
            + sel("s", [(k, tt(*v[:2])) for k, v in LT.STATUSES.items()], f["s"], tt("Any status", "Cualquier estado"))
            + sel("y", [(y, y) for y in years], f["y"], tt("All years", "Todos los años"))
            + f'<button class="btn sm">{tt("Filter", "Filtrar")}</button><a class="btn sm ghost" href="/lighting/buildings/">{tt("Clear", "Limpiar")}</a></form>')
    chips = (f'<div class="lchips"><span class="chip"><b>{len(ps)}</b> {tt("projects", "proyectos")}</span><span class="chip"><b>{UI.num(T_.fixtures)}</b> {tt("LED fixtures", "luminarias LED")}</span>'
             f'<span class="chip"><b>{UI.num(T_.area_m2)}</b> m²</span><span class="chip"><b>{UI.num(T_.mwh_saved)}</b> MWh/{tt("yr", "año")}</span>'
             f'<span class="chip"><b>{UI.num(T_.co2_t_year)}</b> t CO₂e/{tt("yr", "año")}</span><span class="chip"><b>{UI.num(T_.co2_t_since)}</b> t CO₂e {tt("since handover", "desde la entrega")}</span></div>')
    rows = ""
    for p in ps:
        s = p.kwh_saved
        st = LT.STATUSES[p.status]
        rows += (f'<tr><td>{_clogo(ds, p)}</td><td><a href="/lighting/p/{p.id}"><b>{UI.e(p.customer)}</b></a><div class="small muted">{UI.e(tt(*LT.KINDS[p.kind]))}</div></td>'
                 f'<td>{UI.e(p.park)}</td><td>{_mk(p.market, tt)}</td><td class="nw">{UI.e(p.yardi or "-")}</td><td class="n">{p.year or "-"}</td>'
                 f'<td class="n">{UI.num(p.fixtures)}</td><td class="n">{UI.num(p.area_m2)}</td><td class="n">{UI.num(100 * p.saving_share) + " %" if s else "-"}</td>'
                 f'<td class="n">{UI.num(s / 1000, 1) if s else "-"}</td><td class="nw">{UI.e(p.wo_number or "-")}</td>'
                 f'<td><span class="pill" style="background:{st[2]}22;color:#14211f">{UI.e(tt(*st[:2]))}</span></td></tr>')
    csv_link = (f'<a class="r" href="/lighting/projects.csv?{UI.e(request.query_string.decode())}">CSV ›</a>' if S.can(g.user["role"], "export") else "")
    body = f"""{_ltabs("list", tt, len(LT.pipeline(ds)))}
<h1 class="pt">{tt("All LED projects", "Todos los proyectos LED")}</h1><p class="muted" style="margin-top:0">{tt("Every building ARGIA has relit for Prologis, and the projects under way.", "Cada nave que ARGIA ha iluminado para Prologis, y los proyectos en curso.")}</p>
{form}{chips}
<div class="card"><h2>{tt("Projects", "Proyectos")}{csv_link}</h2><div style="overflow-x:auto"><table class="t sortable"><tr><th></th><th>{tt("Customer", "Cliente")}</th><th>{tt("Park", "Parque")}</th><th>{tt("City", "Ciudad")}</th>
<th>{tt("Prologis building code", "Código Prologis")}</th><th class="n">{tt("Year", "Año")}</th><th class="n">{tt("Fixtures", "Luminarias")}</th><th class="n">m²</th><th class="n">{tt("Saving", "Ahorro")}</th>
<th class="n">MWh/{tt("yr", "año")}</th><th>{tt("WO", "OT")}</th><th>{tt("Status", "Estado")}</th></tr>{rows or f'<tr><td colspan="12" class="muted">{tt("No project matches these filters.", "Ningún proyecto coincide con los filtros.")}</td></tr>'}</table></div></div>"""
    return page(tt("LED projects", "Proyectos LED"), body, on="lighting")


@app.get("/lighting/p/<int:pid>")
def lighting_project(pid: int):
    ds = _lds()
    tt, today = t(), today_mx()
    p = ds.project(pid)
    if p is None:
        abort(404)
    st = LT.STATUSES[p.status]
    s = p.kwh_saved
    moved = LO.pins(db()).get(p.id)
    pic = _plant_map(p, tt, moved) if p.lat is not None else ""
    pmsg = {"saved": tt("Position saved.", "Posición guardada."), "reset": tt("Back to the position in the data file.", "De vuelta a la posición del archivo de datos.")}.get(request.args.get("pin", ""), "")
    hand = p.handover_date if not p.handover_planned else (f'{p.handover_date} ({tt("planned", "planeada")})' if p.handover_date else None)
    since = p.co2_t_since(today)
    facts = [(tt("Customer", "Cliente"), UI.e(p.customer)), (tt("Type", "Tipo"), UI.e(tt(*LT.KINDS[p.kind]))),
             (tt("Prologis building code", "Código Prologis"), UI.e(p.yardi or "-")),
             (tt("Park", "Parque"), UI.e(p.park)), (tt("Building", "Nave"), UI.e(p.bldg or "-")), (tt("City", "Ciudad"), _mk(p.market, tt)),
             (tt("Work order", "Orden de trabajo"), UI.e(p.wo_number or "-")), (tt("Prologis PO", "OC Prologis"), UI.e(p.po_number or "-")),
             (tt("Work order signed", "OT firmada"), UI.e(p.wo_date or "-")), (tt("Handover", "Entrega"), UI.e(hand or "-")),
             (tt("Warranty until", "Garantía hasta"), _warranty_until(p, tt)),
             (tt("LED fixtures", "Luminarias LED"), UI.num(p.fixtures)), (tt("Area", "Superficie"), f"{UI.num(p.area_m2)} m²" if p.area_m2 else "-"),
             (tt("Fixture maker", "Fabricante"), UI.e(p.maker or "-")), (tt("Controls", "Controles"), UI.e(p.controls or "-")),
             (tt("Design light level", "Nivel de diseño"), UI.e(p.lux or "-"))]
    ftab = "".join(f'<tr><td class="muted">{k}</td><td><b>{v}</b></td></tr>' for k, v in facts)
    energy = ""
    if s:
        mx = p.kwh_before
        energy = (f'<div class="card"><h2>{tt("Energy (design)", "Energía (diseño)")}</h2>'
                  f'<div class="small">{tt("Before", "Antes")} <b>{UI.num(p.kwh_before)} kWh/{tt("yr", "año")}</b></div><div class="hbar"><i style="width:100%;background:#9aa4ad"></i></div>'
                  f'<div class="small" style="margin-top:8px">{tt("After", "Después")} <b>{UI.num(p.kwh_after)} kWh/{tt("yr", "año")}</b></div><div class="hbar"><i style="width:{100 * p.kwh_after / mx:.1f}%"></i></div>'
                  f'<div class="facts" style="margin-top:14px"><div class="chip"><b>{UI.num(100 * p.saving_share)} %</b> {tt("less energy", "menos energía")}</div>'
                  f'<div class="chip"><b>{UI.num(s / 1000, 1)}</b> MWh/{tt("yr", "año")}</div><div class="chip"><b>{UI.num(p.co2_t_per_year(today.year), 1)}</b> t CO₂e/{tt("yr", "año")}</div>'
                  f'<div class="chip"><b>{UI.num(since, 1) if since is not None else "-"}</b> t CO₂e {tt("since handover", "desde la entrega")}</div></div></div>')
    steps = [("audit", tt("Audit", "Auditoría")), ("study", tt("Lighting study", "Estudio lumínico")), ("proforma", "Proforma"),
             ("wo", tt("Work order", "Orden de trabajo")), ("handover", tt("Handover", "Entrega"))]
    stepper = "".join(f'<div class="st{" done" if p.docs.get(k) else ""}"><i></i>{lbl}</div>' for k, lbl in steps)
    reg = warranty_reg()
    wclaims = WR.for_building(reg.claims, p.yardi) if reg else []
    wcard = ""
    if reg is not None and p.yardi:
        wrows = "".join(_claim_row(c, tt, today, with_place=False) for c in wclaims)
        mine = LO.claims(db(), p.yardi)
        wcard = (f'<div class="card" style="margin-top:16px"><h2>{tt("Warranty claims in this building", "Reclamos de garantía en esta nave")}'
                 + (f'<a class="btn sm" style="margin-left:auto" href="/lighting/warranty/new?code={UI.e(p.yardi)}">{tt("Report a failure", "Reportar una falla")}</a>'
                    if S.can(g.user["role"], "light_claim_new") else "")
                 + f'<a class="r" href="/lighting/warranty/">{tt("All claims", "Todos los reclamos")} ›</a></h2>'
                 + "".join(f'<div class="small" style="margin:0 0 6px"><a href="/lighting/warranty/c/{UI.e(r["number"])}/">{UI.e(r["number"])}</a> · '
                           f'{UI.e(r["created_utc"][:10])} · {UI.e(tt(*LO.SYMPTOM.get(r["symptom"], (r["symptom"], r["symptom"]))))} {_lc_pill(r["status"], tt)}</div>' for r in mine)
                 + (f'<div style="overflow-x:auto"><table class="t sortable">{_claim_head(tt, with_place=False)}{wrows}</table></div>' if wclaims
                    else f'<span class="muted">{tt("No warranty claim recorded for this building.", "Sin reclamos de garantía registrados para esta nave.")}</span>') + "</div>")
    same = [q for q in ds.projects if LT.park_key(q) == LT.park_key(p) and q.id != p.id]
    others = "".join(f'<tr><td>{_clogo(ds, q)}</td><td><a href="/lighting/p/{q.id}">{UI.e(q.customer)}</a></td><td class="nw">{UI.e(q.yardi or q.bldg or "-")}</td>'
                     f'<td class="n">{q.year or "-"}</td><td class="n">{UI.num(q.fixtures)}</td></tr>' for q in sorted(same, key=lambda q: -(q.year or 0)))
    body = f"""{_ltabs("list", tt, len(LT.pipeline(ds)))}
<section class="card phead"><div class="phlogo">{_clogo(ds, p, big=True)}</div>
<div class="phmain"><div class="kick">{_mk(p.market, tt)} · {UI.e(p.park)}</div><h1>{UI.e(p.customer)}</h1>
<div class="phfacts">{f'<span>{tt("Prologis code", "Código Prologis")}<b>{UI.e(p.yardi)}</b></span>' if p.yardi else ""}{f'<span>{tt("Building", "Nave")}<b>{UI.e(p.bldg)}</b></span>' if p.bldg else ""}<span>{tt("Type", "Tipo")}<b>{UI.e(tt(*LT.KINDS[p.kind]))}</b></span>
<span class="pill" style="background:{st[2]}22;color:#14211f">{UI.e(tt(*st[:2]))}</span></div></div>
<div class="phact">{_audit_btn(p.yardi, tt, small=False)}{_offer_btn(_latest_offer([f"P{p.id:03d}"]), tt)}</div></section>
<div class="stepper">{stepper}</div><p class="small muted" style="margin:0 0 6px">{tt("Steps with a document in ARGIA's project folders are filled in.", "Los pasos con documento en las carpetas de ARGIA aparecen llenos.")}</p>
<div class="grid g2 lg"><div class="card"><h2>{tt("The plant", "La planta")}</h2>{flash(pmsg) if pmsg else ""}{pic or tt("No position recorded.", "Sin posición registrada.")}</div>
<div class="card"><h2>{tt("Project", "Proyecto")}</h2><div style="overflow-x:auto"><table class="t">{ftab}</table></div></div></div>
<div class="grid g2e lg">{energy}<div class="card"><h2>{tt("Other projects in this park", "Otros proyectos en este parque")}</h2>
{f'<table class="t">{others}</table>' if others else f'<span class="muted">{tt("None yet.", "Ninguno aún.")}</span>'}</div></div>{_docs_card(p, tt)}{wcard}"""
    return page(p.customer, body, on="lighting")


@app.get("/lighting/customers/")
def lighting_customers():
    ds = _lds()
    tt, today = t(), today_mx()
    rows = LT.summary_rows(LT.group_by([p for p in ds.projects if p.group], lambda p: p.group), today)
    cname = {k: (ds.customers[k].name if k in ds.customers else k) for k, _ in rows}
    rows = sorted(rows, key=lambda r: _q(cname[r[0]]))                                 # v355: A-Z (accents ignored)
    cards = ""
    for k, t_ in rows:
        name = cname[k]
        parks = sorted({p.park for p in ds.projects if p.group == k})
        cards += (f'<a class="svc ccard" href="/lighting/buildings/?c={UI.e(k)}" data-n="{UI.e(_q(name))}" data-p="{UI.e(_q(" ".join(parks)))}">{_clogo(ds, k, name, size="mid")}'
                  f'<h3>{UI.e(name)}</h3><p>{UI.e(", ".join(parks[:4]))}{"…" if len(parks) > 4 else ""}</p>'
                  f'<div class="foot2"><span><b>{t_.projects}</b> {tt("project", "proyecto") if t_.projects == 1 else tt("projects", "proyectos")}</span><span><b>{UI.num(t_.fixtures)}</b> {tt("fixtures", "luminarias")}</span>'
                  f'<span><b>{UI.num(t_.mwh_saved)}</b> MWh/{tt("yr", "año")}</span></div></a>')
    other = [p for p in ds.delivered if not p.group]
    search = (f'<div class="csearch"><input type="search" id="csearch" autocomplete="off" placeholder="{tt("Customer name (first letters), or a park", "Nombre del cliente (primeras letras), o un parque")}" '
              f'aria-label="{tt("Search customers", "Buscar clientes")}"><span class="small muted" id="ccount">{len(rows)} {tt("customers", "clientes")}</span></div>'
              f'<p class="muted" id="cnone" hidden>{tt("No customer matches.", "Ningún cliente coincide.")}</p>')
    search_js = ("<script>(function(){var i=document.getElementById('csearch'),n=document.getElementById('ccount'),z=document.getElementById('cnone'),"
                 "cards=document.querySelectorAll('.ccard[data-n]');"
                 "function nq(s){return s.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase().trim();}"
                 # v355: the name must START with what is typed ("a" -> only names starting with A); from 3 letters a park
                 # whose name has a word starting with it also matches
                 "function hit(e,q){if(!q||e.dataset.n.indexOf(q)===0)return true;"
                 "return q.length>=3&&(' '+e.dataset.p).indexOf(' '+q)>=0;}"
                 "i.addEventListener('input',function(){var q=nq(i.value),k=0;cards.forEach(function(e){var m=hit(e,q);e.hidden=!m;if(m)k++;});"
                 f"n.textContent=q?k+' {tt('of', 'de')} '+cards.length+' {tt('customers', 'clientes')}':cards.length+' {tt('customers', 'clientes')}';"
                 "z.hidden=k>0;});})();</script>")
    body = f"""{_ltabs("cust", tt, len(LT.pipeline(ds)))}
<h1 class="pt">{tt("Customers", "Clientes")}</h1><p class="muted" style="margin-top:0">{len(rows)} {tt("companies in Prologis buildings relit by ARGIA (including projects in progress), A to Z. Logos are the companies' own; they identify the occupant at the time of the retrofit.", "empresas en naves de Prologis iluminadas por ARGIA (incluye proyectos en proceso), de la A a la Z. Los logotipos son de cada empresa; identifican al ocupante al momento del proyecto.")}</p>
{search}
<div class="shopgrid">{cards}</div>{search_js}
<p class="small muted">{tt("Not listed here: vacant spaces, exteriors, common areas and stock", "No listados aquí: espacios vacantes, exteriores, áreas comunes y stock")} ({len(other)} {tt("projects", "proyectos")}).</p>"""
    return page(tt("LED customers", "Clientes LED"), body, on="lighting")


def _occ_cell(r, ds, fp, tt) -> str:
    """v361: who occupies the building per ARGIA's records, and whether that company already has LED in other buildings."""
    occ = LT.occupants(r, ds, fp)
    if not occ:
        txt = tt("Vacant", "Vacante") if LT.vacant(r) else "-"
        return f'<td data-v="{UI.e(txt if txt != "-" else "")}"><span class="muted">{txt}</span></td>'
    parts = []
    for o in occ:
        badge = ""
        if o.led_elsewhere:
            n = o.led_elsewhere
            lbl = tt(f"LED in {n} other building" + ("s" if n > 1 else ""), f"LED en {n} nave" + ("s" if n > 1 else "") + " más")
            href = f"/lighting/buildings/?c={UI.e(o.group)}" if o.group else "/lighting/buildings/"
            badge = f' <a class="pill ledpill" href="{href}" title="{tt("Already an ARGIA LED customer in other Prologis buildings", "Ya es cliente LED de ARGIA en otras naves de Prologis")}">★ {lbl}</a>'
        here = f' <span class="small muted">({tt("part already LED", "parte ya en LED")})</span>' if o.source == "project" else ""
        parts.append(f'<div>{"<b>" if o.source == "opportunity" else ""}{UI.e(o.name)}{"</b>" if o.source == "opportunity" else ""}{here}{badge}</div>')
    return f'<td data-v="{UI.e(occ[0].name)}">{"".join(parts)}</td>'


@app.get("/lighting/pipeline/")
def lighting_pipeline():
    ds = _lds()
    tt, today = t(), today_mx()
    allp = LT.pipeline(ds)
    stf, mf = request.args.get("s", ""), request.args.get("m", "")
    rows_ = [r for r in allp if (not stf or r.status == stf) and (not mf or r.building.market == mf)]
    it = LT.intensity(ds)
    pot = LT.potential(rows_, it, today.year)
    cnt = {k: sum(1 for s in allp if s.status == k) for k in LT.BSTATUS}
    pts = [{"lat": r.building.lat, "lon": r.building.lon, "c": LT.BSTATUS[r.status][2],
            "r": max(5, min(16, (r.building.area_m2 or 5000) ** 0.5 / 14)),
            "t": f"{r.building.name or r.building.yardi} ({r.building.yardi})",
            "s": f"{tt(*LT.BSTATUS[r.status][:2])} · {UI.num(r.building.area_m2)} m²",
            "u": LT.google_maps_url(r.building.lat, r.building.lon), "l": tt("Open in Google Maps", "Abrir en Google Maps"),
            "ph": sat_photo(r.building.lat, r.building.lon, r.building.geo == "building", 480, 270)}
           for r in rows_ if r.building.lat is not None]
    rows = ""
    fp = LT.footprint(ds)
    for r in rows_:
        b = r.building
        last = max((o.last_proforma for o in r.opportunities if o.last_proforma), default=None)
        est = (r.remaining_m2 or 0) * it.kwh_per_m2 / 1000 if it else None
        sh = r.share or 0
        sat = (f'<a href="{UI.e(LT.google_maps_url(b.lat, b.lon))}" target="_blank" rel="noopener" title="{tt("Satellite view", "Vista satelital")}">🛰</a>'
               if b.lat is not None else "")
        aud = any(o.audit or o.study or o.proforma for o in r.opportunities) or any(q.docs.get("audit") and not q.delivered for q in r.projects)
        rows += (f'<tr><td class="nw"><b>{UI.e(b.yardi)}</b></td><td>{UI.e(b.name or "-")}<div class="small muted">{UI.e(b.park or "")}</div></td>'
                 f'{_occ_cell(r, ds, fp, tt)}<td>{_mk(b.market, tt) if b.market else "-"}<div class="small muted">{UI.e(b.city or "")}</div></td><td class="n">{UI.num(b.area_m2)}</td>'
                 f'<td style="min-width:110px"><div class="hbar"><i style="width:{100 * sh:.0f}%"></i></div><span class="small muted">{UI.num(100 * sh)} % LED</span></td>'
                 f'<td class="n">{UI.num(r.remaining_m2)}</td><td><span class="pill" style="background:{LT.BSTATUS[r.status][2]}22;color:#14211f">{UI.e(tt(*LT.BSTATUS[r.status][:2]))}</span></td>'
                 f'<td>{tt("yes", "sí") if aud else tt("no", "no")}</td><td class="nw" data-v="{UI.e(last or "")}">{UI.e(last or "-")} {_offer_btn(_latest_offer([f"O{o.id:03d}" for o in r.opportunities]), tt)}</td>'
                 f'<td class="n">{UI.num(est) if est is not None else "-"}</td><td class="nw">{sat} {_audit_btn(b.yardi, tt)}</td></tr>')
    tabs2 = "".join(f'<a class="tab{" on" if stf == k else ""}" href="?s={k}&m={UI.e(mf)}">{UI.e(tt(*LT.BSTATUS[k][:2]))}<b>{cnt[k]}</b></a>'
                    for k in ("in_progress", "audited", "partial", "not_started"))
    mopts = "".join(f'<option value="{k}"{" selected" if mf == k else ""}>{UI.e(tt(*v))}</option>' for k, v in LT.MARKETS.items()
                    if any(r.building.market == k for r in allp))
    csv_link = f'<a class="r" href="/lighting/pipeline.csv?s={UI.e(stf)}&m={UI.e(mf)}">CSV ›</a>' if S.can(g.user["role"], "export") else ""
    kp = [(tt("Buildings", "Naves"), UI.num(len(rows_)), ""), (tt("Floor area to convert", "Superficie por convertir"), UI.num(pot["m2"] / 1e6, 2), "M m²"),
          (tt("Potential saving (estimate)", "Ahorro potencial (estimado)"), UI.num(pot["mwh"] / 1000, 1), f"GWh/{tt('yr', 'año')}"),
          (tt("CO₂e potential (estimate)", "CO₂e potencial (estimado)"), UI.num(pot["co2_t"]), f"t/{tt('yr', 'año')}"),
          (tt("LED fixtures (estimate)", "Luminarias LED (estimado)"), UI.num(pot["fixtures"]), "")]
    kpis = "".join(f'<div class="kpi"><div class="l">{l}</div><div class="v">{v}<small>{u}</small></div></div>' for l, v, u in kp)
    basis = (tt(f"Estimate = area still to convert x the median design saving of {it.n} delivered projects ({UI.num(it.kwh_per_m2, 1)} kWh/m² per year, {UI.num(it.fixtures_per_1000m2, 1)} fixtures per 1,000 m²).",
                f"Estimado = superficie por convertir x el ahorro de diseño mediano de {it.n} proyectos entregados ({UI.num(it.kwh_per_m2, 1)} kWh/m² al año, {UI.num(it.fixtures_per_1000m2, 1)} luminarias por 1,000 m²).")
             if it else "")
    body = f"""{_ltabs("pipe", tt, len(allp))}
<section class="hero"><div class="glow"></div><div class="k">{tt("Not yet on LED", "Aún sin LED")}</div>
<h1>{tt("Prologis buildings still to convert", "Naves de Prologis por convertir")}</h1>
<div class="sub">{tt("Prologis property database, locations of 5,000 m² and more, without an ARGIA LED project or converted only in part. Audited buildings have an ARGIA audit, lighting study or proforma.", "Base de propiedades de Prologis, ubicaciones de 5,000 m² o más, sin proyecto LED de ARGIA o convertidas solo en parte. Las auditadas tienen auditoría, estudio lumínico o proforma de ARGIA.")}</div>
<div class="kpis">{kpis}</div></section>
<div class="tabs"><a class="tab{" on" if not stf else ""}" href="?m={UI.e(mf)}">{tt("All", "Todas")}<b>{len(allp)}</b></a>{tabs2}
<form method="get" style="margin-left:auto"><input type="hidden" name="s" value="{UI.e(stf)}"><select name="m" onchange="this.form.submit()" style="width:auto"><option value="">{tt("All cities", "Todas las ciudades")}</option>{mopts}</select></form></div>
<div class="card"><h2 style="flex-wrap:wrap">{tt("Map", "Mapa")}<span class="r legend">{"".join(f'<span><i style="background:{LT.BSTATUS[k][2]}"></i>{UI.e(tt(*LT.BSTATUS[k][:2]))}</span>' for k in ("in_progress", "audited", "partial", "not_started"))}</span></h2>{_lmap(pts, "460px")}</div>
<div class="card" style="margin-top:16px"><h2>{tt("Buildings", "Naves")}{csv_link}</h2><div style="overflow-x:auto"><table class="t sortable"><tr><th>{tt("Code", "Código")}</th><th>{tt("Building", "Nave")}</th><th>{tt("Customer", "Cliente")}</th><th>{tt("City", "Ciudad")}</th>
<th class="n">m²</th><th>{tt("On LED", "En LED")}</th><th class="n">{tt("To convert m²", "Por convertir m²")}</th><th>{tt("Status", "Estado")}</th><th>{tt("ARGIA audit of the rest", "Auditoría ARGIA del resto")}</th><th>{tt("Last proforma", "Última proforma")}</th>
<th class="n">MWh/{tt("yr", "año")} ({tt("est.", "est.")})</th><th></th></tr>{rows or f'<tr><td colspan="12" class="muted">{tt("Nothing to show.", "Nada que mostrar.")}</td></tr>'}</table></div></div>
<p class="small muted">{tt("Customer = the company in ARGIA's records for the building (bold: the audit or proforma; others: part of the building already relit). ★ = that company already has ARGIA LED in other Prologis buildings. A building without such a record shows -; it does not mean it is empty.", "Cliente = la empresa en los registros de ARGIA para la nave (negrita: la auditoría o proforma; otras: parte de la nave ya iluminada). ★ = esa empresa ya tiene LED de ARGIA en otras naves de Prologis. Una nave sin ese registro muestra -; no significa que esté vacía.")}</p>
<p class="small muted">{tt("Partly converted = ARGIA has delivered LED in part of the building (less than 80 % of its area); it does not mean the rest was audited - see the audit column.", "Convertida en parte = ARGIA entregó LED en parte de la nave (menos del 80 % de su superficie); no significa que el resto esté auditado - ver la columna de auditoría.")} {basis} {tt("No ARGIA record does not prove a building has no LED: a tenant or another contractor may have converted it. Buildings without a recorded area are not listed.", "Que ARGIA no tenga registro no prueba que la nave no tenga LED: un inquilino u otro contratista pudo convertirla. Las naves sin superficie registrada no se listan.")}</p>"""
    return page(tt("Not yet on LED", "Aún sin LED"), body, on="lighting")


@app.get("/lighting/warranty/")
def lighting_warranty():
    ds = _lds()
    reg = warranty_reg()
    if reg is None:
        abort(404)
    tt, today = t(), today_mx()
    stf = request.args.get("s", "")
    stf = stf if stf in WR.STATUSES else ""
    mf = request.args.get("m", "")
    mf = mf if mf in LT.MARKETS else ""
    q = request.args.get("q", "")[:60]
    rows_ = WR.filtered(reg.claims, stf, mf, q)
    S_ = WR.summary(reg.claims)
    kp = [(tt("Warranty claims", "Reclamos de garantía"), UI.num(S_.claims), "", f"{S_.buildings} {tt('buildings', 'naves')}"),
          (tt("Fixtures replaced or repaired", "Luminarias reemplazadas o reparadas"), UI.num(S_.fixtures), "",
           tt(f"of {UI.num(LT.totals(ds.projects, today).fixtures)} installed", f"de {UI.num(LT.totals(ds.projects, today).fixtures)} instaladas")),
          (tt("Open now", "Abiertos ahora"), UI.num(S_.open), "", ""),
          (tt("Median days to close", "Mediana de días al cierre"), UI.num(S_.median_days) if S_.median_days is not None else "-", "",
           (tt(f"{UI.num(100 * S_.within_30)} % closed within 30 days", f"{UI.num(100 * S_.within_30)} % cerrados en 30 días") if S_.within_30 is not None else ""))]
    kpis = "".join(f'<div class="kpi"><div class="l">{l}</div><div class="v">{v}<small>{u}</small></div><div class="d">{d}</div></div>' for l, v, u, d in kp)
    years = WR.per_year(reg.claims)
    chart = UI.bar_chart([(str(y), n, None) for y, n, _ in years], unit=tt("claims", "reclamos"), h=190)
    cnt = {k: sum(1 for c in reg.claims if c.status == k and (not mf or c.market == mf)) for k in WR.STATUSES}
    tabs2 = "".join(f'<a class="tab{" on" if stf == k else ""}" href="?s={k}&m={UI.e(mf)}&q={UI.e(q)}">{UI.e(tt(*WR.STATUSES[k][:2]))}<b>{cnt[k]}</b></a>' for k in ("open", "closed"))
    mopts = "".join(f'<option value="{k}"{" selected" if k == mf else ""}>{UI.e(tt(*v))}</option>' for k, v in LT.MARKETS.items()
                    if any(c.market == k for c in reg.claims))
    rows = "".join(_claim_row(c, tt, today) for c in rows_)
    terms = [tt(f"LED fixtures carry the manufacturer's {reg.years}-year limited warranty, counted from the installation (Acuity Brands; the handover date is shown on each project page).",
                f"Las luminarias LED tienen la garantía limitada del fabricante de {reg.years} años desde la instalación (Acuity Brands; la fecha de entrega está en cada proyecto)."),
             tt("Report a failure to ARGIA with the W1 claim form and photos or a video of the fixture; without them the manufacturer can reject the claim.",
                "Reporte la falla a ARGIA con el formato de reclamo W1 y fotos o video de la luminaria; sin ellos el fabricante puede rechazar el reclamo."),
             tt("Each claim gets a W- ticket number; keep it in the e-mail subject so the whole history stays in one thread.",
                "Cada reclamo recibe un número de ticket W-; manténgalo en el asunto del correo para conservar todo el historial en un hilo."),
             tt("ARGIA confirms the claim, files it with the manufacturer, installs the replacement and closes the ticket with a short report (cause found, fixtures replaced).",
                "ARGIA confirma el reclamo, lo tramita con el fabricante, instala el reemplazo y cierra el ticket con un reporte breve (causa encontrada, luminarias reemplazadas).")]
    body = f"""{_ltabs("warr", tt, len(LT.pipeline(ds)))}
<section class="hero"><div class="glow"></div><div class="k">{tt("Warranty", "Garantías")}</div>
<h1>{tt("Warranty claims on the LED fixtures", "Reclamos de garantía de las luminarias LED")}</h1>
<div class="sub">{tt("Every failure reported on an ARGIA LED installation in a Prologis building, from the report to the replacement.", "Cada falla reportada en una instalación LED de ARGIA en una nave de Prologis, del reporte al reemplazo.")}</div>{f'<div style="margin-top:12px"><a class="btn" href="/lighting/warranty/new">{tt("Report a failed fixture", "Reportar una luminaria con falla")}</a></div>' if S.can(g.user["role"], "light_claim_new") else ""}
<div class="kpis">{kpis}</div></section>
{_portal_claims_card(tt)}
{_coverage_card(ds, reg, tt, today)}
<div class="grid g2 lg"><div class="card"><h2>{tt("Claims per year", "Reclamos por año")}</h2>{chart}
<p class="small muted" style="margin:4px 0 0">{tt("By the year the failure was reported.", "Por el año en que se reportó la falla.")}</p></div>
<div class="card"><h2>{tt("How the warranty works", "Cómo funciona la garantía")}</h2><ol class="small" style="margin:0;padding-left:18px;line-height:1.55">{"".join(f"<li>{UI.e(x)}</li>" for x in terms)}</ol></div></div>
<div class="tabs" style="margin-top:16px"><a class="tab{" on" if not stf else ""}" href="?m={UI.e(mf)}&q={UI.e(q)}">{tt("All", "Todos")}<b>{sum(cnt.values())}</b></a>{tabs2}
<form method="get" class="lfilt" style="margin:0 0 0 auto"><input type="hidden" name="s" value="{UI.e(stf)}"><input type="search" name="q" value="{UI.e(q)}" placeholder="{tt("Search code, park, customer, model", "Buscar código, parque, cliente, modelo")}">
<select name="m" onchange="this.form.submit()" style="width:auto"><option value="">{tt("All cities", "Todas las ciudades")}</option>{mopts}</select></form></div>
<div class="card"><h2>{tt("Claims", "Reclamos")}<span class="r muted">{len(rows_)}</span></h2><div style="overflow-x:auto"><table class="t sortable">{_claim_head(tt)}
{rows or f'<tr><td colspan="13" class="muted">{tt("No claim matches these filters.", "Ningún reclamo coincide con los filtros.")}</td></tr>'}</table></div></div>
<p class="small muted">{tt("Source: ARGIA's warranty trackers (claims 2022 to August 2025, and the W- ticket sheet since then). Days = report to replacement; for an open claim, days since the report (+). A building code is shown when the tracker names the building; older claims name the customer and city only.", "Fuente: registros de garantías de ARGIA (reclamos 2022 a agosto 2025, y la hoja de tickets W- desde entonces). Días = del reporte al reemplazo; para un reclamo abierto, días desde el reporte (+). El código de nave aparece cuando el registro nombra la nave; los reclamos antiguos solo nombran cliente y ciudad.")} {tt("Data as of", "Datos al")} {UI.e(reg.generated)}.</p>"""
    return page(tt("LED warranty", "Garantías LED"), body, on="lighting")


# ------------------------------------------------------------------ v356: warranty claims in the portal, warranty periods, calendar
def _bopts(ds: LT.Dataset, cur: str = "") -> str:
    """Building picker: every Prologis building code ARGIA has worked in (code · park · customers)."""
    by: Dict[str, List[LT.Project]] = {}
    for p in ds.projects:
        if p.yardi:
            by.setdefault(p.yardi, []).append(p)
    out = []
    for code in sorted(by):
        ps = by[code]
        who = ", ".join(sorted({p.customer for p in ps if p.kind == "tenant"}))[:60]
        out.append(f'<option value="{UI.e(code)}"{" selected" if code == cur else ""}>{UI.e(code)} · {UI.e(ps[0].park)} · {_mk(ps[0].market, t())}'
                   f'{(" · " + UI.e(who)) if who else ""}</option>')
    return "".join(out)


def _bname(ds: Optional[LT.Dataset], code: str) -> str:
    ps = [p for p in ds.projects if p.yardi == code] if ds else []
    return f"{ps[0].park} ({LT.MARKETS[ps[0].market][0]})" if ps else ""


def _lc_pill(status: str, tt) -> str:
    en, es, col = LO.CLAIM_STATUS.get(status, (status, status, "#9aa4ad"))
    return f'<span class="pill" style="background:{col}22;color:#14211f">{UI.e(tt(en, es))}</span>'


def _portal_claims_card(tt) -> str:
    rows = LO.claims(db())
    btn = (f'<a class="btn sm" href="/lighting/warranty/new">{tt("Report a failure", "Reportar una falla")}</a>'
           if S.can(g.user["role"], "light_claim_new") else "")
    body = "".join(f'<tr><td class="nw"><a href="/lighting/warranty/c/{UI.e(r["number"])}/">{UI.e(r["number"])}</a></td>'
                   f'<td class="nw">{UI.e(r["created_utc"][:10])}</td><td class="nw">{UI.e(r["code"])}</td><td>{UI.e(r["customer"] or "-")}</td>'
                   f'<td class="n">{r["fixtures"] or "-"}</td><td class="small">{UI.e(tt(*LO.SYMPTOM.get(r["symptom"], (r["symptom"], r["symptom"]))))}</td>'
                   f'<td class="nw">{UI.e(r["w_ticket"] or "-")}</td><td>{_lc_pill(r["status"], tt)}</td></tr>' for r in rows)
    table = (f'<div style="overflow-x:auto"><table class="t sortable"><tr><th>{tt("Claim", "Reclamo")}</th><th>{tt("Reported", "Reportado")}</th>'
             f'<th>{tt("Prologis building code", "Código Prologis")}</th><th>{tt("Customer", "Cliente")}</th><th class="n">{tt("Fixtures", "Luminarias")}</th>'
             f'<th>{tt("What happens", "Qué pasa")}</th><th>{tt("ARGIA ticket", "Ticket ARGIA")}</th><th>{tt("Status", "Estado")}</th></tr>{body}</table></div>'
             if rows else f'<p class="muted" style="margin:0">{tt("No claim has been reported in the portal yet.", "Aún no se ha reportado ningún reclamo en el portal.")}</p>')
    return (f'<div class="card" style="margin-bottom:16px"><h2>{tt("Claims reported in the portal", "Reclamos reportados en el portal")}<span class="r">{btn}</span></h2>'
            f'<p class="small muted" style="margin-top:0">{tt("Report a failed LED fixture here: ARGIA receives it at once, answers in the claim and keeps its status up to date.", "Reporte aquí una luminaria LED con falla: ARGIA lo recibe al momento, responde en el reclamo y mantiene su estado al día.")}</p>{table}</div>')


def _coverage_card(ds: LT.Dataset, reg: Optional[WR.Register], tt, today: dt.date) -> str:
    """v356: the 5-year warranty period of every delivered building, ending soonest first."""
    cov = LO.coverage(ds.projects, today, reg.years if reg else 5)
    byid = {p.id: p for p in ds.projects}
    n = {k: sum(1 for c in cov if c.state == k) for k in ("in", "ending", "ended")}
    lab = {"in": (tt("In warranty", "En garantía"), "#23B2A9"), "ending": (tt("Ends within 12 months", "Termina en 12 meses"), "#e0a100"),
           "ended": (tt("Warranty ended", "Garantía terminada"), "#9aa4ad")}
    order = sorted(cov, key=lambda c: ({"ending": 0, "in": 1, "ended": 2}[c.state], c.until))
    rows = "".join(f'<tr><td class="nw"><a href="/lighting/p/{c.project_id}">{UI.e(c.code or "-")}</a></td><td>{UI.e(byid[c.project_id].park)}</td>'
                   f'<td>{_mk(byid[c.project_id].market, tt)}</td><td>{UI.e(byid[c.project_id].customer)}</td><td class="nw">{UI.e(c.handover)}</td>'
                   f'<td class="nw">{UI.e(c.until)}</td><td class="n">{c.days_left if c.days_left >= 0 else "-"}</td>'
                   f'<td><span class="pill" style="background:{lab[c.state][1]}22;color:#14211f">{UI.e(lab[c.state][0])}</span></td></tr>' for c in order)
    chips = "".join(f'<span class="chip"><b>{n[k]}</b> {UI.e(lab[k][0])}</span>' for k in ("in", "ending", "ended"))
    return (f'<div class="card" style="margin-bottom:16px"><h2>{tt("Warranty period of every building", "Periodo de garantía de cada nave")}</h2>'
            f'<p class="small muted" style="margin-top:0">{tt("5 years from the handover of each delivered project; projects without a handover date are not listed.", "5 años desde la entrega de cada proyecto; los proyectos sin fecha de entrega no aparecen.")}</p>'
            f'<div class="lchips">{chips}</div><div class="sitebox" style="max-height:360px"><div><table class="t sortable"><tr><th>{tt("Prologis building code", "Código Prologis")}</th>'
            f'<th>{tt("Park", "Parque")}</th><th>{tt("City", "Ciudad")}</th><th>{tt("Customer", "Cliente")}</th><th>{tt("Handover", "Entrega")}</th>'
            f'<th>{tt("Warranty until", "Garantía hasta")}</th><th class="n">{tt("Days left", "Días restantes")}</th><th>{tt("Status", "Estado")}</th></tr>{rows}</table></div></div></div>')


@app.get("/lighting/warranty/new")
def light_claim_new(msg: str = ""):
    ds = _lds()
    need("light_claim_new")
    tt = t()
    f = request.form if request.method == "POST" else request.args
    code = (f.get("code") or "").upper()
    syms = "".join(f'<option value="{k}"{" selected" if f.get("symptom") == k else ""}>{UI.e(tt(en, es))}</option>' for k, en, es in LO.SYMPTOMS)
    body = f"""{_ltabs("warr", tt, len(LT.pipeline(ds)))}
<div class="card form"><div class="kick">{tt("LED warranty", "Garantía LED")}</div><h1 class="pt">{tt("Report a failed fixture", "Reportar una luminaria con falla")}</h1>{flash(msg, True)}
<p class="small muted">{tt("ARGIA receives the claim at once. Photos or a short video of the fixture are needed for the manufacturer; add them here or later in the claim.", "ARGIA recibe el reclamo al momento. El fabricante necesita fotos o un video corto de la luminaria; agréguelos aquí o después en el reclamo.")}</p>
<form method="post" action="/lighting/warranty/new" enctype="multipart/form-data">{csrf_field()}
<label>{tt("Building (Prologis building code)", "Nave (código Prologis)")}</label><select name="code" required><option value="">-</option>{_bopts(ds, code)}</select>
<div class="row"><div><label>{tt("Fixtures affected", "Luminarias afectadas")}</label><input name="fixtures" inputmode="numeric" value="{UI.e(f.get("fixtures", ""))}"></div>
<div><label>{tt("What happens", "Qué pasa")}</label><select name="symptom" required>{syms}</select></div></div>
<label>{tt("Where in the building (area, aisle, office...)", "Dónde en la nave (área, pasillo, oficina...)")}</label><input name="area" maxlength="160" value="{UI.e(f.get("area", ""))}">
<label>{tt("Description", "Descripción")}</label><textarea name="desc">{UI.e(f.get("desc", ""))}</textarea>
<label>{tt("Contact on site (name, phone) for the visit", "Contacto en sitio (nombre, teléfono) para la visita")}</label><input name="contact" maxlength="200" value="{UI.e(f.get("contact", ""))}">
<label>{tt("Photo or video (optional)", "Foto o video (opcional)")}</label><input type="file" name="file" accept="image/*,video/*,.pdf">
<div style="margin-top:14px"><button class="btn">{tt("Send to ARGIA", "Enviar a ARGIA")}</button> <a class="btn ghost" href="/lighting/warranty/">{tt("Cancel", "Cancelar")}</a></div></form></div>"""
    return page(tt("Report a failure", "Reportar una falla"), body, on="lighting")


@app.post("/lighting/warranty/new")
def light_claim_new_post():
    ds = _lds()
    need("light_claim_new")
    tt = t()
    f = request.form
    code = (f.get("code") or "").strip().upper()
    if not any(p.yardi == code for p in ds.projects):
        return light_claim_new(tt("Choose the building from the list.", "Elija la nave de la lista."))
    custs = sorted({p.customer for p in ds.projects if p.yardi == code and p.kind == "tenant"})
    c = db()
    try:
        cl = LO.create_claim(c, code, ", ".join(custs), f.get("fixtures"), f.get("area", ""), f.get("symptom", ""), f.get("desc", ""),
                             f.get("contact", ""), g.user["username"], ip(), today_mx())
    except LO.Invalid as ex:
        return light_claim_new(tt(f"Please check: {ex}.", f"Revise: {ex}."))
    did = _upload(request.files.get("file"), code, "lighting") if request.files.get("file") else None
    if did:
        LO.add_claim_event(c, cl["id"], g.user["username"], "file", "", {"doc": did})
    subj, body = LO.claim_mail(cl, _bname(ds, code), "https://prologis.argia.com.mx", f'{g.user["name"] or g.user["username"]} ({g.user["org"]})')
    AL.queue(c, "light_claim", cl["number"], subj, body, AL.recipients(c, "critical"), False)
    return redirect(f"/lighting/warranty/c/{cl['number']}/")


def _light_claim(number: str):
    cl = LO.claim(db(), number)
    if cl is None:
        abort(404)
    return cl


@app.get("/lighting/warranty/c/<number>/")
def light_claim_page(number, msg: str = ""):
    ds = _lds()
    tt = t()
    cl = _light_claim(number)
    c = db()
    files = {d["id"]: d for d in c.execute("SELECT * FROM documents WHERE folder='lighting' AND site_code=? AND deleted=0", (cl["code"],))}
    tl = ""
    for ev in LO.claim_events(c, cl["id"]):
        m = json.loads(ev["meta"] or "{}")
        what = {"created": tt("reported the failure", "reportó la falla"), "comment": tt("commented", "comentó"),
                "status": f'{tt("moved it to", "lo pasó a")} <b>{UI.e(tt(*LO.CLAIM_STATUS.get(m.get("to", ""), ("", "", ""))[:2]))}</b>'
                          + (f' · {tt("ARGIA ticket", "ticket ARGIA")} <b>{UI.e(m["w"])}</b>' if m.get("w") else ""),
                "file": tt("attached a file", "adjuntó un archivo"),
                "visit": f'{tt("scheduled a visit for", "programó una visita para")} <b>{UI.e(m.get("date", ""))}</b>'}.get(ev["kind"], ev["kind"])
        att = ""
        if ev["kind"] == "file" and m.get("doc") in files:
            d = files[m["doc"]]
            att = f'<div><a class="chip" href="/docs/{d["id"]}/download">📎 {UI.e(d["name"])}</a></div>'
        tl += (f'<div class="ev"><div class="small muted">{mx_time(ev["ts_utc"])} · <b>{UI.e(ev["username"])}</b> {what}</div>'
               + (f'<div>{UI.e(ev["body"])}</div>' if ev["body"] else "") + f'{att}</div>')
    acts = ""
    if S.can(g.user["role"], "light_work"):
        btns = "".join(f'<button class="btn sm ghost" name="to" value="{k}">{UI.e(tt(*LO.CLAIM_STATUS[k][:2]))}</button> ' for k in LO.CLAIM_NEXT.get(cl["status"], ()))
        acts += (f'<form method="post" action="/lighting/warranty/c/{UI.e(number)}/status">{csrf_field()}<label>{tt("Move to", "Mover a")}</label>'
                 f'<div class="row"><input name="note" placeholder="{tt("note for Prologis (optional)", "nota para Prologis (opcional)")}">'
                 f'<input name="w" value="{UI.e(cl["w_ticket"])}" placeholder="W-1234-00" style="max-width:140px"></div><div style="margin-top:8px">{btns}</div></form>'
                 f'<form method="post" action="/lighting/warranty/c/{UI.e(number)}/visit" style="margin-top:12px;display:flex;gap:6px;align-items:end;flex-wrap:wrap">{csrf_field()}'
                 f'<div><label>{tt("Visit date", "Fecha de visita")}</label><input name="date" type="date" required></div>'
                 f'<div><label>{tt("Time", "Hora")}</label><input name="slot" placeholder="09:00" style="width:90px"></div>'
                 f'<button class="btn sm">{tt("Schedule visit", "Programar visita")}</button></form>')
    if S.can(g.user["role"], "comment"):
        acts += (f'<form method="post" action="/lighting/warranty/c/{UI.e(number)}/comment" enctype="multipart/form-data" style="margin-top:12px">{csrf_field()}'
                 f'<label>{tt("Comment, photo or video", "Comentario, foto o video")}</label><textarea name="body"></textarea>'
                 f'<input type="file" name="file" accept="image/*,video/*,.pdf" style="margin-top:6px"><div style="margin-top:8px"><button class="btn sm">{tt("Add", "Agregar")}</button></div></form>')
    proj = [p for p in ds.projects if p.yardi == cl["code"]]
    facts = [(tt("Prologis building code", "Código Prologis"), f'<a href="/lighting/buildings/?q={UI.e(cl["code"])}">{UI.e(cl["code"])}</a>'),
             (tt("Park", "Parque"), UI.e(proj[0].park if proj else "-")), (tt("Customer", "Cliente"), UI.e(cl["customer"] or "-")),
             (tt("Fixtures affected", "Luminarias afectadas"), UI.e(str(cl["fixtures"] or "-"))), (tt("Where", "Dónde"), UI.e(cl["area"] or "-")),
             (tt("What happens", "Qué pasa"), UI.e(tt(*LO.SYMPTOM.get(cl["symptom"], (cl["symptom"], cl["symptom"]))))),
             (tt("Contact on site", "Contacto en sitio"), UI.e(cl["contact"] or "-")), (tt("ARGIA ticket", "Ticket ARGIA"), UI.e(cl["w_ticket"] or "-")),
             (tt("Reported", "Reportado"), mx_time(cl["created_utc"])), (tt("Closed", "Cerrado"), mx_time(cl["closed_utc"]))]
    ftab = "".join(f'<tr><td class="muted">{k}</td><td><b>{v}</b></td></tr>' for k, v in facts)
    body = f"""{_ltabs("warr", tt, len(LT.pipeline(ds)))}
<div class="kick">{tt("LED warranty claim", "Reclamo de garantía LED")} · {UI.e(cl["number"])}</div><h1 class="pt">{UI.e(cl["code"])} · {UI.e(tt(*LO.SYMPTOM.get(cl["symptom"], (cl["symptom"], cl["symptom"]))))}</h1>
<div style="margin:6px 0 12px">{_lc_pill(cl["status"], tt)}</div>{flash(msg, True)}
<div class="grid g2 lg"><div class="card"><h2>{tt("The claim", "El reclamo")}</h2><table class="t">{ftab}</table>
{f'<p style="white-space:pre-wrap">{UI.e(cl["description"])}</p>' if cl["description"] else ""}</div>
<div class="card"><h2>{tt("Timeline", "Historial")}</h2><div class="tl">{tl}</div>{acts}</div></div>"""
    return page(cl["number"], body, on="lighting")


@app.post("/lighting/warranty/c/<number>/status")
def light_claim_status(number):
    need("light_work")
    tt = t()
    cl = _light_claim(number)
    c = db()
    try:
        new = LO.set_claim_status(c, number, request.form.get("to", ""), g.user["username"], request.form.get("note", ""),
                                  request.form.get("w", ""), ip())
    except LO.Invalid as ex:
        return light_claim_page(number, tt(f"Not possible: {ex}.", f"No es posible: {ex}."))
    to = [r["email"] for r in c.execute("SELECT email FROM users WHERE username=? AND email<>''", (cl["created_by"],))]
    if to:
        st = LO.CLAIM_STATUS[new["status"]][0]
        AL.queue(c, "light_claim_status", number, f"[Prologis LED warranty] {number} - {st}",
                 f"Your warranty claim {number} ({new['code']}) is now: {st}.\n{request.form.get('note', '')}\n\n"
                 f"https://prologis.argia.com.mx/lighting/warranty/c/{number}/\n", to, False)
    return redirect(f"/lighting/warranty/c/{number}/")


@app.post("/lighting/warranty/c/<number>/comment")
def light_claim_comment(number):
    need("comment")
    cl = _light_claim(number)
    c = db()
    did = _upload(request.files.get("file"), cl["code"], "lighting") if request.files.get("file") else None
    body = (request.form.get("body") or "").strip()
    if body:
        LO.add_claim_event(c, cl["id"], g.user["username"], "comment", body)
        S.audit(c, g.user["username"], ip(), "light_claim_comment", number)
    if did:
        LO.add_claim_event(c, cl["id"], g.user["username"], "file", "", {"doc": did})
    return redirect(f"/lighting/warranty/c/{number}/")


@app.post("/lighting/warranty/c/<number>/visit")
def light_claim_visit(number):
    need("light_work")
    tt = t()
    cl = _light_claim(number)
    c = db()
    d = request.form.get("date", "")
    try:
        LO.add_event(c, "warranty", cl["code"], f"{tt('Warranty visit', 'Visita de garantía')} {number}", d, d,
                     request.form.get("slot", ""), "confirmed", "", g.user["username"], ref=number, ip=ip())
    except LO.Invalid as ex:
        return light_claim_page(number, tt(f"Not possible: {ex}.", f"No es posible: {ex}."))
    LO.add_claim_event(c, cl["id"], g.user["username"], "visit", request.form.get("slot", ""), {"date": d})
    if "SCHEDULED" in LO.CLAIM_NEXT.get(cl["status"], ()):
        LO.set_claim_status(c, number, "SCHEDULED", g.user["username"], "", "", ip())
    to = [r["email"] for r in c.execute("SELECT email FROM users WHERE username=? AND email<>''", (cl["created_by"],))]
    if to:
        AL.queue(c, "light_claim_status", number, f"[Prologis LED warranty] {number} - visit on {d}",
                 f"ARGIA will visit {cl['code']} on {d} {request.form.get('slot', '')} for warranty claim {number}.\n\n"
                 f"https://prologis.argia.com.mx/lighting/warranty/c/{number}/\n", to, False)
    return redirect(f"/lighting/warranty/c/{number}/")


# ------------------------------------------------------------------ v356: lighting calendar
@app.get("/lighting/calendar/")
def light_calendar():
    ds = _lds()
    tt, today = t(), today_mx()
    y, m = LO.month_of(request.args.get("mo", ""), today)
    weeks = LO.month_grid(y, m)
    first, last = weeks[0][0], weeks[-1][-1]
    ents = LO.entries(db(), first, last, ds.projects)
    up = [e for e in LO.entries(db(), today, today + dt.timedelta(days=60), ds.projects) if e.end >= today]
    names = {p.yardi: p.park for p in ds.projects if p.yardi}

    def chip(e: LO.Entry) -> str:
        en, es, col = LO.EVENT_KIND[e.kind]
        dim = ";opacity:.55" if e.status == "requested" else ""
        return (f'<a class="calev" href="{UI.e(e.href)}" style="border-left-color:{col}{dim}" title="{UI.e(e.title)}">'
                f'{UI.e(e.slot + " " if e.slot else "")}{UI.e(e.code or tt(en, es))}<span>{UI.e(e.title)}</span></a>')
    days = "".join(f'<div class="calh">{d}</div>' for d in (tt("Mon", "Lun"), tt("Tue", "Mar"), tt("Wed", "Mié"), tt("Thu", "Jue"), tt("Fri", "Vie"), tt("Sat", "Sáb"), tt("Sun", "Dom")))
    for w in weeks:
        for d in w:
            here = "".join(chip(e) for e in ents if e.start <= d <= e.end)
            cls = "cald" + (" out" if d.month != m else "") + (" today" if d == today else "")
            days += f'<div class="{cls}"><div class="calnum">{d.day}</div>{here}</div>'
    py, pm = LO.shift_month(y, m, -1)
    ny, nm = LO.shift_month(y, m, 1)
    legend = "".join(f'<span><i style="background:{col}"></i>{UI.e(tt(en, es))}</span>' for k, en, es, col in LO.EVENT_KINDS)
    can_add = S.can(g.user["role"], "light_work")
    can_req = S.can(g.user["role"], "light_request")
    btn = (f'<a class="btn sm" href="/lighting/calendar/new">{tt("Add work on site", "Agregar trabajo en sitio")}</a>' if can_add else
           f'<a class="btn sm" href="/lighting/calendar/new">{tt("Request an audit", "Solicitar una auditoría")}</a>' if can_req else "")
    ups = "".join(f'<tr><td class="nw">{UI.e(e.start.isoformat())}{(" - " + UI.e(e.end.isoformat())) if e.end != e.start else ""}</td>'
                  f'<td><span class="pill" style="background:{LO.EVENT_KIND[e.kind][2]}22;color:#14211f">{UI.e(tt(*LO.EVENT_KIND[e.kind][:2]))}</span></td>'
                  f'<td class="nw">{UI.e(e.code or "-")}</td><td>{UI.e(names.get(e.code, ""))}</td><td><a href="{UI.e(e.href)}">{UI.e(e.title)}</a></td>'
                  f'<td>{UI.e(tt(*LO.EVENT_STATUS.get(e.status, (e.status, e.status))))}</td></tr>' for e in up)
    body = f"""{_ltabs("cal", tt, len(LT.pipeline(ds)))}
<h1 class="pt">{tt("Lighting calendar", "Calendario de iluminación")}</h1><p class="muted" style="margin-top:0">{tt("Audits, installations, handovers and warranty visits in Prologis buildings: what ARGIA does on site, and when.", "Auditorías, instalaciones, entregas y visitas de garantía en naves de Prologis: qué hace ARGIA en sitio, y cuándo.")}</p>
<div class="card"><h2 style="flex-wrap:wrap"><a class="btn sm ghost" href="?mo={py:04d}-{pm:02d}">‹</a> {UI.e(tt(_MONTHS_EN[m - 1], _MONTHS_ES[m - 1]))} {y} <a class="btn sm ghost" href="?mo={ny:04d}-{nm:02d}">›</a>
<a class="btn sm ghost" href="/lighting/calendar/">{tt("Today", "Hoy")}</a><span class="r">{btn}</span></h2>
<div class="legend" style="margin:0 0 8px">{legend}<span class="small muted">{tt("faded = requested, not confirmed yet", "tenue = solicitado, aún sin confirmar")}</span></div>
<div class="calgrid">{days}</div></div>
<div class="card" style="margin-top:16px"><h2>{tt("Next 60 days", "Próximos 60 días")}</h2>
{f'<div style="overflow-x:auto"><table class="t sortable"><tr><th>{tt("Date", "Fecha")}</th><th>{tt("What", "Qué")}</th><th>{tt("Prologis building code", "Código Prologis")}</th><th>{tt("Park", "Parque")}</th><th></th><th>{tt("Status", "Estado")}</th></tr>{ups}</table></div>' if ups else f'<p class="muted" style="margin:0">{tt("Nothing scheduled.", "Nada programado.")}</p>'}</div>"""
    return page(tt("Lighting calendar", "Calendario de iluminación"), body, on="lighting")


_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_MONTHS_ES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]


@app.get("/lighting/calendar/new")
def light_cal_new(msg: str = ""):
    ds = _lds()
    if not (S.can(g.user["role"], "light_work") or S.can(g.user["role"], "light_request")):
        abort(403)
    tt = t()
    argia = S.can(g.user["role"], "light_work")
    f = request.form if request.method == "POST" else request.args
    kinds = "".join(f'<option value="{k}"{" selected" if f.get("kind") == k else ""}>{UI.e(tt(en, es))}</option>' for k, en, es, _ in LO.EVENT_KINDS)
    kind_field = (f'<label>{tt("What", "Qué")}</label><select name="kind">{kinds}</select>' if argia
                  else f'<input type="hidden" name="kind" value="audit"><p class="small muted">{tt("ARGIA confirms the date or proposes another one; you will see it on the calendar.", "ARGIA confirma la fecha o propone otra; la verá en el calendario.")}</p>')
    body = f"""{_ltabs("cal", tt, len(LT.pipeline(ds)))}
<div class="card form"><div class="kick">{tt("Lighting calendar", "Calendario de iluminación")}</div><h1 class="pt">{tt("Add work on site", "Agregar trabajo en sitio") if argia else tt("Request an audit", "Solicitar una auditoría")}</h1>{flash(msg, True)}
<form method="post" action="/lighting/calendar/new">{csrf_field()}{kind_field}
<label>{tt("Building (Prologis building code)", "Nave (código Prologis)")}</label><select name="code"><option value="">-</option>{_bopts(ds, (f.get("code") or "").upper())}</select>
<label>{tt("Or a building not in the list (code and address)", "O una nave que no está en la lista (código y dirección)")}</label><input name="other" maxlength="160" value="{UI.e(f.get("other", ""))}">
<div class="row"><div><label>{tt("From", "Desde")}</label><input type="date" name="start" required value="{UI.e(f.get("start", ""))}"></div>
<div><label>{tt("To (optional)", "Hasta (opcional)")}</label><input type="date" name="end" value="{UI.e(f.get("end", ""))}"></div>
<div><label>{tt("Time", "Hora")}</label><input name="slot" placeholder="09:00" value="{UI.e(f.get("slot", ""))}"></div></div>
<label>{tt("Title", "Título")}</label><input name="title" maxlength="160" value="{UI.e(f.get("title", ""))}">
<label>{tt("Notes (access, contact, what is needed)", "Notas (acceso, contacto, qué se necesita)")}</label><textarea name="notes">{UI.e(f.get("notes", ""))}</textarea>
<div style="margin-top:14px"><button class="btn">{tt("Save", "Guardar") if argia else tt("Send request", "Enviar solicitud")}</button> <a class="btn ghost" href="/lighting/calendar/">{tt("Cancel", "Cancelar")}</a></div></form></div>"""
    return page(tt("Lighting calendar", "Calendario de iluminación"), body, on="lighting")


@app.post("/lighting/calendar/new")
def light_cal_new_post():
    ds = _lds()
    argia = S.can(g.user["role"], "light_work")
    if not (argia or S.can(g.user["role"], "light_request")):
        abort(403)
    tt = t()
    f = request.form
    kind = f.get("kind", "audit") if argia else "audit"
    code = (f.get("code") or "").strip().upper()
    notes = f.get("notes", "")
    if f.get("other"):
        notes = f"{tt('Building', 'Nave')}: {f.get('other')}\n{notes}"
    title = f.get("title", "") or (f"{tt(*LO.EVENT_KIND[kind][:2])} {code}".strip() if kind in LO.EVENT_KIND else "")
    c = db()
    try:
        eid = LO.add_event(c, kind, code, title, f.get("start", ""), f.get("end", ""), f.get("slot", ""),
                           "confirmed" if argia else "requested", notes, g.user["username"],
                           requested_by="" if argia else g.user["username"], ip=ip())
    except LO.Invalid as ex:
        return light_cal_new(tt(f"Please check: {ex}.", f"Revise: {ex}."))
    if not argia:
        ev = LO.event(c, eid)
        AL.queue(c, "light_audit", str(eid), f"[Prologis LED] Audit requested - {code or f.get('other', '')} - {ev['start_date']}",
                 f"{g.user['name'] or g.user['username']} ({g.user['org']}) requested an audit.\nBuilding: {code} {_bname(ds, code)} {f.get('other', '')}\n"
                 f"Date: {ev['start_date']} {ev['slot']}\n\n{f.get('notes', '')}\n\nhttps://prologis.argia.com.mx/lighting/calendar/e/{eid}/\n",
                 AL.recipients(c, "critical"), False)
    return redirect(f"/lighting/calendar/e/{eid}/")


@app.get("/lighting/calendar/e/<int:eid>/")
def light_cal_event(eid: int, msg: str = ""):
    ds = _lds()
    tt = t()
    ev = LO.event(db(), eid)
    if ev is None:
        abort(404)
    en, es, col = LO.EVENT_KIND[ev["kind"]]
    acts = ""
    if S.can(g.user["role"], "light_work"):
        sts = "".join(f'<button class="btn sm ghost" name="status" value="{k}">{UI.e(tt(e1, e2))}</button> ' for k, e1, e2 in LO.EVENT_STATUSES if k != ev["status"])
        acts = (f'<form method="post" action="/lighting/calendar/e/{eid}/">{csrf_field()}<div class="row">'
                f'<div><label>{tt("From", "Desde")}</label><input type="date" name="start" value="{UI.e(ev["start_date"])}"></div>'
                f'<div><label>{tt("To", "Hasta")}</label><input type="date" name="end" value="{UI.e(ev["end_date"])}"></div>'
                f'<div><label>{tt("Time", "Hora")}</label><input name="slot" value="{UI.e(ev["slot"])}"></div></div>'
                f'<div style="margin-top:8px"><button class="btn sm" name="status" value="{UI.e(ev["status"])}">{tt("Save dates", "Guardar fechas")}</button> {sts}</div></form>')
    facts = [(tt("What", "Qué"), f'<span class="pill" style="background:{col}22;color:#14211f">{UI.e(tt(en, es))}</span>'),
             (tt("Status", "Estado"), UI.e(tt(*LO.EVENT_STATUS[ev["status"]]))),
             (tt("Prologis building code", "Código Prologis"), UI.e(ev["code"] or "-")), (tt("Park", "Parque"), UI.e(_bname(ds, ev["code"]) or "-")),
             (tt("Dates", "Fechas"), UI.e(ev["start_date"] + ("" if ev["end_date"] == ev["start_date"] else " - " + ev["end_date"]))),
             (tt("Time", "Hora"), UI.e(ev["slot"] or "-")),
             (tt("Warranty claim", "Reclamo de garantía"), f'<a href="/lighting/warranty/c/{UI.e(ev["ref"])}/">{UI.e(ev["ref"])}</a>' if ev["ref"] else "-"),
             (tt("Requested by", "Solicitado por"), UI.e(ev["requested_by"] or "-"))]
    ftab = "".join(f'<tr><td class="muted">{k}</td><td><b>{v}</b></td></tr>' for k, v in facts)
    body = f"""{_ltabs("cal", tt, len(LT.pipeline(ds)))}
<div class="kick">{tt("Lighting calendar", "Calendario de iluminación")}</div><h1 class="pt">{UI.e(ev["title"])}</h1>{flash(msg, True)}
<div class="grid g2 lg"><div class="card"><table class="t">{ftab}</table>{f'<p style="white-space:pre-wrap">{UI.e(ev["notes"])}</p>' if ev["notes"] else ""}
<a class="btn sm ghost" href="/lighting/calendar/?mo={UI.e(ev["start_date"][:7])}">{tt("Back to the calendar", "Volver al calendario")}</a></div>
{f'<div class="card"><h2>{tt("Update", "Actualizar")}</h2>{acts}</div>' if acts else ""}</div>"""
    return page(ev["title"], body, on="lighting")


@app.post("/lighting/calendar/e/<int:eid>/")
def light_cal_event_post(eid: int):
    need("light_work")
    tt = t()
    f = request.form
    try:
        LO.update_event(db(), eid, g.user["username"], f.get("status") or None, f.get("start", ""), f.get("end", ""), f.get("slot"), ip())
    except LO.Invalid as ex:
        return light_cal_event(eid, tt(f"Not possible: {ex}.", f"No es posible: {ex}."))
    return redirect(f"/lighting/calendar/e/{eid}/")


@app.post("/lighting/p/<int:pid>/pin")
def lighting_pin(pid: int):
    """v360: save the plant's position dragged on the map, or go back to the data file's point."""
    ds = _lds()
    if not _can_pin():
        abort(403)
    raw = lighting_ds().project(pid)
    if raw is None or ds.project(pid) is None:
        abort(404)
    c = db()
    if request.form.get("reset"):
        LO.clear_pin(c, pid, g.user["username"], ip())
        return redirect(f"/lighting/p/{pid}?pin=reset")
    ref = (raw.lat, raw.lon) if raw.lat is not None and raw.geo in ("building", "park") else None
    try:
        LO.set_pin(c, pid, request.form.get("lat"), request.form.get("lon"), ref, g.user["username"], ip())
    except LO.Invalid as ex:
        return Response(f"Position not saved: {html.escape(str(ex))}.", status=400, mimetype="text/plain")
    return redirect(f"/lighting/p/{pid}?pin=saved")


@app.get("/lighting/p/<int:pid>/doc/<int:n>")
def lighting_doc(pid: int, n: int):
    """v357: a project document from the server's copy - opens in the browser, or downloads with ?dl=1."""
    ds = _lds()
    if ds.project(pid) is None:
        abort(404)
    files = project_docs().get(pid, [])
    if not 0 <= n < len(files):
        abort(404)
    f = files[n]
    base = os.path.realpath(os.path.join(LIGHT_DIR, "docs"))
    p = os.path.realpath(os.path.join(base, f["path"]))
    if not p.startswith(base + os.sep) or not os.path.isfile(p):
        abort(404)
    S.audit(db(), g.user["username"], ip(), "lighting_doc", f"P{pid}/{f['type']}", f["name"])
    mime = mimetypes.guess_type(f["name"])[0] or "application/octet-stream"
    inline = mime in ("application/pdf", "image/jpeg", "image/png") and request.args.get("dl") != "1"
    r = send_file(p, mimetype=mime, as_attachment=not inline, download_name=f["name"], max_age=0)
    r.headers["Cache-Control"] = "private, no-store"
    return r


# ------------------------------------------------------------------------------------------------ v359 offer simulator
_OFFERS_CACHE: Dict = {"mtime": None, "by_id": {}, "by_key": {}}


def offers() -> Tuple[Dict[str, ES.Offer], Dict[str, List[ES.Offer]]]:
    """v359: the PROLOGIS Essentials proformas the simulator can recalculate (lighting/offers.json, built on the laptop by
    build_offers_json.py). A broken file keeps the last good one; no file = no simulator."""
    path = os.path.join(LIGHT_DIR, "offers.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        _OFFERS_CACHE.update(mtime=None, by_id={}, by_key={})
        return {}, {}
    if _OFFERS_CACHE["mtime"] != mt:
        try:
            lst = ES.load(path)
            by_key: Dict[str, List[ES.Offer]] = {}
            for o in sorted(lst, key=lambda o: (o.date or "", o.id)):
                by_key.setdefault(o.key, []).append(o)
            _OFFERS_CACHE.update(mtime=mt, by_id={o.id: o for o in lst}, by_key=by_key)
        except (OSError, ValueError, TypeError, KeyError) as ex:
            _OFFERS_CACHE.update(mtime=mt)
            print(f"offers.json not loaded: {ex}", file=sys.stderr)
    return _OFFERS_CACHE["by_id"], _OFFERS_CACHE["by_key"]


def _can_offer() -> bool:
    return S.can(g.user["role"], "light_request")


def _can_cal() -> bool:
    return S.can(g.user["role"], "light_request") or S.can(g.user["role"], "light_work")


def _latest_offer(keys) -> Optional[ES.Offer]:
    """The most recent simulable proforma among register keys (O### opportunities, P### projects)."""
    _, by_key = offers()
    cand = [o for k in keys for o in by_key.get(k, [])]
    return max(cand, key=lambda o: (o.date or "", o.id)) if cand else None


def _audit_btn(code: Optional[str], tt, small: bool = True) -> str:
    if not code or not _can_cal():
        return ""
    return (f'<a class="btn {"sm " if small else ""}ghost" href="/lighting/calendar/new?kind=audit&amp;code={UI.e(code)}" '
            f'title="{tt("Schedule an audit of this building and put it on the calendar", "Programar una auditoría de esta nave en el calendario")}">'
            f'📅 {tt("Audit", "Auditoría") if small else tt("Schedule audit", "Programar auditoría")}</a>')


def _offer_btn(o: Optional[ES.Offer], tt) -> str:
    if o is None or not _can_offer():
        return ""
    return (f'<a class="btn sm" href="/lighting/offer/{UI.e(o.id)}/" title="{tt("Simulate the Prologis Essentials offer of this proforma", "Simular la oferta Prologis Essentials de esta proforma")}">'
            f'🧮 {tt("Simulate", "Simular")}</a>')


def _omxn(v, d: int = 0) -> str:
    return "-" if v is None else ("-$" if v < 0 else "$") + UI.num(abs(v), d)


def _opct(v, d: int = 1) -> str:
    return "-" if v is None else f"{UI.num(100 * v, d)} %"


_OFFER_WARN = {
    "no_before": ("The proforma has no consumption of the current lighting: enter it to see the customer's saving.",
                  "La proforma no tiene el consumo de la iluminación actual: ingréselo para ver el ahorro del cliente."),
    "no_saving": ("With these numbers the new lighting does not use less electricity than the current one.",
                  "Con estos datos la nueva iluminación no consume menos electricidad que la actual."),
    "customer_pays_more": ("The customer pays more in Essentials fee than it saves in electricity.",
                           "El cliente paga más de cuota Essentials de lo que ahorra en electricidad."),
    "not_repaid": ("The fees over the contract do not repay Prologis's investment.",
                   "Las cuotas del contrato no recuperan la inversión de Prologis."),
    "check_data": ("Return above 100 % a year: the cost or the area in this proforma is probably not right - check it with ARGIA.",
                   "Retorno de más de 100 % al año: el costo o la superficie de esta proforma probablemente no son correctos - revíselo con ARGIA."),
}
_SUGGEST = {
    "target": ("Price for the target IRR", "Precio para la TIR objetivo",
               "Prices per m² scaled so Prologis gets exactly the target IRR.", "Precios por m² ajustados para que Prologis obtenga exactamente la TIR objetivo."),
    "share": ("Customer keeps 30 % of the saving", "El cliente conserva 30 % del ahorro",
              "Fee = 70 % of the electricity the customer saves: an easy offer to accept.", "Cuota = 70 % de la electricidad que ahorra el cliente: una oferta fácil de aceptar."),
    "longer": ("Longer contract, same IRR", "Contrato más largo, misma TIR",
               "Spread the investment over more months: a lower monthly fee at the target IRR.", "Repartir la inversión en más meses: cuota mensual menor con la TIR objetivo."),
    "nofee": ("Without the Prologis fee", "Sin la cuota Prologis",
              "The 7 % Prologis fee removed from the work order: a lower investment.", "Sin la cuota Prologis de 7 % en la orden de trabajo: menor inversión."),
}


def _offer_results(o: ES.Offer, s: ES.Scenario, tt) -> str:
    """The results panel; also returned by /calc so the page updates without a reload (one place computes)."""
    r, r0 = ES.compute(o, s), ES.compute(o, ES.standard(o))
    std = ES.standard(o)
    kp = [(tt("Prologis IRR", "TIR Prologis"), _opct(r.irr) if r.irr is not None else tt("not repaid", "no se recupera"), tt(f"target {UI.num(100 * s.target_irr, 1)} %", f"objetivo {UI.num(100 * s.target_irr, 1)} %")),
          (tt("Payback", "Recuperación"), (UI.num(r.payback_months) + f' <small>{tt("months", "meses")}</small>') if r.payback_months is not None else "-", tt(f"contract {s.months} months", f"contrato {s.months} meses")),
          (tt("Essentials fee", "Cuota Essentials"), _omxn(r.monthly_fee) + f' <small>/{tt("mo", "mes")}</small>', f'{_omxn(r.fees_total)} {tt("over the contract", "en el contrato")}'),
          (tt("Customer net saving", "Ahorro neto del cliente"), (_omxn(r.net_saving_m) + f' <small>/{tt("mo", "mes")}</small>') if r.net_saving_m is not None else "-",
           (tt(f"keeps {_opct(r.customer_keeps, 0)} of the saving", f"conserva {_opct(r.customer_keeps, 0)} del ahorro") if r.net_saving_m >= 0
            else tt("pays more than it saves", "paga más de lo que ahorra")) if r.customer_keeps is not None and r.net_saving_m is not None else "")]
    kpis = "".join(f'<div class="kpi"><div class="l">{l}</div><div class="v">{v}</div><div class="d">{d}</div></div>' for l, v, d in kp)
    lo, hi = r.fee_for_target, r.fee_breakeven
    if hi is None:
        band = tt(f"Prologis reaches {UI.num(100 * s.target_irr, 1)} % with a fee of at least <b>{_omxn(lo)}</b> a month. Enter the consumption of the current lighting to see what the customer saves.",
                  f"Prologis alcanza {UI.num(100 * s.target_irr, 1)} % con una cuota de al menos <b>{_omxn(lo)}</b> al mes. Ingrese el consumo de la iluminación actual para ver el ahorro del cliente.")
        bar = ""
    else:
        top = max(hi, lo, r.monthly_fee) * 1.15 or 1
        pos = lambda v: f"{max(0.0, min(100.0, 100 * v / top)):.1f}%"
        ok = lo <= hi
        band = (tt(f"Any monthly fee between <b>{_omxn(lo)}</b> (Prologis gets {UI.num(100 * s.target_irr, 1)} %) and <b>{_omxn(hi)}</b> (the customer stops saving) works for both sides.",
                   f"Cualquier cuota mensual entre <b>{_omxn(lo)}</b> (Prologis obtiene {UI.num(100 * s.target_irr, 1)} %) y <b>{_omxn(hi)}</b> (el cliente deja de ahorrar) funciona para ambos.")
                if ok else
                tt(f"No fee works for both: Prologis needs at least <b>{_omxn(lo)}</b> a month for {UI.num(100 * s.target_irr, 1)} %, but the customer saves only <b>{_omxn(hi)}</b>. Lower the investment (Prologis fee, customer contribution, ARGIA margin) or lengthen the contract.",
                   f"Ninguna cuota funciona para ambos: Prologis necesita al menos <b>{_omxn(lo)}</b> al mes para {UI.num(100 * s.target_irr, 1)} %, pero el cliente ahorra solo <b>{_omxn(hi)}</b>. Reduzca la inversión (cuota Prologis, aportación del cliente, margen de ARGIA) o alargue el contrato."))
        bar = (f'<div class="dealbar"><i class="zone{"" if ok else " bad"}" style="left:{pos(min(lo, hi))};width:calc({pos(max(lo, hi))} - {pos(min(lo, hi))})"></i>'
               f'<b class="mark" style="left:{pos(r.monthly_fee)}" title="{tt("This offer", "Esta oferta")}"></b></div>'
               f'<div class="dealaxis"><span>$0</span><span>{tt("this offer", "esta oferta")}: {_omxn(r.monthly_fee)}</span><span>{_omxn(top)}</span></div>')
    warn = "".join(f'<div class="flash err" style="margin:8px 0 0">{UI.e(tt(*_OFFER_WARN[w]))}</div>' for w in r.warnings if w in _OFFER_WARN)

    def row(label, a, b, strong=False):
        tag = "b" if strong else "span"
        return f'<tr><td>{label}</td><td class="n"><{tag}>{a}</{tag}></td><td class="n muted">{b}</td></tr>'
    margin_note = (f' <span class="pill" style="background:#e8a23a22;color:#7a5200">{tt("needs ARGIA approval", "requiere aprobación de ARGIA")}</span>'
                   if s.profit < o.profit - 1e-9 else "")
    prol = (row(tt("Direct costs (fixtures, labour, materials...)", "Costos directos (luminarias, mano de obra, materiales...)"), _omxn(r.direct), _omxn(r0.direct))
            + row(tt(f"Corporate overhead {UI.num(100 * o.oh)} %", f"Gastos generales {UI.num(100 * o.oh)} %"), _omxn(r.oh_mxn), _omxn(r0.oh_mxn))
            + row(tt(f"ARGIA profit {UI.num(100 * s.profit, 1)} %", f"Utilidad ARGIA {UI.num(100 * s.profit, 1)} %") + margin_note, _omxn(r.profit_mxn), _omxn(r0.profit_mxn))
            + row(tt(f"Prologis fee {UI.num(100 * s.prologis_fee, 1)} %", f"Cuota Prologis {UI.num(100 * s.prologis_fee, 1)} %"), _omxn(r.fee_mxn), _omxn(r0.fee_mxn))
            + row(tt("Work order total", "Total de la orden de trabajo"), _omxn(r.investment), _omxn(r0.investment), True)
            + row(tt("Customer contribution at start", "Aportación del cliente al inicio"), _omxn(-min(s.contribution, r.investment)) if s.contribution else "-", "-")
            + row(tt("Prologis pays", "Prologis paga"), _omxn(r.outlay), _omxn(r0.outlay), True)
            + row(tt("Essentials fee per month", "Cuota Essentials al mes"), _omxn(r.monthly_fee), _omxn(r0.monthly_fee))
            + row(tt("Fees over the contract", "Cuotas en el contrato"), f'{_omxn(r.fees_total)} <span class="small muted">({s.months} {tt("mo", "m")})</span>', f'{_omxn(r0.fees_total)} ({std.months})')
            + row(tt("Prologis IRR (a year)", "TIR Prologis (anual)"), _opct(r.irr) if r.irr is not None else tt("not repaid", "no se recupera"), _opct(r0.irr) if r0.irr is not None else tt("not repaid", "no se recupera"), True)
            + row(tt("Payback", "Recuperación"), f'{UI.num(r.payback_months)} {tt("months", "meses")}' if r.payback_months is not None else "-", f'{UI.num(r0.payback_months)} {tt("months", "meses")}' if r0.payback_months is not None else "-"))
    cust = (row(tt("Lighting electricity now, kWh a year", "Electricidad de iluminación actual, kWh al año"), UI.num(r.kwh_before) if r.kwh_before else "-", UI.num(r0.kwh_before) if r0.kwh_before else "-")
            + row(tt("With LED (design), kWh a year", "Con LED (diseño), kWh al año"), UI.num(r.kwh_after) if r.kwh_after else "-", UI.num(r0.kwh_after) if r0.kwh_after else "-")
            + row(tt(f"Electricity cost a month now ({UI.num(s.kwh_price, 2)} MXN/kWh)", f"Costo de electricidad al mes actual ({UI.num(s.kwh_price, 2)} MXN/kWh)"), _omxn(r.cost_before_m), _omxn(r0.cost_before_m))
            + row(tt("Electricity cost a month with LED", "Costo de electricidad al mes con LED"), _omxn(r.cost_after_m), _omxn(r0.cost_after_m))
            + row(tt("Electricity saved a month", "Electricidad ahorrada al mes"), _omxn(r.gross_saving_m), _omxn(r0.gross_saving_m), True)
            + row(tt("Essentials fee a month", "Cuota Essentials al mes"), _omxn(-r.monthly_fee), _omxn(-r0.monthly_fee))
            + row(tt("Customer's net saving a month", "Ahorro neto del cliente al mes"), _omxn(r.net_saving_m), _omxn(r0.net_saving_m), True)
            + row(tt("Over the contract", "En el contrato"), _omxn(r.net_saving_m * s.months if r.net_saving_m is not None else None), _omxn(r0.net_saving_m * std.months if r0.net_saving_m is not None else None)))
    head = f'<tr><th></th><th class="n">{tt("This offer", "Esta oferta")}</th><th class="n">{tt("Proforma", "Proforma")}</th></tr>'
    sug = ""
    for k, sc in ES.suggestions(o, s):
        rr = ES.compute(o, sc)
        en, es, den, des = _SUGGEST[k]
        qs = "&amp;".join(f"{a}={UI.e(b)}" for a, b in ES.to_args(sc).items())
        sug += (f'<div class="sugg"><b>{UI.e(tt(en, es))}</b><div class="small muted">{UI.e(tt(den, des))}</div>'
                f'<div class="small">{tt("Fee", "Cuota")} <b>{_omxn(rr.monthly_fee)}</b>/{tt("mo", "mes")} · {s.months if k != "longer" else sc.months} {tt("months", "meses")} · '
                f'{tt("IRR", "TIR")} <b>{_opct(rr.irr) if rr.irr is not None else tt("not repaid", "no se recupera")}</b> · {tt("customer", "cliente")} <b>{_omxn(rr.net_saving_m)}</b>/{tt("mo", "mes")}</div>'
                f'<a class="btn sm ghost" href="?{qs}" data-apply="{qs}">{tt("Apply", "Aplicar")}</a></div>')
    return f"""<section class="hero"><div class="glow"></div><div class="k">{tt("Result", "Resultado")}</div><div class="kpis">{kpis}</div></section>{warn}
<div class="card" style="margin-top:16px"><h2>{tt("Where the deal works", "Dónde funciona el trato")}</h2><p style="margin-top:0">{band}</p>{bar}</div>
<div class="grid g2 lg" style="margin-top:16px"><div class="card"><h2>{tt("Prologis Essentials", "Prologis Essentials")}</h2><div style="overflow-x:auto"><table class="t nosort">{head}{prol}</table></div></div>
<div class="card"><h2>{tt("Saving calculator (customer)", "Calculadora de ahorro (cliente)")}</h2><div style="overflow-x:auto"><table class="t nosort">{head}{cust}</table></div></div></div>
<div class="card" style="margin-top:16px"><h2>{tt("Ideas for this offer", "Ideas para esta oferta")}</h2><div class="suggs">{sug}</div></div>"""


def _offer_or_404(oid: str) -> ES.Offer:
    if not _can_offer():
        abort(403)
    by_id, _ = offers()
    o = by_id.get(oid) if ES.ID_RE.match(oid or "") else None
    if o is None:
        abort(404)
    return o


@app.get("/lighting/offer/<oid>/")
def lighting_offer(oid: str):
    """v359: the PROLOGIS Essentials offer simulator for one proforma."""
    o = _offer_or_404(oid)
    return _offer_page(o, ES.from_args(o, request.args))


def _offer_page(o: ES.Offer, s: ES.Scenario, msg: str = "", ok: bool = False):
    ds = _lds()
    tt = t()
    _, by_key = offers()
    versions = by_key.get(o.key, [])
    vlinks = ("".join(f'<a class="tab{" on" if v.id == o.id else ""}" href="/lighting/offer/{UI.e(v.id)}/">{UI.e(v.date or v.id)}<b>{UI.e(v.id.rsplit("-", 1)[1])}</b></a>' for v in versions)
              if len(versions) > 1 else "")
    std = ES.standard(o)

    def inp(name, val, step, mn, mx, suffix="", extra=""):
        return (f'<div class="inp"><input type="number" name="{name}" value="{UI.e(val)}" step="{step}" min="{mn}" max="{mx}" {extra}>'
                f'<span class="muted small">{suffix}</span></div>')
    rates = "".join((f'<label>{tt(*ES.AREA_NAMES[a])} · {UI.num(o.areas[a])} m²</label>{inp("r_" + a, f"{s.rates[a]:g}", "0.1", 0, 30, tt("MXN per m² a month", "MXN por m² al mes"))}')
                    if o.areas[a] else f'<input type="hidden" name="r_{a}" value="{s.rates[a]:g}">' for a in ES.AREAS)
    months = "".join(f'<option value="{m}"{" selected" if m == s.months else ""}>{m} {tt("months", "meses")}</option>' for m in sorted({36, 48, 60, 72, 84, 96, 120, s.months}))
    kb_ph = UI.num(o.kwh_before) if o.kwh_before else tt("not in the proforma - please enter", "no está en la proforma - ingréselo")
    send = (f'<div class="card" style="margin-top:16px"><h2>{tt("Send this offer to ARGIA", "Enviar esta oferta a ARGIA")}</h2>'
            f'<form method="post" action="/lighting/offer/{UI.e(o.id)}/send" id="sendf">{csrf_field()}<input type="hidden" name="qs" value="">'
            f'<label>{tt("Note for ARGIA (customer, deadline, what you need)", "Nota para ARGIA (cliente, fecha límite, qué necesita)")}</label><textarea name="note" maxlength="1500"></textarea>'
            f'<div style="margin-top:10px"><button class="btn">{tt("Send to ARGIA", "Enviar a ARGIA")}</button></div></form></div>')
    title = f"{o.code or o.park or o.key} · {o.tenant}"
    where = " · ".join(x for x in (o.park, o.building) if x and x.strip())
    body = f"""{_ltabs("pipe", tt, len(LT.pipeline(ds)))}
<div class="kick">{tt("Prologis Essentials offer simulator", "Simulador de oferta Prologis Essentials")}</div><h1 class="pt">{UI.e(title)}</h1>
<p class="muted" style="margin-top:0">{UI.e(where)}{" · " if where else ""}{tt("proforma of", "proforma del")} {UI.e(o.date or "-")} ({UI.e(o.file)}). {_audit_btn(o.code, tt, small=False)}</p>
{f'<div class="tabs">{vlinks}</div>' if vlinks else ""}{flash(msg, not ok) if msg else ""}
<div class="offgrid"><div class="card form offform"><h2>{tt("Conditions", "Condiciones")}</h2>
<form method="get" id="offf">{rates}
<label>{tt("Contract length", "Duración del contrato")}</label><select name="m">{months}</select>
<label>{tt("Customer contribution at the start", "Aportación del cliente al inicio")}</label>{inp("c", f"{s.contribution:g}", "1000", 0, 1e9, "MXN")}
<label>{tt("Prologis fee on the work order", "Cuota Prologis en la orden de trabajo")}</label>{inp("pf", f"{100 * s.prologis_fee:g}", "0.5", 0, 10, f'% · {tt("proforma", "proforma")} {UI.num(100 * std.prologis_fee, 1)} %')}
<label>{tt("ARGIA profit", "Utilidad de ARGIA")}</label>{inp("pr", f"{100 * s.profit:g}", "0.5", 0, 15, f'% · {tt("proforma", "proforma")} {UI.num(100 * std.profit, 1)} % · {tt("lower needs ARGIA approval", "menor requiere aprobación de ARGIA")}')}
<label>{tt("Electricity price", "Precio de la electricidad")}</label>{inp("kp", f"{s.kwh_price:g}", "0.05", 0.5, 10, f'MXN/kWh · {tt("proforma", "proforma")} {UI.num(std.kwh_price, 2)}')}
<label>{tt("Lighting consumption now", "Consumo de iluminación actual")}</label>{inp("kb", f"{s.kwh_before:g}" if s.kwh_before else "", "100", 0, 1e8, "kWh/" + tt("yr", "año"), f'placeholder="{UI.e(kb_ph)}"')}
<label>{tt("Prologis target IRR", "TIR objetivo de Prologis")}</label>{inp("t", f"{100 * s.target_irr:g}", "0.5", 0, 60, "%")}
<div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap"><button class="btn">{tt("Recalculate", "Recalcular")}</button><a class="btn ghost" href="/lighting/offer/{UI.e(o.id)}/">{tt("Back to the proforma", "Volver a la proforma")}</a></div>
</form></div><div id="offres">{_offer_results(o, s, tt)}</div></div>
{send if _can_offer() else ""}
<p class="small muted">{tt("How it is calculated: the work order is built as in ARGIA's proforma (direct costs + corporate overhead + ARGIA profit + Prologis fee), Prologis pays it at the start (less any customer contribution) and receives the Essentials fee every month of the contract; the IRR is that of the monthly cash flows, expressed per year. The proforma workbook's own IRR cell uses yearly sums and per-ft² USD prices that are not the MXN prices it charges, so its IRR can differ from the one shown here. Electricity savings are design values from ARGIA's lighting study at the price per kWh above; no maintenance savings, inflation or taxes are included.",
"Cómo se calcula: la orden de trabajo se arma como en la proforma de ARGIA (costos directos + gastos generales + utilidad de ARGIA + cuota Prologis), Prologis la paga al inicio (menos la aportación del cliente) y recibe la cuota Essentials cada mes del contrato; la TIR es la de los flujos mensuales, expresada al año. La celda de TIR del libro de la proforma usa sumas anuales y precios en USD por ft² que no son los precios en MXN que cobra, por lo que su TIR puede diferir de la que se muestra aquí. Los ahorros de electricidad son valores de diseño del estudio lumínico de ARGIA al precio por kWh indicado; no incluye ahorro de mantenimiento, inflación ni impuestos.")}</p>
<script>(function(){{var f=document.getElementById('offf'),box=document.getElementById('offres'),sf=document.getElementById('sendf'),tm=null;
function qs(){{return new URLSearchParams(new FormData(f)).toString();}}
function go(q){{fetch('/lighting/offer/{UI.e(o.id)}/calc?'+q,{{credentials:'same-origin'}}).then(function(r){{return r.ok?r.json():null;}}).then(function(d){{if(!d)return;box.innerHTML=d.html;history.replaceState(null,'','?'+q);if(sf)sf.qs.value=q;}});}}
f.addEventListener('input',function(){{clearTimeout(tm);tm=setTimeout(function(){{go(qs());}},250);}});
f.addEventListener('submit',function(e){{e.preventDefault();go(qs());}});
box.addEventListener('click',function(e){{var a=e.target.closest('a[data-apply]');if(!a)return;e.preventDefault();var p=new URLSearchParams(a.getAttribute('data-apply'));
p.forEach(function(v,k){{var el=f.elements[k];if(el&&el.length&&!el.tagName){{el=el[el.length-1];}}if(el){{if(el.tagName==='SELECT'&&!Array.from(el.options).some(function(o){{return o.value===v;}})){{var o=document.createElement('option');o.value=v;o.textContent=v;el.appendChild(o);}}el.value=v;}}}});go(qs());}});
if(sf)sf.qs.value=qs();}})();</script>"""
    S.audit(db(), g.user["username"], ip(), "lighting_offer_view", o.id)
    return page(tt("Offer simulator", "Simulador de oferta"), body, on="lighting")


@app.get("/lighting/offer/<oid>/calc")
def lighting_offer_calc(oid: str):
    o = _offer_or_404(oid)
    s = ES.from_args(o, request.args)
    r = Response(json.dumps({"html": _offer_results(o, s, t())}), mimetype="application/json")
    r.headers["Cache-Control"] = "private, no-store"
    return r


@app.post("/lighting/offer/<oid>/send")
def lighting_offer_send(oid: str):
    """v359: a Prologis manager sends the scenario to the ARGIA desk (outbox mail with the numbers and a link)."""
    o = _offer_or_404(oid)
    tt = t()
    from urllib.parse import parse_qsl, urlencode
    args = dict(parse_qsl((request.form.get("qs") or "")[:2000]))
    s = ES.from_args(o, args)
    r = ES.compute(o, s)
    qs = urlencode(ES.to_args(s))
    note = (request.form.get("note") or "")[:1500]
    c = db()
    lines = "\n".join(f"  {tt(*ES.AREA_NAMES[a])}: {o.areas[a]:,.0f} m2 x {s.rates[a]:g} MXN/m2 a month" for a in ES.AREAS if o.areas[a])
    irr = f"{100 * r.irr:.1f} %" if r.irr is not None else "not repaid"
    net = f"{r.net_saving_m:,.0f} MXN a month" if r.net_saving_m is not None else "unknown (no current consumption)"
    body = (f"{g.user['name'] or g.user['username']} ({g.user['org']}) sent an Essentials offer for {o.code or o.key} {o.tenant} "
            f"({o.park}, {o.building}; proforma {o.date}, {o.file}).\n\n{lines}\n  Contract: {s.months} months\n"
            f"  Prologis fee: {100 * s.prologis_fee:g} %   ARGIA profit: {100 * s.profit:g} %{'  (BELOW THE PROFORMA - needs ARGIA approval)' if s.profit < o.profit - 1e-9 else ''}\n"
            f"  Customer contribution: {s.contribution:,.0f} MXN   Electricity price: {s.kwh_price:g} MXN/kWh\n\n"
            f"  Work order: {r.investment:,.0f} MXN   Prologis pays: {r.outlay:,.0f} MXN\n  Essentials fee: {r.monthly_fee:,.0f} MXN a month\n"
            f"  Prologis IRR: {irr}   Customer net saving: {net}\n\nNote:\n{note}\n\n"
            f"https://prologis.argia.com.mx/lighting/offer/{o.id}/?{qs}\n")
    AL.queue(c, "light_offer", o.id, f"[Prologis LED] Essentials offer - {o.code or o.key} {o.tenant}", body, AL.recipients(c, "critical"), False)
    S.audit(c, g.user["username"], ip(), "lighting_offer_send", o.id, qs[:240])
    return _offer_page(o, s, tt("Sent to ARGIA. They will answer by e-mail.", "Enviado a ARGIA. Le responderán por correo."), ok=True)


@app.get("/lighting/logo/<key>.png")
def lighting_logo(key):
    ds = _lds()
    c = ds.customers.get(key)
    if not c or not c.logo or not LT.SLUG_RE.match(key):
        abort(404)
    path = os.path.join(LIGHT_DIR, "logos", c.logo)
    try:
        with open(path, "rb") as fh:
            data = fh.read(2_000_001)
    except OSError:
        abort(404)
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 2_000_000:
        abort(404)                                  # only real PNGs are served (no SVG: it could carry script)
    r = Response(data, mimetype="image/png")
    r.headers["Cache-Control"] = "private, max-age=86400"
    return r


def _csv_response(rows: List[List], name: str) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response("﻿" + plain(buf.getvalue()), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/lighting/projects.csv")
def lighting_csv():
    ds = _lds()
    need("export")
    ps, _ = _filter(ds)
    S.audit(db(), g.user["username"], ip(), "export", "lighting projects csv")
    return _csv_response(LT.csv_rows(ps, today_mx().year), "ARGIA_Prologis_LED_projects.csv")


@app.get("/lighting/pipeline.csv")
def lighting_pipeline_csv():
    ds = _lds()
    need("export")
    stf, mf = request.args.get("s", ""), request.args.get("m", "")
    it = LT.intensity(ds)
    rows = [["yardi", "building", "park", "city", "market", "area_m2", "led_share_pct", "to_convert_m2", "status",
             "audited", "last_proforma", "est_mwh_year", "customer", "customer_led_other_buildings"]]
    fp = LT.footprint(ds)
    for r in LT.pipeline(ds):
        if (stf and r.status != stf) or (mf and r.building.market != mf):
            continue
        b = r.building
        rows.append([b.yardi, b.name or "", b.park or "", b.city or "", b.market or "", b.area_m2 or "",
                     round(100 * (r.share or 0), 1), round(r.remaining_m2 or 0), r.status,
                     "yes" if r.status == "audited" or any(o.audit for o in r.opportunities) else "no",
                     max((o.last_proforma for o in r.opportunities if o.last_proforma), default=""),
                     round((r.remaining_m2 or 0) * it.kwh_per_m2 / 1000, 1) if it else "",
                     "; ".join(o.name for o in LT.occupants(r, ds, fp)) or ("Vacant" if LT.vacant(r) else ""),
                     "; ".join(str(o.led_elsewhere) for o in LT.occupants(r, ds, fp))])
    S.audit(db(), g.user["username"], ip(), "export", "lighting pipeline csv")
    return _csv_response(rows, "ARGIA_Prologis_not_yet_LED.csv")


# ------------------------------------------------------------------ v363: due diligence of a prospect's portfolio
# A server-only file DATA_DIR/dd/properties.json (argia/prologis/dd.py): the prospect's properties from public
# sources, what satellite imagery shows, and every estimate computed from assumptions written in the file.
# No file = no menu entry and /dd/ answers 404 (Prologis and Irritec unchanged).
DD_FILE = os.environ.get("ARGIA_PL_DD", os.path.join(DATA_DIR, "dd", "properties.json"))
_DD_CACHE: Dict[str, object] = {"mtime": None, "pf": None}
DD_COL = {"pv": "#FFDC00", "operating": "#ffffff", "development": "#9aa4ad"}


def dd_pf() -> Optional[DD.Portfolio]:
    """The due diligence, re-read when the file changes; a broken file keeps the last good one (log says why)."""
    try:
        mt = os.path.getmtime(DD_FILE)
    except OSError:
        _DD_CACHE.update(mtime=None, pf=None)
        return None
    if _DD_CACHE["mtime"] != mt:
        try:
            _DD_CACHE.update(mtime=mt, pf=DD.load(DD_FILE))
        except (OSError, ValueError, TypeError, KeyError) as ex:
            _DD_CACHE.update(mtime=mt)
            print(f"due diligence file not loaded: {ex}", file=sys.stderr)
    return _DD_CACHE["pf"]


def _dd() -> DD.Portfolio:
    need("view")
    pf = dd_pf()
    if pf is None:
        abort(404)
    return pf


def _mxn_m(v: float) -> str:
    return "$" + UI.num(v / 1e6, 1) + " M"


def _dd_status(p: DD.Prop, tt) -> str:
    st = (tt("Operating", "En operación") if p.status == "operating" else tt("In development", "En desarrollo"))
    pv = f' <span class="pill s-lime">{tt("PV on the roof", "FV en el techo")}</span>' if p.pv_seen else ""
    return f'<span class="pill {"s-ok" if p.status == "operating" else "s-off"}">{st}</span>{pv}'


def _dd_opps(e: Dict, tt) -> str:
    return " ".join(f'<span class="pill s-info">{UI.e(tt(*DD.OPPS[k]))}</span>' for k in e["opps"])


@app.get("/dd/", endpoint="dd_home")
def dd_home():
    tt = t()
    pf = _dd()
    a = pf.assumptions
    tot = DD.totals(pf)
    rows, pts = "", []
    for p in DD.ranked(pf):
        e = DD.estimate(p, a)
        rows += (f'<tr><td><a href="/dd/{p.code}"><b>{UI.e(p.name)}</b></a><div class="small muted">{UI.e(tt(p.kind, p.kind_es or p.kind))}</div></td>'
                 f'<td>{UI.e(p.city)}</td><td>{_dd_status(p, tt)}</td>'
                 f'<td class="n">{UI.num(p.gla_m2) if p.gla_m2 else "-"}</td><td class="n">{UI.num(p.parking) if p.parking else "-"}</td>'
                 f'<td class="n">{UI.num(e["existing_kwp"]) if e["existing_kwp"] else "-"}</td>'
                 f'<td class="n">{UI.num(e["new_kwp"])}</td><td class="n">{UI.num(e["new_mwh"])}</td>'
                 f'<td class="n">{UI.num(e["led_fixtures"] + e["light_fixtures"]) if (e["led_fixtures"] + e["light_fixtures"]) else "-"}</td>'
                 f'<td class="n" data-v="{e["value_mxn"]:.0f}">{_mxn_m(e["value_mxn"])}</td><td>{_dd_opps(e, tt)}</td></tr>')
        pts.append({"code": p.code, "name": p.name, "lat": p.lat, "lon": p.lon, "approx": p.approx,
                    "c": DD_COL["pv"] if p.pv_seen else DD_COL[p.status], "city": p.city,
                    "st": tt("Operating", "En operación") if p.status == "operating" else tt("In development", "En desarrollo"),
                    "pv": round(e["existing_kwp"]), "nk": round(e["new_kwp"]), "v": _mxn_m(e["value_mxn"]),
                    "ph": sat_photo(p.lat, p.lon, True, 480, 270) if gmaps_key() else ""})
    lb = json.dumps({"pv": tt("PV seen, est. kWp", "FV vista, kWp est."), "nk": tt("New PV potential, kWp", "Potencial FV nueva, kWp"),
                     "v": tt("Est. savings a year", "Ahorro est. al año"), "open": tt("Open the property", "Abrir la propiedad"),
                     "approx": tt("approximate location", "ubicación aproximada")}, ensure_ascii=False)
    co = pf.company
    a_rows = [
        (tt("Roof area", "Área de techo"), tt("the published leasable area (single-level retail) unless a roof area is given", "el área rentable publicada (comercio de un nivel) salvo que se indique el área de techo")),
        (tt("Usable roof share", "Parte útil del techo"), f'{a["roof_use"] * 100:.0f} %'),
        (tt("Roof area per kWp", "Área de techo por kWp"), f'{UI.num(a["m2_per_kwp"], 1)} m²'),
        (tt("Specific yield", "Producción específica"), tt("PVGIS per property (10° tilt, 14 % losses); default", "PVGIS por propiedad (10° de inclinación, 14 % de pérdidas); por defecto") + f' {UI.num(a["yield_default"])} kWh/kWp'),
        (tt("Solar carport", "Estacionamiento solar"), f'{a["carport_share"] * 100:.0f} % ' + tt("of the parking spaces", "de los cajones") + f' × {UI.num(a["kwp_per_space"], 1)} kWp'),
        (tt("LED parking lighting", "LED en estacionamiento"), tt("1 fixture per", "1 luminaria por") + f' {UI.num(a["spaces_per_fixture"])} ' + tt("spaces", "cajones") + f', {UI.num(a["led_w_saved"])} W ' + tt("saved", "ahorrados") + f', {UI.num(a["led_hours"])} h/' + tt("year", "año")),
        (tt("Energy price", "Precio de energía"), f'MXN {UI.num(a["tariff_mxn_kwh"], 2)}/kWh ' + tt("(CFE GDMTH average, to confirm with your bills)", "(promedio CFE GDMTH, a confirmar con sus recibos)")),
        (tt("Grid emission factor", "Factor de emisión de la red"), f'{a["co2_kg_kwh"]:.3f} kg CO₂/kWh (SEMARNAT/CRE)'),
    ]
    arows = "".join(f'<tr><td>{UI.e(k)}</td><td>{v}</td></tr>' for k, v in a_rows)
    offer = [
        (tt("O&M for the PV you already have", "O&M de la FV que ya tienen"),
         tt("Monitoring of every inverter, alarms, tickets with response times, preventive maintenance plan, cleaning, thermography and a monthly report per property, on this platform.",
            "Monitoreo de cada inversor, alarmas, tickets con tiempos de respuesta, plan de mantenimiento preventivo, limpieza, termografía y un reporte mensual por propiedad, en esta plataforma.")),
        (tt("New PV on roofs and carports", "FV nueva en techos y estacionamientos"),
         tt("Audit, design, permits, interconnection with CFE, construction and commissioning; the plant joins O&M on the same platform the day it starts.",
            "Auditoría, diseño, permisos, interconexión con CFE, construcción y puesta en marcha; la planta entra a O&M en la misma plataforma el día que arranca.")),
        (tt("LED lighting partner", "Socio de iluminación LED"),
         tt("Warehouses (high-bay LED with sensors), parking, common areas and back-of-house: fixture audit, lighting design, installation and measured savings per property.",
            "Naves (LED high-bay con sensores), estacionamiento, áreas comunes y áreas de servicio: levantamiento de luminarias, diseño de iluminación, instalación y ahorro medido por propiedad.")),
    ]
    offer_html = "".join(f'<div class="card"><h2 style="margin:0 0 6px;font-size:17px">{UI.e(h)}</h2><p class="small" style="margin:0">{UI.e(d)}</p></div>' for h, d in offer)
    body = f"""<div class="hero"><div class="k">{tt("Due diligence", "Due diligence")} · {UI.e(pf.as_of)}</div>
<h1>{UI.e(tt(pf.title, pf.title_es or pf.title))}</h1><div class="sub">{UI.e(tt(co.get("about", ""), co.get("about_es") or co.get("about", "")))}</div>
<div class="kpis"><div class="kpi"><div class="l">{tt("Properties", "Propiedades")}</div><div class="v">{tot["properties"]}</div><div class="d">{tot["operating"]} {tt("operating", "en operación")} · {tot["development"]} {tt("in development", "en desarrollo")}</div></div>
<div class="kpi"><div class="l">{tt("PV already on the roof", "FV ya en el techo")}</div><div class="v">{tot["pv_seen"]}<small>{tt("properties", "propiedades")}</small></div><div class="d">≈ {UI.num(tot["existing_kwp"] / 1000, 1)} MWp {tt("est. from imagery", "est. por imagen")}</div></div>
<div class="kpi"><div class="l">{tt("New PV potential", "Potencial FV nueva")}</div><div class="v">{UI.num(tot["new_kwp"] / 1000, 1)}<small>MWp</small></div><div class="d">{UI.num(tot["new_mwh"])} MWh/{tt("year", "año")}</div></div>
<div class="kpi"><div class="l">{tt("LED fixtures", "Luminarias LED")}</div><div class="v">{UI.num(tot["led_fixtures"] + tot["light_fixtures"])}</div><div class="d">{UI.num(tot["led_mwh"] + tot["light_mwh"])} MWh/{tt("year saved", "año ahorrados")}</div></div>
<div class="kpi"><div class="l">{tt("Est. savings a year", "Ahorro est. al año")}</div><div class="v">{_mxn_m(tot["value_mxn"])}</div><div class="d">MXN · {UI.num(tot["co2_t"])} t CO₂ {tt("avoided", "evitadas")}</div></div></div></div>
<div class="grid g2"><div class="card"><h2>{tt("The portfolio on the map of Mexico", "El portafolio en el mapa de México")}</h2>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<div id="ddmap" style="height:520px;border-radius:12px;background:#dfe6e4;z-index:0"></div>
<div class="small muted" style="margin-top:8px"><span style="color:#b39500">●</span> {tt("PV seen on the roof", "FV vista en el techo")} &nbsp; <span style="color:#6b7a78">○</span> {tt("operating, no PV seen", "en operación, sin FV vista")} &nbsp; <span style="color:#9aa4ad">●</span> {tt("in development", "en desarrollo")}</div></div>
<div><div class="card"><h2>{tt("What ARGIA brings", "Lo que ARGIA aporta")}</h2><p class="small muted" style="margin-top:0">{tt("One partner for the energy of the whole portfolio.", "Un solo socio para la energía de todo el portafolio.")}</p></div>{offer_html}</div></div>
<div class="card" style="margin-top:16px"><h2>{tt("Properties, ranked", "Propiedades, en orden de prioridad")}</h2><p class="small muted" style="margin-top:0">{tt("Properties with PV already on the roof first (an O&M contract can start now), then by estimated yearly savings. All sizes are estimates from public data and satellite imagery, to be confirmed by a site audit.", "Primero las propiedades con FV ya instalada (un contrato de O&M puede iniciar ya), luego por ahorro anual estimado. Todos los tamaños son estimaciones con datos públicos e imagen satelital, a confirmar con una visita.")}</p>
<div style="overflow-x:auto"><table class="t sortable"><tr><th>{tt("Property", "Propiedad")}</th><th>{tt("City", "Ciudad")}</th><th>{tt("Status", "Estado")}</th><th>{tt("GLA m²", "ABR m²")}</th><th>{tt("Parking", "Cajones")}</th><th>{tt("PV seen kWp", "FV vista kWp")}</th><th>{tt("New PV kWp", "FV nueva kWp")}</th><th>MWh/{tt("year", "año")}</th><th>LED</th><th>{tt("Savings/year", "Ahorro/año")}</th><th>{tt("Opportunities", "Oportunidades")}</th></tr>{rows}</table></div></div>
<div class="grid g2e"><div class="card"><h2>{tt("Assumptions", "Supuestos")}</h2><table class="t nosort">{arows}</table></div>
<div class="card"><h2>{tt("Sources and limits", "Fuentes y límites")}</h2><p class="small">{tt("Properties, areas, anchors, parking and opening dates come from the public pages of", "Propiedades, áreas, anclas, cajones y fechas de apertura vienen de las páginas públicas de")} <a href="{UI.e(co.get("url", ""))}" rel="noopener" target="_blank">{UI.e(co.get("name", ""))}</a>. {tt("Locations from the published addresses (approximate where marked). PV on roofs as seen on satellite imagery of unknown date; who owns it (the property or an anchor tenant) is not known.", "Ubicaciones a partir de las direcciones publicadas (aproximadas donde se indica). FV en techos según imagen satelital de fecha desconocida; no se sabe quién es dueño (la propiedad o una tienda ancla).")}</p>
<p class="small">{tt("Every figure on this page is an estimate for a first conversation, not an offer. A site audit, the CFE bills and the roof drawings turn it into a proposal.", "Cada cifra de esta página es una estimación para una primera conversación, no una oferta. Una visita, los recibos de CFE y los planos de techos la convierten en propuesta.")}</p></div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>(function(){{var P={json.dumps(pts, ensure_ascii=False).replace("</", "<\\/")};var LB={lb};
var m=L.map('ddmap',{{scrollWheelZoom:false}});
var st=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri',maxZoom:19}}).addTo(m);
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri, Maxar, Earthstar Geographics',maxZoom:19}});
L.control.layers({{'Streets':st,'Satellite':sat}}).addTo(m);
function esc(s){{return String(s).replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
function n0(v){{return Number(v).toLocaleString('en-US');}}
var b=[];P.forEach(function(p){{b.push([p.lat,p.lon]);
var mk=L.circleMarker([p.lat,p.lon],{{radius:p.pv?11:8,color:'#131317',weight:2,fillColor:p.c,fillOpacity:.95}}).addTo(m);
mk.bindPopup((p.ph?'<img src="'+esc(p.ph)+'" referrerpolicy="origin" alt="" style="display:block;width:240px;height:135px;object-fit:cover;border-radius:6px;margin-bottom:6px;background:#dfe6e4">':'')
+'<b>'+esc(p.name)+'</b><div class="small muted">'+esc(p.city)+' · '+esc(p.st)+'</div><table style="width:100%;margin:6px 0;font-size:12.5px">'
+(p.pv?'<tr><td>'+LB.pv+'</td><td style="text-align:right"><b>'+n0(p.pv)+'</b></td></tr>':'')
+'<tr><td>'+LB.nk+'</td><td style="text-align:right"><b>'+n0(p.nk)+'</b></td></tr><tr><td>'+LB.v+'</td><td style="text-align:right"><b>'+esc(p.v)+'</b></td></tr></table>'
+(p.approx?'<div class="small muted"><i>'+LB.approx+'</i></div>':'')+'<a href="/dd/'+esc(p.code)+'">'+LB.open+' ›</a>',{{minWidth:240,maxWidth:260}});}});
if(b.length)m.fitBounds(b,{{padding:[30,30],maxZoom:12}});}})();</script>"""
    return page(tt("Due diligence", "Due diligence"), body, "dd")


@app.get("/dd/lighting/", endpoint="dd_lighting")
def dd_lighting():
    """v364: the LED lighting part of the due diligence - interior LED of the industrial buildings (a
    "lighting" block in the file), LED in the parking lots, a calculator for any other building, and how
    ARGIA runs an LED project. Only where the file has a lighting block."""
    tt = t()
    pf = _dd()
    a = pf.assumptions
    lit = [p for p in pf.props if p.lighting]
    if not lit:
        abort(404)
    tot = DD.totals(pf)
    co2 = a["co2_kg_kwh"]
    cards = ""
    for p in lit:
        e, li = DD.estimate(p, a), DD.light(p, a)
        lt = p.lighting
        st = tt(*DD.LIGHT_STATUSES[lt.status])
        rows = [(tt("Lit floor area", "Área iluminada"), f'{UI.num(li["area_m2"])} m²'),
                (tt("High-bay fixtures", "Luminarias high-bay"), f'{UI.num(li["fixtures"])} ({tt("1 per", "1 por")} {UI.num(li["area_m2"] / li["fixtures"]) if li["fixtures"] else "-"} m²)'),
                (tt("Today", "Hoy"), f'{UI.e(tt(*DD.BASELINES[lt.baseline]))} · {UI.num(li["w_before"])} W'),
                (tt("With ARGIA", "Con ARGIA"), f'LED {UI.num(li["w_after"])} W + ' + tt("occupancy and daylight sensors", "sensores de presencia y luz natural") + f' (-{a["led_controls_share"] * 100:.0f} %)'),
                (tt("Hours a year", "Horas al año"), UI.num(li["hours"])),
                (tt("Energy before / after", "Energía antes / después"), f'{UI.num(li["mwh_before"])} / {UI.num(li["mwh_after"])} MWh'),
                (tt("Saved a year", "Ahorro al año"), f'<b>{UI.num(li["mwh"])} MWh · {_mxn_m(e["light_mxn"])} MXN · {UI.num(li["mwh"] * co2)} t CO₂</b>')]
        if e["led_fixtures"]:
            rows.append((tt("Parking and yard", "Estacionamiento y patio"), f'{UI.num(e["led_fixtures"])} ' + tt("fixtures", "luminarias") + f' · {UI.num(e["led_mwh"], 1)} MWh · {_mxn_m(e["led_mxn"])} MXN'))
        if e["new_kwp"]:
            rows.append((tt("Solar on the same roof", "Solar en el mismo techo"), f'{UI.num(e["new_kwp"])} kWp · {UI.num(e["new_mwh"])} MWh/' + tt("year", "año")))
        trs = "".join(f'<tr><td class="muted">{h}</td><td>{v}</td></tr>' for h, v in rows)
        note = tt(lt.note, lt.note_es or lt.note)
        cards += f"""<div class="card"><div class="grid g2e" style="margin-top:0">
<figure style="margin:0"><img src="{UI.e(sat_photo(p.lat, p.lon, False, 640, 400))}" referrerpolicy="origin" alt="" style="width:100%;aspect-ratio:16/10;object-fit:cover;border-radius:12px;background:#dfe6e4">
{f'<figcaption class="small muted">{tt("Approximate location: the exact site is not published", "Ubicación aproximada: el sitio exacto no está publicado")}</figcaption>' if p.approx else ""}</figure>
<div><div class="kick">{UI.e(tt(p.kind, p.kind_es or p.kind))} · {UI.e(p.city)}</div><h2 style="margin:2px 0 6px;font-size:20px"><a href="/dd/{p.code}">{UI.e(p.name)}</a></h2>
<span class="pill s-off">{UI.e(st)}</span><table class="t nosort" style="margin-top:8px">{trs}</table>{f'<p class="small muted">{UI.e(note)}</p>' if note else ""}</div></div></div>"""
    prow = ""
    for p in DD.ranked(pf):
        e = DD.estimate(p, a)
        if e["led_fixtures"]:
            prow += (f'<tr><td><a href="/dd/{p.code}">{UI.e(p.name)}</a></td><td>{UI.e(p.city)}</td><td class="n">{UI.num(p.parking)}</td>'
                     f'<td class="n">{UI.num(e["led_fixtures"])}</td><td class="n">{UI.num(e["led_mwh"], 1)}</td><td class="n" data-v="{e["led_mxn"]:.0f}">{_mxn_m(e["led_mxn"])}</td></tr>')
    fx = tot["light_fixtures"] + tot["led_fixtures"]
    mwh = tot["light_mwh"] + tot["led_mwh"]
    steps = [(tt("Audit", "Auditoría"), tt("Fixture count, lux readings, operating hours and 12 months of CFE bills, building by building.", "Conteo de luminarias, mediciones de lux, horas de operación y 12 meses de recibos de CFE, edificio por edificio.")),
             (tt("Lighting design", "Diseño de iluminación"), tt("Photometric study per area to the levels of NOM-025-STPS and the power limits of NOM-007-ENER.", "Estudio fotométrico por área con los niveles de la NOM-025-STPS y los límites de potencia de la NOM-007-ENER.")),
             (tt("Proposal", "Propuesta"), tt("Savings per building and two ways to pay: purchase, or a monthly fee covered by the savings (Light as a Service).", "Ahorro por edificio y dos formas de pago: compra, o una cuota mensual cubierta por el ahorro (Light as a Service).")),
             (tt("Installation", "Instalación"), tt("In phases, at night or by zones, without stopping the tenants' operations.", "Por etapas, de noche o por zonas, sin detener la operación de los inquilinos.")),
             (tt("Measured savings", "Ahorro medido"), tt("Every building on this platform: fixtures, warranties, maintenance and the energy saved, month by month.", "Cada edificio en esta plataforma: luminarias, garantías, mantenimiento y la energía ahorrada, mes a mes."))]
    steps_html = "".join(f'<div class="card" style="padding:12px 14px"><div class="kick">{i}</div><b>{UI.e(h)}</b><p class="small" style="margin:4px 0 0">{UI.e(d)}</p></div>'
                         for i, (h, d) in enumerate(steps, 1))
    cj = json.dumps({"co2": co2, "tariff": a["tariff_mxn_kwh"], "per": a["led_m2_per_fixture"], "wa": a["led_w_after"],
                     "ctl": a["led_controls_share"], "h": a["led_hours_interior"]})
    calc = f"""<div class="card"><h2>{tt("Savings calculator for any building", "Calculadora de ahorro para cualquier edificio")}</h2>
<p class="small muted" style="margin-top:0">{tt("Type the numbers of one of your buildings; the result uses the same method as the cards above.", "Escriba los datos de uno de sus edificios; el resultado usa el mismo método que las tarjetas de arriba.")}</p>
<div class="grid g2e" style="margin-top:6px"><div>
<label>{tt("Lit floor area, m²", "Área iluminada, m²")}</label><input id="lc_a" type="number" min="0" step="100" value="20000">
<label>{tt("Fixtures today", "Luminarias actuales")}</label><select id="lc_w"><option value="458">{tt("Metal halide 400 W (458 W with ballast)", "Aditivo metálico 400 W (458 W con balastro)")}</option>
<option value="295">{tt("Metal halide 250 W (295 W with ballast)", "Aditivo metálico 250 W (295 W con balastro)")}</option><option value="351">{tt("Fluorescent T5HO, 6 tubes (351 W)", "Fluorescente T5HO, 6 tubos (351 W)")}</option>
<option value="234">{tt("Fluorescent T5HO, 4 tubes (234 W)", "Fluorescente T5HO, 4 tubos (234 W)")}</option></select>
<label>{tt("Hours a year", "Horas al año")}</label><input id="lc_h" type="number" min="0" max="8760" step="100" value="{a["led_hours_interior"]:.0f}">
<label>{tt("Energy price, MXN/kWh", "Precio de energía, MXN/kWh")}</label><input id="lc_p" type="number" min="0" step="0.05" value="{a["tariff_mxn_kwh"]:.2f}"></div>
<div><table class="t nosort"><tr><td class="muted">{tt("LED fixtures", "Luminarias LED")}</td><td class="n"><b id="lc_n">-</b></td></tr>
<tr><td class="muted">{tt("Energy before / after", "Energía antes / después")}</td><td class="n"><b id="lc_e">-</b></td></tr>
<tr><td class="muted">{tt("Saved a year", "Ahorro al año")}</td><td class="n"><b id="lc_s">-</b></td></tr>
<tr><td class="muted">{tt("Money saved a year", "Dinero ahorrado al año")}</td><td class="n"><b id="lc_m">-</b></td></tr>
<tr><td class="muted">{tt("CO₂ avoided a year", "CO₂ evitado al año")}</td><td class="n"><b id="lc_c">-</b></td></tr></table>
<p class="small muted">{tt("1 LED fixture per", "1 luminaria LED por")} {UI.num(a["led_m2_per_fixture"])} m², {UI.num(a["led_w_after"])} W, {tt("sensors save", "los sensores ahorran")} {a["led_controls_share"] * 100:.0f} %.</p></div></div>
<script>(function(){{var C={cj};function g(i){{var v=parseFloat(document.getElementById(i).value);return isFinite(v)&&v>0?v:0;}}
function f(v,d){{return Number(v).toLocaleString('en-US',{{minimumFractionDigits:d||0,maximumFractionDigits:d||0}});}}
function run(){{var A=g('lc_a'),W=g('lc_w'),H=Math.min(g('lc_h'),8760),P=g('lc_p');var n=A?Math.ceil(A/C.per):0;
var b=n*W*H/1e6,af=n*C.wa*H*(1-C.ctl)/1e6,s=Math.max(0,b-af);
document.getElementById('lc_n').textContent=f(n);document.getElementById('lc_e').textContent=f(b)+' / '+f(af)+' MWh';
document.getElementById('lc_s').textContent=f(s)+' MWh ('+(b?f(s/b*100):0)+' %)';document.getElementById('lc_m').textContent='MXN $'+f(s*1000*P);
document.getElementById('lc_c').textContent=f(s*C.co2)+' t';}}
['lc_a','lc_w','lc_h','lc_p'].forEach(function(i){{document.getElementById(i).addEventListener('input',run);}});run();}})();</script></div>"""
    body = f"""<div class="hero"><div class="k">{tt("LED lighting", "Iluminación LED")} · {UI.e(pf.as_of)}</div>
<h1>{tt("LED lighting for the industrial and retail portfolio", "Iluminación LED para el portafolio industrial y comercial")}</h1>
<div class="sub">{tt("High-bay LED with occupancy and daylight sensors in the warehouses, LED in the parking lots, designed, installed and measured by ARGIA.", "LED high-bay con sensores de presencia y luz natural en las naves, LED en los estacionamientos; diseño, instalación y medición de ARGIA.")}</div>
<div class="kpis"><div class="kpi"><div class="l">{tt("Industrial buildings", "Naves industriales")}</div><div class="v">{tot["lighting"]}</div><div class="d">{UI.num(sum(DD.light(p, a)["area_m2"] for p in lit))} m² {tt("lit", "iluminados")}</div></div>
<div class="kpi"><div class="l">{tt("LED fixtures", "Luminarias LED")}</div><div class="v">{UI.num(fx)}</div><div class="d">{UI.num(tot["light_fixtures"])} {tt("interior", "interiores")} · {UI.num(tot["led_fixtures"])} {tt("parking", "estacionamiento")}</div></div>
<div class="kpi"><div class="l">{tt("Energy saved", "Energía ahorrada")}</div><div class="v">{UI.num(mwh)}<small>MWh/{tt("year", "año")}</small></div><div class="d">{tt("estimate", "estimación")}</div></div>
<div class="kpi"><div class="l">{tt("Est. savings a year", "Ahorro est. al año")}</div><div class="v">{_mxn_m(tot["light_mxn"] + tot["led_mxn"])}</div><div class="d">MXN · {UI.num(mwh * co2)} t CO₂ {tt("avoided", "evitadas")}</div></div></div></div>
<h2 style="margin:22px 0 4px">{tt("Industrial buildings", "Naves industriales")}</h2>{cards}
<div class="grid g2e">{calc}<div class="card"><h2>{tt("Parking lots of the shopping centers", "Estacionamientos de los centros comerciales")}</h2>
<div style="overflow-x:auto"><table class="t sortable"><tr><th>{tt("Property", "Propiedad")}</th><th>{tt("City", "Ciudad")}</th><th>{tt("Spaces", "Cajones")}</th><th>{tt("Fixtures", "Luminarias")}</th><th>MWh/{tt("year", "año")}</th><th>{tt("Savings/year", "Ahorro/año")}</th></tr>{prow}</table></div></div></div>
<h2 style="margin:22px 0 4px">{tt("How ARGIA runs an LED project", "Cómo ARGIA ejecuta un proyecto LED")}</h2><div class="grid g3" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">{steps_html}</div>
<div class="card" style="margin-top:16px"><h2>{tt("Assumptions", "Supuestos")}</h2><p class="small">{tt("Interior: 1 high-bay fixture per", "Interior: 1 luminaria high-bay por")} {UI.num(a["led_m2_per_fixture"])} m² ({tt("11 m clear height, 200-300 lux", "altura libre de 11 m, 200-300 lux")}); {tt("today", "hoy")} {UI.num(a["led_w_before"])} W {tt("per fixture (metal halide 400 W with ballast) unless the building says otherwise; LED", "por luminaria (aditivo metálico 400 W con balastro) salvo que el edificio indique otra cosa; LED")} {UI.num(a["led_w_after"])} W; {tt("sensors save", "los sensores ahorran")} {a["led_controls_share"] * 100:.0f} % {tt("of the LED load", "de la carga LED")}; {UI.num(a["led_hours_interior"])} h/{tt("year", "año")}. {tt("Parking: 1 fixture per", "Estacionamiento: 1 luminaria por")} {UI.num(a["spaces_per_fixture"])} {tt("spaces", "cajones")}, {UI.num(a["led_w_saved"])} W {tt("saved", "ahorrados")}, {UI.num(a["led_hours"])} h. MXN {UI.num(a["tariff_mxn_kwh"], 2)}/kWh; {co2:.3f} kg CO₂/kWh. {tt("Estimates for a first conversation; the audit replaces every number.", "Estimaciones para una primera conversación; la auditoría reemplaza cada número.")}</p></div>"""
    return page(tt("Lighting", "Iluminación"), body, "ddlight")


@app.get("/dd/<code>", endpoint="dd_prop")
def dd_prop(code):
    tt = t()
    pf = _dd()
    p = pf.prop(code)
    if p is None:
        abort(404)
    e = DD.estimate(p, pf.assumptions)
    k = gmaps_key()
    pics = "".join(f'<figure style="margin:0"><img src="{UI.e(sat_photo(p.lat, p.lon, bld, 640, 360))}" referrerpolicy="origin" alt="" '
                   f'style="display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:12px;background:#dfe6e4">'
                   f'<figcaption class="small muted" style="margin-top:4px">{UI.e(cap)}</figcaption></figure>'
                   for bld, cap in ((True, tt("The building", "El edificio") if not p.approx else tt("Approximate location: the exact site is not published", "Ubicación aproximada: el sitio exacto no está publicado")),
                                    (False, tt("The surroundings", "El entorno"))))
    src = "Google" if k else "Esri, Maxar, Earthstar Geographics"
    facts = [(tt("Address", "Dirección"), UI.e(p.address or "-")),
             (tt("Type", "Tipo"), UI.e(tt(p.kind, p.kind_es or p.kind) or "-")),
             (tt("Gross leasable area", "Área bruta rentable"), (UI.num(p.gla_m2) + " m²") if p.gla_m2 else "-"),
             (tt("Roof area used here", "Área de techo considerada"), (UI.num(e["roof_m2"]) + " m²") if e["roof_m2"] else "-"),
             (tt("Units", "Locales"), UI.e(p.units or "-")),
             (tt("Anchor tenants", "Tiendas ancla"), UI.e(", ".join(p.anchors) or "-")),
             (tt("Parking spaces", "Cajones de estacionamiento"), UI.num(p.parking) if p.parking else "-"),
             (tt("Opened", "Apertura"), UI.e(p.opened or "-")),
             (tt("Location", "Ubicación"), f'{p.lat:.5f}, {p.lon:.5f}' + (f' <i class="muted">({tt("approximate", "aproximada")})</i>' if p.approx else ""))]
    if p.notes:
        facts.append((tt("Notes", "Notas"), UI.e(tt(p.notes, p.notes_es or p.notes))))
    if p.sources:
        facts.append((tt("Sources", "Fuentes"), "<br>".join(f'<a href="{UI.e(u)}" rel="noopener" target="_blank">{UI.e(u.split("//", 1)[-1][:70])}</a>' for u in p.sources)))
    frows = "".join(f'<tr><td class="muted">{h}</td><td>{v}</td></tr>' for h, v in facts)
    y = UI.num(e["yield"])
    est = [(tt("PV seen on the roof (est.)", "FV vista en el techo (est.)"), (f'{UI.num(e["existing_kwp"])} kWp · {UI.num(e["existing_mwh"])} MWh/' + tt("year", "año")) if e["existing_kwp"] else tt("none seen", "no se ve")),
           (tt("New rooftop PV", "FV nueva en techo"), f'{UI.num(e["new_roof_kwp"])} kWp'),
           (tt("Solar carport", "Estacionamiento solar"), f'{UI.num(e["carport_kwp"])} kWp'),
           (tt("New PV production", "Producción FV nueva"), f'{UI.num(e["new_mwh"])} MWh/' + tt("year", "año") + f' ({y} kWh/kWp)'),
           (tt("PV savings", "Ahorro FV"), f'{_mxn_m(e["pv_mxn"])} MXN/' + tt("year", "año")),
           (tt("LED parking fixtures", "Luminarias LED estacionamiento"), f'{UI.num(e["led_fixtures"])} · {UI.num(e["led_mwh"], 1)} MWh/' + tt("year", "año") + f' · {_mxn_m(e["led_mxn"])} MXN'),
           *([(tt("LED interior fixtures", "Luminarias LED interiores"), f'{UI.num(e["light_fixtures"])} · {UI.num(e["light_mwh"])} MWh/' + tt("year", "año") + f' · {_mxn_m(e["light_mxn"])} MXN')] if e["light_fixtures"] else []),
           (tt("CO₂ avoided", "CO₂ evitado"), f'{UI.num(e["co2_t"])} t/' + tt("year", "año"))]
    erows = "".join(f'<tr><td class="muted">{UI.e(h)}</td><td class="n"><b>{v}</b></td></tr>' for h, v in est)
    site = reg().site(p.code)
    links = (f'<a class="btn ghost sm" href="/sites/{p.code}/">{tt("Open in the platform", "Abrir en la plataforma")}</a>' if site else "")
    body = f"""<div class="small"><a href="/dd/">‹ {tt("Due diligence", "Due diligence")}</a></div>
<div class="card phead" style="grid-template-columns:minmax(0,1fr) auto"><div class="phmain"><div class="kick">{UI.e(tt(p.kind, p.kind_es or p.kind))} · {UI.e(p.city)}, {UI.e(p.state)}</div><h1>{UI.e(p.name)}</h1>
<div class="phfacts">{_dd_status(p, tt)}<span>{tt("Est. savings a year", "Ahorro est. al año")} <b>{_mxn_m(e["value_mxn"])} MXN</b></span><span>{tt("New PV", "FV nueva")} <b>{UI.num(e["new_kwp"])} kWp</b></span></div>
<div style="margin-top:8px">{_dd_opps(e, tt)}</div></div><div class="phact">{links}</div></div>
<div class="grid g2e">{pics}</div><div class="small muted" style="margin-top:4px">{tt("Imagery", "Imagen")}: {src}</div>
<div class="grid g2e"><div class="card"><h2>{tt("Facts", "Datos")}</h2><table class="t nosort">{frows}</table></div>
<div class="card"><h2>{tt("Estimates", "Estimaciones")}</h2><table class="t nosort">{erows}</table>
<p class="small muted">{tt("Estimates from the assumptions on the due diligence page; to be confirmed by a site audit and the CFE bills.", "Estimaciones con los supuestos de la página de due diligence; a confirmar con una visita y los recibos de CFE.")}</p></div></div>"""
    return page(p.name, body, "dd")


def main(argv: List[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--create-admin":
        if len(argv) < 5:
            print("usage: prologis_app.py --create-admin USER 'Full name' EMAIL")
            return 2
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        c = S.connect()
        pw = S.create_user(c, argv[2], argv[3], argv[4], "ARGIA", "admin", "cli")
        print(f"admin {argv[2]} created. One-time password (change at first sign-in, then enrol MFA): {pw}")
        return 0
    if len(argv) >= 2 and argv[1] == "--add-user":
        # v376: --add-user USER 'Full name' EMAIL ORG ROLE [--simple-password] [--password-stdin]; the operator
        # types the password (getpass twice, or one line on stdin piped by the Windows runner, which asks twice,
        # masked): never on the command line, in a file or in a log
        args = [x for x in argv[2:] if x not in ("--simple-password", "--password-stdin")]
        simple = "--simple-password" in argv
        if len(args) != 5:
            print("usage: prologis_app.py --add-user USER 'Full name' EMAIL ORG ROLE [--simple-password] [--password-stdin]")
            return 2
        if simple and not mfa_off():
            print("refused: --simple-password only while this instance's dated MFA exception (ARGIA_PL_MFA_OFF_UNTIL) runs")
            return 2
        if "--password-stdin" in argv:
            pw = sys.stdin.readline().rstrip("\r\n")
        else:
            import getpass
            pw = getpass.getpass(f"Password for {args[0]}: ")
            if pw != getpass.getpass("Same password again: "):
                print("the two passwords differ - nothing created")
                return 1
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        c = S.connect()
        try:
            S.create_user_with_password(c, args[0], args[1], args[2], args[3], args[4], pw, "console", allow_simple=simple)
        except ValueError as exc:
            print(f"not created: {exc}")
            return 1
        print(f"user {args[0].strip().lower()} created ({args[4]}, {args[3]}); password as typed, no forced change"
              + (f"; MFA not asked until {MFA_OFF_UNTIL}" if mfa_off() else "; authenticator enrolment at first sign-in"))
        return 0
    if len(argv) >= 3 and argv[1] == "--seed-catalog":
        with open(argv[2], encoding="utf-8") as fh:
            items = json.load(fh)
        c = S.connect()
        n = S.seed_catalog(c, items, "seed")
        print(f"catalogue: {n} added, {len(items) - n} already present (never overwritten)")
        return 0
    if len(argv) >= 3 and argv[1] == "--seed-parts":
        with open(argv[2], encoding="utf-8") as fh:
            items = json.load(fh)
        c = S.connect()
        n = A.seed_parts(c, items, "seed")
        print(f"spare parts: {n} added, {len(items) - n} already present (never overwritten)")
        return 0
    if len(argv) >= 2 and argv[1] == "--alarm-run":
        if not os.path.exists(os.environ.get("ARGIA_PL_DB", S.DEFAULT_DB)):
            print("alarm run skipped: the Prologis store is not available (encrypted volume locked?)")
            return 0
        now = dt.datetime.fromisoformat(argv[2]) if len(argv) >= 3 else now_mx()
        c = S.connect()
        cfg = None
        if AL.setting(c, "mail_mode") == "live":
            from argia.alerts import emailer
            cfg = emailer.load_smtp()
        r = AL.run(c, reg(), now, cfg=cfg)
        print(f"alarm run {now:%Y-%m-%d %H:%M} MX: " + ", ".join(f"{k}={v}" for k, v in r.items())
              + f" (mail mode {AL.setting(c, 'mail_mode')}{'' if cfg or AL.setting(c, 'mail_mode') != 'live' else ', NO SMTP CONFIG'})")
        return 0
    if len(argv) >= 2 and argv[1] == "--scan-uploads":
        if not os.path.exists(os.environ.get("ARGIA_PL_DB", S.DEFAULT_DB)):
            print("upload scan skipped: the Prologis store is not available (encrypted volume locked?)")
            return 0
        c = S.connect()
        r = SC.scan_pending(c, FILES_DIR, os.path.join(DATA_DIR, "quarantine"))
        if r["infected"]:
            bad = c.execute("SELECT * FROM documents WHERE scan_status='infected' AND scan_utc>=?",
                            ((utc_now() - dt.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
            subj, body = SC.infected_mail(bad)
            AL.queue(c, "malware", "scan", subj, body, AL.recipients(c, "critical"), False)
            AL.deliver(c, None if AL.setting(c, "mail_mode") != "live" else __import__("argia.alerts.emailer", fromlist=["x"]).load_smtp())
        print("upload scan: " + ", ".join(f"{k}={v}" for k, v in r.items()))
        return 0
    if len(argv) >= 2 and argv[1] == "--seed-sample-tickets":
        _seed_sample_tickets()
        return 0
    if len(argv) >= 2 and argv[1] in ("--seed-sample-handover", "--clear-sample-handover"):
        c = S.connect()
        if argv[1] == "--clear-sample-handover":
            print(f"sample handover rows removed: {HO.clear_sample(c)}")
        else:
            rg = reg()
            codes = [x.code for x in sorted(rg.sites, key=lambda x: (not x.operating, x.park, x.code))]
            print(f"sample handover rows written: {HO.seed_sample(c, codes, today_mx())} (real rows are never overwritten)")
        c.close()
        return 0
    os.makedirs(FILES_DIR, mode=0o700, exist_ok=True)
    reg()                                    # fail fast on a bad registry
    app.run(host="127.0.0.1", port=int(os.environ.get("ARGIA_PL_PORT", "8520")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
