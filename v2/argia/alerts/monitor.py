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
PLANT_STALE_MIN = 45          # in-window silence that raises an alert
DISK_ALERT_PCT = 85.0

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
    production window — a quiet plant at night is normal."""
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
    that still looks green if you only count what answers."""
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


# ----------------------------------------------------------- send logic
def plan_sends(active: List[Alert],
               state: Dict[str, Tuple[dt.datetime, bool]],
               now: dt.datetime,
               resend_hours: float = RESEND_HOURS
               ) -> Tuple[List[Alert], List[str]]:
    """(alerts to email now, recovered keys to email).

    ``state``: {key: (last_sent_utc, active_flag)} from alert_state.
    A new key or one past the resend window is emailed; a state key
    marked active whose condition vanished is a recovery.
    """
    active_keys = {a.key for a in active}
    to_send: List[Alert] = []
    for a in active:
        st = state.get(a.key)
        if st is None or not st[1]:
            to_send.append(a)
        elif (now - st[0]).total_seconds() >= resend_hours * 3600:
            to_send.append(a)
    recovered = [k for k, (_ls, act) in sorted(state.items())
                 if act and k not in active_keys]
    return to_send, recovered


def render_body(to_send: List[Alert], recovered: List[str],
                now_mx_str: str) -> str:
    """The email body. Pure, plain text, no fluff."""
    lines = [f"ARGIA monitoring — {now_mx_str} MX", ""]
    crit = [a for a in to_send if a.severity == SEV_CRIT]
    warn = [a for a in to_send if a.severity != SEV_CRIT]
    for label, items in (("CRITICAL", crit), ("WARNING", warn)):
        if items:
            lines.append(f"{label}:")
            for a in items:
                lines.append(f"  • {a.title}")
                lines.append(f"    {a.detail}")
            lines.append("")
    if recovered:
        lines.append("RECOVERED:")
        lines.extend(f"  • {k}" for k in recovered)
        lines.append("")
    lines.append("Portal: https://monitoring.argia.com.mx")
    return "\n".join(lines)
