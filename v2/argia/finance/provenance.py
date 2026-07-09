"""Data-provenance registry — the single source of audit text.

Every finance tab and column gets a short "where this number comes
from" statement here. Two consumers:

  * ``scripts/annotate_finance_tabs.py`` writes these into the live
    sheet (header-cell notes + a NOTES-tab section), so anyone opening
    the spreadsheet sees the provenance without leaving it.
  * Report builders (invoicing annex, investor report) import
    ``report_sources()`` for their audit footers, so the sheet and the
    PDFs can never tell different stories about the same column.

House rule: when a calculation or source changes, this file changes in
the same commit.
"""

from __future__ import annotations

from typing import Dict, List

# ---------------------------------------------------------------------------
# Column-level provenance: {tab: {column: note}}
# ---------------------------------------------------------------------------

COLUMN_NOTES: Dict[str, Dict[str, str]] = {
    "Loans": {
        "loan_id": "Assigned at migration (plant + sequence, e.g. "
                   "SLP1-L2). The unit of financial identity — a plant "
                   "can carry any number of loans.",
        "principal_mxn": "MXN value at booking, from bank contract via "
                         "v1 ARGIA_Solar LoanPayments (export "
                         "2026-07-08). For USD loans this is the "
                         "booking-date restatement.",
        "currency": "MXN = peso-denominated facility; USD = "
                    "dollar-denominated (both LaaS loans). USD "
                    "facilities' MXN figures move with the exchange "
                    "rate.",
        "total_installments": "From the bank amortization table. "
                              "LOAX1's month-1 '1/83' in v1 was a "
                              "typo; installments run 1..82.",
        "first_month": "First installment month, from the bank "
                       "schedule.",
        "last_month": "Final installment month, from the bank "
                      "schedule.",
        "bank": "Lender, from v1 LoanPayments.",
        "plant_key": "Asset the facility finances.",
        "project_name": "Customer/project label, from v1.",
    },
    "Loan_Schedule": {
        "payment_mxn": "Monthly installment in MXN, from the bank "
                       "amortization table (v1 LoanPayments). For USD "
                       "loans: payment_ccy × xr, verified exact on all "
                       "154 USD rows at migration.",
        "payment_ccy": "USD loans only: the installment in USD (sum of "
                       "the facility's components). Blank for MXN "
                       "loans.",
        "xr": "Exchange rate applied to that month. Past months: "
              "historical rate actually paid. Future months: v1's "
              "projection (last known rate, 17.98) — a projection, "
              "not a commitment.",
        "due_after_mxn": "Remaining repayment obligation after the "
                         "installment (v1 running balance). NOTE: this "
                         "is principal+interest combined — v1 never "
                         "stored the rate, so an interest/principal "
                         "split is not derivable from this data.",
        "installment_no": "Position in the amortization sequence.",
        "ref_month": "Month the installment is due.",
        "loan_id": "Foreign key to Loans.",
        "plant_key": "Denormalized from Loans for query convenience.",
        "total_installments": "Denormalized from Loans.",
    },
    "Contract_Monthly": {
        "design_kwh": "Engineering expectation for the AS-BUILT plant "
                      "(incl. expansions, e.g. GTO1 phase 2), "
                      "degradation clocked from actual COD. Source: "
                      "Argia design model (was Design_Monthly). Feeds "
                      "'% of design'. Updated when a design rerun is "
                      "done.",
        "contract_kwh": "What the PPA contract obliges (excl. "
                        "uncontracted expansions). Source: signed "
                        "contract via v1 ContractData (export "
                        "2026-07-09). Feeds maintenance-day 'energía "
                        "compensada' (monthly value ÷ days in month) "
                        "and expected income.",
        "tariff_mxn": "PPA tariff in force THAT month, from the signed "
                      "contract incl. negotiated escalations (v1 "
                      "ContractData). Edit future rows on "
                      "renegotiation; never edit invoiced months.",
        "fixed_income_ccy": "LaaS monthly fee in native currency, from "
                            "the service contract (LOAX1 26,750 USD / "
                            "LGTO1 15,233 USD). Blank for PPA rows. "
                            "MXN value = fee × the same XR the loan "
                            "schedule uses that month.",
        "ccy": "Currency of fixed_income_ccy (USD for both current "
               "LaaS contracts). Blank for PPA rows.",
        "plant_key": "Asset key.",
        "year": "Calendar year of the row.",
        "month": "Calendar month of the row (1-12).",
    },
    "Plants": {
        "om_cost_monthly_mxn": "MANUAL INPUT: average monthly O&M cost "
                               "per plant, MXN (Argia estimate, not an "
                               "invoice feed). Feeds the investor "
                               "report opex line, prorated for partial "
                               "periods. Updating it recomputes past "
                               "on-demand reports — treat regenerated "
                               "history accordingly.",
    },
}

# ---------------------------------------------------------------------------
# NOTES-tab section (freeform lines, appended once, marker-guarded)
# ---------------------------------------------------------------------------

NOTES_MARKER = "Finance layer (v60-v61) — data provenance"

NOTES_SECTION: List[str] = [
    "",
    NOTES_MARKER,
    "Loans + Loan_Schedule: migrated 1:1 from v1 ARGIA_Solar "
    "LoanPayments (export 2026-07-08); bank amortization tables are "
    "the ultimate source. 9 loans, 589 monthly rows, 2023-09..2032-09.",
    "  - Debt service is always DERIVED by summing Loan_Schedule rows "
    "for a month — no stored per-plant payment exists anywhere "
    "(v1's stale-SLP1 failure mode is structurally excluded).",
    "  - USD loans (LOAX1, LGTO1): payment_mxn = payment_ccy x xr. "
    "Future months use v1's projected rate (17.98) — projections, "
    "not commitments.",
    "  - due_after_mxn is principal+interest combined; no interest "
    "rate was ever recorded, so an interest/principal split needs the "
    "banks' rates (pending).",
    "Contract_Monthly: commercial expectations per plant-month, "
    "2024..2043 (1,235 rows).",
    "  - contract_kwh + tariff_mxn: from signed contracts via v1 "
    "ContractData (export 2026-07-09), escalations included.",
    "  - design_kwh: Argia engineering model, as-built capacity, "
    "degradation from actual COD (supersedes Design_Monthly; that tab "
    "can be deleted once kpi.log shows 'Design baseline loaded ... "
    "from Contract_Monthly').",
    "  - GTO1 design > contract from 2026-07: phase-2 expansion built "
    "but not yet contracted. Both numbers are correct; they measure "
    "different things.",
    "  - fixed_income_ccy: LaaS fees from service contracts, "
    "USD-indexed; MXN conversion uses the loan schedule's XR for the "
    "same month so income and debt service share an FX basis.",
    "Plants.om_cost_monthly_mxn: MANUAL average monthly O&M per plant "
    "(MXN). Not invoice-fed by design.",
    "Maintenance-day billing ('energia compensada'): contract_kwh / "
    "days-in-month is the agreed basis (settled 2026-07; v1's "
    "efficiency-factor de-rating is deliberately NOT carried over).",
    "Reports: every generated report carries these same source "
    "statements in its audit footer (argia/finance/provenance.py is "
    "the single source; sheet notes and report footers are generated "
    "from it).",
]


def report_sources(*tabs: str) -> Dict[str, Dict[str, str]]:
    """Provenance for the given tabs, for report audit footers.
    Unknown tab names raise — a report citing an undocumented source
    should fail loudly in tests, not print an empty footer."""
    out = {}
    for tab in tabs:
        if tab not in COLUMN_NOTES:
            raise KeyError("no provenance registered for tab %r" % tab)
        out[tab] = COLUMN_NOTES[tab]
    return out
