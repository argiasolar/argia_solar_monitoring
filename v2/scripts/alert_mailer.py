"""Monitoring health of the server (pio06 only).

Every 30 minutes (argia-mailer.timer): gathers facts, evaluates the
pure conditions in argia.alerts.monitor, keeps them in alert_state.

v305 (Tomasz, 2026-10-04: "make sure we have our server monitored,
either from the Pi or GitHub ... mails should be last resort"):
* only the solar-monitoring chain is watched (monitor.MONITORING_UNITS);
* every run writes /root/argia_backups/health.json - the office Pi reads
  it over its read-only backup SFTP key and pushes a CRITICAL problem to
  the phone (pi/report_watch/health_watch.py); the Pi also pushes when
  the file stops updating, i.e. when this server or this job is dead;
* mail is the last resort: a CRITICAL monitoring problem still active
  after 6 h, to the administrator, at most once a day; no WARNING mails,
  no recovery mails. Reconciliation, satellite, CFE and drift findings
  stay in alert_state and the health file's "other" list - the record.

Earlier history:

v223: the PLANT conditions (plant dark / stale, inverter silent) left
this mailer - they duplicated the alert ledger's own rules (acute
``data_stale`` / ``plant_offline`` / ``inverter_silent``, the daily
tier) with worse evidence and no explanation, and at 06:07 MX they
called a plant that had not woken up yet "CRITICAL", twice (Tomasz,
2026-09-07). What is left here is the administrator's watch: failed
jobs, disk, PostgreSQL, reconciliation, sensor drift, CFE pipeline,
status-quo drift.
Since v176 each subscriber can be scoped to specific plants: he then
receives only alerts about those plants. Infrastructure and
monitoring-internal alerts (disk, failed jobs, reconciliation, sensor
drift) go to the administrator only (v217, ARGIA_MAIL_ADMIN); CAPEX
plants are never mailed; plants are named, codes are detail.
Recipients with identical views share one message. Without
/root/.argia_mail the run still tracks state and logs - it never
crashes and never spams.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from argia.alerts import emailer, monitor, naming, subscriptions
from argia.core.time_utils import MX_TZ
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.alert_mailer")

UNITS = monitor.MONITORING_UNITS
"""v305: only the solar-monitoring chain is watched (Tomasz: the Drive
archive, finance, invoices, CFE push and the partner/demo sites are not
the monitoring's job to track)."""


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


def gather_failed_units() -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for u in UNITS:
        r = subprocess.run(
            ["systemctl", "show", f"{u}.service", "-p", "ExecMainStatus",
             "-p", "Result", "-p", "ExecMainExitTimestamp"],
            capture_output=True, text=True, timeout=20)
        props = dict(ln.split("=", 1) for ln in r.stdout.splitlines()
                     if "=" in ln)
        # Judge by systemd's verdict, not the raw exit code - units may
        # declare SuccessExitStatus (argia-kpi exits 1 on a partial day
        # by design, e.g. QRO1 dark; that is not a failure).
        result = props.get("Result", "success")
        if result not in ("", "success"):
            status = props.get("ExecMainStatus", "?")
            out.append((f"{u}.service",
                        f"{result} (exit {status}) at "
                        f"{props.get('ExecMainExitTimestamp', '?')}"))
    return out


def gather_satellite_drift() -> List[Tuple[str, str, str, str]]:
    """Latest satellite_check verdict per plant, recent runs only (a
    check that stopped running must not nag forever from stale rows).
    Table may not exist before the first satcheck run - that's a clean
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
    pipeline is not deployed (table absent or empty) - silent."""
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
    """v223: the INSERT branch also stamps last_sent - before, a key seen
    for the first time was mailed, inserted WITHOUT last_sent, and
    mailed again on the next tick as "never sent" (SAG 06:07 + 06:37,
    2026-09-07)."""
    stmts = []
    for a in active:
        mailed = a.key in sent_keys
        sent = ", last_sent = now()" if mailed else ""
        stmts.append(
            f"INSERT INTO alert_state (key, severity, last_sent) VALUES"
            f" ({_txt(a.key)}, {_txt(a.severity)}, {'now()' if mailed else 'NULL'})"
            f" ON CONFLICT (key) DO UPDATE SET last_seen = now(),"
            f" active = true, severity = EXCLUDED.severity,"
            f" first_seen = CASE WHEN alert_state.active"
            f" THEN alert_state.first_seen ELSE now() END{sent};")
    for k in recovered:
        stmts.append(f"UPDATE alert_state SET active = false"
                     f" WHERE key = {_txt(k)};")
    if stmts:
        psql_exec("\n".join(stmts))


DRIFT_JSON = "/root/argia_logs/drift_latest.json"


def gather_drift() -> Optional[dict]:
    """The nightly drift_check report, or None when it does not exist."""
    import json
    try:
        with open(DRIFT_JSON, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


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
        LOG.info("ARGIA_PG_MIRROR not enabled - nothing to do here")
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
            "[ARGIA] test - alert mailer is live",
            "This is a test from the ARGIA alert mailer on pio06.\n"
            "You receive plant/server/infrastructure alerts here.\n"
            "Manage recipients: https://portal.argia.com.mx/setup/",
            cfg["SMTP_USER"], emails)
        ok = emailer.send(msg, cfg)
        LOG.info("test mail to %s: %s", emails, "SENT" if ok else "FAILED")
        return 0 if ok else 1

    try:
        pg_ok = bool(psql_rows("SELECT 1;"))
    except RuntimeError:
        pg_ok = False
    disk = shutil.disk_usage("/")
    disk_pct = 100.0 * disk.used / disk.total

    # a plant under a logged maintenance window never alarms - logging an
    # event (any category) in /setup/ is the official way to silence a
    # known-down plant (e.g. QRO1) without losing the paper trail
    try:
        in_maint = {r[0] for r in psql_rows(
            "SELECT DISTINCT plant_key FROM maintenance_event"
            " WHERE start_ts <= now() AND (end_ts IS NULL"
            " OR end_ts >= now());")}
    except RuntimeError:
        in_maint = set()
    # v223: no plant-dark / plant-stale / inverter-silent here any more -
    # the alert ledger (alerts_snapshot / alerts_daily) owns plant and
    # inverter conditions, with the vendor counter as evidence
    s_alerts = [a for a in monitor.satellite_alerts(
                    gather_satellite_drift())
                if a.key.split(":")[1] not in in_maint]
    tele_age = gather_telemetry_age()
    pi_status = gather_pi_status()            # v305.2: the server watches its watchdog
    active = (s_alerts
              + monitor.infra_alerts(gather_failed_units(), disk_pct, pg_ok)
              + monitor.telemetry_alerts(tele_age, now_mx)
              + monitor.recon_alerts(gather_recon_fails())
              + monitor.cfe_alerts(gather_cfe_status(),
                                   today=now_mx.date())
              + monitor.drift_alerts(gather_drift(), now=now)
              + monitor.pi_alerts(pi_status, now))
    state, _sev_by_key = load_state()
    active_keys = {a.key for a in active}
    recovered = [k for k, st in sorted(state.items()) if st[1] and k not in active_keys]
    health = monitor.health_doc(active, state, now, tele_age, disk_pct, pg_ok)
    health["pi"] = monitor.pi_summary(pi_status)
    to_send = monitor.plan_last_resort(active, state, now)
    LOG.info("active=%d in_scope=%d last_resort_mail=%d recovered=%d recipients=%d mail_cfg=%s",
             len(active), sum(1 for a in active if monitor.in_scope(a.key)), len(to_send),
             len(recovered), len(rcpt), bool(cfg))
    for a in active:
        LOG.info("ACTIVE %s [%s] %s%s", a.key, a.severity, a.title,
                 "" if monitor.in_scope(a.key) else " (record only, not monitoring scope)")
    if args.dry_run:
        LOG.info("[dry-run] health %s, %d problem(s) - file not written", health["status"], len(health["problems"]))
        return 0
    write_health(health)                 # v305: the Pi reads this and pushes; mail is the last resort
    sent_keys: List[str] = []
    if to_send:
        admins = sorted(subscriptions.admin_emails())       # v217: infrastructure goes to the administrator only
        if cfg and admins:
            names = naming.load_names()
            subject = (f"[ARGIA] still failing after {monitor.LAST_RESORT_HOURS:.0f} h: "
                       + ", ".join(a.title for a in to_send[:3]) + (" ..." if len(to_send) > 3 else ""))
            body = monitor.render_body(to_send, [], now_mx.strftime("%Y-%m-%d %H:%M"), names=names)
            ok = emailer.send(emailer.build_email(subject, body, cfg["SMTP_USER"], admins), cfg)
            LOG.info("last-resort mail %s to %d recipient(s) (%d alert(s))",
                     "SENT" if ok else "FAILED", len(admins), len(to_send))
            if ok:
                sent_keys = [a.key for a in to_send]
        else:
            LOG.warning("last-resort mail due but mail not configured (config=%s admins=%d)",
                        bool(cfg), len(admins))
    persist(active, sent_keys, recovered)
    return 0


HEALTH_JSON = os.environ.get("ARGIA_HEALTH_JSON", "/root/argia_backups/health.json")


def write_health(doc: dict, path: str = "") -> None:
    """Atomic write of the health file the office Pi fetches (read-only SFTP
    to /root/argia_backups, the same key as the nightly backup pull)."""
    import json
    path = path or HEALTH_JSON
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        LOG.error("health file not written (%s) - the Pi will report it stale", e)


def gather_pi_status() -> Optional[dict]:
    """The office Pi's hourly self-report (pi/report_watch/pi_status.py), or None.
    v308: the newest pi_status_<stamp>.json in the inbox; older ones are removed."""
    import json
    path = monitor.newest_push(monitor.PI_INBOX, "pi_status")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def gather_telemetry_age() -> Optional[float]:
    """Minutes since the newest telemetry row with a power reading, any plant."""
    try:
        r = psql_rows("SELECT extract(epoch FROM now() - max(ts_utc)) / 60.0 FROM telemetry"
                      " WHERE ts_utc > now() - interval '1 day' AND power_w IS NOT NULL;")
        return float(r[0][0]) if r and r[0] and r[0][0] else None
    except (RuntimeError, ValueError):
        return None


if __name__ == "__main__":
    sys.exit(main())
