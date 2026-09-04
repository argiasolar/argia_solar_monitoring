# Ask ARGIA — phase 0 runbook (v187, 2026-09-04)

A chat assistant over the monitoring database. Read-only. One user
(tomasz.zemelka@argia.com.mx). Anthropic Messages API, model
`claude-sonnet-5` by default.

What it is NOT: new monitoring. Detection stays in `monitor.py` and the
mailer. This is an interface for questions the dashboards answer slowly
("why", "compare", "how much did it cost").

## Pieces

| File | Role |
|---|---|
| `argia/ask/tools.py` | 8 read-only functions over PG, fixed SQL, validated inputs. **The numbers come from here.** |
| `argia/ask/agent.py` | tool-calling loop, Anthropic client (`requests`, no SDK), `ask_log` writer |
| `argia/ask/__main__.py` | CLI: `python3 -m argia.ask "question"` and `--tool NAME` (no model) |
| `server/bundle/ask_app.py` | Flask on 127.0.0.1:8513, `/ask/` page + `/ask/api`, allow-list by e-mail |
| `server/bundle/argia-ask.service` | systemd unit |
| `server/bundle/nginx-argia_session.conf` | `location /ask/` → 8513, behind the session login |
| `server/bundle/auth_core.py` | `/ask/` = any signed-in user at nginx; the app does the real gate |
| `tests/unit/test_ask_*.py`, `tests/fixtures/ask_golden.json` | 53 tests; 9 golden cases offline + live |

Tools: `get_portfolio_overview`, `get_plant_overview`, `get_generation`,
`get_performance`, `get_inverter_performance`, `get_active_alarms`,
`get_alarm_history`, `get_lost_generation`.

Tables read: plant, inverter, telemetry, daily_production, alert_state,
maintenance_event. Table written: `ask_log` (created on first use).

## Deploy on pio06

```bash
# 0. API key — console.anthropic.com → API keys → create, name "argia-ask".
#    Root-only file, never in the repo, env or a unit file:
install -m 600 /dev/null /root/.argia_ask
printf 'ANTHROPIC_API_KEY=%s\n' 'sk-ant-PASTE-HERE' > /root/.argia_ask
grep -c ANTHROPIC_API_KEY /root/.argia_ask          # expect 1

# 1. code
cd /root/argia_v2 && git fetch origin && git checkout ask-phase0 && git pull
cd v2 && python3 -m pytest tests/unit/test_ask_tools.py tests/unit/test_ask_agent.py tests/unit/test_ask_app.py -q
#    expect: 53 passed, 9 skipped (the live golden set)

# 2. the tools against the real database — no model, no key needed
python3 -m argia.ask --tool get_portfolio_overview | head -40
python3 -m argia.ask --tool get_generation --arg plant=GTO1 --arg date_from=2026-09-01 --arg date_to=2026-09-03
python3 -m argia.ask --tool get_inverter_performance --arg plant=GTO1 --arg date=yesterday
python3 -m argia.ask --tool get_plant_overview --arg plant=Prologis      # expect {"error": "unknown plant ... Known plants: ..."}

# 3. first real question (writes ask_log)
python3 -m argia.ask --user tomasz "Why did GTO1 produce less yesterday?"
runuser -u postgres -- psql -d argia_mont -c \
  "SELECT ts, username, left(question,50) q, tools, input_tokens+output_tokens tok, latency_ms FROM ask_log ORDER BY id DESC LIMIT 3;"

# 4. web
B=/root/argia_v2/v2/server/bundle
cp $B/ask_app.py $B/auth_core.py /opt/argia/bundle/
cp $B/argia-ask.service /etc/systemd/system/
cp $B/nginx-argia_session.conf /etc/nginx/snippets/argia_auth.conf
systemctl daemon-reload
systemctl enable --now argia-ask.service
systemctl restart argia-auth.service                   # picks up the /ask/ area
nginx -t && systemctl reload nginx

# 5. verify
systemctl is-active argia-ask argia-auth               # active active
curl -s http://127.0.0.1:8513/ask/healthz              # {"ok":true,"allowed_emails":["tomasz.zemelka@argia.com.mx"],...}
curl -s -o /dev/null -w '%{http_code}\n' https://report.argia.com.mx/ask/     # 302 (anonymous → login)
sqlite3 /opt/argia/auth/users.db "SELECT username,email FROM users WHERE lower(email)='tomasz.zemelka@argia.com.mx';"
#    must return your account. If empty: set the e-mail in /setup/ (edit user),
#    or add Environment=ARGIA_ASK_USERS=<your username> to the unit and restart.
```

Then open https://report.argia.com.mx/ask/ signed in as yourself. Any
other account gets 403.

## Cost

Sonnet 5 is USD 2 / 10 per million input / output tokens. A question is
roughly 3–8 k input tokens (system prompt + tool JSON) and a few hundred
output — about USD 0.01–0.03 per question. `ask_log` records tokens per
answer; `SELECT sum(input_tokens)*2e-6 + sum(output_tokens)*1e-5 FROM ask_log;`
is the bill so far in USD.

## Regression

- Offline (every CI run): `test_golden_offline` — the scripted model
  follows each case's tool plan and the harness must hand it every figure
  the answer needs. Add a case whenever a real question goes wrong.
- Live (on demand, costs a few cents):
  `ARGIA_ASK_LIVE=1 ANTHROPIC_API_KEY=sk-ant-... python3 -m pytest tests/unit/test_ask_agent.py -k golden_live -m integration -q`
  asserts the real model picked the planned tools and quoted the planned
  numbers (and did not invent the ones in `must_not_quote`). Wording is
  never asserted.

## Known limits (phase 0)

- Alarm history is what `alert_state` kept; it has no per-alarm plant
  column, so plant filtering is `key LIKE '%GTO1%'`.
- `get_lost_generation` = expected − actual on the days below expectation.
  That includes soiling, curtailment and data gaps, not only downtime.
  The tool says so in its `note`; the model is told to pass it on.
- Follow-up context is the last 12 text turns held by the browser tab;
  reloading the page forgets it.
- No documents, no charts, no writes. Finance tools only after phase 0
  has proven useful.
