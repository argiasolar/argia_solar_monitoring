"""v194 — Sheets retirement phase 3c: the Alerts ledger in PostgreSQL.

Locks: the switch defaults to sheet; PG records parse to the same
AlertRecord objects as the sheet; the upsert replaces every column
(the engine's block write semantics) keyed by alert_id; write_ledger is
the one write path of both engine scripts; a PG read failure raises
rather than returning an empty ledger; parity rules.
"""
import pathlib

import pytest

from argia.core import alerts_pg as P
from argia.core import alerts_state as S

V2 = pathlib.Path(__file__).resolve().parents[2]

REC = S.AlertRecord(
    alert_id="ALT-20260904-386", alert_key="mex1:inv:es2470051826:inverter_temp_high",
    plant_key="MEX1", inverter_sn="ES2470051826", metric="inverter_temp_high",
    severity="WARNING", state=S.AlertState.OPEN,
    opened_utc="2026-09-04T18:30:07+00:00", last_seen_utc="2026-09-05T00:07:07+00:00",
    resolved_utc="", value=61.5, threshold=60, message="Inverter ES2470051826 at 61.5 C",
    channels_sent="sheet,email", explanation="Heat-sink temperature above the limit; check fans/shade")
PG_CSV = ("alert_id,alert_key,plant_key,inverter_sn,metric,severity,state,opened_utc,last_seen_utc,"
          "resolved_utc,value,threshold,message,channels_sent,explanation\n"
          "ALT-20260904-386,mex1:inv:es2470051826:inverter_temp_high,MEX1,ES2470051826,"
          "inverter_temp_high,WARNING,OPEN,2026-09-04T18:30:07+00:00,2026-09-05T00:07:07+00:00,,"
          "61.5,60,Inverter ES2470051826 at 61.5 C,\"sheet,email\","
          "Heat-sink temperature above the limit; check fans/shade\n")


class FakeSheets:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def read_table(self, tab, a1="A1:Z"):
        self.calls.append(("read_table", tab, a1)); return self.rows

    def read_range(self, tab, a1):
        self.calls.append(("read_range", tab, a1)); return [S.ALERTS_HEADER]

    def ensure_tab(self, tab):
        self.calls.append(("ensure_tab", tab))

    def write_values(self, tab, a1, values):
        self.calls.append(("write_values", tab, a1, values))


class TestShape:
    def test_header_is_the_sheets(self):
        assert P.HEADER == S.ALERTS_HEADER

    def test_pg_csv_parses_to_the_same_record(self):
        ledger = S.records_from_rows(P.csv_to_records(PG_CSV))
        assert ledger.records == (REC,)
        # and the sheet round-trip of the same record parses identically
        row = dict(zip(S.ALERTS_HEADER, S.record_to_row(REC)))
        assert S.records_from_rows([row]).records == (REC,)

    def test_upsert_replaces_every_column(self):
        sql = P.build_upsert_sql([S.record_to_row(REC)])
        assert sql.startswith("INSERT INTO alert_ledger (alert_id, alert_key")
        assert "'ALT-20260904-386', 'mex1:inv:es2470051826:inverter_temp_high'" in sql
        assert ", '', 61.5, 60.0, 'Inverter ES2470051826 at 61.5 C', 'sheet,email'," in sql
        assert "ON CONFLICT (alert_id) DO UPDATE SET alert_key = EXCLUDED.alert_key" in sql
        for h in S.ALERTS_HEADER[1:]:
            assert f"{h} = EXCLUDED.{h}" in sql
        assert "alert_id = EXCLUDED" not in sql
        assert P.build_upsert_sql([]) == ""

    def test_quotes_and_blanks(self):
        r = S.record_to_row(S.AlertRecord("A", "k", "P", "", "m", "INFO", S.AlertState.RESOLVED,
                                          "t1", "t2", "t3", None, None, "it's dark", "", ""))
        sql = P.build_upsert_sql([r])
        assert "'it''s dark'" in sql and "NULL, NULL" in sql

    def test_ensure_idempotent(self):
        assert "CREATE TABLE IF NOT EXISTS alert_ledger" in P.ENSURE_SQL
        assert "CREATE INDEX IF NOT EXISTS" in P.ENSURE_SQL


class TestDoors:
    def test_sheet_mode_unchanged(self, monkeypatch):
        monkeypatch.delenv("ARGIA_ALERTS_SOURCE", raising=False)
        fs = FakeSheets([dict(zip(S.ALERTS_HEADER, S.record_to_row(REC)))])
        assert S.load_alerts_ledger(fs).records == (REC,)
        assert S.write_ledger(fs, [REC]) == 1
        assert ("read_table", "Alerts", "A1:O") in fs.calls
        assert fs.calls[-1][:3] == ("write_values", "Alerts", "A2:O2")
        S.create_alerts_tab_if_missing(fs)
        assert ("ensure_tab", "Alerts") in fs.calls

    def test_pg_mode_never_touches_the_sheet(self, monkeypatch):
        monkeypatch.setenv("ARGIA_ALERTS_SOURCE", "pg")
        monkeypatch.setattr(P, "_fetch_csv", lambda sql: PG_CSV)
        written = {}
        monkeypatch.setattr(P, "write_rows", lambda rows: written.setdefault("rows", rows) and len(rows))
        monkeypatch.setattr(P, "ensure", lambda: written.setdefault("ensured", True))
        fs = FakeSheets()
        assert S.load_alerts_ledger(fs).records == (REC,)
        assert S.write_ledger(fs, [REC]) == 1
        assert S.create_alerts_tab_if_missing(fs) is False
        assert fs.calls == [] and written["rows"] == [S.record_to_row(REC)] and written["ensured"]

    def test_pg_read_failure_raises_not_empty(self, monkeypatch):
        monkeypatch.setenv("ARGIA_ALERTS_SOURCE", "pg")
        monkeypatch.setattr(P, "_fetch_csv", lambda sql: (_ for _ in ()).throw(RuntimeError("psql failed")))
        with pytest.raises(RuntimeError):
            S.load_alerts_ledger(FakeSheets())

    def test_engine_scripts_use_the_one_write_path(self):
        for name in ("alerts_daily.py", "alerts_snapshot.py"):
            src = (V2 / "scripts" / name).read_text(encoding="utf-8")
            assert "write_ledger(sheets, records)" in src        # v196: post-mail records
            assert 'write_values("Alerts"' not in src and "write_values(\n" not in src


class TestParity:
    @pytest.fixture
    def cmp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "alerts_backfill_pg", V2 / "scripts" / "alerts_backfill_pg.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_clean_and_diff(self, cmp):
        a = S.AlertsLedger(records=(REC,))
        assert cmp.compare(a, a)["ok"]
        b = S.AlertsLedger(records=(S.AlertRecord(**{**REC.__dict__, "state": S.AlertState.RESOLVED}),))
        rep = cmp.compare(a, b)
        assert rep["diffs"] == [("ALT-20260904-386", "state", S.AlertState.OPEN, S.AlertState.RESOLVED)]
        assert rep["open_sheet"] == 1 and rep["open_pg"] == 0 and not rep["ok"]

    def test_only_in_sheet_fails_only_in_pg_allowed(self, cmp):
        a = S.AlertsLedger(records=(REC,))
        assert not cmp.compare(a, S.AlertsLedger())["ok"]
        assert cmp.compare(S.AlertsLedger(), a)["ok"]
        assert "VERDICT: CLEAN" in cmp.render(cmp.compare(a, a))
