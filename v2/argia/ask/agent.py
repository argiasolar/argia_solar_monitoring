"""The tool-calling loop and the Anthropic Messages client.

``ask()`` is provider-agnostic: it takes any object with
``complete(system, messages, tools) -> response`` where the response has
the Anthropic Messages shape (``content`` blocks, ``stop_reason``,
``usage``). Tests drive it with a scripted fake; production uses
``AnthropicLLM`` — a thin ``requests`` call, no SDK to install on pio06.

What the model may do: choose tools, read their JSON, write the answer.
What it may not do: invent numbers. The system prompt says so, and the
portal renders the tool results next to the answer so a reader can
check every figure against its source.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from argia.ask.tools import MX, TOOLS, plants, run_tool, to_json

DEFAULT_MODEL = os.environ.get("ARGIA_ASK_MODEL", "claude-sonnet-5")
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
KEY_FILE = os.environ.get("ARGIA_ASK_KEYFILE", "/root/.argia_ask")
MAX_TURNS = 8
MAX_TOKENS = 4000

SYSTEM_TEMPLATE = """You are Ask ARGIA, the assistant of Argia Solar's PV fleet monitoring \
(Zapopan, Mexico). You answer questions about the fleet using ONLY the tools \
provided. Today is {today} (America/Mexico_City).

Fleet vocabulary (name = plant_key, brand, kWp DC, portfolio):
{vocab}

NAMES: in your answer always call a plant by its name (Taigene, SAG, \
Vitalmex...), never by its key (GTO1, MEX2). Keys exist only as tool inputs \
and tool outputs carry the name in the "name" field.

Rules:
1. Every number in your answer must come from a tool result in this \
conversation. Never estimate, extrapolate or recall figures. If a tool \
returns an error or empty data, say exactly what is missing instead of guessing.
2. Prefer the fixed tools (they carry the reports' own rules); add calls only \
when the question needs them (e.g. "why" -> generation, then inverter \
performance and alarm history for the bad days). When no fixed tool covers \
the question, call describe_tables, then query_database with ONE read-only \
SELECT that aggregates in SQL — never page through raw rows.
3. Say which period and which data freshness the answer is based on when it \
matters. daily_production is stamped once a day by the KPI job; telemetry is \
5-minute live data.
4. Money comes ONLY from get_revenue (earned) or get_lost_generation (missed). \
Never multiply kWh by a tariff yourself, never add up plants yourself — if \
a total is needed, call the tool without a plant. CAPEX plants earn nothing.
5. TABLES: whenever the answer carries numbers for more than one item (plants, \
days, inverters, months, charges), present them as a pipe table, one row per \
item, and END THE TABLE WITH A SUMMARY ROW (label it TOTAL, or AVERAGE for \
ratios) taken from the tool's "totals" field — never computed by you. The \
tool says whether a figure is a sum or a kWp-weighted mean; keep that. Then \
one or two sentences with the finding. No headings, no emojis, no bullet \
lists of everything you saw. Under 150 words of prose.
6. LANGUAGE: answer in {language}, whatever language the question is in.
7. Do not list sources yourself — the interface shows the tool results you used.
8. THE STANDARD: for anything about design rules, requirements, tolerances, \
checklists, terminology or "what does the standard/AGS say", call \
search_standard (in the answer language) and quote or paraphrase the slide, \
citing it as "ARGIA Golden Standard, slide N — title". Never answer standard \
questions from memory.
9. Read-only: if asked to change, create or send anything, say it is not \
available yet.{scope_note}"""

SCOPE_NOTE = """
10. This account sees only these plants: {plants}. Answer only about them; \
say so if asked about others. Fleet totals and money are not available."""


LANGUAGES = {"en": "English", "es": "Spanish"}

# v219 (Tomasz: "when I use English my reference links must point to the EN
# version"): the answer language follows the QUESTION, the interface
# language is only the tiebreaker. Cheap, deterministic word evidence.
_ES_WORDS = frozenset("el la los las de del que qué cuánto cuánta cuántos cuántas cuál cuáles cómo dónde cuándo por para con "
                      "planta plantas energía hoy ayer mes semana año este esta estos estas fue son está están hay "
                      "producción rendimiento inversor inversores alerta alertas norma estándar dice sobre entre y o "
                      "muestra muéstrame dame lista cuanto cual como donde cuando produccion energia".split())
_EN_WORDS = frozenset("the a an of what which how much many where when why is are was were does do did show list give me "
                      "plant plants energy today yesterday month week year this these those and or for with about "
                      "production yield inverter inverters alert alerts standard says between last total please".split())
_WORD = re.compile(r"[a-záéíóúñü]+", re.I)


def detect_lang(question: str, fallback: str = "en") -> str:
    """'en' or 'es' from the words of the question; ``fallback`` (the
    interface language) only when the evidence is equal. Pure."""
    fb = (fallback or "en").lower()
    fb = fb if fb in LANGUAGES else "en"
    q = question or ""
    es = sum(1 for w in _WORD.findall(q.lower()) if w in _ES_WORDS) + q.count("¿") + q.count("¡")
    en = sum(1 for w in _WORD.findall(q.lower()) if w in _EN_WORDS)
    if es > en:
        return "es"
    if en > es:
        return "en"
    return fb


def build_system(rows: Callable[[str], List[List[str]]],
                 today: Optional[dt.date] = None, lang: str = "en",
                 scope: Optional[set] = None) -> str:
    today = today or dt.datetime.now(MX).date()
    language = LANGUAGES.get((lang or "en").lower(), "English")
    ps = plants(rows)
    vocab = "\n".join(
        f"  {p['name']} = {k} ({p['brand']}, {p['kwp_dc']:g} kWp, "
        f"{p['portfolio'] or 'n/a'}{'' if p['active'] else ', INACTIVE'})"
        for k, p in ps.items() if scope is None or k in scope)
    scope_note = ""
    if scope is not None:
        names = ", ".join(ps[k]["name"] for k in sorted(scope) if k in ps) or "none"
        scope_note = SCOPE_NOTE.format(plants=names)
    return SYSTEM_TEMPLATE.format(today=today.isoformat(), vocab=vocab,
                                  language=language, scope_note=scope_note)


# ------------------------------------------------------------------ client
def _keyfile(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if "=" in ln and not ln.startswith("#"):
                    k, v = ln.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_api_key(path: str = KEY_FILE) -> str:
    """ANTHROPIC_API_KEY from the environment, else from ``path`` (a
    root-only file with ``ANTHROPIC_API_KEY=...``). Never logged."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or \
        _keyfile(path).get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(f"no API key: set ANTHROPIC_API_KEY or put "
                           f"ANTHROPIC_API_KEY=... in {path}")
    return key


def load_workspace_id(path: str = KEY_FILE) -> str:
    """Optional: a Console key that is not scoped to a workspace must
    send ``anthropic-workspace-id``. ANTHROPIC_WORKSPACE_ID from the
    environment or the key file; '' when the key is workspace-scoped."""
    return (os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
            or _keyfile(path).get("ANTHROPIC_WORKSPACE_ID", ""))


class AnthropicLLM:
    """Messages API over ``requests``; one call per ``complete``."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 max_tokens: int = MAX_TOKENS, timeout: int = 90,
                 workspace_id: str = ""):
        self.api_key, self.model = api_key, model
        self.max_tokens, self.timeout = max_tokens, timeout
        self.workspace_id = workspace_id or load_workspace_id()

    def headers(self) -> Dict[str, str]:
        h = {"x-api-key": self.api_key, "anthropic-version": API_VERSION,
             "content-type": "application/json"}
        if self.workspace_id:
            h["anthropic-workspace-id"] = self.workspace_id
        return h

    def complete(self, system: str, messages: List[dict], tools: List[dict]) -> dict:
        import requests                          # lazy: tests never need it
        body = {"model": self.model, "max_tokens": self.max_tokens,
                "system": system, "messages": messages, "tools": tools}
        r = requests.post(API_URL, json=body, timeout=self.timeout,
                          headers=self.headers())
        if r.status_code != 200:
            raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:300]}")
        return r.json()


# -------------------------------------------------------------------- loop
@dataclass
class Answer:
    question: str
    text: str = ""
    tool_calls: List[dict] = field(default_factory=list)   # name, input, result
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    turns: int = 0
    error: Optional[str] = None
    lang: str = "en"           # v219: the language the answer was asked for

    def tools_used(self) -> List[str]:
        return [c["name"] for c in self.tool_calls]

    def as_dict(self) -> dict:
        return {"question": self.question, "answer": self.text,
                "tool_calls": self.tool_calls, "model": self.model,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "latency_ms": self.latency_ms, "turns": self.turns,
                "error": self.error, "lang": self.lang}


def ask(question: str, rows: Callable[[str], List[List[str]]], llm: Any,
        history: Optional[List[dict]] = None, max_turns: int = MAX_TURNS,
        system: Optional[str] = None, lang: str = "en",
        scope: Optional[set] = None) -> Answer:
    """Run the question to a final text answer.

    ``history`` is prior turns as ``[{"role": "user"|"assistant",
    "content": str}, ...]`` — final texts only, so a follow-up ("why?")
    keeps its context without replaying every tool payload.
    """
    t0 = time.monotonic()
    lang = detect_lang(question, lang)          # v219: the question decides
    ans = Answer(question=question, model=getattr(llm, "model", ""), lang=lang)
    system = system or build_system(rows, lang=lang, scope=scope)
    messages: List[dict] = [m for m in (history or [])
                            if m.get("role") in ("user", "assistant")
                            and isinstance(m.get("content"), str) and m["content"]]
    messages.append({"role": "user", "content": question})
    try:
        for _ in range(max_turns):
            ans.turns += 1
            resp = llm.complete(system, messages, TOOLS)
            usage = resp.get("usage") or {}
            ans.input_tokens += int(usage.get("input_tokens") or 0)
            ans.output_tokens += int(usage.get("output_tokens") or 0)
            content = resp.get("content") or []
            messages.append({"role": "assistant", "content": content})
            uses = [b for b in content if b.get("type") == "tool_use"]
            if resp.get("stop_reason") != "tool_use" or not uses:
                ans.text = "\n".join(b.get("text", "") for b in content
                                     if b.get("type") == "text").strip()
                if not ans.text:
                    ans.error = (f"model returned no text (stop_reason="
                                 f"{resp.get('stop_reason')!r})")
                elif resp.get("stop_reason") == "max_tokens":
                    ans.error = "answer cut off (max_tokens)"
                break
            results = []
            for b in uses:
                result = run_tool(rows, b.get("name", ""), b.get("input") or {}, scope=scope, lang=lang)
                ans.tool_calls.append({"name": b.get("name"), "input": b.get("input") or {},
                                       "result": result})
                results.append({"type": "tool_result", "tool_use_id": b.get("id"),
                                "content": to_json(result)})
            messages.append({"role": "user", "content": results})
        else:
            ans.error = f"no final answer after {max_turns} turns"
    except Exception as e:                        # noqa: BLE001 — surfaced, logged
        ans.error = f"{type(e).__name__}: {e}"
    ans.latency_ms = int((time.monotonic() - t0) * 1000)
    return ans


# --------------------------------------------------------------------- log
ENSURE_LOG_SQL = """CREATE TABLE IF NOT EXISTS ask_log (
    id            serial PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    username      text NOT NULL DEFAULT '',
    question      text NOT NULL,
    answer        text,
    tools         jsonb,
    model         text,
    input_tokens  int,
    output_tokens int,
    latency_ms    int,
    turns         int,
    error         text
);"""


def _q(s: Any) -> str:
    return "NULL" if s is None else "'" + str(s).replace("'", "''") + "'"


def log_sql(username: str, ans: Answer) -> str:
    """INSERT for the Q/A log. Tool inputs are kept; results are not (they
    are reproducible from the question and the date), so the log stays
    small and holds no bulk data."""
    tools = [{"name": c["name"], "input": c["input"],
              "error": (c["result"] or {}).get("error")} for c in ans.tool_calls]
    return ("INSERT INTO ask_log (username, question, answer, tools, model,"
            " input_tokens, output_tokens, latency_ms, turns, error) VALUES ("
            f"{_q(username)}, {_q(ans.question)}, {_q(ans.text)}, "
            f"{_q(json.dumps(tools, ensure_ascii=False))}::jsonb, {_q(ans.model)}, "
            f"{ans.input_tokens}, {ans.output_tokens}, {ans.latency_ms}, "
            f"{ans.turns}, {_q(ans.error)});")


def log_answer(exec_: Callable[[str], None], username: str, ans: Answer) -> None:
    exec_(ENSURE_LOG_SQL)
    exec_(log_sql(username, ans))
