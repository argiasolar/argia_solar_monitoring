import json, base64
d=json.load(open('results.json'))
def img(p): return "data:image/png;base64,"+base64.b64encode(open(p,'rb').read()).decode()
inv={r['sn']:r for r in d['inverters']}
names={"GTO1":"Taigene","MEX1":"SAG","MEX2":"Vitalmex","NL1":"Plastic Omnium","SLP1":"Quimica Coyoacan","SLP2":"Holiday Inn Express","NL2":"Budenheim","GTO2":"Hirschmann","QRO1":"Tetra Pak","MEX3":"SMS","TAM1":"Ryder"}
def f(v,dec=0): return "—" if v is None else f"{v:,.{dec}f}"
hot=[r for r in d['inverters'] if r['h70']>=5]
hot.sort(key=lambda r:(-r['h75'],-r['h70']))
rows=""
for r in hot:
    md=r['midday']; ge75 = (md['hot_ge75']/md['cool_lt65']-1)*100 if md['hot_ge75'] and md['cool_lt65'] else None
    ml = "—" if (r['mean_loss_pct'] is None or r['se_pct'] is None or r['n_hot_ref'] < 10) else ("%+.1f %% ± %.1f" % (-r['mean_loss_pct'], r['se_pct']))
    g75 = "—" if ge75 is None else ("%+.1f %%" % ge75)
    rows+=("<tr><td>%s · %s<div class=sub>%s · %g kWp DC</div></td><td>%.1f</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s<div class=sub>n=%d</div></td><td>%s<div class=sub>n=%d</div></td><td><b>%s</b> – %s</td><td>%s</td></tr>"
           % (names[r['plant']], r['label'], r['sn'], r['rated_dc'], r['peak_c'], f(r['h65']), f(r['h70']), f(r['h75']), f(r['h_derating']), g75, md['n_hot'], ml, r['n_hot_ref'], f(r['net_kwh']), f(r['lost_kwh']), f(r['lost_mxn'])))
nl1=d['monthly']['NL1']
T=open('report_template.html',encoding='utf-8').read()
html=(T.replace("{{INTERVALS}}", f"{d['fleet']['intervals']:,}").replace("{{H65}}", f"{d['fleet']['h65']:,.0f}")
      .replace("{{IMG_CURVE}}", img('chart_curve.png')).replace("{{IMG_NL1}}", img('chart_nl1_curve.png')).replace("{{IMG_DAY}}", img('chart_day_NL1.png'))
      .replace("{{JUL}}", f"{nl1['2026-07']:,.0f}").replace("{{AUG}}", f"{nl1['2026-08']:,.0f}").replace("{{SEP}}", f"{nl1['2026-09']:,.0f}").replace("{{TOT}}", f"{sum(nl1.values()):,.0f}")
      .replace("{{NET1}}", f(inv['JGMAE65009']['net_kwh'])).replace("{{NET4}}", f(inv['JGMAE6500G']['net_kwh'])).replace("{{ROWS}}", rows))
open('/tmp/out/INVERTER_HEAT_VS_OUTPUT_2026-09-07.html','w',encoding='utf-8').write(html)
print(len(html)//1024,"KB")
