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

Pure.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Iterable, List

from argia.alerts.engine import Candidate

INFO_ALWAYS = frozenset({"inverter_temp_high", "inverter_silent", "energy_daily_pct", "plant_twin_yield"})
"""Metrics whose WARNING is INFO in every tier."""

INFO_ACUTE = frozenset({"data_stale"})
"""Metrics whose WARNING is INFO when the acute (intraday) tier sees it."""

_TAG = re.compile(r"\[WARNING\]\s*$")


def grade(c: Candidate, tier: str) -> Candidate:
    """The candidate at its policy severity. ``tier`` is 'acute' or 'daily'."""
    if tier not in ("acute", "daily"):
        raise ValueError(f"unknown tier: {tier!r}")
    if (c.severity or "").upper() != "WARNING":
        return c
    if c.metric in INFO_ALWAYS or (tier == "acute" and c.metric in INFO_ACUTE):
        return dataclasses.replace(c, severity="INFO", message=_TAG.sub("[INFO]", c.message or ""))
    return c


def grade_all(cands: Iterable[Candidate], tier: str) -> List[Candidate]:
    return [grade(c, tier) for c in cands]
