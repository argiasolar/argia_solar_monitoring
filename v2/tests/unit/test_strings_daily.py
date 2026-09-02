"""Tests: per-string daily reduction (v170) — the raw material for the
solar director's string-level analysis.

The math tested here decides whether a string gets flagged for a field
visit, so the allocation identities (shares sum to 1, string energies
sum to their MPPT counter) are asserted exactly, and the reduction is
also run against a REAL captured getMAXHistory fixture, not only
synthetic rows.
"""

import json
import pathlib

from argia.kpi.strings import (
    ENSURE_TABLE_SQL, channel_day_stats, mppt_of_string, upsert_sqls)
from argia.vendors.growatt_web_parser import parse_max_history

V2 = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = (V2 / "tests/fixtures/growatt_web/"
           "GTO1_getMAXHistory_JFM5D8900B_2026-05-11.json")


def _rows(t, **kw):
    base = {"time": t}
    base.update(kw)
    return base


class TestMapping:
    def test_max_pairing_convention(self):
        assert mppt_of_string(1) == 1 and mppt_of_string(2) == 1
        assert mppt_of_string(3) == 2 and mppt_of_string(4) == 2
        assert mppt_of_string(31) == 16 and mppt_of_string(32) == 16


class TestChannelDayStats:
    def test_mppt_energy_is_counter_maximum(self):
        s = channel_day_stats([
            _rows("2026-08-01 10:00:00", epv1Today=5.0, vpv1=600),
            _rows("2026-08-01 15:00:00", epv1Today=90.2, vpv1=610),
            _rows("2026-08-01 12:00:00", epv1Today=40.0, vpv1=605),
        ])
        assert s["mppt"][1]["energy_kwh"] == 90.2
        assert s["samples"] == 3

    def test_string_energy_allocated_by_current_share(self):
        rows = [_rows(f"2026-08-01 10:{m:02d}:00", epv1Today=100.0,
                      vpv1=600, currentString1=6.0, currentString2=4.0)
                for m in range(0, 30, 5)]
        s = channel_day_stats(rows)
        s1, s2 = s["string"][1], s["string"][2]
        assert abs(s1["share"] - 0.6) < 1e-6
        assert abs(s2["share"] - 0.4) < 1e-6
        # allocation identity: the pair reassembles its MPPT counter
        assert abs(s1["energy_kwh"] + s2["energy_kwh"] - 100.0) < 0.01

    def test_gap_dt_is_clamped(self):
        # 4 h gap between samples must count as <= 10 min of current
        s = channel_day_stats([
            _rows("2026-08-01 08:00:00", currentString1=6.0),
            _rows("2026-08-01 12:00:00", currentString1=6.0),
        ])
        assert s["string"][1]["q_ah"] <= 6.0 * (10 / 60) * 2 + 1e-9

    def test_sleeping_channels_are_omitted_not_zero(self):
        s = channel_day_stats([_rows("2026-08-01 10:00:00", epv1Today=50.0,
                                     vpv1=600, vpv2=9.9,
                                     currentString3=0.0)])
        assert 2 not in s["mppt"]          # 9.9 V = asleep/unpopulated
        assert 3 not in s["string"]        # zero current -> no record

    def test_str_unmatch_flag_carried(self):
        s = channel_day_stats([_rows("2026-08-01 10:00:00", StrUnmatch=3)])
        assert s["flags"]["str_unmatch"] == 3

    def test_real_fixture_reduces_cleanly(self):
        d = json.loads(FIXTURE.read_text(encoding="utf-8"))
        rows = parse_max_history(d["response"])
        s = channel_day_stats(rows)
        assert s["samples"] == 80                       # one full page
        assert len(s["mppt"]) >= 1
        for n, rec in s["string"].items():
            assert rec["mppt"] == mppt_of_string(n)
            if rec["share"] is not None:
                assert 0.0 <= rec["share"] <= 1.0


class TestUpsertSql:
    def test_statements_are_idempotent_upserts(self):
        stats = channel_day_stats([
            _rows("2026-08-01 10:00:00", epv1Today=10.0, vpv1=600,
                  currentString1=5.0, currentString2=5.0)])
        sqls = upsert_sqls("GTO1", "SN1", "2026-08-01", stats)
        assert len(sqls) == 3                           # pv1 + s1 + s2
        for sql in sqls:
            assert "ON CONFLICT" in sql and "DO UPDATE" in sql
        assert "'pv1','mppt'" in sqls[0]
        assert "'s1','string'" in sqls[1]

    def test_table_has_the_channel_pk(self):
        assert "PRIMARY KEY (plant_key, inverter_sn, prod_date, channel)" \
            in ENSURE_TABLE_SQL


class TestCollectorScript:
    def test_pages_through_history(self):
        src = (V2 / "scripts/string_daily.py").read_text(encoding="utf-8")
        assert "haveNext" in src and "MAX_PAGES" in src
        assert "start += 1" in src

    def test_timer_runs_after_mx_sunset(self):
        t = (V2 / "server/bundle/argia-strings.timer").read_text(
            encoding="utf-8")
        assert "21:15:00 America/Mexico_City" in t
