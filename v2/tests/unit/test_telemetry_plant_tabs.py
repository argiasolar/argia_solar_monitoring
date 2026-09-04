"""v184: the per-plant Telemetry_<KEY> sheet mirror is off by default.

Incident this locks down (2026-08-18 and again 2026-09-03): nine wide
tabs x 143 columns x ~4.5k rows/day filled the workbook's 10,000,000-cell
hard cap. Every append then failed — including ``Telemetry_Argia``, the
ONLY telemetry tab anything reads (kpi_eod via argia.kpi.reader,
alerts_snapshot, watchdog, dashboard_update). The per-plant tabs were a
write-only mirror of data Postgres already holds, so they are now off
unless ARGIA_SHEET_PLANT_TABS says otherwise.
"""

import pathlib

import pytest

from argia.telemetry.schema import PLANT_SCHEMA, plant_tab_name
from argia.telemetry.sheets_writer import SchemaMismatchError
from scripts import telemetry_5m as t5

SRC = (pathlib.Path(t5.__file__)).read_text(encoding="utf-8")


class _Recorder:
    """Stands in for the module's two sheet-writing functions."""

    def __init__(self, boom=None):
        self.ensured = []
        self.written = []
        self.boom = boom

    def ensure(self, sheets, tab, schema):
        self.ensured.append((tab, schema))

    def write(self, sheets, tab, schema, rows, dry_run=False):
        if self.boom:
            raise self.boom
        self.written.append((tab, schema, list(rows), dry_run))
        return {"inserted": len(rows), "updated": 0, "unchanged": 0}


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(t5, "ensure_telemetry_tab", r.ensure)
    monkeypatch.setattr(t5, "write_telemetry_rows", r.write)
    return r


ROWS = [["2026-09-04T00:00:00Z", "x", "SN1"]]


class TestSwitch:
    def test_off_by_default(self):
        assert t5.plant_tabs_enabled({}) is False

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_truthy_tokens_enable(self, v):
        assert t5.plant_tabs_enabled({t5.PLANT_TABS_ENV: v}) is True

    @pytest.mark.parametrize("v", ["0", "", "no", "off", "false", "banana"])
    def test_everything_else_disables(self, v):
        assert t5.plant_tabs_enabled({t5.PLANT_TABS_ENV: v}) is False

    def test_reads_the_real_environ_by_default(self, monkeypatch):
        monkeypatch.delenv(t5.PLANT_TABS_ENV, raising=False)
        assert t5.plant_tabs_enabled() is False
        monkeypatch.setenv(t5.PLANT_TABS_ENV, "1")
        assert t5.plant_tabs_enabled() is True


class TestMirror:
    def test_disabled_writes_nothing_and_is_not_an_error(self, rec):
        assert t5._mirror_plant_tab(None, "GTO1", ROWS, enabled=False) == 0
        assert rec.ensured == [] and rec.written == []

    def test_enabled_writes_the_wide_tab(self, rec):
        assert t5._mirror_plant_tab(None, "GTO1", ROWS, enabled=True) == 0
        assert rec.ensured == [(plant_tab_name("GTO1"), PLANT_SCHEMA)]
        tab, schema, rows, dry = rec.written[0]
        assert (tab, schema, rows, dry) == (
            plant_tab_name("GTO1"), PLANT_SCHEMA, ROWS, False)

    def test_dry_run_flag_is_passed_through(self, rec):
        t5._mirror_plant_tab(None, "NL1", ROWS, dry_run=True, enabled=True)
        assert rec.written[0][3] is True

    def test_no_rows_is_a_no_op_even_when_enabled(self, rec):
        assert t5._mirror_plant_tab(None, "GTO1", [], enabled=True) == 0
        assert rec.ensured == [] and rec.written == []

    @pytest.mark.parametrize("exc", [
        SchemaMismatchError("drift"),
        RuntimeError("above the limit of 10000000 cells"),
    ])
    def test_write_failure_counts_exactly_one_error(self, monkeypatch, exc):
        r = _Recorder(boom=exc)
        monkeypatch.setattr(t5, "ensure_telemetry_tab", r.ensure)
        monkeypatch.setattr(t5, "write_telemetry_rows", r.write)
        assert t5._mirror_plant_tab(None, "GTO1", ROWS, enabled=True) == 1


class TestWiring:
    def test_all_four_vendors_go_through_the_helper(self):
        assert SRC.count("errors += _mirror_plant_tab(") == 4

    def test_the_old_inline_block_is_gone(self):
        # exactly one place may name the wide schema + tab together
        assert SRC.count("ensure_telemetry_tab(sheets, tab, PLANT_SCHEMA)") == 1
        assert SRC.count('log.error("[%s] sheet write failed: %s"') == 1

    def test_the_argia_tab_is_never_gated_by_the_switch(self):
        # Telemetry_Argia is what kpi_eod reads — it must always be written
        head, _, tail = SRC.partition("def _mirror_plant_tab")
        body = tail.split("\ndef ", 1)[0]
        assert "ARGIA_TAB_NAME" not in body
        assert "ARGIA_SCHEMA" not in body
        assert "ARGIA_TAB_NAME" in SRC

    def test_default_stays_off_in_the_deployed_unit(self):
        v2 = pathlib.Path(t5.__file__).resolve().parents[1]
        svc = (v2 / "server" / "bundle" / "argia-telemetry.service")
        if svc.exists():
            assert t5.PLANT_TABS_ENV not in svc.read_text(encoding="utf-8")
