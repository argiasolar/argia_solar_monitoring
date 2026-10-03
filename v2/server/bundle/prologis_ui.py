"""Look and building blocks of prologis.argia.com.mx (v292).

Co-branded: ARGIA's platform (clean white cards, ARGIA teal for every
action) dressed in Prologis's palette - the deep green of its wordmark
for the header and headings, and the four colours of its globe
(#1B4D4A deep green, #2CB5E5 sky, #23B2A9 teal, #71BE45 lime) as the
data colours. The Prologis logo is served from the server's brand folder
(never committed: the repo is public); without it the header shows the
word only.

Pure HTML/SVG helpers - no Flask here, so every piece is unit-testable.
"""
from __future__ import annotations

import html
import math
from typing import Optional, Sequence, Tuple

DEEP, SKY, TEAL, LIME = "#1B4D4A", "#2CB5E5", "#23B2A9", "#71BE45"
ARGIA_TEAL, ARGIA_DEEP = "#05b1a9", "#053b38"
AMBER, RED, GREY = "#e8a23a", "#d0574f", "#9aa4ad"


def e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


class T:
    """Bilingual text for one request: ``t('Sites', 'Sitios')``."""

    def __init__(self, lang: str = "en"):
        self.lang = "es" if lang == "es" else "en"

    def __call__(self, en: str, es: str) -> str:
        return es if self.lang == "es" else en


def num(v, d: int = 0) -> str:
    if v is None:
        return "-"
    return f"{v:,.{d}f}"


CSS = """
:root{--deep:#1B4D4A;--deep2:#123836;--sky:#2CB5E5;--teal:#23B2A9;--lime:#71BE45;--act:#05b1a9;--act2:#05847d;
--bg:#f3f5f4;--card:#fff;--ink:#14211f;--ink2:#3c4b49;--muted:#6b7a78;--line:#e2e7e6;--amber:#e8a23a;--red:#d0574f}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:14.5px/1.55 "Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--act2);text-decoration:none}a:hover{color:var(--deep)}
.top{background:var(--deep);color:#fff;position:sticky;top:0;z-index:1100;box-shadow:0 2px 14px rgba(0,0,0,.18)}
.stripe{height:4px;background:linear-gradient(90deg,var(--deep2) 0 25%,var(--sky) 25% 50%,var(--teal) 50% 75%,var(--lime) 75% 100%)}
.topin{max-width:1360px;margin:0 auto;display:flex;align-items:center;gap:18px;padding:10px 20px}
.brand{display:flex;align-items:center;gap:12px;color:#fff;font-weight:800;letter-spacing:.02em;white-space:nowrap}
.brand img.pl{height:30px;background:#fff;border-radius:6px;padding:3px 8px}
.brand img.pl.rev{height:32px;background:none;border-radius:0;padding:0}
.brand .x{opacity:.6;font-weight:400}
.brand img.ar{height:22px;filter:brightness(0) invert(1)}
.brand .tag{font-size:11px;font-weight:600;letter-spacing:.16em;text-transform:uppercase;opacity:.75;border-left:1px solid rgba(255,255,255,.3);padding-left:12px}
nav.main{display:flex;gap:2px;margin-left:auto;flex-wrap:wrap}
nav.main a{color:#d7ebe8;padding:8px 11px;border-radius:9px;font-weight:600;font-size:13.5px}
nav.main a:hover{background:rgba(255,255,255,.08);color:#fff}
nav.main a.on{background:#fff;color:var(--deep)}
.who{display:flex;align-items:center;gap:10px;font-size:12.5px;color:#cfe3e0;white-space:nowrap}
.who a{color:#fff;font-weight:600}.who a.on{text-decoration:underline}
.sample{background:repeating-linear-gradient(135deg,#fff7e6 0 14px,#fff1d6 14px 28px);color:#7a5200;text-align:center;font-size:12.5px;font-weight:700;padding:6px 12px;border-bottom:1px solid #f2dcae;letter-spacing:.02em}
main{max-width:1360px;margin:0 auto;padding:22px 20px 60px}
.hero{background:radial-gradient(1200px 300px at 85% -40%,rgba(44,181,229,.35),transparent 60%),linear-gradient(120deg,var(--deep2),var(--deep) 55%,#20605b);color:#fff;border-radius:20px;padding:26px 28px;position:relative;overflow:hidden}
.hero h1{font-size:30px;line-height:1.1;margin:4px 0 6px;font-weight:800;letter-spacing:-.01em}
.hero .k{font-size:11.5px;letter-spacing:.18em;text-transform:uppercase;color:#9fe3dc;font-weight:800}
.hero .sub{color:#cfe6e3;font-size:13.5px}
.hero .glow{position:absolute;right:-60px;bottom:-80px;width:320px;height:320px;border-radius:50%;background:conic-gradient(var(--deep) 0 25%,var(--sky) 0 50%,var(--teal) 0 75%,var(--lime) 0);opacity:.13}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:18px;position:relative}
.kpi{background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.14);border-radius:14px;padding:12px 14px}
.kpi .l{font-size:11.5px;color:#bfe0dc;font-weight:700;letter-spacing:.04em}
.kpi .v{font-size:26px;font-weight:800;letter-spacing:-.01em;line-height:1.15}
.kpi .v small{font-size:13px;font-weight:600;color:#bfe0dc;margin-left:3px}
.kpi .d{font-size:11.5px;color:#a9cfca}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--lime);box-shadow:0 0 0 0 rgba(113,190,69,.7);animation:pulse 1.8s infinite;margin-right:6px;vertical-align:1px}
@keyframes pulse{70%{box-shadow:0 0 0 9px rgba(113,190,69,0)}100%{box-shadow:0 0 0 0 rgba(113,190,69,0)}}
.grid{display:grid;gap:16px;margin-top:16px}
.g2{grid-template-columns:minmax(0,1.6fr) minmax(0,1fr)}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}.g2e{grid-template-columns:repeat(2,minmax(0,1fr))}
@media(max-width:1520px){.brand .tag{display:none}}
@media(max-width:980px){.g2,.g3,.g2e{grid-template-columns:1fr}nav.main{margin-left:0}.topin{flex-wrap:wrap}}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;box-shadow:0 1px 2px rgba(20,33,31,.04)}
.card h2{font-size:15px;margin:0 0 10px;color:var(--deep);font-weight:800;display:flex;align-items:center;gap:8px}
.card h2 .r{margin-left:auto;font-size:12px;font-weight:600}
h1.pt{font-size:26px;color:var(--deep);margin:0 0 4px;font-weight:800;letter-spacing:-.01em}
.kick{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--teal);font-weight:800}
.muted{color:var(--muted)}.small{font-size:12px}
table.t{width:100%;border-collapse:collapse;font-size:13.5px}
table.t th{text-align:left;font-size:11.5px;color:var(--muted);font-weight:700;letter-spacing:.03em;border-bottom:1px solid var(--line);padding:8px 8px;white-space:nowrap}
table.t td{border-bottom:1px solid var(--line);padding:9px 8px;vertical-align:middle}
table.t tr:hover td{background:#f8fbfa}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;border-radius:999px;padding:2px 10px;font-size:11.5px;font-weight:700;white-space:nowrap}
.s-ok{background:#e7f6ea;color:#2f7d32}.s-warn{background:#fff3df;color:#9a6100}.s-bad{background:#fde9e7;color:#b2443c}.s-off{background:#eef1f1;color:#5f6c6a}.s-info{background:#e5f5fc;color:#16759a}.s-lime{background:#eef8e6;color:#3f7a1f}
.btn{display:inline-flex;align-items:center;gap:6px;border:0;border-radius:10px;background:var(--act);color:#fff;font-weight:700;padding:9px 14px;font-size:13.5px;cursor:pointer;text-decoration:none}
.btn:hover{background:var(--act2);color:#fff}.btn.ghost{background:#fff;color:var(--deep);border:1px solid var(--line)}.btn.danger{background:var(--red)}
.btn.sm{padding:5px 10px;font-size:12px;border-radius:8px}
input,select,textarea{font:inherit;border:1px solid #cfd8d6;border-radius:10px;padding:8px 10px;background:#fff;color:var(--ink);width:100%}
textarea{min-height:90px}
label{font-size:12px;font-weight:700;color:var(--ink2);display:block;margin:10px 0 4px}
.form{max-width:640px}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.flash{background:#e7f6ea;border:1px solid #bfe3c5;color:#245c28;border-radius:12px;padding:10px 14px;margin-bottom:14px}
.flash.err{background:#fde9e7;border-color:#f3c3be;color:#8a2f28}
.once{font-family:ui-monospace,Consolas,monospace;font-size:18px;background:#fffbe8;border:1px dashed #e8c75c;border-radius:10px;padding:10px 14px;display:inline-block;letter-spacing:.06em}
.stages{display:flex;gap:4px;margin:6px 0}.stages span{flex:1;height:8px;border-radius:4px;background:#e4ebea}.stages span.done{background:var(--teal)}.stages span.now{background:var(--lime)}
.tl{border-left:2px solid var(--line);margin-left:8px;padding-left:16px}.tl .ev{position:relative;margin-bottom:14px}
.tl .ev:before{content:"";position:absolute;left:-23px;top:5px;width:12px;height:12px;border-radius:50%;background:#fff;border:3px solid var(--teal)}
.chip{display:inline-flex;gap:6px;align-items:center;background:#f1f6f5;border:1px solid var(--line);border-radius:10px;padding:6px 10px;font-size:12.5px}
#map{height:620px;border-radius:16px;border:1px solid var(--line);position:relative;z-index:0;isolation:isolate}
.mini-map{height:330px!important}
@media(max-width:700px){.brand .tag{display:none}.kpis{grid-template-columns:1fr 1fr}.hero .glow{display:none}.hero h1{font-size:24px}
main{padding:14px 12px 40px}.hero{padding:20px 18px}.kpi .v{font-size:21px}table.t{font-size:12.5px}.pstep{display:none}.topin{padding:8px 12px;gap:10px}
.brand img.pl{height:24px}.brand img.pl.rev{height:26px}.brand img.ar{height:18px}nav.main a{padding:6px 9px;font-size:12.5px}.row{grid-template-columns:1fr}}
td.nw,th.nw{white-space:nowrap}
.shophero{background:radial-gradient(900px 260px at 90% -30%,rgba(113,190,69,.35),transparent 60%),linear-gradient(120deg,var(--deep2),var(--deep) 60%,#20605b);color:#fff;border-radius:20px;padding:24px 28px;display:flex;gap:16px;align-items:end;flex-wrap:wrap}
.shophero h1{font-size:26px;line-height:1.15;margin:4px 0 6px;font-weight:800}.shophero .sub{color:#cfe6e3;font-size:13.5px;max-width:760px}
.shophero .acts{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}.cartn{background:#fff;color:var(--act2);border-radius:999px;padding:0 8px;font-size:12px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 4px}.tab{padding:7px 13px;border-radius:999px;background:#fff;border:1px solid var(--line);color:var(--ink2);font-weight:700;font-size:13px}
.tab.on{background:var(--deep);color:#fff;border-color:var(--deep)}.tab b{margin-left:4px;color:var(--teal)}
h2.cath{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--teal);margin:22px 0 10px}
.shopgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px}
.svc{display:flex;flex-direction:column;background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 18px;color:var(--ink);transition:transform .12s,box-shadow .12s;position:relative;overflow:hidden}
.svc:before{content:"";position:absolute;left:0;top:0;right:0;height:4px;background:linear-gradient(90deg,var(--deep) 0 25%,var(--sky) 25% 50%,var(--teal) 50% 75%,var(--lime) 75%)}
.svc:hover{transform:translateY(-2px);box-shadow:0 10px 24px rgba(20,33,31,.10);color:var(--ink)}
.svc .cat{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:800}
.svc h3{margin:4px 0 6px;font-size:16px;color:var(--deep)}.svc p{margin:0 0 8px;font-size:13px;color:var(--ink2)}
.svc ul,ul.inc{margin:0 0 10px;padding-left:18px;font-size:12.5px;color:var(--ink2)}
.svc .foot2{margin-top:auto;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;border-top:1px solid var(--line);padding-top:10px}
.price{font-weight:800;color:var(--deep);font-size:16px}
.sitebox{max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:12px;padding:4px 10px}
label.site,label.ck{display:flex;gap:8px;align-items:center;font-weight:400;margin:6px 0;font-size:13px;color:var(--ink)}
label.site input,label.ck input{width:auto}
.est{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-top:14px;background:#f1f6f5;border-radius:12px;padding:12px 14px}
.est .big{font-size:22px;font-weight:800;color:var(--deep)}
table.tot{max-width:340px;margin:10px 0 0 auto}table.tot td{border:0;padding:3px 8px}
form.qf{display:flex;gap:6px;justify-content:flex-end}form.qf input{width:110px;padding:4px 8px}
.stepper{display:flex;gap:4px;margin:10px 0 4px;flex-wrap:wrap}.stepper .st{flex:1;min-width:90px;font-size:11.5px;font-weight:700;color:var(--muted)}
.stepper .st i{display:block;height:8px;border-radius:4px;background:#e4ebea;margin-bottom:4px}.stepper .st.done i{background:var(--teal)}.stepper .st.now i{background:var(--lime)}.stepper .st.now{color:var(--deep)}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:10px}
th.grp{background:#f1f6f5;color:var(--deep)!important;font-size:12px!important;letter-spacing:.12em!important;text-transform:uppercase}
.foot{max-width:1360px;margin:0 auto;padding:0 20px 30px;color:var(--muted);font-size:11.5px;display:flex;gap:14px;flex-wrap:wrap}
.plegend{margin-top:12px;border-top:1px solid var(--line);padding-top:6px}
.plegend .lgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:0 22px}
.plegend .lgh{display:flex;align-items:baseline;gap:10px;font-weight:800;font-size:12.5px;margin:10px 0 2px;letter-spacing:.02em;border-bottom:1px dashed var(--line);padding-bottom:3px}
.plegend .lrow{display:flex;align-items:flex-start;gap:8px;margin:0;padding:4px 0;font-weight:400;font-size:13px;color:var(--ink);cursor:default}
.plegend.sel .lrow{cursor:pointer}.plegend .lrow input{width:auto;margin:3px 0 0}
.plegend .ldot{flex:none;width:12px;height:12px;border-radius:50%;margin-top:4px;box-shadow:0 0 0 2px #fff,0 0 0 3px var(--line)}
.plegend .lsub{display:block;font-size:11.5px;color:var(--muted)}.plegend a{color:var(--ink)}.plegend a:hover{color:var(--act2)}
.hero.tiles{padding:6px 18px 18px}
.legend span{display:inline-flex;align-items:center;gap:6px;margin-right:14px;font-size:12px}.legend i{width:10px;height:10px;border-radius:50%;display:inline-block}
.login{min-height:100vh;display:grid;grid-template-columns:1.1fr 1fr}
.login .art{background:radial-gradient(800px 400px at 20% 120%,rgba(113,190,69,.35),transparent 60%),radial-gradient(700px 400px at 100% -10%,rgba(44,181,229,.35),transparent 60%),linear-gradient(140deg,#0f2f2d,var(--deep));color:#fff;padding:48px;display:flex;flex-direction:column;justify-content:space-between}
.login .art h1{font-size:40px;line-height:1.05;margin:0 0 12px;font-weight:800;letter-spacing:-.02em}
.login .box{display:flex;align-items:center;justify-content:center;padding:40px;background:#fff}
.login .box form{width:100%;max-width:380px}
@media(max-width:900px){.login{grid-template-columns:1fr}.login .art{padding:28px;min-height:auto}}
"""


def stripe_svg(size: int = 22) -> str:
    """A small four-colour disc - ARGIA's nod to the Prologis globe, drawn,
    not copied (the real logo comes from the server's brand folder)."""
    r = size / 2
    parts = []
    cols = [DEEP, SKY, TEAL, LIME]
    for i, c in enumerate(cols):
        a0, a1 = i * math.pi / 2 - math.pi / 2, (i + 1) * math.pi / 2 - math.pi / 2
        x0, y0 = r + r * math.cos(a0), r + r * math.sin(a0)
        x1, y1 = r + r * math.cos(a1), r + r * math.sin(a1)
        parts.append(f'<path d="M{r},{r} L{x0:.2f},{y0:.2f} A{r},{r} 0 0 1 {x1:.2f},{y1:.2f} Z" fill="{c}"/>')
    return f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" aria-hidden="true">{"".join(parts)}</svg>'


def status_pill(status: str, t: T) -> str:
    m = {"producing": ("s-ok", t("Producing", "Produciendo")),
         "night": ("s-off", t("Night", "Noche")),
         "inverter_fault": ("s-bad", t("Inverter fault", "Falla de inversor")),
         "comm_loss": ("s-warn", t("Communication loss", "Sin comunicación")),
         "pre_pto": ("s-info", t("Before PTO", "Antes de PTO"))}
    cls, txt = m.get(status, ("s-off", status))
    return f'<span class="pill {cls}">{e(txt)}</span>'


# ------------------------------------------------------------------ charts
def area_chart(series: Sequence[Tuple[str, Optional[float]]], expected: Sequence[Tuple[str, float]] = (),
               w: int = 760, h: int = 220, unit: str = "kW", color: str = TEAL) -> str:
    """Today's power curve: filled area (gaps stay gaps), dashed expected."""
    pts = [(t, v) for t, v in series]
    allv = [v for _, v in pts if v is not None] + [v for _, v in expected]
    if not pts or not allv:
        return '<div class="muted small">-</div>'
    times = sorted({t for t, _ in pts} | {t for t, _ in expected})
    t0 = _mins(times[0]) if times else 0
    t1 = max(_mins("19:30"), _mins(times[-1]))
    vmax = max(allv) * 1.12 or 1
    pl, pr, pt, pb = 46, 10, 10, 26

    def x(tt):
        return pl + (w - pl - pr) * (_mins(tt) - t0) / max(1, (t1 - t0))

    def y(v):
        return pt + (h - pt - pb) * (1 - v / vmax)
    segs, cur = [], []
    for tt, v in pts:
        if v is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append((x(tt), y(v)))
    if cur:
        segs.append(cur)
    body = []
    for k in range(5):
        v = vmax * k / 4
        yy = y(v)
        body.append(f'<line x1="{pl}" x2="{w - pr}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#e8eeed"/>'
                    f'<text x="{pl - 6}" y="{yy + 4:.1f}" text-anchor="end" font-size="10.5" fill="#7d8b89">{v:,.0f}</text>')
    for hh in range(6, 21, 2):
        xx = x(f"{hh:02d}:00")
        if pl <= xx <= w - pr:
            body.append(f'<text x="{xx:.1f}" y="{h - 8}" text-anchor="middle" font-size="10.5" fill="#7d8b89">{hh:02d}:00</text>')
    gid = f"g{len(pts)}_{int(vmax)}_{w}"
    body.append(f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{color}" stop-opacity=".45"/>'
                f'<stop offset="1" stop-color="{color}" stop-opacity=".03"/></linearGradient></defs>')
    base = y(0)
    for s in segs:
        d = "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in s)
        body.append(f'<path d="{d} L{s[-1][0]:.1f},{base:.1f} L{s[0][0]:.1f},{base:.1f} Z" fill="url(#{gid})"/>'
                    f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round"/>')
    if expected:
        d = "M" + " L".join(f"{x(tt):.1f},{y(v):.1f}" for tt, v in expected)
        body.append(f'<path d="{d}" fill="none" stroke="{DEEP}" stroke-width="1.3" stroke-dasharray="4 4" opacity=".7"/>')
    if segs:
        lx, ly = segs[-1][-1]
        body.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="{LIME}" stroke="#fff" stroke-width="2"/>')
    body.append(f'<text x="{pl}" y="{pt + 2}" font-size="10.5" fill="#7d8b89" dy="-1">{e(unit)}</text>')
    return f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="power curve">{"".join(body)}</svg>'


def bar_chart(bars: Sequence[Tuple[str, float, Optional[float]]], w: int = 760, h: int = 200,
              unit: str = "kWh", color: str = TEAL) -> str:
    """Daily bars (label, actual, expected-or-None): expected as a tick."""
    if not bars:
        return '<div class="muted small">-</div>'
    vmax = max(max(a, b or 0) for _, a, b in bars) * 1.1 or 1
    pl, pr, pt, pb = 50, 8, 10, 24
    n = len(bars)
    bw = (w - pl - pr) / n
    out = []
    for k in range(5):
        v = vmax * k / 4
        yy = pt + (h - pt - pb) * (1 - v / vmax)
        out.append(f'<line x1="{pl}" x2="{w - pr}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#e8eeed"/>'
                   f'<text x="{pl - 6}" y="{yy + 4:.1f}" text-anchor="end" font-size="10.5" fill="#7d8b89">{v:,.0f}</text>')
    for i, (lab, a, ex) in enumerate(bars):
        x0 = pl + i * bw + bw * 0.15
        hh = (h - pt - pb) * a / vmax
        y0 = h - pb - hh
        low = ex is not None and ex > 0 and a < 0.85 * ex
        out.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{bw * 0.7:.1f}" height="{hh:.1f}" rx="2" fill="{AMBER if low else color}">'
                   f'<title>{e(lab)}: {a:,.0f} {e(unit)}{"" if ex is None else f" / exp {ex:,.0f}"}</title></rect>')
        if ex:
            ye = h - pb - (h - pt - pb) * ex / vmax
            out.append(f'<line x1="{x0 - 1:.1f}" x2="{x0 + bw * 0.7 + 1:.1f}" y1="{ye:.1f}" y2="{ye:.1f}" stroke="{DEEP}" stroke-width="1.6"/>')
        if n <= 14 or i % max(1, n // 10) == 0:
            out.append(f'<text x="{x0 + bw * 0.35:.1f}" y="{h - 8}" text-anchor="middle" font-size="10" fill="#7d8b89">{e(lab[-5:])}</text>')
    return f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="daily energy">{"".join(out)}</svg>'


def donut(share: float, color: str = TEAL, size: int = 86, label: str = "") -> str:
    share = max(0.0, min(1.0, share or 0.0))
    r, c = size / 2 - 7, size / 2
    circ = 2 * math.pi * r
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}"><circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="#e8eeed" stroke-width="9"/>'
            f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{color}" stroke-width="9" stroke-linecap="round" '
            f'stroke-dasharray="{circ * share:.1f} {circ:.1f}" transform="rotate(-90 {c} {c})"/>'
            f'<text x="{c}" y="{c + 5}" text-anchor="middle" font-size="15" font-weight="800" fill="{DEEP}">{e(label)}</text></svg>')


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)
