"""CLI:  python3 -m argia.ask "why did GTO1 produce less yesterday?"

Options:
  --user NAME     who is asking (goes to ask_log; default $USER)
  --model ID      Anthropic model id (default: ARGIA_ASK_MODEL or claude-sonnet-5)
  --tool NAME [--arg k=v ...]
                  run ONE tool directly, no model — prints its JSON. Use it
                  to check what the assistant would see: the numbers come
                  from here, the model only phrases them.
  --json          print the whole Answer (tool calls, tokens, latency)
  --no-log        do not write to ask_log

Runs on pio06 only (needs the PostgreSQL peer login).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from argia.ask import agent, tools
from argia.store import pgq


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m argia.ask")
    ap.add_argument("question", nargs="?", default="")
    ap.add_argument("--user", default=os.environ.get("USER", "cli"))
    ap.add_argument("--model", default=agent.DEFAULT_MODEL)
    ap.add_argument("--tool", choices=sorted(tools.DISPATCH))
    ap.add_argument("--arg", action="append", default=[], metavar="k=v")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-log", action="store_true")
    a = ap.parse_args(argv)

    if a.tool:
        params = dict(kv.split("=", 1) for kv in a.arg if "=" in kv)
        print(json.dumps(tools.run_tool(pgq.psql_rows, a.tool, params),
                         indent=2, ensure_ascii=False, default=str))
        return 0
    if not a.question.strip():
        ap.error("a question (or --tool) is required")

    llm = agent.AnthropicLLM(agent.load_api_key(), model=a.model)
    ans = agent.ask(a.question, pgq.psql_rows, llm)
    if not a.no_log:
        try:
            agent.log_answer(pgq.psql_exec, a.user, ans)
        except Exception as e:                    # noqa: BLE001
            print(f"[ask_log] not written: {e}", file=sys.stderr)
    if a.json:
        print(json.dumps(ans.as_dict(), indent=2, ensure_ascii=False, default=str))
        return 1 if ans.error else 0
    if ans.error:
        print(f"ERROR: {ans.error}", file=sys.stderr)
    print(ans.text)
    used = ", ".join(f"{c['name']}({', '.join(f'{k}={v}' for k, v in c['input'].items())})"
                     for c in ans.tool_calls) or "none"
    print(f"\n-- tools: {used}\n-- {ans.model}, {ans.turns} turn(s), "
          f"{ans.input_tokens}+{ans.output_tokens} tokens, {ans.latency_ms} ms",
          file=sys.stderr)
    return 1 if ans.error else 0


if __name__ == "__main__":
    sys.exit(main())
