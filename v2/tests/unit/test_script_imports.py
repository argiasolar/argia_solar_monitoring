"""Script import-integrity regression.

Born 2026-07-03: a silent edit left ``production_statement`` out of
``scripts/kpi_eod.py``'s imports. ``py_compile`` passed (NameError is a
runtime error), no unit test executes the script's main loop, and the live
run crashed. This module closes the class: for every operational script,
every global name referenced anywhere in its code must resolve in the
module namespace or builtins — a missing import fails HERE, at test time.
"""

from __future__ import annotations

import builtins
import dis
import importlib
import pytest

SCRIPTS = [
    "scripts.kpi_eod",
    "scripts.alerts_daily",
    "scripts.alerts_snapshot",
    "scripts.archive_month",
    "scripts.archive_preflight",
    "scripts.report_daily",
]


def _referenced_globals(module) -> set:
    """Every LOAD_GLOBAL name used by any function in the module."""
    names = set()
    for obj in vars(module).values():
        code = getattr(obj, "__code__", None)
        if code is None or getattr(obj, "__module__", None) != module.__name__:
            continue
        stack = [code]
        while stack:
            c = stack.pop()
            for ins in dis.get_instructions(c):
                if ins.opname == "LOAD_GLOBAL":
                    names.add(ins.argval)
            stack.extend(k for k in c.co_consts if hasattr(k, "co_code"))
    return names


@pytest.mark.parametrize("modname", SCRIPTS)
def test_every_referenced_global_resolves(modname):
    module = importlib.import_module(modname)
    missing = sorted(
        n for n in _referenced_globals(module)
        if not hasattr(module, n) and not hasattr(builtins, n)
    )
    assert not missing, (
        f"{modname} references undefined global(s): {missing} — "
        f"almost certainly a missing import")
