#!/usr/bin/env python3
"""ARGIA for Prologis - the customer platform at prologis.argia.com.mx (v292).

Runs on 127.0.0.1:8520 behind nginx (TLS). Its own login - separate from
the ARGIA portal: own user database, scrypt passwords, mandatory TOTP
second factor, roles, idle timeout, CSRF tokens on every form, an audit
log of every login, change, upload and download, and a data export.

    /                 overview: live portfolio, map, alerts, tickets, projects
    /map/             every site on a satellite map
    /sites/           all sites, live; /sites/<code>/ one site
    /tickets/         O&M tickets on the MSA response-time classes
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
from argia.prologis import metering as M                  # noqa: E402
from argia.prologis import registry as R                  # noqa: E402
from argia.prologis import sla as SLA                     # noqa: E402
from argia.prologis import store as S                     # noqa: E402
from argia.prologis import totp as TOTP                   # noqa: E402

DATA_DIR = os.environ.get("ARGIA_PL_DIR", "/opt/argia/prologis")
FILES_DIR = os.environ.get("ARGIA_PL_FILES", os.path.join(DATA_DIR, "files"))
BRAND_DIR = os.path.join(DATA_DIR, "brand")
COOKIE = "pl_sid"
SECURE_COOKIE = os.environ.get("ARGIA_PL_INSECURE_COOKIE", "") != "1"
MAX_UPLOAD = 50 * 1024 * 1024
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".dwg", ".dxf", ".xlsx", ".xls", ".csv", ".docx",
               ".doc", ".pptx", ".zip", ".txt", ".kmz", ".mp4", ".heic"}
PUBLIC = {"login", "login_post", "healthz", "brand", "lang"}
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


def _brand_img(name: str, cls: str, alt: str) -> str:
    return (f'<img class="{cls}" src="/brand/{name}" alt="{UI.e(alt)}">'
            if os.path.exists(os.path.join(BRAND_DIR, name)) else "")


def page(title: str, body: str, on: str = "", sample: bool = False, wide: bool = True) -> Response:
    tt = t()
    u = g.user or {}
    nav = [("/", tt("Overview", "Resumen"), "home"), ("/map/", tt("Map", "Mapa"), "map"),
           ("/sites/", tt("Sites", "Sitios"), "sites"), ("/tickets/", tt("Tickets", "Tickets"), "tickets"),
           ("/projects/", tt("Projects", "Proyectos"), "projects"), ("/docs/", tt("Documents", "Documentos"), "docs")]
    if S.can(u.get("role", ""), "users"):
        nav.append(("/admin/users/", tt("Users", "Usuarios"), "users"))
    links = "".join(f'<a href="{h}" class="{"on" if k == on else ""}">{UI.e(lbl)}</a>' for h, lbl, k in nav)
    other = "es" if tt.lang == "en" else "en"
    who = (f'<div class="who">{UI.e(u.get("name") or u.get("username", ""))} · '
           f'<a href="/lang/{other}">{other.upper()}</a> · <a href="/security/">{tt("Security", "Seguridad")}</a>'
           f' · <a href="/logout">{tt("Sign out", "Salir")}</a></div>')
    banner = (f'<div class="sample">{tt("SAMPLE DATA - simulated production until Prologis grants ARGIA access to the SolarEdge / Hark accounts. Sites, sizes and locations are real.", "DATOS DE MUESTRA - producción simulada hasta que Prologis otorgue a ARGIA acceso a SolarEdge / Hark. Sitios, tamaños y ubicaciones son reales.")}</div>'
              if sample else "")
    pl = _brand_img("prologis_logo.png", "pl", "Prologis")
    html_ = f"""<!doctype html><html lang="{tt.lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>{UI.e(title)} - ARGIA for Prologis</title><style>{UI.CSS}</style></head><body>
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
    if name not in ("prologis_logo.png", "prologis_mark.png"):
        abort(404)
    p = os.path.join(BRAND_DIR, name)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="image/png", max_age=86400)


@app.get("/lang/<code>")
def lang(code):
    r = redirect(request.referrer or "/")
    r.set_cookie("pl_lang", "es" if code == "es" else "en", max_age=365 * 86400, samesite="Lax", secure=SECURE_COOKIE)
    return r


def _login_page(msg: str = "", status: int = 200) -> Response:
    tt = UI.T(request.cookies.get("pl_lang", "en"))
    pl = _brand_img("prologis_logo.png", "pl", "Prologis")
    nxt = UI.e(request.args.get("next", "/"))
    html_ = f"""<!doctype html><html lang="{tt.lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Sign in - ARGIA for Prologis</title><style>{UI.CSS}
.login .art .brandrow{{display:flex;align-items:center;gap:14px}} .login .art img.pl{{height:34px;background:#fff;border-radius:8px;padding:4px 10px}}
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
    return c.execute(f"SELECT * FROM tickets WHERE status IN ({q}) ORDER BY id DESC", S.OPEN).fetchall()


def sla_of(c, tk, now_utc: dt.datetime) -> tuple:
    cls = SLA.BY_CODE.get(tk["sla_class"], SLA.BY_CODE["OTHER"])
    det = dt.datetime.fromisoformat(tk["detected_utc"])
    appr = dt.datetime.fromisoformat(tk["approved_utc"]) if tk["approved_utc"] else None
    start = SLA.clock_start(cls, det, appr if tk["approval"] in ("approved",) else (det if tk["approval"] == "not_required" else None))
    if start is None:
        return cls, None, "not_started"
    end = dt.datetime.fromisoformat(tk["responded_utc"]) if tk["responded_utc"] else now_utc
    spans = [(dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b) if b else None)
             for a, b in S.waiting_spans(S.ticket_events(c, tk["id"]))]
    used = SLA.elapsed_hours(start, end, spans)
    return cls, used, SLA.status(cls, used, bool(tk["responded_utc"]))


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


def map_block(rg: R.Registry, lv: Dict[str, M.LiveState], h_cls: str = "", projects: bool = True) -> str:
    pts = []
    col = {"producing": UI.LIME, "night": "#7f8c8a", "inverter_fault": UI.RED, "comm_loss": UI.AMBER, "pre_pto": UI.SKY}
    for s in rg.sites:
        st = lv[s.code]
        pts.append({"lat": s.lat, "lon": s.lon, "c": col.get(st.status, "#999"), "code": s.code, "name": s.name,
                    "kwp": s.kwp, "kw": st.kw_now, "kwh": st.kwh_today, "st": st.status, "approx": s.geo_approx,
                    "url": f"/sites/{s.code}/"})
    if projects:
        for p in rg.projects:
            if p.lat is not None and not rg.site(p.site_code):
                pts.append({"lat": p.lat, "lon": p.lon, "c": UI.DEEP, "code": p.id, "name": p.name, "kwp": p.kwp or 0,
                            "kw": None, "kwh": 0, "st": "project:" + p.stage, "approx": True, "url": "/projects/"})
    data = json.dumps(pts)
    return f"""<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<div id="map" class="{h_cls}"></div><script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>(function(){{var P={data};var m=L.map('map',{{scrollWheelZoom:false}});
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri, Maxar, Earthstar Geographics',maxZoom:19}}).addTo(m);
var st=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',{{attribution:'&copy; Esri',maxZoom:19}});
L.control.layers({{'Satellite':sat,'Streets':st}}).addTo(m);var b=[];
P.forEach(function(p){{var r=Math.max(7,Math.min(16,Math.sqrt(p.kwp||100)/2.2));
var mk=L.circleMarker([p.lat,p.lon],{{radius:r,color:'#fff',weight:2,fillColor:p.c,fillOpacity:.95}}).addTo(m);
mk.bindPopup('<b>'+p.name+'</b><br>'+p.code+' · '+Math.round(p.kwp)+' kWp<br>'+(p.kw!=null?('Now: '+Math.round(p.kw)+' kW · today '+Math.round(p.kwh)+' kWh<br>'):'')+p.st.replace('_',' ')+(p.approx?'<br><i>approximate location</i>':'')+'<br><a href="'+p.url+'">Open</a>');if(p.st.indexOf('project:')!==0)b.push([p.lat,p.lon]);}});
if(!b.length)P.forEach(function(p){{b.push([p.lat,p.lon]);}});
if(b.length)m.fitBounds(b,{{padding:[30,30],maxZoom:13}});}})();</script>
<div class="legend small" style="margin-top:8px"><span><i style="background:{UI.LIME}"></i>Producing</span><span><i style="background:{UI.RED}"></i>Inverter fault</span>
<span><i style="background:{UI.AMBER}"></i>Communication loss</span><span><i style="background:#7f8c8a"></i>Night</span><span><i style="background:{UI.SKY}"></i>Before PTO</span><span><i style="background:{UI.DEEP}"></i>EPC project</span></div>"""


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
    body = f"""<div class="hero"><div class="glow"></div><div class="k">{UI.e(rg.portfolio.get("market", "Mexico City"))} · {tt("Rooftop solar portfolio", "Portafolio solar en techos")}</div>
<h1>{tt("Good", "Buen")} {tt("morning" if now.hour < 12 else ("afternoon" if now.hour < 19 else "evening"), "día" if now.hour < 12 else ("tarde" if now.hour < 19 else "noche"))}{", " + UI.e((g.user or {}).get("name", "").split(" ")[0]) if (g.user or {}).get("name") else ""}.</h1>
<div class="sub"><span class="live-dot"></span>{tt("Live", "En vivo")} · {now.strftime("%d %b %Y %H:%M")} · {k["operating"]} {tt("operating sites", "sitios operando")} · {UI.num(k["kwp"] / 1000, 2)} MWp {tt("under ARGIA care", "a cargo de ARGIA")}</div>
<div class="kpis"><div class="kpi"><div class="l">{tt("Power now", "Potencia ahora")}</div><div class="v" id="kw">{UI.num(k["kw_now"] / 1000, 2)}<small>MW</small></div><div class="d">{tt("of", "de")} {UI.num(k["kwp_operating"] / 1000, 2)} MWp</div></div>
<div class="kpi"><div class="l">{tt("Energy today", "Energía hoy")}</div><div class="v" id="kwh">{UI.num(k["kwh_today"] / 1000, 2)}<small>MWh</small></div><div class="d">{tt("so far", "hasta ahora")}</div></div>
<div class="kpi"><div class="l">{tt("Last 30 days", "Últimos 30 días")}</div><div class="v">{UI.num(k["mwh_30d"], 0)}<small>MWh</small></div><div class="d">{UI.num(k["co2_t_30d"], 0)} t CO₂ {tt("avoided", "evitadas")}</div></div>
<div class="kpi"><div class="l">{tt("Availability 30 d", "Disponibilidad 30 d")}</div><div class="v">{k["availability_30d"] * 100:.2f}<small>%</small></div><div class="d">{tt("MSA method, >150 W/m²", "Método MSA, >150 W/m²")}</div></div>
<div class="kpi"><div class="l">{tt("Performance ratio", "Performance ratio")}</div><div class="v">{k["pr_30d"] * 100:.1f}<small>%</small></div><div class="d">{tt("30-day, weighted", "30 días, ponderado")}</div></div>
<div class="kpi"><div class="l">{tt("Open tickets", "Tickets abiertos")}</div><div class="v">{len(tks)}</div><div class="d">{k["alerts"]} {tt("live alerts", "alertas en vivo")}</div></div></div></div>
<div class="grid g2"><div class="card"><h2>{tt("Portfolio power today", "Potencia del portafolio hoy")}<span class="r muted">{tt("dashed: clear-sky expectation", "punteado: esperado cielo despejado")}</span></h2>{curve}</div>
<div class="card"><h2>{tt("Live alerts", "Alertas en vivo")}<span class="r"><a href="/sites/">{tt("All sites", "Todos los sitios")} ›</a></span></h2><table class="t">{al}</table></div></div>
<div class="grid g2"><div class="card"><h2>{tt("Where your energy is made", "Dónde se produce su energía")}<span class="r"><a href="/map/">{tt("Full map", "Mapa completo")} ›</a></span></h2>{map_block(rg, lv, "mini-map", projects=False)}</div>
<div><div class="card"><h2>{tt("Open tickets", "Tickets abiertos")}<span class="r"><a href="/tickets/">{tt("All", "Todos")} ›</a></span></h2><table class="t">{trows}</table></div>
<div class="card" style="margin-top:16px"><h2>{tt("Construction pipeline", "Avance de construcción")}<span class="r"><a href="/projects/">{tt("Projects", "Proyectos")} ›</a></span></h2>{projs or tt("No projects.", "Sin proyectos.")}</div></div></div>
<script>setTimeout(function(){{location.reload()}},300000);</script>"""
    return page(tt("Overview", "Resumen"), body, "home", sample=True)


@app.get("/map/")
def map_page():
    tt = t()
    rg = reg()
    lv = live_all(rg, now_mx())
    body = (f'<div class="kick">{tt("Portfolio map", "Mapa del portafolio")}</div><h1 class="pt">{len(rg.sites)} {tt("rooftops", "techos")} · {UI.num(rg.kwp_total / 1000, 2)} MWp</h1>'
            f'<p class="muted small">{tt("Locations are approximate (industrial park level) until exact coordinates are captured at onboarding.", "Ubicaciones aproximadas (a nivel parque industrial) hasta registrar coordenadas exactas en el onboarding.")}</p>'
            + map_block(rg, lv))
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
    tks = c.execute("SELECT * FROM tickets WHERE site_code=? ORDER BY id DESC LIMIT 8", (s.code,)).fetchall()
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
    trs = "".join(f'<tr><td><a href="/tickets/{tk["number"]}/">{tk["number"]}</a></td><td>{UI.e(tk["title"])}</td><td>{UI.e(dict((k, en) for k, en, _ in S.TICKET_STATUSES).get(tk["status"], tk["status"]))}</td></tr>' for tk in tks) \
        or f'<tr><td class="muted">{tt("No tickets.", "Sin tickets.")}</td></tr>'
    drs = "".join(f'<tr><td><a href="/docs/{d["id"]}/download">{UI.e(d["name"])}</a></td><td class="small muted">{UI.e(d["folder"])}</td></tr>' for d in docs) \
        or f'<tr><td class="muted">{tt("No documents yet.", "Sin documentos aún.")}</td></tr>'
    body = f"""<div class="kick">{UI.e(s.park)} · {UI.e(s.city)}</div><h1 class="pt">{UI.e(s.name)}</h1>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 4px"><span class="chip">{s.code}</span><span class="chip">{UI.num(s.kwp, 1)} kWp</span><span class="chip">PTO {UI.e(s.pto)}</span>
<span class="chip">{UI.e(s.monitoring)}</span><span class="chip">{UI.e(s.address)}</span></div>{live_cards}
<div class="grid g2e"><div class="card"><h2>{tt("Tickets", "Tickets")}<span class="r"><a class="btn sm" href="/tickets/new?site={s.code}">{tt("New", "Nuevo")}</a></span></h2><table class="t">{trs}</table></div>
<div class="card"><h2>{tt("Documents", "Documentos")}<span class="r"><a href="/docs/?site={s.code}">{tt("Folder", "Carpeta")} ›</a></span></h2><table class="t">{drs}</table></div></div>"""
    return page(s.name, body, "sites", sample=s.operating)


# ------------------------------------------------------------------ tickets
@app.get("/tickets/")
def tickets():
    tt = t()
    rg = reg()
    c = db()
    show = request.args.get("show", "open")
    rows_db = open_tickets(c) if show == "open" else c.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    nowu = utc_now()
    rows, results = "", []
    for tk in c.execute("SELECT * FROM tickets").fetchall():
        results.append(sla_of(c, tk, nowu)[2])
    comp = SLA.compliance(results)
    for tk in rows_db:
        cls, used, st = sla_of(c, tk, nowu)
        s = rg.site(tk["site_code"])
        stl = dict((k, tt(en, es)) for k, en, es in S.TICKET_STATUSES)[tk["status"]]
        appr = {"pending": f'<span class="pill s-warn">{tt("Approval pending", "Aprobación pendiente")}</span>',
                "approved": f'<span class="pill s-ok">{tt("Approved", "Aprobado")}</span>',
                "rejected": f'<span class="pill s-bad">{tt("Rejected", "Rechazado")}</span>'}.get(tk["approval"], "")
        rows += (f'<tr><td class="nw"><a href="/tickets/{tk["number"]}/"><b>{tk["number"]}</b></a>{SAMPLE_PILL if tk["sample"] else ""}</td>'
                 f'<td>{UI.e(tk["title"])}<div class="small muted">{UI.e(s.name if s else tk["site_code"])}</div></td>'
                 f'<td><span class="pill s-off">{cls.priority}</span> <span class="small">{UI.e(tt(cls.en, cls.es))}</span></td>'
                 f'<td>{stl} {appr}</td><td>{sla_pill(cls, used, st, tt)}</td><td class="small muted">{mx_time(tk["detected_utc"])}</td></tr>')
    rows = rows or f'<tr><td class="muted">{tt("No tickets.", "Sin tickets.")}</td></tr>'
    new = f'<a class="btn" href="/tickets/new">{tt("New ticket", "Nuevo ticket")}</a>' if S.can(g.user["role"], "ticket_new") else ""
    body = f"""<div style="display:flex;align-items:end;gap:12px;flex-wrap:wrap"><div><div class="kick">{tt("Operation & maintenance", "Operación y mantenimiento")}</div>
<h1 class="pt">{tt("Tickets", "Tickets")}</h1></div><div style="margin-left:auto;display:flex;gap:8px">{new}
<a class="btn ghost" href="?show={"all" if show == "open" else "open"}">{tt("Show all" if show == "open" else "Open only", "Ver todos" if show == "open" else "Solo abiertos")}</a></div></div>
<div class="grid g3"><div class="card"><h2>{tt("Response-time compliance", "Cumplimiento de tiempo de respuesta")}</h2><div style="font-size:30px;font-weight:800;color:var(--deep)">{"-" if comp is None else f"{comp * 100:.0f}%"}</div><div class="small muted">{tt("closed clocks that met the MSA deadline", "relojes cerrados dentro del plazo del MSA")}</div></div>
<div class="card"><h2>{tt("Open", "Abiertos")}</h2><div style="font-size:30px;font-weight:800;color:var(--deep)">{len(open_tickets(c))}</div><div class="small muted">{tt("new, responded, in progress or waiting", "nuevos, atendidos, en curso o en espera")}</div></div>
<div class="card"><h2>{tt("How the clock works", "Cómo corre el reloj")}</h2><div class="small">{tt("Classes and deadlines are the MSA's (Schedule A). The clock starts at detection, or at Prologis's dispatch approval where the MSA requires it, and stops while waiting on Prologis or site access.", "Clases y plazos del MSA (Anexo A). El reloj inicia en la detección, o en la aprobación de despacho de Prologis cuando el MSA lo exige, y se detiene mientras se espera a Prologis o al acceso.")}</div></div></div>
<div class="card" style="margin-top:16px;overflow-x:auto"><table class="t"><tr><th>#</th><th>{tt("Issue", "Asunto")}</th><th>{tt("MSA class", "Clase MSA")}</th><th>{tt("Status", "Estado")}</th><th>{tt("Response clock", "Reloj de respuesta")}</th><th>{tt("Detected", "Detectado")}</th></tr>{rows}</table></div>"""
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
    tt = t()
    c = db()
    tk = _ticket(number)
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
    acts += (f'<form method="post" action="/tickets/{number}/comment" enctype="multipart/form-data" style="margin-top:12px">{csrf_field()}<label>{tt("Comment", "Comentario")}</label>'
             f'<textarea name="body"></textarea><input type="file" name="file" style="margin-top:6px"><div style="margin-top:8px"><button class="btn sm">{tt("Add", "Agregar")}</button></div></form>')
    chips = (('<span class="chip">' + UI.num(tk["kw_lost"]) + ' kW</span>') if tk["kw_lost"] else "") + \
        (('<span class="chip">MXN ' + UI.num(tk["estimate_mxn"]) + '</span>') if tk["estimate_mxn"] else "")
    body = f"""<div class="kick">{UI.e(s.name if s else tk["site_code"])} · {tk["number"]}{" · SAMPLE" if tk["sample"] else ""}</div><h1 class="pt">{UI.e(tk["title"])}</h1>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0"><span class="chip">{cls.priority} · {UI.e(tt(cls.en, cls.es))}</span><span class="chip">{UI.e(dict((k, tt(en, es)) for k, en, es in S.TICKET_STATUSES)[tk["status"]])}</span>{sla_pill(cls, used, st, tt)}
{chips}</div>
<div class="grid g2"><div class="card"><h2>{tt("Timeline", "Historial")}</h2><div class="tl">{tl}</div></div><div class="card"><h2>{tt("Actions", "Acciones")}</h2>
<div class="small muted">{tt("Detected", "Detectado")} {mx_time(tk["detected_utc"])} · {tt("Responded", "Atendido")} {mx_time(tk["responded_utc"])} · {tt("Resolved", "Resuelto")} {mx_time(tk["resolved_utc"])}</div>{acts}</div></div>"""
    return page(tk["number"], body, "tickets")


@app.post("/tickets/<number>/status")
def ticket_status(number):
    need("ticket_work")
    tk = _ticket(number)
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
    if len(argv) >= 2 and argv[1] == "--seed-sample-tickets":
        _seed_sample_tickets()
        return 0
    os.makedirs(FILES_DIR, mode=0o700, exist_ok=True)
    reg()                                    # fail fast on a bad registry
    app.run(host="127.0.0.1", port=int(os.environ.get("ARGIA_PL_PORT", "8520")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
