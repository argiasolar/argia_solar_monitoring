"""Shared test helpers."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(*path_parts: str) -> dict:
    """Load a JSON fixture from tests/fixtures/."""
    path = FIXTURES_DIR.joinpath(*path_parts)
    return json.loads(path.read_text(encoding="utf-8"))
