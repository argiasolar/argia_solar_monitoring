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

Metering is labelled SAMPLE until Prologis grants SolarEdge access
(argia.prologis.metering). Customer data (registry, database, files,
logo) lives only under /opt/argia/prologis on the server.

    python3 prologis_app.py                     serve
    python3 prologis_app.py --create-admin USER "Full name" EMAIL
    python3 prologis_app.py --seed-sample-tickets
    python3 prologis_app.py --seed-catalog FILE  add missing services from a JSON list (never overwrites)
    python3 prologis_app.py --seed-parts FILE    add missing spare parts and minimums (never overwrites)
    python3 prologis_app.py --alarm-run [ISO]    one alarm engine pass (the 5-minute timer; ISO = MX time, tests)
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import mimetypes
import os
import sys
import time
import zipfile
from typing import Dict, List, Optional

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
from argia.prologis import metering as M                  # noqa: E402
from argia.prologis import monthly as MR                  # noqa: E402
from argia.prologis import registry as R                  # noqa: E402
from argia.prologis import sla as SLA                     # noqa: E402
from argia.prologis import store as S                     # noqa: E402
from argia.prologis import totp as TOTP                   # noqa: E402
from argia.prologis import xlsx as XL                     # noqa: E402

DATA_DIR = os.environ.get("ARGIA_PL_DIR", "/opt/argia/prologis")
FILES_DIR = os.environ.get("ARGIA_PL_FILES", os.path.join(DATA_DIR, "files"))
BRAND_DIR = os.path.join(DATA_DIR, "brand")
# v294: prologis_logo_white.png is the reversed logo (white wordmark, as on the
# Prologis buildings) for the dark green header; the colour logo stays the fallback
BRAND_FILES = ("prologis_logo.png", "prologis_logo_white.png", "prologis_mark.png")
COOKIE = "pl_sid"
SECURE_COOKIE = os.environ.get("ARGIA_PL_INSECURE_COOKIE", "") != "1"
MAX_UPLOAD = 50 * 1024 * 1024
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".dwg", ".dxf", ".xlsx", ".xls", ".csv", ".docx",
               ".doc", ".pptx", ".zip", ".txt", ".kmz", ".mp4", ".heic"}
PUBLIC = {"login", "login_post", "healthz", "brand", "lang", "favicon"}
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
    return R.load()


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
    if not row["mfa_ok"] and request.endpoint not in ("mfa", "mfa_post", "logout"):
        return redirect("/mfa")
    if row["mfa_ok"] and row["must_change"] and request.endpoint not in ("password", "password_post", "logout"):
        return redirect("/password")
    return None


@app.after_request
def _headers(r):
    if r.mimetype == "text/html" and not r.direct_passthrough:
        r.set_data(plain(r.get_data(as_text=True)))
    r.headers.setdefault("Cache-Control", "no-store")
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "same-origin"
    r.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: https://server.arcgisonline.com https://cdnjs.cloudflare.com; "
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
           ("/shop/", tt("Services", "Servicios"), "shop"), ("/assets/", tt("Assets", "Activos"), "assets"),
           ("/alarms/", tt("Alarms", "Alarmas"), "alarms"), ("/reports/", tt("Reports", "Reportes"), "reports"),
           ("/projects/", tt("Projects", "Proyectos"), "projects"), ("/docs/", tt("Documents", "Documentos"), "docs")]
    links = "".join(f'<a href="{h}" class="{"on" if k == on else ""}">{UI.e(lbl)}</a>' for h, lbl, k in nav)
    other = "es" if tt.lang == "en" else "en"
    users_link = (f'<a href="/admin/users/" class="{"on" if on == "users" else ""}">{tt("Users", "Usuarios")}</a> · '
                  if S.can(u.get("role", ""), "users") else "")
    who = (f'<div class="who">{UI.e(u.get("name") or u.get("username", ""))} · {users_link}'
           f'<a href="/lang/{other}">{other.upper()}</a> · <a href="/security/">{tt("Security", "Seguridad")}</a>'
           f' · <a href="/logout">{tt("Sign out", "Salir")}</a></div>')
    banner = (f'<div class="sample">{tt("SAMPLE DATA - simulated production until Prologis grants ARGIA access to the SolarEdge / Hark accounts. Sites, sizes and locations are real.", "DATOS DE MUESTRA - producción simulada hasta que Prologis otorgue a ARGIA acceso a SolarEdge / Hark. Sitios, tamaños y ubicaciones son reales.")}</div>'
              if sample else "")
    pl = _logo()
    html_ = f"""<!doctype html><html lang="{tt.lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>{UI.e(title)} - ARGIA for Prologis</title>{ICON_LINKS}<style>{UI.CSS}</style></head><body>
<div class="top"><div class="stripe"></div><div class="topin">
<a class="brand" href="/">{pl or UI.stripe_svg(26) + ' PROLOGIS'}<span class="x">×</span>
<img class="ar" src="{argia_logo.MARK_URI}" alt="ARGIA"><span class="tag">{tt("Solar O&M platform", "Plataforma O&M solar")}</span></a>
<nav class="main">{links}</nav>{who}</div></div>{banner}
<main>{body}</main>
<div class="foot"><span>ARGIA for Prologis · {tt("operated by", "operado por")} ARGIA - Smart Energy Solutions</span>
<span>{tt("Times in Mexico City time", "Horas en tiempo de Ciudad de México")}</span><a href="/security/">{tt("Security & data", "Seguridad y datos")}</a></div>
</body></html>"""
    return Response(html_, mimetype="text/html")


def flash(msg: str, err: bool = False) -> str:
    return f'<div class="flash{" err" if err else ""}">{UI.e(msg)}</div>' if msg else ""


# ------------------------------------------------------------------ public
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/brand/<name>")
def brand(name):
    if name not in BRAND_FILES:
        abort(404)
    p = os.path.join(BRAND_DIR, name)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="image/png", max_age=86400)


@app.get("/favicon.png", endpoint="favicon")
@app.get("/favicon.ico", endpoint="favicon")
@app.get("/apple-touch-icon.png", endpoint="favicon")
def favicon():
    for name in FAVICONS[request.path.lstrip("/")]:
        p = os.path.join(BRAND_DIR, name)
        if os.path.exists(p):
            return send_file(p, mimetype="image/png", max_age=86400)
    abort(404)


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
<div><div class="kick" style="color:#9fe3dc">{tt("Solar O&M platform - Mexico", "Plataforma O&M solar - México")}</div>
<h1>{tt("Every rooftop. Every kilowatt. One view.", "Cada techo. Cada kilowatt. Una vista.")}</h1>
<p style="color:#cfe6e3;max-width:520px">{tt("Live production, response-time tickets on your MSA classes, construction pipeline and documentation for the Prologis rooftop solar portfolio, operated by ARGIA.", "Producción en vivo, tickets con los tiempos de respuesta de su MSA, avance de construcción y documentación del portafolio solar de Prologis, operado por ARGIA.")}</p></div>
<div class="small" style="color:#9fc9c4">{tt("Private platform prepared for Prologis by ARGIA - Smart Energy Solutions. Authorised users only; every access is logged.", "Plataforma privada preparada para Prologis por ARGIA - Smart Energy Solutions. Solo usuarios autorizados; todo acceso queda registrado.")}</div></div>
<div class="box"><form method="post" action="/login"><div class="kick">{tt("Sign in", "Iniciar sesión")}</div>
<h1 class="pt" style="margin-bottom:14px">ARGIA for Prologis</h1>{flash(msg, True)}
<input type="hidden" name="next" value="{nxt}">
<label>{tt("User", "Usuario")}</label><input name="username" autocomplete="username" required autofocus>
<label>{tt("Password", "Contraseña")}</label><input name="password" type="password" autocomplete="current-password" required>
<div style="margin-top:16px"><button class="btn" type="submit">{tt("Continue", "Continuar")}</button></div>
<p class="muted small" style="margin-top:18px">{tt("A second factor (authenticator app) is required after the password.", "Después de la contraseña se requiere un segundo factor (app autenticadora).")}
<a href="/lang/{"es" if tt.lang == "en" else "en"}">{"Español" if tt.lang == "en" else "English"}</a></p></form></div></div></body></html>"""
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
    sid = S.new_session(c, u, ip(), now, mfa_ok=False)
    S.audit(c, u, ip(), "login_password_ok", u)
    nxt = request.form.get("next") or "/"
    r = redirect("/mfa?next=" + (nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"))
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


def _map_points(rg: R.Registry, lv: Dict[str, M.LiveState], projects: bool) -> List[Dict]:
    pts = []
    for s in rg.sites:
        st = lv[s.code]
        pts.append({"lat": s.lat, "lon": s.lon, "c": MAP_COL.get(st.status, "#999"), "code": s.code, "name": s.name,
                    "kwp": s.kwp, "kw": st.kw_now, "kwh": st.kwh_today, "st": st.status, "approx": s.geo_approx,
                    "url": f"/sites/{s.code}/", "g": "op" if s.operating else "pre", "sub": s.city})
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


def map_block(rg: R.Registry, lv: Dict[str, M.LiveState], h_cls: str = "", projects: bool = True, mode: str = "list") -> str:
    """The Leaflet map with a legend of every plant, name and size.
    mode "list": the legend only names them (overview). mode "select": each
    plant and each group has a tick box; unticked plants leave the map and
    the tiles (see map_page) total what stays ticked. The choice is kept in
    this browser (localStorage, per-viewer convenience only)."""
    tt = t()
    pts = _map_points(rg, lv, projects)
    sel = mode == "select"
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
    status = (f'<div class="legend small" style="margin-top:8px"><span><i style="background:{UI.LIME}"></i>{tt("Producing", "Produciendo")}</span>'
              f'<span><i style="background:{UI.RED}"></i>{tt("Inverter fault", "Falla de inversor")}</span><span><i style="background:{UI.AMBER}"></i>{tt("Communication loss", "Sin comunicación")}</span>'
              f'<span><i style="background:#7f8c8a"></i>{tt("Night", "Noche")}</span><span><i style="background:{UI.SKY}"></i>{tt("Before PTO", "Antes de PTO")}</span>'
              + (f'<span><i style="background:{UI.DEEP}"></i>{tt("EPC project", "Proyecto EPC")}</span>' if projects else "") + "</div>")
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
    return f"""<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<div id="map" class="{h_cls}"></div><script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
{status}{legend}
<script>(function(){{var P={json.dumps(pts)};var m=L.map('map',{{scrollWheelZoom:false}});var MK={{}};
var st=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri',maxZoom:19}}).addTo(m);
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri, Maxar, Earthstar Geographics',maxZoom:19}});
L.control.layers({{'Streets':st,'Satellite':sat}}).addTo(m);
P.forEach(function(p){{var r=Math.max(7,Math.min(16,Math.sqrt(p.kwp||100)/2.2));
var mk=L.circleMarker([p.lat,p.lon],{{radius:r,color:'#fff',weight:2,fillColor:p.c,fillOpacity:.95}}).addTo(m);MK[p.code]=mk;
mk.bindPopup('<b>'+p.name+'</b><br>'+p.code+' · '+Math.round(p.kwp)+' kWp<br>'+(p.kw!=null?('Now: '+Math.round(p.kw)+' kW · today '+Math.round(p.kwh)+' kWh<br>'):'')+p.st.replace('_',' ')+(p.approx?'<br><i>approximate location</i>':'')+'<br><a href="'+p.url+'">Open</a>');}});
function fitAll(){{var b=[];P.forEach(function(p){{if(p.g!=='epc')b.push([p.lat,p.lon]);}});if(!b.length)P.forEach(function(p){{b.push([p.lat,p.lon]);}});
if(b.length)m.fitBounds(b,{{padding:[30,30],maxZoom:13}});}}
{js_sel}}})();</script>"""



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
<h1>{tt("Good", "Buen")} {tt("morning" if now.hour < 12 else ("afternoon" if now.hour < 19 else "evening"), "día" if now.hour < 12 else ("tarde" if now.hour < 19 else "noche"))}{", " + UI.e((g.user or {}).get("name", "").split(" ")[0]) if (g.user or {}).get("name") else ""}.</h1>
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
            + tiles + script + '<div style="margin-top:14px">' + map_block(rg, lv, mode="select") + "</div>")
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
    for s in sorted(rg.sites, key=lambda x: (not x.operating, x.park, x.code)):
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
        rows += (f'<tr><td><a href="/sites/{s.code}/"><b>{UI.e(s.name)}</b></a><div class="small muted">{s.code} · {UI.e(s.city)}</div></td>'
                 f'<td>{UI.status_pill(x.status, tt)}</td><td class="n">{UI.num(s.kwp, 0)}</td><td class="n">{UI.num(x.kw_now)}</td>'
                 f'<td class="n">{UI.num(x.kwh_today)}</td>{extra}</tr>')
    body = f"""<div class="kick">{tt("Sites", "Sitios")}</div><h1 class="pt">{tt("Every rooftop, live", "Cada techo, en vivo")}</h1>
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
<span class="chip">{UI.e(s.monitoring)}</span><span class="chip">{UI.e(s.address)}</span></div>{live_cards}
<div class="grid g2e"><div class="card"><h2>{tt("Tickets", "Tickets")}<span class="r"><a class="btn sm ghost" href="/shop/">{tt("Order a service", "Pedir servicio")}</a> <a class="btn sm" href="/tickets/new?site={s.code}">{tt("New", "Nuevo")}</a></span></h2><table class="t">{trs}</table></div>
<div class="card"><h2>{tt("Documents", "Documentos")}<span class="r"><a href="/docs/?site={s.code}">{tt("Folder", "Carpeta")} ›</a></span></h2><table class="t">{drs}</table></div></div>
<div class="card" style="margin-top:16px"><h2>{tt("Equipment", "Equipos")}<span class="r"><a href="/assets/?site={s.code}">{tt("Register", "Registro")} ›</a></span></h2>{eq_html}</div>"""
    return page(s.name, body, "sites", sample=s.operating)


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
            rows += f'<tr><td><b>{UI.e(s.name)}</b><div class="small muted">{s.code}</div></td><td colspan="8" class="muted small">{tt("Contract terms not set (effective date, kWh rate, annual fee).", "Términos del contrato sin fijar (fecha efectiva, tarifa kWh, cuota anual).")}</td></tr>'
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
        lis = "".join(f'<tr><td><a href="/docs/{d["id"]}/download">{UI.e(d["name"])}</a><div class="small muted">{UI.e(d["site_code"])} · {d["size"] / 1024:,.0f} KB · {UI.e(d["uploaded_by"])} · {mx_time(d["uploaded_utc"])}</div></td></tr>' for d in items)
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
            f'<p class="small muted">{tt("Every upload and download is recorded in the audit log. Files are stored encrypted in transit (TLS) in ARGIA&#39;s Prologis-only file store.", "Cada carga y descarga queda en la bitácora. Los archivos viajan cifrados (TLS) al almacén exclusivo de Prologis en ARGIA.")}</p>'
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
        (tt("Two-factor sign-in", "Acceso con dos factores"), tt("Password (scrypt-hashed, 12+ characters) plus an authenticator code (TOTP) for every user.", "Contraseña (scrypt, 12+ caracteres) más código de app autenticadora (TOTP) para cada usuario.")),
        (tt("Least privilege", "Mínimo privilegio"), tt("Four roles; Prologis managers approve dispatches, ARGIA operators work tickets, only administrators manage users.", "Cuatro roles; los gerentes de Prologis aprueban despachos, los operadores de ARGIA trabajan tickets, solo administradores gestionan usuarios.")),
        (tt("Encryption in transit", "Cifrado en tránsito"), tt("TLS 1.2 / 1.3 only, HSTS, strict content-security policy.", "Solo TLS 1.2 / 1.3, HSTS, política de contenido estricta.")),
        (tt("Every action logged", "Todo queda registrado"), tt("Sign-ins, failures, uploads, downloads, ticket and user changes - downloadable by Prologis managers.", "Accesos, fallos, cargas, descargas, cambios de tickets y usuarios - descargable por gerentes de Prologis.")),
        (tt("Your data, exportable", "Sus datos, exportables"), tt("One click exports sites, energy, tickets, documents index, users and audit log as CSV.", "Un clic exporta sitios, energía, tickets, índice de documentos, usuarios y bitácora en CSV.")),
        (tt("Brute-force protection", "Protección contra fuerza bruta"), tt("Lock-out after 5 failed attempts per user or address; idle sessions end after 8 hours.", "Bloqueo tras 5 intentos fallidos por usuario o dirección; sesiones inactivas terminan a las 8 horas.")),
        (tt("Tested releases", "Versiones probadas"), tt("Every release passes 4,500+ automated tests before it reaches the server; the server is checked against the code every morning.", "Cada versión pasa más de 4,500 pruebas automáticas antes de llegar al servidor; el servidor se compara con el código cada mañana.")),
    ]
    cards = "".join(f'<div class="card"><h2>{UI.stripe_svg(16)} {UI.e(a)}</h2><div class="small">{UI.e(b)}</div></div>' for a, b in items)
    body = (f'<div class="kick">{tt("Trust", "Confianza")}</div><h1 class="pt">{tt("How we protect Prologis data", "Cómo protegemos los datos de Prologis")}</h1>'
            f'<div class="grid g2e">{cards}</div>'
            + (f'<p style="margin-top:16px"><a class="btn" href="/export.zip">{tt("Export all data", "Exportar todos los datos")}</a> <a class="btn ghost" href="/audit/">{tt("Audit log", "Bitácora")}</a></p>' if S.can(g.user["role"], "export") else ""))
    return page(tt("Security", "Seguridad"), body)


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
    if len(argv) >= 2 and argv[1] == "--seed-sample-tickets":
        _seed_sample_tickets()
        return 0
    os.makedirs(FILES_DIR, mode=0o700, exist_ok=True)
    reg()                                    # fail fast on a bad registry
    app.run(host="127.0.0.1", port=int(os.environ.get("ARGIA_PL_PORT", "8520")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
