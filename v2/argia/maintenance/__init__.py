"""Maintenance events — the operational + billing record of anything
that takes a plant off its normal production curve.

Two things live off this one manually-entered tab (v91):

* **Deemed energy** ("energía compensada") — when *customer* operations
  force a PPA plant down, the contract entitles Argia to bill what the
  plant would have produced. The basis is contract-anchored
  (``Contract_Monthly.contract_kwh`` ÷ days-in-month), never estimated
  from other days — see :mod:`argia.maintenance.deemed`.
* **Actual O&M cost** — every event can carry a real ``cost_mxn`` that
  replaces the old flat ``om_cost_monthly_mxn`` estimate in the
  financial report. There are no recurring O&M retainers; cost is
  incurred only when work happens.

Fail-closed by construction: an event does nothing (no deemed billing,
no cost) until ``approved_by`` is set, exactly like Recipients.
"""
