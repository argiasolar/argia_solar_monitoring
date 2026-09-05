"""v200 — the admin catalog: drawers, tabs, cards; every old route still
answers; the Data sources card never leaks a secret."""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))
import setup_catalog as cat  # noqa: E402  (pure: no flask)

APP_SRC = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")


def routes():
    """(method, path) for every @app.get/@app.post in setup_app.py (AST)."""
    tree = ast.parse(APP_SRC)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr in ("get", "post") and d.args
                        and isinstance(d.args[0], ast.Constant)):
                    out.add((d.func.attr, d.args[0].value))
    return out


class TestModel:
    def test_five_drawers_with_unique_keys_and_tabs(self):
        assert cat.DRAWER_KEYS == ["people", "plants", "finance", "cfe", "system"]
        for d in cat.DRAWERS:
            keys = [t for t, _, _ in d["tabs"]]
            assert len(keys) == len(set(keys)), d["key"]
            assert d["color"].startswith("#") and d["sub_en"] and d["sub_es"]

    def test_urls(self):
        assert cat.drawer_url("finance") == "/setup/finance/"
        assert cat.drawer_url("finance", "loans") == "/setup/finance/#loans"
        assert cat.drawer("nope") is None


class TestHtml:
    def test_home_lists_every_drawer_and_tab(self):
        h = cat.catalog_home()
        for d in cat.DRAWERS:
            assert f'href="/setup/{d["key"]}/"' in h
            for t, en, _ in d["tabs"]:
                assert f'href="/setup/{d["key"]}/#{t}"' in h and f'>{en}<' in h.replace("&amp;", "&")

    def test_drawer_page_orders_sections_by_tab_and_marks_active(self):
        d = cat.drawer("finance")
        h = cat.drawer_page(d, [("changelog", "<i>c</i>"), ("loans", "<i>l</i>"), ("bogus", "x")])
        assert h.index('id="loans"') < h.index('id="changelog"') and "bogus" not in h
        assert '<a href="/setup/finance/" class="on"' in h
        assert '<a href="#om"' in h                        # tab bar lists every tab
        assert "<i>l</i>" in h and "<i>c</i>" in h

    def test_escaping(self):
        d = {"key": "x", "en": "A&B", "es": "A&B", "color": "#000", "sub_en": "<s>", "sub_es": "<s>",
             "tabs": [("t", "T&", "T&")]}
        h = cat.catalog_home([d])
        assert "A&amp;B" in h and "&lt;s&gt;" in h and "<s>" not in h


class TestParsers:
    def test_timers_from_systemd_json(self):
        now = 1_788_570_000_000_000                       # 2026-09-05 01:00:00 UTC
        t = ('[{"next":1788571020000000,"left":1788571020000000,"last":1788570720090959,'
             '"passed":206752874332,"unit":"argia-monitoring-gen.timer","activates":"argia-monitoring-gen.service"},'
             '{"next":null,"left":null,"last":1788480900000000,"passed":1,"unit":"argia-kpimirror.timer",'
             '"activates":"argia-kpimirror.service"},'
             '{"next":1790917200000000,"left":0,"last":0,"passed":0,"unit":"argia-archive-month.timer",'
             '"activates":"argia-archive-month.service"}]')
        fmt = lambda us: f"T{us // 1_000_000}"
        rows = cat.parse_timers(t, now_us=now, fmt=fmt)
        assert [r["unit"] for r in rows] == ["argia-monitoring-gen.timer", "argia-kpimirror.timer", "argia-archive-month.timer"]
        assert rows[0] == {"next": "T1788571020", "left": "17min", "last": "T1788570720", "passed": "0s",
                           "unit": "argia-monitoring-gen.timer", "svc": "argia-monitoring-gen.service"}
        assert rows[1]["next"] == "" and rows[1]["left"] == "" and rows[1]["passed"] == "1 day"   # disabled timer
        assert rows[2]["last"] == "" and rows[2]["passed"] == "" and rows[2]["left"] == "3 weeks 6 days"

    def test_jobs_card_asks_systemd_for_json(self):
        src = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        assert "'list-timers', '--all', '--no-pager', '--output=json', 'argia-*'" in src

    def test_timers_tolerate_garbage(self):
        assert cat.parse_timers("", now_us=1) == [] and cat.parse_timers("not json", now_us=1) == []
        assert cat.parse_timers('[{"unit":"ssh.service"}]', now_us=1) == []

    def test_span(self):
        M = 1_000_000
        assert cat._span(8 * M) == "8s" and cat._span(188 * M) == "3min 8s"
        assert cat._span(5280 * M) == "1h 28min" and cat._span(3600 * M) == "1h"
        assert cat._span(2 * 86400 * M + 3 * 3600 * M) == "2 days 3h" and cat._span(-5) == "0s"

    def test_failed_units(self):
        assert cat.parse_failed_units("argia-sync.service loaded failed failed X\nssh.service loaded failed failed Y\n") == ["argia-sync.service"]
        assert cat.parse_failed_units("") == []


class TestSources:
    ENV = ("# comment\nGOOGLE_CREDENTIALS_FILE=/root/sa.json\nSMTP_PASS=hunter2\n"
           "ARGIA_CONFIG_SOURCE=pg\nARGIA_KPI_WRITE=both\nexport ARGIA_SHEET_OUTBOX=0\n"
           "GOOGLE_SHEET_ID_V2=1abcSECRET\nANTHROPIC_API_KEY=sk-xyz\n")

    def test_parse_keeps_switches_only_and_never_a_secret(self):
        sw = cat.parse_env_switches(self.ENV)
        assert sw["ARGIA_CONFIG_SOURCE"] == "pg" and sw["ARGIA_KPI_WRITE"] == "both"
        assert sw["ARGIA_SHEET_OUTBOX"] == "0"
        assert sw["GOOGLE_SHEET_ID_V2"] == "set" and sw["ARGIA_SOLAR_SHEET_ID"] == "unset"
        assert "SMTP_PASS" not in sw and "ANTHROPIC_API_KEY" not in sw and "GOOGLE_CREDENTIALS_FILE" not in sw
        assert "1abcSECRET" not in str(sw)

    def test_still_needed_matches_the_package_rule(self):
        sw = cat.parse_env_switches(self.ENV)
        need = cat.sheet_still_needed(sw)
        assert need == ["ARGIA_KPI_WRITE=both"]                # v205: unset = pg, only the explicit 'both' remains
        need2 = cat.sheet_still_needed(dict(cat.parse_env_switches(self.ENV), ARGIA_SHEET_TELEMETRY="1"))
        assert "ARGIA_SHEET_TELEMETRY=on" in need2
        allpg = {k: "pg" for k, _, _ in cat.SWITCH_META if not k.startswith("ARGIA_SHEET_")}
        allpg.update({"ARGIA_SHEET_TELEMETRY": "0", "ARGIA_SHEET_OUTBOX": "0"})
        assert cat.sheet_still_needed(allpg) == []

    def test_card_shows_switches_and_verdict_without_values_of_ids(self):
        h = cat.sources_card_html(cat.parse_env_switches(self.ENV))
        assert "ARGIA_CONFIG_SOURCE" in h and ">pg<" in h and "GOOGLE_SHEET_ID_V2" in h
        assert "1abcSECRET" not in h and "hunter2" not in h and "sk-xyz" not in h
        assert "still needed by" in h
        rows = {n: v for n, v, _, _ in cat.switch_rows(cat.parse_env_switches(self.ENV))}
        assert rows["ARGIA_SHEET_TELEMETRY"] == "0 (default)" and rows["ARGIA_TELEMETRY_SOURCE"] == "pg (default)"
        assert rows["ARGIA_SHEET_OUTBOX"] == "0" and rows["ARGIA_KPI_WRITE"] == "both"      # set = shown bare
        done = {k: "pg" for k, _, _ in cat.SWITCH_META}
        done.update({"ARGIA_SHEET_TELEMETRY": "0", "ARGIA_SHEET_OUTBOX": "0", "ARGIA_SHEET_JOBLOG": "0"})
        assert "not needed by any job" in cat.sources_card_html(done)


class TestRoutes:
    OLD_POSTS = {"/update", "/suspend", "/add", "/settings", "/password", "/delete",
                 "/maint/add", "/maint/close", "/maint/approve", "/maint/delete",
                 "/mail/add", "/mail/toggle", "/mail/delete",
                 "/finance/om", "/finance/prbaseline", "/finance/sla", "/finance/principal",
                 "/finance/payments", "/finance/fx", "/finance/extend", "/finance/truncate",
                 "/finance/fee", "/finance/tariff", "/account/change", "/account/profile"}
    OLD_GETS = {"/", "/finance", "/account/", "/account/whoami", "/healthz"}
    NEW_GETS = {"/people/", "/plants/", "/finance/", "/cfe/", "/system/"}

    def test_every_old_route_still_answers_and_the_drawers_exist(self):
        r = routes()
        posts = {p for m, p in r if m == "post"}
        gets = {p for m, p in r if m == "get"}
        assert self.OLD_POSTS <= posts, self.OLD_POSTS - posts
        assert (self.OLD_GETS | self.NEW_GETS) <= gets, (self.OLD_GETS | self.NEW_GETS) - gets

    def test_post_outcomes_land_on_their_drawer(self):
        assert "_post_done(to='/setup/plants/#maintenance'" in APP_SRC
        assert "_post_done(to='/setup/people/#mail'" in APP_SRC
        assert APP_SRC.count("_post_done(to='/setup/plants/#maintenance'") >= 4
        assert APP_SRC.count("_post_done(to='/setup/people/#mail'") >= 3

    def test_finance_page_is_split_into_the_drawer_tabs(self):
        for key in ("loans", "om", "fees", "tariffs", "baselines", "changelog", "invoicing"):
            assert f"sect['{key}']" in APP_SRC, key
        assert "def invoicing_card" in APP_SRC

    def test_company_admins_keep_their_page(self):
        assert "return page(body, msg=msg, once=once, title=('Report access setup'" in APP_SRC
