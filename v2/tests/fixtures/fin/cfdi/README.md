# Demo supplier CFDIs

Synthetic CFDI 4.0 files (RFCs, UUIDs and names invented). What each one exercises:

- `01_electro_A1021_inverters_mxn.xml` — ingreso MXN, matches PO-2026-0007 exactly (three-way PASS)
- `02_estructuras_B77_structure_mxn.xml` — ingreso MXN, partial billing of PO-2026-0009 (partially received)
- `03_modulos_M310_modules_usd.xml` — ingreso USD with TipoCambio, PO in USD
- `04_ingenieria_C5_design_retencion.xml` — ingreso with IVA retention (professional services)
- `05_electro_NC12_credit_note.xml` — egreso (credit note) related to 01 by TipoRelacion 01
- `06_electro_P3_payment_complement.xml` — complemento de pago: 58,000 of 116,000 on 01
- `07_estructuras_B90_no_po.xml` — ingreso without a PO -> MISSING_PO exception
- `08_electro_A1021_duplicate_file.xml` — the same UUID as 01 in a second file -> must import 0 rows
- `09_tampered_total.xml` — Total does not equal subtotal+IVA -> refused (check_totals)
- `10_foreign_not_ours.xml` — receptor is not one of our RFCs -> FOREIGN_CFDI exception, never a payable
