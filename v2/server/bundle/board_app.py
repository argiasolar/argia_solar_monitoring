#!/usr/bin/env python3
"""The Solar team board - /board/ on portal.argia.com.mx (v325).

A Kanban board for the daily stand-up: Backlog, This week, In progress
(3 cards per person), Review, Done. Cards can be dragged between columns
and between people (stand-up view), carry an owner, a process from the
process map, a due date, a blocked flag with its reason, watchers and a
comment timeline.

Runs on 127.0.0.1:8516; nginx proxies /board/ here behind the session
login and forwards the account as X-Remote-User. Who may open it:

  owner   ARGIA_BOARD_EMAILS / ARGIA_BOARD_USERS, or a line in
          ARGIA_BOARD_ALLOW (/opt/argia/auth/board_allow.txt, read live).
          Everything, including Members and the starter backlog.
  editor  added by an owner on the Members tab: create, edit, move, comment.
  viewer  added by an owner: read, comment, watch.

Everybody else gets 403, and the landing tile stays hidden (/board/me).
Every write is a JSON POST that must carry the header X-Argia-Board: 1 -
a cross-site form cannot set it, and the session cookie is SameSite=Lax.

  ARGIA_BOARD_PORT    default 8516
"""
from __future__ import annotations

import html
import json
import os
import sys
from typing import Dict, List, Optional

from flask import Flask, jsonify, request, send_file

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from plain_text import plain                    # noqa: E402  (v262)
sys.path.insert(0, os.environ.get('ARGIA_V2_DIR', '/root/argia_v2/v2'))

import portal_chrome as PC                       # noqa: E402
from argia.board import core as C                # noqa: E402
from argia.board.service import Board, NotFound, decorate   # noqa: E402
from argia.board.store import PgStore            # noqa: E402

app = Flask(__name__)
# v326: up to 10 files of 15 MB in one post (nginx client_max_body_size 160m for /board/)
app.config['MAX_CONTENT_LENGTH'] = C.MAX_FILES_PER_POST * C.MAX_FILE + 1024 * 1024
FILES_DIR = os.environ.get('ARGIA_BOARD_FILES', '/opt/argia/board_files')

OWNER_EMAILS = {e.strip().lower() for e in os.environ.get('ARGIA_BOARD_EMAILS', 'tomasz.zemelka@argia.com.mx').split(',') if e.strip()}
OWNER_USERS = {u.strip().lower() for u in os.environ.get('ARGIA_BOARD_USERS', '').split(',') if u.strip()}
ALLOW_FILE = os.environ.get('ARGIA_BOARD_ALLOW', '/opt/argia/auth/board_allow.txt')
CSRF_HEADER = 'X-Argia-Board'


@app.after_request
def _after(r):
    """v262: no em dash leaves this app; nothing of the board is cached."""
    if r.mimetype == 'text/html' and not r.direct_passthrough:
        r.set_data(plain(r.get_data(as_text=True)))
    r.headers['Cache-Control'] = 'no-store'
    return r


# ------------------------------------------------------------- storage
def board() -> Board:
    b = app.config.get('BOARD')
    if b is None:
        st = PgStore()
        st.ensure()
        b = app.config['BOARD'] = Board(st)
    return b


# ------------------------------------------------------------ identity
def actor() -> str:
    return (request.headers.get('X-Remote-User') or '').strip()


def staff() -> List[Dict[str, str]]:
    """Active internal accounts: [{username, name, email}] (the people a
    card can be assigned to, and an owner can add as members)."""
    fn = app.config.get('STAFF')
    if fn:
        return fn()
    try:
        import setup_app as sa
        c = sa.db()
        rows = c.execute("SELECT username, first_name, last_name, email FROM users"
                         " WHERE disabled = 0 AND (level = 'argia' OR is_admin = 1) ORDER BY username").fetchall()
        c.close()
        return [{'username': r[0], 'name': sa.display_name(r[1], r[2], r[0]), 'email': (r[3] or '').strip().lower()}
                for r in rows]
    except Exception:                                    # noqa: BLE001
        return []


def allow_entries() -> set:
    try:
        with open(app.config.get('ALLOW_FILE', ALLOW_FILE), encoding='utf-8') as fh:
            return {ln.split('#', 1)[0].strip().lower() for ln in fh if ln.split('#', 1)[0].strip()}
    except OSError:
        return set()


def is_owner(username: str, people: Optional[List[Dict[str, str]]] = None) -> bool:
    u = (username or '').strip().lower()
    if not u:
        return False
    owners_e = app.config.get('OWNER_EMAILS', OWNER_EMAILS)
    owners_u = app.config.get('OWNER_USERS', OWNER_USERS)
    allow = allow_entries()
    if u in owners_u or u in allow:
        return True
    for p in (people if people is not None else staff()):
        if p['username'].lower() == u:
            return bool(p['email']) and (p['email'] in owners_e or p['email'] in allow)
    return False


def role_of(username: str) -> Optional[str]:
    """'owner' / 'editor' / 'viewer' / None."""
    people = staff()
    if is_owner(username, people):
        return 'owner'
    if not any(p['username'] == username for p in people):
        return None                                      # disabled or customer account
    try:
        return board().role_of(username)
    except Exception:                                    # noqa: BLE001
        return None


def usernames() -> List[str]:
    return [p['username'] for p in staff()]


# --------------------------------------------------------------- helpers
def _err(msg: str, code: int):
    return jsonify({'error': msg}), code


def _guard(action: str, write: bool = False):
    """(username, role) when allowed, else a ready error response."""
    me = actor()
    role = role_of(me)
    if role is None:
        return None, _err('No access to the team board.', 403)
    if write and request.headers.get(CSRF_HEADER) != '1':
        return None, _err('Missing request header.', 403)
    if not C.can(role, action):
        return None, _err('Your role on the board does not allow this.', 403)
    return (me, role), None


def _body() -> Dict:
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _run(fn):
    try:
        return fn()
    except C.Invalid as ex:
        return _err(str(ex), 400)
    except NotFound:
        return _err('That card does not exist (any more).', 404)


def _card_json(c: Dict) -> Dict:
    return decorate([c])[0]


# ------------------------------------------------------------------ API
@app.get('/board/me')
def me():
    r = role_of(actor())
    return jsonify({'allowed': r is not None, 'role': r})


@app.get('/board/api/state')
def api_state():
    who, err = _guard('read')
    if err:
        return err
    return jsonify(state(who[0], who[1]))


def state(me: str, role: str) -> Dict:
    return {
        'me': me, 'role': role, 'wip': C.WIP_LIMIT,
        'rights': sorted(r for r in ('create', 'edit', 'move', 'archive', 'comment', 'watch', 'members', 'seed') if C.can(role, r)),
        'cols': [{'key': k, 'en': en, 'es': es, 'hen': hen, 'hes': hes} for k, en, es, hen, hes in C.COLUMNS],
        'procs': [{'key': k, 'en': en, 'es': es} for k, en, es in C.PROCESSES],
        'people': [{'username': p['username'], 'name': p['name']} for p in staff()],
        'cards': decorate(board().cards()),
    }


@app.get('/board/api/cards/<int:cid>')
def api_card(cid):
    who, err = _guard('read')
    if err:
        return err
    return _run(lambda: jsonify({'card': _card_json(board().card(cid)), 'events': board().history(cid),
                                 'files': board().files(cid)}))


def files_dir() -> str:
    return app.config.get('FILES_DIR', FILES_DIR)


@app.post('/board/api/cards/<int:cid>/files')
def api_files(cid):
    """v326: a comment with attachments (multipart: body + files). Pasted
    screenshots arrive here as ordinary PNG files."""
    who, err = _guard('comment', write=True)
    if err:
        return err
    ups = [(f.filename, f.read()) for f in request.files.getlist('files') if f and f.filename]
    return _run(lambda: jsonify({'card': _card_json(board().attach(who[0], cid, ups, files_dir(), request.form.get('body', ''))),
                                 'events': board().history(cid), 'files': board().files(cid)}))


@app.get('/board/api/cards/<int:cid>/files/<int:fid>')
def api_file(cid, fid):
    who, err = _guard('read')
    if err:
        return err
    try:
        row, path = board().file(cid, fid, files_dir())
    except NotFound:
        return _err('That file does not exist (any more).', 404)
    inline = row['mime'] in ('image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf')
    r = send_file(path, mimetype=row['mime'], as_attachment=not inline, download_name=row['filename'], max_age=0)
    r.headers['X-Content-Type-Options'] = 'nosniff'
    r.headers['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    return r


@app.errorhandler(413)
def too_big(_e):
    return _err('Too much at once: at most 10 files of 15 MB each.', 413)


@app.post('/board/api/cards')
def api_create():
    who, err = _guard('create', write=True)
    if err:
        return err
    return _run(lambda: (jsonify({'card': _card_json(board().create(who[0], _body(), usernames()))}), 201))


@app.post('/board/api/cards/<int:cid>')
def api_update(cid):
    who, err = _guard('edit', write=True)
    if err:
        return err
    return _run(lambda: jsonify({'card': _card_json(board().update(who[0], cid, _body(), usernames()))}))


@app.post('/board/api/cards/<int:cid>/move')
def api_move(cid):
    who, err = _guard('move', write=True)
    if err:
        return err
    d = _body()
    return _run(lambda: jsonify({'card': _card_json(board().move(
        who[0], cid, d.get('col'), d.get('order'), d.get('assignee'), usernames()))}))


@app.post('/board/api/cards/<int:cid>/comment')
def api_comment(cid):
    who, err = _guard('comment', write=True)
    if err:
        return err
    return _run(lambda: jsonify({'card': _card_json(board().comment(who[0], cid, _body().get('body'))),
                                 'events': board().history(cid)}))


@app.post('/board/api/cards/<int:cid>/watch')
def api_watch(cid):
    who, err = _guard('watch', write=True)
    if err:
        return err
    d = _body()
    return _run(lambda: jsonify({'card': _card_json(board().watch(
        who[0], cid, d.get('username') or who[0], bool(d.get('on', True)), usernames()))}))


@app.post('/board/api/cards/<int:cid>/archive')
def api_archive(cid):
    who, err = _guard('archive', write=True)
    if err:
        return err
    return _run(lambda: (board().archive(who[0], cid), jsonify({'ok': True}))[1])


@app.post('/board/api/members')
def api_members():
    who, err = _guard('members', write=True)
    if err:
        return err
    d = _body()
    role = d.get('role') or None
    return _run(lambda: jsonify({'members': board().set_member(who[0], d.get('username'), role, usernames())}))


@app.post('/board/api/seed')
def api_seed():
    who, err = _guard('seed', write=True)
    if err:
        return err
    return jsonify({'added': board().seed(who[0])})


@app.get('/board/healthz')
def healthz():
    return 'ok\n'


# ---------------------------------------------------------------- pages
def T(en: str, es: Optional[str] = None) -> str:
    return PC.t(en, es)


def tabs_for(role: str):
    subs = PC.SECTIONS['board'][2]
    return subs if role == 'owner' else [s for s in subs if s[0] != 'members']


def no_access():
    body = (f'<div class="card" style="padding:18px"><b>{T("The team board is private for now.", "El tablero del equipo es privado por ahora.")}</b>'
            f'<div class="muted">{T("Ask Tomasz to add you on the Members tab.", "Pide a Tomasz que te agregue en la pestaña Miembros.")}</div></div>')
    return PC.page('Team board', body, 'board', '', tabs=[]), 403


def _boot(data: Dict) -> str:
    """JSON safe inside <script>: no '<' at all, so no tag can close or open."""
    return json.dumps(data, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


@app.get('/board/')
def page_board():
    me_, role = actor(), role_of(actor())
    if role is None:
        return no_access()
    body = (f'<div class="bwrap"><script type="application/json" id="boot">{_boot(state(me_, role))}</script>'
            + TOOLBAR + '<div id="board" class="board" aria-live="polite"></div>' + DRAWER + '</div>'
            + f'<script>{JS}</script>')
    return PC.page('Team board', body, 'board', '', extra_head=f'<style>{CSS}</style>', tabs=tabs_for(role), wide=True)


@app.get('/board/members/')
def page_members():
    me_, role = actor(), role_of(actor())
    if role is None:
        return no_access()
    if role != 'owner':
        return PC.page('Team board', f'<div class="card" style="padding:18px">{T("Only board owners manage members.", "Solo los dueños del tablero gestionan miembros.")}</div>',
                       'board', 'members', tabs=tabs_for(role)), 403
    people = staff()
    names = {p['username']: p['name'] for p in people}
    owners = [p for p in people if is_owner(p['username'], people)]
    mem = {m['username']: m['role'] for m in board().members()}
    e = html.escape
    rows = ''.join(
        f'<tr><td><b>{e(names.get(u, u))}</b> <span class="mono muted">{e(u)}</span></td>'
        f'<td><select data-u="{e(u, quote=True)}" class="mrole"><option value="editor"{" selected" if r == "editor" else ""}>editor</option>'
        f'<option value="viewer"{" selected" if r == "viewer" else ""}>viewer</option></select></td>'
        f'<td><button type="button" class="bbtn ghost mdel" data-u="{e(u, quote=True)}">{T("Remove", "Quitar")}</button></td></tr>'
        for u, r in sorted(mem.items()))
    own = ''.join(f'<li><b>{e(p["name"])}</b> <span class="mono muted">{e(p["username"])}</span></li>' for p in owners) or '<li class="muted">-</li>'
    opts = ''.join(f'<option value="{e(p["username"], quote=True)}">{e(p["name"])} ({e(p["username"])})</option>'
                   for p in people if p['username'] not in mem and p not in owners)
    body = f'''<div class="bwrap" style="max-width:860px">
<div class="card" style="padding:20px 22px;display:flex;flex-direction:column;gap:14px">
 <h2 class="ct">{T("Who sees the team board", "Quién ve el tablero del equipo")}</h2>
 <p class="muted" style="margin:0">{T("Editors create, edit and move cards. Viewers read, comment and watch. Owners also manage this list; owners are set on the server (board_allow.txt).", "Los editores crean, editan y mueven tarjetas. Los lectores leen, comentan y siguen. Los dueños además gestionan esta lista; los dueños se configuran en el servidor (board_allow.txt).")}</p>
 <div><div class="kicker">{T("Owners", "Dueños")}</div><ul style="margin:6px 0 0;padding-left:18px">{own}</ul></div>
 <div><div class="kicker">{T("Members", "Miembros")}</div>
  <table class="mtab"><tbody>{rows or f'<tr><td class="muted">{T("Nobody yet. The board is visible to the owners only.", "Nadie todavía. Solo los dueños ven el tablero.")}</td></tr>'}</tbody></table></div>
 <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
  <select id="madd"><option value="">{T("Add a person...", "Agregar a una persona...")}</option>{opts}</select>
  <select id="maddrole"><option value="editor">editor</option><option value="viewer">viewer</option></select>
  <button type="button" class="bbtn" id="maddbtn">{T("Add", "Agregar")}</button>
  <span id="mmsg" class="muted"></span>
 </div>
</div></div>
<script>{MEMBERS_JS}</script>'''
    return PC.page('Team board', body, 'board', 'members', extra_head=f'<style>{CSS}</style>', tabs=tabs_for(role))


TOOLBAR = '''<div class="bbar">
 <input id="q" type="search" autocomplete="off" data-ph-en="Search cards" data-ph-es="Buscar tarjetas" placeholder="Search cards">
 <select id="fwho" aria-label="Person"></select>
 <select id="fproc" aria-label="Process"></select>
 <span id="chips" class="bchips"></span>
 <span class="bgrow"></span>
 <button type="button" class="bbtn ghost" id="lanes" aria-pressed="false"></button>
 <button type="button" class="bbtn" id="newcard"></button>
</div>
<div id="hint" class="bhint"></div>'''

DRAWER = '''<div id="scrim" class="scrim" hidden></div>
<aside id="drawer" class="drawer" hidden aria-label="Card">
 <div class="dhead"><span id="dnum" class="mono muted"></span><button type="button" class="ib dclose" id="dclose" aria-label="Close">&times;</button></div>
 <div id="dbody" class="dbody"></div>
</aside>'''

CSS = r'''
.bwrap{display:flex;flex-direction:column;gap:12px}
.bbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.bbar input,.bbar select,.drawer input,.drawer select,.drawer textarea,#madd,#maddrole,.mrole{font:inherit;font-size:13.5px;padding:7px 10px;border:1px solid var(--line2);border-radius:8px;background:#fff;color:var(--ink)}
.bbar input{width:220px;max-width:100%}
.bgrow{flex:1}
.bbtn{font:inherit;font-weight:700;font-size:13px;padding:8px 14px;border-radius:8px;border:1px solid var(--teal2);background:var(--teal2);color:#fff;cursor:pointer;white-space:nowrap}
.bbtn:hover{background:var(--deep);border-color:var(--deep)}
.bbtn.ghost{background:#fff;color:var(--deep);border-color:var(--line2)}.bbtn.ghost:hover{border-color:var(--teal)}
.bbtn.ghost[aria-pressed="true"]{background:var(--green-bg);border-color:var(--teal);color:var(--teal2)}
.bbtn.danger{background:#fff;color:#c2554e;border-color:#e8b9b5}
.bbtn:disabled{opacity:.5;cursor:default}
.bchips{display:flex;gap:6px;flex-wrap:wrap}
.bchip{font:inherit;font-size:12px;font-weight:700;border-radius:999px;padding:4px 10px;border:1px solid var(--line2);background:#fff;color:var(--ink2);cursor:pointer}
.bchip.on{border-color:currentColor}
.bchip.blk{color:#c2554e}.bchip.due{color:#b26a00}.bchip.old{color:#6b3fb5}
.bhint{font-size:12.5px;color:var(--muted);min-height:1em}
.board{overflow-x:auto;padding-bottom:8px}
.bgrid{display:grid;grid-template-columns:repeat(5,minmax(230px,1fr));gap:12px;min-width:1190px}
.bgrid.lanes{grid-template-columns:150px repeat(5,minmax(210px,1fr));min-width:1250px;row-gap:0}
.bcolh{background:#fff;border:1px solid var(--line);border-radius:12px 12px 0 0;border-bottom:0;padding:10px 12px 8px;display:flex;align-items:center;gap:8px;position:sticky;top:0}
.bcolh .nm{font-weight:800;color:var(--deep);font-size:14px}
.bcolh .ct{font:700 11.5px ui-monospace,Consolas,monospace;color:var(--muted);background:var(--bg);border-radius:9px;padding:1px 7px}
.bcolh .add{margin-left:auto;width:28px;height:28px;border-radius:8px;border:1px solid var(--line2);background:#fff;color:var(--teal2);font-size:18px;line-height:1;cursor:pointer}
.bcolh .add:hover{border-color:var(--teal)}
.bcolh .tw{position:relative}
.blist{background:#f6f7f9;border:1px solid var(--line);border-top:0;border-radius:0 0 12px 12px;padding:8px;display:flex;flex-direction:column;gap:8px;min-height:120px}
.lanes .blist{border-radius:0;border-top:1px solid var(--line);min-height:70px}
.lanes .lrow-last .blist{border-radius:0 0 12px 12px}
.lhead{padding:10px 10px 10px 2px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:3px}
.lhead b{font-size:13.5px;color:var(--ink)}
.lhead .w{font:700 11px ui-monospace,Consolas,monospace;color:var(--muted)}
.lhead .w.over{color:#c2554e}
.bc{background:#fff;border:1px solid var(--line);border-radius:10px;padding:9px 10px 8px;display:flex;flex-direction:column;gap:6px;cursor:grab;position:relative;box-shadow:0 1px 0 rgba(26,29,35,.04);user-select:none}
.bc:hover{border-color:var(--line2);box-shadow:0 4px 12px rgba(26,29,35,.08)}
.bc:focus-visible{outline:2px solid var(--teal);outline-offset:1px}
.bc.ro{cursor:pointer}
.bc.blocked{border-left:4px solid #c2554e;padding-left:7px}
.bc.overwip{box-shadow:0 0 0 2px #f0a83b inset}
.bc .t{font-weight:600;font-size:13.5px;line-height:1.35;color:var(--ink);overflow-wrap:anywhere}
.bc .meta{display:flex;flex-wrap:wrap;gap:5px;align-items:center;font-size:11.5px;color:var(--muted)}
.bc .num{font:600 10.5px ui-monospace,Consolas,monospace;color:#8b95a1}
.bc .proc{font:700 10.5px ui-monospace,Consolas,monospace;background:var(--green-bg);color:var(--teal2);border-radius:5px;padding:1px 5px}
.bc .pri-high{color:#c2554e;font-weight:800}
.bc .pri-low{color:#8b95a1}
.bc .due{border-radius:5px;padding:1px 5px;background:var(--bg)}.bc .due.late{background:#fdeaea;color:#c2554e;font-weight:700}
.bc .age{border-radius:5px;padding:1px 5px;background:var(--bg)}.bc .age.amber{background:#fff4e0;color:#b26a00;font-weight:700}.bc .age.red{background:#fdeaea;color:#c2554e;font-weight:700}
.bc .blk{font-size:11.5px;color:#c2554e;font-weight:600;overflow-wrap:anywhere}
.bc .foot{display:flex;align-items:center;gap:8px}
.bc .foot .sp{flex:1}
.av{width:24px;height:24px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font:800 10px/1 "Segoe UI",Roboto,sans-serif;color:#fff;flex:none}
.av.none{background:#fff;border:1px dashed var(--line2);color:var(--muted)}
.handle{display:none;width:26px;height:26px;margin:-4px -4px -4px 0;border:0;background:none;color:#8b95a1;touch-action:none;cursor:grab;font-size:16px;line-height:1}
@media (pointer:coarse){.handle{display:inline-flex;align-items:center;justify-content:center}}
.dph{border:2px dashed var(--teal);border-radius:10px;background:var(--green-bg);min-height:44px}
.dghost{position:fixed;z-index:80;pointer-events:none;transform:rotate(2deg);box-shadow:0 12px 30px rgba(26,29,35,.22);opacity:.95}
.dragging .bc{cursor:grabbing}
.qadd{display:flex;flex-direction:column;gap:6px}
.qadd textarea{font:inherit;font-size:13.5px;padding:8px 10px;border:1px solid var(--teal);border-radius:10px;resize:vertical;min-height:58px}
.qadd .r{display:flex;gap:6px}
.empty{color:var(--muted);font-size:12.5px;text-align:center;padding:14px 4px}
.drawer[hidden],.scrim[hidden]{display:none!important}
.scrim{position:fixed;inset:0;background:rgba(5,59,56,.18);z-index:60}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(520px,100%);background:#fff;border-left:1px solid var(--line);box-shadow:-12px 0 40px rgba(26,29,35,.14);z-index:70;display:flex;flex-direction:column}
.dhead{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--line)}
.dclose{font-size:22px}
.dbody{overflow-y:auto;padding:16px 18px 28px;display:flex;flex-direction:column;gap:14px}
.dbody .ttl{font-size:17px;font-weight:700;width:100%;min-height:0;resize:none;line-height:1.35}
.dbody label{display:flex;flex-direction:column;gap:4px;font-size:11.5px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.dbody label.inl{flex-direction:row;align-items:center;gap:8px;text-transform:none;letter-spacing:0;font-size:13.5px;color:var(--ink);font-weight:600}
.dbody textarea{min-height:110px;resize:vertical}
.dgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:520px){.dgrid{grid-template-columns:1fr}}
.wlist{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.wchip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line2);border-radius:999px;padding:2px 4px 2px 3px;font-size:12.5px}
.wchip button{border:0;background:none;color:var(--muted);cursor:pointer;font-size:14px;padding:0 4px}
.tl{display:flex;flex-direction:column;gap:10px;border-top:1px solid var(--line);padding-top:12px}
.ev{display:flex;gap:10px;font-size:13px}
.ev .bd{flex:1;min-width:0}
.ev .who{font-weight:700}
.ev .when{color:var(--muted);font-size:11.5px;margin-left:6px}
.ev .txt{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);border-radius:8px;padding:7px 10px;margin-top:3px}
.ev.sys{color:var(--muted);font-size:12.5px}
.dmsg{font-size:12.5px;min-height:1em}.dmsg.bad{color:#c2554e}.dmsg.ok{color:var(--teal2)}
.drow{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.dbody>*{flex-shrink:0}
.cbox{border-radius:10px;display:flex;flex-direction:column}.cbox textarea{width:100%}.cbox.drop{outline:2px dashed var(--teal);outline-offset:3px}
.pend{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.pchip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line2);border-radius:8px;padding:3px 4px 3px 3px;font-size:12px;max-width:100%}
.pchip img{width:36px;height:36px;object-fit:cover;border-radius:5px}.pchip button{border:0;background:none;color:var(--muted);cursor:pointer;font-size:15px}
.atts{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}.thumb img{max-width:220px;max-height:160px;border-radius:8px;border:1px solid var(--line);display:block}
.fchip{display:inline-flex;gap:6px;align-items:center;border:1px solid var(--line2);border-radius:8px;padding:5px 9px;font-size:12.5px;overflow-wrap:anywhere}
.mtab{width:100%;border-collapse:collapse;margin-top:6px}.mtab td{padding:8px 6px;border-bottom:1px solid var(--line)}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--deep);color:#fff;padding:9px 16px;border-radius:9px;font-size:13px;z-index:90}
.toast.bad{background:#c2554e}
.seedbox{background:#fff;border:1px dashed var(--teal);border-radius:12px;padding:16px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
'''

JS = r'''
(function(){
"use strict";
var S=JSON.parse(document.getElementById('boot').textContent);
var LS={get:function(k,d){try{var v=localStorage.getItem(k);return v===null?d:v;}catch(e){return d;}},set:function(k,v){try{localStorage.setItem(k,v);}catch(e){}}};
function lang(){return (LS.get('argia_lang','')||document.documentElement.lang||'en')==='es'?'es':'en';}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function tx(en,es){var l=lang();return '<span data-en="'+esc(en)+'" data-es="'+esc(es)+'">'+esc(l==='es'?es:en)+'</span>';}
function tt(en,es){return lang()==='es'?es:en;}
function can(r){return S.rights.indexOf(r)>=0;}
var PEOPLE={};S.people.forEach(function(p){PEOPLE[p.username]=p.name||p.username;});
function nm(u){return u?(PEOPLE[u]||u):tt('Unassigned','Sin asignar');}
var PAL=['#05847d','#3b6fb6','#b26a00','#6b3fb5','#c2554e','#2f7d3a','#8a5a44','#4f4a94'];
function col4(u){var h=0;for(var i=0;i<u.length;i++){h=(h*31+u.charCodeAt(i))>>>0;}return PAL[h%PAL.length];}
function ini(u){var n=nm(u).trim().split(/\s+/);return ((n[0]||'?')[0]+(n.length>1?n[n.length-1][0]:'')).toUpperCase();}
function av(u){return u?'<span class="av" style="background:'+col4(u)+'" title="'+esc(nm(u))+'">'+esc(ini(u))+'</span>':'<span class="av none" title="'+esc(tt('Unassigned','Sin asignar'))+'">?</span>';}
var PROC={};S.procs.forEach(function(p){PROC[p.key]=p;});
var COLS=S.cols;
var F={q:LS.get('board_q',''),who:LS.get('board_who',''),proc:LS.get('board_proc',''),chip:'',lanes:LS.get('board_lanes','0')==='1'};
var boardEl=document.getElementById('board');
var open=null, dirty=false, busy=false;

function api(method,url,body){
 return fetch(url,{method:method,credentials:'same-origin',headers:method==='GET'?{}:{'Content-Type':'application/json','X-Argia-Board':'1'},body:body?JSON.stringify(body):undefined})
  .then(function(r){return r.json().catch(function(){return {};}).then(function(d){if(!r.ok){throw new Error(d.error||('HTTP '+r.status));}return d;});});
}
function toast(msg,bad){var t=document.createElement('div');t.className='toast'+(bad?' bad':'');t.textContent=msg;document.body.appendChild(t);setTimeout(function(){t.remove();},bad?4200:1800);}
function reload(){return api('GET','/board/api/state').then(function(d){S=d;S.people.forEach(function(p){PEOPLE[p.username]=p.name||p.username;});render();}).catch(function(e){toast(e.message,true);});}
function upsert(c){for(var i=0;i<S.cards.length;i++){if(S.cards[i].id===c.id){S.cards[i]=c;return;}}S.cards.push(c);}

/* ------------------------------------------------------------ filters */
function match(c){
 if(F.who==='__me'&&c.assignee!==S.me)return false;
 if(F.who&&F.who!=='__me'&&F.who!=='__none'&&c.assignee!==F.who)return false;
 if(F.who==='__none'&&c.assignee)return false;
 if(F.proc&&c.process!==F.proc)return false;
 if(F.chip==='blocked'&&!c.blocked)return false;
 if(F.chip==='overdue'&&!c.overdue)return false;
 if(F.chip==='stuck'&&!c.aging)return false;
 if(F.q){var h=(c.number+' '+c.title+' '+c.description+' '+nm(c.assignee)+' '+(c.blocked_reason||'')).toLowerCase();
  var ws=F.q.toLowerCase().split(/\s+/).filter(Boolean);for(var i=0;i<ws.length;i++){if(h.indexOf(ws[i])<0)return false;}}
 return true;
}
function wipOf(){var w={};S.cards.forEach(function(c){if(c.col==='doing'&&c.assignee){w[c.assignee]=(w[c.assignee]||0)+1;}});return w;}

function toolbar(){
 var q=document.getElementById('q');if(q.value!==F.q)q.value=F.q;
 var w=document.getElementById('fwho');
 w.innerHTML='<option value="">'+esc(tt('Everyone','Todos'))+'</option><option value="__me">'+esc(tt('Only me','Solo yo'))+'</option><option value="__none">'+esc(tt('Unassigned','Sin asignar'))+'</option>'+
  S.people.map(function(p){return '<option value="'+esc(p.username)+'">'+esc(p.name)+'</option>';}).join('');
 w.value=F.who;
 var p=document.getElementById('fproc');
 p.innerHTML='<option value="">'+esc(tt('All processes','Todos los procesos'))+'</option>'+S.procs.map(function(x){return '<option value="'+x.key+'">'+x.key+' '+esc(tt(x.en,x.es))+'</option>';}).join('');
 p.value=F.proc;
 var nb=0,nd=0,ns=0;S.cards.forEach(function(c){if(c.blocked)nb++;if(c.overdue)nd++;if(c.aging)ns++;});
 document.getElementById('chips').innerHTML=
  '<button type="button" class="bchip blk'+(F.chip==='blocked'?' on':'')+'" data-chip="blocked">'+tx('Blocked','Bloqueadas')+' '+nb+'</button>'+
  '<button type="button" class="bchip due'+(F.chip==='overdue'?' on':'')+'" data-chip="overdue">'+tx('Overdue','Vencidas')+' '+nd+'</button>'+
  '<button type="button" class="bchip old'+(F.chip==='stuck'?' on':'')+'" data-chip="stuck" title="'+esc(tt('Sitting too long in their column','Demasiado tiempo en su columna'))+'">'+tx('Stuck','Detenidas')+' '+ns+'</button>';
 var l=document.getElementById('lanes');l.setAttribute('aria-pressed',F.lanes?'true':'false');l.innerHTML=tx('Stand-up view','Vista de daily');
 var n=document.getElementById('newcard');n.innerHTML='+ '+tx('New card','Nueva tarjeta');n.hidden=!can('create');
 document.getElementById('hint').innerHTML=F.lanes?tx('Stand-up: walk the board right to left. Done first, then Review, In progress, blockers. Drag a card to another person to hand it over.','Daily: recorre el tablero de derecha a izquierda. Primero Hecho, luego Revisión, En curso, bloqueos. Arrastra una tarjeta a otra persona para entregarla.'):
  (can('move')?tx('Drag cards between columns. Click a card to open it.','Arrastra tarjetas entre columnas. Haz clic en una tarjeta para abrirla.'):tx('You can read, comment and watch cards.','Puedes leer, comentar y seguir tarjetas.'));
}

/* -------------------------------------------------------------- render */
function cardHtml(c,over){
 var due=c.due?'<span class="due'+(c.overdue?' late':'')+'" title="'+esc(tt('Due','Vence'))+'">'+esc(c.due.slice(5).replace('-','/'))+'</span>':'';
 var age=(c.col!=='done'&&c.col!=='backlog'&&c.days>0)?'<span class="age '+(c.aging||'')+'" title="'+esc(tt('Days in this column','Días en esta columna'))+'">'+c.days+'d</span>':'';
 var pri=c.priority==='high'?'<span class="pri-high" title="'+esc(tt('High priority','Prioridad alta'))+'">&#9650;</span>':c.priority==='low'?'<span class="pri-low" title="'+esc(tt('Low priority','Prioridad baja'))+'">&#9660;</span>':'';
 var proc=c.process?'<span class="proc" title="'+esc(PROC[c.process]?tt(PROC[c.process].en,PROC[c.process].es):'')+'">'+esc(c.process)+'</span>':'';
 var cm=(c.comments?'<span title="'+esc(tt('Comments','Comentarios'))+'">&#128172; '+c.comments+'</span>':'')+(c.files?' <span title="'+esc(tt('Attachments','Adjuntos'))+'">&#128206; '+c.files+'</span>':'');
 return '<div class="bc'+(c.blocked?' blocked':'')+(over?' overwip':'')+(can('move')?'':' ro')+'" data-id="'+c.id+'" tabindex="0" role="button" aria-label="'+esc(c.number+' '+c.title)+'">'+
  '<div class="meta">'+pri+'<span class="num">'+esc(c.number)+'</span>'+proc+due+age+'</div>'+
  '<div class="t">'+esc(c.title)+'</div>'+
  (c.blocked?'<div class="blk">&#9940; '+esc(c.blocked_reason)+'</div>':'')+
  '<div class="foot">'+av(c.assignee)+'<span class="meta">'+cm+'</span><span class="sp"></span>'+(can('move')?'<button type="button" class="handle" aria-label="Drag">&#8942;&#8942;</button>':'')+'</div></div>';
}
function colHead(k){
 var c=COLS.filter(function(x){return x.key===k;})[0];
 var n=S.cards.filter(function(x){return x.col===k&&match(x);}).length;
 return '<div class="bcolh" title="'+esc(tt(c.hen,c.hes))+'"><span class="nm">'+tx(c.en,c.es)+'</span><span class="ct">'+n+(k==='doing'?' &middot; '+S.wip+'/'+tt('person','persona'):'')+'</span>'+
  (can('create')?'<button type="button" class="add" data-add="'+k+'" aria-label="'+esc(tt('Add a card','Agregar tarjeta'))+'">+</button>':'')+'</div>';
}
function listHtml(col,lane,cards,wip){
 var inner=cards.map(function(c){return cardHtml(c,c.col==='doing'&&c.assignee&&wip[c.assignee]>S.wip);}).join('');
 return '<div class="blist" data-col="'+col+'" data-lane="'+(lane===null?'':esc(lane))+'"'+(lane===null?' data-free="1"':'')+'>'+(inner||(lane===null?'<div class="empty">'+tx('Nothing here','Nada aquí')+'</div>':''))+'</div>';
}
function sorted(list){return list.slice().sort(function(a,b){return (a.position-b.position)||(a.id-b.id);});}
function render(){
 toolbar();
 var vis=sorted(S.cards.filter(match)), wip=wipOf(), h='';
 if(!S.cards.length&&can('seed')){
  h='<div class="seedbox"><b>'+tx('The board is empty.','El tablero está vacío.')+'</b><span class="muted">'+tx('Load the first backlog from the process map (9 cards), or add your own.','Carga el primer backlog del mapa de procesos (9 tarjetas), o agrega las tuyas.')+'</span>'+
    '<button type="button" class="bbtn" id="seed">'+tx('Load starter backlog','Cargar backlog inicial')+'</button></div>';
 }
 if(!F.lanes){
  h+='<div class="bgrid">'+COLS.map(function(c){return '<div>'+colHead(c.key)+listHtml(c.key,null,vis.filter(function(x){return x.col===c.key;}),wip)+'</div>';}).join('')+'</div>';
 }else{
  var lanes=[];S.people.forEach(function(p){if(vis.some(function(c){return c.assignee===p.username;})||p.username===S.me)lanes.push(p.username);});
  vis.forEach(function(c){if(c.assignee&&lanes.indexOf(c.assignee)<0)lanes.push(c.assignee);});
  lanes.push('');
  if(F.who&&F.who!=='__me'&&F.who!=='__none')lanes=lanes.filter(function(u){return u===F.who;});
  if(F.who==='__me')lanes=[S.me];if(F.who==='__none')lanes=[''];
  h+='<div class="bgrid lanes"><div></div>'+COLS.map(function(c){return colHead(c.key);}).join('');
  lanes.forEach(function(u,i){
   var mine=vis.filter(function(c){return (c.assignee||'')===u;}), n=wip[u]||0;
   h+='<div class="lhead'+(i===lanes.length-1?' lrow-last':'')+'">'+av(u)+'<b>'+esc(nm(u))+'</b>'+(u?'<span class="w'+(n>S.wip?' over':'')+'">'+tx('In progress','En curso')+' '+n+'/'+S.wip+'</span>':'')+'</div>';
   h+=COLS.map(function(c){return '<div class="'+(i===lanes.length-1?'lrow-last':'')+'">'+listHtml(c.key,u,mine.filter(function(x){return x.col===c.key;}),wip)+'</div>';}).join('');
  });
  h+='</div>';
 }
 boardEl.innerHTML=h;
}

/* ------------------------------------------------------------ toolbar events */
document.getElementById('q').addEventListener('input',function(e){F.q=e.target.value;LS.set('board_q',F.q);render();});
document.getElementById('fwho').addEventListener('change',function(e){F.who=e.target.value;LS.set('board_who',F.who);render();});
document.getElementById('fproc').addEventListener('change',function(e){F.proc=e.target.value;LS.set('board_proc',F.proc);render();});
document.getElementById('chips').addEventListener('click',function(e){var b=e.target.closest('[data-chip]');if(!b)return;F.chip=F.chip===b.dataset.chip?'':b.dataset.chip;render();});
document.getElementById('lanes').addEventListener('click',function(){F.lanes=!F.lanes;LS.set('board_lanes',F.lanes?'1':'0');render();});
function laneDefault(){return F.lanes&&boardEl.querySelector('.blist[data-lane="'+S.me+'"]')?S.me:null;}
document.getElementById('newcard').addEventListener('click',function(){quickAdd('backlog',laneDefault());});
boardEl.addEventListener('click',function(e){
 var a=e.target.closest('[data-add]');if(a){quickAdd(a.dataset.add,laneDefault());return;}
 var sd=e.target.closest('#seed');if(sd){sd.disabled=true;api('POST','/board/api/seed',{}).then(function(d){toast(tt('Loaded ','Cargadas ')+d.added);reload();}).catch(function(x){toast(x.message,true);});}
});
boardEl.addEventListener('keydown',function(e){var c=e.target.closest('.bc');if(c&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openCard(+c.dataset.id);}});

function quickAdd(col,lane){
 var list=boardEl.querySelector('.blist[data-col="'+col+'"]'+(lane!==null?'[data-lane="'+lane+'"]':''));if(!list)return;
 if(list.querySelector('.qadd'))return list.querySelector('textarea').focus();
 var em=list.querySelector('.empty');if(em)em.remove();
 var f=document.createElement('div');f.className='qadd';
 f.innerHTML='<textarea maxlength="200" placeholder="'+esc(tt('What needs to be done? Enter to add, Esc to cancel','¿Qué hay que hacer? Enter para agregar, Esc para cancelar'))+'"></textarea><div class="r"><button type="button" class="bbtn">'+tx('Add','Agregar')+'</button><button type="button" class="bbtn ghost">'+tx('Cancel','Cancelar')+'</button></div>';
 list.insertBefore(f,list.firstChild);
 var ta=f.querySelector('textarea');ta.focus();
 function go(){var title=ta.value.trim();if(!title){render();return;}
  var body={title:title,col:col};if(list.dataset.lane)body.assignee=list.dataset.lane;else if(F.who&&F.who.indexOf('__')!==0)body.assignee=F.who;else if(F.who==='__me')body.assignee=S.me;
  ta.disabled=true;api('POST','/board/api/cards',body).then(function(d){upsert(d.card);render();quickAdd(col,lane);}).catch(function(x){ta.disabled=false;toast(x.message,true);});}
 f.querySelector('.bbtn').onclick=go;f.querySelector('.ghost').onclick=function(){render();};
 ta.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();go();}if(e.key==='Escape'){render();}});
}

/* --------------------------------------------------------------- drag & drop */
var D=null;
boardEl.addEventListener('pointerdown',function(e){
 var el=e.target.closest('.bc');if(!el||e.button!==0)return;
 var onHandle=!!e.target.closest('.handle');
 if(e.target.closest('button,a,input,select,textarea')&&!onHandle)return;
 if(e.pointerType==='touch'&&!onHandle){D={el:el,id:+el.dataset.id,x:e.clientX,y:e.clientY,started:false,tapOnly:true};return;}
 D={el:el,id:+el.dataset.id,x:e.clientX,y:e.clientY,started:false,tapOnly:!can('move')};
 if(onHandle){e.preventDefault();}
});
function startDrag(e){
 var r=D.el.getBoundingClientRect();
 D.dx=D.x-r.left;D.dy=D.y-r.top;D.started=true;busy=true;
 D.ph=document.createElement('div');D.ph.className='dph';D.ph.style.height=r.height+'px';
 D.el.parentNode.insertBefore(D.ph,D.el);
 D.g=D.el.cloneNode(true);D.g.classList.add('dghost');D.g.style.width=r.width+'px';document.body.appendChild(D.g);
 D.el.style.display='none';document.body.classList.add('dragging');
}
function listAt(x,y){var el=document.elementFromPoint(x,y);return el?el.closest('.blist'):null;}
document.addEventListener('pointermove',function(e){
 if(!D)return;
 if(!D.started){if(D.tapOnly||Math.abs(e.clientX-D.x)+Math.abs(e.clientY-D.y)<6)return;startDrag(e);}
 e.preventDefault();
 D.g.style.left=(e.clientX-D.dx)+'px';D.g.style.top=(e.clientY-D.dy)+'px';
 var l=listAt(e.clientX,e.clientY);
 if(l){var em=l.querySelector('.empty');if(em)em.remove();
  var kids=[].slice.call(l.querySelectorAll('.bc')).filter(function(k){return k!==D.el;}),before=null;
  for(var i=0;i<kids.length;i++){var r=kids[i].getBoundingClientRect();if(e.clientY<r.top+r.height/2){before=kids[i];break;}}
  if(before){if(D.ph.nextSibling!==before)l.insertBefore(D.ph,before);}else if(D.ph.parentNode!==l||D.ph.nextSibling){l.appendChild(D.ph);}
 }
 var br=boardEl.getBoundingClientRect();
 if(e.clientX>br.right-40)boardEl.scrollLeft+=18;else if(e.clientX<br.left+40)boardEl.scrollLeft-=18;
},{passive:false});
function endDrag(cancel){
 var d=D;D=null;busy=false;document.body.classList.remove('dragging');
 if(!d||!d.started)return d;
 var l=d.ph.parentNode;d.g.remove();
 if(cancel||!l){render();return null;}
 l.insertBefore(d.el,d.ph);d.ph.remove();d.el.style.display='';
 var col=l.dataset.col,order=[].slice.call(l.querySelectorAll('.bc')).map(function(k){return +k.dataset.id;});
 var body={col:col,order:order};
 if(!l.dataset.free){body.assignee=l.dataset.lane;}
 var c=S.cards.filter(function(x){return x.id===d.id;})[0];
 var prev=JSON.stringify(c);
 order.forEach(function(id,i){S.cards.forEach(function(x){if(x.id===id&&(x.col===col||x.id===d.id)){x.position=i+1;}});});
 if(c){if(c.col!==col){c.col=col;c.days=0;c.aging='';}if(body.assignee!==undefined)c.assignee=body.assignee;}
 render();
 api('POST','/board/api/cards/'+d.id+'/move',body).then(function(r){upsert(r.card);render();}).catch(function(x){toast(x.message,true);if(c){upsert(JSON.parse(prev));}reload();});
 return null;
}
document.addEventListener('pointerup',function(e){
 if(!D)return;
 var d=D;
 if(!d.started){D=null;if(Math.abs(e.clientX-d.x)+Math.abs(e.clientY-d.y)<8)openCard(d.id);return;}
 endDrag(false);
});
document.addEventListener('pointercancel',function(){if(D&&D.started)endDrag(true);D=null;});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){if(D&&D.started){endDrag(true);}else if(open){closeCard();}}});

/* ------------------------------------------------------------------- drawer */
var drawer=document.getElementById('drawer'),scrim=document.getElementById('scrim'),dbody=document.getElementById('dbody');
function closeCard(){if(dirty&&!window.confirm(tt('Discard your changes?','¿Descartar tus cambios?')))return;open=null;dirty=false;drawer.hidden=true;scrim.hidden=true;}
document.getElementById('dclose').onclick=closeCard;scrim.onclick=closeCard;
function when(ts){var d=new Date(ts);if(isNaN(d))return ts;return d.toLocaleString(lang()==='es'?'es-MX':'en-GB',{timeZone:'America/Mexico_City',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});}
function colName(k){var c=COLS.filter(function(x){return x.key===k;})[0];return c?tt(c.en,c.es):k;}
var FILES=[];
function fsize(n){return n>1048576?(n/1048576).toFixed(1)+' MB':Math.max(1,Math.round(n/1024))+' kB';}
function filesHtml(e){var fs=FILES.filter(function(f){return f.event_id===e.id;});if(!fs.length)return '';
 return '<div class="atts">'+fs.map(function(f){var u='/board/api/cards/'+open+'/files/'+f.id;
  return /^image\/(png|jpeg|gif|webp)$/.test(f.mime)?'<a class="thumb" href="'+u+'" target="_blank" rel="noopener"><img src="'+u+'" alt="'+esc(f.filename)+'" loading="lazy"></a>'
   :'<a class="fchip" href="'+u+'" target="_blank" rel="noopener">&#128206; '+esc(f.filename)+' <span class="muted">'+fsize(f.size)+'</span></a>';}).join('')+'</div>';}
function evHtml(e){
 var who='<span class="who">'+esc(nm(e.actor))+'</span><span class="when">'+esc(when(e.ts))+'</span>',m=e.meta||{},t='';
 if(e.kind==='comment')return '<div class="ev">'+av(e.actor)+'<div class="bd">'+who+'<div class="txt">'+esc(e.body)+'</div>'+filesHtml(e)+'</div></div>';
 if(e.kind==='attachment')return '<div class="ev">'+av(e.actor)+'<div class="bd">'+who+' '+esc(tt('added files','agregó archivos'))+filesHtml(e)+'</div></div>';
 if(e.kind==='created')t=tt('created the card','creó la tarjeta');
 else if(e.kind==='moved')t=tt('moved it','la movió')+': '+colName(m.from)+' &rarr; '+colName(m.to);
 else if(e.kind==='assigned')t=tt('assigned it to','la asignó a')+' '+esc(nm(m.to));
 else if(e.kind==='blocked')t=tt('marked it blocked','la marcó bloqueada')+': '+esc(e.body);
 else if(e.kind==='unblocked')t=tt('unblocked it','la desbloqueó');
 else if(e.kind==='edited')t=tt('edited','editó')+' '+esc((m.fields||[]).join(', '));
 else if(e.kind==='watcher_added')t=tt('added watcher','agregó seguidor')+' '+esc(nm(m.who));
 else if(e.kind==='watcher_removed')t=tt('removed watcher','quitó seguidor')+' '+esc(nm(m.who));
 else if(e.kind==='archived')t=tt('archived it','la archivó');
 else t=esc(e.kind);
 return '<div class="ev sys">'+av(e.actor)+'<div class="bd">'+who+' '+t+'</div></div>';
}
function opt(v,label,sel){return '<option value="'+esc(v)+'"'+(v===sel?' selected':'')+'>'+esc(label)+'</option>';}
var PEND=[];
function pendDraw(){var p=document.getElementById('pend');if(!p)return;p.innerHTML=PEND.map(function(f,i){var img=/^image\//.test(f.type)?'<img src="'+URL.createObjectURL(f)+'" alt="">':'&#128206;';return '<span class="pchip">'+img+'<span>'+esc(f.name)+' <span class="muted">'+fsize(f.size)+'</span></span><button type="button" data-unpend="'+i+'" aria-label="remove">&times;</button></span>';}).join('');
 p.querySelectorAll('[data-unpend]').forEach(function(b){b.onclick=function(){PEND.splice(+b.dataset.unpend,1);pendDraw();};});}
function addFiles(list){[].slice.call(list||[]).forEach(function(f){PEND.push(f);});dirty=true;pendDraw();}
function stamp(){var d=new Date(),z=function(n){return (n<10?'0':'')+n;};return d.getFullYear()+z(d.getMonth()+1)+z(d.getDate())+'-'+z(d.getHours())+z(d.getMinutes())+z(d.getSeconds());}
function drawCard(c,events,files){
 FILES=files||[];PEND=[];
 var ro=!can('edit'),dis=ro?' disabled':'';
 document.getElementById('dnum').textContent=c.number+(c.days?' · '+c.days+(lang()==='es'?' días en ':' days in ')+colName(c.col):'');
 var people=opt('',tt('Unassigned','Sin asignar'),c.assignee||'')+S.people.map(function(p){return opt(p.username,p.name,c.assignee);}).join('');
 var procs=opt('',tt('No process','Sin proceso'),c.process||'')+S.procs.map(function(p){return opt(p.key,p.key+' '+tt(p.en,p.es),c.process);}).join('');
 var cols=COLS.map(function(x){return opt(x.key,tt(x.en,x.es),c.col);}).join('');
 var pris=[['high',tt('High','Alta')],['normal',tt('Normal','Normal')],['low',tt('Low','Baja')]].map(function(p){return opt(p[0],p[1],c.priority);}).join('');
 var wl=(c.watchers||[]).map(function(u){return '<span class="wchip">'+av(u)+esc(nm(u))+(can('watch')?'<button type="button" data-unwatch="'+esc(u)+'" aria-label="remove">&times;</button>':'')+'</span>';}).join('');
 var wadd=S.people.filter(function(p){return (c.watchers||[]).indexOf(p.username)<0;}).map(function(p){return opt(p.username,p.name,'');}).join('');
 dbody.innerHTML=
  '<textarea class="ttl" id="f_title" maxlength="200" rows="2"'+dis+'>'+esc(c.title)+'</textarea>'+
  '<div class="dgrid">'+
   '<label>'+tx('Column','Columna')+'<select id="f_col"'+(can('move')?'':' disabled')+'>'+cols+'</select></label>'+
   '<label>'+tx('Owner','Responsable')+'<select id="f_assignee"'+dis+'>'+people+'</select></label>'+
   '<label>'+tx('Process','Proceso')+'<select id="f_process"'+dis+'>'+procs+'</select></label>'+
   '<label>'+tx('Priority','Prioridad')+'<select id="f_priority"'+dis+'>'+pris+'</select></label>'+
   '<label>'+tx('Due date','Fecha límite')+'<input type="date" id="f_due" value="'+esc(c.due||'')+'"'+dis+'></label>'+
   '<label class="inl" style="align-self:end"><input type="checkbox" id="f_blocked"'+(c.blocked?' checked':'')+dis+'> '+tx('Blocked','Bloqueada')+'</label>'+
  '</div>'+
  '<label id="l_reason"'+(c.blocked?'':' hidden')+'>'+tx('Why is it blocked? Who can unblock it?','¿Por qué está bloqueada? ¿Quién puede desbloquearla?')+'<input id="f_blocked_reason" maxlength="500" value="'+esc(c.blocked_reason||'')+'"'+dis+'></label>'+
  '<label>'+tx('Description','Descripción')+'<textarea id="f_description" maxlength="8000"'+dis+'>'+esc(c.description||'')+'</textarea></label>'+
  (ro?'':'<div class="drow"><button type="button" class="bbtn" id="dsave">'+tx('Save','Guardar')+'</button><span class="dmsg" id="dmsg"></span><span class="bgrow"></span>'+(can('archive')?'<button type="button" class="bbtn danger" id="darch">'+tx('Archive','Archivar')+'</button>':'')+'</div>')+
  '<div><div class="kicker" style="margin-bottom:6px">'+tx('Watchers','Seguidores')+'</div><div class="wlist">'+(wl||'<span class="muted">-</span>')+
   (can('watch')&&wadd?'<select id="wadd"><option value="">'+esc(tt('+ Add watcher','+ Agregar seguidor'))+'</option>'+wadd+'</select>':'')+'</div></div>'+
  '<div class="tl"><div class="kicker">'+tx('Comments and history','Comentarios e historial')+'</div>'+
   (can('comment')?'<div id="cbox" class="cbox"><textarea id="f_comment" maxlength="4000" placeholder="'+esc(tt('Write a comment. Paste a screenshot (Ctrl+V) or drop files here. Ctrl+Enter to post.','Escribe un comentario. Pega una captura (Ctrl+V) o suelta archivos aquí. Ctrl+Enter para publicar.'))+'" style="min-height:70px"></textarea><div id="pend" class="pend"></div></div>'+
    '<div class="drow"><button type="button" class="bbtn ghost" id="dcomm">'+tx('Post comment','Publicar comentario')+'</button><button type="button" class="bbtn ghost" id="dattach">&#128206; '+tx('Attach files','Adjuntar archivos')+'</button><input type="file" id="f_files" multiple hidden accept=".png,.jpg,.jpeg,.gif,.webp,.heic,.pdf,.txt,.csv,.xlsx,.docx,.pptx,.zip,.kmz,.dwg,.mp4"></div>':'')+
   '<div id="evs">'+events.slice().reverse().map(evHtml).join('')+'</div></div>';
 dirty=false;
 ['f_title','f_assignee','f_process','f_priority','f_due','f_blocked','f_blocked_reason','f_description'].forEach(function(id){var el=document.getElementById(id);if(el){el.addEventListener('input',function(){dirty=true;});el.addEventListener('change',function(){dirty=true;});}});
 var fb=document.getElementById('f_blocked');if(fb)fb.addEventListener('change',function(){document.getElementById('l_reason').hidden=!fb.checked;if(fb.checked)document.getElementById('f_blocked_reason').focus();});
 var sv=document.getElementById('dsave');if(sv)sv.onclick=function(){save(c);};
 var fc=document.getElementById('f_col');if(fc)fc.onchange=function(){moveTo(c,fc.value);};
 var cm=document.getElementById('dcomm'),ct=document.getElementById('f_comment');
 if(cm){cm.onclick=function(){comment(c,ct);};ct.addEventListener('keydown',function(e){if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){comment(c,ct);}});
  var fi=document.getElementById('f_files');document.getElementById('dattach').onclick=function(){fi.click();};fi.onchange=function(){addFiles(fi.files);fi.value='';};
  dbody.addEventListener('paste',function(e){var it=(e.clipboardData&&e.clipboardData.items)||[],got=[];for(var i=0;i<it.length;i++){if(it[i].kind==='file'){var f=it[i].getAsFile();if(f){var ext=(f.type.split('/')[1]||'png').replace('jpeg','jpg');got.push(new File([f],/^image\//.test(f.type)&&(!f.name||f.name==='image.png')?'screenshot-'+stamp()+'.'+ext:f.name,{type:f.type}));}}}if(got.length){e.preventDefault();addFiles(got);ct.focus();}});
  var cb=document.getElementById('cbox');['dragenter','dragover'].forEach(function(k){cb.addEventListener(k,function(e){e.preventDefault();cb.classList.add('drop');});});['dragleave','drop'].forEach(function(k){cb.addEventListener(k,function(e){e.preventDefault();cb.classList.remove('drop');});});
  cb.addEventListener('drop',function(e){if(e.dataTransfer&&e.dataTransfer.files.length)addFiles(e.dataTransfer.files);});}
 var wa=document.getElementById('wadd');if(wa)wa.onchange=function(){if(wa.value)watch(c,wa.value,true);};
 dbody.querySelectorAll('[data-unwatch]').forEach(function(b){b.onclick=function(){watch(c,b.dataset.unwatch,false);};});
 var ar=document.getElementById('darch');if(ar)ar.onclick=function(){
  if(ar.dataset.sure!=='1'){ar.dataset.sure='1';ar.innerHTML=tx('Click again to archive','Haz clic otra vez para archivar');return;}
  api('POST','/board/api/cards/'+c.id+'/archive',{}).then(function(){S.cards=S.cards.filter(function(x){return x.id!==c.id;});dirty=false;closeCard();render();toast(tt('Archived','Archivada'));}).catch(function(x){toast(x.message,true);});};
}
function openCard(id){
 open=id;drawer.hidden=false;scrim.hidden=false;dbody.innerHTML='<div class="muted">...</div>';
 api('GET','/board/api/cards/'+id).then(function(d){if(open!==id)return;upsert(d.card);drawCard(d.card,d.events,d.files);}).catch(function(x){toast(x.message,true);closeCard();});
}
function msg(t,bad){var m=document.getElementById('dmsg');if(m){m.className='dmsg '+(bad?'bad':'ok');m.textContent=t;}}
function save(c){
 var body={},map={title:'f_title',assignee:'f_assignee',process:'f_process',priority:'f_priority',due:'f_due',description:'f_description',blocked_reason:'f_blocked_reason'};
 Object.keys(map).forEach(function(k){var v=document.getElementById(map[k]).value;if(String(c[k]==null?'':c[k])!==v)body[k]=v;});
 var b=document.getElementById('f_blocked').checked;if(b!==!!c.blocked)body.blocked=b;
 if(!Object.keys(body).length){msg(tt('Nothing changed','Sin cambios'));return;}
 api('POST','/board/api/cards/'+c.id,body).then(function(d){upsert(d.card);render();dirty=false;msg(tt('Saved','Guardado'));openCard(c.id);}).catch(function(x){msg(x.message,true);});
}
function moveTo(c,col){
 var order=sorted(S.cards.filter(function(x){return x.col===col&&x.id!==c.id;})).map(function(x){return x.id;});order.push(c.id);
 api('POST','/board/api/cards/'+c.id+'/move',{col:col,order:order}).then(function(d){upsert(d.card);render();openCard(c.id);}).catch(function(x){msg(x.message,true);});
}
function comment(c,ta){var v=ta.value.trim();if(!v&&!PEND.length)return;ta.disabled=true;
 var done=function(d){upsert(d.card);render();PEND=[];var keep=false;drawCard(d.card,d.events,d.files);dirty=keep;};
 if(!PEND.length){api('POST','/board/api/cards/'+c.id+'/comment',{body:v}).then(function(d){d.files=FILES;done(d);}).catch(function(x){ta.disabled=false;toast(x.message,true);});return;}
 var fd=new FormData();fd.append('body',v);PEND.forEach(function(f){fd.append('files',f,f.name);});
 document.getElementById('dcomm').disabled=true;
 fetch('/board/api/cards/'+c.id+'/files',{method:'POST',credentials:'same-origin',headers:{'X-Argia-Board':'1'},body:fd})
  .then(function(r){return r.json().catch(function(){return {};}).then(function(d){if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;});})
  .then(done).catch(function(x){ta.disabled=false;var b=document.getElementById('dcomm');if(b)b.disabled=false;toast(x.message,true);});}
function watch(c,u,on){api('POST','/board/api/cards/'+c.id+'/watch',{username:u,on:on}).then(function(d){upsert(d.card);openCard(c.id);}).catch(function(x){toast(x.message,true);});}

/* keep the board fresh for the stand-up screen: every 30 s, never mid-drag or mid-edit */
setInterval(function(){if(!busy&&!D&&!(open&&dirty)&&document.visibilityState==='visible'&&!boardEl.querySelector('.qadd'))reload();},30000);
/* the header language switch re-renders the parts drawn here */
document.addEventListener('click',function(e){if(e.target.closest('.lang-btn'))setTimeout(function(){render();if(open&&!dirty)openCard(open);},0);});
render();
var m=location.hash.match(/^#SB-(\d+)$/);if(m)openCard(+m[1]);
})();
'''

MEMBERS_JS = r'''
(function(){
function post(u,role){return fetch('/board/api/members',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Argia-Board':'1'},body:JSON.stringify({username:u,role:role})})
 .then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.error||r.status);return d;});});}
function done(){location.reload();}
function fail(e){document.getElementById('mmsg').textContent=e.message;}
document.getElementById('maddbtn').onclick=function(){var u=document.getElementById('madd').value;if(!u)return;post(u,document.getElementById('maddrole').value).then(done).catch(fail);};
document.querySelectorAll('.mrole').forEach(function(s){s.onchange=function(){post(s.dataset.u,s.value).then(done).catch(fail);};});
document.querySelectorAll('.mdel').forEach(function(b){b.onclick=function(){post(b.dataset.u,null).then(done).catch(fail);};});
})();
'''


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('ARGIA_BOARD_PORT', '8516')))
