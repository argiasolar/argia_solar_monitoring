"""v325 - the board scenarios against a real PostgreSQL, through the exact
command the server runs (pgq: runuser -u postgres -- psql; the conftest puts
a test-double runuser on PATH)."""
from __future__ import annotations

import os

import pytest

from argia.board.service import Board
from argia.board.store import PgStore
from tests.board_scenarios import SCENARIOS
from tests.portal.conftest import reset_db


@pytest.fixture
def board(pg_env, monkeypatch):
    reset_db(pg_env)
    for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH"):
        monkeypatch.setenv(k, pg_env[k])
    st = PgStore()
    st.ensure()
    st.ensure()                       # idempotent
    return Board(st)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
def test_scenario_on_postgresql(board, scenario):
    scenario(board)


def test_schema_refuses_bad_values_even_past_the_app(board):
    import subprocess
    r = subprocess.run(["psql", "-d", "argia_mont", "-v", "ON_ERROR_STOP=1", "-c",
                        "INSERT INTO board_card (title, col, created_by) VALUES ('x', 'nowhere', 't');"],
                       env=dict(os.environ), capture_output=True, text=True)
    assert r.returncode != 0 and "board_card_col_check" in r.stderr


def test_v367_migration_keeps_the_cards_of_an_existing_board(pg_env, monkeypatch):
    """The live board was created by the v325 schema: the new columns are added
    in place (ALTER ... IF NOT EXISTS), the existing cards keep everything and
    get no parent; the new rules work on them right away."""
    reset_db(pg_env)
    for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH"):
        monkeypatch.setenv(k, pg_env[k])
    old_sql = V325_SCHEMA
    assert "parent_id" not in old_sql
    run = PgStore()._run
    run(old_sql)
    run("INSERT INTO board_card (title, col, created_by, blocked, blocked_reason) VALUES ('Old card', 'doing', 'tomasz', true, 'CFE');")
    st = PgStore()
    st.ensure()
    st.ensure()
    b = Board(st)
    old_card = b.cards()[0]
    assert old_card['title'] == 'Old card' and old_card['parent_id'] is None and old_card['unblocks_id'] is None
    assert (old_card['kids_total'], old_card['unblock_open']) == (0, 0)
    u = b.create_unblocker('tomasz', old_card['id'], {}, ['tomasz'])
    b.move('tomasz', u['id'], 'done', [u['id']])
    assert b.card(old_card['id'])['blocked'] is False


# the board schema as v325-v366 created it on pio06 (argia/board/store.py at 92d0834)
V325_SCHEMA = """CREATE TABLE IF NOT EXISTS board_card (
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
CREATE TABLE IF NOT EXISTS board_file (
    id          serial PRIMARY KEY,
    card_id     int  NOT NULL REFERENCES board_card(id) ON DELETE CASCADE,
    event_id    int  REFERENCES board_event(id) ON DELETE SET NULL,
    filename    text NOT NULL,
    stored_as   text NOT NULL,
    size        int  NOT NULL,
    mime        text NOT NULL,
    uploaded_by text NOT NULL DEFAULT '',
    ts          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_board_file_card ON board_file (card_id);
CREATE TABLE IF NOT EXISTS board_member (
    username text PRIMARY KEY,
    role     text NOT NULL CHECK (role IN ('editor','viewer')),
    added_by text NOT NULL DEFAULT '',
    added_at timestamptz NOT NULL DEFAULT now()
);
"""
