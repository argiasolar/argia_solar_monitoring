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
