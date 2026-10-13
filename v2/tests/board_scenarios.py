"""v325 - one behaviour list for the Solar team board, run against the
in-memory store (tests/unit/test_board_service.py) and against a real
PostgreSQL through the production psql path (tests/portal/test_board_pg.py).
Each scenario takes a fresh ``Board``."""
from __future__ import annotations

import pytest

from argia.board import core as C
from argia.board.service import NotFound

PEOPLE = ['tomasz', 'arturo', 'vit']
NASTY = "O'Brien; DROP TABLE board_card; -- \\n :foo :'bar' \\copy 100% ñandú \U0001F31E"


def s_create_and_list(b):
    a = b.create('tomasz', {'title': 'First', 'col': 'todo', 'assignee': 'arturo', 'process': 'p2'}, PEOPLE)
    z = b.create('tomasz', {'title': 'Second', 'col': 'todo'}, PEOPLE)
    assert a['col'] == 'todo' and a['process'] == 'P2' and a['priority'] == 'normal'
    assert z['position'] > a['position']
    assert [c['title'] for c in b.cards()] == ['First', 'Second']
    assert a['watchers'] == ['arturo', 'tomasz']
    assert [e['kind'] for e in b.history(a['id'])] == ['created']


def s_text_survives_quoting(b):
    c = b.create('tomasz', {'title': NASTY, 'description': NASTY + '\n2nd line'}, PEOPLE)
    got = b.card(c['id'])
    assert got['title'] == NASTY[:C.MAX_TITLE] and got['description'].endswith('2nd line')
    b.comment('tomasz', c['id'], NASTY)
    assert b.history(c['id'])[-1]['body'] == NASTY
    assert len(b.cards()) == 1          # the table is still there


def s_edit_records_events(b):
    c = b.create('tomasz', {'title': 'Edit me'}, PEOPLE)
    b.update('tomasz', c['id'], {'title': 'Edited', 'priority': 'high', 'due': '2026-10-20'}, PEOPLE)
    b.update('tomasz', c['id'], {'assignee': 'vit'}, PEOPLE)
    b.update('tomasz', c['id'], {'title': 'Edited'}, PEOPLE)          # no change -> no event
    got = b.card(c['id'])
    assert (got['title'], got['priority'], got['due'], got['assignee']) == ('Edited', 'high', '2026-10-20', 'vit')
    kinds = [e['kind'] for e in b.history(c['id'])]
    assert kinds == ['created', 'edited', 'assigned']
    assert b.history(c['id'])[1]['meta'] == {'fields': ['due', 'priority', 'title']}
    assert 'vit' in got['watchers']
    b.update('tomasz', c['id'], {'due': ''}, PEOPLE)
    assert b.card(c['id'])['due'] is None


def s_blocked_needs_reason(b):
    c = b.create('tomasz', {'title': 'Blocky'}, PEOPLE)
    with pytest.raises(C.Invalid):
        b.update('tomasz', c['id'], {'blocked': True}, PEOPLE)
    b.update('tomasz', c['id'], {'blocked': True, 'blocked_reason': 'Waiting for CFE'}, PEOPLE)
    got = b.card(c['id'])
    assert got['blocked'] is True and got['blocked_reason'] == 'Waiting for CFE'
    b.update('tomasz', c['id'], {'blocked': True}, PEOPLE)              # reason kept, no event
    b.update('tomasz', c['id'], {'blocked': False}, PEOPLE)
    got = b.card(c['id'])
    assert got['blocked'] is False and got['blocked_reason'] == ''
    ev = b.history(c['id'])
    assert [e['kind'] for e in ev] == ['created', 'blocked', 'unblocked'] and ev[1]['body'] == 'Waiting for CFE'


def s_move_and_reorder(b):
    x = b.create('tomasz', {'title': 'X', 'col': 'todo'}, PEOPLE)
    y = b.create('tomasz', {'title': 'Y', 'col': 'doing'}, PEOPLE)
    z = b.create('tomasz', {'title': 'Z', 'col': 'doing'}, PEOPLE)
    b.move('tomasz', x['id'], 'doing', [z['id'], x['id'], y['id']])
    doing = [c['title'] for c in b.cards() if c['col'] == 'doing']
    assert doing == ['Z', 'X', 'Y']
    assert [e['kind'] for e in b.history(x['id'])] == ['created', 'moved']
    assert b.history(x['id'])[1]['meta'] == {'from': 'todo', 'to': 'doing'}
    # reorder inside the same column: no 'moved' event
    b.move('tomasz', x['id'], 'doing', [x['id'], z['id'], y['id']])
    assert [c['title'] for c in b.cards() if c['col'] == 'doing'] == ['X', 'Z', 'Y']
    assert len(b.history(x['id'])) == 2
    # to done: done_at set; back out: cleared
    b.move('tomasz', y['id'], 'done', [y['id']])
    assert b.card(y['id'])['done_at']
    b.move('tomasz', y['id'], 'review', [y['id']])
    assert b.card(y['id'])['done_at'] is None
    # a drop into another person's lane also reassigns
    b.move('tomasz', z['id'], 'review', [y['id'], z['id']], assignee='arturo', people=PEOPLE)
    got = b.card(z['id'])
    assert got['col'] == 'review' and got['assignee'] == 'arturo'
    # ids from other columns in the order list are ignored, not moved
    b.move('tomasz', x['id'], 'doing', [y['id'], x['id']])
    assert b.card(y['id'])['col'] == 'review'


def s_bad_moves_refused(b):
    c = b.create('tomasz', {'title': 'C'}, PEOPLE)
    for col, order in (('nowhere', [c['id']]), ('todo', []), ('todo', [c['id'], c['id']]), ('todo', ['x'])):
        with pytest.raises(C.Invalid):
            b.move('tomasz', c['id'], col, order)
    with pytest.raises(NotFound):
        b.move('tomasz', 99999, 'todo', [99999])


def s_comments_watchers_archive(b):
    c = b.create('tomasz', {'title': 'Talk'}, PEOPLE)
    with pytest.raises(C.Invalid):
        b.comment('arturo', c['id'], '   ')
    b.comment('arturo', c['id'], 'On it')
    got = b.card(c['id'])
    assert got['comments'] == 1 and 'arturo' in got['watchers']
    b.watch('tomasz', c['id'], 'vit', True, PEOPLE)
    assert 'vit' in b.card(c['id'])['watchers']
    b.watch('tomasz', c['id'], 'vit', False, PEOPLE)
    assert 'vit' not in b.card(c['id'])['watchers']
    with pytest.raises(C.Invalid):
        b.watch('tomasz', c['id'], 'stranger', True, PEOPLE)
    b.archive('tomasz', c['id'])
    assert b.cards() == []
    with pytest.raises(NotFound):
        b.card(c['id'])


def s_members(b):
    assert b.members() == []
    b.set_member('tomasz', 'arturo', 'editor', PEOPLE)
    b.set_member('tomasz', 'vit', 'viewer', PEOPLE)
    assert b.role_of('arturo') == 'editor' and b.role_of('vit') == 'viewer' and b.role_of('x') is None
    b.set_member('tomasz', 'vit', 'editor', PEOPLE)
    assert b.role_of('vit') == 'editor'
    b.set_member('tomasz', 'vit', None, PEOPLE)
    assert [m['username'] for m in b.members()] == ['arturo']
    with pytest.raises(C.Invalid):
        b.set_member('tomasz', 'arturo', 'boss', PEOPLE)
    with pytest.raises(C.Invalid):
        b.set_member('tomasz', 'stranger', 'viewer', PEOPLE)


def s_seed_once(b):
    n = b.seed('tomasz')
    assert n == len(C.STARTER) and len(b.cards()) == n
    assert b.seed('tomasz') == 0 and len(b.cards()) == n
    assert {c['col'] for c in b.cards()} <= set(C.COL_KEYS)


SCENARIOS = [s_create_and_list, s_text_survives_quoting, s_edit_records_events, s_blocked_needs_reason,
             s_move_and_reorder, s_bad_moves_refused, s_comments_watchers_archive, s_members, s_seed_once]
