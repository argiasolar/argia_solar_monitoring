#!/usr/bin/env python3
"""ARGIA session login — the service nginx asks on every request.

Replaces HTTP Basic auth.  Basic had no logout: the browser cached the
password and re-sent it on the next click, so "Log out" could only ever
show a page and hope the user closed the window.  Here the session is a
row in a database; logout deletes it, and the next request is anonymous
whatever the browser kept.

Routes (proxied by nginx, listening on 127.0.0.1 only):

  GET  /check    auth_request target. 200 = allowed, 401 = sign in,
                 403 = signed in but not for this page.  Passes the
                 username back to nginx in X-Argia-User.
  GET  /login    the form (bilingual, honours ?next=)
  POST /login    verify, create the session, set the cookie, redirect
  GET|POST /logout   delete the session, clear the cookie
  GET  /session/whoami  small JSON for the identity chip

Credential checking, username/password normalisation and the wrong-
password throttle are imported from setup_app rather than reimplemented:
those exact rules were arrived at by debugging real login failures
(capitalised usernames, trailing spaces) and must not drift apart.
"""
import html
import os
import time

from flask import Flask, request, make_response, redirect

import auth_core as ac
import setup_app as sa

AUTH_DIR = os.environ.get('ARGIA_AUTH_DIR', '/opt/argia/auth')
SESSION_DB = os.path.join(AUTH_DIR, 'sessions.db')
SECRET_PATH = os.path.join(AUTH_DIR, 'session.key')

app = Flask(__name__)
_SECRET = None


def secret():
    global _SECRET
    if _SECRET is None:
        _SECRET = ac.load_secret(SECRET_PATH)
    return _SECRET


def user_row(username):
    """The user as setup_app stores them, or None."""
    if not username:
        return None
    c = sa.db()
    r = c.execute('SELECT username,level,reports,is_admin,plant_admin,'
                  'disabled,first_name,last_name FROM users WHERE username=?',
                  (username,)).fetchone()
    c.close()
    if not r:
        return None
    return {'username': r[0], 'level': r[1], 'reports': r[2],
            'is_admin': r[3], 'plant_admin': r[4], 'disabled': r[5],
            'first': r[6], 'last': r[7]}


def current_user():
    """(username, row) for the request's cookie, or (None, None).

    Touches `last` so an active session does not idle out under
    someone.  A session whose user was deleted or disabled since login
    is dropped here, so revoking access takes effect on the next click
    rather than at the next expiry.
    """
    sid = ac.verify(request.cookies.get(ac.COOKIE), secret())
    if not sid:
        return None, None
    c = ac.open_sessions(SESSION_DB)
    row = c.execute('SELECT username,created,last FROM sessions WHERE sid=?',
                    (sid,)).fetchone()
    if not row:
        c.close()
        return None, None
    username, created, last = row
    now = int(time.time())
    if not ac.session_alive(created, last, now):
        c.execute('DELETE FROM sessions WHERE sid=?', (sid,))
        c.commit()
        c.close()
        return None, None
    if now - last > 60:                       # one write a minute, not a request
        c.execute('UPDATE sessions SET last=? WHERE sid=?', (now, sid))
        c.commit()
    c.close()
    u = user_row(username)
    if not u or u['disabled']:
        return None, None
    return username, u


# ------------------------------------------------------------------ check

@app.route('/check')
def check():
    uri = request.headers.get('X-Original-URI', '/')
    area = ac.area_for_path(uri)
    if area is ac.PUBLIC:
        return ('', 200)
    username, u = current_user()
    if not u:
        return ('', 401)
    if not ac.may(u, area):
        return ('', 403)
    r = make_response('', 200)
    r.headers['X-Argia-User'] = username
    return r


@app.route('/session/whoami')
def whoami():
    username, u = current_user()
    if not u:
        return ({'user': ''}, 200, {'Cache-Control': 'no-store'})
    name = sa.display_name(u['first'], u['last'], username)
    return ({'user': username, 'name': name,
             'admin': bool(u['is_admin'] or u['plant_admin'])},
            200, {'Cache-Control': 'no-store'})


# ------------------------------------------------------------------ login

def safe_next(raw):
    """Only same-site absolute paths — never an attacker's host.

    //evil.example and /\\evil.example are both browser-relative
    protocol shorthands, so a leading slash alone is not enough.
    """
    n = (raw or '/').strip()
    if not n.startswith('/') or n.startswith('//') or n.startswith('/\\'):
        return '/'
    if ac.area_for_path(n) is ac.PUBLIC:
        return '/'                      # do not bounce back to /login
    return n


def login_page(nxt='/', error='', user=''):
    err = (f'<p class="err">{html.escape(error)}</p>') if error else ''
    logo = getattr(sa, 'LOGO_URI', '') or ''
    logo_img = (f'<img src="{logo}" alt="ARGIA SOLAR" class="logo">'
                if logo else '<div class="logo">ARGIA SOLAR</div>')
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Sign in — ARGIA</title>
<style>
body{{margin:0;font-family:"Segoe UI",system-ui,sans-serif;background:#f6f7f8;
 color:#202124;font-size:15px;display:flex;min-height:100vh;align-items:center;
 justify-content:center;}}
.box{{background:#fff;border:1px solid #e0e3e7;border-radius:14px;padding:32px 30px;
 width:min(380px,92vw);box-shadow:0 1px 3px rgba(0,0,0,.06);}}
.logo{{height:26px;display:block;margin:0 auto 22px;font-weight:700;
 letter-spacing:.18em;text-align:center;}}
h1{{font-size:17px;font-weight:600;margin:0 0 18px;text-align:center;}}
label{{display:block;font-size:13px;color:#5f6368;margin:12px 0 4px;}}
input{{width:100%;box-sizing:border-box;border:1px solid #dadce0;border-radius:8px;
 padding:9px 11px;font-size:15px;font-family:inherit;}}
input:focus{{outline:2px solid #1a73e8;outline-offset:-1px;border-color:#1a73e8;}}
button{{width:100%;margin-top:20px;background:#1c2733;color:#fff;border:0;
 border-radius:8px;padding:11px;font-size:15px;font-family:inherit;cursor:pointer;}}
button:hover{{background:#2b3a4a;}}
.err{{background:#fce8e6;border:1px solid #f3b8b2;color:#a50e0e;border-radius:8px;
 padding:9px 11px;font-size:13.5px;margin:0 0 6px;}}
.sub{{color:#5f6368;font-size:12.5px;text-align:center;margin:18px 0 0;line-height:1.6;}}
</style></head><body>
<form class="box" method="post" action="/login">
{logo_img}
<h1>Sign in / Iniciar sesión</h1>
{err}
<input type="hidden" name="next" value="{html.escape(nxt, quote=True)}">
<label for="u">User / Usuario</label>
<input id="u" name="user" autocomplete="username" autocapitalize="none"
 autocorrect="off" spellcheck="false" required value="{html.escape(user, quote=True)}">
<label for="p">Password / Contraseña</label>
<input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in / Entrar</button>
<p class="sub">ARGIA Solar — authorised users only.<br>
Solo usuarios autorizados.</p>
</form></body></html>'''


@app.route('/login', methods=['GET'])
def login_form():
    username, u = current_user()
    if u:
        return redirect(safe_next(request.args.get('next')), 303)
    r = make_response(login_page(safe_next(request.args.get('next'))))
    r.headers['Cache-Control'] = 'no-store'
    # 401 keeps caches and crawlers away from a page that is a wall,
    # and lets the browser's back button behave.  No WWW-Authenticate
    # header, so no native dialog — that is the whole point.
    r.status_code = 401
    return r


@app.route('/login', methods=['POST'])
def login_post():
    nxt = safe_next(request.form.get('next'))
    user = sa.clean_username(request.form.get('user'))
    pw = sa.clean_password(request.form.get('password'))
    left = sa.pw_lock_left(user)
    if left:
        return _login_fail(nxt, user,
                           f'Too many attempts. Try again in {left} s. / '
                           f'Demasiados intentos. Espere {left} s.')
    if not user or not pw or not sa.verify_pw(user, pw):
        sa.pw_note_fail(user)
        return _login_fail(nxt, user,
                           'Wrong user or password. / '
                           'Usuario o contraseña incorrectos.')
    u = user_row(user)
    if not u or u['disabled']:
        return _login_fail(nxt, user,
                           'This account is disabled. / '
                           'Esta cuenta está desactivada.')
    sa.pw_clear_fails(user)
    sid = ac.new_sid()
    now = int(time.time())
    c = ac.open_sessions(SESSION_DB)
    c.execute('INSERT INTO sessions(sid,username,created,last,agent)'
              ' VALUES(?,?,?,?,?)',
              (sid, user, now, now, (request.headers.get('User-Agent') or '')[:200]))
    c.execute('DELETE FROM sessions WHERE last < ?', (now - ac.ABS_MAX,))
    c.commit()
    c.close()
    r = redirect(nxt, 303)
    r.set_cookie(ac.COOKIE, ac.sign(sid, secret()), max_age=ac.ABS_MAX,
                 secure=True, httponly=True, samesite='Lax', path='/')
    return r


def _login_fail(nxt, user, msg):
    r = make_response(login_page(nxt, msg, user))
    r.status_code = 401
    r.headers['Cache-Control'] = 'no-store'
    return r


# ----------------------------------------------------------------- logout

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    """Delete the session server-side, then clear the cookie.

    Order matters: if the delete fails we would rather leave the cookie
    in place and report an error than tell someone they are signed out
    while the session is still live.
    """
    sid = ac.verify(request.cookies.get(ac.COOKIE), secret())
    if sid:
        c = ac.open_sessions(SESSION_DB)
        c.execute('DELETE FROM sessions WHERE sid=?', (sid,))
        c.commit()
        c.close()
    r = redirect('/logged-out.html', 303)
    r.set_cookie(ac.COOKIE, '', max_age=0, expires=0, secure=True,
                 httponly=True, samesite='Lax', path='/')
    r.headers['Cache-Control'] = 'no-store'
    return r


if __name__ == '__main__':
    # threaded: nginx asks this service on every request, including the
    # parallel ones a page makes for its own assets.
    app.run(host='127.0.0.1', threaded=True,
            port=int(os.environ.get('ARGIA_AUTH_PORT', 8512)))
