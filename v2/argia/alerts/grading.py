"""One severity policy for the alert ledger (v303).

Tomasz, 2026-10-03, on the 19:00 mail listing ten "open issues" a day:
"I am getting a lot of warnings which most probably doesn't even qualify
for warnings". Counted on production (30 days): 31 inverter-heat
WARNINGs, 29 data_stale, 18 silent-inverter, 3 twin-yield, 3 energy
WARNINGs - almost none of them with a measured loss of energy.

The rule every tier now follows (applied to the candidates right before
reconcile, so the ledger, the portal and every mail agree):

* CRITICAL - energy is being lost or a unit / plant is off. Unchanged.
* WARNING  - a measured loss that is smaller, or a data problem that
  lasted a whole production day: one inverter producing less than its
  peers, a weak string, a vendor fault flag, a day without (or with a
  6 h hole in) telemetry.
* INFO     - a flag WITHOUT a measured loss. Kept on the portal, never
  mailed. If it turns into a loss, the detector raises it to CRITICAL
  (an escalation re-arms the mail):
    - inverter_temp_high: hot but producing like its cooler peers
      (CRITICAL needs the measured shortfall or the inverter's own
      thermal derating; the "still above the clear level 60" hysteresis
      line is INFO too);
    - inverter_silent: no data but its counter kept climbing (a
      datalogger / RS485 link, no energy lost), or - in the acute tier -
      a gap not yet long enough to call the unit off;
    - energy_daily_pct: a day at 70-85 % of the weather-adjusted
      expectation (below 70 % stays CRITICAL);
    - plant_twin_yield: the lagging twin at 70-85 %;
    - data_stale in the ACUTE tier (a few hours without data during the
      day): the daily tier raises it to WARNING when the day ends with
      no telemetry or a hole over 6 h.

v305 (Tomasz, 2026-10-04: "the warning should be sent where we are losing
money; if something is potentially going to lose money only flag it on the
dashboard; mails should be last resort"). Missing data is never a measured
loss - the counter decides once it reports again:

* data_stale is INFO in both tiers (a plant without data is blind, not
  losing money we can prove);
* inverter_silent in the acute tier is INFO at any length (the vendor
  counter decides when it reappears); in the daily tier only "the unit
  was OFF" (the counter grew less than its siblings) stays CRITICAL - the
  "no counter to confirm" and "kept producing" cases are INFO
  (alerts_daily.daily_silent_candidates);
* LAST RESORT (``escalate_blind``): a plant or inverter that stays blind
  for BLIND_DAYS days in a row becomes a WARNING once, so a datalogger
  that never comes back is not silent forever.

Pure.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Dict, Iterable, List

from argia.alerts.engine import Candidate

INFO_ALWAYS = frozenset({"inverter_temp_high", "inverter_silent", "energy_daily_pct", "plant_twin_yield"})
"""Metrics whose WARNING is INFO in every tier."""

INFO_ACUTE = frozenset({"data_stale"})
"""Metrics whose WARNING is INFO when the acute (intraday) tier sees it."""

POTENTIAL = frozenset({"data_stale", "inverter_silent"})
"""v305: missing data - a possible loss, never a measured one. INFO at any
severity, except the daily "the unit was OFF" verdict (see the module doc)."""

BLIND_DAYS = 3
"""v305 last resort: blind this many days in a row -> one WARNING mail."""

_TAG = re.compile(r"\[(?:WARNING|CRITICAL)\]\s*$")


def grade(c: Candidate, tier: str) -> Candidate:
    """The candidate at its policy severity. ``tier`` is 'acute' or 'daily'."""
    if tier not in ("acute", "daily"):
        raise ValueError(f"unknown tier: {tier!r}")
    sev = (c.severity or "").upper()
    if c.metric in POTENTIAL and (tier == "acute" or c.metric == "data_stale") and sev in ("WARNING", "CRITICAL"):
        return _info(c)
    if sev != "WARNING":
        return c
    if c.metric in INFO_ALWAYS or (tier == "acute" and c.metric in INFO_ACUTE):
        return _info(c)
    return c


def _info(c: Candidate) -> Candidate:
    return dataclasses.replace(c, severity="INFO", message=_TAG.sub("[INFO]", c.message or ""))


def escalate_blind(cands: Iterable[Candidate], open_since: Dict[str, dt.datetime],
                   now_utc: dt.datetime, days: int = BLIND_DAYS) -> List[Candidate]:
    """The last resort: an INFO data_stale / inverter_silent whose alert has
    been OPEN since at least ``days`` days becomes a WARNING (mailed once:
    an escalation re-arms the mail; the 3-hourly re-mail is for CRITICAL
    only). ``open_since``: {alert_key: opened_utc} of the OPEN ledger records."""
    out = []
    for c in cands:
        since = open_since.get(c.alert_key)
        if (c.metric in POTENTIAL and (c.severity or "").upper() == "INFO" and since is not None
                and (now_utc - since) >= dt.timedelta(days=days)):
            n = (now_utc - since).days
            msg = re.sub(r"\s*\[INFO\]\s*$", "", c.message or "")
            c = dataclasses.replace(c, severity="WARNING",
                                    message=f"{msg} - no data for {n} days, production cannot be confirmed:"
                                            " check the datalogger [WARNING]")
        out.append(c)
    return out


def grade_all(cands: Iterable[Candidate], tier: str) -> List[Candidate]:
    return [grade(c, tier) for c in cands]
