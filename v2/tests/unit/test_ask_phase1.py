"""Ask ARGIA phase 1 (v215): every table reachable read-only, the ARGIA
Golden Standard searchable, tables with a summary row, customer-scoped
accounts. Fake database keyed on /*tag:...*/ as in test_ask_tools."""
from __future__ import annotations

import json
import re

import pytest

from argia.ask import agent as A
from argia.ask import knowledge as K
from argia.ask import sqltool as S
from argia.ask import tools as T

TAG = re.compile(r"/\*tag:(\w+)\*/")


class FakeDB:
    def __init__(self, data):
        self.data, self.calls, self.sql = data, [], []

    def __call__(self, sql):
        m = TAG.search(sql)
        assert m, f"query without tag: {sql[:80]}"
        self.calls.append(m.group(1))
        self.sql.append(sql)
        v = self.data.get(m.group(1), [])
        return v(sql) if callable(v) else v


PLANTS = [["GTO1", "Taigene", "GROWATT", "500.0", "PPA", "t", "2.1", "0.80"],
          ["MEX2", "Vitalmex", "HUAWEI", "300.0", "PPA", "t", "2.3", "0.80"],
          ["GTO2", "Hirschmann", "SOLAREDGE", "200.0", "CAPEX", "t", "0", "0"]]
BASE = {"plants": PLANTS, "freshness": [["2026-09-04 16:05:00+00", "2026-09-03"]]}


# ------------------------------------------------------------ knowledge
PAGE = ('<html><head></head><body><div class="topbar"></div>'
        '<script>const DATA = ' + json.dumps({
            "en": {"slides": [
                '<img src="data:image/png;base64,AAAA"><h1>ARGIA Golden Standard</h1><p>Designer training</p>',
                '<h2>String sizing</h2><p>Strings must stay within the inverter MPPT window at &minus;10&nbsp;&deg;C.</p><ul><li>Voc check</li><li>Vmp check</li></ul>',
                '',
                '<div class="k">AGS-701</div><h2>PR_STC</h2><p>Temperature-corrected PR per IEC 61724-3.</p>']},
            "es": {"slides": ['<h1>Estándar Dorado ARGIA</h1><p>Capacitación</p>', '<h2>Dimensionamiento de strings</h2><p>Ventana MPPT a -10 °C.</p>']},
            "cz": {"slides": ['<h1>Zlatý standard</h1>']}}) +
        ';\nfunction go(){}</script></body></html>')


class TestKnowledge:
    def test_parse_ags_slides_per_language(self):
        rows = K.parse_ags(PAGE)
        by = {(r["lang"], r["n"]): r for r in rows}
        assert ("en", 3) not in by                      # empty slide skipped
        assert by[("en", 2)]["title"] == "String sizing"
        assert "MPPT window at −10 °C" in by[("en", 2)]["body"]     # entities decoded, image gone
        assert "base64" not in by[("en", 1)]["body"] and "Voc check" in by[("en", 2)]["body"]
        assert by[("en", 4)]["body"].startswith("AGS-701")
        assert by[("en", 4)]["title"] == "AGS-701 · PR_STC"          # clause code leads the title
        assert {r["lang"] for r in rows} == {"en", "es", "cz"}
        assert all(r["doc"] == "AGS" for r in rows)

    def test_missing_data_fails_loud(self):
        with pytest.raises(ValueError):
            K.parse_ags("<html><body>no deck</body></html>")

    def test_upsert_prune_and_search_sql(self):
        rows = K.parse_ags(PAGE)
        sqls = K.build_upsert_sql(rows, batch=2)
        assert len(sqls) == 3 and all("ON CONFLICT (doc, lang, n) DO UPDATE" in s for s in sqls)
        assert "('AGS','en',2,'String sizing'," in sqls[0]
        assert K.build_prune_sql("AGS", "en", 364) == "DELETE FROM knowledge WHERE doc='AGS' AND lang='en' AND n > 364;"
        q = K.search_sql("string sizing; -10 'C", "es", 3)
        assert "to_tsquery('simple', 'string:* | sizing:* | 10:*')" in q and "lang='es'" in q and "LIMIT 3" in q
        assert K.tsquery("What does AGS-104 say about PR_STC?") == "what:* | does:* | ags:* | 104:* | say:* | about:* | pr:* | stc:*"
        assert K.tsquery("") == ""
        assert "/*tag:knowledge_search*/" in q
        # psql -A -t splits output on newlines: the body must come back on one line
        assert "regexp_replace(body, E'[\\n\\t]+', ' ', 'g')" in q
        assert "LIMIT 10" in K.search_sql("x", "xx", 99)          # clamped, unknown lang -> en

    def test_excerpt_centres_on_the_query(self):
        body = "a" * 2000 + " MPPT window " + "b" * 2000
        ex = K.excerpt(body, "mppt", width=300)
        assert "MPPT window" in ex and ex.startswith("…") and ex.endswith("…") and len(ex) <= 304

    def test_ensure_sql_is_generated_tsvector_with_gin(self):
        assert "tsv        tsvector GENERATED ALWAYS AS" in K.ENSURE_SQL and "USING gin (tsv)" in K.ENSURE_SQL


# -------------------------------------------------------------- sqltool
class TestSqlGuard:
    @pytest.mark.parametrize("sql", [
        "SELECT plant_key, sum(energy_kwh) FROM daily_production GROUP BY 1",
        "select * from plant where active",
        "WITH d AS (SELECT * FROM daily_production) SELECT count(*) FROM d JOIN plant p ON p.plant_key = d.plant_key",
        "SELECT * FROM public.cfe_tariff LIMIT 5;",
    ])
    def test_reads_pass(self, sql):
        assert S.guard(sql)

    @pytest.mark.parametrize("sql,why", [
        ("DELETE FROM plant", "only SELECT"),
        ("SELECT 1; DROP TABLE plant", "one statement"),
        ("SELECT * FROM plant -- x", "comments"),
        ("SELECT * FROM ask_log", "table not allowed"),
        ("SELECT * FROM users", "table not allowed"),
        ("SELECT pg_sleep(10)", "not allowed"),
        ("SELECT * FROM pg_catalog.pg_tables", "not readable"),
        ("SELECT * FROM information_schema.tables", "not readable"),
        ("SELECT * INTO x FROM plant", "not allowed"),
        ("SELECT set_config('a','b',false)", "not allowed"),
        ("", "empty"),
    ])
    def test_writes_and_escapes_are_rejected(self, sql, why):
        with pytest.raises(S.SqlRejected) as e:
            S.guard(sql)
        assert why in str(e.value)

    def test_wrap_is_read_only_capped_and_named(self):
        w = S.wrap("SELECT plant_key FROM plant")
        assert w.startswith("BEGIN READ ONLY; SET LOCAL statement_timeout = 10000;")
        assert "SELECT row_to_json(q)::text FROM (SELECT plant_key FROM plant) q LIMIT 200; COMMIT;" in w
        assert S.parse_rows([['{"plant_key": "GTO1", "n": 2}'], [""], ["not json"]]) == [{"plant_key": "GTO1", "n": 2}]

    def test_describe_lists_only_existing_allowed_tables(self):
        db = FakeDB({"describe_tables": [["plant", "plant_key", "text"], ["plant", "kwp_dc", "numeric"],
                                         ["ask_log", "id", "integer"]]})
        d = S.describe(db, ["plant", "ask_log", "nope"])
        assert [t["table"] for t in d["tables"]] == ["plant"]
        assert d["tables"][0]["columns"] == ["plant_key text", "kwp_dc numeric"] and "portfolio" in d["tables"][0]["note"]
        assert "ask_log" not in S.ALLOWED_TABLES and "users" not in S.ALLOWED_TABLES


# ------------------------------------------------------------ new tools
class TestNewTools:
    def test_query_database_returns_rows_by_column(self):
        db = FakeDB(dict(BASE, query_database=[['{"plant_key":"GTO1","kwh":1843.5}'], ['{"plant_key":"MEX2","kwh":1200}']]))
        out = T.run_tool(db, "query_database", {"sql": "SELECT plant_key, sum(energy_kwh) kwh FROM daily_production GROUP BY 1"})
        assert out["rows"][0] == {"plant_key": "GTO1", "kwh": 1843.5}
        assert out["totals"] == {"rows": 2, "capped": False, "column_sums": {"kwh": 3043.5}}
        assert db.sql[-1].startswith("BEGIN READ ONLY")
        bad = T.run_tool(db, "query_database", {"sql": "DELETE FROM plant"})
        assert "query rejected" in bad["error"]

    def test_query_database_sql_error_comes_back_as_text(self):
        def boom(sql):
            raise RuntimeError("psql failed: ERROR:  column nope does not exist\nLINE 1")
        db = FakeDB(dict(BASE, query_database=boom))
        out = T.run_tool(db, "query_database", {"sql": "SELECT nope FROM plant"})
        assert out["error"].startswith("query failed: ERROR:  column nope")

    def test_search_standard_hits_with_citation(self):
        db = FakeDB(dict(BASE, knowledge_search=[["12", "String sizing", "Strings must stay within the MPPT window", "0.6"]]))
        out = T.run_tool(db, "search_standard", {"query": "MPPT window", "lang": "es", "limit": 3})
        assert out["hits"][0]["ref"] == "AGS slide 12 — String sizing" and out["totals"] == {"hits": 1}
        assert "lang='es'" in db.sql[-1]
        assert T.run_tool(db, "search_standard", {"query": ""})["error"] == "query is required"
        empty = T.run_tool(FakeDB(dict(BASE, knowledge_search=[])), "search_standard", {"query": "zzz"})
        assert empty["hits"] == [] and "nothing in the standard" in empty["note"]

    def test_reconciliation_and_monthly_close(self):
        db = FakeDB(dict(BASE, recon_daily=[
            ["GTO1", "2026-09-02", "1800", "1843", "1843", "61.1", "-2.33", "REVIEW", "completeness 61.1% < 95%", "1843", "vendor_plant_daily"],
            ["MEX2", "2026-09-02", "1200", "1200", "1200", "100", "0.00", "PASS", "ok", "1200", "inverter_counters"]],
            recon_monthly=[["GTO1", "2026-08", "52000", "inverter_counter_daily_sum", "PASS", "2026-09-01 06:10", "auto", "", "51900", "52000", "52010", "52005", "99.1"],
                           ["GTO2", "2026-08", "", "", "REVIEW", "", "", "vendor gap", "", "", "", "", ""]]))
        r = T.run_tool(db, "get_reconciliation", {"date_from": "2026-09-02", "date_to": "2026-09-02"})
        assert r["totals"] == {"plant_days": 2, "by_status": {"REVIEW": 1, "PASS": 1}, "kpi_kwh": 3043.0}
        assert r["days"][0]["name"] == "Taigene" and r["days"][0]["reference_basis"] == "vendor_plant_daily"
        m = T.run_tool(db, "get_monthly_close", {"month": "2026-08"})
        assert m["totals"] == {"plant_months": 2, "closed": 1, "open": 1, "billing_kwh": 52000.0}
        assert m["months"][0]["closed"] is True and m["months"][1]["closed"] is False
        assert m["months"][0]["interval_sum_kwh"] == 51900.0 and "billing_basis" in db.sql[-2]
        assert "YYYY-MM" in T.run_tool(db, "get_monthly_close", {"month": "August"})["error"]

    def test_cfe_tariffs_average_and_region(self):
        db = FakeDB(dict(BASE, cfe_latest=[["2026-09"]],
                         cfe_charges=[["ENERGIA BASE", "MXN/KWH", "1.0470", "0.5964", "2.0240", "17", "t"],
                                      ["ENERGIA INTERMEDIA", "MXN/KWH", "1.7151", "0.9220", "2.5679", "17", "t"],
                                      ["ENERGIA PUNTA", "MXN/KWH", "2.0003", "1.2420", "3.6117", "17", "t"],
                                      ["CAPACIDAD", "MXN/KW-MONTH", "358.4", "344.26", "397.44", "17", "t"]]))
        out = T.run_tool(db, "get_cfe_tariffs", {})
        assert out["tariff"] == "GDMTH" and out["month"] == "2026-09" and out["region"] == "average of all regions"
        assert out["totals"]["energy_avg_mxn_per_kwh"] == pytest.approx(1.5875, abs=1e-4) and out["totals"]["charges"] == 4
        out2 = T.run_tool(db, "get_cfe_tariffs", {"tariff": "gdmth", "region": "Bajio", "month": "2026-08"})
        assert "region = 'BAJIO'" in db.sql[-1] and "= '2026-08'" in db.sql[-1] and out2["region"] == "BAJIO"
        assert "bad tariff" in T.run_tool(db, "get_cfe_tariffs", {"tariff": "x;y"})["error"]

    def test_performance_carries_a_fleet_summary_row(self):
        def perf(sql):
            return [["GTO1", "1843", "2110", "1843", "0.71", "0.894", "1", "1", "5.2"],
                    ["MEX2", "1200", "1200", "1200", "0.80", "1.0", "1", "1", "5.0"]]
        db = FakeDB(dict(BASE, perf=perf))
        out = T.run_tool(db, "get_performance", {"date_from": "2026-09-01", "date_to": "2026-09-03"})
        t = out["totals"]
        assert t["plants"] == 2 and t["production_kwh"] == 3043.0 and t["kwp_dc"] == 800.0
        # kWp-weighted: (0.71*500 + 0.80*300) / 800
        assert t["pr"] == pytest.approx(0.744, abs=1e-3) and "kWp-weighted" in t["basis"]

    def test_registry_and_dispatch_agree(self):
        names = {t["name"] for t in T.TOOLS}
        assert names == set(T.DISPATCH)
        for n in ("search_standard", "query_database", "describe_tables", "get_reconciliation",
                  "get_monthly_close", "get_cfe_tariffs"):
            assert n in names


# ---------------------------------------------------------------- scope
class TestScope:
    def test_internal_tools_and_foreign_plants_are_refused(self):
        db = FakeDB(dict(BASE))
        assert "not available for this account" in T.run_tool(db, "query_database", {"sql": "SELECT 1"}, scope={"GTO2"})["error"]
        assert "not available for this account" in T.run_tool(db, "get_revenue", {"date_from": "2026-09-01", "date_to": "2026-09-02"}, scope={"GTO2"})["error"]
        assert "not in your account's scope" in T.run_tool(db, "get_plant_overview", {"plant": "Taigene"}, scope={"GTO2"})["error"]

    def test_lists_are_filtered_and_totals_dropped(self):
        def perf(sql):
            return [["GTO1", "1843", "2110", "1843", "0.71", "0.894", "1", "1", "5.2"],
                    ["GTO2", "500", "", "", "0.75", "", "1", "1", ""]]
        db = FakeDB(dict(BASE, perf=perf))
        out = T.run_tool(db, "get_performance", {"date_from": "2026-09-01", "date_to": "2026-09-03"}, scope={"GTO2"})
        assert [p["plant_key"] for p in out["plants"]] == ["GTO2"] and "totals" not in out
        full = T.run_tool(db, "get_performance", {"date_from": "2026-09-01", "date_to": "2026-09-03"})
        assert len(full["plants"]) == 2 and "totals" in full

    def test_system_prompt_lists_only_scoped_plants(self):
        db = FakeDB(dict(BASE))
        sysm = A.build_system(db, lang="en", scope={"GTO2"})
        vocab = sysm.split("Fleet vocabulary")[1].split("NAMES:")[0]
        assert "Hirschmann = GTO2" in vocab and "Taigene" not in vocab
        assert "This account sees only these plants: Hirschmann" in sysm
        assert "This account sees only" not in A.build_system(db, lang="en")

    def test_ask_app_scope_from_the_account(self, monkeypatch):
        pytest.importorskip("flask")                    # the laptop venv has no flask (CI has)
        import importlib, sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server" / "bundle"))
        aa = importlib.import_module("ask_app")
        aa.app.config["ROWS"] = FakeDB(dict(BASE))
        with aa.app.test_request_context("/ask/api"):
            assert aa.scope_of("tomasz", lambda u: {"level": "argia", "reports": "", "is_admin": 0}) is None
            assert aa.scope_of("boss", lambda u: {"level": "client", "reports": "", "is_admin": 1}) is None
            assert aa.scope_of("owner", lambda u: {"level": "client", "reports": "gto2,financial", "is_admin": 0}) == {"GTO2"}
            assert aa.scope_of("capexer", lambda u: {"level": "client", "reports": "capex", "is_admin": 0}) == {"GTO2"}
            assert aa.scope_of("gone", lambda u: None) is None        # no row = internal (test/dev boxes)
            assert aa.scope_of("off", lambda u: {"level": "argia", "disabled": 1}) == set()


# --------------------------------------------------------------- prompt
class TestPromptRules:
    def test_tables_end_with_a_summary_row_from_the_tool(self):
        assert "END THE TABLE WITH A SUMMARY ROW" in A.SYSTEM_TEMPLATE
        assert 'taken from the tool\'s "totals" field' in A.SYSTEM_TEMPLATE

    def test_standard_and_sql_rules(self):
        assert "call \\\nsearch_standard" in A.SYSTEM_TEMPLATE or "search_standard" in A.SYSTEM_TEMPLATE
        assert "ARGIA Golden Standard, slide N — title" in A.SYSTEM_TEMPLATE
        assert "describe_tables, then query_database" in A.SYSTEM_TEMPLATE
        assert "phase 0" not in A.SYSTEM_TEMPLATE

    def test_every_multi_row_tool_has_totals(self):
        db = FakeDB(dict(BASE, perf=lambda sql: [["GTO1", "1843", "2110", "1843", "0.71", "0.894", "1", "1", "5.2"]],
                         today_live=[], alarms=[], daily_range=[], recon_daily=[], recon_monthly=[],
                         cfe_latest=[["2026-09"]], cfe_charges=[["ENERGIA BASE", "MXN/KWH", "1", "1", "1", "1", "t"]],
                         knowledge_search=[], revenue=[], contract=[]))
        for name, params in (("get_portfolio_overview", {}), ("get_performance", {"date_from": "2026-09-01", "date_to": "2026-09-02"}),
                             ("get_generation", {"plant": "GTO1", "date_from": "2026-09-01", "date_to": "2026-09-02"}),
                             ("get_revenue", {"date_from": "2026-09-01", "date_to": "2026-09-02"}),
                             ("get_reconciliation", {"date_from": "2026-09-01", "date_to": "2026-09-02"}),
                             ("get_monthly_close", {}), ("get_cfe_tariffs", {}), ("search_standard", {"query": "mppt"})):
            out = T.run_tool(db, name, params)
            assert "totals" in out, (name, out)
