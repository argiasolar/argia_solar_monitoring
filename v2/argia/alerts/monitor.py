"""Plant / server / infrastructure alert conditions + send-state logic.

PURE functions: the mailer script gathers facts (PG, systemd, disk) and
this module decides WHAT is alarming and WHEN to email about it —
new alerts immediately, still-active ones re-sent every RESEND_HOURS,
and a recovery mail when a condition clears. Deduplication lives in the
``alert_state`` table keyed by a stable alert key.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

RESEND_HOURS = 6
WARN_RESEND_HOURS = 24.0
"""WARNINGs never interrupt: they ride the once-daily digest tick and
re-send at most daily. (2026-08-27 harness — the first server night put
13 mails in Tomasz's inbox, mostly WARN churn.)"""
PLANT_STALE_MIN = 45          # in-window silence that raises an alert
DISK_ALERT_PCT = 85.0

# v202 anti-noise round 2 (Tomasz 2026-09-05: "meaningless notifications").
# Sep 2-4 inbox: TAM1 dark re-mailed every 6 h for three days (12 mails
# saying the same thing), NL2's flaky datalogger mailed "stale 53 min"
# and "recovered" within the hour, and every transient telemetry exit
# code became "job failed" + "recovered".
CONFIRM_PREFIXES = ("plant-stale", "unit-failed", "inverter-silent",
                    "recon-fail", "cfe-")
"""Keys that must survive TWO consecutive ticks (30 min) before their
first mail. A plant with no telemetry at all (plant-dark), a full disk or
a dead PostgreSQL still mail on sight."""
LONG_RUNNING_HOURS = 24.0
LONG_RESEND_HOURS = 24.0
"""A CRITICAL active for more than a day is a known outage: re-mail once
a day, not four times."""
RECOVERY_MIN_HOURS = 3.0
"""A recovery earns its own mail only when the outage lasted this long;
shorter ones ride along with the next mail that goes out anyway."""

SEV_CRIT = "CRITICAL"
SEV_WARN = "WARNING"


@dataclass(frozen=True)
class Alert:
    key: str          # stable id, e.g. "plant-stale:GTO1"
    severity: str
    title: str
    detail: str


# ------------------------------------------------------------ conditions
def plant_alerts(freshness: Dict[str, Optional[float]],
                 in_window: bool) -> List[Alert]:
    """freshness: {plant_key: minutes since last usable sample, or None
    when the plant has no data today}. Only alarms inside the MX
    production window — a quiet plant at night is normal.

    v223: NOT wired in the mailer any more — the alert ledger's
    ``data_stale`` / ``plant_offline`` rules own the plant (kept as a
    pure, tested function)."""
    out: List[Alert] = []
    if not in_window:
        return out
    for pk, age in sorted(freshness.items()):
        if age is None:
            out.append(Alert(f"plant-dark:{pk}", SEV_CRIT,
                             f"{pk}: no telemetry today",
                             f"{pk} produced no usable telemetry sample "
                             "today while inside the production window."))
        elif age > PLANT_STALE_MIN:
            out.append(Alert(f"plant-stale:{pk}", SEV_CRIT,
                             f"{pk}: telemetry stale {age:.0f} min",
                             f"Last usable sample from {pk} is "
                             f"{age:.0f} minutes old (threshold "
                             f"{PLANT_STALE_MIN})."))
    return out


def inverter_alerts(silent: List[Tuple[str, str, str]],
                    in_window: bool) -> List[Alert]:
    """silent: [(plant, sn, label)] — inverters configured ACTIVE that
    produced no usable sample today while their plant reports. This is
    the GTO2 lesson (2026-08-26): a dead inverter hides inside a plant
    that still looks green if you only count what answers. v223: not
    wired in the mailer — the ledger's ``inverter_silent`` (acute +
    daily, judged by the vendor counter) owns it."""
    if not in_window:
        return []
    return [Alert(f"inverter-silent:{pk}:{sn}", SEV_WARN,
                  f"{pk}: inverter {label} silent",
                  f"Inverter {label} ({sn}) at {pk} is configured active "
                  "but produced no usable telemetry today while the rest "
                  "of the plant reports — dead, disconnected, or "
                  "unmonitored.")
            for pk, sn, label in sorted(silent)]


def infra_alerts(failed_units: List[Tuple[str, str]],
                 disk_used_pct: Optional[float],
                 pg_ok: bool) -> List[Alert]:
    """failed_units: [(unit, result-info)]."""
    out: List[Alert] = []
    for unit, info in sorted(failed_units):
        out.append(Alert(f"unit-failed:{unit}", SEV_CRIT,
                         f"job failed: {unit}",
                         f"systemd reports {unit} failed: {info}"))
    if disk_used_pct is not None and disk_used_pct >= DISK_ALERT_PCT:
        out.append(Alert("disk-full", SEV_WARN,
                         f"server disk at {disk_used_pct:.0f}%",
                         f"Root filesystem usage {disk_used_pct:.0f}% "
                         f"(threshold {DISK_ALERT_PCT:g}%)."))
    if not pg_ok:
        out.append(Alert("postgres-down", SEV_CRIT,
                         "PostgreSQL unreachable",
                         "psql against argia_mont failed — collection "
                         "mirror, reconciliation and portal are degraded."))
    return out


def recon_alerts(fail_rows: List[Tuple[str, str, str]]) -> List[Alert]:
    """fail_rows: [(plant, date_iso, note)] with status FAIL."""
    return [Alert(f"recon-fail:{pk}:{d}", SEV_WARN,
                  f"reconciliation FAIL: {pk} {d}",
                  f"Daily reconciliation for {pk} on {d} FAILED: {note}")
            for pk, d, note in sorted(fail_rows)]


def satellite_alerts(rows: List[Tuple[str, str, str, str]]) -> List[Alert]:
    """rows: (plant, status, drift_pct, note) from the latest
    satellite_check run. Only REVIEW alarms — OK and NO_DATA are the
    check's own bookkeeping. Key is per-plant (no date): a persisting
    drift stays ONE alert that re-sends, then recovers when the sensor
    is fixed."""
    out: List[Alert] = []
    for r in rows:
        if not r or len(r) < 4 or r[1] != "REVIEW":
            continue
        pk, drift, note = r[0], r[2], r[3]
        out.append(Alert(f"satellite-drift:{pk}", SEV_WARN,
                         f"{pk}: irradiance sensor drift suspected",
                         f"{pk}: measured/satellite irradiance ratio "
                         f"moved {drift}% vs baseline. {note}"))
    return sorted(out, key=lambda a: a.key)


def cfe_alerts(status: Optional[dict],
               today: Optional[dt.date] = None) -> List[Alert]:
    """CFE tariff pipeline watchdog (Pi fetcher + pio06 ingest).
    status keys: heartbeat_age_h, probe_status, last_csv_result,
    coverage_month ('YYYY-MM-01' or ''). status None = pipeline not
    deployed yet -> silent. All WARNING (ride the daily digest)."""
    if not status:
        return []
    today = today or dt.date.today()
    out: List[Alert] = []
    age = status.get("heartbeat_age_h")
    if age is None or age > 72:
        shown = "never" if age is None else f"{age:.0f}h ago"
        out.append(Alert("cfe-heartbeat", SEV_WARN,
                         "CFE Pi heartbeat stale",
                         f"Last heartbeat from the CFE fetcher Pi: "
                         f"{shown} (expected daily). Check the Pi in "
                         f"Zapopan (argiapi.local, ~/cfe/logs/)."))
    elif status.get("probe_status") != "ok":
        out.append(Alert("cfe-probe", SEV_WARN,
                         "CFE portal probe failing on the Pi",
                         "Daily GDMTH probe did not complete — WAF "
                         "block, page change, or browser issue. See "
                         "~/cfe/logs/ on the Pi."))
    if status.get("last_csv_result") == "rejected":
        out.append(Alert("cfe-reject", SEV_WARN,
                         "CFE tariff CSV rejected by ingest",
                         "Last pushed CSV failed validation on pio06 "
                         "— see /opt/argia/cfe_inbox/rejected/."))
    cov = (status.get("coverage_month") or "")[:7]
    if today.day >= 10 and cov < today.strftime("%Y-%m"):
        out.append(Alert("cfe-coverage", SEV_WARN,
                         "CFE tariffs not updated this month",
                         f"Newest scraped tariff month is "
                         f"{cov or 'none'}; expected "
                         f"{today.strftime('%Y-%m')} by day 10. The "
                         f"/cfe page may be showing stale rates."))
    return sorted(out, key=lambda a: a.key)


DRIFT_MAX_AGE_H = 30.0


def drift_alerts(report: Optional[dict], now: Optional[dt.datetime] = None) -> List[Alert]:
    """v218 status-quo harness: scripts/drift_check.py writes a JSON
    report nightly; its findings become ONE admin-only WARNING per
    section — files that differ from git ("config-drift"), smoke
    answers that changed ("smoke-fail"), and a stale or missing report
    ("drift-stale") so a dead harness cannot pass for a clean one. All
    WARNING: they ride the daily digest, never the night."""
    if not report:
        return [Alert("drift-stale", SEV_WARN, "status-quo check has not run",
                      "No drift_check report found — the argia-drift timer did not "
                      "run or could not write /root/argia_logs/drift_latest.json.")]
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        gen = dt.datetime.strptime(report.get("generated_utc", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        age_h = (now - gen).total_seconds() / 3600.0
    except ValueError:
        age_h = None
    out: List[Alert] = []
    if age_h is None or age_h > DRIFT_MAX_AGE_H:
        out.append(Alert("drift-stale", SEV_WARN, "status-quo check is stale",
                         f"Last drift_check report is {'unreadable' if age_h is None else f'{age_h:.0f} h old'} "
                         f"(expected daily)."))
    lines = list(report.get("findings") or [])
    conf = [l for l in lines if not l.split(":")[0].strip().startswith(("portal-", "old-", "backup-", "portfolio-", "timers-"))]
    smoke = [l for l in lines if l not in conf]
    if conf:
        out.append(Alert("config-drift", SEV_WARN,
                         f"deployed files differ from git ({len(conf)} finding(s))",
                         "What runs on the server is not what is in the repo: "
                         + "; ".join(conf[:12]) + (" …" if len(conf) > 12 else "")
                         + ". Deploy from git or commit the server's version — never leave the two apart."))
    if smoke:
        out.append(Alert("smoke-fail", SEV_WARN,
                         f"live checks changed ({len(smoke)} finding(s))",
                         "The portal or the backups no longer answer as the go-live checklist says: "
                         + "; ".join(smoke[:12]) + (" …" if len(smoke) > 12 else "")))
    return out


# ----------------------------------------------------------- send logic
def plan_sends(active: List[Alert],
               state: Dict[str, Tuple[dt.datetime, bool]],
               now: dt.datetime,
               resend_hours: float = RESEND_HOURS,
               warn_digest: Optional[bool] = None,
               warn_resend_hours: float = WARN_RESEND_HOURS
               ) -> Tuple[List[Alert], List[str]]:
    """(alerts to email now, recovered keys — ALL of them, for state).

    ``state``: {key: (last_sent_utc, active_flag)} from alert_state.

    Severity policy (2026-08-27 anti-noise harness):
    - CRITICAL: mails when new or past ``resend_hours``; recovery mailed.
    - WARNING: only mails on the digest tick (``warn_digest=True``,
      which the mailer passes once a day at 07:07 MX) and at most every
      ``warn_resend_hours``. A WARNING never interrupts the night.
    - ``warn_digest=None`` keeps the historic behavior (all severities
      treated alike) — old callers and tests are unaffected.

    The returned ``recovered`` list is EVERY cleared key (persist must
    flip them inactive); the mailer decides which recoveries are worth
    a line in the mail via recoveries_to_mail().
    """
    active_keys = {a.key for a in active}
    to_send: List[Alert] = []
    for a in active:
        st = state.get(a.key)
        is_new = st is None or not st[1]
        # state rows may be (ts, active), (ts, active, ever_sent) or
        # (ts, active, ever_sent, first_seen); a tracked-but-never-mailed
        # WARN must still make its first digest
        ever_sent = st[2] if (st is not None and len(st) > 2) else True
        first_seen = st[3] if (st is not None and len(st) > 3 and st[1]) else None
        if is_new and needs_confirmation(a.key):
            continue                        # seen once: wait for the next tick
        age_h = None if is_new else (now - st[0]).total_seconds() / 3600.0
        crit_resend = resend_hours
        if first_seen is not None and \
                (now - first_seen).total_seconds() / 3600.0 >= LONG_RUNNING_HOURS:
            crit_resend = max(resend_hours, LONG_RESEND_HOURS)
        if warn_digest is None or a.severity == SEV_CRIT:
            if is_new or not ever_sent or age_h >= crit_resend:
                to_send.append(a)
        else:                                   # WARNING under the policy
            if warn_digest and (is_new or not ever_sent
                                or age_h >= warn_resend_hours):
                to_send.append(a)
    recovered = [k for k, st in sorted(state.items())
                 if st[1] and k not in active_keys]
    return to_send, recovered


def needs_confirmation(key: str) -> bool:
    """Pure: does this key wait for a second sighting before mailing?"""
    return key.startswith(CONFIRM_PREFIXES)


def recoveries_to_mail(recovered: List[str],
                       severity_by_key: Dict[str, str],
                       mailed_keys: Optional[set] = None,
                       active_hours: Optional[Dict[str, float]] = None,
                       with_alerts: bool = False,
                       min_hours: float = RECOVERY_MIN_HOURS) -> List[str]:
    """Only CRITICAL recoveries earn a mail line; a WARNING quietly
    clearing is tomorrow's digest simply not mentioning it. Unknown
    severity mails (safe side).

    v202: a recovery of something never mailed (``mailed_keys`` given and
    the key not in it) is never mailed — nobody was told it was down.
    A short outage (``active_hours`` below ``min_hours``) rides along
    only when a mail goes out anyway (``with_alerts``); unknown duration
    mails (safe side)."""
    out = []
    for k in recovered:
        if severity_by_key.get(k, SEV_CRIT) != SEV_CRIT:
            continue
        if mailed_keys is not None and k not in mailed_keys:
            continue
        if not with_alerts and active_hours is not None and k in active_hours \
                and active_hours[k] < min_hours:
            continue
        out.append(k)
    return out


def describe_key(key: str, names=None) -> str:
    """A recovered key for people (v217): 'plant-stale:GTO1' ->
    'Taigene (GTO1): telemetry stale'; 'unit-failed:x.service' ->
    'scheduled job failed: x.service'. Pure; codes when ``names`` is
    None."""
    from argia.alerts import naming, subscriptions
    head, _, rest = key.partition(":")
    what = naming.phrase(head)
    plant = subscriptions.alert_plant(key)
    if plant:
        who = names.plant_full(plant) if names else plant
        tail = rest.split(":", 1)[1] if ":" in rest else ""
        if head == "inverter-silent" and tail:
            return f"{who}: {what} — {names.inverter(plant, tail) if names else tail}"
        if tail:
            return f"{who}: {what} {tail}"
        return f"{who}: {what}"
    return f"{what}: {rest}" if rest else what


def humanize(a: Alert, names) -> Alert:
    """The same alert with the plant named first and the code kept as the
    detail: 'GTO1: no telemetry today' -> 'Taigene (GTO1): no telemetry
    today'; other code mentions in the text become names. Pure."""
    from argia.alerts import subscriptions
    if names is None:
        return a
    plant = subscriptions.alert_plant(a.key)
    title, detail = a.title, a.detail
    if plant and title.startswith(f"{plant}: "):
        title = f"{names.plant_full(plant)}: " + names.text(title[len(plant) + 2:], strip_prefix=False)
    else:
        title = names.text(title, strip_prefix=False)
    return Alert(a.key, a.severity, title, names.text(detail, strip_prefix=False))


def render_body(to_send: List[Alert], recovered: List[str],
                now_mx_str: str, names=None) -> str:
    """The email body. Pure, plain text, no fluff. ``names`` (v217:
    argia.alerts.naming.Names) renders plants by name, code as detail."""
    lines = [f"ARGIA monitoring — {now_mx_str} MX", ""]
    shown = [humanize(a, names) for a in to_send]
    crit = [a for a in shown if a.severity == SEV_CRIT]
    warn = [a for a in shown if a.severity != SEV_CRIT]
    for label, items in (("CRITICAL", crit), ("WARNING", warn)):
        if items:
            lines.append(f"{label}:")
            for a in items:
                lines.append(f"  • {a.title}")
                lines.append(f"    {a.detail}")
            lines.append("")
    if recovered:
        lines.append("RECOVERED:")
        lines.extend(f"  • {describe_key(k, names)}" for k in recovered)
        lines.append("")
    lines.append("Portal: https://portal.argia.com.mx/monitoring/")
    return "\n".join(lines)
