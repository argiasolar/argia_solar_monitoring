"""Pure authorisation logic for the ARGIA session login.

Everything here is a plain function over plain data: no Flask, no
sockets, no filesystem beyond an explicit sqlite path.  That is
deliberate — this module decides who may read which page, so it has to
be testable without a server.

Three jobs:

  area_for_path()  which grant a URL needs.  This MUST agree with what
                   the nginx Basic-auth snippet did, location for
                   location, or the switch to sessions silently changes
                   who can see what.  Longest prefix wins, as in nginx.
  may()            whether a user row satisfies that grant.
  sign()/verify()  the cookie: an HMAC over the session id, so a
                   forged or edited cookie is rejected before the
                   database is touched.

The session itself lives server-side (see sessions.py usage in
auth_app): the cookie carries only an opaque id.  That is what makes
logout real — deleting the row kills the session even if someone kept
a copy of the cookie, which is exactly what HTTP Basic could not do.
"""
import base64
import hashlib
import hmac
import os
import sqlite3
import time

PLANTS = ['gto1', 'mex1', 'mex2', 'nl1', 'slp1', 'slp2',
          'gto2', 'qro1', 'nl2', 'mex3', 'tam1']
AREAS = ['financial'] + PLANTS + ['capex', 'monitoring']

# Areas that are not in AREAS but that a path can demand.
ALL = 'all'          # any signed-in user
ADMIN = 'admin'      # /setup/
PUBLIC = None        # no login at all

# Pages reachable without a session. Keep this list short and exact:
# every entry is a hole in the login.
PUBLIC_EXACT = {
    '/login', '/login/', '/logout',
    '/logged-out.html', '/no-access.html',
    '/favicon.ico', '/favicon.svg', '/apple-touch-icon.png',
    '/apple-touch-icon-precomposed.png',
}
PUBLIC_PREFIX = ('/.well-known/',)

# Longest-prefix table, mirroring nginx location blocks one for one.
# Order does not matter: the lookup picks the longest match.
PREFIX_AREA = {
    '/setup/': ADMIN,
    '/account/': ALL,
    '/financial/': 'financial',
    # invoice annexes carry PPA tariffs and revenue — same audience as
    # the financial report (v155, monthly-close feature 2026-09-01)
    '/invoices/': 'financial',
    # the portfolio map shows fleet-wide PPA revenue — financial-grade
    # content, financial-grade gate (v177)
    '/portfolio/': 'financial',
    '/capex/': 'capex',
    '/monitoring/': 'monitoring',
    '/monitoring/assets/': ALL,
    '/monitoring/capex/': 'capex',
}
for _p in PLANTS:
    PREFIX_AREA[f'/{_p}/'] = _p
# CAPEX plants are gated per plant inside the monitoring portal too, so
# an owner reaches their own live page and nothing else.  PPA plants
# are NOT listed here: they fall through to '/monitoring/', which is
# internal-only, exactly as before.
for _p in ('gto2', 'qro1', 'nl2', 'mex3', 'tam1'):
    PREFIX_AREA[f'/monitoring/{_p}/'] = _p


def normalise(uri):
    """Path part of a request URI, query string and fragment removed.

    nginx matches locations against the decoded path; a query string
    must never be able to change which grant is required
    (/setup/?x=/gto1/ asks for admin, not gto1)."""
    p = (uri or '/').split('#', 1)[0].split('?', 1)[0]
    if not p.startswith('/'):
        p = '/' + p
    while '//' in p:
        p = p.replace('//', '/')
    return p


def area_for_path(uri):
    """The grant this URL needs: PUBLIC, ALL, ADMIN or an area name."""
    p = normalise(uri)
    if p in PUBLIC_EXACT or p.startswith(PUBLIC_PREFIX):
        return PUBLIC
    # '/setup' and '/account' without the slash are 301s in nginx, but
    # be strict here in case that ever changes.
    if p in ('/setup', '/account'):
        return PREFIX_AREA[p + '/']
    best, area = '', ALL
    for prefix, a in PREFIX_AREA.items():
        if p.startswith(prefix) and len(prefix) > len(best):
            best, area = prefix, a
    return area


def may(user, area):
    """Whether a user row may read a page needing `area`.

    `user` is a mapping with level / reports / is_admin / plant_admin /
    disabled, as stored by setup_app.  A missing or disabled user may
    nothing — including the pages any signed-in user can read.
    """
    if not user or user.get('disabled'):
        return False
    if area is PUBLIC:
        return True
    if area == ADMIN:
        return bool(user.get('is_admin') or user.get('plant_admin'))
    if area == ALL:
        return True
    if user.get('level') == 'argia':
        return True                      # employees: everything but /setup/
    granted = {x for x in (user.get('reports') or '').split(',') if x}
    return area in granted


# ----------------------------------------------------------------- cookie

COOKIE = 'argia_s'


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def sign(sid, secret):
    """sid.signature — the cookie value."""
    sid = sid.encode() if isinstance(sid, str) else sid
    mac = hmac.new(secret, sid, hashlib.sha256).digest()[:18]
    return f'{_b64(sid)}.{_b64(mac)}'


def verify(value, secret):
    """The session id inside a cookie, or None when it does not verify.

    Rejects anything malformed without raising: this runs on every
    request, including ones crafted to break it.
    """
    if not value or not isinstance(value, str) or value.count('.') != 1:
        return None
    sid_b64, mac_b64 = value.split('.')
    try:
        pad = lambda s: s + '=' * (-len(s) % 4)      # noqa: E731
        sid = base64.urlsafe_b64decode(pad(sid_b64))
        mac = base64.urlsafe_b64decode(pad(mac_b64))
    except Exception:                                # noqa: BLE001
        return None
    good = hmac.new(secret, sid, hashlib.sha256).digest()[:18]
    if not hmac.compare_digest(mac, good):
        return None
    try:
        return sid.decode('ascii')
    except UnicodeDecodeError:
        return None


def new_sid():
    return _b64(os.urandom(18))


def load_secret(path):
    """Read the HMAC key, creating it on first use. 0600, owner only."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        with open(path, 'rb') as fh:
            raw = fh.read().strip()
        if len(raw) >= 32:
            return raw
    except FileNotFoundError:
        pass
    raw = base64.urlsafe_b64encode(os.urandom(48))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as fh:
        fh.write(raw)
    return raw


# ---------------------------------------------------------------- sessions

# Idle timeout and an absolute cap.  The cap matters more than it looks:
# without it a browser left open on a shared machine stays signed in for
# ever, which is the failure mode Basic auth had.
IDLE_MAX = 12 * 3600
ABS_MAX = 30 * 24 * 3600


def open_sessions(path):
    c = sqlite3.connect(path, timeout=10)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions(
        sid TEXT PRIMARY KEY, username TEXT NOT NULL,
        created INTEGER NOT NULL, last INTEGER NOT NULL,
        agent TEXT NOT NULL DEFAULT '')''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_sess_user'
              ' ON sessions(username)')
    return c


def session_alive(created, last, now=None, idle=IDLE_MAX, cap=ABS_MAX):
    now = time.time() if now is None else now
    return (now - last) < idle and (now - created) < cap
