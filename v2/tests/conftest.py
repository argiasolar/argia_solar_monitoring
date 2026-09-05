"""Shared test helpers."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(*path_parts: str) -> dict:
    """Load a JSON fixture from tests/fixtures/."""
    path = FIXTURES_DIR.joinpath(*path_parts)
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- v205
# The code defaults are PostgreSQL now (every ARGIA_*_SOURCE = pg,
# ARGIA_KPI_WRITE = pg, sheet writes off). The sheet branches of the
# doors still exist until v206 deletes them, and ~170 legacy tests drive
# the sheet parsers through them with FakeSheets objects. Those tests run
# with the switches pinned to the legacy value; tests that assert the
# DEFAULT pass an explicit env dict or delete the variables themselves.
import os as _os

import pytest as _pytest

_LEGACY_SHEET_ENV = {
    "ARGIA_TELEMETRY_SOURCE": "sheet", "ARGIA_KPI_SOURCE": "sheet",
    "ARGIA_FINANCE_SOURCE": "sheet", "ARGIA_INVOICING_SOURCE": "sheet",
    "ARGIA_ALERTS_SOURCE": "sheet", "ARGIA_DASHBOARD_SOURCE": "sheet",
    "ARGIA_CONFIG_SOURCE": "sheet", "ARGIA_KPI_WRITE": "sheet",
    "ARGIA_SHEET_TELEMETRY": "1", "ARGIA_SHEET_OUTBOX": "1",
}


@_pytest.fixture(autouse=True)
def _legacy_sheet_switches(monkeypatch):
    for k, v in _LEGACY_SHEET_ENV.items():
        if k not in _os.environ:
            monkeypatch.setenv(k, v)
    yield
