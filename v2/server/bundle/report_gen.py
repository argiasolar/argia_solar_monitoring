#!/usr/bin/env python3
"""ARGIA reporting site generator — reads PostgreSQL argia_mont, writes static pages.

Pages (paths map 1:1 to the future report.argia.com.mx subdomains):
  /index.html            landing: fleet + sustainability overview, links (EN/ES, print-PDF)
  /financial/index.html  PPA + LaaS financial report (interactive range, EN/ES, print-PDF)
  /<key>/index.html      per-plant performance, all 10 plants, lowercase key (EN/ES, print-PDF)
  /capex/index.html      CAPEX overview (EN/ES, print-PDF)

Run on pio06:  python3 report_gen.py [outroot]
"""
import subprocess
import datetime as dt
import html
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from argia_logo import LOGO_URI  # official wordmark, transparent PNG data URI
from argia_client_logos import CLIENT_LOGOS  # plant_key -> (display name, grayscale data URI)

OUTROOT = sys.argv[1] if len(sys.argv) > 1 else '/www/hosting/monitoring.argia.com.mx/www'
DB = 'argia_mont'
PPA = ['GTO1', 'MEX1', 'MEX2', 'NL1', 'SLP1', 'SLP2']
CAPEX = ['GTO2', 'QRO1', 'NL2', 'MEX3']
LAAS = ['LGTO1', 'LOAX1']
FX_FALLBACK = 17.98  # projected USD/MXN per v1 NOTES


def q(sql):
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', DB,
                        '-t', '-A', '-F', '\t', '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    return [ln.split('\t') for ln in r.stdout.strip().splitlines() if ln.strip()]


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def dim(y, m):
    return (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.date(y, m, 1)).days


# ================= data =================
asof = q("SELECT max(prod_date) FROM daily_production;")[0][0]
first = q("SELECT min(prod_date) FROM daily_production;")[0][0]
gen_at = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

plants = {r[0]: {'customer': r[1], 'brand': r[2], 'kwp': f(r[3]), 'portfolio': r[4],
                 'tariff': f(r[5]), 'om': f(r[6]), 'prb': f(r[7]), 'inv': f(r[8])}
          for r in q("SELECT plant_key, customer, brand, kwp_dc, portfolio, "
                     "coalesce(tariff_mxn_per_kwh,0), coalesce(om_cost_monthly_mxn,0), "
                     "coalesce(pr_baseline,0), coalesce(investment_mxn,0) FROM plant;")}

SLA_TARGET = 0.98   # availability SLA threshold — ASSUMPTION until per-contract SLAs are provided

loans = {}
for r in q("SELECT loan_id, plant_key, project_name, total_installments, first_month, last_month FROM loan;"):
    loans.setdefault(r[1], []).append(
        {'id': r[0], 'name': r[2], 'total': int(f(r[3])), 'first': r[4], 'last': r[5]})

daily = {}   # (key, 'YYYY-MM-DD') -> kwh
for r in q("SELECT plant_key, prod_date, coalesce(energy_kwh,0) FROM daily_production;"):
    daily[(r[0], r[1])] = f(r[2])

monthly_kwh = {}  # (key,'YYYY-MM') -> kwh
for (k, d), v in daily.items():
    monthly_kwh[(k, d[:7])] = monthly_kwh.get((k, d[:7]), 0) + v

contract = {}  # (key,'YYYY-MM') -> {kwh, tariff, fee_ccy}
for r in q("SELECT plant_key, year, month, coalesce(contract_kwh,0), coalesce(tariff_mxn,0), "
           "coalesce(fixed_income_ccy,0) FROM contract_monthly;"):
    contract[(r[0], f'{int(f(r[1])):04d}-{int(f(r[2])):02d}')] = {
        'kwh': f(r[3]), 'tariff': f(r[4]), 'fee': f(r[5])}

debt_m = {}   # (asset,'YYYY-MM') -> payment_mxn ; xr_m: 'YYYY-MM' -> xr
xr_m = {}
for r in q("SELECT l.plant_key, to_char(s.ref_month,'YYYY-MM'), sum(s.payment_mxn), max(s.xr) "
           "FROM loan_schedule s JOIN loan l ON l.loan_id=s.loan_id GROUP BY 1,2;"):
    debt_m[(r[0], r[1])] = f(r[2])
    if len(r) > 3 and f(r[3]):        # max(xr) can be NULL -> psql drops the trailing field
        xr_m[r[1]] = f(r[3])

_r = q("SELECT round(100.0*coalesce(sum(s.payment_mxn) FILTER (WHERE l.currency='USD'),0)"
       "/nullif(sum(s.payment_mxn),0),1) FROM loan_schedule s JOIN loan l ON l.loan_id=s.loan_id;")
usd_share = f(_r[0][0]) if _r and _r[0] else 0.0   # % of portfolio debt service that is USD

avail_d = {}   # (key, 'YYYY-MM-DD') -> availability 0..1 (v2 days only)
for r in q("SELECT plant_key, prod_date, availability FROM daily_production "
           "WHERE availability IS NOT NULL;"):
    if len(r) > 2:
        avail_d[(r[0], r[1])] = f(r[2])

dq_d = {}      # (key, 'YYYY-MM-DD') -> 1 if data_class='full' else 0 (v2 days only)
for r in q("SELECT plant_key, prod_date, data_class FROM daily_production "
           "WHERE data_class IS NOT NULL AND data_class <> '';"):
    if len(r) > 2:
        dq_d[(r[0], r[1])] = 1 if r[2].strip() == 'full' else 0

pr30 = {r[0]: f(r[1]) for r in q(
    f"SELECT plant_key, avg(pr) FROM daily_production WHERE source='v2' AND pr IS NOT NULL "
    f"AND prod_date > date '{asof}' - 30 GROUP BY plant_key;")}
last_seen = {r[0]: r[1] for r in q(
    "SELECT plant_key, max(prod_date) FROM daily_production GROUP BY plant_key;")}


def expected_month_kwh(k, ym):
    """Expected production for a month without (complete) actuals.
    PPA: contracted kWh for that month; else same month last year's actual;
    else the average of this year's actual months. Estimate — rendered grey."""
    c = contract.get((k, ym))
    if c and c.get('kwh'):
        return c['kwh']
    prev = f'{int(ym[:4]) - 1}{ym[4:]}'
    if monthly_kwh.get((k, prev)):
        return monthly_kwh[(k, prev)]
    ys = [v for (kk, m), v in monthly_kwh.items() if kk == k and m[:4] == ym[:4]]
    return sum(ys) / len(ys) if ys else 0.0


def year_months_with_flags(keys):
    """12 columns for the current year: actuals through the asof month (blue),
    expected for the remaining months (grey). Returns (pairs, flags)."""
    year, cur_m = asof[:4], asof[:7]
    pairs, fl = [], []
    for i in range(1, 13):
        m = f'{year}-{i:02d}'
        if m <= cur_m:
            pairs.append((m, sum(monthly_kwh.get((k, m), 0.0) for k in keys)))
            fl.append(False)
        else:
            pairs.append((m, sum(expected_month_kwh(k, m) for k in keys)))
            fl.append(True)
    return pairs, fl


def tariff_for(k, ym):
    c = contract.get((k, ym))
    if c and c['tariff']:
        return c['tariff']
    return plants[k]['tariff']


def asset_name(k):
    if k in plants:
        return plants[k]['customer']
    for pk, ls in loans.items():
        if pk == k and ls:
            return ls[0]['name']
    return k


# ================= financial atoms (daily, per asset) =================
d0 = dt.date.fromisoformat('2024-02-29')
d1 = dt.date.fromisoformat(asof)
atoms = []  # [date, asset, actual_rev, expected_rev, om, debt]
cur = d0
while cur <= d1:
    ds = cur.isoformat()
    ym = ds[:7]
    n = dim(cur.year, cur.month)
    for k in PPA:
        t = tariff_for(k, ym)
        c = contract.get((k, ym), {})
        act = daily.get((k, ds), 0.0) * t
        exp = (c.get('kwh', 0.0) / n) * t
        om = plants[k]['om'] / n
        db = debt_m.get((k, ym), 0.0) / n
        if act or exp or db:
            atoms.append([ds, k, round(act, 2), round(exp, 2), round(om, 2), round(db, 2)])
    for k in LAAS:
        c = contract.get((k, ym), {})
        fee_mxn = c.get('fee', 0.0) * xr_m.get(ym, FX_FALLBACK)
        db = debt_m.get((k, ym), 0.0) / n
        if fee_mxn or db:
            atoms.append([ds, k, round(fee_mxn / n, 2), round(fee_mxn / n, 2), 0.0, round(db, 2)])
    cur += dt.timedelta(days=1)

loanpos = {}
for k in PPA + LAAS:
    ls = loans.get(k, [])
    pick = None
    for l in ls:
        if l['first'][:10] <= asof <= (l['last'][:10] if l['last'] else '9999'):
            if pick is None or l['first'] > pick['first']:
                pick = l
    if pick:
        paid = len(q(f"SELECT 1 FROM loan_schedule WHERE loan_id='{pick['id']}' "
                     f"AND ref_month <= date '{asof}';"))
        loanpos[k] = f"{paid}/{pick['total']}"
    else:
        loanpos[k] = '—'

assets_meta = {k: {'name': asset_name(k),
                   'type': 'PPA' if k in PPA else 'LaaS',
                   'kwp': plants[k]['kwp'] if k in plants else None,
                   'loanpos': loanpos.get(k, '—')}
               for k in PPA + LAAS}

# ================= shared page chrome (matches the ARGIA financial_report style) =================
STYLE = '''
:root{ --bg:#f6f7f8; --surface:#fff; --ink:#202124; --ink2:#5f6368; --muted:#80868b;
 --grid:#eceef0; --axis:#d5d9dd; --border:#e4e7ea; --s1:#2a78d6; --s2:#eb6834;
 --green-bg:#e6f4ea; --green-tx:#137333; --amber-bg:#fdf0dc; --amber-tx:#a05c00;
 --laas-bg:#ecebf6; --laas-tx:#4f4a94; }
body{margin:0;font-family:"Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
 background:var(--bg);color:var(--ink);font-size:15px;}
.wrap{max-width:1040px;margin:0 auto;padding:26px 18px 48px;}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;}
.top h1{font-size:23px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;
 color:#1c2733;margin:0;}
h1.sect{font-size:19px;font-weight:700;margin:28px 0 10px;color:#1c2733;}
.titlerow{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-top:6px;}
.rng{color:#5f6368;font-size:13.5px;font-variant-numeric:tabular-nums;}
.logo{height:26px;width:auto;margin-top:2px;}
.audit{font-size:12.5px;color:#5f6368;line-height:1.65;}
.audit b{color:#3c4043;}
.sub{color:var(--ink2);font-size:13.5px;margin-top:4px;}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0;}
.btn{background:var(--surface);border:1px solid #dadce0;border-radius:8px;
 padding:6px 13px;font-size:13.5px;color:var(--ink);cursor:pointer;text-decoration:none;}
.btn:hover{border-color:#b9bec4;}
.btn.active{border-color:var(--s1);box-shadow:inset 0 0 0 1px var(--s1);}
.nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin:16px 0;}
.nav a{display:block;background:var(--surface);border:1px solid var(--border);
 border-radius:10px;padding:14px 16px;text-decoration:none;color:var(--ink);}
.nav a:hover{border-color:#b9bec4;}
.nav a b{display:block;font-size:14.5px;} .nav a span{font-size:13px;color:var(--ink2);}
.clogo{height:20px;width:auto;max-width:110px;object-fit:contain;display:block;
 margin-bottom:8px;filter:grayscale(1);opacity:.8;transition:filter .25s,opacity .25s;}
.pcard:hover .clogo{filter:none;opacity:1;}
.pcard span{display:block;}
.navbig a{display:flex;gap:14px;align-items:center;padding:17px 18px;
 border-left:3px solid var(--s1);}
.navbig svg{width:30px;height:30px;stroke:var(--s1);flex-shrink:0;}
.navbig .nb b{font-size:14.5px;} .navbig .nb{display:block;}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:16px 0;}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:13px 16px;}
.tlabel{font-size:13px;color:var(--ink2);}
.tval{font-size:23px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums;}
.thero{font-size:27px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums;}
.unit{font-size:13px;font-weight:400;color:var(--ink2);}
.tsub{font-size:12px;color:var(--muted);margin-top:2px;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
 padding:16px 18px;margin:14px 0;overflow-x:auto;}
.card h2{font-size:14px;font-weight:600;margin:0 0 6px;}
.note{font-size:12.5px;color:var(--muted);margin:0 0 8px;}
svg{max-width:100%;height:auto;display:block;}
.grid{stroke:var(--grid);stroke-width:1;} .axis{stroke:var(--axis);stroke-width:1;}
.tick{fill:var(--muted);font-size:12px;} .lab{fill:var(--ink2);font-size:12.5px;}
.bar{fill:var(--s1);} .bar:hover{opacity:.85;}
.bar.exp{fill:#c9ced4;}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round;}
.line.s1{stroke:var(--s1);} .line.s2{stroke:var(--s2);} .line.rev{stroke:#1e8e3e;}
.tile.good{background:#f2faf4;border-color:#bfe3c9;}
.tile.warn{background:#fdf6e7;border-color:#eed7a4;}
.tile.bad{background:#fdefee;border-color:#f0b9b4;}
.dot{stroke:var(--surface);stroke-width:2;} .dot.s1{fill:var(--s1);} .dot.s2{fill:var(--s2);}
.hit{fill:transparent;}
.legend{display:flex;gap:14px;font-size:13px;color:var(--ink2);margin:0 0 6px;}
.key{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px;}
table{border-collapse:collapse;width:100%;font-size:13.5px;}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--grid);}
th{color:var(--ink2);font-weight:600;font-size:12px;}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}
.badge{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;
 background:var(--green-bg);color:var(--green-tx);}
.badge.laas{background:var(--laas-bg);color:var(--laas-tx);}
.pill{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12.5px;
 background:var(--green-bg);color:var(--green-tx);font-variant-numeric:tabular-nums;}
.pill.warn{background:var(--amber-bg);color:var(--amber-tx);}
details{font-size:13px;color:var(--ink2);}
details summary{cursor:pointer;font-weight:600;font-size:14px;color:var(--ink);}
details[open] summary{margin-bottom:6px;}
.pdfrow{text-align:center;margin:22px 0 4px;}
footer{font-size:12px;color:var(--muted);margin-top:20px;line-height:1.6;}
a{color:var(--s1);}
@media print{
 body{background:#fff;color:#000;} .controls,.noprint{display:none !important;}
 .card,.tile,.nav a{border:1px solid #ddd;box-shadow:none;background:#fff;}
 .wrap{max-width:100%;padding:0;} }
'''

I18N_JS = '''
<script>
function argiaLogout(){fetch('/',{headers:{Authorization:'Basic eDp4'}}).catch(()=>{})
 .finally(()=>{location.href='/logged-out.html';});}
function setLang(l){
 document.querySelectorAll('[data-en]').forEach(e=>{e.textContent=e.dataset[l]||e.dataset.en;});
 document.querySelectorAll('.lang-btn').forEach(b=>b.classList.toggle('active',b.dataset.l===l));
 document.documentElement.lang = l==='es'?'es':'en';
 try{localStorage.setItem('argia_lang',l);}catch(e){}
}
window.addEventListener('DOMContentLoaded',()=>{
 let l='en'; try{l=localStorage.getItem('argia_lang')||'en';}catch(e){}
 setLang(l);
});
</script>'''


# Client-side range engine for plant pages. Plain string (no f-string braces).
# Expects globals injected per page: D (dates), E (kWh), RV (revenue MXN or null),
# AV ({date: availability}), ASOF.
PLANT_JS = r'''
<script>
const $=id=>document.getElementById(id);
const nf=(n,d)=>n.toLocaleString('en-US',{maximumFractionDigits:d===undefined?0:d});
function yt(m){if(m<=0)return[0,1];let s=Math.pow(10,Math.floor(Math.log10(m)));
 if(m/s>5)s*=2;if(m/s<2)s/=2;const o=[];for(let v=0;v<=m*1.001;v+=s)o.push(v);
 if(o[o.length-1]<m)o.push(o[o.length-1]+s);return o;}
function colsvg(labs,vals,revs,unit,runit,cl){
 const W=980,H=250,pl=52,pr=revs?60:8,pt=14,pb=28,pw=W-pl-pr,ph=H-pt-pb;
 const n=vals.length; if(!n)return '<p class="note">—</p>';
 const vmax=Math.max.apply(null,vals.concat(cl||[]))||1, tk=yt(vmax), top=tk[tk.length-1];
 let s='<svg viewBox="0 0 '+W+' '+H+'" role="img">';
 tk.forEach(v=>{const y=pt+ph*(1-v/top);
  s+='<line x1="'+pl+'" y1="'+y.toFixed(1)+'" x2="'+(W-pr)+'" y2="'+y.toFixed(1)+'" class="grid"/>';
  s+='<text x="'+(pl-6)+'" y="'+(y+4).toFixed(1)+'" class="tick" text-anchor="end">'+nf(v)+'</text>';});
 const slot=pw/n, bw=Math.min(40,Math.max(2,slot-2));
 for(let i=0;i<n;i++){const h=ph*vals[i]/top, x=pl+i*slot+(slot-bw)/2, y=pt+ph-h;
  s+='<rect class="bar" x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+bw.toFixed(1)+
     '" height="'+Math.max(h,0).toFixed(1)+'" rx="2"><title>'+labs[i]+': '+nf(vals[i],1)+' '+unit+
     (revs?' · '+nf(revs[i])+' '+runit:'')+'</title></rect>';}
 if(cl&&cl.some(v=>v>0)){
  let d='';for(let i=0;i<n;i++){const x=pl+i*slot+slot/2, y=pt+ph*(1-Math.min(cl[i],top)/top);
   d+=(i?'L':'M')+x.toFixed(1)+','+y.toFixed(1);}
  s+='<path class="line s2" d="'+d+'"><title>expected ('+unit+')</title></path>';}
 if(revs&&revs.some(v=>v>0)){const rmax=Math.max.apply(null,revs)||1, rtk=yt(rmax), rtop=rtk[rtk.length-1];
  rtk.forEach(v=>{const y=pt+ph*(1-v/rtop);
   s+='<text x="'+(W-pr+8)+'" y="'+(y+4).toFixed(1)+'" class="tick" fill="#1e8e3e">'+nf(v)+'</text>';});
  let d='';for(let i=0;i<n;i++){const x=pl+i*slot+slot/2, y=pt+ph*(1-revs[i]/rtop);
   d+=(i?'L':'M')+x.toFixed(1)+','+y.toFixed(1);}
  s+='<path class="line rev" d="'+d+'"/>';}
 const ev=Math.max(1,Math.floor(n/12));
 for(let i=0;i<n;i+=ev){s+='<text x="'+(pl+i*slot+slot/2).toFixed(1)+'" y="'+(H-8)+
   '" class="tick" text-anchor="middle">'+labs[i]+'</text>';}
 s+='<line x1="'+pl+'" y1="'+(pt+ph)+'" x2="'+(W-pr)+'" y2="'+(pt+ph)+'" class="axis"/></svg>';
 return s;}
function compute(){
 const d0=$('d0').value, d1=$('d1').value; if(!d0||!d1||d0>d1)return;
 const days=Math.round((new Date(d1)-new Date(d0))/864e5)+1;
 let idx=[];for(let i=0;i<D.length;i++)if(D[i]>=d0&&D[i]<=d1)idx.push(i);
 let prod=0,rev=0,ctr=0,avs=[],dqn=0,dqf=0;
 idx.forEach(i=>{prod+=E[i];if(RV)rev+=RV[i];if(C)ctr+=C[i];
  const a=AV[D[i]];if(a!=null)avs.push(a);
  const dq=DQ[D[i]];if(dq!=null){dqn++;dqf+=dq;}});
 $('r_prod').textContent=nf(prod/1000,2);
 if($('r_rev')){if(rev>=1e6){$('r_rev').textContent=nf(rev/1e6,2);$('r_rev_u').textContent='M MXN';}
  else{$('r_rev').textContent=nf(rev);$('r_rev_u').textContent='MXN';}}
 const av=avs.length?avs.reduce((x,y)=>x+y,0)/avs.length:null;
 $('r_avail').textContent=av!=null?(100*av).toFixed(1)+'%':'—';
 const rr=$('r_range');rr.textContent=d0+' – '+d1;
 document.querySelectorAll('.rdays').forEach(e=>e.textContent=days+' d');
 const semTile=(id,cls)=>{const e=$(id);if(e)e.className='tile'+(cls?' '+cls:'');};
 $('r_co2').textContent=(prod/1000*CO2F).toFixed(1);
 if(C&&ctr>0){const pct=prod/ctr;
  $('r_vsctr').textContent=' · '+(100*pct).toFixed(0)+'% '+VSL;
  semTile('t_prod',pct>=0.95?'good':pct>=0.8?'warn':'bad');
 }else{$('r_vsctr').textContent='';semTile('t_prod','');}
 if(av!=null){$('r_sla').textContent=av>=SLA?'MET':'BREACH';
  semTile('t_avail',av>=SLA?'good':av>=SLA-0.03?'warn':'bad');
 }else{$('r_sla').textContent='—';semTile('t_avail','');}
 if(dqn){const q2=dqf/dqn;$('r_dq').textContent=(100*q2).toFixed(0)+'%';
  semTile('t_dq',q2>=0.95?'good':q2>=0.8?'warn':'bad');
 }else{$('r_dq').textContent='—';semTile('t_dq','');}
 let labs,vals,revs=null,cexp=null,unit,runit;
 if(days>92){const g={},gr={},gc={};
  idx.forEach(i=>{const m=D[i].slice(0,7);g[m]=(g[m]||0)+E[i];
   if(RV)gr[m]=(gr[m]||0)+RV[i];if(C)gc[m]=(gc[m]||0)+C[i];});
  labs=Object.keys(g).sort();vals=labs.map(m=>g[m]/1000);
  if(RV)revs=labs.map(m=>gr[m]/1000);if(C)cexp=labs.map(m=>(gc[m]||0)/1000);
  unit='MWh';runit='k MXN';
 }else{labs=idx.map(i=>D[i].slice(5));vals=idx.map(i=>E[i]);
  if(RV)revs=idx.map(i=>RV[i]);if(C)cexp=idx.map(i=>C[i]);unit='kWh';runit='MXN';}
 $('d_unit').textContent=unit+(RV?' · money '+runit:'');
 $('dchart').innerHTML=colsvg(labs,vals,revs,unit,runit,cexp);
}
const fd=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+
 String(d.getDate()).padStart(2,'0');   // local, never toISOString (UTC shifts a day)
function preset(w){
 const a=new Date(ASOF+'T00:00:00');let d0,d1=ASOF;
 if(w==='30d'){const s=new Date(a);s.setDate(s.getDate()-29);d0=fd(s);}
 else if(w==='mtd'){d0=ASOF.slice(0,8)+'01';}
 else if(w==='prev'){d0=fd(new Date(a.getFullYear(),a.getMonth()-1,1));
  d1=fd(new Date(a.getFullYear(),a.getMonth(),0));}
 else if(w==='ytd'){d0=ASOF.slice(0,4)+'-01-01';}
 else{d0=D[0];}
 $('d0').value=d0;$('d1').value=d1;compute();
}
window.addEventListener('DOMContentLoaded',()=>{
 preset('30d');
 $('d0').addEventListener('change',compute);
 $('d1').addEventListener('change',compute);
});
</script>'''


def t(en, es):
    return f'<span data-en="{html.escape(en, quote=True)}" data-es="{html.escape(es, quote=True)}">{html.escape(en)}</span>'


LOGO = f'<img class="logo" src="{LOGO_URI}" alt="ARGIA SOLAR">'


def chrome_top(title_en, title_es, sub, home='.', show_home=True, range_id=None,
               right_sub=''):
    home_btn = f'<a class="btn" href="{home}/">{t("Home", "Inicio")}</a>' if show_home else ''
    rng = f'<span class="rng" id="{range_id}"></span>' if range_id else ''
    right = f'<span class="sub" style="margin-left:auto">{right_sub}</span>' if right_sub else ''
    return f'''<div class="top">
 <div><div class="titlerow"><h1>{t(title_en, title_es)}</h1>{rng}</div><div class="sub">{sub}</div></div>
 {LOGO}</div>
<div class="controls noprint">
 {home_btn}
 <button class="btn lang-btn" data-l="en" onclick="setLang('en')">EN</button>
 <button class="btn lang-btn" data-l="es" onclick="setLang('es')">ES</button>
 <button class="btn" onclick="window.print()">{t('Download PDF','Descargar PDF')}</button>
 <button class="btn" onclick="argiaLogout()">{t('Log out','Cerrar sesión')}</button>
 {right}
</div>'''


def pdf_bottom():
    return (f'<div class="pdfrow noprint"><button class="btn" onclick="window.print()">'
            f'{t("Download PDF (current selection)","Descargar PDF (selección actual)")}</button></div>')


def page(body, title):
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow">'
            f'<title>{html.escape(title)}</title><style>{STYLE}</style></head>'
            f'<body><div class="wrap">{body}</div>{I18N_JS}</body></html>')


# ================= svg helpers =================
def yticks(vmax):
    if vmax <= 0:
        return [0, 1]
    step = 10 ** math.floor(math.log10(vmax))
    if vmax / step > 5:
        step *= 2
    if vmax / step < 2:
        step /= 2
    out, v = [], 0.0
    while v <= vmax * 1.001:
        out.append(v)
        v += step
    if out[-1] < vmax:          # top tick must COVER vmax or lines/bars clip past the plot
        out.append(out[-1] + step)
    return out


MONTH_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
MONTH_ES = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']


def columns_svg(pairs, unit, scale=1.0, width=980, height=240, lab_fmt=None,
                show_values=False, month_names=False, flags=None):
    """flags: optional list of bools parallel to pairs — True renders the column
    grey ('expected' month) instead of brand blue."""
    if not pairs:
        return '<p class="note">—</p>'
    vals = [f(v) / scale for _, v in pairs]
    vmax = max(vals) or 1
    tks = yticks(vmax)
    pad_l, pad_b, pad_t = 50, 28, (30 if show_values else 12)
    W, H = width, height
    pw, ph = W - pad_l - 8, H - pad_t - pad_b
    n = len(pairs)
    slot = pw / n
    bw = min(56 if month_names else 24, max(4, slot - 2))
    out = [f'<svg viewBox="0 0 {W} {H}" role="img">']
    for tk in tks:
        y = pad_t + ph * (1 - tk / tks[-1])
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W-8}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" class="tick" text-anchor="end">{int(tk):,}</text>')
    for i, (m, v) in enumerate(pairs):
        val = f(v) / scale
        h = ph * val / tks[-1]
        x = pad_l + i * slot + (slot - bw) / 2
        y = pad_t + ph - h
        r = min(4, bw / 2, h)
        cls = 'bar exp' if (flags and flags[i]) else 'bar'
        tag = ' (expected)' if (flags and flags[i]) else ''
        out.append(f'<path class="{cls}" d="M{x:.1f},{y+r:.1f} q0,-{r} {r},-{r} h{bw-2*r:.1f} '
                   f'q{r},0 {r},{r} v{max(h-r,0):.1f} h-{bw:.1f} z">'
                   f'<title>{m}{tag}: {val:,.1f} {unit}</title></path>')
        if show_values:
            ly = max(y - 6, 12)   # clamp so tall columns never push the label out of view
            out.append(f'<text x="{x+bw/2:.1f}" y="{ly:.1f}" class="tick" '
                       f'text-anchor="middle" font-weight="600">{val:,.0f}</text>')
    ev = max(1, n // 10)
    for i, (m, _) in enumerate(pairs):
        if i % ev == 0:
            if month_names and len(m) >= 7:
                mi = int(m[5:7]) - 1
                out.append(f'<text x="{pad_l+i*slot+slot/2:.1f}" y="{H-8}" class="tick" '
                           f'text-anchor="middle" data-en="{MONTH_EN[mi]}" '
                           f'data-es="{MONTH_ES[mi]}">{MONTH_EN[mi]}</text>')
                continue
            lab = lab_fmt(m) if lab_fmt else m
            out.append(f'<text x="{pad_l+i*slot+slot/2:.1f}" y="{H-8}" class="tick" '
                       f'text-anchor="middle">{lab}</text>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t+ph}" x2="{W-8}" y2="{pad_t+ph}" class="axis"/>')
    out.append('</svg>')
    return ''.join(out)


def monthly_svg(pairs, flags, contract_mwh=None, revenue_kmxn=None,
                width=980, height=280):
    """12-month plant chart: MWh bars (grey = expected), optional orange
    contract-baseline polyline (left axis) and green revenue line (right axis,
    k MXN, its own scale)."""
    vals = [f(v) / 1000.0 for _, v in pairs]
    cv = contract_mwh if (contract_mwh and any(contract_mwh)) else None
    rv = revenue_kmxn if (revenue_kmxn and any(revenue_kmxn)) else None
    vmax = max(vals + (cv or [0.0])) or 1
    tks = yticks(vmax)
    top = tks[-1]
    pad_l, pad_b, pad_t = 52, 28, 30
    pad_r = 64 if rv else 10
    W, H = width, height
    pw, ph = W - pad_l - pad_r, H - pad_t - pad_b
    n = len(pairs)
    slot = pw / n
    bw = min(52, max(6, slot - 10))
    out = [f'<svg viewBox="0 0 {W} {H}" role="img">']
    for tk in tks:
        y = pad_t + ph * (1 - tk / top)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W-pad_r}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" class="tick" text-anchor="end">{int(tk):,}</text>')
    for i, (m, v) in enumerate(pairs):
        val = vals[i]
        h = ph * val / top
        x = pad_l + i * slot + (slot - bw) / 2
        y = pad_t + ph - h
        cls = 'bar exp' if flags[i] else 'bar'
        tag = ' (expected)' if flags[i] else ''
        out.append(f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                   f'height="{max(h,0):.1f}" rx="3"><title>{m}{tag}: {val:,.1f} MWh'
                   + (f' · {revenue_kmxn[i]:,.0f} k MXN' if rv else '') + '</title></rect>')
        ly = max(y - 6, 12)
        out.append(f'<text x="{x+bw/2:.1f}" y="{ly:.1f}" class="tick" '
                   f'text-anchor="middle" font-weight="600">{val:,.0f}</text>')
        mi = int(m[5:7]) - 1
        out.append(f'<text x="{pad_l+i*slot+slot/2:.1f}" y="{H-8}" class="tick" '
                   f'text-anchor="middle" data-en="{MONTH_EN[mi]}" data-es="{MONTH_ES[mi]}">'
                   f'{MONTH_EN[mi]}</text>')
    if cv:
        d = ' '.join(f'{"M" if i == 0 else "L"}{pad_l+i*slot+slot/2:.1f},'
                     f'{pad_t+ph*(1-min(c,top)/top):.1f}' for i, c in enumerate(cv))
        out.append(f'<path class="line s2" d="{d}"><title>Contract baseline (MWh)</title></path>')
    if rv:
        rtks = yticks(max(rv))
        rtop = rtks[-1]
        for tk in rtks:
            y = pad_t + ph * (1 - tk / rtop)
            out.append(f'<text x="{W-pad_r+8}" y="{y+4:.1f}" class="tick" fill="#1e8e3e">{int(tk):,}</text>')
        d = ' '.join(f'{"M" if i == 0 else "L"}{pad_l+i*slot+slot/2:.1f},'
                     f'{pad_t+ph*(1-r/rtop):.1f}' for i, r in enumerate(rv))
        out.append(f'<path class="line rev" d="{d}"><title>Revenue (k MXN)</title></path>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t+ph}" x2="{W-pad_r}" y2="{pad_t+ph}" class="axis"/>')
    out.append('</svg>')
    return ''.join(out)


def lines2_svg(mlist, s1v, s2v, n1, n2, unit, scale=1.0, width=980, height=240):
    if not mlist:
        return '<p class="note">—</p>'
    v1 = [f(s1v.get(m, 0)) / scale for m in mlist]
    v2 = [f(s2v.get(m, 0)) / scale for m in mlist]
    vmax = max(v1 + v2) or 1
    tks = yticks(vmax)
    pad_l, pad_b, pad_t, pad_r = 54, 28, 12, 104
    W, H = width, height
    pw, ph = W - pad_l - pad_r, H - pad_t - pad_b
    n = len(mlist)
    def pt(i, v):
        return (pad_l + pw * i / max(n - 1, 1), pad_t + ph * (1 - v / tks[-1]))
    out = [f'<svg viewBox="0 0 {W} {H}" role="img">']
    for tk in tks:
        y = pad_t + ph * (1 - tk / tks[-1])
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W-pad_r}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" class="tick" text-anchor="end">{int(tk):,}</text>')
    for cls, vals, nm in (('s1', v1, n1), ('s2', v2, n2)):
        d = ' '.join(f'{"M" if i==0 else "L"}{pt(i,v)[0]:.1f},{pt(i,v)[1]:.1f}'
                     for i, v in enumerate(vals))
        out.append(f'<path class="line {cls}" d="{d}"/>')
        ex, ey = pt(n - 1, vals[-1])
        out.append(f'<circle class="dot {cls}" cx="{ex:.1f}" cy="{ey:.1f}" r="4"/>')
        out.append(f'<text x="{ex+9:.1f}" y="{ey+4:.1f}" class="lab">{nm}</text>')
        for i, v in enumerate(vals):
            x, y = pt(i, v)
            out.append(f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="9">'
                       f'<title>{mlist[i]} · {nm}: {v:,.0f} {unit}</title></circle>')
    ev = max(1, n // 8)
    for i, m in enumerate(mlist):
        if i % ev == 0:
            out.append(f'<text x="{pad_l+pw*i/max(n-1,1):.1f}" y="{H-8}" class="tick" '
                       f'text-anchor="middle">{m[2:]}</text>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t+ph}" x2="{W-pad_r}" y2="{pad_t+ph}" class="axis"/>')
    out.append('</svg>')
    return ''.join(out)


# ================= page: plant performance (PPA + CAPEX) =================
def plant_page(k):
    p = plants[k]
    is_ppa = p['portfolio'] == 'PPA'
    mrows = sorted((ym, v) for (kk, ym), v in monthly_kwh.items() if kk == k)
    life = sum(v for _, v in mrows)
    pr = pr30.get(k)
    stale = last_seen.get(k, asof) < asof

    # daily series for the client-side range engine (also decides which tiles exist)
    series = sorted((d, v) for (kk, d), v in daily.items() if kk == k)
    dlist = [d for d, _ in series]
    elist = [round(v, 1) for _, v in series]
    rlist = [round(v * tariff_for(k, d[:7]), 2) for d, v in series]
    if not any(rlist):
        rlist = None            # no tariff anywhere -> no money on this page
    clist = [round(contract.get((k, d[:7]), {}).get('kwh', 0.0)
                   / dim(int(d[:4]), int(d[5:7])), 1) for d, _ in series]
    if not any(clist):
        clist = None            # no contract/expected data
    avmap = {d: round(avail_d[(k, d)], 4) for d, _ in series if (k, d) in avail_d}
    dqmap = {d: dq_d[(k, d)] for d, _ in series if (k, d) in dq_d}

    tiles = ['<div class="tiles">',
             f'<div class="tile"><div class="tlabel">{t("Lifetime production","Producción histórica")}</div>'
             f'<div class="tval">{life/1000:,.1f} <span class="unit">MWh</span></div>'
             f'<div class="tsub">{mrows[0][0] if mrows else ""} → {asof}</div></div>',
             f'<div class="tile"><div class="tlabel">{t("Capacity","Capacidad")}</div>'
             f'<div class="tval">{p["kwp"]:,.1f} <span class="unit">kWp DC</span></div>'
             f'<div class="tsub">{p["brand"]}</div></div>',
             f'<div class="tile" id="t_prod"><div class="tlabel">{t("Production, selected range","Producción, rango elegido")}</div>'
             f'<div class="tval"><span id="r_prod">—</span> <span class="unit">MWh</span></div>'
             f'<div class="tsub"><span id="r_range"></span> · <span class="rdays"></span>'
             f'<span id="r_vsctr"></span></div></div>']
    if rlist:
        money_lab = (t("Est. revenue, selected range", "Ingreso est., rango elegido") if is_ppa
                     else t("Est. savings, selected range", "Ahorro est., rango elegido"))
        money_sub = (f'{p["tariff"]:.3f} MXN/kWh' if is_ppa
                     else t("production × your grid tariff", "producción × su tarifa de red"))
        tiles.append(f'<div class="tile"><div class="tlabel">{money_lab}</div>'
                     f'<div class="tval"><span id="r_rev">—</span> <span class="unit" id="r_rev_u">MXN</span></div>'
                     f'<div class="tsub">{money_sub}</div></div>')
    tiles.append(f'<div class="tile"><div class="tlabel">{t("CO2 avoided, selected range","CO2 evitado, rango elegido")}</div>'
                 f'<div class="tval"><span id="r_co2">—</span> <span class="unit">t</span></div>'
                 f'<div class="tsub">{CO2_T_PER_MWH} tCO2/MWh</div></div>')
    if not is_ppa and p['inv'] > 0 and rlist:
        life_sav = sum(rlist)
        pct = life_sav / p['inv'] * 100
        last12 = [r for d2, r in zip(dlist, rlist) if d2 >= (
            dt.date.fromisoformat(asof) - dt.timedelta(days=365)).isoformat()]
        eta = ''
        if sum(last12) > 0:
            months_left = max(p['inv'] - life_sav, 0) / (sum(last12) / 12.0)
            eta_d = dt.date.fromisoformat(asof) + dt.timedelta(days=months_left * 30.4)
            eta = f' · {t("est. payback","recuperación est.")} {eta_d.strftime("%Y-%m")}'
        pcls = 'good' if pct >= 100 else ''
        tiles.append(f'<div class="tile {pcls}"><div class="tlabel">{t("Investment recovered","Inversión recuperada")}</div>'
                     f'<div class="tval">{pct:,.0f}%</div>'
                     f'<div class="tsub">{life_sav/1e6:,.2f} / {p["inv"]/1e6:,.2f} M MXN{eta}</div></div>')
    tiles.append(f'<div class="tile" id="t_avail"><div class="tlabel">{t("Availability, selected range","Disponibilidad, rango elegido")}</div>'
                 f'<div class="tval" id="r_avail">—</div>'
                 f'<div class="tsub">SLA {SLA_TARGET*100:.0f}%: <b id="r_sla">—</b> · '
                 f'{t("assumed target","objetivo supuesto")}</div></div>')
    tiles.append(f'<div class="tile" id="t_dq"><div class="tlabel">{t("Data quality, selected range","Calidad de datos, rango elegido")}</div>'
                 f'<div class="tval" id="r_dq">—</div>'
                 f'<div class="tsub">{t("full-coverage days (v2)","días con cobertura completa (v2)")}</div></div>')
    if pr:
        base = p.get('prb') or 0
        prc = ('good' if pr >= base else 'warn' if pr >= base - 0.05 else 'bad') if base else ''
        tiles.append(f'<div class="tile {prc}"><div class="tlabel">{t("Performance ratio, 30d","Performance ratio, 30d")}</div>'
                     f'<div class="tval">{pr*100:,.1f}%</div>'
                     f'<div class="tsub">{t("baseline","línea base")} {base*100:,.0f}%</div></div>')
    tiles.append('</div>')

    warn = ''
    if stale:
        warn = (f'<div class="card" style="border-color:var(--warn)"><b>'
                f'{t("Data ends " + last_seen.get(k, "?") + " — collection for this plant is currently interrupted.", "Datos hasta " + last_seen.get(k, "?") + " — la captura de datos de esta planta está interrumpida.")}'
                '</b></div>')

    controls = f'''<div class="controls noprint">
 <label class="sub">{t("From","Desde")} <input type="date" id="d0" class="btn"></label>
 <label class="sub">{t("To","Hasta")} <input type="date" id="d1" class="btn"></label>
 <button class="btn" onclick="preset('30d')">{t("Last 30 days","Últimos 30 días")}</button>
 <button class="btn" onclick="preset('mtd')">{t("Month to date","Mes en curso")}</button>
 <button class="btn" onclick="preset('prev')">{t("Previous month","Mes anterior")}</button>
 <button class="btn" onclick="preset('ytd')">{t("Year to date","Año en curso")}</button>
 <button class="btn" onclick="preset('all')">{t("Full range","Rango completo")}</button>
 <a class="btn" href="https://monitoring.argia.com.mx/{k.lower()}/">{t("Live monitoring ↗","Monitoreo en vivo ↗")}</a>
 <span class="sub rdays"></span>
</div>'''

    body = [chrome_top(p['customer'], p['customer'],
                       f'{k} · {p["portfolio"]} · {t("data","datos")} → {last_seen.get(k, asof)} · {gen_at}',
                       home='..'), controls, ''.join(tiles), warn]

    rev_leg = ''
    if clist:
        exp_word = t("contract (daily)", "contrato (diario)") if is_ppa else t("expected (daily)", "esperado (diario)")
        rev_leg += f' · <span class="key" style="background:var(--s2)"></span>{exp_word}'
    if rlist:
        money_word = (t("revenue (right axis)", "ingreso (eje derecho)") if is_ppa
                      else t("savings (right axis)", "ahorro (eje derecho)"))
        rev_leg += f' · <span class="key" style="background:#1e8e3e"></span>{money_word}'
    body.append(f'<div class="card"><h2>{t("Daily production — selected range","Producción diaria — rango elegido")}</h2>'
                f'<p class="note"><span id="d_unit">kWh</span>{rev_leg}</p>'
                '<div id="dchart"></div></div>')

    y12, yfl = year_months_with_flags([k])
    cml = [contract.get((k, m), {}).get('kwh', 0.0) / 1000.0 for m, _ in y12]
    if not any(cml):
        cml = None
    rml = [round(v * tariff_for(k, m) / 1000.0, 1) for m, v in y12]
    if not any(rml):
        rml = None
    legend = (f'<div class="legend"><span><span class="key" style="background:var(--s1)"></span>'
              f'{t("actual MWh","MWh real")}</span>'
              f'<span><span class="key" style="background:#c9ced4"></span>'
              f'{t("expected MWh","MWh esperado")}</span>')
    if cml:
        base_word = (t("contract baseline", "línea base contractual") if is_ppa
                     else t("expected baseline", "línea base esperada"))
        legend += f'<span><span class="key" style="background:var(--s2)"></span>{base_word}</span>'
    if rml:
        m_word = (t("revenue, k MXN (right axis)", "ingreso, k MXN (eje derecho)") if is_ppa
                  else t("savings, k MXN (right axis)", "ahorro, k MXN (eje derecho)"))
        legend += f'<span><span class="key" style="background:#1e8e3e"></span>{m_word}</span>'
    legend += '</div>'
    body.append(f'<div class="card"><h2>{t("Monthly production","Producción mensual")} · {asof[:4]}</h2>'
                + legend + monthly_svg(y12, yfl, contract_mwh=cml, revenue_kmxn=rml) + '</div>')

    vs_lab = 'vs contract' if is_ppa else 'vs expected'
    body.append('<script>const D=' + json.dumps(dlist) + ';const E=' + json.dumps(elist)
                + ';const RV=' + (json.dumps(rlist) if rlist else 'null')
                + ';const C=' + (json.dumps(clist) if clist else 'null')
                + ';const AV=' + json.dumps(avmap)
                + ';const DQ=' + json.dumps(dqmap)
                + f';const VSL="{vs_lab}";const CO2F={CO2_T_PER_MWH};'
                + f'const SLA={SLA_TARGET};const ASOF="{asof}";</script>' + PLANT_JS)

    if is_ppa or clist or rlist:
        ctr_word = t("Contract", "Contrato") if is_ppa else t("Expected", "Esperado")
        ms = [m for m, _ in mrows][-13:]
        cvals = {m: contract.get((k, m), {}).get('kwh', 0.0) for m in ms}
        avals = {m: monthly_kwh.get((k, m), 0.0) for m in ms}
        title_ln = (t("Actual vs. contracted energy", "Energía real vs. contratada") if is_ppa
                    else t("Actual vs. expected energy", "Energía real vs. esperada"))
        body.append(f'<div class="card"><h2>{title_ln}</h2>'
                    '<p class="note">MWh · 13 m</p>'
                    f'<div class="legend"><span><span class="key" style="background:var(--s1)"></span>'
                    f'{t("Actual","Real")}</span><span><span class="key" style="background:var(--s2)"></span>'
                    f'{ctr_word}</span></div>'
                    + lines2_svg(ms, avals, cvals, 'Act', 'Exp' if not is_ppa else 'Ctr',
                                 'MWh', scale=1000.0) + '</div>')
        rows = []
        ta = tc = trv = 0.0
        av_ms = []
        for m in ms[-6:]:
            a, cc = avals.get(m, 0), cvals.get(m, 0)
            pct = a / cc * 100 if cc else 0
            tr = tariff_for(k, m)
            avs = [v for (kk, d), v in avail_d.items() if kk == k and d[:7] == m]
            mav = sum(avs) / len(avs) if avs else None
            if mav is not None:
                av_ms.append(mav)
            av_cell = f'{mav*100:,.1f}%' if mav is not None else '—'
            ta += a; tc += cc; trv += a * tr
            rows.append(f'<tr><td>{m}</td><td class="num">{a:,.0f}</td><td class="num">{cc:,.0f}</td>'
                        f'<td class="num">{pct:,.0f}%</td><td class="num">{av_cell}</td>'
                        f'<td class="num">{a*tr:,.0f}</td></tr>')
        tav = (f'{sum(av_ms)/len(av_ms)*100:,.1f}%' if av_ms else '—')
        tpct = f'{ta/tc*100:,.0f}%' if tc else '—'
        rows.append(f'<tr><td><b>TOTAL</b></td><td class="num"><b>{ta:,.0f}</b></td>'
                    f'<td class="num"><b>{tc:,.0f}</b></td><td class="num"><b>{tpct}</b></td>'
                    f'<td class="num"><b>{tav}</b></td><td class="num"><b>{trv:,.0f}</b></td></tr>')
        ctr_h = t("Contract kWh", "kWh contrato") if is_ppa else t("Expected kWh", "kWh esperado")
        mon_h = t("Revenue MXN", "Ingreso MXN") if is_ppa else t("Savings MXN", "Ahorro MXN")
        body.append(f'<div class="card"><h2>{t("Last 6 months","Últimos 6 meses")}</h2><table>'
                    f'<tr><th>{t("Month","Mes")}</th><th class="num">{t("Actual kWh","kWh real")}</th>'
                    f'<th class="num">{ctr_h}</th><th class="num">%</th>'
                    f'<th class="num">{t("Availability","Disponibilidad")}</th>'
                    f'<th class="num">{mon_h}</th></tr>'
                    + ''.join(rows) + '</table></div>')

    body.append(pdf_bottom())
    body.append(f'<footer>{t("Source: PostgreSQL argia_mont (v1 history + v2 KPI). Revenue is an estimate (energy × tariff), not invoiced amounts.","Fuente: PostgreSQL argia_mont (historia v1 + KPI v2). El ingreso es estimado (energía × tarifa), no facturado.")}</footer>')
    return page(''.join(body), f'{k} — ARGIA')


# ================= page: financial (PPA + LaaS, interactive) =================
def financial_page():
    meta_js = json.dumps(assets_meta, ensure_ascii=False)
    atoms_js = json.dumps(atoms, separators=(',', ':'))
    body = [chrome_top('Financial Report', 'Reporte Financiero', '', home='..',
                       range_id='hdr_range',
                       right_sub=f'{t("generated","generado")} {gen_at} · '
                                 f'{t("actuals through","reales hasta")} {asof}')]
    body.append(f'''
<div class="controls noprint">
 <label class="sub">{t("From","Desde")} <input type="date" id="d0" class="btn"></label>
 <label class="sub">{t("To","Hasta")} <input type="date" id="d1" class="btn"></label>
 <button class="btn" onclick="preset('mtd')">{t("Month to date","Mes en curso")}</button>
 <button class="btn" onclick="preset('prev')">{t("Previous month","Mes anterior")}</button>
 <button class="btn" onclick="preset('all')">{t("Full range","Rango completo")}</button>
 <span class="sub" id="ndays"></span>
</div>
<div class="tiles">
 <div class="tile"><div class="tlabel">{t("Expected revenue","Ingreso esperado")}</div><div class="thero" id="k_exp">—</div></div>
 <div class="tile"><div class="tlabel">{t("Actual revenue","Ingreso real")}</div><div class="thero" id="k_act">—</div></div>
 <div class="tile"><div class="tlabel">{t("Net cash (actual)","Flujo neto (real)")}</div><div class="thero" id="k_net">—</div></div>
 <div class="tile"><div class="tlabel">{t("DSCR expected","DSCR esperado")}</div><div class="thero" id="k_de">—</div></div>
 <div class="tile"><div class="tlabel">{t("DSCR actual","DSCR real")}</div><div class="thero" id="k_da">—</div></div>
</div>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;">
 <div class="card"><h2>{t("Expected — contracted","Esperado — contratado")}</h2><table id="tbl_exp"></table></div>
 <div class="card"><h2>{t("Actual — accrued","Real — devengado")}</h2><table id="tbl_act"></table></div>
</div>
<div class="card"><h2>{t("Per-asset detail","Detalle por activo")}</h2>
 <table id="tbl_assets"></table></div>
<div class="card audit"><details><summary>{t("How these numbers are calculated","Cómo se calculan estos números")}</summary>
<p><b>{t("Revenue (PPA):","Ingreso (PPA):")}</b> {t("measured daily energy × the contract tariff in force THAT month, from the migrated contract table (v1 ContractData). Revenue accrues daily by nature; it is an accrual estimate, never invoiced amounts. No IVA in any figure.","energía diaria medida × la tarifa contractual vigente ESE mes, de la tabla de contratos migrada (v1 ContractData). El ingreso se devenga por día; es un estimado devengado, nunca montos facturados. Ninguna cifra incluye IVA.")}</p>
<p><b>{t("Expected revenue:","Ingreso esperado:")}</b> {t("contracted monthly kWh prorated per day × the same tariff — partial periods prorate by elapsed days.","kWh mensuales contratados prorrateados por día × la misma tarifa — periodos parciales se prorratean por días transcurridos.")}</p>
<p><b>{t("LaaS fees:","Cuotas LaaS:")}</b> {t("monthly fee in native currency from the service contract (LOAX1 26,750 USD / LGTO1 15,233 USD); MXN value = fee × the loan-schedule FX of that month. Future months use the v1 projection (last known rate 17.98) — a projection, not a commitment.","cuota mensual en moneda nativa del contrato de servicio (LOAX1 26,750 USD / LGTO1 15,233 USD); valor MXN = cuota × tipo de cambio del mes según la tabla del crédito. Meses futuros usan la proyección v1 (última tasa conocida 17.98) — proyección, no compromiso.")}</p>
<p><b>{t("Debt service:","Servicio de deuda:")}</b> {t("DERIVED as the sum of loan_schedule installments for the period — never a stored per-plant figure. Monthly installment in MXN from the bank amortization tables (v1 LoanPayments); USD loans: payment_ccy × FX, verified row-by-row at migration. Partial periods prorate by elapsed days. Principal+interest combined — v1 never stored the rate, so an interest/principal split is not derivable from this data.","DERIVADO como la suma de las cuotas de loan_schedule del periodo — nunca una cifra almacenada por planta. Cuota mensual en MXN de las tablas de amortización bancarias (v1 LoanPayments); créditos USD: payment_ccy × tipo de cambio, verificado fila por fila en la migración. Periodos parciales se prorratean por días. Capital+interés combinados — v1 nunca guardó la tasa, así que la división interés/capital no es derivable de estos datos.")}</p>
<p><b>{t("Loan position:","Posición del crédito:")}</b> {t("installments paid / total per ACTIVE loan at the period's end month, from loan_schedule (completed loans drop out; several active loans would show separately).","cuotas pagadas / total por crédito ACTIVO al mes final del periodo, según loan_schedule (créditos concluidos salen; varios créditos activos se muestran por separado).")}</p>
<p><b>DSCR:</b> {t("Σ revenue ÷ Σ debt service for the same period — a debt-weighted aggregate, NOT an average of per-asset ratios (an average would let a small loan's high ratio mask a large loan's shortfall). An asset with no debt has no DSCR.","Σ ingreso ÷ Σ servicio de deuda del mismo periodo — agregado ponderado por deuda, NO un promedio de razones por activo (un promedio dejaría que la razón alta de un crédito pequeño oculte el déficit de uno grande). Un activo sin deuda no tiene DSCR.")}</p>
<p><b>{t("FX position:","Posición cambiaria:")}</b> {usd_share:,.1f}% {t("of portfolio debt service is USD-denominated, matched by USD-indexed LaaS fees at the same rate — net portfolio FX exposure ≈ zero.","del servicio de deuda del portafolio está denominado en USD, cubierto por cuotas LaaS indexadas a USD a la misma tasa — exposición cambiaria neta ≈ cero.")}</p>
<p><b>O&M:</b> {t("flat 3,000 MXN per PPA plant per month (plant config), prorated by elapsed days; no O&M cost is booked against LaaS fees.","3,000 MXN fijos por planta PPA al mes (configuración), prorrateado por días; no se registra costo O&M contra cuotas LaaS.")}</p>
<p><b>{t("Pipeline:","Origen de datos:")}</b> {t("PostgreSQL argia_mont on pio06 (v1 history + v2 KPI, migrated 2026-08-25, totals verified against the v1/v2 reconciliation). This page embeds per-day figures computed server-side by the generator; the date picker only sums them — no financial logic runs in the browser.","PostgreSQL argia_mont en pio06 (historia v1 + KPI v2, migrado 2026-08-25, totales verificados contra la conciliación v1/v2). Esta página incluye cifras por día calculadas en el servidor; el selector de fechas solo las suma — ninguna lógica financiera corre en el navegador.")}</p>
</details></div>
{pdf_bottom()}
<script>
const META={meta_js};
const A={atoms_js};
const ASOF="{asof}", FIRST="2024-02-29";
const fmt=n=>n.toLocaleString('en-US',{{maximumFractionDigits:0}});
function pill(v){{const c=v>=1.2?'pill':(v>=1.0?'pill warn':'pill warn');return `<span class="${{c}}">${{(v*100).toFixed(0)}}%</span>`;}}
function compute(){{
 const d0=document.getElementById('d0').value, d1=document.getElementById('d1').value;
 if(!d0||!d1||d0>d1)return;
 const days=Math.round((new Date(d1)-new Date(d0))/864e5)+1;
 document.getElementById('ndays').textContent=days+' d';
 const hr=document.getElementById('hdr_range'); if(hr)hr.textContent=d0+' – '+d1;
 const per={{}};
 for(const k in META) per[k]={{act:0,exp:0,om:0,debt:0}};
 for(const a of A){{ if(a[0]>=d0&&a[0]<=d1&&per[a[1]]){{const p=per[a[1]];p.act+=a[2];p.exp+=a[3];p.om+=a[4];p.debt+=a[5];}} }}
 let T={{act:0,exp:0,om:0,debt:0}};
 for(const k in per){{T.act+=per[k].act;T.exp+=per[k].exp;T.om+=per[k].om;T.debt+=per[k].debt;}}
 document.getElementById('k_exp').textContent=fmt(T.exp);
 document.getElementById('k_act').textContent=fmt(T.act);
 document.getElementById('k_net').textContent=fmt(T.act-T.om-T.debt);
 document.getElementById('k_de').textContent=T.debt? (100*(T.exp-T.om)/T.debt).toFixed(0)+'%':'—';
 document.getElementById('k_da').textContent=T.debt? (100*(T.act-T.om)/T.debt).toFixed(0)+'%':'—';
 const rows=(o,which)=>`
  <tr><td data-en="Revenue" data-es="Ingreso">Revenue</td><td class="num">${{fmt(which==='e'?o.exp:o.act)}}</td></tr>
  <tr><td data-en="O&M costs" data-es="Costos O&M">O&M costs</td><td class="num">(${{fmt(o.om)}})</td></tr>
  <tr><td data-en="Debt service" data-es="Servicio de deuda">Debt service</td><td class="num">(${{fmt(o.debt)}})</td></tr>
  <tr><td><b data-en="Net cash after debt service" data-es="Flujo neto tras deuda">Net cash after debt service</b></td>
      <td class="num"><b>${{fmt((which==='e'?o.exp:o.act)-o.om-o.debt)}}</b></td></tr>
  <tr><td data-en="Portfolio DSCR" data-es="DSCR portafolio">Portfolio DSCR</td>
      <td class="num">${{o.debt?pill(((which==='e'?o.exp:o.act)-o.om)/o.debt):'—'}}</td></tr>`;
 document.getElementById('tbl_exp').innerHTML=rows(T,'e');
 document.getElementById('tbl_act').innerHTML=rows(T,'a');
 let h=`<tr><th data-en="Asset" data-es="Activo">Asset</th><th data-en="Type" data-es="Tipo">Type</th>
   <th class="num" data-en="Exp. revenue" data-es="Ingreso esp.">Exp. revenue</th>
   <th class="num" data-en="Actual revenue" data-es="Ingreso real">Actual revenue</th>
   <th class="num">O&M</th><th class="num" data-en="Debt service" data-es="Serv. deuda">Debt service</th>
   <th class="num" data-en="Loan position" data-es="Posición crédito">Loan</th>
   <th class="num">DSCR exp.</th><th class="num">DSCR act.</th></tr>`;
 const order=Object.keys(per).sort((a,b)=>per[b].act-per[a].act);
 for(const k of order){{const p=per[k],m=META[k];
  if(p.act===0&&p.exp===0&&p.debt===0)continue;
  h+=`<tr><td><b>${{m.name}}</b><br><span class="sub">${{k}}${{m.kwp?' · '+Math.round(m.kwp)+' kWp':''}}</span></td>
   <td><span class="badge ${{m.type==='LaaS'?'laas':''}}">${{m.type}}</span></td>
   <td class="num">${{fmt(p.exp)}}</td><td class="num">${{fmt(p.act)}}</td>
   <td class="num">${{p.om?fmt(p.om):'–'}}</td><td class="num">${{fmt(p.debt)}}</td>
   <td class="num">${{m.loanpos}}</td>
   <td class="num">${{p.debt?pill((p.exp-p.om)/p.debt):'—'}}</td>
   <td class="num">${{p.debt?pill((p.act-p.om)/p.debt):'—'}}</td></tr>`;}}
 h+=`<tr><td><b>PORTFOLIO</b></td><td></td><td class="num"><b>${{fmt(T.exp)}}</b></td>
   <td class="num"><b>${{fmt(T.act)}}</b></td><td class="num">${{fmt(T.om)}}</td>
   <td class="num"><b>${{fmt(T.debt)}}</b></td><td></td>
   <td class="num">${{T.debt?pill((T.exp-T.om)/T.debt):''}}</td>
   <td class="num">${{T.debt?pill((T.act-T.om)/T.debt):''}}</td></tr>`;
 document.getElementById('tbl_assets').innerHTML=h;
 try{{const l=localStorage.getItem('argia_lang')||'en';setLang(l);}}catch(e){{}}
}}
function preset(w){{
 const d1=document.getElementById('d1'), d0=document.getElementById('d0');
 const end=new Date(ASOF+'T00:00:00');
 if(w==='mtd'){{d0.value=ASOF.slice(0,8)+'01';d1.value=ASOF;}}
 else if(w==='prev'){{const fd2=x=>x.getFullYear()+'-'+String(x.getMonth()+1).padStart(2,'0')+'-'+String(x.getDate()).padStart(2,'0');
  d0.value=fd2(new Date(end.getFullYear(),end.getMonth()-1,1));
  d1.value=fd2(new Date(end.getFullYear(),end.getMonth(),0));}}
 else{{d0.value=FIRST;d1.value=ASOF;}}
 compute();
}}
window.addEventListener('DOMContentLoaded',()=>{{
 document.getElementById('d0').value='2026-07-01';
 document.getElementById('d1').value=ASOF;
 document.getElementById('d0').addEventListener('change',compute);
 document.getElementById('d1').addEventListener('change',compute);
 compute();
}});
</script>''')
    return page(''.join(body), 'Financial Report — ARGIA')


# ================= page: CAPEX overview =================
def capex_index():
    body = [chrome_top('CAPEX Plants — Overview', 'Plantas CAPEX — Resumen',
                       f'{t("data through","datos hasta")} {asof} · {gen_at}', home='..')]
    cap = sum(plants[k]['kwp'] for k in CAPEX)
    tot30 = sum(sum(daily.get((k, (dt.date.fromisoformat(asof) - dt.timedelta(days=i)).isoformat()), 0.0)
                    for i in range(30)) for k in CAPEX)
    body.append(f'''<div class="tiles">
<div class="tile"><div class="tlabel">{t("Plants","Plantas")}</div><div class="tval">{len(CAPEX)}</div></div>
<div class="tile"><div class="tlabel">{t("Capacity","Capacidad")}</div>
 <div class="tval">{cap/1000:,.2f} <span class="unit">MWp DC</span></div></div>
<div class="tile"><div class="tlabel">{t("Production, last 30 days","Producción, últimos 30 días")}</div>
 <div class="tval">{tot30/1000:,.1f} <span class="unit">MWh</span></div></div>
</div>''')
    rows = []
    tkwp = t30sum = tsav = 0.0
    for k in CAPEX:
        p = plants[k]
        l30 = sum(daily.get((k, (dt.date.fromisoformat(asof) - dt.timedelta(days=i)).isoformat()), 0.0)
                  for i in range(30))
        pr = pr30.get(k)
        ls = last_seen.get(k, '—')
        status = ('<span class="pill">OK</span>' if ls == asof
                  else f'<span class="pill warn">{t("data ends","datos hasta")} {ls}</span>')
        pr_cell = f'{pr*100:,.1f}%' if pr else '—'
        sav30 = sum(daily.get((k, d2), 0.0) * tariff_for(k, d2[:7]) for d2 in
                    [(dt.date.fromisoformat(asof) - dt.timedelta(days=i)).isoformat()
                     for i in range(30)])
        sav_cell = f'{sav30:,.0f}' if sav30 else '—'
        tkwp += p['kwp']; t30sum += l30; tsav += sav30
        rows.append(f'<tr><td><a href="../{k.lower()}/"><b>{k}</b></a><br><span class="sub">'
                    f'{html.escape(p["customer"][:40])}</span></td>'
                    f'<td class="num">{p["kwp"]:,.0f}</td>'
                    f'<td class="num">{l30/1000:,.2f}</td>'
                    f'<td class="num">{sav_cell}</td>'
                    f'<td class="num">{pr_cell}</td>'
                    f'<td>{status}</td></tr>')
    rows.append(f'<tr><td><b>TOTAL</b></td><td class="num"><b>{tkwp:,.0f}</b></td>'
                f'<td class="num"><b>{t30sum/1000:,.2f}</b></td>'
                f'<td class="num"><b>{tsav:,.0f}</b></td>'
                f'<td class="num">—</td><td></td></tr>')
    body.append(f'<div class="card"><h2>{t("Per plant","Por planta")}</h2><table>'
                f'<tr><th>{t("Plant","Planta")}</th><th class="num">kWp DC</th>'
                f'<th class="num">{t("30d MWh","MWh 30d")}</th>'
                f'<th class="num">{t("30d savings MXN","Ahorro 30d MXN")}</th>'
                f'<th class="num">PR 30d</th>'
                f'<th>{t("Status","Estado")}</th></tr>' + ''.join(rows) + '</table></div>')
    body.append(pdf_bottom())
    body.append(f'<footer>{t("Each plant has a standalone performance page — click its key.","Cada planta tiene su propia página de desempeño — haz clic en su clave.")}</footer>')
    return page(''.join(body), 'CAPEX — ARGIA')


# ================= page: landing (report.argia.com.mx) =================
CO2_T_PER_MWH = 0.438      # Mexico grid emission factor (SEMARNAT/CRE 2023)
HOME_KWH_YR = 2000.0       # avg Mexican household consumption per year, approx.


def landing_page():
    life = sum(monthly_kwh.values())                       # kWh
    fleet_kwp = sum(p['kwp'] for p in plants.values())
    this_m = asof[:7]
    mtd = sum(v for (k2, m2), v in monthly_kwh.items() if m2 == this_m)
    rev_life = sum(a[2] for a in atoms)                    # actual MXN, PPA + LaaS
    co2 = life / 1000.0 * CO2_T_PER_MWH                    # tonnes

    body = [chrome_top('ARGIA — Reports', 'ARGIA — Reportes',
                       f'{len(plants)} {t("plants","plantas")} · 2 LaaS · '
                       f'{t("data","datos")} {first} → {asof} · {gen_at}',
                       show_home=False)]
    body.append(f'''<div class="tiles">
<div class="tile"><div class="tlabel">{t("Clean energy generated","Energía limpia generada")}</div>
 <div class="thero">{life/1e6:,.2f} <span class="unit">GWh</span></div>
 <div class="tsub">{first} → {asof}</div></div>
<div class="tile"><div class="tlabel">{t("CO2 avoided","CO2 evitado")}</div>
 <div class="thero">{co2:,.0f} <span class="unit">t</span></div>
 <div class="tsub">{t("grid factor","factor de red")} {CO2_T_PER_MWH} tCO2/MWh</div></div>
<div class="tile"><div class="tlabel">{t("Revenue generated","Ingreso generado")}</div>
 <div class="thero">{rev_life/1e6:,.1f} <span class="unit">M MXN</span></div>
 <div class="tsub">PPA + LaaS · {t("accrued","devengado")}</div></div>
<div class="tile"><div class="tlabel">{t("Fleet capacity","Capacidad instalada")}</div>
 <div class="tval">{fleet_kwp/1000:,.2f} <span class="unit">MWp DC</span></div>
 <div class="tsub">6 PPA · 4 CAPEX · 2 LaaS</div></div>
<div class="tile"><div class="tlabel">{t("This month","Este mes")}</div>
 <div class="tval">{mtd/1000:,.0f} <span class="unit">MWh</span></div>
 <div class="tsub">{this_m} → {asof[8:]}</div></div>
</div>''')
    y12, yfl = year_months_with_flags(list(plants))
    body.append(f'<div class="card"><h2>{t("Monthly production — whole fleet","Producción mensual — flota completa")} · {asof[:4]}</h2>'
                f'<p class="note">MWh · <span style="color:#9aa1a8">&#9632;</span> {t("grey = expected (contract / prior year)","gris = esperado (contrato / año anterior)")}</p>'
                + columns_svg(y12, 'MWh', scale=1000.0, show_values=True, month_names=True,
                              flags=yfl)
                + '</div>')
    ic_fin = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round">'
              '<path d="M3 21h18M6 21V10M11 21V4M16 21v-8M21 21V7"/></svg>')
    ic_cap = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M3 21V9l6 4V9l6 4V5h6v16H3z"/></svg>')
    ic_set = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round">'
              '<circle cx="12" cy="8" r="3.2"/><path d="M5 20c1.4-3 4-4.5 7-4.5s5.6 1.5 7 4.5"/></svg>')
    ic_cfe = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M13 2L4 14h6l-1 8 9-12h-6l1-8z"/></svg>')
    ic_mon = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M3 12h4l2-6 4 12 2-6h6"/></svg>')
    body.append(f'<h1 class="sect">{t("Reports","Reportes")}</h1>')
    body.append(f'''<div class="nav navbig">
<a href="financial/">{ic_fin}<span class="nb"><b>{t("Financial Report","Reporte Financiero")}</b>
 <span>{t("PPA + LaaS · revenue, debt service, DSCR","PPA + LaaS · ingreso, deuda, DSCR")}</span></span></a>
<a href="capex/">{ic_cap}<span class="nb"><b>{t("CAPEX Plants — Overview","Plantas CAPEX — Resumen")}</b>
 <span>{t("portfolio overview + status","resumen del portafolio + estado")}</span></span></a>
<a href="setup/">{ic_set}<span class="nb"><b>{t("User & access setup","Gestión de usuarios y accesos")}</b>
 <span>{t("admins only · users, passwords, per-report access","solo admins · usuarios, contraseñas, acceso por reporte")}</span></span></a>
<a href="cfe/">{ic_cfe}<span class="nb"><b>{t("CFE Tariffs","Tarifas CFE")}</b>
 <span>{t("all industrial tariffs · every charge · 12 months","todas las tarifas industriales · todos los cargos · 12 meses")}</span></span></a>
<a href="https://monitoring.argia.com.mx/">{ic_mon}<span class="nb"><b>{t("Live Monitoring","Monitoreo en Vivo")}</b>
 <span>{t("5-minute fleet view · inverters · reconciliation","vista de flota cada 5 min · inversores · conciliación")}</span></span></a>
</div>''')
    body.append(f'<h1 class="sect" style="margin-top:14px">{t("Plant performance","Desempeño por planta")}</h1>')
    import re as _re
    cards = []
    for k in PPA + CAPEX:
        p = plants[k]
        name, logo = CLIENT_LOGOS.get(k, (p['customer'][:30], ''))
        img = (f'<img src="{logo}" alt="{html.escape(name)}" class="clogo">' if logo else '')
        locm = _re.search(r'\(([^)]+)\)', p['customer'])
        loc = locm.group(1) if locm else ''
        cards.append(f'<a href="{k.lower()}/" class="pcard">{img}'
                     f'<b>{html.escape(name)}</b>'
                     f'<span>{p["kwp"]:,.0f} kWp DC{(" · " + html.escape(loc)) if loc else ""}</span>'
                     f'<span>{k} · {p["portfolio"]}</span></a>')
    body.append('<div class="nav">' + ''.join(cards) + '</div>')
    body.append(f'''<div class="card audit"><details><summary>{t("How these numbers are calculated","Cómo se calculan estos números")}</summary>
<p><b>{t("Clean energy:","Energía limpia:")}</b> {t("sum of measured daily production of all 10 plants (PostgreSQL argia_mont: v1 history 2024-02-29 → 2026-06-30 + v2 KPI from 2026-07-01; totals verified against the v1/v2 reconciliation).","suma de la producción diaria medida de las 10 plantas (PostgreSQL argia_mont: historia v1 2024-02-29 → 2026-06-30 + KPI v2 desde 2026-07-01; totales verificados contra la conciliación v1/v2).")}</p>
<p><b>{t("CO2 avoided:","CO2 evitado:")}</b> {t("energy × 0.438 tCO2/MWh, the official Mexican grid emission factor (SEMARNAT/CRE) — an estimate of displaced grid generation.","energía × 0.438 tCO2/MWh, el factor de emisión oficial de la red mexicana (SEMARNAT/CRE) — estimación de generación de red desplazada.")}</p>
<p><b>{t("Revenue generated:","Ingreso generado:")}</b> {t("accrued PPA revenue (measured energy × contract tariff of each month) + LaaS fees at the loan-schedule FX of each month. An accrual estimate, not invoiced amounts; no IVA.","ingreso PPA devengado (energía medida × tarifa contractual de cada mes) + cuotas LaaS al tipo de cambio mensual de la tabla del crédito. Estimado devengado, no facturado; sin IVA.")}</p>
<p>{t("Access is restricted to authorized ARGIA users. Data through","Acceso restringido a usuarios autorizados de ARGIA. Datos hasta")} {asof}.</p>
</details></div>''')
    body.append(pdf_bottom())
    return page(''.join(body), 'ARGIA — Reports')


# ================= write =================
def write(rel, content):
    p = os.path.join(OUTROOT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write(content)
    os.chmod(p, 0o644)
    print(f'  {rel:24} {len(content):>8,} B')

print(f'generating into {OUTROOT} (asof {asof})')
def logged_out_page():
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><title>Logged out — ARGIA</title>'
            f'<style>{STYLE}</style></head><body><div class="wrap" style="max-width:520px;'
            'text-align:center;padding-top:80px">'
            f'{LOGO}<h1 style="margin:26px 0 10px">You are logged out / Sesión cerrada</h1>'
            '<p class="sub">Your browser may keep the session until every window is closed.<br>'
            'El navegador puede conservar la sesión hasta cerrar todas las ventanas.</p>'
            '<p style="margin-top:24px"><a class="btn" href="/">Log in again / '
            'Iniciar sesión</a></p></div></body></html>')


write('index.html', landing_page())
write('logged-out.html', logged_out_page())
write('financial/index.html', financial_page())
write('capex/index.html', capex_index())
for k in PPA + CAPEX:
    write(f'{k.lower()}/index.html', plant_page(k))
print('DONE')
