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

    def related(self, card_id: int) -> Dict:
        """v367: the parent, the cards under it, the card it unblocks and
        the cards that unblock it - what the side panel lists."""
        c = self.card(card_id)

        def brief(x: Optional[Dict]) -> Optional[Dict]:
            if not x or x.get('archived'):
                return None
            return {'id': x['id'], 'number': C.card_number(x['id']), 'title': x['title'], 'col': x['col'],
                    'assignee': x.get('assignee') or '', 'blocked': bool(x.get('blocked'))}
        par = self.s.get_card(int(c['parent_id'])) if c.get('parent_id') else None
        unb = self.s.get_card(int(c['unblocks_id'])) if c.get('unblocks_id') else None
        return {'parent': brief(par), 'unblocks': brief(unb),
                'children': [brief(k) for k in self.s.children(int(card_id))],
                'unblockers': [brief(k) for k in self.s.unblockers(int(card_id))]}

    # ---------------------------------------------------------- parents
    def _check_parent(self, card: Optional[Dict], parent_id: Optional[int]) -> None:
        """One level: the parent is a live top-level card, not the card
        itself, and a card that has cards under it stays top-level."""
        if parent_id is None:
            return
        p = self.s.get_card(int(parent_id))
        if p is None or p.get('archived'):
            raise C.Invalid(f'Parent {C.card_number(parent_id)} does not exist.')
        if card is not None and int(p['id']) == int(card['id']):
            raise C.Invalid('A card cannot be its own parent.')
        if p.get('parent_id'):
            raise C.Invalid(f'{C.card_number(p["id"])} is itself under {C.card_number(p["parent_id"])}:'
                            ' only one level of parents.')
        if card is not None and int(card.get('kids_total') or 0) > 0:
            raise C.Invalid('This card has cards under it, so it cannot go under another card.')

    def _parent_events(self, actor: str, card_id: int, old: Optional[int], new: Optional[int], why: str = '') -> None:
        self.s.add_event(card_id, actor, 'parent', why, {'from': old, 'to': new})
        if old:
            self.s.add_event(int(old), actor, 'child_removed', why, {'child': int(card_id)})
        if new:
            self.s.add_event(int(new), actor, 'child_added', '', {'child': int(card_id)})

    # ----------------------------------------------------------- writes
    def create(self, actor: str, data: Dict, people: Optional[Iterable[str]] = None,
               unblocks_id: Optional[int] = None) -> Dict:
        f = C.validate(data, partial=False, people=people)
        f['col'] = C.validate_col(data.get('col') or 'backlog')
        self._check_parent(None, f.get('parent_id'))
        if unblocks_id is not None:
            f['unblocks_id'] = int(unblocks_id)
        pos = self.s.max_position(f['col']) + 1
        cid = self.s.insert_card(f, actor, pos)
        meta = {'col': f['col']}
        if f.get('parent_id'):
            meta['parent'] = f['parent_id']
        if unblocks_id is not None:
            meta['unblocks'] = int(unblocks_id)
        self.s.add_event(cid, actor, 'created', '', meta)
        if f.get('parent_id'):
            self.s.add_event(int(f['parent_id']), actor, 'child_added', '', {'child': cid})
        self.s.set_watcher(cid, actor, True)
        if f.get('assignee'):
            self.s.set_watcher(cid, f['assignee'], True)
        return self.card(cid)

    def create_unblocker(self, actor: str, card_id: int, data: Dict,
                         people: Optional[Iterable[str]] = None) -> Dict:
        """v367: a card whose job is to unblock ``card_id``: in This week,
        high priority, the blocked reason as its description. When the last
        open one reaches Done the blocked card is unblocked (``move``)."""
        cur = self.card(card_id)
        if not cur.get('blocked'):
            raise C.Invalid('Mark the card blocked first, with the reason.')
        data = dict(data or {})
        par = cur.get('parent_id') or (cur['id'] if int(cur.get('kids_total') or 0) > 0 else None)
        num = C.card_number(cur['id'])
        body = {'title': C.clean_text(data.get('title'), C.MAX_TITLE) or C.unblock_title(cur['title']),
                'description': f"{cur.get('blocked_reason') or ''}\n\nUnblocks {num}: {cur['title']}".strip(),
                'assignee': data.get('assignee') or '', 'due': data.get('due') or None,
                'priority': 'high', 'process': cur.get('process') or '', 'parent_id': par, 'col': 'todo'}
        new = self.create(actor, body, people, unblocks_id=int(cur['id']))
        self.s.add_event(int(cur['id']), actor, 'unblock_task', '', {'card': new['id']})
        if cur.get('assignee'):
            self.s.set_watcher(new['id'], cur['assignee'], True)
        return self.card(new['id'])

    def update(self, actor: str, card_id: int, data: Dict, people: Optional[Iterable[str]] = None) -> Dict:
        cur = self.card(card_id)
        data = dict(data or {})
        if data.get('blocked') not in (None, False, '', 'false', '0') and 'blocked_reason' not in data:
            data['blocked_reason'] = cur.get('blocked_reason') or ''
        f = C.validate(data, partial=True, people=people)
        changes = {k: v for k, v in f.items() if (cur.get(k) if k != 'due' else (cur.get('due') or None)) != v}
        if not changes:
            return cur
        if 'parent_id' in changes:
            self._check_parent(cur, changes['parent_id'])
        self.s.update_card(int(card_id), changes)
        rest = dict(changes)
        if 'assignee' in rest:
            a = rest.pop('assignee')
            self.s.add_event(card_id, actor, 'assigned', '', {'from': cur.get('assignee') or '', 'to': a})
            if a:
                self.s.set_watcher(card_id, a, True)
        if 'parent_id' in rest:
            new_parent = rest.pop('parent_id')
            self._parent_events(actor, card_id, cur.get('parent_id'), new_parent)
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
            if col == 'done' and cur.get('unblocks_id'):
                self._auto_unblock(actor, int(cur['unblocks_id']), int(card_id))
        if assignee is not None and assignee != (cur.get('assignee') or ''):
            self.update(actor, card_id, {'assignee': assignee}, people=people)
        return self.card(card_id)

    def _auto_unblock(self, actor: str, target_id: int, by_id: int) -> None:
        """v367: the blocked card is unblocked when its last open unblocking
        card reaches Done. A card unblocked or archived meanwhile is left alone."""
        t = self.s.get_card(target_id)
        if t is None or t.get('archived') or not t.get('blocked'):
            return
        if int(t.get('unblock_open') or 0) > 0:
            return
        self.s.update_card(target_id, {'blocked': False, 'blocked_reason': ''})
        self.s.add_event(target_id, actor, 'unblocked', '', {'auto': True, 'by': by_id})

    def comment(self, actor: str, card_id: int, body) -> Dict:
        self.card(card_id)
        text = C.clean_text(body, C.MAX_COMMENT)
        if not text:
            raise C.Invalid('Write something first.')
        self.s.add_event(card_id, actor, 'comment', text)
        self.s.set_watcher(card_id, actor, True)
        return self.card(card_id)

    def attach(self, actor: str, card_id: int, uploads, files_dir: str, body='') -> Dict:
        """A comment with files (or files alone). ``uploads`` = [(filename, bytes)].
        Everything is checked before anything is written; a file lands on disk
        under files_dir/<card id>/<random>.<ext> (0600) and is registered only
        after the write succeeded."""
        import os
        import uuid
        self.card(card_id)
        uploads = list(uploads or [])
        if len(uploads) > C.MAX_FILES_PER_POST:
            raise C.Invalid(f'At most {C.MAX_FILES_PER_POST} files at once.')
        checked = [(C.check_upload(name, data), data) for name, data in uploads]
        text = C.clean_text(body, C.MAX_COMMENT)
        if not checked and not text:
            raise C.Invalid('Write something or add a file first.')
        ev = self.s.add_event(card_id, actor, 'comment' if text else 'attachment', text,
                              {'files': [chk[0] for chk, _ in checked]} if checked else {})
        d = os.path.join(files_dir, str(int(card_id)))
        os.makedirs(d, mode=0o700, exist_ok=True)
        for (name, ext, mime, _inline), data in checked:
            stored = uuid.uuid4().hex + ext
            path = os.path.join(d, stored)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as fh:
                fh.write(data)
            self.s.add_file(card_id, ev, name, stored, len(data), mime, actor)
        self.s.set_watcher(card_id, actor, True)
        return self.card(card_id)

    def file(self, card_id: int, file_id: int, files_dir: str):
        """(row, absolute path) of one attachment of a live card, or NotFound."""
        import os
        self.card(card_id)
        for f in self.s.files(card_id):
            if int(f['id']) == int(file_id):
                path = os.path.join(files_dir, str(int(card_id)), os.path.basename(f['stored_as']))
                if not os.path.isfile(path):
                    raise NotFound(file_id)
                return f, path
        raise NotFound(file_id)

    def files(self, card_id: int) -> List[Dict]:
        self.card(card_id)
        return [{k: v for k, v in f.items() if k != 'stored_as'} for f in self.s.files(card_id)]

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
        for k in self.s.children(int(card_id)):        # v367: the cards under it stay, without a parent
            self.s.update_card(int(k['id']), {'parent_id': None})
            self.s.add_event(int(k['id']), actor, 'parent', 'parent archived', {'from': int(card_id), 'to': None})
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
