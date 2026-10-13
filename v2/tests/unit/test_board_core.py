"""v325 - pure rules of the Solar team board."""
from __future__ import annotations

import datetime as dt

import pytest

from argia.board import core as C


def test_roles():
    assert C.can('viewer', 'read') and C.can('viewer', 'comment') and C.can('viewer', 'watch')
    assert not C.can('viewer', 'create') and not C.can('viewer', 'move') and not C.can('viewer', 'edit')
    assert C.can('editor', 'move') and C.can('editor', 'archive') and not C.can('editor', 'members')
    assert C.can('owner', 'members') and C.can('owner', 'seed')
    assert not C.can(None, 'read') and not C.can('stranger', 'read')


def test_validate_full_and_partial():
    f = C.validate({'title': '  Hello \r\n world  ', 'process': 'e2', 'priority': 'HIGH', 'due': '2026-10-20'})
    assert f['title'] == 'Hello \n world' and f['process'] == 'E2' and f['priority'] == 'high'
    assert f['due'] == '2026-10-20' and f['blocked'] is False and f['blocked_reason'] == ''
    assert C.validate({'priority': 'low'}, partial=True) == {'priority': 'low'}
    assert C.validate({'title': 'x' * 500})['title'] == 'x' * C.MAX_TITLE
    assert C.validate({'title': 'a' + chr(0) + 'b'})['title'] == 'ab'


@pytest.mark.parametrize("bad", [
    {'title': ''}, {'title': '   '}, {'title': 'x', 'process': 'P99'}, {'title': 'x', 'priority': 'urgent'},
    {'title': 'x', 'due': '13/10/2026'}, {'title': 'x', 'blocked': True}, {'title': 'x', 'assignee': 'ghost'},
])
def test_validate_refuses(bad):
    with pytest.raises(C.Invalid):
        C.validate(bad, people=['tomasz'])
    with pytest.raises(C.Invalid):
        C.validate('not a dict')


def test_unblock_clears_reason():
    assert C.validate({'blocked': 'false', 'blocked_reason': 'x'}, partial=True) == {'blocked': False, 'blocked_reason': ''}
    assert C.validate({'blocked': 'on', 'blocked_reason': 'CFE'}, partial=True)['blocked'] is True


def test_columns_and_order():
    assert C.COL_KEYS == ['backlog', 'todo', 'doing', 'review', 'done']
    assert C.validate_col(' Doing ') == 'doing'
    with pytest.raises(C.Invalid):
        C.validate_col('later')
    assert C.validate_order([3, '1', 2], 1) == [3, 1, 2]
    for bad in ([3, 2], [1, 1], 'x', [True], [0, 1], list(range(1, C.MAX_ORDER + 2))):
        with pytest.raises(C.Invalid):
            C.validate_order(bad, 1)


def test_aging_thresholds():
    assert C.aging('doing', 4) == '' and C.aging('doing', 5) == 'amber' and C.aging('doing', 10) == 'red'
    assert C.aging('review', 3) == 'amber' and C.aging('todo', 14) == 'red'
    assert C.aging('backlog', 400) == '' and C.aging('done', 400) == ''


def test_days_in_column_and_timestamps():
    now = dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)
    assert C.days_in_column('2026-10-10T11:00:00+00:00', now) == 3
    assert C.days_in_column('2026-10-10 11:00:00.123+00', now) == 3
    assert C.days_in_column(None, now) == 0 and C.days_in_column('garbage', now) == 0
    assert C.days_in_column('2026-10-20T00:00:00+00:00', now) == 0


def test_wip_counts_only_in_progress():
    cards = [{'col': 'doing', 'assignee': 'a'}] * 4 + [{'col': 'doing', 'assignee': ''}] * 5 + \
            [{'col': 'todo', 'assignee': 'b'}] * 9 + [{'col': 'doing', 'assignee': 'b', 'archived': True}] * 9
    assert C.wip(cards) == {'a': 4, '': 5}
    assert C.over_wip(cards) == {'a'}


def test_overdue_and_number():
    today = dt.date(2026, 10, 13)
    assert C.overdue('2026-10-12', today) and not C.overdue('2026-10-13', today)
    assert not C.overdue(None, today) and not C.overdue('bad', today)
    assert C.card_number(5) == 'SB-0005'


def test_processes_match_the_process_map():
    assert C.PROCESS_KEYS == ['P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8', 'P9', 'M1', 'E1', 'E2', 'E3', 'E4', 'E5']
