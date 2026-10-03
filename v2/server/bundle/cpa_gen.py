#!/usr/bin/env python3
"""cpa.argia.com.mx generator (v296) - ARGIA for CPA.

CPA, an industrial real estate partner, and ARGIA deliver solar and LED
lighting to CPA's tenants. This site shows CPA - and its sustainability
team - the clean energy those rooftops make and the CO2 it avoids, live,
with a clean energy report (PDF in English and Spanish, plus CSV).

* Which plants, the names shown, the CPA logo: server-only
  (/opt/argia/cpa/cpa.json and /opt/argia/cpa/brand/) - the repo is public.
* Data: read-only PostgreSQL (default_transaction_read_only, timeout):
  daily_production for closed days, telemetry for today. Same numbers as
  the portal; CO2 from argia.core.co2 (one register).
* Login: nginx HTTP Basic with its own user file
  /opt/argia/cpa/cpa.htpasswd - nothing of the portal login is used.
* Output: static pages, built in a staging folder, checked, then swapped
  in; the last good site stays online if anything fails. No mail, no
  ntfy, no vendor call, no write to the database.

    python3 /opt/argia/bundle/cpa_gen.py [OUT_DIR]
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get("ARGIA_V2_DIR", "/root/argia_v2/v2"))

from argia.core import co2 as co2reg                      # noqa: E402
from argia.cpa import charts as C                         # noqa: E402
from argia.cpa import report as RP                        # noqa: E402

OUT_DEFAULT = "/www/hosting/cpa.argia.com.mx/www"
CPA_DIR = os.environ.get("ARGIA_CPA_DIR", "/opt/argia/cpa")
PORTAL_ROOT = os.environ.get("ARGIA_PORTAL_ROOT", "/www/hosting/portal.argia.com.mx/www")
DB = os.environ.get("ARGIA_PG_DB", "argia_mont")
PG_OPTS = "-c default_transaction_read_only=on -c statement_timeout=60000"
MX_TZ = "America/Mexico_City"
MX_D = f"(ts_utc AT TIME ZONE '{MX_TZ}')::date"
WINDOW = (6, 20)
KEY_RE = re.compile(r"^[A-Z0-9_]{2,16}$")
# v299 (Tomasz: "shades of blue"): one blue ramp in CPA's palette - deep,
# mid, light. The dataviz validator: every pair >= 22 dE apart for normal
# vision and >= 21 under colour-vision deficiency; the lightest sits above
# the categorical lightness band (expected for a one-hue ramp), so every
# chart carries a legend, a 2px white gap between stacked parts and a
# tooltip naming the site. A 4th+ site gets grey and still its name.
SITE_COLOURS = ["#1d3c78", "#2f80d1", "#9ccbf0", "#5a6b8c", "#b8c4d8"]
NAVY, BLUE, TEAL = "#1f1d4f", "#2ea3f2", "#05b1a9"
CO2_COLOUR = NAVY
STATUS_RING = {"live": "#1e8e3e", "stale": "#e8a23a", "dark": "#c5221f", "night": "#9aa0a6"}
RENDER_PDF = None          # set in main(); tests replace it (CI has no chromium)


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def L(en: str, es: str) -> str:
    """Both languages; CSS shows the active one (html.es)."""
    return f'<span lang="en">{en}</span><span lang="es">{es}</span>'


def mx_now() -> dt.datetime:
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo(MX_TZ)).replace(tzinfo=None)


# ------------------------------------------------------------------ config
def load_config(path: Optional[str] = None) -> dict:
    """{"partner": "CPA", "sites": [{"key", "name", "city", "logo": bool, "approx": bool}]}"""
    with open(path or os.path.join(CPA_DIR, "cpa.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    sites = cfg.get("sites") or []
    if not sites:
        raise ValueError("cpa.json: no sites")
    for s in sites:
        if not KEY_RE.match(str(s.get("key", ""))) or not str(s.get("name", "")).strip():
            raise ValueError(f"cpa.json: bad site entry {s!r}")
    return cfg


# ------------------------------------------------------------------ database
def q(sql: str) -> List[List[str]]:
    env = dict(os.environ)
    env["PGOPTIONS"] = PG_OPTS
    r = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-d", DB, "-X", "-t", "-A", "-F", "\t",
                        "-v", "ON_ERROR_STOP=1", "-c", sql], capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])
    return [ln.split("\t") for ln in r.stdout.splitlines() if ln.strip()]


def _f(x) -> Optional[float]:
    try:
        return None if x in (None, "") else float(x)
    except ValueError:
        return None


def fetch(cfg: dict, now: dt.datetime) -> Tuple[List[RP.Site], Dict, Dict[str, RP.Live]]:
    keys = [s["key"] for s in cfg["sites"]]
    inlist = ",".join(f"'{k}'" for k in keys)          # keys are checked by KEY_RE
    today = now.date().isoformat()
    meta = {r[0]: r for r in q(f"SELECT plant_key, kwp_dc, lat, lon FROM plant WHERE plant_key IN ({inlist});")}
    sites = []
    for s in cfg["sites"]:
        m = meta.get(s["key"])
        if not m:
            raise ValueError(f"plant {s['key']} not in the database")
        sites.append(RP.Site(key=s["key"], name=s["name"].strip(), city=s.get("city", ""), kwp=_f(m[1]) or 0.0,
                             lat=_f(m[2]), lon=_f(m[3]), approx=bool(s.get("approx")), slug=RP.slugify(s["name"])))
    daily: Dict[str, List] = {k: [] for k in keys}
    for k, d, kwh in q(f"SELECT plant_key, prod_date, energy_kwh FROM daily_production WHERE plant_key IN ({inlist}) "
                       f"AND prod_date < '{today}' ORDER BY 1, 2;"):
        daily[k].append((dt.date.fromisoformat(d), _f(kwh)))
    live = {k: RP.Live() for k in keys}
    for k, e, age in q("SELECT s.plant_key, coalesce(sum(s.e),0), min(s.age_min) FROM (SELECT plant_key, inverter_sn, "
                       "max(etoday_kwh) AS e, extract(epoch FROM now() - max(ts_utc))/60 AS age_min FROM telemetry "
                       f"WHERE {MX_D} = '{today}' AND plant_key IN ({inlist}) AND (etoday_kwh IS NOT NULL OR power_w IS NOT NULL) "
                       "GROUP BY 1, 2) s GROUP BY 1;"):
        live[k].today_kwh, live[k].age_min = _f(e) or 0.0, _f(age)
    for k, kw in q("SELECT plant_key, sum(power_w)/1000.0 FROM (SELECT DISTINCT ON (plant_key, inverter_sn) plant_key, power_w "
                   f"FROM telemetry WHERE ts_utc > now() - interval '30 minutes' AND power_w IS NOT NULL AND plant_key IN ({inlist}) "
                   "ORDER BY plant_key, inverter_sn, ts_utc DESC) t GROUP BY 1;"):
        live[k].kw = _f(kw) or 0.0
    mx = f"(ts_utc AT TIME ZONE '{MX_TZ}')"
    for k, b, kw in q(f"SELECT plant_key, b, sum(p)/1000.0 FROM (SELECT plant_key, inverter_sn, to_char({mx}, 'HH24') || ':' || "
                      f"lpad(((extract(minute FROM {mx})::int / 15) * 15)::text, 2, '0') AS b, avg(power_w) AS p FROM telemetry "
                      f"WHERE {MX_D} = '{today}' AND plant_key IN ({inlist}) AND power_w IS NOT NULL GROUP BY 1, 2, 3) t "
                      "GROUP BY 1, 2 ORDER BY 1, 2;"):
        live[k].curve.append((b, _f(kw) or 0.0))
    return sites, daily, live


# ------------------------------------------------------------------ look
CSS = """
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Poppins:wght@500;600;700;800&display=swap');
:root{--navy:#1f1d4f;--navy2:#14123a;--blue:#2ea3f2;--blue2:#02509b;--teal:#05b1a9;--green:#1baf7a;--bg:#f5f7fb;--card:#fff;
--ink:#16152e;--ink2:#4a4d63;--muted:#7a7d92;--line:#e6e8ef}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 'DM Sans',Segoe UI,Roboto,Arial,sans-serif}
html:not(.es) [lang=es],html.es [lang=en]{display:none}html:not(.es) .t-es,html.es .t-en{display:none}
h1,h2,h3,.num{font-family:Poppins,'DM Sans',Segoe UI,sans-serif}a{color:var(--blue2);text-decoration:none}a:hover{color:var(--blue)}
.top{position:sticky;top:0;z-index:1100;background:var(--navy);color:#fff;box-shadow:0 2px 18px rgba(20,18,58,.35)}
.topin{max-width:1280px;margin:0 auto;display:flex;align-items:center;gap:18px;padding:12px 22px}
.brand{display:flex;align-items:center;gap:12px;color:#fff;font-weight:700}.brand img.cpa{height:30px}.brand .x{opacity:.55}
.brand img.ar{height:20px;filter:brightness(0) invert(1)}.brand .word{font-family:Poppins;font-weight:800;font-size:22px;letter-spacing:.02em}
nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap}nav a{color:#d9dcf2;padding:8px 13px;border-radius:999px;font-weight:600;font-size:14px}
nav a:hover{color:#fff;background:rgba(255,255,255,.08)}nav a.on{background:#fff;color:var(--navy)}
.lang{display:flex;gap:2px;background:rgba(255,255,255,.1);border-radius:999px;padding:3px}.lang button{border:0;background:none;color:#cfd3ef;font:600 12px 'DM Sans';padding:5px 10px;border-radius:999px;cursor:pointer}
html:not(.es) .lang .b-en,html.es .lang .b-es{background:#fff;color:var(--navy)}
main{max-width:1280px;margin:0 auto;padding:24px 22px 70px}
.hero{position:relative;overflow:hidden;border-radius:26px;color:#fff;padding:38px 40px 30px;
background:radial-gradient(900px 380px at 88% -20%,rgba(46,163,242,.55),transparent 60%),radial-gradient(700px 300px at 0% 120%,rgba(5,177,169,.45),transparent 60%),linear-gradient(125deg,var(--navy2),var(--navy) 55%,#2a2a7a)}
.hero .sun{position:absolute;right:-90px;top:-90px;width:420px;height:420px;border-radius:50%;border:1px solid rgba(255,255,255,.12);box-shadow:0 0 0 60px rgba(255,255,255,.03),0 0 0 120px rgba(255,255,255,.025)}
.hero .k{font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:#a9d8fb;font-weight:700}
.hero h1{font-size:40px;line-height:1.08;margin:8px 0 8px;font-weight:800;letter-spacing:-.01em;max-width:820px}
.hero .sub{color:#cdd2f2;max-width:760px}
.big{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-top:26px;position:relative}
.big .t{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);border-radius:18px;padding:16px 18px;backdrop-filter:blur(4px)}
.big .l{font-size:12.5px;color:#b9c0ec;font-weight:600}.big .v{font-family:Poppins;font-size:34px;font-weight:800;line-height:1.15}
.big .v small{font-size:15px;font-weight:600;color:#b9c0ec;margin-left:4px}.big .d{font-size:12.5px;color:#aab2e3}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#5ee08f;margin-right:6px;box-shadow:0 0 0 0 rgba(94,224,143,.7);animation:pulse 1.8s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 10px rgba(94,224,143,0)}100%{box-shadow:0 0 0 0 rgba(94,224,143,0)}}
@media(prefers-reduced-motion:reduce){.dot{animation:none}}
.sec{margin-top:30px}.sec>h2{font-size:22px;margin:0 0 4px;color:var(--navy)}.sec>.lead{color:var(--ink2);margin:0 0 14px;max-width:860px}
.kick{font-size:11.5px;letter-spacing:.18em;text-transform:uppercase;color:var(--blue2);font-weight:700}
.grid{display:grid;gap:16px}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}.g4{grid-template-columns:repeat(4,minmax(0,1fr))}
@media(max-width:980px){.g2,.g3,.g4{grid-template-columns:1fr}.topin{flex-wrap:wrap}nav{margin-left:0}.hero{padding:26px 22px}.hero h1{font-size:28px}}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:18px 20px;box-shadow:0 1px 2px rgba(22,21,46,.04)}
.card h3{font-size:15.5px;margin:0 0 10px;color:var(--navy);display:flex;gap:8px;align-items:center}.card h3 .r{margin-left:auto;font:500 12px 'DM Sans';color:var(--muted)}
.site{position:relative;overflow:hidden;padding:0;display:flex;flex-direction:column;color:var(--ink);transition:transform .15s,box-shadow .15s}
.site:hover{transform:translateY(-3px);box-shadow:0 14px 30px rgba(22,21,46,.12);color:var(--ink)}
.site .ph{height:150px;background:linear-gradient(135deg,#dfe7f7,#eef2fb) center/cover no-repeat;position:relative}
.site .ph .logo{position:absolute;left:14px;bottom:-24px;background:#fff;border-radius:14px;padding:8px 12px;box-shadow:0 6px 16px rgba(22,21,46,.15);height:52px;display:flex;align-items:center}
.site .ph .logo img{max-height:34px;max-width:120px}.site .ph .logo .mono{font:800 18px Poppins;color:var(--navy)}
.site .bd{padding:34px 18px 16px}.site h3{margin:0;font-size:18px}.site .city{color:var(--muted);font-size:13px}
.kv{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}.kv div{background:#f6f8fc;border-radius:12px;padding:8px 10px}
.kv b{display:block;font:700 17px Poppins;color:var(--navy)}.kv span{font-size:11.5px;color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:3px 10px;font-size:12px;font-weight:700}
.p-live{background:#e3f7ea;color:#1d7a43}.p-stale{background:#fff3df;color:#9a6100}.p-dark{background:#fde9e7;color:#b2443c}.p-night{background:#eef0f6;color:#5b5f73}
.chart{width:100%;height:auto;display:block}.chart .hit{cursor:crosshair}.chart .hit:hover{fill:rgba(31,29,79,.05)}
.clegend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--ink2);margin-top:6px}.clegend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}
#tip{position:fixed;pointer-events:none;z-index:99;background:#14123a;color:#fff;border-radius:10px;padding:8px 11px;font-size:12.5px;line-height:1.45;box-shadow:0 8px 24px rgba(0,0,0,.25);display:none;max-width:260px}
#tip b{display:block;color:#a9d8fb;font-weight:700}
.eq{display:flex;gap:14px;align-items:center}.eq .ic{flex:none;width:54px;height:54px;border-radius:16px;display:grid;place-items:center;background:#eaf6ff;font-size:26px}
.eq b{font:800 28px Poppins;color:var(--navy);display:block;line-height:1.1}.eq .lab{color:var(--ink2);font-size:13.5px}
.esg .badge{display:inline-grid;place-items:center;width:46px;height:46px;border-radius:12px;color:#fff;font:800 18px Poppins;margin-bottom:8px}
#map{height:520px;border-radius:16px;border:1px solid var(--line);position:relative;z-index:0;isolation:isolate}
.pin{position:relative;cursor:pointer}.pin .ph{border-radius:50%;background:#dfe7f7 center/cover no-repeat;box-shadow:0 3px 12px rgba(20,18,58,.35);box-sizing:border-box}
.pin .st{position:absolute;right:-2px;top:-2px;width:14px;height:14px;border-radius:50%;border:2px solid #fff}
.pin .st.live{animation:pulse 2s infinite}
.pin .nm{position:absolute;top:100%;left:50%;transform:translateX(-50%);margin-top:3px;white-space:nowrap;font:700 12px 'DM Sans',sans-serif;color:var(--navy);text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 4px #fff}
.mtip{font:13px 'DM Sans',sans-serif;color:var(--ink);min-width:230px}.mtip img{width:100%;height:110px;object-fit:cover;border-radius:10px;margin-bottom:6px;display:block}
.mtip h4{margin:0;font:700 15px Poppins;color:var(--navy)}.mtip .sub{color:var(--muted);font-size:12px;margin-bottom:4px}
.mtip table{width:100%;border-collapse:collapse}.mtip td{padding:1px 0}.mtip td:last-child{text-align:right;font-weight:700}.mtip .go{margin-top:6px;color:var(--blue2);font-weight:700}
.leaflet-tooltip.mt{border-radius:14px;border:1px solid var(--line);box-shadow:0 10px 28px rgba(20,18,58,.22);padding:10px 12px}
.mlegend{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px 18px;margin-top:12px}
.mlegend a{display:flex;gap:12px;align-items:center;color:var(--ink);padding:6px;border-radius:14px}.mlegend a:hover{background:#f3f6fc;color:var(--ink)}
.mlegend .th{flex:none;width:48px;height:48px;border-radius:50%;background:#dfe7f7 center/cover no-repeat;border:4px solid}
.mlegend b{display:block;font:700 15px Poppins;color:var(--navy)}.mlegend .sub{font-size:12px;color:var(--muted)}
.mkey{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--ink2);margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
.mkey i{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px;vertical-align:-1px}
.btn{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:10px 18px;font-weight:700;background:var(--blue);color:#fff;border:0;cursor:pointer;font-size:14px}
.btn:hover{background:var(--blue2);color:#fff}.btn.ghost{background:#fff;color:var(--navy);border:1px solid var(--line)}.btn.navy{background:var(--navy)}
table.t{width:100%;border-collapse:collapse;font-size:13.5px}table.t th{text-align:left;font-size:11.5px;color:var(--muted);font-weight:700;border-bottom:1px solid var(--line);padding:8px}
table.t td{border-bottom:1px solid var(--line);padding:8px}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}tr.tot td{font-weight:700;border-top:2px solid var(--navy)}
.note{font-size:12.5px;color:var(--muted)}.nodata{padding:40px;text-align:center;color:var(--muted)}
.foot{max-width:1280px;margin:0 auto;padding:0 22px 34px;color:var(--muted);font-size:12px;display:flex;gap:16px;flex-wrap:wrap}
.cta{display:flex;gap:18px;align-items:center;justify-content:space-between;flex-wrap:wrap;background:linear-gradient(120deg,var(--navy),#2a2a7a);color:#fff;border-radius:22px;padding:24px 28px}
.cta h3{color:#fff;font-size:20px;margin:0}.cta p{margin:4px 0 0;color:#cdd2f2}
label.f{display:block;font-size:12.5px;font-weight:700;color:var(--ink2);margin:10px 0 4px}input.f{width:100%;font:inherit;border:1px solid #cfd4e2;border-radius:12px;padding:9px 12px}
@media(max-width:700px){.big .v{font-size:26px}.kv{grid-template-columns:1fr 1fr}.brand img.ar{display:none}main{padding:16px 12px 50px}}
@media print{.top,.noprint,.foot{display:none!important}html,body{background:#fff}main{padding:0}.card{box-shadow:none;break-inside:avoid}}
"""

MAP_JS = """(function(){if(!window.L)return;var P=__P__;var m=L.map('map',{scrollWheelZoom:false});
var st=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',{attribution:'&copy; Esri',maxZoom:19}).addTo(m);
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{attribution:'&copy; Esri, Maxar',maxZoom:19});
L.control.layers({'Streets':st,'Satellite':sat}).addTo(m);var b=[];
function nf(v){return Number(v).toLocaleString('en-US',{maximumFractionDigits:0});}
function es(){return document.documentElement.classList.contains('es');}
var MK=[];
// pins of sites a couple of km apart would sit on top of each other: push
// the later one sideways (screen pixels only - the location stays true;
// sideways so no pin moves towards the map edge where its card would clip)
function declutter(){var pts=MK.map(function(o){return m.latLngToContainerPoint(o.ll);});var off=MK.map(function(){return [0,0];});
for(var j=1;j<MK.length;j++){for(var it=0;it<4;it++){for(var i=0;i<j;i++){var ax=pts[i].x+off[i][0],ay=pts[i].y+off[i][1],bx=pts[j].x+off[j][0],by=pts[j].y+off[j][1];
var dx=bx-ax,dy=by-ay,d=Math.sqrt(dx*dx+dy*dy),need=(MK[i].px+MK[j].px)/2+18;if(d<need){off[j][0]+=(dx>=0?1:-1)*(need-d);}}}}
MK.forEach(function(o,k){var el=o.mk.getElement();if(el&&el.firstChild)el.firstChild.style.transform='translate('+off[k][0].toFixed(0)+'px,'+off[k][1].toFixed(0)+'px)';
var t=o.mk.getTooltip();if(t)t.options.offset=L.point(off[k][0],off[k][1]);});}
var LBL={live:['Live','En vivo'],stale:['Delayed data','Datos retrasados'],dark:['No data today','Sin datos hoy'],night:['Night','Noche']};
P.forEach(function(p){var px=Math.round(Math.max(46,Math.min(76,Math.sqrt(p.kwp)*2.6)));
var bg=p.photo?"background-image:url('"+p.photo+"')":'background:'+p.c;
var icon=L.divIcon({className:'',iconSize:[px,px],iconAnchor:[px/2,px/2],
html:'<div class="pin"><div class="ph" style="width:'+px+'px;height:'+px+'px;border:4px solid '+p.c+';'+bg+'"></div><span class="st '+p.st+'" style="background:'+p.ring+'"></span><div class="nm">'+p.name+'</div></div>'});
var mk=L.marker([p.lat,p.lon],{icon:icon}).addTo(m);
mk.bindTooltip(function(){var e=es(),k=e?1:0;return '<div class="mtip">'+(p.photo?'<img src="'+p.photo+'" alt="">':'')+'<h4>'+p.name+'</h4><div class="sub">'+p.city+' · '+nf(p.kwp)+' kWp · '+LBL[p.st][k]+(p.approx?(e?' · ubicación aproximada':' · approximate location'):'')+'</div><table>'
+'<tr><td>'+(e?'Generando ahora':'Generating now')+'</td><td>'+nf(p.kw)+' kW</td></tr><tr><td>'+(e?'Hoy':'Today')+'</td><td>'+nf(p.kwh)+' kWh</td></tr>'
+'<tr><td>'+(e?'A la fecha':'To date')+'</td><td>'+nf(p.mwh)+' MWh</td></tr><tr><td>CO2e '+(e?'evitado':'avoided')+'</td><td>'+nf(p.co2)+' t</td></tr></table><div class="go">'+(e?'Abrir el sitio':'Open the site')+' &rarr;</div></div>';},
{direction:'auto',offset:[0,0],opacity:1,className:'mt'});
mk.on('click',function(){window.location=p.url;});MK.push({mk:mk,ll:L.latLng(p.lat,p.lon),px:px});b.push([p.lat,p.lon]);});
if(b.length)m.fitBounds(b,{padding:[150,150],maxZoom:12});
m.on('zoomend',declutter);declutter();})();"""

TIP_JS = """(function(){var t=document.createElement('div');t.id='tip';document.body.appendChild(t);
document.addEventListener('mousemove',function(ev){var el=ev.target.closest?ev.target.closest('[data-tip]'):null;
if(!el){t.style.display='none';return;}var es=document.documentElement.classList.contains('es');
var s=(es&&el.getAttribute('data-tip-es'))||el.getAttribute('data-tip');var p=s.split('|');
t.innerHTML='<b>'+p[0].replace(/</g,'&lt;')+'</b>'+p.slice(1).map(function(x){return x.replace(/</g,'&lt;');}).join('<br>');
t.style.display='block';var x=ev.clientX+14,y=ev.clientY+14;if(x+270>innerWidth)x=ev.clientX-280;t.style.left=x+'px';t.style.top=y+'px';});})();"""

LANG_JS = """(function(){var h=document.documentElement,m=(location.hash||'').toLowerCase();var l='en';
try{l=localStorage.getItem('cpa_lang')||'en';}catch(e){}if(m==='#es'||m==='#en')l=m.slice(1);
h.classList.toggle('es',l==='es');h.lang=l;
window.setLang=function(x){h.classList.toggle('es',x==='es');h.lang=x;try{localStorage.setItem('cpa_lang',x);}catch(e){}};})();"""

COUNT_JS = """(function(){if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
document.querySelectorAll('[data-count]').forEach(function(el){var to=parseFloat(el.dataset.count),d=parseInt(el.dataset.dec||'0'),t0=null;
function f(v){return v.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});}
function step(ts){if(!t0)t0=ts;var k=Math.min(1,(ts-t0)/1400);el.textContent=f(to*(1-Math.pow(1-k,3)));if(k<1)requestAnimationFrame(step);}
requestAnimationFrame(step);});})();"""


def fmt(v, d: int = 0) -> str:
    return C.fmt(v, d)


def count(v: float, d: int = 0) -> str:
    return f'<span data-count="{v:.{d}f}" data-dec="{d}">{fmt(v, d)}</span>'


class Ctx:
    """Everything a page needs, computed once per run."""

    def __init__(self, cfg, sites, daily, live, now, brand):
        self.cfg, self.sites, self.daily, self.live, self.now, self.brand = cfg, sites, daily, live, now, brand
        self.partner = cfg.get("partner", "CPA")
        self.today = now.date()
        self.mon = RP.monthly(daily)
        self.months = RP.months_with_data(self.mon, [s.key for s in sites])
        self.colour = {s.key: SITE_COLOURS[i] if i < len(SITE_COLOURS) else "#8a8fa3" for i, s in enumerate(sites)}
        first = self.months[0] if self.months else RP.ym(self.today)
        self.closed = RP.period_totals(sites, self.mon, first, "9999-12")
        self.today_t = RP.live_totals(sites, live, self.today)
        self.life = {k: RP.Totals(self.closed[k].kwh + self.today_t[k].kwh, self.closed[k].co2_t + self.today_t[k].co2_t)
                     for k in self.closed}
        self.in_window = WINDOW[0] <= now.hour < WINDOW[1]
        self.last_closed = RP.previous_month(self.today)
        y = int(self.last_closed[:4])
        self.ytd_first = f"{y}-01"
        self.ytd = RP.period_totals(sites, self.mon, self.ytd_first, self.last_closed)
        self.started = {s.key: RP.first_day(daily, s.key) for s in sites}

    def kw_now(self) -> float:
        return sum(x.kw for x in self.live.values())


def logo_html(site: RP.Site, cfg_site: dict) -> str:
    uri = ""
    if cfg_site.get("logo"):
        try:
            from argia_client_logos import CLIENT_LOGOS
            uri = CLIENT_LOGOS.get(site.key, ("", ""))[1]
        except ImportError:
            uri = ""
    if uri:
        return f'<img src="{uri}" alt="{esc(site.name)}">'
    return f'<span class="mono">{esc(site.name[:3].upper())}</span>'


def status_pill(st: str) -> str:
    return {"live": f'<span class="pill p-live"><span class="dot" style="margin:0"></span>{L("Live", "En vivo")}</span>',
            "stale": f'<span class="pill p-stale">{L("Delayed data", "Datos retrasados")}</span>',
            "dark": f'<span class="pill p-dark">{L("No data today", "Sin datos hoy")}</span>',
            "night": f'<span class="pill p-night">{L("Night", "Noche")}</span>'}[st]


def page(ctx: Ctx, title: str, body: str, on: str, depth: int, extra_head: str = "") -> str:
    up = "../" * depth
    nav = [("index.html", L("Overview", "Resumen"), "home"), ("sites/index.html", L("Sites", "Sitios"), "sites"),
           ("report/index.html", L("Clean energy report", "Reporte de energía limpia"), "report"),
           ("led/index.html", L("LED lighting", "Iluminación LED"), "led")]
    links = "".join(f'<a href="{up}{h}" class="{"on" if k == on else ""}">{lbl}</a>' for h, lbl, k in nav)
    cpa = (f'<img class="cpa" src="{up}assets/cpa_logo_white.png" alt="{esc(ctx.partner)}">' if ctx.brand.get("logo")
           else f'<span class="word">{esc(ctx.partner.lower())}</span>')
    import argia_logo
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{esc(title)} - {esc(ctx.partner)} x ARGIA</title>
<link rel="icon" href="{up}favicon.png"><script>{LANG_JS}</script><style>{CSS}</style>{extra_head}</head><body>
<div class="top"><div class="topin"><a class="brand" href="{up}index.html">{cpa}<span class="x">×</span><img class="ar" src="{argia_logo.MARK_URI}" alt="ARGIA"></a>
<nav>{links}</nav><div class="lang noprint"><button class="b-en" onclick="setLang('en')">EN</button><button class="b-es" onclick="setLang('es')">ES</button></div></div></div>
<main>{body}</main>
<div class="foot"><span>{esc(ctx.partner)} × ARGIA · {L("Solar and LED lighting for CPA tenants", "Solar e iluminación LED para inquilinos de CPA")}</span>
<span>{L("Live data from the inverters, refreshed every 5 minutes", "Datos en vivo de los inversores, cada 5 minutos")} · {L("updated", "actualizado")} {ctx.now:%d/%m/%Y %H:%M} ({L("Mexico City", "Ciudad de México")})</span>
<span>{L("Operated by", "Operado por")} ARGIA - Smart Energy Solutions</span></div>
<script>{TIP_JS}</script></body></html>"""


def month_charts(ctx: Ctx, months: Sequence[str]) -> Tuple[str, str]:
    en, es = C.spark_months(list(months))
    series = [(s.name, ctx.colour[s.key], {m: v / 1000.0 for m, v in ctx.mon.get(s.key, {}).items()}) for s in ctx.sites]
    bars = C.stacked_bars(months, series, "MWh", en, es, d=1)
    cum, run = [], 0.0
    for m in months:
        run += sum(RP.co2_t(s.key, m, ctx.mon.get(s.key, {}).get(m, 0.0)) for s in ctx.sites)
        cum.append((m, run))
    co2 = C.area(cum, CO2_COLOUR, "t CO2e", en, es, d=0)
    return bars, co2


def today_chart(ctx: Ctx, sites: Sequence[RP.Site]) -> str:
    times = []
    h, m = WINDOW[0], 0
    while h < WINDOW[1]:
        times.append(f"{h:02d}:{m:02d}")
        m += 15
        if m == 60:
            h, m = h + 1, 0
    series = [(s.name, ctx.colour[s.key], dict(ctx.live[s.key].curve)) for s in sites]
    return C.stacked_area(times, series, "kW")


def overview(ctx: Ctx) -> str:
    kwp = sum(s.kwp for s in ctx.sites)
    life, ytd = ctx.life["_all"], ctx.ytd["_all"]
    eq = RP.equivalents(life.co2_t)
    first = min((d for d in ctx.started.values() if d), default=ctx.today)
    cards = ""
    for s, cs in zip(ctx.sites, ctx.cfg["sites"]):
        x = ctx.live[s.key]
        st = RP.status(x, ctx.in_window)
        ph = f"background-image:url(assets/photos/{s.slug}.jpg)" if ctx.brand.get("photos", {}).get(s.slug) else ""
        cards += f"""<a class="card site" href="sites/{s.slug}/index.html" style="border-top:5px solid {ctx.colour[s.key]}"><div class="ph" style="{ph}"><div class="logo">{logo_html(s, cs)}</div></div>
<div class="bd"><div style="display:flex;align-items:center;gap:8px"><h3>{esc(s.name)}</h3><span style="margin-left:auto">{status_pill(st)}</span></div>
<div class="city">{esc(s.city)} · {fmt(s.kwp, 0)} kWp</div>
<div class="kv"><div><b>{fmt(x.kw, 0)}</b><span>{L("kW now", "kW ahora")}</span></div><div><b>{fmt(x.today_kwh, 0)}</b><span>{L("kWh today", "kWh hoy")}</span></div>
<div><b>{fmt(ctx.life[s.key].kwh / 1000, 0)}</b><span>{L("MWh to date", "MWh a la fecha")}</span></div></div></div></a>"""
    bars, co2 = month_charts(ctx, ctx.months)
    leg = C.legend([(s.name, ctx.colour[s.key]) for s in ctx.sites])
    pts, mleg = [], ""
    for s in ctx.sites:
        st = RP.status(ctx.live[s.key], ctx.in_window)
        photo = f"assets/photos/{s.slug}.jpg" if ctx.brand.get("photos", {}).get(s.slug) else ""
        if s.lat is not None and s.lon is not None:
            pts.append({"lat": s.lat, "lon": s.lon, "name": s.name, "city": s.city, "kwp": s.kwp, "c": ctx.colour[s.key],
                        "url": f"sites/{s.slug}/index.html", "photo": photo, "st": st, "ring": STATUS_RING[st],
                        "kw": round(ctx.live[s.key].kw, 1), "kwh": round(ctx.live[s.key].today_kwh, 1),
                        "mwh": round(ctx.life[s.key].kwh / 1000, 1), "co2": round(ctx.life[s.key].co2_t, 1), "approx": s.approx})
        th = f"background-image:url({photo});" if photo else f"background:{ctx.colour[s.key]};"
        approx = L(" · approximate location", " · ubicación aproximada") if s.approx else ""
        mleg += (f'<a href="sites/{s.slug}/index.html"><span class="th" style="{th}border-color:{ctx.colour[s.key]}"></span>'
                 f'<span style="flex:1"><b>{esc(s.name)}</b><span class="sub">{esc(s.city)} · {fmt(s.kwp, 0)} kWp{approx}</span></span>'
                 f'<span style="text-align:right">{status_pill(st)}<span class="sub" style="display:block">{fmt(ctx.live[s.key].kw, 0)} kW</span></span></a>')
    mkey = (f'<div class="mkey"><span>{L("Ring = the site&#39;s colour in every chart", "Aro = el color del sitio en todas las gráficas")}</span>'
            f'<span>{L("Circle size = installed kWp", "Tamaño = kWp instalados")}</span>'
            f'<span><i style="background:{STATUS_RING["live"]}"></i>{L("Live", "En vivo")}</span>'
            f'<span><i style="background:{STATUS_RING["stale"]}"></i>{L("Delayed data", "Datos retrasados")}</span>'
            f'<span><i style="background:{STATUS_RING["dark"]}"></i>{L("No data today", "Sin datos hoy")}</span>'
            f'<span><i style="background:{STATUS_RING["night"]}"></i>{L("Night", "Noche")}</span></div>')
    sdg = [("7", "#fcc30b", "Affordable and clean energy", "Energía asequible y no contaminante",
            "On-site solar supplies the tenants' operations with zero-emission electricity.", "Solar en sitio abastece la operación de los inquilinos con electricidad sin emisiones."),
           ("9", "#fd6925", "Industry, innovation and infrastructure", "Industria, innovación e infraestructura",
            "Industrial parks with monitored, efficient energy infrastructure.", "Parques industriales con infraestructura energética eficiente y monitoreada."),
           ("13", "#3f7e44", "Climate action", "Acción por el clima",
            "Every kWh from the roofs displaces grid electricity and its emissions.", "Cada kWh de los techos desplaza electricidad de la red y sus emisiones.")]
    sdg_html = "".join(f'<div class="card esg"><div class="badge" style="background:{c}">{n}</div><h3>{L(f"SDG {n}: {en}", f"ODS {n}: {es}")}</h3><div class="note">{L(de, ds)}</div></div>'
                       for n, c, en, es, de, ds in sdg)
    body = f"""<div class="hero"><div class="sun"></div><div class="k">{esc(ctx.partner)} × ARGIA · {L("Clean energy programme", "Programa de energía limpia")}</div>
<h1>{L("Clean energy, made on your tenants' rooftops.", "Energía limpia, generada en los techos de sus inquilinos.")}</h1>
<div class="sub">{L(f"{len(ctx.sites)} solar sites · {fmt(kwp, 0)} kWp · producing since {first:%B %Y} · live from the inverters",
                     f"{len(ctx.sites)} sitios solares · {fmt(kwp, 0)} kWp · produciendo desde {first:%m/%Y} · en vivo desde los inversores")}</div>
<div class="big"><div class="t"><div class="l">{L("Clean energy generated", "Energía limpia generada")}</div><div class="v num">{count(life.kwh / 1000, 1)}<small>MWh</small></div><div class="d">{L("since the first day", "desde el primer día")}</div></div>
<div class="t"><div class="l">{L("CO2e emissions avoided", "Emisiones de CO2e evitadas")}</div><div class="v num">{count(life.co2_t, 1)}<small>t</small></div><div class="d">{L("vs. the Mexican grid", "vs. la red mexicana")}</div></div>
<div class="t"><div class="l"><span class="dot"></span>{L("Power right now", "Potencia ahora")}</div><div class="v num">{count(ctx.kw_now(), 0)}<small>kW</small></div><div class="d">{fmt(ctx.today_t["_all"].kwh, 0)} kWh {L("today so far", "hoy hasta ahora")}</div></div>
<div class="t"><div class="l">{L(f"This year (to {C.month_label(ctx.last_closed)})", f"Este año (a {C.month_label(ctx.last_closed, True)})")}</div><div class="v num">{count(ytd.kwh / 1000, 1)}<small>MWh</small></div><div class="d">{fmt(ytd.co2_t, 1)} t CO2e {L("avoided", "evitadas")}</div></div></div></div>
<div class="sec"><h2>{L("The sites", "Los sitios")}</h2><p class="lead">{L("Each rooftop, live. Open a site for its day, month and history.", "Cada techo, en vivo. Abra un sitio para ver su día, mes e historial.")}</p><div class="grid g3">{cards}</div></div>
<div class="sec grid g2"><div class="card"><h3>{L("Power today", "Potencia hoy")}<span class="r">kW · {L("every 15 min", "cada 15 min")}</span></h3>{today_chart(ctx, ctx.sites)}{leg}</div>
<div class="card"><h3>{L("Clean energy by month", "Energía limpia por mes")}<span class="r">MWh</span></h3>{bars}{leg}</div></div>
<div class="sec"><h2>{L("Climate impact", "Impacto climático")}</h2><p class="lead">{L("Avoided emissions add up month after month. The equivalences below are illustrations (US EPA factors), not measurements.", "Las emisiones evitadas se acumulan mes a mes. Las equivalencias son ilustrativas (factores de la EPA de EE. UU.), no mediciones.")}</p>
<div class="grid g2"><div class="card"><h3>{L("CO2e avoided, cumulative", "CO2e evitado, acumulado")}<span class="r">t CO2e</span></h3>{co2}</div>
<div class="grid" style="align-content:start"><div class="card eq"><div class="ic">🌳</div><div><b>{count(eq["trees"], 0)}</b><span class="lab">{L("tree seedlings grown for 10 years", "árboles plantados y cultivados 10 años")}</span></div></div>
<div class="card eq"><div class="ic">🚗</div><div><b>{count(eq["cars_year"], 0)}</b><span class="lab">{L("cars off the road for a year", "autos fuera de circulación un año")}</span></div></div>
<div class="card eq"><div class="ic">⛽</div><div><b>{count(eq["gasoline_l"] / 1000, 0)}</b><span class="lab">{L("thousand litres of gasoline not burned", "miles de litros de gasolina no quemados")}</span></div></div></div></div></div>
<div class="sec"><h2>{L("Where the energy is made", "Dónde se genera la energía")}</h2><div class="card" style="padding:10px"><div id="map"></div>
<div class="mlegend">{mleg}</div>{mkey}</div></div>
<div class="sec"><h2>{L("Ready for your ESG reporting", "Listo para su reporte ASG")}</h2><p class="lead">{L("The figures map directly to the frameworks CPA reports against.", "Las cifras corresponden directamente a los marcos que CPA reporta.")}</p>
<div class="grid g3">{sdg_html}</div>
<div class="grid g2" style="margin-top:16px"><div class="card"><h3>GRESB</h3><div class="note">{L("On-site renewable energy generated per asset (MWh), by month - the energy and renewables indicators of the GRESB Real Estate assessment.", "Energía renovable generada en sitio por activo (MWh), por mes - indicadores de energía y renovables de la evaluación GRESB Real Estate.")}</div></div>
<div class="card"><h3>GHG Protocol</h3><div class="note">{L("For the tenant, solar consumed on site reduces purchased electricity (Scope 2). For CPA as landlord it sits in Scope 3, category 13 (downstream leased assets). Factor per month and site in the report.", "Para el inquilino, la energía solar consumida en sitio reduce la electricidad comprada (Alcance 2). Para CPA como arrendador está en Alcance 3, categoría 13 (activos arrendados). Factor por mes y sitio en el reporte.")}</div></div></div>
<div class="cta" style="margin-top:16px"><div><h3>{L("Clean energy report", "Reporte de energía limpia")}</h3><p>{L("Year to date and since the start, per site and month, with method and factors. PDF in English and Spanish, data as CSV.", "Año a la fecha y desde el inicio, por sitio y mes, con método y factores. PDF en inglés y español, datos en CSV.")}</p></div>
<a class="btn" href="report/index.html">{L("Open the report", "Abrir el reporte")} →</a></div></div>
<div class="sec"><div class="card" style="display:flex;gap:18px;align-items:center;flex-wrap:wrap"><div style="font-size:34px">💡</div><div style="flex:1;min-width:240px"><h3 style="margin:0">{L("LED lighting upgrades", "Mejora de iluminación LED")}</h3>
<div class="note">{L("The second half of the programme: replace old warehouse lighting with LED. Estimate the savings for any building.", "La otra mitad del programa: reemplazar la iluminación de las naves por LED. Estime el ahorro de cualquier edificio.")}</div></div><a class="btn ghost" href="led/index.html">{L("LED savings estimator", "Estimador de ahorro LED")} →</a></div></div>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"><script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>{MAP_JS.replace("__P__", json.dumps(pts))}</script><script>{COUNT_JS}</script>"""
    return page(ctx, "Clean energy", body, "home", 0)


def sites_index(ctx: Ctx) -> str:
    rows = ""
    for s in ctx.sites:
        st = RP.status(ctx.live[s.key], ctx.in_window)
        started = ctx.started.get(s.key)
        rows += (f'<tr><td><a href="{s.slug}/index.html"><b>{esc(s.name)}</b></a><div class="note">{esc(s.city)}</div></td><td>{status_pill(st)}</td>'
                 f'<td class="n">{fmt(s.kwp, 0)}</td><td class="n">{fmt(ctx.live[s.key].kw, 0)}</td><td class="n">{fmt(ctx.live[s.key].today_kwh, 0)}</td>'
                 f'<td class="n">{fmt(ctx.ytd[s.key].kwh / 1000, 1)}</td><td class="n">{fmt(ctx.life[s.key].kwh / 1000, 1)}</td>'
                 f'<td class="n">{fmt(ctx.life[s.key].co2_t, 1)}</td><td>{started.strftime("%d/%m/%Y") if started else "-"}</td></tr>')
    a, y = ctx.life["_all"], ctx.ytd["_all"]
    rows += (f'<tr class="tot"><td>{L("All sites", "Todos")}</td><td></td><td class="n">{fmt(sum(s.kwp for s in ctx.sites), 0)}</td>'
             f'<td class="n">{fmt(ctx.kw_now(), 0)}</td><td class="n">{fmt(ctx.today_t["_all"].kwh, 0)}</td><td class="n">{fmt(y.kwh / 1000, 1)}</td>'
             f'<td class="n">{fmt(a.kwh / 1000, 1)}</td><td class="n">{fmt(a.co2_t, 1)}</td><td></td></tr>')
    body = f"""<div class="kick">{L("Sites", "Sitios")}</div><h1 style="color:var(--navy);margin:4px 0 14px">{L("Every rooftop in the programme", "Cada techo del programa")}</h1>
<div class="card" style="overflow-x:auto"><table class="t"><tr><th>{L("Site", "Sitio")}</th><th>{L("Status", "Estado")}</th><th class="n">kWp</th><th class="n">{L("kW now", "kW ahora")}</th>
<th class="n">{L("kWh today", "kWh hoy")}</th><th class="n">MWh {L("this year", "este año")}</th><th class="n">MWh {L("to date", "a la fecha")}</th><th class="n">t CO2e</th><th>{L("Since", "Desde")}</th></tr>{rows}</table>
<p class="note">{L(f"This year = January to {C.month_label(ctx.last_closed)}, closed months. To date includes today.", f"Este año = enero a {C.month_label(ctx.last_closed, True)}, meses cerrados. A la fecha incluye hoy.")}</p></div>"""
    return page(ctx, "Sites", body, "sites", 1)


def site_page(ctx: Ctx, s: RP.Site, cs: dict) -> str:
    x = ctx.live[s.key]
    st = RP.status(x, ctx.in_window)
    mons = RP.months_with_data(ctx.mon, [s.key])
    en, es = C.spark_months(mons)
    bars = C.stacked_bars(mons, [(s.name, ctx.colour[s.key], {m: v / 1000.0 for m, v in ctx.mon.get(s.key, {}).items()})],
                          "MWh", en, es, d=1)
    last30 = [(d, v) for d, v in ctx.daily.get(s.key, []) if d >= ctx.today - dt.timedelta(days=30)]
    cats = [d.isoformat() for d, _ in last30]
    dbars = C.stacked_bars(cats, [(s.name, ctx.colour[s.key], {d.isoformat(): (v or 0.0) for d, v in last30})], "kWh",
                           [d.strftime("%d %b") for d, _ in last30], [d.strftime("%d/%m") for d, _ in last30], h=220)
    sy = RP.specific_yield(s, ctx.mon, ctx.last_closed)
    f_now = co2reg.factor(ctx.today.year, s.key)
    started = ctx.started.get(s.key)
    ph = f"background-image:linear-gradient(180deg,rgba(20,18,58,.78),rgba(20,18,58,.92)),url(../../assets/photos/{s.slug}.jpg)" \
        if ctx.brand.get("photos", {}).get(s.slug) else ""
    body = f"""<div class="hero" style="{ph};background-size:cover;background-position:center"><div class="k">{L("Site", "Sitio")} · {esc(s.city)}</div>
<div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap"><div class="card" style="padding:10px 14px;display:flex;align-items:center;height:62px">{logo_html(s, cs).replace('<img ', '<img style="max-height:40px;max-width:150px" ')}</div>
<h1 style="margin:0">{esc(s.name)}</h1>{status_pill(st)}</div>
<div class="sub" style="margin-top:8px">{fmt(s.kwp, 1)} kWp · {L("producing since", "produciendo desde")} {started.strftime("%d/%m/%Y") if started else "-"}</div>
<div class="big"><div class="t"><div class="l"><span class="dot"></span>{L("Power now", "Potencia ahora")}</div><div class="v num">{fmt(x.kw, 0)}<small>kW</small></div><div class="d">{fmt(x.today_kwh, 0)} kWh {L("today", "hoy")}</div></div>
<div class="t"><div class="l">{L("Clean energy to date", "Energía limpia a la fecha")}</div><div class="v num">{fmt(ctx.life[s.key].kwh / 1000, 1)}<small>MWh</small></div><div class="d">{fmt(ctx.ytd[s.key].kwh / 1000, 1)} MWh {L("this year", "este año")}</div></div>
<div class="t"><div class="l">{L("CO2e avoided to date", "CO2e evitado a la fecha")}</div><div class="v num">{fmt(ctx.life[s.key].co2_t, 1)}<small>t</small></div><div class="d">{L("factor", "factor")} {f_now:.3f} kg/kWh</div></div>
<div class="t"><div class="l">{L("Specific yield, 12 months", "Rendimiento específico, 12 meses")}</div><div class="v num">{fmt(sy, 0) if sy else "-"}<small>kWh/kWp</small></div><div class="d">{L("energy per installed kWp", "energía por kWp instalado") if sy else L("after 12 full months", "tras 12 meses completos")}</div></div></div></div>
<div class="sec grid g2"><div class="card"><h3>{L("Power today", "Potencia hoy")}<span class="r">kW</span></h3>{today_chart(ctx, [s])}</div>
<div class="card"><h3>{L("Last 30 days", "Últimos 30 días")}<span class="r">kWh</span></h3>{dbars}</div></div>
<div class="sec card"><h3>{L("Clean energy by month", "Energía limpia por mes")}<span class="r">MWh</span></h3>{bars}</div>
<p class="note"><a href="../index.html">← {L("All sites", "Todos los sitios")}</a></p>"""
    return page(ctx, s.name, body, "sites", 2)


def report_page(ctx: Ctx, pdfs: Dict[str, str], csv_name: str) -> str:
    """The clean energy report: year to date (closed months) and since the
    start, per site and month, with the method. Printed to PDF as is."""
    months_ytd = [m for m in RP.months_between(ctx.ytd_first, ctx.last_closed) if m in set(ctx.months)]
    life_closed = ctx.closed
    rows = ""
    for s in ctx.sites:
        started = ctx.started.get(s.key)
        fac = sorted({f"{co2reg.factor_for_month(m, s.key):.3f}" for m in ctx.mon.get(s.key, {})})
        rows += (f'<tr><td><b>{esc(s.name)}</b><div class="note">{esc(s.city)}</div></td><td class="n">{fmt(s.kwp, 1)}</td>'
                 f'<td>{started.strftime("%d/%m/%Y") if started else "-"}</td><td class="n">{fmt(ctx.ytd[s.key].kwh / 1000, 2)}</td>'
                 f'<td class="n">{fmt(ctx.ytd[s.key].co2_t, 2)}</td><td class="n">{fmt(life_closed[s.key].kwh / 1000, 2)}</td>'
                 f'<td class="n">{fmt(life_closed[s.key].co2_t, 2)}</td><td>{", ".join(fac) or "-"}</td></tr>')
    y, lc = ctx.ytd["_all"], life_closed["_all"]
    rows += (f'<tr class="tot"><td>{L("Total", "Total")}</td><td class="n">{fmt(sum(s.kwp for s in ctx.sites), 1)}</td><td></td>'
             f'<td class="n">{fmt(y.kwh / 1000, 2)}</td><td class="n">{fmt(y.co2_t, 2)}</td><td class="n">{fmt(lc.kwh / 1000, 2)}</td>'
             f'<td class="n">{fmt(lc.co2_t, 2)}</td><td></td></tr>')
    head = "".join(f'<th class="n">{L(C.month_label(m), C.month_label(m, True))}</th>' for m in months_ytd)
    mrows = ""
    for s in ctx.sites:
        cells = "".join(f'<td class="n">{fmt(ctx.mon.get(s.key, {}).get(m, 0) / 1000, 1) if m in ctx.mon.get(s.key, {}) else "-"}</td>'
                        for m in months_ytd)
        mrows += f"<tr><td>{esc(s.name)}</td>{cells}</tr>"
    tot_cells = "".join(f'<td class="n">{fmt(sum(ctx.mon.get(s.key, {}).get(m, 0) for s in ctx.sites) / 1000, 1)}</td>' for m in months_ytd)
    mrows += f'<tr class="tot"><td>MWh</td>{tot_cells}</tr>'
    en, es = C.spark_months(months_ytd)
    series = [(s.name, ctx.colour[s.key], {m: v / 1000.0 for m, v in ctx.mon.get(s.key, {}).items()}) for s in ctx.sites]
    chart = C.stacked_bars(months_ytd, series, "MWh", en, es, d=1, h=230)
    eq = RP.equivalents(y.co2_t)
    year = ctx.ytd_first[:4]
    lm = int(ctx.last_closed[5:7])
    period_en = f"January to {C.MONTHS_FULL_EN[lm - 1]} {year}" if lm > 1 else f"January {year}"
    period_es = f"enero a {C.MONTHS_FULL_ES[lm - 1]} de {year}" if lm > 1 else f"enero de {year}"
    import argia_logo
    cpa_logo = ('<img class="cpa" src="../assets/cpa_logo_white.png" alt="' + esc(ctx.partner) + '">' if ctx.brand.get("logo")
                else '<span class="word">' + esc(ctx.partner.lower()) + "</span>")
    dl = "".join(f'<a class="btn {"" if i == 0 else "ghost"}" href="{n}">{lbl}</a> ' for i, (lbl, n) in enumerate(
        [(L("PDF in English", "PDF en inglés"), pdfs.get("en", "")), (L("PDF in Spanish", "PDF en español"), pdfs.get("es", ""))]) if n)
    dl += f'<a class="btn ghost" href="{csv_name}">{L("Data (CSV)", "Datos (CSV)")}</a>'
    factors = ", ".join(f"{y_}: {f:.3f}" for y_, f in sorted(co2reg.FACTOR_BY_YEAR.items()))
    overrides = [esc(s.name) for s in ctx.sites if s.key in co2reg.PLANT_OVERRIDE]
    ov_en = (f" At the customer's request, {', '.join(overrides)} uses its contracted factor in every ARGIA document, so it is used here too."
             if overrides else "")
    ov_es = (f" A solicitud del cliente, {', '.join(overrides)} usa su factor contratado en todos los documentos de ARGIA, y aquí también."
             if overrides else "")
    body = f"""<div class="noprint" style="display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end;margin-bottom:14px">{dl}</div>
<div class="hero" style="padding:30px 34px"><div class="brand" style="margin-bottom:18px">{cpa_logo}<span class="x">×</span><img class="ar" src="{argia_logo.MARK_URI}" alt="ARGIA"></div>
<div class="k">{L("Clean energy report", "Reporte de energía limpia")}</div>
<h1 style="font-size:32px">{L(f"Solar energy and avoided emissions, {period_en}", f"Energía solar y emisiones evitadas, {period_es}")}</h1>
<div class="sub">{L(f"{len(ctx.sites)} sites in CPA parks · prepared {ctx.today:%d %B %Y} by ARGIA from inverter data", f"{len(ctx.sites)} sitios en parques de CPA · preparado el {ctx.today:%d/%m/%Y} por ARGIA con datos de los inversores")}</div>
<div class="big"><div class="t"><div class="l">{L("Solar energy, year to date", "Energía solar, año a la fecha")}</div><div class="v num">{fmt(y.kwh / 1000, 1)}<small>MWh</small></div></div>
<div class="t"><div class="l">{L("CO2e avoided, year to date", "CO2e evitado, año a la fecha")}</div><div class="v num">{fmt(y.co2_t, 1)}<small>t</small></div></div>
<div class="t"><div class="l">{L("Since the start", "Desde el inicio")}</div><div class="v num">{fmt(lc.kwh / 1000, 1)}<small>MWh</small></div><div class="d">{fmt(lc.co2_t, 1)} t CO2e</div></div>
<div class="t"><div class="l">{L("Equivalent to", "Equivale a")}</div><div class="v num">{fmt(eq["trees"], 0)}</div><div class="d">{L("tree seedlings grown 10 years (this year)", "árboles cultivados 10 años (este año)")}</div></div></div></div>
<div class="sec card"><h3>{L("By site", "Por sitio")}</h3><div style="overflow-x:auto"><table class="t"><tr><th>{L("Site", "Sitio")}</th><th class="n">kWp</th><th>{L("Since", "Desde")}</th>
<th class="n">MWh {year}</th><th class="n">t CO2e {year}</th><th class="n">MWh {L("total", "total")}</th><th class="n">t CO2e {L("total", "total")}</th><th>kg CO2e/kWh</th></tr>{rows}</table></div></div>
<div class="sec card"><h3>{L("By month", "Por mes")}<span class="r">MWh</span></h3>{chart}{C.legend([(s.name, ctx.colour[s.key]) for s in ctx.sites])}
<div style="overflow-x:auto;margin-top:10px"><table class="t"><tr><th>{L("Site", "Sitio")}</th>{head}</tr>{mrows}</table></div></div>
<div class="sec grid g2"><div class="card"><h3>{L("Method", "Método")}</h3><div class="note">
<p>{L("Energy: AC energy measured by each site's inverters, collected every 5 minutes and checked every night against the manufacturer's own counters (the figures ARGIA invoices and reports on). Closed months only; the current month is in the live site.", "Energía: energía AC medida por los inversores de cada sitio, leída cada 5 minutos y verificada cada noche contra los contadores del fabricante (las cifras con que ARGIA factura y reporta). Solo meses cerrados; el mes en curso está en el sitio en vivo.")}</p>
<p>{L(f"Avoided emissions: energy x the Mexican national grid emission factor published by SEMARNAT / CRE for the year (kg CO2e/kWh - {factors}; the newest applies until the next is published), location-based.{ov_en}", f"Emisiones evitadas: energía x factor de emisión del Sistema Eléctrico Nacional publicado por SEMARNAT / CRE para el año (kg CO2e/kWh - {factors}; el más reciente aplica hasta que se publique el siguiente), basado en ubicación.{ov_es}")}</p>
<p>{L("Equivalences: US EPA Greenhouse Gas Equivalencies (0.060 t CO2 per tree seedling grown 10 years; 4.6 t CO2 per passenger car per year; 8.887 kg CO2 per gallon of gasoline). Illustrative only.", "Equivalencias: Greenhouse Gas Equivalencies de la EPA de EE. UU. (0.060 t CO2 por árbol cultivado 10 años; 4.6 t CO2 por auto al año; 8.887 kg CO2 por galón de gasolina). Solo ilustrativas.")}</p></div></div>
<div class="card"><h3>{L("How to use it", "Cómo usarlo")}</h3><div class="note">
<p><b>GRESB</b> - {L("on-site renewable energy generated, per asset and month (CSV).", "energía renovable generada en sitio, por activo y mes (CSV).")}</p>
<p><b>GHG Protocol</b> - {L("tenant: Scope 2 reduction for solar consumed on site; CPA: Scope 3 category 13 (downstream leased assets).", "inquilino: reducción de Alcance 2 por la energía solar consumida en sitio; CPA: Alcance 3 categoría 13 (activos arrendados).")}</p>
<p><b>{L("SDGs", "ODS")}</b> - 7, 9, 13.</p>
<p>{L("Data in the CSV: site, month, kWh, factor, t CO2e, kWp. ARGIA keeps the 5-minute inverter history behind every figure.", "Datos del CSV: sitio, mes, kWh, factor, t CO2e, kWp. ARGIA conserva el historial de 5 minutos de los inversores detrás de cada cifra.")}</p></div></div></div>"""
    return page(ctx, "Clean energy report", body, "report", 1,
                extra_head="<style>@page{size:A4;margin:12mm}@media print{.hero{-webkit-print-color-adjust:exact;print-color-adjust:exact}}</style>")


def led_page(ctx: Ctx) -> str:
    from argia.cpa.report import LED_JS
    f = co2reg.CURRENT
    fields = [("n", L("Light fixtures", "Luminarias"), 200), ("wo", L("Watts per fixture today (e.g. metal halide 400 W + ballast)", "Watts por luminaria hoy (p. ej. aditivo metálico 400 W + balastro)"), 458),
              ("wn", L("Watts per LED fixture", "Watts por luminaria LED"), 150), ("h", L("Hours on per day", "Horas encendidas al día"), 16),
              ("d", L("Days per year", "Días al año"), 360), ("p", L("Electricity price, MXN per kWh", "Precio de la electricidad, MXN por kWh"), 2.4)]
    inputs = "".join(f'<label class="f" for="{k}">{lbl}</label><input class="f" id="{k}" type="number" min="0" step="any" value="{v}">' for k, lbl, v in fields)
    body = f"""<div class="hero"><div class="k">{esc(ctx.partner)} × ARGIA · {L("LED lighting", "Iluminación LED")}</div>
<h1>{L("Better light, a fraction of the energy.", "Mejor luz, una fracción de la energía.")}</h1>
<div class="sub">{L("ARGIA replaces warehouse and yard lighting with LED for CPA tenants: audit, design, installation and measured savings. LED results appear on this site once a project is commissioned and measured.", "ARGIA reemplaza la iluminación de naves y patios por LED para inquilinos de CPA: auditoría, diseño, instalación y ahorro medido. Los resultados LED aparecerán aquí cuando un proyecto se ponga en marcha y se mida.")}</div></div>
<div class="sec grid g2"><div class="card"><h3>{L("Savings estimator", "Estimador de ahorro")}</h3>{inputs}
<p class="note">{L(f"An estimate from your inputs, not a measurement. CO2e at the Mexican grid factor ({f:.3f} kg/kWh).", f"Estimación con sus datos, no una medición. CO2e con el factor de la red mexicana ({f:.3f} kg/kWh).")}</p></div>
<div class="card" style="background:linear-gradient(140deg,#14123a,#1f1d4f 60%,#2a2a7a);color:#fff;border:0"><h3 style="color:#a9d8fb">{L("Every year", "Cada año")}</h3>
<div class="big" style="grid-template-columns:1fr 1fr;margin-top:6px"><div class="t"><div class="l">{L("Energy saved", "Energía ahorrada")}</div><div class="v num"><span id="o_kwh">-</span><small>MWh</small></div></div>
<div class="t"><div class="l">{L("Cost saved", "Ahorro")}</div><div class="v num"><span id="o_mxn">-</span><small>MXN</small></div></div>
<div class="t"><div class="l">CO2e {L("avoided", "evitado")}</div><div class="v num"><span id="o_co2">-</span><small>t</small></div></div>
<div class="t"><div class="l">{L("Lighting load cut", "Reducción de carga")}</div><div class="v num"><span id="o_pct">-</span><small>%</small></div><div class="d"><span id="o_kw">-</span> kW</div></div></div></div></div>
<div class="sec grid g3"><div class="card"><h3>1 · {L("Audit", "Auditoría")}</h3><div class="note">{L("Fixture count, wattage, hours and light levels per area.", "Conteo de luminarias, potencia, horas y niveles de iluminación por área.")}</div></div>
<div class="card"><h3>2 · {L("Design and install", "Diseño e instalación")}</h3><div class="note">{L("LED fixtures sized to the required lux, sensors where they pay, installed around the tenant's operation.", "Luminarias LED dimensionadas al nivel de lux requerido, sensores donde convienen, instaladas sin detener la operación.")}</div></div>
<div class="card"><h3>3 · {L("Measure", "Medición")}</h3><div class="note">{L("Before and after metering; the measured savings join this site next to the solar figures.", "Medición antes y después; el ahorro medido se suma a este sitio junto a las cifras solares.")}</div></div></div>
<script>{LED_JS}
(function(){{function v(id){{var x=parseFloat(document.getElementById(id).value);return isNaN(x)?0:x;}}
function f(x,d){{return x.toLocaleString('en-US',{{minimumFractionDigits:d,maximumFractionDigits:d}});}}
function run(){{var wo=v('wo'),wn=Math.min(v('wn'),wo),h=Math.min(v('h'),24),d=Math.min(v('d'),366);
var r=ledEst(v('n'),wo,wn,h,d,v('p'),{f});document.getElementById('o_kwh').textContent=f(r.kwh_year/1000,1);
document.getElementById('o_mxn').textContent=f(r.mxn_year,0);document.getElementById('o_co2').textContent=f(r.t_co2_year,1);
document.getElementById('o_pct').textContent=f(r.pct,0);document.getElementById('o_kw').textContent=f(r.kw_saved,1);}}
document.querySelectorAll('input.f').forEach(function(i){{i.addEventListener('input',run);}});run();}})();</script>"""
    return page(ctx, "LED lighting", body, "led", 1)


# ------------------------------------------------------------------ build
def find_chromium() -> Optional[str]:
    for b in ("chromium", "chromium-browser", "google-chrome", "chrome-headless-shell"):
        p = shutil.which(b)
        if p:
            return p
    return None


def chromium_pdf(html_path: str, pdf_path: str, fragment: str = "") -> bool:
    exe = find_chromium()
    if not exe:
        return False
    r = subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--no-pdf-header-footer",
                        "--virtual-time-budget=4000", "--print-to-pdf=" + pdf_path, "file://" + html_path + fragment],
                       capture_output=True, text=True, timeout=180)
    return r.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 5000


def brand_files(stage: str) -> dict:
    """Copy the server-only CPA logo/mark and the site photos (whitelist)."""
    out = {"logo": False, "photos": {}}
    os.makedirs(os.path.join(stage, "assets", "photos"), exist_ok=True)
    bdir = os.path.join(CPA_DIR, "brand")
    for name in ("cpa_logo_white.png", "cpa_mark.png"):
        src = os.path.join(bdir, name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(stage, "assets", name))
            out["logo"] = out["logo"] or name == "cpa_logo_white.png"
    fav = os.path.join(bdir, "cpa_mark.png")
    if not os.path.isfile(fav):
        fav = os.path.join(PORTAL_ROOT, "favicon.png")
    if os.path.isfile(fav):
        shutil.copyfile(fav, os.path.join(stage, "favicon.png"))
    return out


def copy_photos(stage: str, sites: Sequence[RP.Site], brand: dict) -> None:
    for s in sites:
        src = os.path.join(PORTAL_ROOT, "monitoring", "assets", f"{s.key.lower()}.jpg")
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(stage, "assets", "photos", f"{s.slug}.jpg"))
            brand["photos"][s.slug] = True


def write(stage: str, rel: str, text: str) -> None:
    p = os.path.join(stage, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def check(stage: str, ctx: Ctx) -> List[str]:
    """Problems that stop the publish: an em dash, a None/nan, a plant key on
    a page, a link to a page that does not exist."""
    errs = []
    keys = [s.key for s in ctx.sites]
    for root, _, files in os.walk(stage):
        for fn in files:
            if not fn.endswith(".html"):
                continue
            p = os.path.join(root, fn)
            t = open(p, encoding="utf-8").read()
            vis = re.sub(r"<script.*?</script>|<style.*?</style>|data:image/[^\"')]+", "", t, flags=re.S)
            if chr(0x2014) in t:
                errs.append(f"{p}: em dash")
            if re.search(r">\s*(None|nan)\s*<", vis):
                errs.append(f"{p}: None/nan")
            for k in keys:
                if re.search(rf"\b{re.escape(k)}\b", vis):
                    errs.append(f"{p}: plant code {k}")
            for href in re.findall(r'href="([^"#:]+\.(?:html|pdf|csv))"', t):
                if not os.path.exists(os.path.normpath(os.path.join(root, href))):
                    errs.append(f"{p}: broken link {href}")
    return errs


def build(cfg: dict, sites, daily, live, now: dt.datetime, stage: str, pdf_cache: Optional[str] = None) -> Ctx:
    os.makedirs(stage, exist_ok=True)
    brand = brand_files(stage)
    copy_photos(stage, sites, brand)
    ctx = Ctx(cfg, sites, daily, live, now, brand)
    write(stage, "index.html", overview(ctx))
    write(stage, "sites/index.html", sites_index(ctx))
    for s, cs in zip(sites, cfg["sites"]):
        write(stage, f"sites/{s.slug}/index.html", site_page(ctx, s, cs))
    write(stage, "led/index.html", led_page(ctx))
    csv_name = f"{ctx.partner}_clean_energy_monthly.csv".replace(" ", "_")
    write(stage, f"report/{csv_name}", RP.csv_monthly(sites, ctx.mon, ctx.months))
    stem = f"{ctx.partner}_Clean_Energy_Report_{ctx.last_closed}".replace(" ", "_")
    pdfs = {}
    render = RENDER_PDF or chromium_pdf
    # first pass without PDF links, print it, then the final page with the links
    write(stage, "report/index.html", report_page(ctx, {}, csv_name))
    for lang in ("en", "es"):
        name = f"{stem}_{lang.upper()}.pdf"
        cached = os.path.join(pdf_cache, f"{name}.{now:%Y%m%d}") if pdf_cache else None
        target = os.path.join(stage, "report", name)
        if cached and os.path.exists(cached):
            shutil.copyfile(cached, target)
        elif render(os.path.join(stage, "report", "index.html"), target, "#" + lang):
            if cached:
                os.makedirs(pdf_cache, exist_ok=True)
                for old in os.listdir(pdf_cache):
                    if old.startswith(name):
                        os.remove(os.path.join(pdf_cache, old))
                shutil.copyfile(target, cached)
        if os.path.exists(target):
            pdfs[lang] = name
    write(stage, "report/index.html", report_page(ctx, pdfs, csv_name))
    return ctx


def publish(stage: str, out: str) -> None:
    old = out + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(out):
        os.replace(out, old)
    os.replace(stage, out)
    shutil.rmtree(old, ignore_errors=True)


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = argv[0] if argv else OUT_DEFAULT
    now = mx_now()
    cfg = load_config()
    sites, daily, live = fetch(cfg, now)
    stage = out + ".stage"
    shutil.rmtree(stage, ignore_errors=True)
    ctx = build(cfg, sites, daily, live, now, stage, pdf_cache=os.path.join(CPA_DIR, "reports"))
    errs = check(stage, ctx)
    if errs:
        print("cpa_gen: NOT published:\n  " + "\n  ".join(errs[:20]), file=sys.stderr)
        shutil.rmtree(stage, ignore_errors=True)
        return 1
    publish(stage, out)
    n = sum(len(f) for _, _, f in os.walk(out))
    print(f"cpa_gen: {len(sites)} sites, {ctx.life['_all'].kwh / 1000:.1f} MWh to date, {ctx.life['_all'].co2_t:.1f} t CO2e, "
          f"{ctx.kw_now():.0f} kW now, {n} files -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
