"""v325 - the board operations on the in-memory store (the same scenario
list runs on PostgreSQL in tests/portal/test_board_pg.py)."""
from __future__ import annotations

import datetime as dt

import pytest

from argia.board import core as C
from argia.board.service import Board, decorate
from argia.board.store import MemStore
from tests.board_scenarios import SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
def test_scenario_in_memory(scenario):
    scenario(Board(MemStore()))


def test_done_cards_leave_the_board_after_the_window():
    st = MemStore()
    now = [dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)]
    st.clock = lambda: now[0].isoformat()
    b = Board(st)
    c = b.create('tomasz', {'title': 'Old', 'col': 'doing'})
    b.move('tomasz', c['id'], 'done', [c['id']])
    now[0] += dt.timedelta(days=13)
    assert len(b.cards()) == 1
    now[0] += dt.timedelta(days=2)
    assert b.cards() == []
    assert b.card(c['id'])['col'] == 'done'          # still there, just off the board


def test_decorate_adds_number_aging_overdue():
    now = dt.datetime(2026, 10, 13, 18, tzinfo=dt.timezone.utc)
    cards = [{'id': 7, 'col': 'doing', 'col_since': (now - dt.timedelta(days=6)).isoformat(), 'due': '2026-10-12'},
             {'id': 8, 'col': 'done', 'col_since': now.isoformat(), 'due': '2026-10-01'},
             {'id': 9, 'col': 'review', 'col_since': '2026-10-13 10:00:00+00', 'due': None}]
    d = decorate(cards, now)
    assert [x['number'] for x in d] == ['SB-0007', 'SB-0008', 'SB-0009']
    assert (d[0]['days'], d[0]['aging'], d[0]['overdue']) == (6, 'amber', True)
    assert d[1]['overdue'] is False                       # done cards are never overdue
    assert (d[2]['days'], d[2]['aging']) == (0, '')


def test_seed_cards_are_valid():
    for item in C.STARTER:
        f = C.validate(dict(item))
        assert f['title'] and item['col'] in C.COL_KEYS and f['process'] in C.PROCESS_KEYS
