#!/usr/bin/env python3
"""Ask ARGIA — the /ask/ page of report.argia.com.mx (phase 0).

Runs on 127.0.0.1:8513; nginx proxies /ask/ to it behind the session
login and passes the signed-in username in X-Remote-User. This app
never authenticates anyone — nginx and auth_app do. It only decides
WHO may use the assistant, and that list is explicit:

  ARGIA_ASK_USERS   comma-separated usernames          (default: none)
  ARGIA_ASK_EMAILS  comma-separated e-mails, matched against the
                    account's e-mail in users.db
                    (default: tomasz.zemelka@argia.com.mx)

Everyone else gets 403 — signed in, but not for this page — the same
answer the rest of the site gives. Every question and answer is logged
to ask_log in PostgreSQL with the tools that produced it.

Routes:
  GET  /ask/       the chat page
  POST /ask/api    {"question": str, "history": [{"role","content"}]}
                   -> {"answer", "tool_calls", "model", ...}
  GET  /ask/healthz
"""
import html
import json
import os
import sys

from flask import Flask, request, jsonify, make_response

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get('ARGIA_V2_DIR', '/root/argia_v2/v2'))

try:
    from argia_logo import LOGO_URI
except ImportError:
    LOGO_URI = ''

from argia.ask import agent, tools                 # noqa: E402
from argia.store import pgq                        # noqa: E402

ALLOWED_USERS = {u.strip().lower() for u in
                 os.environ.get('ARGIA_ASK_USERS', '').split(',') if u.strip()}
ALLOWED_EMAILS = {e.strip().lower() for e in
                  os.environ.get('ARGIA_ASK_EMAILS',
                                 'tomasz.zemelka@argia.com.mx').split(',')
                  if e.strip()}
MAX_QUESTION = 1000
MAX_HISTORY = 12                 # turns kept for follow-ups ("why?")

app = Flask(__name__)


# ------------------------------------------------------------- identity
def email_of(username):
    """The account's e-mail from users.db, '' when unknown. Imported
    lazily so the unit tests can run without the setup app's paths."""
    try:
        import setup_app as sa
        return (sa.profile_of(username)[1] or '').strip().lower()
    except Exception:                                # noqa: BLE001
        return ''


def allowed(username, email_lookup=None):
    email_lookup = email_lookup or email_of
    u = (username or '').strip().lower()
    if not u:
        return False
    if u in ALLOWED_USERS:
        return True
    email = (email_lookup(u) or '').strip().lower()
    return bool(ALLOWED_EMAILS) and email in ALLOWED_EMAILS


def actor():
    return (request.headers.get('X-Remote-User') or '').strip()


# ----------------------------------------------------------------- page
PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ask ARGIA</title>
<style>
body{margin:0;font-family:"Segoe UI",system-ui,sans-serif;background:#f6f7f8;color:#1c2733}
header{display:flex;align-items:center;gap:14px;padding:12px 20px;background:#fff;border-bottom:1px solid #e0e3e7}
header img{height:28px}header .t{font-weight:600}header .who{margin-left:auto;font-size:13px;color:#667}
header a{color:#1c2733;font-size:13px;margin-left:14px}
main{max-width:900px;margin:0 auto;padding:18px 16px 120px}
.msg{margin:12px 0;padding:12px 14px;border-radius:12px;line-height:1.45;white-space:pre-wrap}
.q{background:#1c2733;color:#fff;margin-left:15%}
.a{background:#fff;border:1px solid #e0e3e7;margin-right:10%}
.a.err{background:#fce8e6;border-color:#f3b8b2;color:#a50e0e}
.src{font-size:12px;color:#667;margin:4px 0 0 4px}
details{font-size:12px;margin:6px 0 0 4px}details summary{cursor:pointer;color:#456}
table{border-collapse:collapse;font-size:12px;margin:6px 0;max-width:100%;display:block;overflow-x:auto}
th,td{border:1px solid #e0e3e7;padding:3px 6px;text-align:right;white-space:nowrap}
th{background:#f0f2f4;text-align:center}td:first-child,th:first-child{text-align:left}
pre{font-size:11px;background:#f0f2f4;padding:8px;border-radius:8px;overflow:auto;max-height:320px}
form{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #e0e3e7;padding:12px 16px}
.row{max-width:900px;margin:0 auto;display:flex;gap:8px}
input{flex:1;border:1px solid #c9ced4;border-radius:8px;padding:11px;font-size:15px;font-family:inherit}
button{background:#1c2733;color:#fff;border:0;border-radius:8px;padding:11px 18px;font-size:15px;font-family:inherit;cursor:pointer}
button:disabled{opacity:.5}
.hint{max-width:900px;margin:6px auto 0;font-size:12px;color:#667}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.chips span{background:#fff;border:1px solid #c9ced4;border-radius:14px;padding:4px 10px;font-size:13px;cursor:pointer}
</style></head><body>
<header>__LOGO__<span class="t">Ask ARGIA</span><span style="font-size:12px;color:#889;border:1px solid #c9ced4;border-radius:10px;padding:1px 7px">phase 0 · read-only</span>
<span class="who">__USER__</span><a href="/monitoring/">Monitoring</a><a href="/">Reports</a></header>
<main id="log">
<div class="a msg">Ask about the fleet — production, expected, PR, availability, inverters, alarms, lost energy. Every figure comes from the monitoring database; the tool results are shown under each answer so you can check them. Follow-ups ("why?", "and in July?") keep the context.
<div class="chips" id="chips"></div></div>
</main>
<form id="f"><div class="row"><input id="q" autocomplete="off" placeholder="e.g. Why did GTO1 produce less yesterday?" maxlength="__MAXQ__"><button id="b">Ask</button></div>
<div class="hint">Model: __MODEL__ · answers are logged</div></form>
<script>
const EX=["Anything I should worry about today?","Why did GTO1 produce less yesterday?","Which plant performed worst last month?","¿Qué alarmas hay activas ahora?","How much did MEX2's shortfall cost in August?","Compare August with July for the PPA plants"];
const chips=document.getElementById('chips');EX.forEach(t=>{const s=document.createElement('span');s.textContent=t;s.onclick=()=>{q.value=t;f.requestSubmit();};chips.appendChild(s);});
const log=document.getElementById('log'),f=document.getElementById('f'),q=document.getElementById('q'),b=document.getElementById('b');
let history=[];
function el(cls,txt){const d=document.createElement('div');d.className='msg '+cls;d.textContent=txt;log.appendChild(d);window.scrollTo(0,document.body.scrollHeight);return d;}
function fmt(v){if(v===null||v===undefined)return '—';if(typeof v==='number')return Number.isInteger(v)?v.toString():v.toFixed(Math.abs(v)<10?2:1);if(Array.isArray(v))return v.length?v.join(', '):'—';if(typeof v==='object')return JSON.stringify(v);return String(v);}
function table(rows){if(!rows.length||typeof rows[0]!=='object')return null;const cols=Object.keys(rows[0]);const t=document.createElement('table');t.innerHTML='<tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+fmt(r[c]).replace(/</g,'&lt;')+'</td>').join('')+'</tr>').join('');return t;}
function render(call){const d=document.createElement('details');const args=Object.entries(call.input).map(([k,v])=>k+'='+v).join(', ');
 const s=document.createElement('summary');s.textContent=call.name+'('+args+')'+(call.result&&call.result.error?' — error':'');d.appendChild(s);
 const r=call.result||{};let shown=false;
 for(const k of ['plants','days','inverters','alarms','inverter_faults','worst_days','maintenance','open_maintenance','active_alarms']){if(Array.isArray(r[k])&&r[k].length){const h=document.createElement('div');h.textContent=k;h.style.cssText='font-weight:600;margin-top:6px';d.appendChild(h);const t=table(r[k]);if(t){d.appendChild(t);shown=true;}}}
 const rest={};for(const k in r)if(!Array.isArray(r[k])||!r[k].length||typeof r[k][0]!=='object')rest[k]=r[k];
 const p=document.createElement('pre');p.textContent=JSON.stringify(rest,null,1);d.appendChild(p);return d;}
f.onsubmit=async e=>{e.preventDefault();const text=q.value.trim();if(!text)return;el('q',text);q.value='';b.disabled=true;const wait=el('a','…');
 try{const res=await fetch('/ask/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:text,history})});
  const j=await res.json();wait.remove();
  if(!res.ok||j.error){el('a err',(j.error||('HTTP '+res.status))+(j.answer?'\\n'+j.answer:''));}
  else{const d=el('a',j.answer||'(no answer)');const src=document.createElement('div');src.className='src';
   const used=(j.tool_calls||[]).map(c=>c.name);const fr=(j.tool_calls||[]).map(c=>c.result&&c.result.source).find(s=>s)||{};
   src.textContent='Sources: '+(used.length?used.join(', '):'none')+(fr.telemetry_latest_utc?' · telemetry '+fr.telemetry_latest_utc+' UTC':'')+(fr.daily_kpi_latest_date?' · daily KPI through '+fr.daily_kpi_latest_date:'')+' · '+j.model+' · '+j.latency_ms+' ms';
   d.appendChild(src);(j.tool_calls||[]).forEach(c=>d.appendChild(render(c)));
   history.push({role:'user',content:text},{role:'assistant',content:j.answer||''});history=history.slice(-__MAXH__);}
 }catch(err){wait.remove();el('a err','request failed: '+err);}
 b.disabled=false;q.focus();};
</script></body></html>'''


def page(user):
    logo = f'<img src="{LOGO_URI}" alt="ARGIA">' if LOGO_URI else ''
    body = (PAGE.replace('__LOGO__', logo)
            .replace('__USER__', html.escape(user))
            .replace('__MODEL__', html.escape(agent.DEFAULT_MODEL))
            .replace('__MAXQ__', str(MAX_QUESTION))
            .replace('__MAXH__', str(MAX_HISTORY)))
    r = make_response(body)
    r.headers['Cache-Control'] = 'no-store'
    return r


def forbidden():
    r = make_response('<!doctype html><meta charset="utf-8"><title>Ask ARGIA</title>'
                      '<p style="font-family:system-ui;margin:40px">Ask ARGIA is not '
                      'enabled for this account (phase 0). <a href="/">Back</a></p>', 403)
    return r


# --------------------------------------------------------------- routes
@app.get('/')
@app.get('/ask/')
def index():
    user = actor()
    if not allowed(user):
        return forbidden()
    return page(user)


@app.post('/api')
@app.post('/ask/api')
def api():
    user = actor()
    if not allowed(user):
        return jsonify({'error': 'not enabled for this account'}), 403
    body = request.get_json(silent=True) or {}
    question = str(body.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'question is required'}), 400
    if len(question) > MAX_QUESTION:
        return jsonify({'error': f'question too long (max {MAX_QUESTION})'}), 400
    history = [{'role': h.get('role'), 'content': h.get('content')}
               for h in (body.get('history') or [])
               if isinstance(h, dict)][-MAX_HISTORY:]
    try:
        llm = app.config.get('LLM') or agent.AnthropicLLM(agent.load_api_key())
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 503
    rows = app.config.get('ROWS') or pgq.psql_rows
    ans = agent.ask(question, rows, llm, history=history)
    try:
        agent.log_answer(app.config.get('EXEC') or pgq.psql_exec, user, ans)
    except Exception as e:                           # noqa: BLE001
        app.logger.warning('ask_log not written: %s', e)
    out = ans.as_dict()
    out['user'] = user
    return jsonify(out), (200 if not ans.error else 502)


@app.get('/healthz')
@app.get('/ask/healthz')
def healthz():
    return jsonify({'ok': True, 'model': agent.DEFAULT_MODEL,
                    'tools': [t['name'] for t in tools.TOOLS],
                    'allowed_users': sorted(ALLOWED_USERS),
                    'allowed_emails': sorted(ALLOWED_EMAILS)})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('ARGIA_ASK_PORT', '8513')),
            threaded=True)
