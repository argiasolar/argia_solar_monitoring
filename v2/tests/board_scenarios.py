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


def s_attachments(b):
    import os
    import tempfile
    d = tempfile.mkdtemp(prefix="board_files_")
    c = b.create('tomasz', {'title': 'With files'}, PEOPLE)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    got = b.attach('arturo', c['id'], [("screenshot 13/10.png", png), ("../../etc/notes.pdf", b"%PDF-1.4 x")], d, 'See screenshot')
    assert got['files'] == 2 and got['comments'] == 1 and 'arturo' in got['watchers']
    fl = b.files(c['id'])
    assert [f['filename'] for f in fl] == ['10.png', 'notes.pdf'] and 'stored_as' not in fl[0]
    ev = b.history(c['id'])[-1]
    assert ev['kind'] == 'comment' and ev['meta'] == {'files': ['10.png', 'notes.pdf']} and fl[0]['event_id'] == ev['id']
    row, path = b.file(c['id'], fl[0]['id'], d)
    assert open(path, 'rb').read() == png and os.path.dirname(path) == os.path.join(d, str(c['id']))
    if os.name != "nt":                    # v327: Windows has no POSIX modes (laptop gate)
        assert oct(os.stat(path).st_mode & 0o777) == '0o600'
    # files alone -> an 'attachment' event
    b.attach('tomasz', c['id'], [("plan.xlsx", b"PK\x03\x04data")], d)
    assert b.history(c['id'])[-1]['kind'] == 'attachment' and b.card(c['id'])['files'] == 3
    # refused before anything is written: one bad file stops the whole post
    for bad in ([("x.exe", b"MZ..")], [("report.pdf", b"MZ\x90\x00")], [("a.svg", b"<svg/>")], [("e.png", b"")],
                [("big.png", b"0" * (C.MAX_FILE + 1))], [("ok.png", png)] * (C.MAX_FILES_PER_POST + 1), []):
        with pytest.raises(C.Invalid):
            b.attach('tomasz', c['id'], bad, d, '')
    assert b.card(c['id'])['files'] == 3 and len(os.listdir(os.path.join(d, str(c['id'])))) == 3
    with pytest.raises(NotFound):
        b.file(c['id'], 99999, d)
    b.archive('tomasz', c['id'])
    with pytest.raises(NotFound):
        b.file(c['id'], fl[0]['id'], d)


def s_parent_and_children(b):
    """v367: one level of parents - progress, events on both sides, filters by id."""
    eng = b.create('tomasz', {'title': 'engine.argia.com.mx', 'col': 'backlog'}, PEOPLE)
    k1 = b.create('tomasz', {'title': 'Feature A', 'col': 'doing', 'parent_id': eng['id']}, PEOPLE)
    k2 = b.create('tomasz', {'title': 'Feature B', 'col': 'todo', 'parent_id': 'SB-%04d' % eng['id']}, PEOPLE)
    loose = b.create('tomasz', {'title': 'Loose'}, PEOPLE)
    assert k1['parent_id'] == eng['id'] and k2['parent_id'] == eng['id'] and loose['parent_id'] is None
    got = b.card(eng['id'])
    assert (got['kids_total'], got['kids_done']) == (2, 0)
    b.move('tomasz', k1['id'], 'done', [k1['id']])
    assert (b.card(eng['id'])['kids_total'], b.card(eng['id'])['kids_done']) == (2, 1)
    # an existing card goes under the parent through an edit, and out again
    b.update('tomasz', loose['id'], {'parent_id': eng['id']}, PEOPLE)
    assert b.card(eng['id'])['kids_total'] == 3
    ev = b.history(loose['id'])[-1]
    assert ev['kind'] == 'parent' and ev['meta'] == {'from': None, 'to': eng['id']}
    b.update('tomasz', loose['id'], {'parent_id': ''}, PEOPLE)
    assert b.card(loose['id'])['parent_id'] is None and b.card(eng['id'])['kids_total'] == 2
    kinds = [e['kind'] for e in b.history(eng['id'])]
    assert kinds == ['created', 'child_added', 'child_added', 'child_added', 'child_removed']
    rel = b.related(eng['id'])
    assert [x['title'] for x in rel['children']] == ['Feature A', 'Feature B'] and rel['parent'] is None
    assert b.related(k2['id'])['parent']['number'] == 'SB-%04d' % eng['id']
    # an unrelated edit does not touch the parent
    b.update('tomasz', k2['id'], {'title': 'Feature B2'}, PEOPLE)
    assert b.card(k2['id'])['parent_id'] == eng['id']


def s_parent_one_level_only(b):
    top = b.create('tomasz', {'title': 'Top'}, PEOPLE)
    kid = b.create('tomasz', {'title': 'Kid', 'parent_id': top['id']}, PEOPLE)
    other = b.create('tomasz', {'title': 'Other'}, PEOPLE)
    for bad in ({'parent_id': kid['id']},            # under a card that is itself under a parent
                {'parent_id': 99999},                # does not exist
                {'parent_id': 'abc'}, {'parent_id': True}):
        with pytest.raises(C.Invalid):
            b.create('tomasz', dict(bad, title='x'), PEOPLE)
    with pytest.raises(C.Invalid):
        b.update('tomasz', other['id'], {'parent_id': kid['id']}, PEOPLE)
    with pytest.raises(C.Invalid):                   # a parent cannot go under another card
        b.update('tomasz', top['id'], {'parent_id': other['id']}, PEOPLE)
    with pytest.raises(C.Invalid):                   # nor under itself
        b.update('tomasz', other['id'], {'parent_id': other['id']}, PEOPLE)
    assert b.card(top['id'])['parent_id'] is None and b.card(other['id'])['parent_id'] is None
    # archiving the parent frees its cards (they stay on the board)
    b.archive('tomasz', top['id'])
    got = b.card(kid['id'])
    assert got['parent_id'] is None
    ev = b.history(kid['id'])[-1]
    assert ev['kind'] == 'parent' and ev['body'] == 'parent archived' and ev['meta'] == {'from': top['id'], 'to': None}
    with pytest.raises(C.Invalid):                   # an archived card is not a parent any more
        b.update('tomasz', other['id'], {'parent_id': top['id']}, PEOPLE)


def s_unblock_card_and_auto_unblock(b):
    """v367: blocked -> an unblocking card in This week; its Done unblocks the card."""
    top = b.create('tomasz', {'title': 'Topic'}, PEOPLE)
    c = b.create('tomasz', {'title': 'Install inverter', 'col': 'doing', 'assignee': 'arturo',
                            'process': 'P6', 'parent_id': top['id']}, PEOPLE)
    with pytest.raises(C.Invalid):                   # not blocked yet
        b.create_unblocker('tomasz', c['id'], {}, PEOPLE)
    b.update('tomasz', c['id'], {'blocked': True, 'blocked_reason': 'CFE permit missing'}, PEOPLE)
    u = b.create_unblocker('tomasz', c['id'], {'assignee': 'vit', 'due': '2026-11-20'}, PEOPLE)
    assert u['title'] == 'Unblock: Install inverter' and u['col'] == 'todo' and u['priority'] == 'high'
    assert u['unblocks_id'] == c['id'] and u['assignee'] == 'vit' and u['due'] == '2026-11-20'
    assert u['process'] == 'P6' and u['parent_id'] == top['id']          # stays in the same topic
    assert u['description'].startswith('CFE permit missing') and ('SB-%04d' % c['id']) in u['description']
    assert {'tomasz', 'vit', 'arturo'} <= set(u['watchers'])
    assert b.card(c['id'])['unblock_open'] == 1
    assert b.history(c['id'])[-1]['kind'] == 'unblock_task' and b.history(c['id'])[-1]['meta'] == {'card': u['id']}
    assert b.history(u['id'])[0]['meta'] == {'col': 'todo', 'parent': top['id'], 'unblocks': c['id']}
    rel = b.related(c['id'])
    assert [x['id'] for x in rel['unblockers']] == [u['id']] and b.related(u['id'])['unblocks']['id'] == c['id']
    # a second unblocking card: the first Done does not unblock yet
    u2 = b.create_unblocker('tomasz', c['id'], {'title': 'Call CFE'}, PEOPLE)
    assert u2['title'] == 'Call CFE'
    b.move('vit', u['id'], 'done', [u['id']])
    assert b.card(c['id'])['blocked'] is True and b.card(c['id'])['unblock_open'] == 1
    b.move('vit', u2['id'], 'review', [u2['id']])
    assert b.card(c['id'])['blocked'] is True
    b.move('vit', u2['id'], 'done', [u2['id']])
    got = b.card(c['id'])
    assert got['blocked'] is False and got['blocked_reason'] == ''
    ev = b.history(c['id'])[-1]
    assert ev['kind'] == 'unblocked' and ev['actor'] == 'vit' and ev['meta'] == {'auto': True, 'by': u2['id']}
    # a card unblocked by hand first is left alone when its unblocking card finishes
    d = b.create('tomasz', {'title': 'D'}, PEOPLE)
    b.update('tomasz', d['id'], {'blocked': True, 'blocked_reason': 'r'}, PEOPLE)
    ud = b.create_unblocker('tomasz', d['id'], {}, PEOPLE)
    b.update('tomasz', d['id'], {'blocked': False}, PEOPLE)
    n = len(b.history(d['id']))
    b.move('tomasz', ud['id'], 'done', [ud['id']])
    assert len(b.history(d['id'])) == n and b.card(d['id'])['blocked'] is False
    # an unblocking card for a parent card goes under that parent
    p2 = b.create('tomasz', {'title': 'P2'}, PEOPLE)
    b.create('tomasz', {'title': 'k', 'parent_id': p2['id']}, PEOPLE)
    b.update('tomasz', p2['id'], {'blocked': True, 'blocked_reason': 'budget'}, PEOPLE)
    assert b.create_unblocker('tomasz', p2['id'], {}, PEOPLE)['parent_id'] == p2['id']


SCENARIOS = [s_create_and_list, s_text_survives_quoting, s_edit_records_events, s_blocked_needs_reason,
             s_move_and_reorder, s_bad_moves_refused, s_comments_watchers_archive, s_members, s_seed_once,
             s_attachments, s_parent_and_children, s_parent_one_level_only, s_unblock_card_and_auto_unblock]
