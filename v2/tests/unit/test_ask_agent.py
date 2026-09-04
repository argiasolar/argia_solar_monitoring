"""Unit tests — argia.ask.agent with a scripted LLM and the fake DB.

Also the golden regression set (tests/fixtures/ask_golden.json): each
question names the tools a correct answer needs and the figures it must
quote. Offline, a scripted model follows the plan and we check the
harness delivers those figures. With ARGIA_ASK_LIVE=1 and an API key
the same set runs against the real model (marked integration).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re

import pytest

from argia.ask import agent as A
from argia.ask import tools as T
from tests.conftest import load_fixture
from tests.unit.test_ask_tools import BASE, FakeDB


# ------------------------------------------------------------- scripted LLM
class ScriptedLLM:
    """Replays a list of responses; records every request."""
    model = "fake-model"

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def complete(self, system, messages, tools):
        self.requests.append({"system": system, "messages": copy.deepcopy(messages),
                              "tools": tools})
        if not self.responses:
            raise AssertionError("scripted LLM ran out of responses")
        return self.responses.pop(0)


def tool_use(name, inp, uid="tu1"):
    return {"stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 20},
            "content": [{"type": "text", "text": "checking"},
                        {"type": "tool_use", "id": uid, "name": name, "input": inp}]}


def final(text):
    return {"stop_reason": "end_turn", "usage": {"input_tokens": 300, "output_tokens": 80},
            "content": [{"type": "text", "text": text}]}


@pytest.fixture
def db():
    d = FakeDB(dict(BASE))
    d.data["daily_range"] = [["2026-09-03", "1843", "2110", "5.2", "0.71", "0.894", "A", ""]]
    d.data["perf"] = [["GTO1", "1843", "2110", "1843", "0.71", "0.894", "1", "1", "5.2"]]
    d.data["inverters"] = [["SN3", "INV-03", "50"], ["SN4", "INV-04", "50"]]
    d.data["inverter_day"] = [["SN3", "250", "1", "", "40000", "16:00", "120"]]
    return d


# ------------------------------------------------------------------- loop
def test_ask_runs_tools_then_answers(db):
    llm = ScriptedLLM([
        tool_use("get_generation", {"plant": "GTO1", "date_from": "2026-09-03",
                                    "date_to": "2026-09-03"}, "a"),
        tool_use("get_inverter_performance", {"plant": "GTO1", "date": "2026-09-03"}, "b"),
        final("GTO1 made 1843 kWh vs 2110 expected (87.3%); INV-04 was silent."),
    ])
    ans = A.ask("why did GTO1 produce less yesterday?", db, llm)
    assert ans.error is None and ans.turns == 3
    assert ans.tools_used() == ["get_generation", "get_inverter_performance"]
    assert ans.tool_calls[0]["result"]["totals"]["vs_expected_pct"] == 87.3
    assert ans.tool_calls[1]["result"]["flags"]["silent"] == ["INV-04"]
    assert "87.3%" in ans.text
    assert ans.input_tokens == 500 and ans.output_tokens == 120
    assert ans.model == "fake-model" and ans.latency_ms >= 0
    # tool_result ids match the tool_use ids, and the transcript alternates
    msgs = llm.requests[-1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert msgs[2]["content"][0]["tool_use_id"] == "a"
    assert msgs[4]["content"][0]["tool_use_id"] == "b"
    assert json.loads(msgs[2]["content"][0]["content"])["plant_key"] == "GTO1"


def test_ask_passes_system_with_vocabulary_and_today(db):
    llm = ScriptedLLM([final("ok")])
    A.ask("hi", db, llm)
    sysmsg = llm.requests[0]["system"]
    assert "GTO1 = Taigene (GROWATT, 500 kWp, PPA)" in sysmsg
    assert "OLD1 = Gone Co (GROWATT, 100 kWp, PPA, INACTIVE)" in sysmsg
    assert dt.datetime.now(T.MX).date().isoformat() in sysmsg
    assert llm.requests[0]["tools"] == T.TOOLS


def test_ask_tool_error_goes_back_to_model(db):
    llm = ScriptedLLM([
        tool_use("get_plant_overview", {"plant": "Prologis"}),
        final("I don't know a plant called Prologis."),
    ])
    ans = A.ask("how is Prologis?", db, llm)
    assert ans.error is None
    assert "unknown plant" in ans.tool_calls[0]["result"]["error"]
    body = json.loads(llm.requests[1]["messages"][2]["content"][0]["content"])
    assert "GTO1=Taigene" in body["error"]


def test_ask_history_keeps_only_text_turns(db):
    llm = ScriptedLLM([final("GTO1.")])
    hist = [{"role": "user", "content": "worst plant last month?"},
            {"role": "assistant", "content": "GTO1 at 87.3%."},
            {"role": "assistant", "content": [{"type": "tool_use"}]},   # dropped
            {"role": "system", "content": "ignored"},                    # dropped
            {"role": "user", "content": ""}]                             # dropped
    A.ask("why?", db, llm, history=hist)
    msgs = llm.requests[0]["messages"]
    assert [m["content"] for m in msgs] == ["worst plant last month?", "GTO1 at 87.3%.", "why?"]


def test_ask_gives_up_after_max_turns(db):
    llm = ScriptedLLM([tool_use("get_active_alarms", {}, f"t{i}") for i in range(5)])
    ans = A.ask("loop", db, llm, max_turns=3)
    assert ans.turns == 3 and "no final answer" in ans.error
    assert len(ans.tool_calls) == 3


def test_ask_llm_failure_is_captured(db):
    class Boom:
        model = "x"

        def complete(self, *a):
            raise RuntimeError("Anthropic API 529: overloaded")
    ans = A.ask("q", db, Boom())
    assert ans.text == "" and "529" in ans.error


# -------------------------------------------------------------------- log
def test_log_sql_quotes_and_keeps_tool_inputs_only():
    ans = A.Answer(question="what's up with GTO1's INV-04?", text="it's silent",
                   model="m", input_tokens=1, output_tokens=2, latency_ms=3, turns=1)
    ans.tool_calls = [{"name": "get_plant_overview", "input": {"plant": "GTO1"},
                       "result": {"huge": "x" * 10000}}]
    sql = A.log_sql("tomasz", ans)
    assert "what''s up with GTO1''s INV-04?" in sql and "it''s silent" in sql
    assert '"plant": "GTO1"' in sql and "huge" not in sql
    assert sql.rstrip().endswith("NULL);")            # no error


def test_log_answer_creates_table_then_inserts():
    seen = []
    A.log_answer(seen.append, "u", A.Answer(question="q", text="a"))
    assert seen[0].startswith("CREATE TABLE IF NOT EXISTS ask_log")
    assert seen[1].startswith("INSERT INTO ask_log")


# ----------------------------------------------------------------- api key
def test_load_api_key_env_then_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert A.load_api_key(str(tmp_path / "none")) == "sk-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    p = tmp_path / ".argia_ask"
    p.write_text("# key\nANTHROPIC_API_KEY='sk-file'\n")
    assert A.load_api_key(str(p)) == "sk-file"
    with pytest.raises(RuntimeError, match="no API key"):
        A.load_api_key(str(tmp_path / "missing"))


def test_workspace_id_header_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    p = tmp_path / ".argia_ask"
    p.write_text("ANTHROPIC_API_KEY=sk-file\n")
    monkeypatch.setattr(A, "KEY_FILE", str(p))
    assert A.load_workspace_id(str(p)) == ""
    assert "anthropic-workspace-id" not in A.AnthropicLLM("k", workspace_id="").headers()
    p.write_text("ANTHROPIC_API_KEY=sk-file\nANTHROPIC_WORKSPACE_ID=wrkspc_01ABC\n")
    assert A.load_workspace_id(str(p)) == "wrkspc_01ABC"
    h = A.AnthropicLLM("k", workspace_id=A.load_workspace_id(str(p))).headers()
    assert h["anthropic-workspace-id"] == "wrkspc_01ABC" and h["x-api-key"] == "k"


# ------------------------------------------------------------- golden set
GOLDEN = load_fixture("ask_golden.json")


def _golden_db():
    d = FakeDB(dict(BASE))
    d.data.update({k: v for k, v in GOLDEN["db"].items()})
    d.data["perf"] = BASE["perf"]
    return d


def _numbers_present(text, must):
    text = text.replace(",", "")
    return [m for m in must if str(m) not in text]


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=[c["id"] for c in GOLDEN["cases"]])
def test_golden_offline(case):
    """The scripted model follows the case's plan; the harness must hand
    it every figure the answer is required to quote."""
    db = _golden_db()
    plan = [tool_use(t["name"], t["input"], f"t{i}") for i, t in enumerate(case["plan"])]
    # the fake's final answer is the tool results themselves — if a required
    # figure is not in them, nothing honest could have quoted it
    llm = ScriptedLLM(plan + [final("")])
    ans = A.ask(case["question"], db, llm)
    assert ans.error is None
    assert ans.tools_used() == [t["name"] for t in case["plan"]]
    payload = T.to_json([c["result"] for c in ans.tool_calls])
    missing = _numbers_present(payload, case["must_quote"])
    assert not missing, f"tool results lack {missing}"


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("ARGIA_ASK_LIVE"),
                    reason="set ARGIA_ASK_LIVE=1 (+ANTHROPIC_API_KEY) to call the model")
@pytest.mark.parametrize("case", GOLDEN["cases"], ids=[c["id"] for c in GOLDEN["cases"]])
def test_golden_live(case):
    """Real model, fake data: did it pick the right tools and quote the
    right numbers? Wording is not asserted."""
    db = _golden_db()
    llm = A.AnthropicLLM(A.load_api_key(), model=os.environ.get("ARGIA_ASK_MODEL",
                                                                A.DEFAULT_MODEL))
    ans = A.ask(case["question"], db, llm,
                system=A.build_system(db, today=dt.date.fromisoformat(GOLDEN["today"])))
    assert ans.error is None, ans.error
    want = {t["name"] for t in case["plan"]}
    assert want <= set(ans.tools_used()), f"used {ans.tools_used()}, wanted {want}"
    missing = _numbers_present(ans.text, case["must_quote"])
    assert not missing, f"answer lacks {missing}:\n{ans.text}"
    for bad in case.get("must_not_quote", []):
        assert not re.search(bad, ans.text), f"answer invented {bad!r}:\n{ans.text}"
