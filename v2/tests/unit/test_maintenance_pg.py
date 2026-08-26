"""Unit tests — maintenance events from the PostgreSQL /setup/ UI."""

import datetime as dt

from argia.core.time_utils import MX_TZ
from argia.maintenance.events import events_from_pg_rows


def _row(**kw):
    base = ["QRO1", "2026-08-26T10:00:00", "", "argia", "", "", "note", ""]
    for i, key in enumerate(["pk", "start", "end", "cat", "ct", "cost",
                             "note", "appr"]):
        if key in kw:
            base[i] = kw[key]
    return base


def test_pg_rows_become_typed_events():
    ev = events_from_pg_rows([_row(pk="gto1", cat="customer",
                                   cost="1234.50", appr="tomasz")])[0]
    assert ev.plant_key == "GTO1"
    assert ev.start_ts == dt.datetime(2026, 8, 26, 10, 0, tzinfo=MX_TZ)
    assert ev.is_ongoing and ev.approved and ev.is_billable_category
    assert ev.cost_mxn == 1234.5


def test_pg_draft_and_argia_events_are_not_billable():
    ev = events_from_pg_rows([_row()])[0]
    assert not ev.approved and not ev.is_billable_category


def test_pg_end_ts_and_bad_rows():
    ok = _row(end="2026-08-26T15:30:00")
    bad_start = _row(start="not-a-date")
    short = ["GTO1", "2026-08-26T10:00:00"]
    events = events_from_pg_rows([ok, bad_start, short, None])
    assert len(events) == 1
    assert events[0].end_ts == dt.datetime(2026, 8, 26, 15, 30,
                                           tzinfo=MX_TZ)


def test_pg_unknown_category_kept_non_billable():
    ev = events_from_pg_rows([_row(cat="typo_category", appr="x")])[0]
    assert ev.approved and not ev.is_billable_category
