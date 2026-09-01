"""Replace SLP1's test loan with the real Oliva Hermanos credit.

Background (Tomasz, 2026-09-01): Quimica Coyoacan's own loan (SLP1-L1,
747,152 MXN / 24 installments) was fully repaid in 2026-05 — that loan
is real history and stays untouched. From 2026-06 the plant's cash flow
services the OLIVA HERMANOS credit (a separate project using Quimica
Coyoacan as payment security). SLP1-L2 (150,000 / 12 x 12,500) was only
a placeholder invented for testing and is DELETED here entirely.

The real credit, cross-verified between the BanBajio "Amortizaciones
Futuras" report (elaborated 01/09/26) and the curated workbook
Oliva_Hermanos_BanBajio_Open_Debt_Sep2026V2.xlsx — every shared
installment matched to the centavo:

    credit 17257769, BanBajio 97-Imodern, borrower Argia Mexico SA de CV
    5,200,000.00 MXN, disbursed 2025-04-11, maturity 2032-04-12
    84 installments (1-3 interest-only; principal 64,197.53/month from
    no. 4); installments 1-16 paid through 2026-08

SLP1 carries this loan FROM 2026-06 (installments 14-84, 71 rows) —
earlier installments were serviced before Quimica took over and must
not rewrite SLP1's older DSCR history. Bank installment numbering is
preserved so the report's loan position reads 16/84 as the bank sees
it (report_gen derives position from max(installment_no), not row
counts, since this change).

due_after_mxn is recomputed from the contractual principal (the
workbook's running-balance column starts from the wrong base and never
reaches zero; the recomputation ends at exactly 0.00 on 2032-04).

Idempotent, dry-run by default. Usage: switch_slp1_oliva.py [--apply]
"""

from __future__ import annotations

import argparse
import logging
import sys

LOG = logging.getLogger("argia.switch_slp1_oliva")

OLD_LOAN = "SLP1-L2"                 # the artificial test loan
NEW_LOAN = "SLP1-L3"
PLANT = "SLP1"
PRINCIPAL = 5_200_000.00
TOTAL_INSTALLMENTS = 84
PROJECT = "OLIVA HERMANOS (BanBajio 17257769, secured by Quimica Coyoacan)"

# (bank installment no, ref month, total payment MXN, outstanding after)
SCHEDULE = [
    (14, '2026-06', 121614.68, 4493827.17), (15, '2026-07', 122632.06, 4429629.64), (16, '2026-08', 116397.31, 4365432.11),
    (17, '2026-09', 109665.62, 4301234.58), (18, '2026-10', 108988.82, 4237037.05), (19, '2026-11', 106896.97, 4172839.52),
    (20, '2026-12', 106250.02, 4108641.99), (21, '2027-01', 106983.24, 4044444.46), (22, '2027-02', 106314.72, 3980246.93),
    (23, '2027-03', 101635.03, 3916049.40), (24, '2027-04', 106293.15, 3851851.87), (25, '2027-05', 101721.29, 3787654.34),
    (26, '2027-06', 103640.61, 3723456.81), (27, '2027-07', 102972.08, 3659259.28), (28, '2027-08', 101074.32, 3595061.75),
    (29, '2027-09', 104050.34, 3530864.22), (30, '2027-10', 97408.21, 3466666.69), (31, '2027-11', 100297.97, 3402469.16),
    (32, '2027-12', 100772.42, 3338271.63), (33, '2028-01', 96718.12, 3274074.10), (34, '2028-02', 98292.39, 3209876.57),
    (35, '2028-03', 97623.87, 3145679.04), (36, '2028-04', 94841.93, 3081481.51), (37, '2028-05', 95251.67, 3017283.98),
    (38, '2028-06', 96631.85, 2953086.45), (39, '2028-07', 92965.74, 2888888.92), (40, '2028-08', 94281.23, 2824691.39),
    (41, '2028-09', 93612.70, 2760493.86), (42, '2028-10', 92016.86, 2696296.33), (43, '2028-11', 94087.15, 2632098.80),
    (44, '2028-12', 88954.59, 2567901.27), (45, '2029-01', 90938.60, 2503703.74), (46, '2029-02', 91111.12, 2439506.21),
    (47, '2029-03', 87143.09, 2375308.68), (48, '2029-04', 88135.10, 2311111.15), (49, '2029-05', 87488.14, 2246913.62),
    (50, '2029-06', 87595.96, 2182716.09), (51, '2029-07', 86194.21, 2118518.56), (52, '2029-08', 87682.23, 2054321.03),
    (53, '2029-09', 84210.20, 1990123.50), (54, '2029-10', 84253.33, 1925925.97), (55, '2029-11', 84900.30, 1861728.44),
    (56, '2029-12', 82334.02, 1797530.91), (57, '2030-01', 82916.28, 1733333.38), (58, '2030-02', 82247.75, 1669135.85),
    (59, '2030-03', 79897.13, 1604938.32), (60, '2030-04', 80910.70, 1540740.79), (61, '2030-05', 80759.74, 1476543.26),
    (62, '2030-06', 78581.64, 1412345.73), (63, '2030-07', 78430.68, 1348148.20), (64, '2030-08', 78689.47, 1283950.67),
    (65, '2030-09', 77136.75, 1219753.14), (66, '2030-10', 76489.79, 1155555.61), (67, '2030-11', 76231.01, 1091358.08),
    (68, '2030-12', 75195.88, 1027160.55), (69, '2031-01', 75584.05, 962963.02), (70, '2031-02', 73578.47, 898765.49),
    (71, '2031-03', 72651.16, 834567.96), (72, '2031-04', 73729.43, 770370.43), (73, '2031-05', 71443.49, 706172.90),
    (74, '2031-06', 71314.10, 641975.37), (75, '2031-07', 70667.14, 577777.84), (76, '2031-08', 70214.27, 513580.31),
    (77, '2031-09', 69545.74, 449382.78), (78, '2031-10', 69028.18, 385185.25), (79, '2031-11', 67949.91, 320987.72),
    (80, '2031-12', 67432.34, 256790.19), (81, '2032-01', 66957.90, 192592.66), (82, '2032-02', 66138.42, 128395.13),
    (83, '2032-03', 65448.32, 64197.60), (84, '2032-04', 64887.69, 0.00),
]

# bank-report checksums — the script refuses to run if the embedded
# table drifts from what the source documents said
SUM_TOTAL = 6_266_930.69             # installments 14-84
SUM_OPEN = 5_906_286.64              # installments 17-84 (bank total)
OUTSTANDING_SEP2026 = 4_365_432.11   # after installment 16


def integrity_ok() -> bool:
    nos = [r[0] for r in SCHEDULE]
    months = [r[1] for r in SCHEDULE]
    return (nos == list(range(14, 85))
            and len(set(months)) == len(months)
            and months == sorted(months)
            and abs(sum(r[2] for r in SCHEDULE) - SUM_TOTAL) < 0.005
            and abs(sum(r[2] for r in SCHEDULE if r[0] >= 17)
                    - SUM_OPEN) < 0.005
            and abs(dict((r[0], r[3]) for r in SCHEDULE)[16]
                    - OUTSTANDING_SEP2026) < 0.005
            and SCHEDULE[-1][3] == 0.00)


def delete_sqls() -> list:
    return [
        "DELETE FROM loan_schedule WHERE loan_id = '%s';" % OLD_LOAN,
        "DELETE FROM loan WHERE loan_id = '%s';" % OLD_LOAN,
    ]


def insert_loan_sql() -> str:
    return ("INSERT INTO loan (loan_id, plant_key, project_name, bank,"
            " currency, principal_mxn, total_installments, first_month,"
            " last_month) VALUES ('%s','%s','%s','BanBajio','MXN',%.2f,"
            "%d,DATE '2026-06-01',DATE '2032-04-01')"
            " ON CONFLICT (loan_id) DO NOTHING;"
            % (NEW_LOAN, PLANT, PROJECT, PRINCIPAL, TOTAL_INSTALLMENTS))


def insert_schedule_sql() -> str:
    values = ",\n".join(
        "('%s',DATE '%s-01',%d,%.2f,%.2f)"
        % (NEW_LOAN, ym, no, pay, due) for no, ym, pay, due in SCHEDULE)
    return ("INSERT INTO loan_schedule (loan_id, ref_month,"
            " installment_no, payment_mxn, due_after_mxn) VALUES\n"
            + values + "\nON CONFLICT DO NOTHING;")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not integrity_ok():
        LOG.error("embedded schedule fails its bank-report checksums")
        return 1

    from argia.store import pg_mirror
    from argia.store.pgq import psql_exec, psql_rows
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0

    old = psql_rows("SELECT count(*) FROM loan_schedule WHERE loan_id"
                    " = '%s';" % OLD_LOAN)[0][0]
    LOG.info("test loan %s: %s schedule row(s) to delete", OLD_LOAN, old)
    LOG.info("new loan %s: %d row(s) 2026-06 .. 2032-04, %.2f MXN total",
             NEW_LOAN, len(SCHEDULE), SUM_TOTAL)
    if not args.apply:
        LOG.info("dry-run — nothing written")
        return 0

    psql_exec("""CREATE TABLE IF NOT EXISTS finance_audit (
        id serial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
        username text NOT NULL, plant_key text, loan_id text,
        action text NOT NULL, detail text);""")
    for sql in delete_sqls():
        psql_exec(sql)
    psql_exec(insert_loan_sql())
    psql_exec(insert_schedule_sql())
    psql_exec("INSERT INTO finance_audit (username, plant_key, loan_id,"
              " action, detail) VALUES ('tomasz','%s','%s','switch',"
              "'%s test loan deleted; real OLIVA HERMANOS credit "
              "17257769 loaded (installments 14-84, bank report "
              "01/09/26)');" % (PLANT, NEW_LOAN, OLD_LOAN))

    # verification against the bank checksums
    got = psql_rows(
        "SELECT count(*), round(sum(payment_mxn)::numeric,2),"
        " min(installment_no), max(installment_no),"
        " round(min(due_after_mxn)::numeric,2)"
        " FROM loan_schedule WHERE loan_id = '%s';" % NEW_LOAN)[0]
    ok = (int(got[0]) == len(SCHEDULE)
          and abs(float(got[1]) - SUM_TOTAL) < 0.005
          and got[2] == "14" and got[3] == "84"
          and float(got[4]) == 0.00)
    LOG.info("VERIFY %s: rows=%s sum=%s no=%s..%s min_due=%s -> %s",
             NEW_LOAN, got[0], got[1], got[2], got[3], got[4],
             "OK" if ok else "MISMATCH")
    left = psql_rows("SELECT count(*) FROM loan_schedule WHERE loan_id"
                     " = '%s';" % OLD_LOAN)[0][0]
    LOG.info("VERIFY %s deleted: %s row(s) remain", OLD_LOAN, left)
    if not ok or left != "0":
        LOG.error("verification FAILED")
        return 1
    LOG.info("verification passed — SLP1 debt is now the Oliva "
             "Hermanos credit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
