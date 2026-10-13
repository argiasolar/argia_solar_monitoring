"""Board operations (v325): validation from ``core``, storage from
``store``, and one timeline event for every change, so the card history
answers "who moved this, and when" at the stand-up."""
from __future__ import annotations

import datetime as dt
from typing import Dict, Iterable, List, Optional

from argia.board import core as C


class NotFound(LookupError):
    pass


class Board:
    def __init__(self, store, done_window_days: int = C.DONE_WINDOW_DAYS):
        self.s = store
        self.done_window_days = done_window_days

    # ------------------------------------------------------------ reads
    def cards(self) -> List[Dict]:
        return self.s.list_cards(self.done_window_days)

    def card(self, card_id: int) -> Dict:
        c = self.s.get_card(int(card_id))
        if c is None or c.get('archived'):
            raise NotFound(card_id)
        return c

    def history(self, card_id: int) -> List[Dict]:
        self.card(card_id)
        return self.s.events(int(card_id))

    # ----------------------------------------------------------- writes
    def create(self, actor: str, data: Dict, people: Optional[Iterable[str]] = None) -> Dict:
        f = C.validate(data, partial=False, people=people)
        f['col'] = C.validate_col(data.get('col') or 'backlog')
        pos = self.s.max_position(f['col']) + 1
        cid = self.s.insert_card(f, actor, pos)
        self.s.add_event(cid, actor, 'created', '', {'col': f['col']})
        self.s.set_watcher(cid, actor, True)
        if f.get('assignee'):
            self.s.set_watcher(cid, f['assignee'], True)
        return self.card(cid)

    def update(self, actor: str, card_id: int, data: Dict, people: Optional[Iterable[str]] = None) -> Dict:
        cur = self.card(card_id)
        data = dict(data or {})
        if data.get('blocked') not in (None, False, '', 'false', '0') and 'blocked_reason' not in data:
            data['blocked_reason'] = cur.get('blocked_reason') or ''
        f = C.validate(data, partial=True, people=people)
        changes = {k: v for k, v in f.items() if (cur.get(k) if k != 'due' else (cur.get('due') or None)) != v}
        if not changes:
            return cur
        self.s.update_card(int(card_id), changes)
        rest = dict(changes)
        if 'assignee' in rest:
            a = rest.pop('assignee')
            self.s.add_event(card_id, actor, 'assigned', '', {'from': cur.get('assignee') or '', 'to': a})
            if a:
                self.s.set_watcher(card_id, a, True)
        if 'blocked' in rest:
            b = rest.pop('blocked')
            reason = rest.pop('blocked_reason', changes.get('blocked_reason', ''))
            self.s.add_event(card_id, actor, 'blocked' if b else 'unblocked', reason if b else '')
        elif 'blocked_reason' in rest and cur.get('blocked'):
            self.s.add_event(card_id, actor, 'blocked', rest.pop('blocked_reason'))
        else:
            rest.pop('blocked_reason', None)
        if rest:
            self.s.add_event(card_id, actor, 'edited', '', {'fields': sorted(rest)})
        return self.card(card_id)

    def move(self, actor: str, card_id: int, col, order, assignee=None,
             people: Optional[Iterable[str]] = None) -> Dict:
        cur = self.card(card_id)
        col = C.validate_col(col)
        order = C.validate_order(order, int(card_id))
        changed = col != cur['col']
        self.s.move_card(int(card_id), col, changed, order)
        if changed:
            self.s.add_event(card_id, actor, 'moved', '', {'from': cur['col'], 'to': col})
        if assignee is not None and assignee != (cur.get('assignee') or ''):
            self.update(actor, card_id, {'assignee': assignee}, people=people)
        return self.card(card_id)

    def comment(self, actor: str, card_id: int, body) -> Dict:
        self.card(card_id)
        text = C.clean_text(body, C.MAX_COMMENT)
        if not text:
            raise C.Invalid('Write something first.')
        self.s.add_event(card_id, actor, 'comment', text)
        self.s.set_watcher(card_id, actor, True)
        return self.card(card_id)

    def watch(self, actor: str, card_id: int, username: str, on: bool,
              people: Optional[Iterable[str]] = None) -> Dict:
        self.card(card_id)
        u = C.clean_text(username, 80)
        if not u or (people is not None and u not in set(people)):
            raise C.Invalid(f'Unknown person "{u}".')
        self.s.set_watcher(card_id, u, bool(on))
        self.s.add_event(card_id, actor, 'watcher_added' if on else 'watcher_removed', '', {'who': u})
        return self.card(card_id)

    def archive(self, actor: str, card_id: int) -> None:
        self.card(card_id)
        self.s.update_card(int(card_id), {'archived': True})
        self.s.add_event(card_id, actor, 'archived')

    # ---------------------------------------------------------- members
    def members(self) -> List[Dict]:
        return self.s.members()

    def set_member(self, actor: str, username: str, role: Optional[str],
                   people: Optional[Iterable[str]] = None) -> List[Dict]:
        u = C.clean_text(username, 80)
        if not u or (people is not None and u not in set(people)):
            raise C.Invalid(f'Unknown person "{u}".')
        if role is not None and role not in C.MEMBER_ROLES:
            raise C.Invalid(f'Role must be one of {", ".join(C.MEMBER_ROLES)}.')
        self.s.set_member(u, role, actor)
        return self.s.members()

    def role_of(self, username: str) -> Optional[str]:
        for m in self.s.members():
            if m['username'] == username:
                return m['role']
        return None

    # ------------------------------------------------------------- seed
    def seed(self, actor: str) -> int:
        """Load the starter backlog into an EMPTY board. Returns cards added
        (0 when the board already has any card, archived ones included)."""
        if self.s.count_all() > 0:
            return 0
        n = 0
        for item in C.STARTER:
            self.create(actor, dict(item))
            n += 1
        return n


def decorate(cards: List[Dict], now: Optional[dt.datetime] = None) -> List[Dict]:
    """Add what the page shows but does not store: number, days in column,
    aging colour, overdue flag."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(dt.timezone(dt.timedelta(hours=-6))).date()
    out = []
    for c in cards:
        r = dict(c)
        r['number'] = C.card_number(c['id'])
        r['days'] = C.days_in_column(c.get('col_since'), now)
        r['aging'] = C.aging(c.get('col'), r['days'])
        r['overdue'] = c.get('col') != 'done' and C.overdue(c.get('due'), today)
        out.append(r)
    return out
