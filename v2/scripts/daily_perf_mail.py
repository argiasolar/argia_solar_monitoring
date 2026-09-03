"""Daily PPA performance email — 19:00 Mexico City (pio06 only).

Tomasz, 2026-09-02: "create email with PPA daily performance where we
summarize the day, we show performance, issues, etc use some modern
template, send it around 7pm so we can still act if something is not
ok even though the data are not yet finalized."

So: this mail is an EARLY heads-up, not the book of record. At 19:00
the nightly close (kpi_eod) has not run — today's figures come straight
from live telemetry (per-inverter max etoday counters) and today's
weather-expected does not exist yet. The mail says so plainly and
leans on what IS solid: yesterday's closed numbers and the
month-to-date vs weather expectation from daily_production.

Recipients: the 'daily' channel of mail_subscription (managed in
/setup/, portal users only). No plant scoping on this channel — it is
one fleet-wide PPA summary by design.

Fires from argia-dailyperf.timer (19:00 America/Mexico_City,
Persistent=true) through run_job.sh. Dry-run prints the text body and
writes the HTML next to /tmp for eyeballing, sends nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html as _html
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from argia.alerts import emailer, subscriptions
from argia.core.time_utils import MX_TZ
from argia.store import pg_mirror

LOG = logging.getLogger("argia.daily_perf_mail")

STALE_MIN = 45          # same threshold the alert mailer uses


def display_name(customer):
    """Human name for the mail — never the plant code (Tomasz, v180:
    'use names in first place and the code like GTO1 as additional').
    'TAIGENE PPA roof (Leon, GTO)' -> 'Taigene'; short all-caps
    acronyms (SAG, SMS) survive. Kept byte-identical to the copy in
    server/monitoring_gen.py — a unit test compares the two. Pure."""
    s = str(customer or '').split('(')[0].split(',')[0]
    for cut in (' PPA', ' CAPEX', ' roof', ' land'):
        i = s.find(cut)
        if i > 0:
            s = s[:i]
    parts = s.strip().split()
    if len(parts) == 1 and len(parts[0]) <= 3 and parts[0].isupper():
        return parts[0]                          # SAG, SMS stay acronyms
    return ' '.join('-'.join(p[:1].upper() + p[1:].lower()
                             for p in w.split('-')) for w in parts)


def logo_png():
    """ARGIA SOLAR wordmark bytes for CID embedding, or None. Reads
    the official asset (server/bundle/argia_logo.py data URI) so mail
    and portal can never diverge; missing file degrades to text."""
    import base64
    import re
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "server", "bundle", "argia_logo.py")
    try:
        src = open(path, encoding="utf-8").read()
        m = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", src)
        return base64.b64decode(m.group(1)) if m else None
    except OSError:
        return None

# ------------------------------------------------------------- gathering

def gather_plants():
    from argia.store.pgq import psql_rows
    return [(r[0], r[1], float(r[2])) for r in psql_rows(
        "SELECT plant_key, customer, kwp_dc FROM plant"
        " WHERE active AND portfolio = 'PPA' ORDER BY plant_key;")
        if len(r) >= 3]


def gather_today():
    """{plant: (kwh_today, minutes_since_last_sample, inverters_seen)}
    from live telemetry, MX 'today'. Energy = sum of per-inverter max
    etoday counters — same basis the plant pages use intraday."""
    from argia.store.pgq import psql_rows
    mx_day = ("(ts_utc AT TIME ZONE 'America/Mexico_City')::date"
              " = (now() AT TIME ZONE 'America/Mexico_City')::date")
    out: Dict[str, Tuple[float, Optional[float], int]] = {}
    for r in psql_rows(
            "SELECT plant_key, coalesce(sum(e),0),"
            " min(age_min), count(*) FROM ("
            " SELECT plant_key, inverter_sn, max(etoday_kwh) AS e,"
            "  extract(epoch FROM now() - max(ts_utc))/60 AS age_min"
            f" FROM telemetry WHERE {mx_day}"
            "  AND (etoday_kwh IS NOT NULL OR power_w IS NOT NULL)"
            " GROUP BY 1, 2) s GROUP BY 1;"):
        if len(r) >= 4:
            try:
                out[r[0]] = (float(r[1]), float(r[2]), int(r[3]))
            except ValueError:
                pass
    return out


def gather_inverter_counts():
    from argia.store.pgq import psql_rows
    return {r[0]: int(r[1]) for r in psql_rows(
        "SELECT plant_key, count(*) FROM inverter WHERE active"
        " GROUP BY 1;") if len(r) >= 2}


def gather_yesterday(today: dt.date):
    from argia.store.pgq import psql_rows
    d = (today - dt.timedelta(days=1)).isoformat()
    out = {}
    for r in psql_rows(
            "SELECT plant_key, coalesce(energy_kwh,0)"
            f" FROM daily_production WHERE prod_date = '{d}';"):
        if len(r) >= 2:
            try:
                out[r[0]] = float(r[1])
            except ValueError:
                pass
    return out


def gather_mtd(today: dt.date):
    """{plant: (mtd_kwh, paired_kwh, expected_kwh)} through yesterday,
    from the closed daily_production rows. Paired = production on days
    that HAVE an expected figure, so vs-expected never divides a full
    month by a partial expectation (the v174 lesson)."""
    from argia.store.pgq import psql_rows
    d0 = today.replace(day=1).isoformat()
    d1 = (today - dt.timedelta(days=1)).isoformat()
    out = {}
    if d1 < d0:                     # the 1st: no closed days yet
        return out
    for r in psql_rows(
            "SELECT plant_key, coalesce(sum(energy_kwh),0),"
            " coalesce(sum(CASE WHEN expected_kwh IS NOT NULL"
            "  THEN energy_kwh END),0), coalesce(sum(expected_kwh),0)"
            f" FROM daily_production WHERE prod_date BETWEEN '{d0}'"
            f" AND '{d1}' GROUP BY 1;"):
        if len(r) >= 4:
            try:
                out[r[0]] = (float(r[1]), float(r[2]), float(r[3]))
            except ValueError:
                pass
    return out


def gather_issues():
    """Active alert keys + severities from alert_state (what the alert
    mailer tracks), plus plants under a logged maintenance window."""
    from argia.store.pgq import psql_rows
    alerts = [(r[0], r[1]) for r in psql_rows(
        "SELECT key, coalesce(severity,'CRITICAL') FROM alert_state"
        " WHERE active ORDER BY 2, 1;") if len(r) >= 2]
    try:
        maint = sorted({r[0] for r in psql_rows(
            "SELECT DISTINCT plant_key FROM maintenance_event"
            " WHERE start_ts <= now() AND (end_ts IS NULL"
            " OR end_ts >= now());") if r and r[0]})
    except RuntimeError:
        maint = []
    return alerts, maint


# ------------------------------------------------------------------ pure

_ISSUE_PHRASE = {
    "plant-dark": "no telemetry today",
    "plant-stale": "telemetry stale",
    "inverter-silent": "inverter silent",
    "recon-fail": "reconciliation FAIL",
    "satellite-drift": "irradiance sensor drift suspected",
}


def describe_issue(key: str) -> str:
    """'plant-stale:GTO1' -> 'GTO1: telemetry stale'. Pure."""
    head, _, rest = key.partition(":")
    phrase = _ISSUE_PHRASE.get(head, head)
    plant = subscriptions.alert_plant(key)
    if plant:
        tail = rest.split(":", 1)[1] if ":" in rest else ""
        extra = f" ({tail})" if tail else ""
        return f"{plant}: {phrase}{extra}"
    return f"server: {phrase}" if phrase == head else phrase


def summarize(plants, today_map, inv_counts, yday_map, mtd_map,
              alerts, maint, today: dt.date, now_hm: str) -> dict:
    """Assemble everything the templates need. Pure, unit-tested."""
    ppa_keys = {k for k, _, _ in plants}
    rows = []
    for key, customer, kwp in plants:
        kwh, age, seen = today_map.get(key, (0.0, None, 0))
        inv_total = inv_counts.get(key, 0)
        mtd_kwh, paired, exp = mtd_map.get(key, (0.0, 0.0, 0.0))
        vs_exp = 100.0 * paired / exp if exp > 0 else None
        plant_alerts = [(k, s) for k, s in alerts
                        if subscriptions.alert_plant(k) == key]
        if key in maint:
            status, cls = "maintenance", "warn"
        elif kwh <= 0.0:
            status, cls = "dark", "bad"
        elif age is not None and age > STALE_MIN:
            status, cls = f"stale {age:.0f} min", "warn"
        elif any(s == "CRITICAL" for _, s in plant_alerts):
            status, cls = "issues", "bad"
        elif plant_alerts:
            status, cls = "check", "warn"
        else:
            status, cls = "OK", "ok"
        rows.append({
            "key": key, "customer": customer, "kwp": kwp,
            "label": display_name(customer),
            "desc": f"{key} · {customer} · {kwp:,.0f} kWp",
            "kwh_today": kwh,
            "yield": kwh / kwp if kwp > 0 else 0.0,
            "inv": f"{seen}/{inv_total}" if inv_total else str(seen),
            "kwh_yday": yday_map.get(key),
            "mtd_kwh": mtd_kwh, "vs_exp": vs_exp,
            "status": status, "cls": cls,
        })
    tot_today = sum(r["kwh_today"] for r in rows)
    tot_yday = sum(v for v in (r["kwh_yday"] for r in rows)
                   if v is not None)
    tot_mtd = sum(r["mtd_kwh"] for r in rows)
    tot_paired = sum(mtd_map.get(r["key"], (0, 0, 0))[1] for r in rows)
    tot_exp = sum(mtd_map.get(r["key"], (0, 0, 0))[2] for r in rows)
    issues = [(describe_issue(k), s) for k, s in alerts
              if subscriptions.alert_plant(k) in ppa_keys
              or subscriptions.alert_plant(k) is None]
    tot_kwp = sum(r["kwp"] for r in rows)
    inv_seen = sum(today_map.get(r["key"], (0, None, 0))[2] for r in rows)
    inv_all = sum(inv_counts.get(r["key"], 0) for r in rows)
    return {
        "date": today.isoformat(), "time": now_hm, "rows": rows,
        "tot_today": tot_today, "tot_yday": tot_yday,
        "tot_mtd": tot_mtd,
        "tot_vs_exp": (100.0 * tot_paired / tot_exp
                       if tot_exp > 0 else None),
        "tot_kwp": tot_kwp,
        "tot_yield": tot_today / tot_kwp if tot_kwp > 0 else 0.0,
        "tot_inv": f"{inv_seen}/{inv_all}" if inv_all else str(inv_seen),
        "issues": issues, "maint": list(maint),
        "n_bad": sum(1 for r in rows if r["cls"] == "bad"),
        "n_warn": sum(1 for r in rows if r["cls"] == "warn"),
    }


def mail_subject(data: dict) -> str:
    n = data["n_bad"] + data["n_warn"]
    flag = f" — {n} plant(s) need attention" if n else " — all OK"
    return ("[ARGIA] Daily PPA performance %s: %s kWh%s"
            % (data["date"], f"{data['tot_today']:,.0f}", flag))


def _pct(v) -> str:
    return f"{v:,.1f}%" if v is not None else "—"


def _num(v, dec=0) -> str:
    return f"{v:,.{dec}f}" if v is not None else "—"


def render_text(data: dict) -> str:
    """Plain-text alternative — grep-able archive copy. Pure."""
    L = [f"ARGIA — Daily PPA performance, {data['date']} "
         f"(as of {data['time']} MX)", "",
         f"Fleet today:   {_num(data['tot_today'])} kWh (live, preliminary)",
         f"Yesterday:     {_num(data['tot_yday'])} kWh (closed)",
         f"Month to date: {_num(data['tot_mtd'])} kWh"
         f" — {_pct(data['tot_vs_exp'])} of weather expectation", ""]
    for r in data["rows"]:
        L.append("  %-20s %8s kWh  %5.2f kWh/kWp  inv %s  [%s]"
                 % (r["label"][:20], _num(r["kwh_today"]),
                    r["yield"], r["inv"], r["status"]))
        L.append("    %s" % r["desc"])
    L.append("  %-20s %8s kWh  %5.2f kWh/kWp  inv %s"
             % ("TOTAL", _num(data["tot_today"]), data["tot_yield"],
                data["tot_inv"]))
    L.append("    %d plants · %s kWp · MTD %s kWh · vs exp %s"
             % (len(data["rows"]), _num(data["tot_kwp"]),
                _num(data["tot_mtd"]), _pct(data["tot_vs_exp"])))
    L.append("")
    if data["issues"]:
        L.append("Open issues:")
        L.extend(f"  • [{s}] {txt}" for txt, s in data["issues"])
    else:
        L.append("Open issues: none")
    L += ["",
          "Today's figures are live telemetry, not yet reconciled — the"
          " nightly close finalizes them. Weather-expected for today"
          " lands with the close as well.",
          "", "Portal: https://monitoring.argia.com.mx",
          "Manage subscriptions: https://report.argia.com.mx/setup/"]
    return "\n".join(L)


_PILL = {"ok": ("#e7f4e8", "#1d7a2c", "OK"),
         "warn": ("#fdf3d7", "#8a6d1a", None),
         "bad": ("#fdeaea", "#b3261e", None)}


def render_html(data: dict) -> str:
    """Modern, email-safe HTML: inline CSS, tables, dark text on light
    ground (house readability rule), no external assets. Pure."""
    e = _html.escape
    tile = ('<td style="background:#ffffff;border:1px solid #e3e8ee;'
            'border-radius:10px;padding:14px 16px;text-align:center">'
            '<div style="font-size:11px;letter-spacing:.06em;'
            'text-transform:uppercase;color:#5f6b7a">%s</div>'
            '<div style="font-size:22px;font-weight:700;color:#16324f;'
            'padding-top:4px">%s</div>'
            '<div style="font-size:11px;color:#8a94a1">%s</div></td>')
    tiles = "".join([
        tile % ("Today (live)", e(_num(data["tot_today"])) + " kWh",
                "preliminary"),
        '<td style="width:10px"></td>',
        tile % ("Yesterday", e(_num(data["tot_yday"])) + " kWh",
                "closed"),
        '<td style="width:10px"></td>',
        tile % ("Month to date", e(_num(data["tot_mtd"])) + " kWh",
                "through yesterday"),
        '<td style="width:10px"></td>',
        tile % ("MTD vs expected", e(_pct(data["tot_vs_exp"])),
                "weather-based"),
    ])
    trs = []
    for r in data["rows"]:
        bg, fg, label = _PILL[r["cls"]]
        pill = ('<span style="background:%s;color:%s;border-radius:99px;'
                'padding:2px 10px;font-size:11px;font-weight:600">%s'
                '</span>' % (bg, fg, e(label or r["status"])))
        trs.append(
            '<tr>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4">'
            f'<b style="color:#16324f;font-size:13.5px">{e(r["label"])}</b>'
            f'<br><span style="color:#8a94a1;font-size:11px">'
            f'{e(r["desc"])}</span></td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:center">{pill}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{e(_num(r["kwh_today"]))}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{r["yield"]:.2f}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:center">{e(r["inv"])}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{e(_num(r["kwh_yday"]))}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{e(_num(r["mtd_kwh"]))}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{e(_pct(r["vs_exp"]))}</td></tr>')
    # summary line at the table bottom — always, even with the tiles
    # above (Tomasz, v180)
    tb = 'padding:8px 10px;border-top:2px solid #16324f;font-weight:700'
    trs.append(
        '<tr>'
        f'<td style="{tb}">TOTAL'
        f'<br><span style="color:#8a94a1;font-size:11px;font-weight:400">'
        f'{len(data["rows"])} plants · {e(_num(data["tot_kwp"]))} kWp'
        '</span></td>'
        f'<td style="{tb}"></td>'
        f'<td style="{tb};text-align:right">{e(_num(data["tot_today"]))}</td>'
        f'<td style="{tb};text-align:right">{data["tot_yield"]:.2f}</td>'
        f'<td style="{tb};text-align:center">{e(data["tot_inv"])}</td>'
        f'<td style="{tb};text-align:right">{e(_num(data["tot_yday"]))}</td>'
        f'<td style="{tb};text-align:right">{e(_num(data["tot_mtd"]))}</td>'
        f'<td style="{tb};text-align:right">{e(_pct(data["tot_vs_exp"]))}'
        '</td></tr>')
    if data["issues"]:
        items = "".join(
            '<li style="padding:3px 0;color:#243041">'
            '<b style="color:%s">[%s]</b> %s</li>'
            % ("#b3261e" if s == "CRITICAL" else "#8a6d1a", e(s), e(txt))
            for txt, s in data["issues"])
        issues_html = ('<ul style="margin:6px 0 0;padding-left:20px">'
                       + items + "</ul>")
    else:
        issues_html = ('<p style="margin:6px 0 0;color:#1d7a2c">'
                       'No open issues.</p>')
    return f'''<!doctype html><html><body style="margin:0;padding:0;
background:#f2f5f8;font-family:'Segoe UI',Roboto,Arial,sans-serif;
color:#243041">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:22px 10px">
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
 style="max-width:640px;width:100%">
<tr><td style="background:#ffffff;border:1px solid #e3e8ee;
border-bottom:3px solid #16324f;border-radius:12px 12px 0 0;
padding:14px 22px">
<img src="cid:argialogo" alt="ARGIA SOLAR" height="26"
 style="height:26px;vertical-align:middle">
<span style="float:right;color:#5f6b7a;font-size:13px;line-height:26px">
Daily PPA performance · {data["date"]} · {data["time"]} MX</span>
</td></tr>
<tr><td style="background:#fbfcfe;border:1px solid #e3e8ee;
border-top:none;padding:18px 22px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>{tiles}</tr></table>
<h3 style="margin:20px 0 6px;font-size:13px;letter-spacing:.05em;
text-transform:uppercase;color:#5f6b7a">Plants</h3>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="font-size:13px;border-collapse:collapse">
<tr style="color:#5f6b7a;font-size:11px;text-transform:uppercase">
<td style="padding:4px 10px">Plant</td>
<td style="padding:4px 10px;text-align:center">Status</td>
<td style="padding:4px 10px;text-align:right">Today kWh</td>
<td style="padding:4px 10px;text-align:right">kWh/kWp</td>
<td style="padding:4px 10px;text-align:center">Inv</td>
<td style="padding:4px 10px;text-align:right">Yday kWh</td>
<td style="padding:4px 10px;text-align:right">MTD kWh</td>
<td style="padding:4px 10px;text-align:right">vs exp</td></tr>
{"".join(trs)}</table>
<h3 style="margin:20px 0 0;font-size:13px;letter-spacing:.05em;
text-transform:uppercase;color:#5f6b7a">Open issues</h3>
{issues_html}
<p style="margin:18px 0 0;padding:10px 12px;background:#fffdf4;
border:1px solid #e7dfc2;border-radius:8px;font-size:12px;
color:#6b5d2a">Today's figures are <b>live telemetry, not yet
reconciled</b> — the nightly close finalizes them and adds today's
weather expectation. This mail goes out at 19:00 so there is still
time to act on anything red.</p>
<p style="margin:14px 0 0;font-size:12px;color:#8a94a1">
<a href="https://monitoring.argia.com.mx" style="color:#2b6cb0">
Open the portal</a> ·
<a href="https://report.argia.com.mx/setup/" style="color:#2b6cb0">
Manage subscriptions</a></p>
</td></tr></table></td></tr></table></body></html>'''


# ------------------------------------------------------------------ main

def recipients():
    return [e for e, _ in subscriptions.only_portal(
        subscriptions.recipients_for("daily"),
        subscriptions.portal_emails())]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="daily PPA performance mail")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    from argia.store.pgq import psql_exec
    psql_exec(subscriptions.ENSURE_SQL)

    now_mx = dt.datetime.now(MX_TZ)
    today = now_mx.date()
    alerts, maint = gather_issues()
    data = summarize(gather_plants(), gather_today(),
                     gather_inverter_counts(), gather_yesterday(today),
                     gather_mtd(today), alerts, maint, today,
                     now_mx.strftime("%H:%M"))
    if not data["rows"]:
        LOG.error("no active PPA plants found — nothing to report")
        return 1
    text = render_text(data)
    LOG.info("summary: today=%.0f kWh, %d plant(s), %d issue(s)",
             data["tot_today"], len(data["rows"]), len(data["issues"]))

    if args.dry_run:
        print(text)
        out = "/tmp/argia_daily_perf_preview.html"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(render_html(data))
        LOG.info("dry-run: HTML preview at %s — nothing sent", out)
        return 0

    rcpt = recipients()
    if not rcpt:
        LOG.warning("no enabled 'daily' subscribers — nothing sent")
        return 0
    cfg = emailer.load_smtp()
    if not cfg:
        LOG.error("no SMTP config (/root/.argia_mail) — not sending")
        return 1
    logo = logo_png()
    msg = emailer.build_html_email(
        mail_subject(data), text, render_html(data), cfg["SMTP_USER"],
        rcpt, images={"argialogo": (logo, "png")} if logo else None)
    if not emailer.send(msg, cfg):
        LOG.error("send FAILED")
        return 1
    LOG.info("sent to %d recipient(s)", len(rcpt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
