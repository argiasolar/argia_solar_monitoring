"""Team email alerts for plants / server / infrastructure (pio06 only).

Every 30 minutes (argia-mailer.timer): gathers facts, evaluates the
pure conditions in argia.alerts.monitor, deduplicates via alert_state,
and emails the 'maintenance' channel of mail_subscription (managed in
/setup/, portal users only) as service@argia.com.mx. New alerts mail
immediately, active ones re-mail every 6 h, recoveries mail once.
Since v176 each subscriber can be scoped to specific plants: he then
receives only alerts about those plants — and no infrastructure noise
(disk, failed jobs), which goes to all-plants subscribers only.
Recipients with identical views share one message. Without
/root/.argia_mail the run still tracks state and logs — it never
crashes and never spams.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from argia.alerts import emailer, monitor, subscriptions
from argia.core.time_utils import MX_TZ
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.alert_mailer")

UNITS = ("argia-telemetry", "argia-telemetry-se", "argia-kpi",
         "argia-alerts-daily", "argia-alerts-snap", "argia-finreport",
         "argia-report-am", "argia-report-pm", "argia-dash-update",
         "argia-client-pages", "argia-archive", "argia-recon",
         "argia-sync", "argia-monitoring-gen", "argia-satcheck",
         "argia-kpimirror", "argia-cfe-push", "argia-archive-month",
         "argia-dailyperf", "argia-invoice", "argia-recon-close")


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def ensure_tables() -> None:
    psql_exec('''
CREATE TABLE IF NOT EXISTS alert_state (
    key        text PRIMARY KEY,
    severity   text,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    last_sent  timestamptz,
    active     boolean NOT NULL DEFAULT true
);''')
    psql_exec(subscriptions.ENSURE_SQL)


def gather_freshness() -> Dict[str, Optional[float]]:
    """{plant: minutes since last usable sample today, None = dark}."""
    out: Dict[str, Optional[float]] = {}
    for r in psql_rows("SELECT plant_key FROM plant WHERE active;"):
        if r and r[0]:
            out[r[0]] = None
    for r in psql_rows(
            "SELECT plant_key, extract(epoch FROM now() - max(ts_utc))/60"
            " FROM telemetry"
            " WHERE (ts_utc AT TIME ZONE 'America/Mexico_City')::date"
            " = (now() AT TIME ZONE 'America/Mexico_City')::date"
            " AND (etoday_kwh IS NOT NULL OR power_w IS NOT NULL)"
            " GROUP BY 1;"):
        if len(r) >= 2 and r[0] in out:
            try:
                out[r[0]] = float(r[1])
            except ValueError:
                pass
    return out


def gather_failed_units() -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for u in UNITS:
        r = subprocess.run(
            ["systemctl", "show", f"{u}.service", "-p", "ExecMainStatus",
             "-p", "Result", "-p", "ExecMainExitTimestamp"],
            capture_output=True, text=True, timeout=20)
        props = dict(ln.split("=", 1) for ln in r.stdout.splitlines()
                     if "=" in ln)
        # Judge by systemd's verdict, not the raw exit code — units may
        # declare SuccessExitStatus (argia-kpi exits 1 on a partial day
        # by design, e.g. QRO1 dark; that is not a failure).
        result = props.get("Result", "success")
        if result not in ("", "success"):
            status = props.get("ExecMainStatus", "?")
            out.append((f"{u}.service",
                        f"{result} (exit {status}) at "
                        f"{props.get('ExecMainExitTimestamp', '?')}"))
    return out


def gather_silent_inverters() -> List[Tuple[str, str, str]]:
    """Configured-active inverters with no usable sample today while
    their plant DOES report (a fully-dark plant is the plant alert's
    job, not a per-inverter storm)."""
    mx_day = ("(ts_utc AT TIME ZONE 'America/Mexico_City')::date"
              " = (now() AT TIME ZONE 'America/Mexico_City')::date")
    usable = "(etoday_kwh IS NOT NULL OR power_w IS NOT NULL)"
    return [(r[0], r[1], r[2]) for r in psql_rows(
        "SELECT i.plant_key, i.inverter_sn,"
        " coalesce(i.inverter_label, i.inverter_sn) FROM inverter i"
        " WHERE i.active"
        " AND EXISTS (SELECT 1 FROM telemetry t"
        f"  WHERE t.plant_key = i.plant_key AND {mx_day} AND {usable})"
        " AND NOT EXISTS (SELECT 1 FROM telemetry t"
        f"  WHERE t.inverter_sn = i.inverter_sn AND {mx_day} AND {usable});")
        if len(r) >= 3]


def gather_satellite_drift() -> List[Tuple[str, str, str, str]]:
    """Latest satellite_check verdict per plant, recent runs only (a
    check that stopped running must not nag forever from stale rows).
    Table may not exist before the first satcheck run — that's a clean
    empty, not an error."""
    try:
        return [(r[0], r[1], r[2], r[3][:200]) for r in psql_rows(
            "SELECT DISTINCT ON (plant_key) plant_key, status,"
            " coalesce(drift_pct::text, '?'), coalesce(note, '')"
            " FROM satellite_check"
            " WHERE check_date >= current_date - 2"
            " ORDER BY plant_key, check_date DESC;") if len(r) >= 4]
    except RuntimeError:
        return []


def gather_cfe_status() -> Optional[dict]:
    """CFE pipeline status for monitor.cfe_alerts(). None while the
    pipeline is not deployed (table absent or empty) — silent."""
    try:
        rows = psql_rows(
            "SELECT round(extract(epoch FROM (now() - heartbeat_ts))"
            " / 3600.0, 1), coalesce(probe_status, ''),"
            " coalesce(last_csv_result, ''),"
            " coalesce((SELECT month::text FROM cfe_tariff"
            "           WHERE source = 'cfe_scrape' GROUP BY month"
            "           HAVING count(DISTINCT tariff_code) >= 10"
            "           ORDER BY month DESC LIMIT 1), '')"
            " FROM cfe_pipeline_status WHERE id = 1;")
    except RuntimeError:
        return None
    if not rows or len(rows[0]) < 4:
        return None
    r = rows[0]
    try:
        age = float(r[0]) if r[0] else None
    except ValueError:
        age = None
    return {"heartbeat_age_h": age, "probe_status": r[1],
            "last_csv_result": r[2], "coverage_month": r[3]}


def gather_recon_fails() -> List[Tuple[str, str, str]]:
    return [(r[0], r[1], r[2][:120]) for r in psql_rows(
        "SELECT plant_key, prod_date::text, coalesce(note,'')"
        " FROM reconciliation_daily WHERE status = 'FAIL'"
        " AND prod_date >= current_date - 3;") if len(r) >= 3]


def load_state() -> Tuple[Dict[str, tuple], Dict[str, str]]:
    """({key: (ts, active, ever_sent, first_seen)}, {key: severity})."""
    state: Dict[str, tuple] = {}
    sev: Dict[str, str] = {}
    for r in psql_rows("SELECT key, coalesce(last_sent, first_seen),"
                       " active, (last_sent IS NOT NULL),"
                       " coalesce(severity, 'CRITICAL'), first_seen"
                       " FROM alert_state;"):
        if len(r) >= 6:
            try:
                ts = dt.datetime.fromisoformat(r[1])
                first = dt.datetime.fromisoformat(r[5])
            except ValueError:
                continue
            state[r[0]] = (ts, r[2] == "t", r[3] == "t", first)
            sev[r[0]] = r[4]
    return state, sev


def persist(active: List[monitor.Alert], sent_keys: List[str],
            recovered: List[str]) -> None:
    stmts = []
    for a in active:
        sent = ", last_sent = now()" if a.key in sent_keys else ""
        stmts.append(
            f"INSERT INTO alert_state (key, severity) VALUES"
            f" ({_txt(a.key)}, {_txt(a.severity)})"
            f" ON CONFLICT (key) DO UPDATE SET last_seen = now(),"
            f" active = true, severity = EXCLUDED.severity,"
            f" first_seen = CASE WHEN alert_state.active"
            f" THEN alert_state.first_seen ELSE now() END{sent};")
    for k in recovered:
        stmts.append(f"UPDATE alert_state SET active = false"
                     f" WHERE key = {_txt(k)};")
    if stmts:
        psql_exec("\n".join(stmts))


def recipients():
    """Enabled 'maintenance' subscribers as (email, scope) pairs, kept
    to portal accounts (the list in /setup/ only offers those, but the
    send-time check holds even if an account was deleted since)."""
    return subscriptions.only_portal(
        subscriptions.recipients_for("maintenance"),
        subscriptions.portal_emails())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="alert mailer")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-mail", action="store_true",
                        help="send a test email to all recipients and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    ensure_tables()
    now = dt.datetime.now(dt.timezone.utc)
    now_mx = dt.datetime.now(MX_TZ)
    cfg = emailer.load_smtp()
    rcpt = recipients()

    if args.test_mail:
        emails = [e for e, _ in rcpt]
        if not cfg or not emails:
            LOG.error("test-mail: config=%s recipients=%d",
                      bool(cfg), len(emails))
            return 1
        msg = emailer.build_email(
            "[ARGIA] test — alert mailer is live",
            "This is a test from the ARGIA alert mailer on pio06.\n"
            "You receive plant/server/infrastructure alerts here.\n"
            "Manage recipients: https://report.argia.com.mx/setup/",
            cfg["SMTP_USER"], emails)
        ok = emailer.send(msg, cfg)
        LOG.info("test mail to %s: %s", emails, "SENT" if ok else "FAILED")
        return 0 if ok else 1

    in_window = 6 <= now_mx.hour < 20
    try:
        pg_ok = bool(psql_rows("SELECT 1;"))
    except RuntimeError:
        pg_ok = False
    disk = shutil.disk_usage("/")
    disk_pct = 100.0 * disk.used / disk.total

    # a plant under a logged maintenance window never alarms — logging an
    # event (any category) in /setup/ is the official way to silence a
    # known-down plant (e.g. QRO1) without losing the paper trail
    try:
        in_maint = {r[0] for r in psql_rows(
            "SELECT DISTINCT plant_key FROM maintenance_event"
            " WHERE start_ts <= now() AND (end_ts IS NULL"
            " OR end_ts >= now());")}
    except RuntimeError:
        in_maint = set()
    p_alerts = [a for a in monitor.plant_alerts(gather_freshness(),
                                                in_window)
                if a.key.split(":", 1)[1] not in in_maint]
    i_alerts = [a for a in monitor.inverter_alerts(
                    gather_silent_inverters(), in_window)
                if a.key.split(":")[1] not in in_maint]
    s_alerts = [a for a in monitor.satellite_alerts(
                    gather_satellite_drift())
                if a.key.split(":")[1] not in in_maint]
    active = (p_alerts + i_alerts + s_alerts
              + monitor.infra_alerts(gather_failed_units(), disk_pct, pg_ok)
              + monitor.recon_alerts(gather_recon_fails())
              + monitor.cfe_alerts(gather_cfe_status(),
                                   today=now_mx.date()))
    # Anti-noise harness (2026-08-27): WARNINGs ride ONE daily digest —
    # the 07:07 MX tick, after the whole morning chain has run.
    digest = now_mx.hour == 7 and now_mx.minute < 30
    state, sev_by_key = load_state()
    to_send, recovered = monitor.plan_sends(active, state, now,
                                            warn_digest=digest)
    mailed_keys = {k for k, st in state.items() if len(st) > 2 and st[2]}
    active_hours = {k: (now - st[3]).total_seconds() / 3600.0
                    for k, st in state.items() if len(st) > 3}
    mail_recovered = monitor.recoveries_to_mail(
        recovered, sev_by_key, mailed_keys=mailed_keys,
        active_hours=active_hours, with_alerts=bool(to_send))
    LOG.info("active=%d to_send=%d recovered=%d (mailable=%d) digest=%s"
             " recipients=%d mail_cfg=%s",
             len(active), len(to_send), len(recovered),
             len(mail_recovered), digest, len(rcpt), bool(cfg))
    for a in active:
        LOG.info("ACTIVE %s [%s] %s", a.key, a.severity, a.title)

    if (to_send or mail_recovered) and not args.dry_run:
        if cfg and rcpt:
            # one message per distinct filtered view — a plant-scoped
            # subscriber sees only his plants, never infra noise
            sent_keys: List[str] = []
            for emails, g_alerts, g_recovered in \
                    subscriptions.group_recipients(to_send, mail_recovered,
                                                   rcpt):
                n_crit = sum(1 for a in g_alerts
                             if a.severity == monitor.SEV_CRIT)
                subject = (f"[ARGIA] {len(g_alerts)} alert(s)"
                           + (f", {n_crit} critical" if n_crit else "")
                           + (f", {len(g_recovered)} recovered"
                              if g_recovered else "")
                           + (" — daily digest" if digest and not n_crit
                              else ""))
                body = monitor.render_body(
                    g_alerts, g_recovered,
                    now_mx.strftime("%Y-%m-%d %H:%M"))
                ok = emailer.send(emailer.build_email(
                    subject, body, cfg["SMTP_USER"], emails), cfg)
                LOG.info("mail %s to %d recipient(s) (%d alert(s))",
                         "SENT" if ok else "FAILED", len(emails),
                         len(g_alerts))
                if ok:
                    sent_keys.extend(a.key for a in g_alerts)
            persist(active, sorted(set(sent_keys)), recovered)
        else:
            LOG.warning("alerts pending but mail not configured "
                        "(config=%s recipients=%d) — state tracked, "
                        "no mail", bool(cfg), len(rcpt))
            persist(active, [], recovered)
    elif not args.dry_run:
        persist(active, [], recovered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
