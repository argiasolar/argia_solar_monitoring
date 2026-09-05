"""v207.0 - the daily string-flag rule reads telemetry_detail, not the
retired per-plant sheet tabs (where it produced nothing since v199)."""
import datetime as dt
import importlib.util
import pathlib

from argia.analytics.vendor_flags import evaluate_string_new_bits
from argia.store import pg_detail

V2 = pathlib.Path(__file__).resolve().parents[2]
UTC = dt.timezone.utc


def _mod():
    spec = importlib.util.spec_from_file_location(
        "alerts_daily", V2 / "scripts" / "alerts_daily.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ts(day, hour):
    # 12:00 MX = 18:00 UTC (CST, no DST)
    return dt.datetime(2026, 9, day, hour + 6, 0, tzinfo=UTC)


def test_split_by_mx_day_and_active_plants():
    m = _mod()
    rows = [
        (_ts(4, 12), "SLP1", "A", {"str_break": "4", "str_unmatch": "0", "str_unblance": "0"}),
        (_ts(3, 12), "SLP1", "A", {"str_break": "0", "str_unmatch": "0", "str_unblance": "0"}),
        (_ts(4, 12), "ZZZ9", "X", {"str_break": "1", "str_unmatch": "0", "str_unblance": "0"}),
        (_ts(1, 12), "SLP1", "A", {"str_break": "2", "str_unmatch": "0", "str_unblance": "0"}),  # before window
    ]
    day, base = m.split_string_samples(rows, "2026-09-04", "2026-09-02", {"SLP1"})
    assert [(pk, sn) for _t, pk, sn, _f in day] == [("SLP1", "A")]
    assert [(pk, sn) for _t, pk, sn, _f in base] == [("SLP1", "A")]
    assert base[0][0] == _ts(3, 12)


def test_new_bit_today_absent_from_baseline_fires_through_the_real_evaluator():
    m = _mod()
    rows = [(_ts(3, h), "SLP2", "B", {"str_break": "0", "str_unmatch": "0", "str_unblance": "0"})
            for h in range(9, 16)]
    rows += [(_ts(4, h), "SLP2", "B", {"str_break": "8", "str_unmatch": "0", "str_unblance": "0"})
             for h in range(9, 16)]
    day, base = m.split_string_samples(rows, "2026-09-04", "2026-08-21", {"SLP2"})
    breaches = evaluate_string_new_bits(day, base)
    assert breaches and breaches[0].plant_key == "SLP2" and breaches[0].inverter_sn == "B"


def test_reader_sql_and_row_shape(monkeypatch):
    seen = {}

    def fake_rows(sql):
        seen["sql"] = sql
        return [["2026-09-04 18:00:00+00", "SLP1", "A", "4", "", "0"],
                ["garbage", "SLP1", "A", "1", "0", "0"]]
    import argia.store.pgq as pgq
    monkeypatch.setattr(pgq, "psql_rows", fake_rows)
    out = pg_detail.read_string_flags("2026-08-21", "2026-09-04")
    assert "FROM telemetry_detail" in seen["sql"]
    assert "BETWEEN DATE '2026-08-21' AND DATE '2026-09-04'" in seen["sql"]
    assert "America/Mexico_City" in seen["sql"]
    assert len(out) == 1
    ts, pk, sn, flags = out[0]
    assert (pk, sn) == ("SLP1", "A") and ts.tzinfo is not None
    assert flags == {"str_break": "4", "str_unmatch": None, "str_unblance": "0"}


def test_alerts_daily_no_longer_reads_sheet_tabs():
    src = (V2 / "scripts" / "alerts_daily.py").read_text(encoding="utf-8")
    assert 'f"Telemetry_{plant.plant_key}"' not in src
    assert "read_string_flags" in src
    # a PG failure is a WARNING, never an INFO "skipped"
    assert 'log.warning("string flags: telemetry_detail unreadable' in src
