"""Financial data layer: loans, schedules, debt service.

Tabs (Argia_Mont_v2):
  Loans          — one row per credit facility
  Loan_Schedule  — one row per loan per month (the amortization truth)

Everything financial derives from Loan_Schedule at query time. Nothing
stores a per-plant "monthly payment" scalar — that pattern is what let
v1's Credit tab go stale when SLP1 refinanced.
"""
