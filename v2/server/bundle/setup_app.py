#!/usr/bin/env python3
"""ARGIA report access manager.

Runs on 127.0.0.1:8511; nginx proxies /setup/ to it behind admin basic auth
(/opt/argia/auth/admin.htpasswd). This app never authenticates anyone itself —
nginx does. It only manages the user database and regenerates the per-area
htpasswd files nginx reads:

  /opt/argia/auth/all.htpasswd        every user           -> landing page /
  /opt/argia/auth/financial.htpasswd  financial report     -> /financial/
  /opt/argia/auth/<plant>.htpasswd    one plant page       -> /<plant>/
  /opt/argia/auth/capex.htpasswd      CAPEX overview       -> /capex/
  /opt/argia/auth/admin.htpasswd      admins               -> /setup/

Levels: 'argia' = access to everything (employees); 'custom' = listed areas only.
Admins additionally reach /setup/. Passwords are bcrypt-hashed (htpasswd -B),
never stored in plain text; a generated password is shown exactly once.
"""
import glob
import gzip
import html
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time as _time

from flask import Flask, request, make_response

sys_dir = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, sys_dir)
try:
    from argia_logo import LOGO_URI
except ImportError:
    LOGO_URI = ''

AUTH_DIR = os.environ.get('ARGIA_AUTH_DIR', '/opt/argia/auth')
DB_PATH = os.path.join(AUTH_DIR, 'users.db')
LEGACY_HTPASSWD = '/opt/argia/.htpasswd'
NGINX_GROUP = os.environ.get('ARGIA_NGINX_GROUP', 'www-data')

PLANTS = ['gto1', 'mex1', 'mex2', 'nl1', 'slp1', 'slp2',
          'gto2', 'qro1', 'nl2', 'mex3', 'tam1']
AREAS = ['financial'] + PLANTS + ['capex', 'monitoring']
AREA_LABEL = {'financial': 'Financial Report', 'capex': 'CAPEX overview',
              'monitoring': 'Live monitoring portal',
              **{p: p.upper() + ' plant page' for p in PLANTS}}

app = Flask(__name__)


def _csrf_token():
    """Persist the CSRF token across app restarts — otherwise every deploy
    silently breaks forms in already-open setup tabs (live incident 2026-08-26:
    a user creation was lost that way)."""
    os.makedirs(AUTH_DIR, exist_ok=True)
    p = os.path.join(AUTH_DIR, 'csrf_token')
    try:
        tok = open(p).read().strip()
        if re.fullmatch(r'[0-9a-f]{32}', tok):
            return tok
    except OSError:
        pass
    tok = secrets.token_hex(16)
    with open(p, 'w') as fh:
        fh.write(tok)
    os.chmod(p, 0o600)
    return tok


CSRF = _csrf_token()

# ---------------- usage statistics (from nginx access logs) ----------------
LOG_GLOBS = os.environ.get(
    'ARGIA_LOG_GLOBS',
    '/var/log/nginx/report.argia.com.mx-access.log*;'
    '/var/log/nginx/monitoring.argia.com.mx/monitoring.argia.com.mx-access.log*'
).split(';')
SESSION_GAP = 30 * 60          # a >30 min silence starts a new session ("login")
_last_refresh = [0.0]

MONTHS = {m: i + 1 for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])}
# combined format, optionally prefixed with the vhost name ("timed" format)
RE_LINE = re.compile(
    r'^(?:[\w.\-:]+ )?(\S+) - (\S+) \[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})'
    r' [^\]]*\] "\w+ ([^ "]*)[^"]*" (\d{3})')


def _parse_logs():
    """-> (hits: {user: [epoch,...]}, fails: {user: count}) from all log files."""
    import calendar
    hits, fails = {}, {}
    for pattern in LOG_GLOBS:
        for path in glob.glob(pattern.strip()):
            opener = gzip.open if path.endswith('.gz') else open
            try:
                with opener(path, 'rt', errors='replace') as fh:
                    for ln in fh:
                        m = RE_LINE.match(ln)
                        if not m:
                            continue
                        user = m.group(2)
                        if user == '-':
                            continue
                        status = m.group(10)
                        if status == '401':
                            fails[user] = fails.get(user, 0) + 1
                            continue
                        if status[0] not in '23' or m.group(9).startswith('/favicon'):
                            continue
                        ts = calendar.timegm((
                            int(m.group(5)), MONTHS.get(m.group(4), 1), int(m.group(3)),
                            int(m.group(6)), int(m.group(7)), int(m.group(8)), 0, 0, 0))
                        hits.setdefault(user, []).append(ts)
            except OSError:
                continue
    return hits, fails


def refresh_stats(force=False):
    """Sessionize log hits and persist per-(user, day) aggregates in SQLite.
    Days present in the current logs are recomputed; older days persist."""
    if not force and _time.time() - _last_refresh[0] < 300:
        return
    _last_refresh[0] = _time.time()
    hits, fails = _parse_logs()
    c = db()
    c.execute('''CREATE TABLE IF NOT EXISTS usage_daily(
        username TEXT NOT NULL, day TEXT NOT NULL,
        requests INTEGER NOT NULL DEFAULT 0, sessions INTEGER NOT NULL DEFAULT 0,
        minutes REAL NOT NULL DEFAULT 0, fails INTEGER NOT NULL DEFAULT 0,
        last_ts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (username, day))''')
    agg = {}   # (user, day) -> [requests, sessions, minutes, last_ts]
    for user, ts_list in hits.items():
        ts_list.sort()
        start = prev = ts_list[0]
        spans = []
        for ts2 in ts_list[1:]:
            if ts2 - prev > SESSION_GAP:
                spans.append((start, prev))
                start = ts2
            prev = ts2
        spans.append((start, prev))
        for s, e in spans:
            day = _time.strftime('%Y-%m-%d', _time.gmtime(s))
            a = agg.setdefault((user, day), [0, 0, 0.0, 0])
            a[1] += 1
            a[2] += max((e - s) / 60.0, 1.0)
            a[3] = max(a[3], e)
        for ts2 in ts_list:
            day = _time.strftime('%Y-%m-%d', _time.gmtime(ts2))
            a = agg.setdefault((user, day), [0, 0, 0.0, 0])
            a[0] += 1
            a[3] = max(a[3], ts2)
    today = _time.strftime('%Y-%m-%d', _time.gmtime())
    for user, n in fails.items():
        a = agg.setdefault((user, today), [0, 0, 0.0, 0])
    for (user, day), a in agg.items():
        c.execute('INSERT INTO usage_daily(username,day,requests,sessions,minutes,fails,last_ts) '
                  'VALUES(?,?,?,?,?,?,?) ON CONFLICT(username,day) DO UPDATE SET '
                  'requests=excluded.requests, sessions=excluded.sessions, '
                  'minutes=excluded.minutes, last_ts=excluded.last_ts',
                  (user, day, a[0], a[1], round(a[2], 1), 0, a[3]))
    for user, n in fails.items():
        c.execute('UPDATE usage_daily SET fails=? WHERE username=? AND day=?',
                  (n, user, today))
    c.commit()
    c.close()


def stats_table(org=None):
    refresh_stats()
    c = db()
    known = {r[0] for r in c.execute('SELECT username FROM users')}
    rows = c.execute(
        'SELECT username, count(DISTINCT day), sum(requests), sum(sessions), '
        'sum(minutes), sum(fails), max(last_ts) FROM usage_daily '
        'GROUP BY username ORDER BY max(last_ts) DESC').fetchall()
    if org:
        mine = {r[0] for r in c.execute('SELECT username FROM users WHERE org=?', (org,))}
        rows = [r for r in rows if r[0] in mine]
    c.close()
    out = ['<div class="card"><h2 data-en="Usage statistics" data-es="Estadísticas de uso">Usage statistics</h2>',
           '<p class="note" data-en="Derived from web-server logs. A login = a session of requests separated by 30+ min of silence; time = summed session length. Log rotation limits full history."',
           ' data-es="Derivado de los logs del servidor. Un acceso = sesión separada por 30+ min; tiempo = duración sumada de sesiones. La rotación de logs limita el histórico.">',
           'Derived from web-server logs. A login = a session of requests separated by 30+ min of silence; time = summed session length. Log rotation limits full history.</p>'
           '<p class="note" data-en="\'no account\' = this name was typed at the login prompt but no such user exists — a failed attempt, a typo, or a deleted account\'s history."'
           ' data-es="\'sin cuenta\' = este nombre se escribió en el login pero el usuario no existe — intento fallido, error de tipeo o historial de una cuenta eliminada.">'
           "'no account' = this name was typed at the login prompt but no such user exists — a failed attempt, a typo, or a deleted account's history.</p>",
           '<table><tr><th data-en="User" data-es="Usuario">User</th>'
           '<th data-en="Last seen (UTC)" data-es="Última vez (UTC)">Last seen (UTC)</th>'
           '<th class="num" data-en="Active days" data-es="Días activos">Active days</th>'
           '<th class="num" data-en="Logins" data-es="Accesos">Logins</th>'
           '<th class="num" data-en="Time in app" data-es="Tiempo en la app">Time in app</th>'
           '<th class="num" data-en="Page views" data-es="Páginas vistas">Page views</th>'
           '<th class="num" data-en="Failed logins" data-es="Accesos fallidos">Failed</th></tr>']
    for u, days, reqs, sess, mins, fl, last in rows:
        mins = mins or 0
        hh, mm = int(mins // 60), int(mins % 60)
        seen = _time.strftime('%Y-%m-%d %H:%M', _time.gmtime(last)) if last else '—'
        badge = '' if u in known else (' <span class="pill adm" data-en="no account" '
                                       'data-es="sin cuenta">no account</span>')
        out.append(f'<tr><td><b>{html.escape(u)}</b>{badge}</td><td>{seen}</td>'
                   f'<td class="num">{days}</td><td class="num">{sess or 0}</td>'
                   f'<td class="num">{hh}h {mm:02d}m</td><td class="num">{reqs or 0}</td>'
                   f'<td class="num">{fl or 0}</td></tr>')
    out.append('</table></div>')
    return ''.join(out)


def db():
    c = sqlite3.connect(DB_PATH)
    c.execute('''CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, hash TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'custom', reports TEXT NOT NULL DEFAULT '',
        is_admin INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL DEFAULT (datetime('now')))''')
    for ddl in ('ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0',
                "ALTER TABLE users ADD COLUMN org TEXT NOT NULL DEFAULT ''",
                'ALTER TABLE users ADD COLUMN plant_admin INTEGER NOT NULL DEFAULT 0',
                # who the account belongs to — shown in the header of
                # every page so nobody has to guess who is signed in
                "ALTER TABLE users ADD COLUMN first_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE users ADD COLUMN last_name TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''"):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    return c


def psql(sql):
    """Run SQL against the reporting DB (argia_mont). Returns stdout or raises."""
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', 'argia_mont',
                        '-v', 'ON_ERROR_STOP=1', '-q', '-t', '-A', '-F', '\t'],
                       input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-400:])
    return r.stdout


def actor():
    """(username, is_global_admin, org) of the logged-in person, from the
    basic-auth identity nginx forwards. Unknown/no-rights users get (u, False, None)."""
    u = (request.headers.get('X-Remote-User') or '').strip()
    c = db()
    row = c.execute('SELECT is_admin, plant_admin, org, disabled FROM users '
                    'WHERE username=?', (u,)).fetchone()
    c.close()
    if not row or row[3]:
        return u, False, None
    if row[0]:
        return u, True, None
    if row[1] and row[2]:
        return u, False, row[2]
    return u, False, None


def active_admins(c):
    return [r[0] for r in
            c.execute('SELECT username FROM users WHERE is_admin=1 AND disabled=0')]


# Generated passwords are read off a screen and retyped by hand or
# dictated over the phone, so the alphabet drops every glyph pair that
# is ambiguous in common fonts: l/I/1, O/0, and the URL-safe '-_' that
# vanish at the end of a copied line. 57 symbols x 14 chars = ~81 bits,
# a shade more than the token_urlsafe(10) it replaces.
PW_ALPHABET = ('abcdefghijkmnopqrstuvwxyz'
               'ABCDEFGHJKLMNPQRSTUVWXYZ'
               '23456789')
PW_LENGTH = 14


def make_password(n=PW_LENGTH):
    return ''.join(secrets.choice(PW_ALPHABET) for _ in range(n))


def display_name(first, last, username):
    """'Eduardo Ramirez' when we know it, else the username. Never
    empty — this string is what identifies the session on screen."""
    full = ' '.join(p for p in ((first or '').strip(),
                                (last or '').strip()) if p)
    return full or (username or '')


def clean_name(raw, limit=60):
    return re.sub(r'\s+', ' ', (raw or '').strip())[:limit]


def clean_email(raw):
    e = (raw or '').strip().lower()[:120]
    return e if (not e or re.fullmatch(r'[^@\s]+@[^@\s.]+\.[^@\s]+', e)) \
        else ''


def profile_of(username):
    """(display name, email, is_admin) for a username; blanks if gone."""
    c = db()
    row = c.execute('SELECT first_name,last_name,email,is_admin,level'
                    ' FROM users WHERE username=?',
                    (username,)).fetchone()
    c.close()
    if not row:
        return username, '', 0, ''
    return (display_name(row[0], row[1], username), row[2], row[3],
            row[4])


def verify_pw(user, pw):
    """True when pw matches the user's stored hash.

    bcrypt when the library is present (the hashes htpasswd -B writes
    are $2y$, which it accepts); otherwise htpasswd -vb, which also
    covers the apr1 fallback hashes. python's crypt module is gone in
    3.13, so there is no stdlib path here."""
    if not pw or not user:
        return False
    c = db()
    row = c.execute('SELECT hash FROM users WHERE username=? '
                    'AND disabled=0', (user,)).fetchone()
    c.close()
    if not row:
        return False
    stored = row[0]
    try:
        import bcrypt
        if stored.startswith('$2'):
            return bcrypt.checkpw(pw.encode(), stored.encode())
    except Exception:                                     # noqa: BLE001
        pass
    try:
        r = subprocess.run(
            ['htpasswd', '-vb', os.path.join(AUTH_DIR, 'all.htpasswd'),
             user, pw], capture_output=True, text=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


# Wrong-current-password throttle. The page already sits behind basic
# auth, so this is not the main defence — it stops someone poking at a
# colleague's unlocked browser from grinding through guesses.
PW_FAIL_MAX = 5
PW_FAIL_WINDOW = 600.0
_PW_FAILS = {}


def pw_lock_left(user, now=None, fails=None):
    """Seconds the user must wait, 0 when not throttled."""
    fails = _PW_FAILS if fails is None else fails
    now = _time.time() if now is None else now
    n, first = fails.get(user, (0, 0.0))
    if n >= PW_FAIL_MAX and now - first < PW_FAIL_WINDOW:
        return int(PW_FAIL_WINDOW - (now - first)) + 1
    return 0


def pw_note_fail(user, now=None, fails=None):
    fails = _PW_FAILS if fails is None else fails
    now = _time.time() if now is None else now
    n, first = fails.get(user, (0, 0.0))
    if now - first >= PW_FAIL_WINDOW:
        n, first = 0, now
    fails[user] = (n + 1, first)


def pw_clear_fails(user, fails=None):
    (_PW_FAILS if fails is None else fails).pop(user, None)


def clean_username(raw):
    """Usernames are stored — and written to htpasswd — in lowercase.
    nginx compares the basic-auth username byte for byte, so 'Eduardo'
    never matches the 'eduardo' line. Normalising here keeps what the
    admin sees identical to what the user must type."""
    return (raw or '').strip().lower()


def clean_password(raw):
    """Trim surrounding whitespace. A password pasted with a trailing
    space used to be hashed WITH the space, so every later login got a
    401 while everything else looked correct (diagnosed 2026-08-28)."""
    return (raw or '').strip()


PW_MIN = 10


def password_problem(new, again, current):
    """Reason the new password is unacceptable, or None. Kept separate
    from the route so the rules are testable."""
    if not new:
        return 'Enter a new password.'
    if len(new) < PW_MIN:
        return f'Use at least {PW_MIN} characters.'
    if new != again:
        return 'The two new passwords do not match.'
    if new == current:
        return 'The new password is the same as the current one.'
    return None


def hash_pw(user, pw):
    """bcrypt via htpasswd when available, else openssl apr1 (nginx-compatible)."""
    try:
        r = subprocess.run(['htpasswd', '-nbB', user, pw],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip().split(':', 1)[1]
    except (FileNotFoundError, subprocess.CalledProcessError):
        r = subprocess.run(['openssl', 'passwd', '-apr1', pw],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()


def seed():
    """First run: import the existing 'argia' user as argia-level admin."""
    c = db()
    n = c.execute('SELECT count(*) FROM users').fetchone()[0]
    if n == 0 and os.path.exists(LEGACY_HTPASSWD):
        for ln in open(LEGACY_HTPASSWD):
            if ':' in ln:
                u, h = ln.strip().split(':', 1)
                c.execute('INSERT OR IGNORE INTO users(username,hash,level,is_admin) '
                          "VALUES(?,?,'argia',1)", (u, h))
        c.commit()
    c.close()


def sync():
    """Rewrite every htpasswd file from the DB. Mode 640 root:www-data."""
    c = db()
    rows = c.execute('SELECT username,hash,level,reports,is_admin FROM users '
                     'WHERE disabled=0').fetchall()
    c.close()
    os.makedirs(AUTH_DIR, exist_ok=True)
    files = {'all': [], 'admin': []}
    for a in AREAS:
        files[a] = []
    c2 = db()
    padmins = {r[0] for r in c2.execute(
        'SELECT username FROM users WHERE plant_admin=1 AND disabled=0')}
    c2.close()
    for u, h, level, reports, adm in rows:
        line = f'{u}:{h}'
        files['all'].append(line)
        if adm or u in padmins:
            files['admin'].append(line)
        granted = AREAS if level == 'argia' else [x for x in reports.split(',') if x]
        for a in granted:
            if a in files:
                files[a].append(line)
    for name, lines in files.items():
        p = os.path.join(AUTH_DIR, f'{name}.htpasswd')
        with open(p, 'w') as fh:
            fh.write('\n'.join(lines) + ('\n' if lines else ''))
        os.chmod(p, 0o640)
        try:
            import grp
            os.chown(p, 0, grp.getgrnam(NGINX_GROUP).gr_gid)
        except (KeyError, PermissionError, OSError):
            pass


def page(body, msg='', once=None):
    once_html = ''
    if once:
        once_html = (f'<div class="card" style="border-color:#137333">'
                     f'<b data-en="One-time password — copy it now, it is not stored:" '
                     f'data-es="Contraseña de un solo uso — cópiala ahora, no se guarda:">'
                     f'One-time password — copy it now, it is not stored:</b> '
                     f'<code style="font-size:16px">{html.escape(once)}</code></div>')
    msg_html = f'<p class="note">{html.escape(msg)}</p>' if msg else ''
    # Who is signed in, spelled out. Basic auth keeps sending cached
    # credentials, so without this the page you are looking at may
    # belong to a different account than the one you last typed.
    _me = clean_username(request.headers.get('X-Remote-User'))
    _name, _mail, _adm, _lvl = profile_of(_me) if _me else ('', '', 0, '')
    who_html = (
        '<div class="usermenu">'
        '<button class="whoami" onclick="argiaMenu(event)"'
        ' aria-haspopup="true" aria-expanded="false">'
        + html.escape(_name)
        + (f' <span class="wu">({html.escape(_me)})</span>'
           if _name != _me else '')
        + (' <span class="pill adm">admin</span>' if _adm else '')
        + ' <span class="car">▾</span></button>'
        '<div class="umenu" id="umenu">'
        '<a href="/account/" data-en="My account" data-es="Mi cuenta">'
        'My account</a>'
        '<div class="umsep"></div>'
        '<div class="umlabel" data-en="Language" data-es="Idioma">'
        'Language</div>'
        '<div class="umrow">'
        '<button class="lang-btn" data-l="en" onclick="setLang(\'en\')">'
        'EN</button>'
        '<button class="lang-btn" data-l="es" onclick="setLang(\'es\')">'
        'ES</button></div>'
        '<div class="umsep"></div>'
        '<button onclick="argiaLogout()" data-en="Log out"'
        ' data-es="Cerrar sesión">Log out</button>'
        '</div></div>') if _me else ''
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Setup — ARGIA</title>
<style>
body{{margin:0;font-family:"Segoe UI",system-ui,sans-serif;background:#f6f7f8;color:#202124;font-size:15px;}}
.wrap{{max-width:1040px;margin:0 auto;padding:26px 18px 48px;}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;}}
h1{{font-size:23px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#1c2733;margin:4px 0 0;}}
.logo{{font-size:21px;font-weight:600;letter-spacing:.3em;color:#1c2733;}}
.sub{{color:#5f6368;font-size:13.5px;margin-top:4px;}}
.card{{background:#fff;border:1px solid #e4e7ea;border-radius:10px;padding:16px 18px;margin:14px 0;overflow-x:auto;}}
.card h2{{font-size:14px;margin:0 0 8px;}}
table{{border-collapse:collapse;width:100%;font-size:13.5px;}}
th,td{{text-align:left;padding:7px 9px;border-bottom:1px solid #eceef0;}}
th{{color:#5f6368;font-size:12px;}}
.btn{{background:#fff;border:1px solid #dadce0;border-radius:8px;padding:6px 13px;font-size:13.5px;cursor:pointer;}}
.btn.danger{{color:#b3261e;}}
input[type=text],input[type=password]{{border:1px solid #dadce0;border-radius:8px;padding:6px 10px;font-size:13.5px;}}
.pill{{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;background:#e6f4ea;color:#137333;}}
.pill.adm{{background:#ecebf6;color:#4f4a94;}}
.areas{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:4px;font-size:13px;}}
.controls{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0;}}
.controls .usermenu{{margin-left:auto;}}
.note{{font-size:13px;color:#80868b;}}
label{{font-size:13.5px;}}
.usermenu{{display:inline-block;position:relative;}}
.whoami{{background:#eef1f4;border:1px solid #dadce0;border-radius:20px;
 padding:5px 12px;font-size:13px;color:#1c2733;font-weight:600;
 cursor:pointer;font-family:inherit;}}
.whoami .wu{{font-weight:400;color:#5f6368;}}
.whoami .car{{margin-left:6px;color:#5f6368;font-size:10px;}}
.umenu{{display:none;position:absolute;right:0;top:calc(100% + 6px);z-index:50;
 min-width:210px;background:#fff;border:1px solid #e4e7ea;border-radius:10px;
 box-shadow:0 6px 24px rgba(0,0,0,.12);padding:6px;text-align:left;}}
.umenu.open{{display:block;}}
.umenu a,.umenu button{{display:block;width:100%;box-sizing:border-box;
 text-align:left;background:none;border:0;border-radius:7px;padding:8px 10px;
 font-size:13.5px;color:#202124;text-decoration:none;cursor:pointer;
 font-family:inherit;}}
.umenu a:hover,.umenu button:hover{{background:#f1f3f5;}}
.umenu .umsep{{border-top:1px solid #e4e7ea;margin:5px 2px;}}
.umenu .umrow{{display:flex;gap:6px;padding:4px 6px 2px;}}
.umenu .umrow button{{border:1px solid #dadce0;text-align:center;padding:5px 0;}}
.umenu .umrow button.active{{border-color:#2a78d6;box-shadow:inset 0 0 0 1px #2a78d6;}}
.umenu .umlabel{{font-size:11px;color:#80868b;padding:6px 10px 0;
 text-transform:uppercase;letter-spacing:.08em;}}
</style></head><body><div class="wrap">
<div class="top"><div><h1 data-en="Report access setup" data-es="Gestión de accesos">Report access setup</h1>
<div class="sub" data-en="Users, passwords and per-report access. Changes apply immediately."
 data-es="Usuarios, contraseñas y acceso por reporte. Los cambios aplican de inmediato.">
Users, passwords and per-report access. Changes apply immediately.</div></div>
<img src="{LOGO_URI}" alt="ARGIA SOLAR" style="height:26px;width:auto;margin-top:2px"></div>
<div class="controls"><a class="btn" href="/" data-en="← Reports"
 data-es="← Reportes">← Reports</a>{who_html}</div>
{once_html}{msg_html}{body}
<script>
// /logout deletes the session row, then redirects. Plain navigation:
// a pre-flight fetch that returns 401 makes the browser pop its own
// sign-in dialog (reported 2026-08-28).
function argiaLogout(){{location.href='/logout';}}
function argiaMenu(ev){{ev.stopPropagation();
 const m=document.getElementById('umenu');m.classList.toggle('open');}}
document.addEventListener('click',()=>{{
 const m=document.getElementById('umenu');if(m)m.classList.remove('open');}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape'){{
 const m=document.getElementById('umenu');if(m)m.classList.remove('open');}}}});
function setLang(l){{document.querySelectorAll('[data-en]').forEach(e=>{{e.textContent=e.dataset[l]||e.dataset.en;}});
document.querySelectorAll('.lang-btn').forEach(b=>b.classList.toggle('active',b.dataset.l===l));
try{{localStorage.setItem('argia_lang',l);}}catch(e){{}}}}
window.addEventListener('DOMContentLoaded',()=>{{let l='en';
try{{l=localStorage.getItem('argia_lang')||'en';}}catch(e){{}};setLang(l);}});
</script></body></html>'''


def users_table(org=None):
    c = db()
    cols = ('username,level,reports,is_admin,created,disabled,org,'
            'plant_admin,first_name,last_name,email')
    if org:
        rows = c.execute(f'SELECT {cols} '
                         'FROM users WHERE org=? AND is_admin=0 ORDER BY username',
                         (org,)).fetchall()
    else:
        rows = c.execute(f'SELECT {cols} '
                         'FROM users ORDER BY username').fetchall()
    c.close()
    out = ['<div class="card"><h2 data-en="Users" data-es="Usuarios">Users</h2><table>',
           '<tr><th data-en="User" data-es="Usuario">User</th>'
           '<th data-en="Name" data-es="Nombre">Name</th>'
           '<th data-en="Access" data-es="Acceso">Access</th>'
           '<th data-en="Created (UTC)" data-es="Creado (UTC)">Created (UTC)</th><th></th></tr>']
    for (u, level, reports, adm, created, dis, uorg, upadm,
         fname, lname, email) in rows:
        if level == 'argia':
            acc = '<span class="pill">argia — all reports</span>'
        else:
            names = [AREA_LABEL.get(a, a) for a in reports.split(',') if a]
            acc = html.escape(', '.join(names) or '—')
        if adm:
            acc += ' <span class="pill adm">admin</span>'
        if upadm and uorg:
            acc += (f' <span class="pill adm">{html.escape(uorg.upper())} '
                    f'<span data-en="company admin" data-es="admin de empresa">company admin</span></span>')
        if dis:
            acc += (' <span class="pill" style="background:#fdeeee;color:#b3261e" '
                    'data-en="suspended" data-es="suspendido">suspended</span>')
        ue = html.escape(u)
        sus_label = ('Enable' if dis else 'Force logout')
        sus_es = ('Reactivar' if dis else 'Forzar salida')
        who = html.escape(display_name(fname, lname, ''))
        mail = html.escape(email or '')
        who_cell = ((f'{who}<br>' if who else '')
                    + (f'<span class="note">{mail}</span>' if mail
                       else ('' if who else '<span class="note">—</span>')))
        out.append(f'''<tr><td><b>{ue}</b></td><td>{who_cell}</td><td>{acc}</td>
<td>{html.escape(created[:16])}</td><td style="white-space:nowrap">
<a class="btn" href="?edit={ue}" data-en="Edit access" data-es="Editar acceso">Edit access</a>
<form method="post" action="suspend" style="display:inline">
 <input type="hidden" name="csrf" value="{CSRF}"><input type="hidden" name="username" value="{ue}">
 <button class="btn{'' if dis else ' danger'}" data-en="{sus_label}" data-es="{sus_es}">{sus_label}</button></form>
<form method="post" action="password" style="display:inline">
 <input type="hidden" name="csrf" value="{CSRF}"><input type="hidden" name="username" value="{ue}">
 <button class="btn" data-en="New password" data-es="Nueva contraseña">New password</button></form>
<form method="post" action="delete" style="display:inline"
 onsubmit="return confirm('Delete {ue}?')">
 <input type="hidden" name="csrf" value="{CSRF}"><input type="hidden" name="username" value="{ue}">
 <button class="btn danger" data-en="Delete" data-es="Eliminar">Delete</button></form>
</td></tr>''')
    out.append('</table>'
               '<p class="note" data-en="Force logout suspends the account: every open session gets 401 on its next request and the login stops working until you press Enable. With password-based access this is the only real forced logout."'
               ' data-es="Forzar salida suspende la cuenta: toda sesión abierta recibe 401 en su siguiente petición y el acceso queda bloqueado hasta Reactivar. Con acceso por contraseña esta es la única salida forzada real.">'
               'Force logout suspends the account: every open session gets 401 on its next request and the login stops working until you press Enable. With password-based access this is the only real forced logout.</p></div>')
    return ''.join(out)


def add_form(org=None):
    common = f'''<input type="hidden" name="csrf" value="{CSRF}">
<p><input type="text" name="username" placeholder="username or email" required
    pattern="[a-z0-9.@_-]{{2,64}}" title="lowercase letters, digits, . _ - @">
 <input type="password" name="password" placeholder="password (blank = generate)"></p>
<p><input type="text" name="first_name" placeholder="first name" maxlength="60">
 <input type="text" name="last_name" placeholder="surname" maxlength="60">
 <input type="text" name="email" placeholder="email" maxlength="120"></p>
<p class="note" data-en="Usernames are stored lowercase and the login is case-sensitive — the user must type it exactly as listed. Spaces around a pasted password are trimmed."
 data-es="Los usuarios se guardan en minúsculas y el acceso distingue mayúsculas — debe escribirse exactamente como aparece en la lista. Los espacios alrededor de una contraseña pegada se eliminan.">
Usernames are stored lowercase and the login is case-sensitive — the user must type it exactly as listed. Spaces around a pasted password are trimmed.</p>'''
    if org:
        return f'''<div class="card"><h2 data-en="Add user — your company" data-es="Agregar usuario — su empresa">Add user — your company</h2>
<form method="post" action="add">{common}
<p class="note" data-en="The user will see only the {org.upper()} plant page."
 data-es="El usuario verá solo la página de la planta {org.upper()}.">The user will see only the {org.upper()} plant page.</p>
<p><label><input type="checkbox" name="plant_admin" value="1">
 <span data-en="company admin — can manage your company's users and plant settings"
  data-es="admin de empresa — gestiona usuarios y ajustes de su planta">company admin — can manage your company's users and plant settings</span></label></p>
<p><button class="btn" data-en="Create user" data-es="Crear usuario">Create user</button></p>
</form></div>'''
    boxes = ''.join(
        f'<label><input type="checkbox" name="reports" value="{a}"> {AREA_LABEL[a]}</label>'
        for a in AREAS)
    orgopts = '<option value="">—</option>' + ''.join(
        f'<option value="{p}">{p.upper()}</option>' for p in PLANTS)
    return f'''<div class="card"><h2 data-en="Add user" data-es="Agregar usuario">Add user</h2>
<form method="post" action="add">{common}
<p><label><input type="radio" name="level" value="argia">
 <b>argia</b> <span data-en="— employee, access to everything"
  data-es="— empleado, acceso a todo">— employee, access to everything</span></label><br>
<label><input type="radio" name="level" value="custom" checked>
 <b>custom</b> <span data-en="— only the reports checked below"
  data-es="— solo los reportes marcados">— only the reports checked below</span></label></p>
<div class="areas">{boxes}</div>
<p><label data-en="Company (for client users):" data-es="Empresa (para usuarios cliente):">Company (for client users):</label>
 <select name="org" class="btn">{orgopts}</select>
 <label><input type="checkbox" name="plant_admin" value="1">
 <span data-en="company admin — manages that company's users + plant settings"
  data-es="admin de empresa — gestiona usuarios y ajustes de esa planta">company admin — manages that company's users + plant settings</span></label></p>
<p><label><input type="checkbox" name="is_admin" value="1">
 <span data-en="ARGIA admin — full control of everything here"
  data-es="admin ARGIA — control total aquí">ARGIA admin — full control of everything here</span></label></p>
<p><button class="btn" data-en="Create user" data-es="Crear usuario">Create user</button></p>
</form></div>'''


def settings_card(org, is_global):
    """Monthly tariff + expected-production editor (writes to the reporting DB),
    plus investment for payback tracking. org = plant key, lowercase."""
    import datetime as dt
    plant = (request.args.get('plant') or org or 'gto2').lower()
    if plant not in PLANTS or (not is_global and plant != org):
        plant = org
    year = request.args.get('year') or str(dt.date.today().year)
    if not re.fullmatch(r'20\d\d', year):
        year = str(dt.date.today().year)
    try:
        rows = psql(f"SELECT month, coalesce(contract_kwh,0), coalesce(tariff_mxn,0) "
                    f"FROM contract_monthly WHERE plant_key='{plant.upper()}' "
                    f"AND year={int(year)};")
        cur = {int(float(r.split('\t')[0])): (float(r.split('\t')[1]), float(r.split('\t')[2]))
               for r in rows.strip().splitlines() if r.strip()}
        inv = psql(f"SELECT coalesce(investment_mxn,0) FROM plant "
                   f"WHERE plant_key='{plant.upper()}';").strip()
    except RuntimeError as e:
        return f'<div class="card"><p class="note">Settings unavailable: {html.escape(str(e)[:120])}</p></div>'
    sel_p = ''.join(f'<option value="{p}"{" selected" if p == plant else ""}>{p.upper()}</option>'
                    for p in (PLANTS if is_global else [plant]))
    sel_y = ''.join(f'<option value="{y}"{" selected" if str(y) == year else ""}>{y}</option>'
                    for y in range(2024, dt.date.today().year + 3))
    mrows = []
    for m in range(1, 13):
        e, t2 = cur.get(m, (0.0, 0.0))
        mrows.append(
            f'<tr><td>{year}-{m:02d}</td>'
            f'<td class="num"><input type="number" step="any" min="0" name="exp_{m}" '
            f'value="{e if e else ""}" placeholder="kWh" style="width:110px"></td>'
            f'<td class="num"><input type="number" step="any" min="0" name="tar_{m}" '
            f'value="{t2 if t2 else ""}" placeholder="MXN/kWh" style="width:110px"></td></tr>')
    return f'''<div class="card"><h2><span data-en="Plant settings" data-es="Ajustes de planta">Plant settings</span> — {plant.upper()}</h2>
<form method="get" action="." style="display:inline">
 <select name="plant" class="btn" onchange="this.form.submit()">{sel_p}</select>
 <select name="year" class="btn" onchange="this.form.submit()">{sel_y}</select>
</form>
<form method="post" action="settings">
<input type="hidden" name="csrf" value="{CSRF}">
<input type="hidden" name="plant" value="{plant}">
<input type="hidden" name="year" value="{year}">
<table style="max-width:480px"><tr><th data-en="Month" data-es="Mes">Month</th>
<th class="num" data-en="Expected production, kWh" data-es="Producción esperada, kWh">Expected production, kWh</th>
<th class="num" data-en="Tariff, MXN/kWh" data-es="Tarifa, MXN/kWh">Tariff, MXN/kWh</th></tr>
{''.join(mrows)}</table>
<p><label data-en="Total investment, MXN (for payback tracking):"
 data-es="Inversión total, MXN (para seguimiento de recuperación):">Total investment, MXN (for payback tracking):</label>
 <input type="number" step="any" min="0" name="investment" value="{inv if inv not in ('', '0', '0.00') else ''}"
  placeholder="MXN" style="width:150px"></p>
<p class="note" data-en="Tariff = what your company pays the grid per kWh — production × tariff is shown as savings. Expected production drives the performance indicators and the grey forecast months. Blank fields are left unchanged. Reports regenerate within a minute of saving."
 data-es="Tarifa = lo que su empresa paga a la red por kWh — producción × tarifa se muestra como ahorro. La producción esperada alimenta los indicadores y los meses grises de pronóstico. Campos vacíos no se modifican. Los reportes se regeneran en un minuto.">
Tariff = what your company pays the grid per kWh — production × tariff is shown as savings. Expected production drives the performance indicators and the grey forecast months. Blank fields are left unchanged. Reports regenerate within a minute of saving.</p>
<p><button class="btn" data-en="Save settings" data-es="Guardar ajustes">Save settings</button></p>
</form></div>'''


def edit_form(u):
    me, is_global, org = actor()
    c = db()
    row = c.execute('SELECT level,reports,is_admin,plant_admin,'
                    'first_name,last_name,email FROM users '
                    'WHERE username=?', (u,)).fetchone()
    c.close()
    if not row:
        return ''
    level, reports, adm, upadm, fname, lname, email = row
    who = f'''<p><input type="text" name="first_name" placeholder="first name"
    maxlength="60" value="{html.escape(fname or '')}">
 <input type="text" name="last_name" placeholder="surname"
    maxlength="60" value="{html.escape(lname or '')}">
 <input type="text" name="email" placeholder="email"
    maxlength="120" value="{html.escape(email or '')}"></p>'''
    if not is_global:
        return f'''<div class="card" style="border-color:var(--s1,#2a78d6)">
<h2><span data-en="Edit" data-es="Editar">Edit</span> — {html.escape(u)}</h2>
<form method="post" action="update">
<input type="hidden" name="csrf" value="{CSRF}">
<input type="hidden" name="username" value="{html.escape(u)}">{who}
<p><label><input type="checkbox" name="plant_admin" value="1"{' checked' if upadm else ''}>
 <span data-en="company admin — can manage your company's users and plant settings"
  data-es="admin de empresa — gestiona usuarios y ajustes de su planta">company admin — can manage your company's users and plant settings</span></label></p>
<p><button class="btn" data-en="Save changes" data-es="Guardar cambios">Save changes</button>
 <a class="btn" href="." data-en="Cancel" data-es="Cancelar">Cancel</a></p>
</form></div>'''
    have = set(reports.split(','))
    boxes = ''.join(
        f'<label><input type="checkbox" name="reports" value="{a}"'
        f'{" checked" if a in have else ""}> {AREA_LABEL[a]}</label>'
        for a in AREAS)
    return f'''<div class="card" style="border-color:var(--s1,#2a78d6)">
<h2><span data-en="Edit access" data-es="Editar acceso">Edit access</span> — {html.escape(u)}</h2>
<form method="post" action="update">
<input type="hidden" name="csrf" value="{CSRF}">
<input type="hidden" name="username" value="{html.escape(u)}">{who}
<p><label><input type="radio" name="level" value="argia"{' checked' if level == 'argia' else ''}>
 <b>argia</b> <span data-en="— employee, access to everything"
  data-es="— empleado, acceso a todo">— employee, access to everything</span></label><br>
<label><input type="radio" name="level" value="custom"{' checked' if level != 'argia' else ''}>
 <b>custom</b> <span data-en="— only the reports checked below"
  data-es="— solo los reportes marcados">— only the reports checked below</span></label></p>
<div class="areas">{boxes}</div>
<p><label><input type="checkbox" name="plant_admin" value="1"{' checked' if upadm else ''}>
 <span data-en="company admin (of the company set at creation)"
  data-es="admin de empresa (de la empresa asignada al crear)">company admin (of the company set at creation)</span></label><br>
<label><input type="checkbox" name="is_admin" value="1"{' checked' if adm else ''}>
 <span data-en="ARGIA admin — can manage everything here"
  data-es="admin ARGIA — puede gestionar todo aquí">ARGIA admin — can manage everything here</span></label></p>
<p><button class="btn" data-en="Save changes" data-es="Guardar cambios">Save changes</button>
 <a class="btn" href="." data-en="Cancel" data-es="Cancelar">Cancel</a></p>
</form></div>'''


# ---------------------------------------------------------------------------
# Maintenance events — the invoicing maintenance flag (v3 item 11).
# Stored in PostgreSQL (argia_mont.maintenance_event). Fail-closed like the
# sheet tab it replaces: only an APPROVED 'customer' event produces deemed
# (billable) energy at billing time; drafts and argia/force-majeure events
# never invent income. ARGIA admins only.
# ---------------------------------------------------------------------------
MAINT_CATEGORIES = ('customer', 'argia', 'force_majeure')
MAINT_COST_TYPES = ('cleaning', 'repair', 'parts', 'inspection', 'other')
_TS_RE = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}$')


def _sqlq(s):
    return "'" + str(s).replace("'", "''") + "'"


def _maint_ensure():
    psql('''CREATE TABLE IF NOT EXISTS maintenance_event (
        id          serial PRIMARY KEY,
        plant_key   text NOT NULL,
        start_ts    timestamptz NOT NULL,
        end_ts      timestamptz,
        category    text NOT NULL DEFAULT 'customer',
        cost_type   text,
        cost_mxn    numeric(12,2),
        note        text,
        approved_by text,
        created_by  text,
        created_at  timestamptz NOT NULL DEFAULT now());''')


def maintenance_card():
    try:
        _maint_ensure()
        raw = psql(
            "SELECT id, plant_key,"
            " to_char(start_ts AT TIME ZONE 'America/Mexico_City','YYYY-MM-DD HH24:MI'),"
            " coalesce(to_char(end_ts AT TIME ZONE 'America/Mexico_City','YYYY-MM-DD HH24:MI'),''),"
            " category, coalesce(cost_type,''), coalesce(cost_mxn::text,''),"
            " coalesce(note,''), coalesce(approved_by,'')"
            " FROM maintenance_event ORDER BY start_ts DESC LIMIT 40;")
    except RuntimeError as e:
        return ('<div class="card"><p class="note">maintenance events '
                f'unavailable: {html.escape(str(e)[:200])}</p></div>')
    trs = []
    for ln in raw.splitlines():
        r = ln.split('\t')
        if len(r) < 9:
            continue
        mid, pk, st, en, cat, ct, cost, note, appr = r[:9]
        acts = []
        if not en:
            acts.append(
                f'<form method="post" action="maint/close" style="display:inline">'
                f'<input type="hidden" name="csrf" value="{CSRF}">'
                f'<input type="hidden" name="id" value="{mid}">'
                '<button class="btn" data-en="end now" data-es="terminar">end now</button></form>')
        if not appr:
            acts.append(
                f'<form method="post" action="maint/approve" style="display:inline">'
                f'<input type="hidden" name="csrf" value="{CSRF}">'
                f'<input type="hidden" name="id" value="{mid}">'
                '<button class="btn" data-en="approve" data-es="aprobar">approve</button></form>')
            acts.append(
                f'<form method="post" action="maint/delete" style="display:inline">'
                f'<input type="hidden" name="csrf" value="{CSRF}">'
                f'<input type="hidden" name="id" value="{mid}">'
                '<button class="btn" data-en="delete" data-es="borrar">delete</button></form>')
        badge = (f'<span class="pill">approved: {html.escape(appr)}</span>' if appr
                 else '<span class="pill" style="background:#fdf0dc;color:#a05c00">DRAFT — not billable</span>')
        trs.append(
            f'<tr><td>{html.escape(pk)}</td><td>{html.escape(st)}</td>'
            f'<td>{html.escape(en) or "<i>ongoing</i>"}</td>'
            f'<td>{html.escape(cat)}</td>'
            f'<td>{html.escape(ct)}{(" · " + html.escape(cost) + " MXN") if cost else ""}</td>'
            f'<td>{html.escape(note[:40])}</td><td>{badge}</td>'
            f'<td>{"".join(acts)}</td></tr>')
    plant_opts = ''.join(f'<option value="{p}">{p.upper()}</option>' for p in PLANTS)
    cat_opts = ''.join(f'<option value="{c}">{c}</option>' for c in MAINT_CATEGORIES)
    ct_opts = '<option value=""></option>' + ''.join(
        f'<option value="{c}">{c}</option>' for c in MAINT_COST_TYPES)
    rows_html = ('<table><tr><th>Plant</th><th data-en="Start MX" data-es="Inicio MX">Start MX</th>'
                 '<th data-en="End MX" data-es="Fin MX">End MX</th><th data-en="Category" data-es="Categoría">Category</th>'
                 '<th data-en="Cost" data-es="Costo">Cost</th><th data-en="Note" data-es="Nota">Note</th>'
                 '<th>Status</th><th></th></tr>' + ''.join(trs) + '</table>'
                 if trs else '<p class="note" data-en="No maintenance events yet." data-es="Sin eventos de mantenimiento.">No maintenance events yet.</p>')
    return f'''<div class="card"><h2 data-en="Maintenance events — the invoicing flag"
 data-es="Eventos de mantenimiento — la bandera de facturación">Maintenance events — the invoicing flag</h2>
<p class="note" data-en="Category 'customer' = customer-caused shutdown: once APPROVED, those hours are billed as deemed energy (contract-anchored), like ARGIA_Solar Invoicing. 'argia' and 'force_majeure' are recorded but never billed. Leave End empty for an ongoing event and close it later."
 data-es="Categoría 'customer' = paro causado por el cliente: una vez APROBADO, esas horas se facturan como energía compensada (anclada al contrato). 'argia' y 'force_majeure' se registran pero nunca se facturan. Deje Fin vacío para un evento en curso.">
Category 'customer' = customer-caused shutdown: once APPROVED, those hours are billed as deemed energy.</p>
{rows_html}
<form method="post" action="maint/add" style="margin-top:12px">
<input type="hidden" name="csrf" value="{CSRF}">
<p><select name="plant">{plant_opts}</select>
 <label class="note" data-en="start" data-es="inicio">start</label> <input type="datetime-local" name="start" required>
 <label class="note" data-en="end (optional)" data-es="fin (opcional)">end (optional)</label> <input type="datetime-local" name="end">
 <select name="category">{cat_opts}</select>
 <select name="cost_type">{ct_opts}</select>
 <input name="cost" placeholder="cost MXN" size="9">
 <input name="note" placeholder="note" size="24">
 <button class="btn" data-en="Log event (draft)" data-es="Registrar evento (borrador)">Log event (draft)</button></p>
</form></div>'''


def _maint_guard():
    me, is_global, org = actor()
    return me if (is_global and check_csrf()) else None


@app.post('/maint/add')
def maint_add():
    me = _maint_guard()
    if not me:
        return stale_page()
    plant = (request.form.get('plant') or '').strip().lower()
    start = (request.form.get('start') or '').strip()
    end = (request.form.get('end') or '').strip()
    cat = (request.form.get('category') or 'customer').strip()
    ct = (request.form.get('cost_type') or '').strip()
    cost = (request.form.get('cost') or '').strip()
    note = (request.form.get('note') or '').strip()[:200]
    if plant not in PLANTS or cat not in MAINT_CATEGORIES \
            or not _TS_RE.match(start) or (end and not _TS_RE.match(end)):
        return render(msg='maintenance: invalid plant/category/time — nothing saved')
    try:
        cost_sql = f'{float(cost):.2f}' if cost else 'NULL'
    except ValueError:
        return render(msg='maintenance: cost must be a number — nothing saved')
    mx = "AT TIME ZONE 'America/Mexico_City'"
    end_sql = f"timestamp '{end.replace('T', ' ')}' {mx}" if end else 'NULL'
    ct_sql = _sqlq(ct) if ct in MAINT_COST_TYPES else 'NULL'
    _maint_ensure()
    psql(f"INSERT INTO maintenance_event (plant_key, start_ts, end_ts,"
         f" category, cost_type, cost_mxn, note, created_by) VALUES"
         f" ({_sqlq(plant.upper())}, timestamp '{start.replace('T', ' ')}' {mx},"
         f" {end_sql}, {_sqlq(cat)}, {ct_sql}, {cost_sql}, {_sqlq(note)},"
         f" {_sqlq(me)});")
    return render(msg='maintenance event logged as DRAFT — approve it to make it billable')


@app.post('/maint/close')
def maint_close():
    me = _maint_guard()
    if not me:
        return stale_page()
    try:
        mid = int(request.form.get('id') or 0)
    except ValueError:
        mid = 0
    psql(f'UPDATE maintenance_event SET end_ts = now() WHERE id = {mid} AND end_ts IS NULL;')
    return render(msg=f'maintenance event #{mid} ended now')


@app.post('/maint/approve')
def maint_approve():
    me = _maint_guard()
    if not me:
        return stale_page()
    try:
        mid = int(request.form.get('id') or 0)
    except ValueError:
        mid = 0
    psql(f'UPDATE maintenance_event SET approved_by = {_sqlq(me)} '
         f'WHERE id = {mid} AND approved_by IS NULL;')
    return render(msg=f'maintenance event #{mid} approved — billable if category=customer')


@app.post('/maint/delete')
def maint_delete():
    me = _maint_guard()
    if not me:
        return stale_page()
    try:
        mid = int(request.form.get('id') or 0)
    except ValueError:
        mid = 0
    psql(f'DELETE FROM maintenance_event WHERE id = {mid} AND approved_by IS NULL;')
    return render(msg=f'draft maintenance event #{mid} deleted')



# ---------------------------------------------------------------------------
# Alert mailing list (v3 item 8) — who receives plant/server/infrastructure
# alert emails from service@argia.com.mx. Consumed by alert_mailer on its
# 30-minute cycle. ARGIA admins only.
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')


def _mail_ensure():
    psql('''CREATE TABLE IF NOT EXISTS mail_recipient (
        email   text PRIMARY KEY,
        enabled boolean NOT NULL DEFAULT true,
        note    text,
        added_by text,
        added_at timestamptz NOT NULL DEFAULT now());''')


def recipients_card():
    try:
        _mail_ensure()
        raw = psql("SELECT email, enabled, coalesce(note,''),"
                   " coalesce(added_by,'') FROM mail_recipient ORDER BY 1;")
    except RuntimeError as e:
        return ('<div class="card"><p class="note">mailing list '
                f'unavailable: {html.escape(str(e)[:200])}</p></div>')
    trs = []
    for ln in raw.splitlines():
        r = ln.split('\t')
        if len(r) < 4:
            continue
        email, en, note, by = r[:4]
        on = en == 't'
        badge = ('<span class="pill">active</span>' if on else
                 '<span class="pill" style="background:#eceef0;color:#5f6368">paused</span>')
        acts = (
            f'<form method="post" action="mail/toggle" style="display:inline">'
            f'<input type="hidden" name="csrf" value="{CSRF}">'
            f'<input type="hidden" name="email" value="{html.escape(email)}">'
            f'<button class="btn">{"pause" if on else "resume"}</button></form>'
            f'<form method="post" action="mail/delete" style="display:inline">'
            f'<input type="hidden" name="csrf" value="{CSRF}">'
            f'<input type="hidden" name="email" value="{html.escape(email)}">'
            '<button class="btn" data-en="remove" data-es="quitar">remove</button></form>')
        trs.append(f'<tr><td>{html.escape(email)}</td><td>{badge}</td>'
                   f'<td>{html.escape(note[:40])}</td>'
                   f'<td>{html.escape(by)}</td><td>{acts}</td></tr>')
    rows_html = ('<table><tr><th>Email</th><th>Status</th>'
                 '<th data-en="Note" data-es="Nota">Note</th>'
                 '<th data-en="Added by" data-es="Agregado por">Added by</th>'
                 '<th></th></tr>' + ''.join(trs) + '</table>'
                 if trs else
                 '<p class="note" data-en="No recipients yet — nobody receives alert emails." data-es="Sin destinatarios — nadie recibe alertas.">No recipients yet — nobody receives alert emails.</p>')
    return f'''<div class="card"><h2 data-en="Alert emails — mailing list"
 data-es="Correos de alerta — lista de distribución">Alert emails — mailing list</h2>
<p class="note" data-en="These addresses receive plant / server / infrastructure alerts from service@argia.com.mx (checked every 30 min; new alerts immediately, active ones re-sent every 6 h, recoveries once). A plant under a logged maintenance event never alarms."
 data-es="Estas direcciones reciben alertas de plantas / servidor / infraestructura desde service@argia.com.mx (cada 30 min). Una planta con mantenimiento registrado no alarma.">
These addresses receive plant / server / infrastructure alerts from service@argia.com.mx.</p>
{rows_html}
<form method="post" action="mail/add" style="margin-top:10px">
<input type="hidden" name="csrf" value="{CSRF}">
<p><input name="email" placeholder="name@company.com" size="28" required>
 <input name="note" placeholder="note" size="18">
 <button class="btn" data-en="Add recipient" data-es="Agregar destinatario">Add recipient</button></p>
</form></div>'''


@app.post('/mail/add')
def mail_add():
    me = _maint_guard()
    if not me:
        return stale_page()
    email = (request.form.get('email') or '').strip().lower()
    note = (request.form.get('note') or '').strip()[:100]
    if not _EMAIL_RE.match(email):
        return render(msg='mailing list: invalid email — nothing saved')
    _mail_ensure()
    psql(f"INSERT INTO mail_recipient (email, note, added_by) VALUES"
         f" ({_sqlq(email)}, {_sqlq(note)}, {_sqlq(me)})"
         f" ON CONFLICT (email) DO UPDATE SET enabled = true,"
         f" note = EXCLUDED.note;")
    return render(msg=f'{email} added to the alert mailing list')


@app.post('/mail/toggle')
def mail_toggle():
    me = _maint_guard()
    if not me:
        return stale_page()
    email = (request.form.get('email') or '').strip().lower()
    psql(f'UPDATE mail_recipient SET enabled = NOT enabled'
         f' WHERE email = {_sqlq(email)};')
    return render(msg=f'{email} toggled')


@app.post('/mail/delete')
def mail_delete():
    me = _maint_guard()
    if not me:
        return stale_page()
    email = (request.form.get('email') or '').strip().lower()
    psql(f'DELETE FROM mail_recipient WHERE email = {_sqlq(email)};')
    return render(msg=f'{email} removed from the alert mailing list')



# ======================= finance setup (admins) =======================
# /setup/finance — the commercial inputs behind the financial report:
# loans, future installments, FX projections, O&M, LaaS fees, tariffs.
# PG is the single authority (webreport reads it too); every write is
# audited and the report page regenerates immediately.

import finance_core as fin

WEBROOT = os.environ.get('ARGIA_WEBROOT',
                         '/www/hosting/monitoring.argia.com.mx/www')
REPORT_GEN = os.path.join(sys_dir, 'report_gen.py')


def _fin_month_now():
    """Current MX month 'YYYY-MM' — the first editable month; anything
    earlier is paid history and immutable."""
    import datetime
    from zoneinfo import ZoneInfo
    return datetime.datetime.now(
        ZoneInfo('America/Mexico_City')).strftime('%Y-%m')


def _fin_guard():
    me, is_global, org = actor()
    return me if (is_global and check_csrf()) else None


def _fin_regen():
    """Regenerate the report pages so the edit is visible immediately.
    ~1.2 s measured; a failure must not hide that the DB write already
    happened, so the caller reports it instead of raising."""
    try:
        r = subprocess.run(['/usr/bin/python3', REPORT_GEN, WEBROOT],
                           capture_output=True, text=True, timeout=180)
        return r.returncode == 0
    except Exception:
        return False


def _fin_write(me, plant, loan_id, action, detail, sqls):
    """One audited finance edit: SQL + audit row + report regen.
    Returns the user-facing message."""
    psql(fin.ENSURE_AUDIT_SQL)
    for s in sqls:
        psql(s)
    psql(fin.sql_audit(me, plant, loan_id, action, detail))
    ok = _fin_regen()
    return ('%s — saved; financial report regenerated' % detail if ok else
            '%s — saved, but report regeneration FAILED; it will refresh '
            'on the next scheduled run' % detail)


def _fin_rows(sql):
    return [ln.split('\t') for ln in psql(sql).splitlines() if ln.strip()]


def _fin_fmt(v, dec=2):
    try:
        return f'{float(v):,.{dec}f}'
    except (TypeError, ValueError):
        return '—'


def finance_page(msg=''):
    me, is_global, org = actor()
    if not is_global:
        return page('<div class="card"><p data-en="Admins only." '
                    'data-es="Solo administradores.">Admins only.</p>'
                    '</div>', msg=msg)
    psql(fin.ENSURE_AUDIT_SQL)
    m0 = _fin_month_now()
    csrf = f'<input type="hidden" name="csrf" value="{CSRF}">'
    mono = 'font-variant-numeric:tabular-nums'

    # ---- loans ----
    cards = [f'''<div class="card"><h2 data-en="How this works"
 data-es="Cómo funciona">How this works</h2>
<p class="note" data-en="Paid history is immutable — every edit applies from the chosen month FORWARD (earliest: {m0}). USD loans: the currency amount and FX are authoritative, MXN is recomputed. Every change is logged below and the financial report regenerates immediately."
 data-es="El historial pagado es inmutable — cada cambio aplica desde el mes elegido EN ADELANTE (mínimo: {m0}). Créditos USD: el monto en divisa y el tipo de cambio mandan, el MXN se recalcula. Todo cambio queda registrado abajo y el reporte financiero se regenera de inmediato.">
Paid history is immutable — edits apply from the chosen month forward.</p></div>''']

    loans = _fin_rows(
        "SELECT l.loan_id, l.plant_key, l.project_name, l.bank,"
        " l.currency, l.principal_mxn, l.total_installments,"
        " to_char(l.first_month,'YYYY-MM'), to_char(l.last_month,'YYYY-MM'),"
        " (SELECT count(*) FROM loan_schedule s WHERE s.loan_id=l.loan_id"
        f"  AND s.ref_month < DATE '{m0}-01'),"
        " (SELECT s.payment_mxn FROM loan_schedule s WHERE s.loan_id=l.loan_id"
        f"  AND s.ref_month >= DATE '{m0}-01' ORDER BY s.ref_month LIMIT 1),"
        " (SELECT s.payment_ccy FROM loan_schedule s WHERE s.loan_id=l.loan_id"
        f"  AND s.ref_month >= DATE '{m0}-01' ORDER BY s.ref_month LIMIT 1),"
        " (SELECT s.xr FROM loan_schedule s WHERE s.loan_id=l.loan_id"
        f"  AND s.ref_month >= DATE '{m0}-01' ORDER BY s.ref_month LIMIT 1)"
        " FROM loan l ORDER BY l.plant_key, l.loan_id;")
    for (lid, pk, name, bank, ccy, principal, total, first, last,
         paid, next_mxn, next_ccy, next_xr) in loans:
        usd = ccy == 'USD'
        e = html.escape
        pay_form = (f'''<form method="post" action="/setup/finance/payments" style="display:inline">
{csrf}<input type="hidden" name="loan_id" value="{e(lid)}">
<label data-en="Payment" data-es="Cuota">Payment</label>
<input type="text" name="amount" size="10" placeholder="{_fin_fmt(next_ccy if usd else next_mxn)}"> {'USD' if usd else 'MXN'}
<label data-en="from" data-es="desde">from</label>
<input type="month" name="from_month" min="{m0}" value="{m0}">
<button class="btn" data-en="Apply" data-es="Aplicar">Apply</button></form>''')
        fx_form = (f'''<form method="post" action="/setup/finance/fx" style="display:inline">
{csrf}<input type="hidden" name="loan_id" value="{e(lid)}">
<label>FX USD/MXN</label>
<input type="text" name="rate" size="7" placeholder="{_fin_fmt(next_xr, 4)}">
<label data-en="from" data-es="desde">from</label>
<input type="month" name="from_month" min="{m0}" value="{m0}">
<button class="btn" data-en="Apply" data-es="Aplicar">Apply</button></form>''') if usd else ''
        principal_form = (f'''<form method="post" action="/setup/finance/principal" style="display:inline">
{csrf}<input type="hidden" name="loan_id" value="{e(lid)}">
<label data-en="Principal MXN" data-es="Principal MXN">Principal MXN</label>
<input type="text" name="amount" size="13" placeholder="{_fin_fmt(principal)}">
<button class="btn" data-en="Apply" data-es="Aplicar">Apply</button></form>''')
        extend_form = (f'''<form method="post" action="/setup/finance/extend" style="display:inline">
{csrf}<input type="hidden" name="loan_id" value="{e(lid)}">
<label data-en="Extend to" data-es="Extender hasta">Extend to</label>
<input type="month" name="to_month" min="{m0}">
<label data-en="at payment" data-es="con cuota">at payment</label>
<input type="text" name="amount" size="10"> {'USD' if usd else 'MXN'}
<button class="btn" data-en="Apply" data-es="Aplicar">Apply</button></form>''')
        trunc_form = (f'''<form method="post" action="/setup/finance/truncate" style="display:inline"
 onsubmit="return confirm('Delete all installments of {e(lid)} from the chosen month onward?')">
{csrf}<input type="hidden" name="loan_id" value="{e(lid)}">
<label data-en="End loan at" data-es="Terminar crédito en">End loan at</label>
<input type="month" name="from_month" min="{m0}">
<button class="btn danger" data-en="Truncate" data-es="Truncar">Truncate</button></form>''')
        cards.append(f'''<div class="card"><h2>{e(pk)} · {e(lid)} — {e(name)}</h2>
<p class="note" style="{mono}">{e(bank)} · {ccy} ·
<span data-en="principal" data-es="principal">principal</span> {_fin_fmt(principal)} MXN ·
<span data-en="position" data-es="posición">position</span> {paid}/{total} · {first} → {last} ·
<span data-en="next payment" data-es="próxima cuota">next payment</span>
{(_fin_fmt(next_ccy) + ' USD × ' + _fin_fmt(next_xr, 4) + ' = ') if usd and next_ccy else ''}{_fin_fmt(next_mxn)} MXN</p>
<div class="controls">{pay_form}</div>
{f'<div class="controls">{fx_form}</div>' if fx_form else ''}
<div class="controls">{principal_form}</div>
<div class="controls">{extend_form}{trunc_form}</div>
</div>''')

    # ---- O&M per plant ----
    om_rows = []
    for pk, cust, om in _fin_rows(
            "SELECT plant_key, customer, coalesce(om_cost_monthly_mxn,0)"
            " FROM plant WHERE active ORDER BY plant_key;"):
        om_rows.append(f'''<tr><td>{html.escape(pk)}</td>
<td>{html.escape(cust)}</td><td style="{mono}">{_fin_fmt(om)}</td>
<td><form method="post" action="/setup/finance/om">{csrf}
<input type="hidden" name="plant" value="{html.escape(pk)}">
<input type="text" name="amount" size="9">
<button class="btn" data-en="Set" data-es="Fijar">Set</button></form></td></tr>''')
    cards.append('<div class="card"><h2 data-en="O&amp;M — monthly cost (MXN)"'
                 ' data-es="O&amp;M — costo mensual (MXN)">O&amp;M — monthly'
                 ' cost (MXN)</h2><table><tr><th>Plant</th><th>Customer</th>'
                 '<th data-en="Current" data-es="Actual">Current</th>'
                 '<th data-en="New value" data-es="Nuevo valor">New value</th>'
                 '</tr>' + ''.join(om_rows) + '</table></div>')

    # ---- LaaS fees + PPA tariffs (contract_monthly, future months) ----
    fee_rows = []
    for pk, fee in _fin_rows(
            "SELECT plant_key, fixed_income_ccy FROM contract_monthly"
            f" WHERE make_date(year, month, 1) = DATE '{m0}-01'"
            " AND fixed_income_ccy IS NOT NULL ORDER BY plant_key;"):
        fee_rows.append(f'''<tr><td>{html.escape(pk)}</td>
<td style="{mono}">{_fin_fmt(fee)} USD</td>
<td><form method="post" action="/setup/finance/fee">{csrf}
<input type="hidden" name="plant" value="{html.escape(pk)}">
<input type="text" name="amount" size="9">
<label data-en="from" data-es="desde">from</label>
<input type="month" name="from_month" min="{m0}" value="{m0}">
<button class="btn" data-en="Set" data-es="Fijar">Set</button></form></td></tr>''')
    tariff_rows = []
    for pk, tar in _fin_rows(
            "SELECT plant_key, tariff_mxn FROM contract_monthly"
            f" WHERE make_date(year, month, 1) = DATE '{m0}-01'"
            " AND tariff_mxn IS NOT NULL ORDER BY plant_key;"):
        tariff_rows.append(f'''<tr><td>{html.escape(pk)}</td>
<td style="{mono}">{_fin_fmt(tar, 4)} MXN/kWh</td>
<td><form method="post" action="/setup/finance/tariff">{csrf}
<input type="hidden" name="plant" value="{html.escape(pk)}">
<input type="text" name="amount" size="8">
<label data-en="from" data-es="desde">from</label>
<input type="month" name="from_month" min="{m0}" value="{m0}">
<button class="btn" data-en="Set" data-es="Fijar">Set</button></form></td></tr>''')
    cards.append('<div class="card"><h2 data-en="LaaS monthly fees (native'
                 ' currency)" data-es="Cuotas LaaS (moneda nativa)">LaaS'
                 ' monthly fees (native currency)</h2><table><tr><th>Asset'
                 '</th><th data-en="Current month" data-es="Mes actual">'
                 'Current month</th><th data-en="New value (flat, from month)"'
                 ' data-es="Nuevo valor (fijo, desde mes)">New value (flat,'
                 ' from month)</th></tr>' + ''.join(fee_rows) +
                 '</table></div>')
    cards.append('<div class="card"><h2 data-en="PPA tariffs (MXN/kWh)"'
                 ' data-es="Tarifas PPA (MXN/kWh)">PPA tariffs (MXN/kWh)</h2>'
                 '<p class="note" data-en="Sets a FLAT tariff from the chosen'
                 ' month onward — contract escalations after that month are'
                 ' overwritten. Use only for renegotiations."'
                 ' data-es="Fija una tarifa PLANA desde el mes elegido —'
                 ' las escalaciones contractuales posteriores se'
                 ' sobrescriben. Solo para renegociaciones.">Sets a FLAT'
                 ' tariff from the chosen month onward.</p><table><tr>'
                 '<th>Plant</th><th data-en="Current month"'
                 ' data-es="Mes actual">Current month</th>'
                 '<th data-en="New value (flat, from month)"'
                 ' data-es="Nuevo valor (fijo, desde mes)">New value</th>'
                 '</tr>' + ''.join(tariff_rows) + '</table></div>')

    # ---- audit tail ----
    audit = _fin_rows(
        "SELECT to_char(ts AT TIME ZONE 'America/Mexico_City',"
        " 'YYYY-MM-DD HH24:MI'), username, plant_key, loan_id, detail"
        " FROM finance_audit ORDER BY id DESC LIMIT 15;")
    audit_rows = ''.join(
        f'<tr><td style="{mono}">{html.escape(a[0])}</td>'
        f'<td>{html.escape(a[1])}</td><td>{html.escape(a[2] or a[3])}</td>'
        f'<td>{html.escape(a[4])}</td></tr>'
        for a in audit if len(a) >= 5)
    cards.append('<div class="card"><h2 data-en="Change log (last 15)"'
                 ' data-es="Registro de cambios (últimos 15)">Change log'
                 ' (last 15)</h2><table><tr><th data-en="When (MX)"'
                 ' data-es="Cuándo (MX)">When (MX)</th><th data-en="Who"'
                 ' data-es="Quién">Who</th><th>Asset</th><th data-en="What"'
                 ' data-es="Qué">What</th></tr>' + (
                     audit_rows or '<tr><td colspan="4" class="note"'
                     ' data-en="No changes yet." data-es="Sin cambios aún.">'
                     'No changes yet.</td></tr>') + '</table></div>')

    back = ('<div class="controls"><a class="btn" href="/setup/"'
            ' data-en="← Access setup" data-es="← Gestión de accesos">'
            '← Access setup</a></div>')
    return page(back + ''.join(cards), msg=msg)


def _fin_loan(loan_id):
    """(plant_key, currency, last_month 'YYYY-MM', last_no) or None."""
    r = _fin_rows(
        "SELECT l.plant_key, l.currency, to_char(max(s.ref_month),'YYYY-MM'),"
        " max(s.installment_no) FROM loan l LEFT JOIN loan_schedule s"
        f" ON s.loan_id = l.loan_id WHERE l.loan_id = {_sqlq(loan_id)}"
        " GROUP BY 1, 2;")
    return r[0] if r and len(r[0]) >= 4 else None


@app.get('/finance')
def finance():
    return finance_page()


@app.post('/finance/om')
def finance_om():
    me = _fin_guard()
    if not me:
        return stale_page()
    plant = (request.form.get('plant') or '').strip().upper()
    amount = fin.parse_num(request.form.get('amount'), 0, fin.MAX_OM)
    if plant.lower() not in PLANTS + ['loax1', 'lgto1'] or amount is None:
        return finance_page(msg='O&M: invalid plant or amount — nothing saved')
    return finance_page(msg=_fin_write(
        me, plant, '', 'om',
        f'{plant}: O&M set to {amount:,.2f} MXN/month',
        [fin.sql_set_om(plant, amount)]))


@app.post('/finance/principal')
def finance_principal():
    me = _fin_guard()
    if not me:
        return stale_page()
    lid = (request.form.get('loan_id') or '').strip()
    info = _fin_loan(lid)
    amount = fin.parse_num(request.form.get('amount'), 1, fin.MAX_PRINCIPAL)
    if not info or amount is None:
        return finance_page(msg='principal: invalid loan or amount — nothing saved')
    return finance_page(msg=_fin_write(
        me, info[0], lid, 'principal',
        f'{lid}: principal set to {amount:,.2f} MXN',
        [fin.sql_set_principal(lid, amount)]))


@app.post('/finance/payments')
def finance_payments():
    me = _fin_guard()
    if not me:
        return stale_page()
    lid = (request.form.get('loan_id') or '').strip()
    info = _fin_loan(lid)
    from_ym = fin.parse_month(request.form.get('from_month'),
                              min_month=_fin_month_now())
    if not info or not from_ym:
        return finance_page(msg='payments: invalid loan or month — nothing saved')
    usd = info[1] == 'USD'
    amount = fin.parse_num(request.form.get('amount'), 0,
                           fin.MAX_PAYMENT_CCY if usd else fin.MAX_PAYMENT_MXN)
    if amount is None:
        return finance_page(msg='payments: invalid amount — nothing saved')
    sql = (fin.sql_set_payment_ccy(lid, from_ym, amount) if usd
           else fin.sql_set_payment_mxn(lid, from_ym, amount))
    return finance_page(msg=_fin_write(
        me, info[0], lid, 'payments',
        f'{lid}: payment {amount:,.2f} {"USD" if usd else "MXN"} from {from_ym}',
        [sql]))


@app.post('/finance/fx')
def finance_fx():
    me = _fin_guard()
    if not me:
        return stale_page()
    lid = (request.form.get('loan_id') or '').strip()
    info = _fin_loan(lid)
    from_ym = fin.parse_month(request.form.get('from_month'),
                              min_month=_fin_month_now())
    rate = fin.parse_num(request.form.get('rate'), fin.FX_MIN, fin.FX_MAX)
    if not info or info[1] != 'USD' or not from_ym or rate is None:
        return finance_page(msg='FX: invalid loan/month/rate — nothing saved')
    return finance_page(msg=_fin_write(
        me, info[0], lid, 'fx',
        f'{lid}: FX projection {rate:.4f} USD/MXN from {from_ym}',
        [fin.sql_set_fx(lid, from_ym, rate)]))


@app.post('/finance/extend')
def finance_extend():
    me = _fin_guard()
    if not me:
        return stale_page()
    lid = (request.form.get('loan_id') or '').strip()
    info = _fin_loan(lid)
    to_ym = fin.parse_month(request.form.get('to_month'),
                            min_month=_fin_month_now())
    if not info or not to_ym:
        return finance_page(msg='extend: invalid loan or month — nothing saved')
    pk, ccy, last_ym, last_no = info[0], info[1], info[2], int(info[3] or 0)
    usd = ccy == 'USD'
    amount = fin.parse_num(request.form.get('amount'), 0,
                           fin.MAX_PAYMENT_CCY if usd else fin.MAX_PAYMENT_MXN)
    if amount is None:
        return finance_page(msg='extend: invalid amount — nothing saved')
    months = fin.months_seq(last_ym, to_ym)
    if not months:
        return finance_page(msg=f'extend: {lid} already runs through {last_ym} — nothing to add')
    xr = None
    if usd:
        r = _fin_rows("SELECT xr FROM loan_schedule WHERE loan_id ="
                      f" {_sqlq(lid)} AND xr IS NOT NULL"
                      " ORDER BY ref_month DESC LIMIT 1;")
        xr = float(r[0][0]) if r and r[0][0] else 17.98
    sqls = [fin.sql_extend(lid, last_no + 1, months, amount,
                           payment_ccy=amount if usd else None,
                           xr=xr if usd else None),
            fin.sql_loan_span_refresh(lid)]
    return finance_page(msg=_fin_write(
        me, pk, lid, 'extend',
        f'{lid}: extended {len(months)} month(s) to {to_ym} at '
        f'{amount:,.2f} {"USD" if usd else "MXN"}', sqls))


@app.post('/finance/truncate')
def finance_truncate():
    me = _fin_guard()
    if not me:
        return stale_page()
    lid = (request.form.get('loan_id') or '').strip()
    info = _fin_loan(lid)
    from_ym = fin.parse_month(request.form.get('from_month'),
                              min_month=_fin_month_now())
    if not info or not from_ym:
        return finance_page(msg='truncate: invalid loan or month — nothing saved')
    return finance_page(msg=_fin_write(
        me, info[0], lid, 'truncate',
        f'{lid}: installments from {from_ym} onward removed',
        fin.sql_truncate(lid, from_ym)))


@app.post('/finance/fee')
def finance_fee():
    me = _fin_guard()
    if not me:
        return stale_page()
    plant = (request.form.get('plant') or '').strip().upper()
    from_ym = fin.parse_month(request.form.get('from_month'),
                              min_month=_fin_month_now())
    amount = fin.parse_num(request.form.get('amount'), 0, fin.MAX_FEE_CCY)
    if not re.match(r'^[A-Z0-9]{3,6}$', plant) or not from_ym \
            or amount is None:
        return finance_page(msg='fee: invalid asset/month/amount — nothing saved')
    return finance_page(msg=_fin_write(
        me, plant, '', 'fee',
        f'{plant}: LaaS fee {amount:,.2f} (native ccy) from {from_ym}',
        [fin.sql_set_fee(plant, from_ym, amount)]))


@app.post('/finance/tariff')
def finance_tariff():
    me = _fin_guard()
    if not me:
        return stale_page()
    plant = (request.form.get('plant') or '').strip().upper()
    from_ym = fin.parse_month(request.form.get('from_month'),
                              min_month=_fin_month_now())
    amount = fin.parse_num(request.form.get('amount'), 0.01, fin.MAX_TARIFF)
    if plant.lower() not in PLANTS or not from_ym or amount is None:
        return finance_page(msg='tariff: invalid plant/month/amount — nothing saved')
    return finance_page(msg=_fin_write(
        me, plant, '', 'tariff',
        f'{plant}: tariff {amount:.4f} MXN/kWh (flat) from {from_ym}',
        [fin.sql_set_tariff(plant, from_ym, amount)]))


def render(msg='', once=None):
    """Role-aware main page."""
    me, is_global, org = actor()
    if not is_global and not org:
        return page('<div class="card"><p data-en="Your account has no management rights."'
                    ' data-es="Su cuenta no tiene permisos de gestión.">'
                    'Your account has no management rights.</p></div>')
    edit_u = (request.args.get('edit') or '').strip()
    ef = edit_form(edit_u) if (edit_u and can_manage(edit_u)) else ''
    if is_global:
        body = ef + users_table() + settings_card(None, True) + stats_table() + add_form()
        body += ('<div class="card"><h2 data-en="Finance setup"'
                 ' data-es="Configuración financiera">Finance setup</h2>'
                 '<p class="note" data-en="Loans, payments, FX rates, O&amp;M,'
                 ' fees and tariffs — the inputs behind the financial report."'
                 ' data-es="Créditos, cuotas, tipo de cambio, O&amp;M, cuotas y'
                 ' tarifas — los insumos del reporte financiero.">Loans, payments,'
                 ' FX rates, O&amp;M, fees and tariffs.</p>'
                 '<a class="btn" href="/setup/finance" data-en="Open finance'
                 ' setup →" data-es="Abrir configuración financiera →">'
                 'Open finance setup →</a></div>')
        body += maintenance_card()
        body += recipients_card()
    else:
        body = (ef + users_table(org=org) + settings_card(org, False)
                + stats_table(org=org) + add_form(org=org))
    return page(body, msg=msg, once=once)


def can_manage(target):
    """May the current actor manage the target user?"""
    me, is_global, org = actor()
    if is_global:
        return True
    if not org:
        return False
    c = db()
    row = c.execute('SELECT org, is_admin FROM users WHERE username=?', (target,)).fetchone()
    c.close()
    return bool(row) and row[0] == org and not row[1]


@app.get('/')
def index():
    return render()


@app.post('/update')
def update():
    if not check_csrf():
        return stale_page()
    me, is_global, org = actor()
    u = (request.form.get('username') or '').strip()
    if not can_manage(u):
        return render(msg='Not allowed.')
    if is_global:
        level = 'argia' if request.form.get('level') == 'argia' else 'custom'
        reports = ','.join(a for a in request.form.getlist('reports') if a in AREAS)
        adm = 1 if request.form.get('is_admin') else 0
    else:                       # company admin can only toggle company-admin flag
        level, reports, adm = 'custom', org, 0
    padm = 1 if request.form.get('plant_admin') else 0
    c = db()
    admins = active_admins(c)
    if not adm and u in admins and len(admins) == 1:
        c.close()
        return render(msg='Refused: cannot remove admin from the last active admin.')
    n = c.execute('UPDATE users SET level=?, reports=?, is_admin=?, '
                  'plant_admin=?, first_name=?, last_name=?, email=? '
                  'WHERE username=?',
                  (level, reports, adm, padm,
                   clean_name(request.form.get('first_name')),
                   clean_name(request.form.get('last_name')),
                   clean_email(request.form.get('email')), u)).rowcount
    c.commit()
    c.close()
    if not n:
        return render(msg='No such user.')
    sync()
    return render(msg=f'Access updated for {u}.')


@app.post('/suspend')
def suspend():
    if not check_csrf():
        return stale_page()
    u = (request.form.get('username') or '').strip()
    if not can_manage(u):
        return render(msg='Not allowed.')
    c = db()
    row = c.execute('SELECT disabled FROM users WHERE username=?', (u,)).fetchone()
    if not row:
        c.close()
        return render(msg='No such user.')
    new_state = 0 if row[0] else 1
    admins = active_admins(c)
    if new_state and u in admins and len(admins) == 1:
        c.close()
        return render(msg='Refused: cannot suspend the last active admin.')
    c.execute('UPDATE users SET disabled=? WHERE username=?', (new_state, u))
    c.commit()
    c.close()
    sync()
    verb = 'suspended — all sessions are now locked out' if new_state else 're-enabled'
    return render(msg=f'User {u} {verb}.')


def check_csrf():
    return request.form.get('csrf') == CSRF


def stale_page():
    return render(msg='This page was open from before an app update, so the action '
                      'was NOT applied. The page has been refreshed — please repeat '
                      'the action once.'), 409


@app.post('/add')
def add():
    if not check_csrf():
        return stale_page()
    me, is_global, org = actor()
    if not is_global and not org:
        return render(msg='No management rights.')
    u = clean_username(request.form.get('username'))
    if not re.fullmatch(r'[a-z0-9.@_-]{2,64}', u):
        return render(msg='Invalid username.')
    pw = clean_password(request.form.get('password'))
    once = None
    if not pw:
        pw = make_password()
        once = pw
    padm = 1 if request.form.get('plant_admin') else 0
    if is_global:
        level = 'argia' if request.form.get('level') == 'argia' else 'custom'
        reports = ','.join(a for a in request.form.getlist('reports') if a in AREAS)
        uorg = (request.form.get('org') or '').strip().lower()
        uorg = uorg if uorg in PLANTS else ''
        adm = 1 if request.form.get('is_admin') else 0
        if padm and uorg and uorg not in reports and level != 'argia':
            reports = ','.join(x for x in (reports.split(',') + [uorg]) if x)
    else:                       # company admin: locked to own plant, never global
        level, reports, uorg, adm = 'custom', org, org, 0
    c = db()
    try:
        c.execute('INSERT INTO users(username,hash,level,reports,is_admin,'
                  'org,plant_admin,first_name,last_name,email) '
                  'VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (u, hash_pw(u, pw), level, reports, adm, uorg, padm,
                   clean_name(request.form.get('first_name')),
                   clean_name(request.form.get('last_name')),
                   clean_email(request.form.get('email'))))
        c.commit()
    except sqlite3.IntegrityError:
        c.close()
        return render(msg=f'User {u} already exists.')
    c.close()
    sync()
    return render(msg=f'User {u} created. They must type the username '
                      f'exactly as "{u}" — logins are case-sensitive, '
                      f'and the password carries no leading or '
                      f'trailing spaces.', once=once)


@app.post('/settings')
def settings():
    if not check_csrf():
        return stale_page()
    me, is_global, org = actor()
    plant = (request.form.get('plant') or '').strip().lower()
    if plant not in PLANTS or (not is_global and plant != org):
        return render(msg='Not allowed for that plant.')
    try:
        year = int(request.form.get('year') or 0)
        assert 2024 <= year <= 2040
    except (ValueError, AssertionError):
        return render(msg='Invalid year.')
    ups = []
    for m in range(1, 13):
        def num(name):
            v = (request.form.get(f'{name}_{m}') or '').strip()
            try:
                return float(v) if v else None
            except ValueError:
                return None
        e, t2 = num('exp'), num('tar')
        if e is None and t2 is None:
            continue
        ups.append((m, e, t2))
    pk = plant.upper()
    for m, e, t2 in ups:
        ev = 'NULL' if e is None else f'{e:.3f}'
        tv = 'NULL' if t2 is None else f'{t2:.4f}'
        psql(f"INSERT INTO contract_monthly(plant_key,year,month,contract_kwh,tariff_mxn) "
             f"VALUES('{pk}',{year},{m},{ev},{tv}) "
             f"ON CONFLICT (plant_key,year,month) DO UPDATE SET "
             f"contract_kwh=COALESCE(EXCLUDED.contract_kwh, contract_monthly.contract_kwh), "
             f"tariff_mxn=COALESCE(EXCLUDED.tariff_mxn, contract_monthly.tariff_mxn);")
    inv = (request.form.get('investment') or '').strip()
    if inv:
        try:
            psql(f"UPDATE plant SET investment_mxn={float(inv):.2f} WHERE plant_key='{pk}';")
        except ValueError:
            pass
    try:
        subprocess.Popen(['systemctl', 'start', 'argia-dashboard.service'])
    except OSError:
        pass
    return render(msg=f'Settings saved for {pk} ({len(ups)} months). '
                      'Reports are regenerating — refresh them in a minute.')


@app.post('/password')
def password():
    if not check_csrf():
        return stale_page()
    u = (request.form.get('username') or '').strip()
    if not can_manage(u):
        return render(msg='Not allowed.')
    pw = make_password()
    c = db()
    n = c.execute('UPDATE users SET hash=? WHERE username=?', (hash_pw(u, pw), u)).rowcount
    c.commit()
    c.close()
    if not n:
        return render(msg='No such user.')
    sync()
    return render(msg=f'Password reset for {u}.', once=pw)


@app.post('/delete')
def delete():
    if not check_csrf():
        return stale_page()
    u = (request.form.get('username') or '').strip()
    if not can_manage(u):
        return render(msg='Not allowed.')
    c = db()
    admins = [r[0] for r in c.execute('SELECT username FROM users WHERE is_admin=1')]
    if u in admins and len(admins) == 1:
        c.close()
        return render(msg='Refused: cannot delete the last admin.')
    c.execute('DELETE FROM users WHERE username=?', (u,))
    c.commit()
    c.close()
    sync()
    return render(msg=f'User {u} deleted.')


# ---------------------------------------------------------------- self
# Any signed-in user changes their OWN password here. Mounted at
# /account/ behind all.htpasswd (see snippets/argia_auth.conf), so it
# needs no admin rights — unlike /setup/, which stays admin-only.

def account_page(msg='', ok=False, user='', pw_done=False):
    """My account: profile (name, surname, email) + password. One page,
    the only place a user manages their own login."""
    tone = '#137333' if ok else '#c5221f'
    note = (f'<div class="card" style="border-color:{tone}">'
            f'<b>{html.escape(msg)}</b></div>') if msg else ''
    if pw_done:   # password just changed — the form would only confuse
        return page(note + '<div class="card"><p data-en="Your browser '
                    'still holds the old password. The next page you '
                    'open will ask for it again — enter the new one, '
                    'and tick &quot;remember&quot; if your browser '
                    'offers it." data-es="Su navegador aún guarda la '
                    'contraseña anterior. La próxima página le '
                    'preguntará de nuevo — escriba la nueva.">'
                    'Your browser still holds the old password. The '
                    'next page you open will ask for it again — enter '
                    'the new one.</p></div>')
    c = db()
    row = c.execute('SELECT first_name,last_name,email,level,is_admin'
                    ' FROM users WHERE username=?', (user,)).fetchone()
    c.close()
    fname, lname, email, level, adm = row or ('', '', '', '', 0)
    access = ('argia — all reports' if level == 'argia'
              else 'limited to the reports granted to you')
    access_es = ('argia — todos los reportes' if level == 'argia'
                 else 'limitado a los reportes que le fueron asignados')
    profile = f'''<div class="card">
<h2 data-en="My details" data-es="Mis datos">My details</h2>
<p class="note" data-en="Signed in as {html.escape(user)} · {access}{' · admin' if adm else ''}. Your username cannot be changed — ask an ARGIA admin."
 data-es="Sesión de {html.escape(user)} · {access_es}{' · admin' if adm else ''}. El usuario no se puede cambiar — pida a un administrador de ARGIA.">
Signed in as {html.escape(user)} · {access}{' · admin' if adm else ''}.</p>
<form method="post" action="profile">
<input type="hidden" name="csrf" value="{CSRF}">
<p><input type="text" name="first_name" placeholder="first name" maxlength="60"
    value="{html.escape(fname or '')}">
 <input type="text" name="last_name" placeholder="surname" maxlength="60"
    value="{html.escape(lname or '')}">
 <input type="text" name="email" placeholder="email" maxlength="120"
    value="{html.escape(email or '')}"></p>
<p><button class="btn" data-en="Save my details" data-es="Guardar mis datos">Save my details</button></p>
</form>
<p class="note" data-en="Your name is what every page shows in the top-right corner, so anyone can see which account they are using."
 data-es="Su nombre es lo que cada página muestra en la esquina superior derecha, para saber qué cuenta se está usando.">
Your name is what every page shows in the top-right corner.</p></div>'''
    return page(note + profile + f'''<div class="card">
<h2 data-en="Change my password" data-es="Cambiar mi contraseña">Change my password</h2>
<form method="post" action="change">
<input type="hidden" name="csrf" value="{CSRF}">
<p><input type="password" name="current" placeholder="current password" required autocomplete="current-password"></p>
<p><input type="password" name="new" placeholder="new password ({PW_MIN}+ characters)" required autocomplete="new-password">
 <input type="password" name="again" placeholder="repeat new password" required autocomplete="new-password"></p>
<p><button class="btn" data-en="Change password" data-es="Cambiar contraseña">Change password</button></p>
</form>
<p class="note" data-en="Spaces at the start or end are trimmed. Forgot the current one? Ask an ARGIA admin for a reset."
 data-es="Los espacios al inicio o final se eliminan. ¿Olvidó la actual? Pida a un administrador de ARGIA que la restablezca.">
Spaces at the start or end are trimmed. Forgot the current one? Ask an ARGIA admin for a reset.</p></div>''')


@app.get('/account/')
def account():
    u = clean_username(request.headers.get('X-Remote-User'))
    return account_page(user=u)


@app.get('/account/whoami')
def whoami():
    """Who nginx authenticated, for the header chip on the static
    pages. Lives under /account/ so it inherits that location's
    all.htpasswd — no extra nginx rule, and it can never answer for
    an unauthenticated caller."""
    u = clean_username(request.headers.get('X-Remote-User'))
    name, email, adm, level = profile_of(u) if u else ('', '', 0, '')
    r = make_response(json.dumps({
        'user': u, 'name': name, 'email': email,
        'admin': bool(adm), 'level': level}), 200)
    r.headers['Content-Type'] = 'application/json'
    r.headers['Cache-Control'] = 'no-store'
    return r


@app.post('/account/change')
def account_change():
    if not check_csrf():
        return stale_page()
    u = clean_username(request.headers.get('X-Remote-User'))
    if not u:
        return account_page('Not signed in.', user=u), 401
    wait = pw_lock_left(u)
    if wait:
        return account_page(f'Too many wrong attempts — try again in '
                            f'{wait // 60 + 1} minute(s).', user=u), 429
    current = clean_password(request.form.get('current'))
    if not verify_pw(u, current):
        pw_note_fail(u)
        return account_page('Current password is not correct.',
                            user=u), 403
    new = clean_password(request.form.get('new'))
    problem = password_problem(
        new, clean_password(request.form.get('again')), current)
    if problem:
        return account_page(problem, user=u), 400
    c = db()
    c.execute('UPDATE users SET hash=? WHERE username=? AND disabled=0',
              (hash_pw(u, new), u))
    c.commit()
    c.close()
    sync()
    pw_clear_fails(u)
    return account_page(f'Password changed for {u}.', ok=True, user=u,
                        pw_done=True)


@app.post('/account/profile')
def account_profile():
    """A user edits their own name / surname / email. Same identity
    rule as the password route: the account comes from the proxy
    header, never from the form, so nobody can edit anyone else."""
    if not check_csrf():
        return stale_page()
    u = clean_username(request.headers.get('X-Remote-User'))
    if not u:
        return account_page('Not signed in.', user=u), 401
    raw_mail = (request.form.get('email') or '').strip()
    email = clean_email(raw_mail)
    if raw_mail and not email:
        return account_page('That email address does not look valid.',
                            user=u), 400
    c = db()
    n = c.execute('UPDATE users SET first_name=?, last_name=?, email=?'
                  ' WHERE username=? AND disabled=0',
                  (clean_name(request.form.get('first_name')),
                   clean_name(request.form.get('last_name')),
                   email, u)).rowcount
    c.commit()
    c.close()
    if not n:
        return account_page('Account not found.', user=u), 404
    return account_page('Your details were saved.', ok=True, user=u)


@app.get('/healthz')
def healthz():
    return make_response('ok', 200)


if __name__ == '__main__':
    os.makedirs(AUTH_DIR, exist_ok=True)
    seed()
    sync()
    app.run(host='127.0.0.1', port=8511, threaded=True)
