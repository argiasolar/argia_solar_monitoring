"""Per-channel mail subscriptions with per-plant scoping (v176).

Tomasz, 2026-09-02: separate mailing lists for (1) maintenance —
live metering / live issues, (2) financial reports, (3) daily
performance; admin adds/removes people in /setup/; ONLY portal users
can receive mail; a maintenance subscriber can be limited to specific
plants so he never hears about a plant outside his access.

One PG table, ``mail_subscription``, keyed (email, channel). The
``plants`` column is a comma-separated list of plant keys; empty means
ALL plants. Infrastructure and monitoring-internal alerts (server
disk, failed jobs, PG down, CFE pipeline, reconciliation, sensor
drift) reach only the administrator (v217, ARGIA_MAIL_ADMIN): nobody
else has access to the tool that fixes them.

Everything here except ``recipients_for``/``portal_emails`` is pure
and unit-tested. The setup UI (server/bundle/setup_app.py) duplicates
ENSURE_SQL inline because the bundle cannot import the argia package —
a source-level test keeps the two copies identical.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

LOG = logging.getLogger("argia.alerts.subscriptions")

CHANNELS = ("maintenance", "financial", "daily", "reports")

# v196: 'reports' = the morning/evening PDF reports the Apps Script
# notifier used to mail from Report_Outbox. The CHECK is re-created so an
# existing table (created with three channels) accepts the fourth.
ENSURE_SQL = """CREATE TABLE IF NOT EXISTS mail_subscription (
    email    text NOT NULL,
    channel  text NOT NULL
             CHECK (channel IN ('maintenance','financial','daily','reports')),
    plants   text NOT NULL DEFAULT '',
    enabled  boolean NOT NULL DEFAULT true,
    username text NOT NULL DEFAULT '',
    added_by text,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (email, channel)
);
ALTER TABLE mail_subscription DROP CONSTRAINT IF EXISTS mail_subscription_channel_check;
ALTER TABLE mail_subscription ADD CONSTRAINT mail_subscription_channel_check
    CHECK (channel IN ('maintenance','financial','daily','reports'));"""

# Alert-key prefixes whose second ':'-segment is a plant key. Everything
# else (unit-failed, disk-full, postgres-down, cfe-*) is infrastructure.
LOG = logging.getLogger("argia.alerts.subscriptions")

_PLANT_PREFIXES = ("plant-dark", "plant-stale", "inverter-silent",
                   "recon-fail", "satellite-drift",
                   # v203: engine (ledger) metrics rendered as issues in the
                   # 19:00 mail — "<metric>:<plant>:<sn>"
                   "inverter_temp_high", "inverter_fault", "inverter_relative",
                   "inverter_silent", "string_fault", "energy_daily_pct",
                   "plant_offline", "plant_twin_yield", "data_stale")


PORTFOLIOS_ENV = "ARGIA_MAIL_PORTFOLIOS"
DEFAULT_PORTFOLIOS = "PPA"
"""v204 (Tomasz 2026-09-05: "exclude the CAPEX plants from the mailing
lists"): alerts about a plant are mailed only when its portfolio is in
this comma list. CAPEX plants (GTO2, QRO1, NL2, MEX3, TAM1) stay in the
ledger and on the portal; nobody is paged about them. Infrastructure
alerts and the PORTFOLIO digest are not plant alerts and always pass."""

NEVER_MAILED_PORTFOLIOS: FrozenSet[str] = frozenset({"CAPEX"})
"""v217 (Tomasz 2026-09-06: "I do not want any alerts related to CAPEX
projects"): CAPEX is never mailed, whatever the env says."""

HOLD_ALL: FrozenSet[str] = frozenset({"*"})
"""v217: the portfolio filter could not be loaded (PG timeout — that is
how two Budenheim alerts reached everyone on 2026-09-06 during a lock
incident). Every PLANT alert is held this run; infrastructure alerts
still pass. The ledger retries unmailed alerts next run and the mailer
keeps its state, so nothing is lost — it is late, not silent."""

ADMIN_ENV = "ARGIA_MAIL_ADMIN"
DEFAULT_ADMIN = "tomasz.zemelka@argia.com.mx"
"""v217: infrastructure and monitoring-internal alerts (failed jobs,
disk, PostgreSQL, CFE pipeline, reconciliation FAIL, sensor drift) go
ONLY to the administrator(s) — "only I have access to the tool and
settings". Comma list in the env; the admin is added to those mails
even without a maintenance subscription."""

INTERNAL_PREFIXES = ("recon-fail", "satellite-drift")
"""Plant-keyed alerts that are about the monitoring itself (our
reconciliation vs the vendor counter, our irradiance sensor check),
not about the plant's hardware — admin-only like infrastructure."""


def mail_portfolios(env=None) -> FrozenSet[str]:
    env = os.environ if env is None else env
    raw = env.get(PORTFOLIOS_ENV)
    raw = DEFAULT_PORTFOLIOS if raw is None else raw
    return frozenset(p.strip().upper() for p in raw.split(",")
                     if p.strip()) - NEVER_MAILED_PORTFOLIOS


def admin_emails(env=None) -> FrozenSet[str]:
    env = os.environ if env is None else env
    raw = env.get(ADMIN_ENV)
    raw = DEFAULT_ADMIN if raw is None else raw
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def excluded_plants(portfolio_by_plant: Dict[str, str],
                    allowed: Optional[FrozenSet[str]] = None) -> FrozenSet[str]:
    """Plant keys whose portfolio is NOT mailed. Pure."""
    allowed = mail_portfolios() if allowed is None else allowed
    allowed = allowed - NEVER_MAILED_PORTFOLIOS
    return frozenset(k.upper() for k, pf in portfolio_by_plant.items()
                     if (pf or "").strip().upper() not in allowed)


def is_mailable(plant_key: Optional[str], excluded: FrozenSet[str]) -> bool:
    """None / unknown plant (infrastructure, PORTFOLIO digest) -> True;
    HOLD_ALL -> no plant alert at all."""
    if not plant_key:
        return True
    if "*" in excluded:
        return False
    return plant_key.upper() not in excluded


def load_excluded_plants() -> FrozenSet[str]:
    """excluded_plants() over the live plant table; HOLD_ALL on any
    error (fail CLOSED — v217: a PG hiccup must never page a CAPEX
    customer; the alerts wait one run)."""
    try:
        from argia.store.pgq import psql_rows
        rows = psql_rows("SELECT plant_key, coalesce(portfolio,'') FROM plant;")
        if not rows:
            raise RuntimeError("plant table returned no rows")
        return excluded_plants({r[0]: r[1] for r in rows if len(r) >= 2})
    except Exception as e:  # noqa: BLE001
        LOG.warning("portfolio filter unavailable (%s) — plant alerts held this run", e)
        return HOLD_ALL


def parse_plants(text: Optional[str]) -> Optional[FrozenSet[str]]:
    """'GTO1, mex1' -> frozenset({'GTO1','MEX1'}); ''/None -> None (=all)."""
    if not text or not text.strip():
        return None
    keys = {p.strip().upper() for p in text.split(",") if p.strip()}
    return frozenset(keys) or None


def plants_field(scope: Optional[Iterable[str]]) -> str:
    """Inverse of parse_plants — canonical DB text for a scope."""
    if not scope:
        return ""
    return ",".join(sorted({p.strip().upper() for p in scope if p.strip()}))


def alert_plant(key: str) -> Optional[str]:
    """Plant key an alert concerns, or None for infrastructure alerts."""
    head, _, rest = key.partition(":")
    if head in _PLANT_PREFIXES and rest:
        return rest.split(":", 1)[0]
    return None


def is_internal(key: str) -> bool:
    """Infrastructure (no plant) or monitoring-internal (INTERNAL_PREFIXES):
    admin-only, never a customer's or an O&M subscriber's business."""
    if alert_plant(key) is None:
        return True
    return key.partition(":")[0] in INTERNAL_PREFIXES


def covers(scope: Optional[FrozenSet[str]], plant: Optional[str],
           admin: bool = False) -> bool:
    """Does a subscriber scope receive an alert about ``plant``?
    scope None = all plants; a limited scope only its own plants.
    Infrastructure (plant None) reaches administrators only (v217)."""
    if plant is None:
        return admin
    return scope is None or plant in scope


def visible(key: str, scope: Optional[FrozenSet[str]], admin: bool = False) -> bool:
    """One rule for alerts and recovered keys: internal -> admin only;
    plant alerts -> by scope."""
    if is_internal(key):
        return admin
    return covers(scope, alert_plant(key), admin)


def filter_alerts(alerts: Sequence, scope: Optional[FrozenSet[str]],
                  admin: bool = False) -> list:
    """Alerts (objects with .key) visible to a scope, order preserved."""
    return [a for a in alerts if visible(a.key, scope, admin)]


def filter_keys(keys: Sequence[str], scope: Optional[FrozenSet[str]],
                admin: bool = False) -> List[str]:
    """Recovered-key strings visible to a scope, order preserved."""
    return [k for k in keys if visible(k, scope, admin)]


def with_admins(recipients: Sequence[Tuple[str, Optional[FrozenSet[str]]]],
                admins: FrozenSet[str]
                ) -> List[Tuple[str, Optional[FrozenSet[str]], bool]]:
    """(email, scope, is_admin) — an administrator missing from the
    subscription list is appended with an empty plant scope, so the
    internal alerts always have a reader. Pure."""
    out = [(e, s, e.strip().lower() in admins) for e, s in recipients]
    present = {e.strip().lower() for e, _ in recipients}
    out.extend((a, frozenset(), True) for a in sorted(admins) if a not in present)
    return out


def group_recipients(alerts: Sequence, recovered: Sequence[str],
                     recipients: Sequence[Tuple[str, Optional[FrozenSet[str]]]],
                     admins: Optional[FrozenSet[str]] = None
                     ) -> List[Tuple[List[str], list, List[str]]]:
    """Group (email, scope) pairs by the identical filtered view so each
    distinct mail is built and sent once: [(emails, alerts, recovered)].
    Recipients whose view is empty are dropped — no content, no mail.
    ``admins`` (default: ARGIA_MAIL_ADMIN) are the only readers of
    internal alerts and are added even when not subscribed."""
    admins = admin_emails() if admins is None else admins
    views: Dict[tuple, List[str]] = {}
    payload: Dict[tuple, Tuple[list, List[str]]] = {}
    for email, scope, admin in with_admins(recipients, admins):
        al = filter_alerts(alerts, scope, admin)
        rc = filter_keys(recovered, scope, admin)
        if not al and not rc:
            continue
        sig = (tuple(a.key for a in al), tuple(rc))
        views.setdefault(sig, []).append(email)
        payload[sig] = (al, rc)
    return [(sorted(views[sig]), payload[sig][0], payload[sig][1])
            for sig in sorted(views)]


def recipients_for(channel: str
                   ) -> List[Tuple[str, Optional[FrozenSet[str]]]]:
    """Enabled (email, scope) subscriptions for a channel, from PG."""
    if channel not in CHANNELS:
        raise ValueError("unknown channel: %r" % channel)
    from argia.store.pgq import psql_rows
    out: List[Tuple[str, Optional[FrozenSet[str]]]] = []
    for r in psql_rows(
            "SELECT email, coalesce(plants,'') FROM mail_subscription"
            f" WHERE enabled AND channel = '{channel}' ORDER BY 1;"):
        if r and r[0]:
            out.append((r[0], parse_plants(r[1] if len(r) > 1 else "")))
    return out


def portal_emails(db_path: str = "/opt/argia/auth/users.db"
                  ) -> Optional[FrozenSet[str]]:
    """Emails of enabled portal accounts (lowercased), or None when the
    portal DB is not readable here (dev box, tests) — callers then skip
    the portal check rather than silencing everyone."""
    import logging
    import os
    import sqlite3
    if not os.path.exists(db_path):
        return None
    try:
        c = sqlite3.connect(db_path)
        try:
            rows = c.execute(
                "SELECT email FROM users WHERE disabled=0"
                " AND email != ''").fetchall()
        finally:
            c.close()
        return frozenset(r[0].strip().lower() for r in rows
                         if r and r[0] and r[0].strip())
    except sqlite3.Error:
        return None


def only_portal(recipients: Sequence[Tuple[str, Optional[FrozenSet[str]]]],
                portal: Optional[FrozenSet[str]]
                ) -> List[Tuple[str, Optional[FrozenSet[str]]]]:
    """Enforce 'only portal users receive email'. portal None = check
    unavailable -> unchanged (the admin UI already restricts adds)."""
    if portal is None:
        return list(recipients)
    return [(e, s) for e, s in recipients if e.strip().lower() in portal]
