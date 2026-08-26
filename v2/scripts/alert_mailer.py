"""Team email alerts for plants / server / infrastructure (pio06 only).

Every 30 minutes (argia-mailer.timer): gathers facts, evaluates the
pure conditions in argia.alerts.monitor, deduplicates via alert_state,
and emails the enabled recipients (mail_recipient, managed in /setup/)
as service@argia.com.mx. New alerts mail immediately, active ones
re-mail every 6 h, recoveries mail once. Without /root/.argia_mail the
run still tracks state and logs — it never crashes and never spams.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from argia.alerts import emailer, monitor
from argia.core.time_utils import MX_TZ
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.alert_mailer")

UNITS = ("argia-telemetry", "argia-telemetry-se", "argia-kpi",
         "argia-alerts-daily", "argia-alerts-snap", "argia-finreport",
         "argia-report-am", "argia-report-pm", "argia-dash-update",
         "argia-client-pages", "argia-archive", "argia-recon",
         "argia-sync", "argia-monitoring-gen")


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
);
CREATE TABLE IF NOT EXISTS mail_recipient (
    email   text PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true,
    note    text,
    added_by text,
    added_at timestamptz NOT NULL DEFAULT now()
);''')


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
        status = props.get("ExecMainStatus", "0")
        if status not in ("", "0"):
            out.append((f"{u}.service",
                        f"exit {status} at "
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


def gather_recon_fails() -> List[Tuple[str, str, str]]:
    return [(r[0], r[1], r[2][:120]) for r in psql_rows(
        "SELECT plant_key, prod_date::text, coalesce(note,'')"
        " FROM reconciliation_daily WHERE status = 'FAIL'"
        " AND prod_date >= current_date - 3;") if len(r) >= 3]


def load_state() -> Dict[str, Tuple[dt.datetime, bool]]:
    out: Dict[str, Tuple[dt.datetime, bool]] = {}
    for r in psql_rows("SELECT key, coalesce(last_sent, first_seen),"
                       " active FROM alert_state;"):
        if len(r) >= 3:
            try:
                ts = dt.datetime.fromisoformat(r[1])
            except ValueError:
                continue
            out[r[0]] = (ts, r[2] == "t")
    return out


def persist(active: List[monitor.Alert], sent_keys: List[str],
            recovered: List[str]) -> None:
    stmts = []
    for a in active:
        sent = ", last_sent = now()" if a.key in sent_keys else ""
        stmts.append(
            f"INSERT INTO alert_state (key, severity) VALUES"
            f" ({_txt(a.key)}, {_txt(a.severity)})"
            f" ON CONFLICT (key) DO UPDATE SET last_seen = now(),"
            f" active = true, severity = EXCLUDED.severity{sent};")
    for k in recovered:
        stmts.append(f"UPDATE alert_state SET active = false"
                     f" WHERE key = {_txt(k)};")
    if stmts:
        psql_exec("\n".join(stmts))


def recipients() -> List[str]:
    return [r[0] for r in psql_rows(
        "SELECT email FROM mail_recipient WHERE enabled ORDER BY 1;")
        if r and r[0]]


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
        if not cfg or not rcpt:
            LOG.error("test-mail: config=%s recipients=%d",
                      bool(cfg), len(rcpt))
            return 1
        msg = emailer.build_email(
            "[ARGIA] test — alert mailer is live",
            "This is a test from the ARGIA alert mailer on pio06.\n"
            "You receive plant/server/infrastructure alerts here.\n"
            "Manage recipients: https://report.argia.com.mx/setup/",
            cfg["SMTP_USER"], rcpt)
        ok = emailer.send(msg, cfg)
        LOG.info("test mail to %s: %s", rcpt, "SENT" if ok else "FAILED")
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
    active = (p_alerts + i_alerts
              + monitor.infra_alerts(gather_failed_units(), disk_pct, pg_ok)
              + monitor.recon_alerts(gather_recon_fails()))
    to_send, recovered = monitor.plan_sends(active, load_state(), now)
    LOG.info("active=%d to_send=%d recovered=%d recipients=%d mail_cfg=%s",
             len(active), len(to_send), len(recovered), len(rcpt),
             bool(cfg))
    for a in active:
        LOG.info("ACTIVE %s [%s] %s", a.key, a.severity, a.title)

    if (to_send or recovered) and not args.dry_run:
        if cfg and rcpt:
            n_crit = sum(1 for a in to_send
                         if a.severity == monitor.SEV_CRIT)
            subject = (f"[ARGIA] {len(to_send)} alert(s)"
                       + (f", {n_crit} critical" if n_crit else "")
                       + (f", {len(recovered)} recovered" if recovered
                          else ""))
            body = monitor.render_body(to_send, recovered,
                                       now_mx.strftime("%Y-%m-%d %H:%M"))
            ok = emailer.send(emailer.build_email(
                subject, body, cfg["SMTP_USER"], rcpt), cfg)
            LOG.info("mail %s to %d recipient(s)",
                     "SENT" if ok else "FAILED", len(rcpt))
            persist(active, [a.key for a in to_send] if ok else [],
                    recovered)
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
