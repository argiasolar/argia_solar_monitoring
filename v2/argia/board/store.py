"""Storage for the Solar team board (v325).

Two stores with one interface:

  PgStore   PostgreSQL through psql (the server's model: ``runuser -u
            postgres -- psql``, peer auth, no password). Every statement
            is built here with quoted literals; results come back as one
            JSON value per query, so no CSV parsing.
  MemStore  the same behaviour in memory, for the app tests.

tests/unit/test_board_service.py runs one scenario list against both, so
they cannot drift apart.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Callable, Dict, List, Optional

ENSURE_SQL = """CREATE TABLE IF NOT EXISTS board_card (
    id             serial PRIMARY KEY,
    title          text NOT NULL,
    description    text NOT NULL DEFAULT '',
    col            text NOT NULL DEFAULT 'backlog'
                   CHECK (col IN ('backlog','todo','doing','review','done')),
    position       double precision NOT NULL DEFAULT 0,
    assignee       text NOT NULL DEFAULT '',
    process        text NOT NULL DEFAULT '',
    priority       text NOT NULL DEFAULT 'normal' CHECK (priority IN ('high','normal','low')),
    due            date,
    blocked        boolean NOT NULL DEFAULT false,
    blocked_reason text NOT NULL DEFAULT '',
    archived       boolean NOT NULL DEFAULT false,
    created_by     text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    col_since      timestamptz NOT NULL DEFAULT now(),
    done_at        timestamptz
);
CREATE INDEX IF NOT EXISTS idx_board_card_col ON board_card (archived, col, position);
CREATE TABLE IF NOT EXISTS board_watcher (
    card_id  int  NOT NULL REFERENCES board_card(id) ON DELETE CASCADE,
    username text NOT NULL,
    PRIMARY KEY (card_id, username)
);
CREATE TABLE IF NOT EXISTS board_event (
    id      serial PRIMARY KEY,
    card_id int  NOT NULL REFERENCES board_card(id) ON DELETE CASCADE,
    ts      timestamptz NOT NULL DEFAULT now(),
    actor   text NOT NULL DEFAULT '',
    kind    text NOT NULL,
    body    text NOT NULL DEFAULT '',
    meta    jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_board_event_card ON board_event (card_id, ts);
CREATE TABLE IF NOT EXISTS board_member (
    username text PRIMARY KEY,
    role     text NOT NULL CHECK (role IN ('editor','viewer')),
    added_by text NOT NULL DEFAULT '',
    added_at timestamptz NOT NULL DEFAULT now()
);
"""

CARD_FIELDS = ('id', 'title', 'description', 'col', 'position', 'assignee', 'process', 'priority', 'due',
               'blocked', 'blocked_reason', 'archived', 'created_by', 'created_at', 'updated_at', 'col_since', 'done_at')


def q(value) -> str:
    """A SQL literal. Strings are single-quoted with quotes doubled
    (standard_conforming_strings is on by default, so a backslash is just a
    character); psql never interpolates :variables or runs \\commands inside a
    quoted literal. None -> NULL, bool -> true/false, int/float as numbers."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        return repr(float(value))
    return "'" + str(value).replace(chr(0), '').replace("'", "''") + "'"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------- PostgreSQL
_CARD_SELECT = ("SELECT c.id, c.title, c.description, c.col, c.position, c.assignee, c.process, c.priority,"
                " c.due, c.blocked, c.blocked_reason, c.archived, c.created_by, c.created_at, c.updated_at,"
                " c.col_since, c.done_at,"
                " (SELECT coalesce(json_agg(w.username ORDER BY w.username), '[]'::json) FROM board_watcher w WHERE w.card_id = c.id) AS watchers,"
                " (SELECT count(*) FROM board_event e WHERE e.card_id = c.id AND e.kind = 'comment') AS comments"
                " FROM board_card c")


class PgStore:
    def __init__(self, run: Optional[Callable[[str], str]] = None):
        self._run = run or self._default_run

    @staticmethod
    def _default_run(sql: str) -> str:
        from argia.store import pgq
        return pgq._run(sql, ['-t', '-A'])

    def _json(self, sql: str):
        out = self._run(sql).strip()
        return json.loads(out) if out else None

    def _list(self, inner: str) -> List[Dict]:
        return self._json(f"SELECT coalesce(json_agg(x), '[]'::json) FROM ({inner}) x;") or []

    def ensure(self) -> None:
        self._run(ENSURE_SQL)

    def list_cards(self, done_window_days: int) -> List[Dict]:
        return self._list(f"{_CARD_SELECT} WHERE NOT c.archived AND (c.col <> 'done' OR c.done_at IS NULL"
                          f" OR c.done_at > now() - interval '{int(done_window_days)} days')"
                          " ORDER BY c.col, c.position, c.id")

    def get_card(self, card_id: int) -> Optional[Dict]:
        got = self._list(f"{_CARD_SELECT} WHERE c.id = {int(card_id)}")
        return got[0] if got else None

    def count_all(self) -> int:
        return int(self._json("SELECT count(*) FROM board_card;") or 0)

    def max_position(self, col: str) -> float:
        v = self._json(f"SELECT coalesce(max(position), 0) FROM board_card WHERE col = {q(col)} AND NOT archived;")
        return float(v or 0)

    def insert_card(self, fields: Dict, actor: str, position: float) -> int:
        cols = ['title', 'description', 'col', 'assignee', 'process', 'priority', 'due', 'blocked', 'blocked_reason']
        vals = [q(fields.get(c, '' if c not in ('due', 'blocked') else (None if c == 'due' else False))) for c in cols]
        done = 'now()' if fields.get('col') == 'done' else 'NULL'
        out = self._run(f"INSERT INTO board_card ({', '.join(cols)}, position, created_by, done_at)"
                        f" VALUES ({', '.join(vals)}, {q(float(position))}, {q(actor)}, {done}) RETURNING id;")
        return int(out.strip().splitlines()[0])

    def update_card(self, card_id: int, changes: Dict) -> None:
        if not changes:
            return
        sets = ', '.join(f'{k} = {q(v)}' for k, v in changes.items())
        self._run(f"UPDATE board_card SET {sets}, updated_at = now() WHERE id = {int(card_id)};")

    def move_card(self, card_id: int, col: str, col_changed: bool, order: List[int]) -> None:
        stmts = ['BEGIN;']
        if col_changed:
            done = 'now()' if col == 'done' else 'NULL'
            stmts.append(f"UPDATE board_card SET col = {q(col)}, col_since = now(), done_at = {done}, updated_at = now()"
                         f" WHERE id = {int(card_id)};")
        for i, cid in enumerate(order):
            stmts.append(f"UPDATE board_card SET position = {i + 1} WHERE id = {int(cid)} AND col = {q(col)};")
        stmts.append('COMMIT;')
        self._run('\n'.join(stmts))

    def add_event(self, card_id: int, actor: str, kind: str, body: str = '', meta: Optional[Dict] = None) -> None:
        self._run(f"INSERT INTO board_event (card_id, actor, kind, body, meta) VALUES ({int(card_id)}, {q(actor)},"
                  f" {q(kind)}, {q(body or '')}, {q(json.dumps(meta or {}, ensure_ascii=False))}::jsonb);")

    def events(self, card_id: int) -> List[Dict]:
        return self._list(f"SELECT id, ts, actor, kind, body, meta FROM board_event WHERE card_id = {int(card_id)} ORDER BY ts, id")

    def set_watcher(self, card_id: int, username: str, on: bool) -> None:
        if on:
            self._run(f"INSERT INTO board_watcher (card_id, username) VALUES ({int(card_id)}, {q(username)}) ON CONFLICT DO NOTHING;")
        else:
            self._run(f"DELETE FROM board_watcher WHERE card_id = {int(card_id)} AND username = {q(username)};")

    def members(self) -> List[Dict]:
        return self._list("SELECT username, role, added_by, added_at FROM board_member ORDER BY username")

    def set_member(self, username: str, role: Optional[str], actor: str) -> None:
        if role is None:
            self._run(f"DELETE FROM board_member WHERE username = {q(username)};")
        else:
            self._run(f"INSERT INTO board_member (username, role, added_by) VALUES ({q(username)}, {q(role)}, {q(actor)})"
                      f" ON CONFLICT (username) DO UPDATE SET role = EXCLUDED.role, added_by = EXCLUDED.added_by, added_at = now();")


# ------------------------------------------------------------------ memory
class MemStore:
    """PgStore's behaviour without a database (the app tests)."""

    def __init__(self):
        self.cards: Dict[int, Dict] = {}
        self.watchers: set = set()
        self.ev: List[Dict] = []
        self.mem: Dict[str, Dict] = {}
        self._id = 0
        self._eid = 0
        self.clock: Optional[Callable[[], str]] = None

    def _ts(self) -> str:
        return self.clock() if self.clock else _now()

    def ensure(self) -> None:
        return None

    def _out(self, c: Dict) -> Dict:
        r = copy.deepcopy(c)
        r['watchers'] = sorted(u for cid, u in self.watchers if cid == c['id'])
        r['comments'] = sum(1 for e in self.ev if e['card_id'] == c['id'] and e['kind'] == 'comment')
        return r

    def list_cards(self, done_window_days: int) -> List[Dict]:
        now = dt.datetime.fromisoformat(self._ts())
        out = []
        for c in self.cards.values():
            if c['archived']:
                continue
            if c['col'] == 'done' and c['done_at']:
                if dt.datetime.fromisoformat(c['done_at']) <= now - dt.timedelta(days=done_window_days):
                    continue
            out.append(self._out(c))
        return sorted(out, key=lambda r: (r['col'], r['position'], r['id']))

    def get_card(self, card_id: int) -> Optional[Dict]:
        c = self.cards.get(int(card_id))
        return self._out(c) if c else None

    def count_all(self) -> int:
        return len(self.cards)

    def max_position(self, col: str) -> float:
        return max([c['position'] for c in self.cards.values() if c['col'] == col and not c['archived']] or [0.0])

    def insert_card(self, fields: Dict, actor: str, position: float) -> int:
        self._id += 1
        ts = self._ts()
        c = {'id': self._id, 'title': fields.get('title', ''), 'description': fields.get('description', ''),
             'col': fields.get('col', 'backlog'), 'position': float(position), 'assignee': fields.get('assignee', ''),
             'process': fields.get('process', ''), 'priority': fields.get('priority', 'normal'), 'due': fields.get('due'),
             'blocked': bool(fields.get('blocked', False)), 'blocked_reason': fields.get('blocked_reason', ''),
             'archived': False, 'created_by': actor, 'created_at': ts, 'updated_at': ts, 'col_since': ts,
             'done_at': ts if fields.get('col') == 'done' else None}
        self.cards[self._id] = c
        return self._id

    def update_card(self, card_id: int, changes: Dict) -> None:
        c = self.cards[int(card_id)]
        c.update(copy.deepcopy(changes))
        c['updated_at'] = self._ts()

    def move_card(self, card_id: int, col: str, col_changed: bool, order: List[int]) -> None:
        c = self.cards[int(card_id)]
        ts = self._ts()
        if col_changed:
            c.update(col=col, col_since=ts, done_at=ts if col == 'done' else None, updated_at=ts)
        for i, cid in enumerate(order):
            o = self.cards.get(int(cid))
            if o and o['col'] == col:
                o['position'] = float(i + 1)

    def add_event(self, card_id: int, actor: str, kind: str, body: str = '', meta: Optional[Dict] = None) -> None:
        self._eid += 1
        self.ev.append({'id': self._eid, 'card_id': int(card_id), 'ts': self._ts(), 'actor': actor, 'kind': kind,
                        'body': body or '', 'meta': copy.deepcopy(meta or {})})

    def events(self, card_id: int) -> List[Dict]:
        return [{k: v for k, v in e.items() if k != 'card_id'} for e in self.ev if e['card_id'] == int(card_id)]

    def set_watcher(self, card_id: int, username: str, on: bool) -> None:
        (self.watchers.add if on else self.watchers.discard)((int(card_id), username))

    def members(self) -> List[Dict]:
        return [dict(m) for _, m in sorted(self.mem.items())]

    def set_member(self, username: str, role: Optional[str], actor: str) -> None:
        if role is None:
            self.mem.pop(username, None)
        else:
            self.mem[username] = {'username': username, 'role': role, 'added_by': actor, 'added_at': self._ts()}
