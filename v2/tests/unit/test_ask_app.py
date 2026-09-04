"""The /ask/ service through Flask's test client: who gets in, what the
API returns, that every answer is logged. The model is scripted and
the database is the fake from test_ask_tools — no network, no PG."""

import importlib
import json
import sys
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))
pytest.importorskip("flask", reason="flask not installed here")

from tests.unit.test_ask_agent import ScriptedLLM, final, tool_use   # noqa: E402
from tests.unit.test_ask_tools import BASE, FakeDB                    # noqa: E402

ME = "tomasz.zemelka@argia.com.mx"


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("ARGIA_ASK_EMAILS", ME)
    monkeypatch.setenv("ARGIA_ASK_USERS", "")
    import ask_app as aa
    importlib.reload(aa)
    emails = {"tomasz": ME, "pedro": "pedro@argia.com.mx", "owner": ""}
    monkeypatch.setattr(aa, "email_of", lambda u: emails.get(u, ""))
    db = FakeDB(dict(BASE))
    db.data["inverter_count"] = [["4", "4"]]
    db.data["maintenance"] = []
    db.data["alarms_active"] = []
    aa.app.config["ROWS"] = db
    aa.app.config["EXEC"] = []
    aa.app.config["EXEC"] = aa.app.config["EXEC"].append
    aa.app.testing = True
    return aa


def hdr(user):
    return {"X-Remote-User": user} if user else {}


class TestGate:
    def test_allow_listed_email_gets_the_page(self, app):
        r = app.app.test_client().get("/ask/", headers=hdr("tomasz"))
        assert r.status_code == 200 and b"Ask ARGIA" in r.data
        assert b"tomasz" in r.data and r.headers["Cache-Control"] == "no-store"

    @pytest.mark.parametrize("user", ["pedro", "owner", "", None])
    def test_everyone_else_is_403(self, app, user):
        cli = app.app.test_client()
        assert cli.get("/ask/", headers=hdr(user)).status_code == 403
        r = cli.post("/ask/api", json={"question": "hi"}, headers=hdr(user))
        assert r.status_code == 403 and "not enabled" in r.get_json()["error"]

    def test_username_allow_list_and_case(self, app, monkeypatch):
        monkeypatch.setattr(app, "ALLOWED_USERS", {"pedro"})
        assert app.allowed("Pedro", lambda u: "")
        assert app.allowed("TOMASZ", lambda u: ME.upper() if u == "tomasz" else "")
        assert not app.allowed("mallory", lambda u: "")

    def test_healthz_shows_the_gate(self, app):
        j = app.app.test_client().get("/ask/healthz").get_json()
        assert j["allowed_emails"] == [ME] and "get_generation" in j["tools"]


class TestApi:
    def test_answer_carries_tool_results_and_is_logged(self, app):
        logged = []
        app.app.config["EXEC"] = logged.append
        app.app.config["LLM"] = ScriptedLLM([
            tool_use("get_plant_overview", {"plant": "Taigene"}),
            final("GTO1 (Taigene): PR 0.71 over 30 days."),
        ])
        r = app.app.test_client().post("/ask/api", json={"question": "How is Taigene?"},
                                       headers=hdr("tomasz"))
        assert r.status_code == 200
        j = r.get_json()
        assert j["answer"].startswith("GTO1") and j["user"] == "tomasz"
        assert j["tool_calls"][0]["name"] == "get_plant_overview"
        assert j["tool_calls"][0]["result"]["plant"]["customer"] == "Taigene"
        assert j["tool_calls"][0]["result"]["source"]["daily_kpi_latest_date"] == "2026-09-03"
        assert logged[0].startswith("CREATE TABLE") and "INSERT INTO ask_log" in logged[1]
        assert "'tomasz'" in logged[1] and "How is Taigene?" in logged[1]

    def test_history_is_forwarded_and_capped(self, app):
        llm = ScriptedLLM([final("because INV-04 was silent")])
        app.app.config["LLM"] = llm
        hist = [{"role": "user", "content": f"q{i}"} for i in range(30)]
        r = app.app.test_client().post("/ask/api", json={"question": "why?", "history": hist},
                                       headers=hdr("tomasz"))
        assert r.status_code == 200
        msgs = llm.requests[0]["messages"]
        assert len(msgs) == app.MAX_HISTORY + 1 and msgs[-1]["content"] == "why?"

    def test_bad_requests(self, app):
        cli = app.app.test_client()
        assert cli.post("/ask/api", json={}, headers=hdr("tomasz")).status_code == 400
        assert cli.post("/ask/api", json={"question": "x" * 1001},
                        headers=hdr("tomasz")).status_code == 400
        assert cli.post("/ask/api", data="not json", headers=hdr("tomasz")).status_code == 400

    def test_model_failure_is_502_with_detail(self, app):
        class Boom:
            model = "m"

            def complete(self, *a):
                raise RuntimeError("Anthropic API 529")
        app.app.config["LLM"] = Boom()
        r = app.app.test_client().post("/ask/api", json={"question": "q"}, headers=hdr("tomasz"))
        assert r.status_code == 502 and "529" in r.get_json()["error"]

    def test_missing_api_key_is_503(self, app, monkeypatch):
        app.app.config.pop("LLM", None)
        monkeypatch.setattr(app.agent, "load_api_key",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("no API key")))
        r = app.app.test_client().post("/ask/api", json={"question": "q"}, headers=hdr("tomasz"))
        assert r.status_code == 503 and "no API key" in r.get_json()["error"]

    def test_log_failure_does_not_break_the_answer(self, app):
        def broken(sql):
            raise RuntimeError("psql failed")
        app.app.config["EXEC"] = broken
        app.app.config["LLM"] = ScriptedLLM([final("fine")])
        r = app.app.test_client().post("/ask/api", json={"question": "q"}, headers=hdr("tomasz"))
        assert r.status_code == 200 and r.get_json()["answer"] == "fine"


def test_nginx_and_systemd_wire_the_same_port():
    conf = (BUNDLE / "nginx-argia_session.conf").read_text(encoding="utf-8")
    unit = (BUNDLE / "argia-ask.service").read_text(encoding="utf-8")
    assert "location /ask/" in conf and "127.0.0.1:8513" in conf
    assert "proxy_set_header X-Remote-User $argia_user" in conf.split("location /ask/")[1].split("}")[0]
    assert "ARGIA_ASK_PORT=8513" in unit and "ask_app.py" in unit
    assert f"ARGIA_ASK_EMAILS={ME}" in unit
    assert "ANTHROPIC_API_KEY=" not in unit.replace("ANTHROPIC_API_KEY=...", "")


def test_page_json_is_valid_javascript_strings():
    """The page template is a Python string holding JS — make sure the
    escapes survive: a '\\n' inside the JS must reach the browser as the
    two characters backslash-n, not as a newline inside a JS literal."""
    import ask_app as aa
    assert "'\\n'" in aa.PAGE
    assert json.dumps(aa.MAX_HISTORY)   # numeric substitution, not a str


class TestMe:
    def test_me_tells_the_landing_page_who_may_use_it(self, app):
        cli = app.app.test_client()
        assert cli.get("/ask/me", headers=hdr("tomasz")).get_json() == {"user": "tomasz", "allowed": True}
        assert cli.get("/ask/me", headers=hdr("pedro")).get_json()["allowed"] is False
        assert cli.get("/ask/me").get_json() == {"user": "", "allowed": False}


def test_landing_page_card_is_hidden_until_ask_me_allows():
    src = (BUNDLE / "report_gen.py").read_text(encoding="utf-8")
    assert '.askonly{display:none;}' in src
    assert 'href="ask/" class="askonly"' in src
    assert "fetch('/ask/me'" in src


def test_system_prompt_language_and_brevity_rules():
    from argia.ask import agent as A
    assert "answer in {language}" in A.SYSTEM_TEMPLATE
    assert "answer in Spanish" in A.build_system(FakeDB(dict(BASE)), lang="es")
    assert "Under 120 words" in A.SYSTEM_TEMPLATE
    assert "No headings, no" in A.SYSTEM_TEMPLATE


class TestLanguage:
    def test_lang_reaches_the_system_prompt_and_the_response(self, app):
        llm = ScriptedLLM([final("Taigene fue la peor.")])
        app.app.config["LLM"] = llm
        r = app.app.test_client().post("/ask/api", json={"question": "worst?", "lang": "es"},
                                       headers=hdr("tomasz"))
        assert r.get_json()["lang"] == "es"
        assert "answer in Spanish" in llm.requests[0]["system"]

    def test_unknown_lang_falls_back_to_english(self, app):
        llm = ScriptedLLM([final("ok")])
        app.app.config["LLM"] = llm
        r = app.app.test_client().post("/ask/api", json={"question": "q", "lang": "de"},
                                       headers=hdr("tomasz"))
        assert r.get_json()["lang"] == "en"
        assert "answer in English" in llm.requests[0]["system"]

    def test_page_has_both_languages_and_no_mixed_chips(self, app):
        html = app.app.test_client().get("/ask/", headers=hdr("tomasz")).get_data(as_text=True)
        assert 'data-l="en"' in html and 'data-l="es"' in html
        assert "¿Qué alarmas hay activas ahora?" in html and "Which alarms are active now?" in html
        assert "localStorage.getItem('argia_lang')" in html     # shared with the reports site
