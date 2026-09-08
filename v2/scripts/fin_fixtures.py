#!/usr/bin/env python3
"""The finance/PM DEMO world — deterministic dummy data for tests, the
Savio mock endpoint and the demo seed (v244, Tomasz: "create some
dummy data so we can always use it for testing purposes going
forward").

    fin_fixtures.py                 # writes tests/fixtures/fin/** (idempotent, seeded)
    fin_fixtures.py --check         # exit 2 when the committed files differ from the generator

One story, told in every format the real world will use:

  * two legal entities: DEMO-MX (ARGIA DEMO MEXICO, MXN, RFC DEM150101AB1)
    and DEMO-CZ (ARGIA DEMO CZ, EUR-ish but kept USD/MXN simple)
  * three bank accounts: DEMO-BBVA-MXN, DEMO-BANORTE-USD, DEMO-CZ-EUR
  * six projects ARG9001..ARG9006 (EPC/CAPEX/PPA/LaaS, one on hold, one
    commissioned into a plant)
  * suppliers with RFCs; POs; supplier CFDIs (ingreso MXN, ingreso USD,
    a credit note, a payment complement, a duplicate file, a tampered
    total, a foreign one that must not become a payable)
  * Savio: customers, invoices in two cursor pages (include=cfdis,items),
    payments, webhooks
  * bank statements July + August per account, balanced to the cent,
    with the payments above, an own transfer, a fee, one unknown line
  * a PMO snapshot (phases, tasks, milestones baseline/planned/actual)
  * a Drive project tree listing

Every number is chosen so the tests can assert exact totals. Nothing
here is real: RFCs, CLABEs, names and UUIDs are synthetic.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import random
import sys
import uuid as _uuid
from decimal import Decimal
from pathlib import Path

V2 = Path(__file__).resolve().parents[1]
OUT = V2 / "tests" / "fixtures" / "fin"
SEED = 20260908

ENTITIES = [
    {"entity_id": "DEMO-MX", "name": "ARGIA DEMO MEXICO SA DE CV", "rfc": "DEM150101AB1", "currency": "MXN"},
    {"entity_id": "DEMO-CZ", "name": "ARGIA DEMO CZ s.r.o.", "rfc": None, "currency": "EUR"},
]
ACCOUNTS = [
    {"account_id": "DEMO-BBVA-MXN", "entity_id": "DEMO-MX", "bank": "BBVA", "currency": "MXN", "clabe_last4": "4455", "purpose": "operations", "clabe": "012180001122334455"},
    {"account_id": "DEMO-BANORTE-USD", "entity_id": "DEMO-MX", "bank": "Banorte", "currency": "USD", "clabe_last4": "7788", "purpose": "USD suppliers", "clabe": "072180009988776677"},
    {"account_id": "DEMO-CZ-EUR", "entity_id": "DEMO-CZ", "bank": "Fio", "currency": "EUR", "clabe_last4": "0100", "purpose": "CZ operations", "clabe": "CZ6520100000002600000100"},
]
COST_CODES = [
    ("1", None, "Development", "Desarrollo"), ("1.1", "1", "Studies", "Estudios"), ("1.2", "1", "Permits", "Permisos"), ("1.3", "1", "Interconnection", "Interconexión"),
    ("2", None, "Engineering", "Ingeniería"), ("2.1", "2", "Design", "Diseño"), ("2.2", "2", "Structural", "Estructural"),
    ("3", None, "Supply", "Suministro"), ("3.1", "3", "Modules", "Módulos"), ("3.2", "3", "Inverters / BESS", "Inversores / BESS"), ("3.3", "3", "Structure", "Estructura"), ("3.4", "3", "Electrical BOS", "BOS eléctrico"), ("3.5", "3", "Monitoring / comms", "Monitoreo / comunicaciones"),
    ("4", None, "Construction", "Construcción"), ("4.1", "4", "Civil", "Civil"), ("4.2", "4", "Mechanical", "Mecánica"), ("4.3", "4", "Electrical", "Eléctrica"), ("4.4", "4", "Site services", "Servicios de obra"), ("4.5", "4", "Safety", "Seguridad"),
    ("5", None, "Commissioning", "Puesta en marcha"), ("5.1", "5", "Testing", "Pruebas"), ("5.2", "5", "CFE / utility", "CFE"), ("5.3", "5", "Handover", "Entrega"),
    ("6", None, "Project overhead", "Indirectos"), ("6.1", "6", "Project management", "Gestión"), ("6.2", "6", "Insurance", "Seguros"), ("6.3", "6", "Logistics", "Logística"), ("6.4", "6", "Contingency", "Contingencia"),
    ("7", None, "O&M", "O&M"), ("7.1", "7", "Preventive", "Preventivo"), ("7.2", "7", "Corrective", "Correctivo"), ("7.3", "7", "Warranty", "Garantía"),
    ("8", None, "Finance", "Finanzas"), ("8.1", "8", "Fees", "Comisiones"), ("8.2", "8", "FX", "Tipo de cambio"),
]
SUPPLIERS = [
    {"rfc": "ELE010101AB1", "name": "ELECTRO SUMINISTROS DEMO SA DE CV", "entity_id": "DEMO-MX", "terms_days": 30, "clabe": "014180000011112222"},
    {"rfc": "EST020202CD2", "name": "ESTRUCTURAS DEL BAJIO DEMO SA DE CV", "entity_id": "DEMO-MX", "terms_days": 45, "clabe": "021180000033334444"},
    {"rfc": "MOD030303EF3", "name": "MODULOS SOLARES DEMO SA DE CV", "entity_id": "DEMO-MX", "terms_days": 60, "clabe": "044180000055556666"},
    {"rfc": "ING040404GH4", "name": "INGENIERIA DEMO SC", "entity_id": "DEMO-MX", "terms_days": 15, "clabe": "058180000077778888"},
]
CUSTOMERS = [
    {"rfc": "TAI050505IJ5", "name": "TAIGENE DEMO SA DE CV", "savio_customer_id": "cus_demo_001", "terms_days": 30},
    {"rfc": "HOT060606KL6", "name": "HOTELES DEMO SLP SA DE CV", "savio_customer_id": "cus_demo_002", "terms_days": 30},
    {"rfc": "LOG070707MN7", "name": "LOGISTICA DEMO DEL NORTE SA DE CV", "savio_customer_id": "cus_demo_003", "terms_days": 45},
    {"rfc": "PLA080808OP8", "name": "PLASTICOS DEMO SA DE CV", "savio_customer_id": "cus_demo_004", "terms_days": 30},
]
PROJECTS = [
    {"project_id": "ARG9001", "entity_id": "DEMO-MX", "customer_rfc": "HOT060606KL6", "name": "Hotel SLP 417 kWp + BESS", "site": "San Luis Potosí, SLP", "project_type": "EPC", "kwp_dc": 417.0, "status": "active", "pm_user": "tomasz", "offer_ref": "OFF-2026-031", "contract_value": 7240000, "contract_ccy": "MXN", "phase": "3_execution"},
    {"project_id": "ARG9002", "entity_id": "DEMO-MX", "customer_rfc": "TAI050505IJ5", "name": "Taigene roof extension 300 kWp", "site": "León, GTO", "project_type": "PPA", "kwp_dc": 300.0, "status": "active", "pm_user": "juan", "offer_ref": "OFF-2026-018", "contract_value": 4100000, "contract_ccy": "MXN", "phase": "2_preparation"},
    {"project_id": "ARG9003", "entity_id": "DEMO-MX", "customer_rfc": "LOG070707MN7", "name": "Logística Norte carport 650 kWp", "site": "Nuevo Laredo, TAM", "project_type": "CAPEX", "kwp_dc": 650.0, "status": "active", "pm_user": "juan", "offer_ref": "OFF-2026-022", "contract_value": 11800000, "contract_ccy": "MXN", "phase": "3_execution"},
    {"project_id": "ARG9004", "entity_id": "DEMO-MX", "customer_rfc": "PLA080808OP8", "name": "Plásticos Demo LaaS lighting", "site": "Monterrey, NL", "project_type": "LAAS", "kwp_dc": None, "status": "on_hold", "pm_user": "arturo", "offer_ref": "OFF-2026-009", "contract_value": 140000, "contract_ccy": "USD", "phase": "8_on hold"},
    {"project_id": "ARG9005", "entity_id": "DEMO-MX", "customer_rfc": "TAI050505IJ5", "name": "Taigene BESS 200 kW / 430 kWh", "site": "León, GTO", "project_type": "CAPEX", "kwp_dc": 0.0, "status": "commissioned", "pm_user": "tomasz", "offer_ref": "OFF-2025-077", "contract_value": 5600000, "contract_ccy": "MXN", "phase": "5_review"},
    {"project_id": "ARG9006", "entity_id": "DEMO-CZ", "customer_rfc": None, "name": "Prologis CZ monitoring pilot", "site": "Prague, CZ", "project_type": "OTHER", "kwp_dc": 0.0, "status": "draft", "pm_user": "vit", "offer_ref": "OFF-2026-040", "contract_value": 38000, "contract_ccy": "EUR", "phase": "1_specification"},
]

OUR_RFC = "DEM150101AB1"


def _uuid5(name: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, "argia-demo/" + name)).upper()


def cfdi_xml(*, uuid, tipo, serie, folio, fecha, emisor_rfc, emisor_nombre, receptor_rfc, receptor_nombre,
             moneda, subtotal, iva, total, tipo_cambio="1", retenido=None, relacionados=(), tipo_relacion="",
             metodo="PPD", uso="G03", concepto="Servicio", pagos=None):
    """A CFDI 4.0 document the parser accepts. `pagos` -> a P complement."""
    rel = ""
    if relacionados:
        rel = (f'<cfdi:CfdiRelacionados TipoRelacion="{tipo_relacion}">'
               + "".join(f'<cfdi:CfdiRelacionado UUID="{u}"/>' for u in relacionados) + "</cfdi:CfdiRelacionados>")
    if tipo == "P":
        pago_xml = "".join(
            f'<pago20:Pago FechaPago="{p["fecha"]}" FormaDePagoP="03" MonedaP="{p["moneda"]}" Monto="{p["monto"]}">'
            + "".join(f'<pago20:DoctoRelacionado IdDocumento="{d["uuid"]}" MonedaDR="{d["moneda"]}" NumParcialidad="{d["n"]}" '
                      f'ImpSaldoAnt="{d["ant"]}" ImpPagado="{d["pag"]}" ImpSaldoInsoluto="{d["ins"]}" ObjetoImpDR="02"/>' for d in p["docs"])
            + "</pago20:Pago>" for p in pagos)
        return (f'<?xml version="1.0" encoding="utf-8"?>\n<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
                f'xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" xmlns:pago20="http://www.sat.gob.mx/Pagos20" '
                f'Version="4.0" Serie="{serie}" Folio="{folio}" Fecha="{fecha}" SubTotal="0" Moneda="XXX" Total="0" '
                f'TipoDeComprobante="P" LugarExpedicion="37138">{rel}'
                f'<cfdi:Emisor Rfc="{emisor_rfc}" Nombre="{emisor_nombre}" RegimenFiscal="601"/>'
                f'<cfdi:Receptor Rfc="{receptor_rfc}" Nombre="{receptor_nombre}" UsoCFDI="CP01" DomicilioFiscalReceptor="37138" RegimenFiscalReceptor="601"/>'
                f'<cfdi:Conceptos><cfdi:Concepto ClaveProdServ="84111506" Cantidad="1" ClaveUnidad="ACT" Descripcion="Pago" ValorUnitario="0" Importe="0" ObjetoImp="01"/></cfdi:Conceptos>'
                f'<cfdi:Complemento><pago20:Pagos Version="2.0">{pago_xml}</pago20:Pagos>'
                f'<tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}" FechaTimbrado="{fecha}" SelloCFD="demo" NoCertificadoSAT="00001000000500000001" SelloSAT="demo"/>'
                f'</cfdi:Complemento></cfdi:Comprobante>\n')
    ret = f' TotalImpuestosRetenidos="{retenido}"' if retenido else ""
    ret_xml = (f'<cfdi:Retenciones><cfdi:Retencion Impuesto="002" Importe="{retenido}"/></cfdi:Retenciones>' if retenido else "")
    tc = f' TipoCambio="{tipo_cambio}"' if moneda != "MXN" else ""
    return (f'<?xml version="1.0" encoding="utf-8"?>\n<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
            f'xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" Version="4.0" Serie="{serie}" Folio="{folio}" Fecha="{fecha}" '
            f'SubTotal="{subtotal}" Descuento="0.00" Moneda="{moneda}"{tc} Total="{total}" TipoDeComprobante="{tipo}" '
            f'MetodoPago="{metodo}" FormaPago="99" LugarExpedicion="37138">{rel}'
            f'<cfdi:Emisor Rfc="{emisor_rfc}" Nombre="{emisor_nombre}" RegimenFiscal="601"/>'
            f'<cfdi:Receptor Rfc="{receptor_rfc}" Nombre="{receptor_nombre}" UsoCFDI="{uso}" DomicilioFiscalReceptor="37138" RegimenFiscalReceptor="601"/>'
            f'<cfdi:Conceptos><cfdi:Concepto ClaveProdServ="26111700" Cantidad="1" ClaveUnidad="E48" Descripcion="{concepto}" ValorUnitario="{subtotal}" Importe="{subtotal}" ObjetoImp="02"/></cfdi:Conceptos>'
            f'<cfdi:Impuestos TotalImpuestosTrasladados="{iva}"{ret}>{ret_xml}<cfdi:Traslados><cfdi:Traslado Base="{subtotal}" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="{iva}"/></cfdi:Traslados></cfdi:Impuestos>'
            f'<cfdi:Complemento><tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}" FechaTimbrado="{fecha}" SelloCFD="demo" NoCertificadoSAT="00001000000500000001" SelloSAT="demo"/></cfdi:Complemento>'
            f'</cfdi:Comprobante>\n')


# The supplier invoices of the story (all on DEMO-MX). Amounts net + 16 % IVA.
SUPPLIER_CFDIS = [
    # file, uuid-name, tipo, supplier idx, project, po, cost, fecha, ccy, subtotal, iva, total, extras
    ("01_electro_A1021_inverters_mxn.xml", "ELE-A-1021", "I", 0, "ARG9001", "PO-2026-0007", "3.2", "2026-08-14T10:22:31", "MXN", "100000.00", "16000.00", "116000.00", {}),
    ("02_estructuras_B77_structure_mxn.xml", "EST-B-77", "I", 1, "ARG9001", "PO-2026-0009", "3.3", "2026-08-20T09:00:00", "MXN", "480000.00", "76800.00", "556800.00", {}),
    ("03_modulos_M310_modules_usd.xml", "MOD-M-310", "I", 2, "ARG9003", "PO-2026-0011", "3.1", "2026-08-05T12:00:00", "USD", "210000.00", "33600.00", "243600.00", {"tipo_cambio": "18.4200"}),
    ("04_ingenieria_C5_design_retencion.xml", "ING-C-5", "I", 3, "ARG9002", "PO-2026-0012", "2.1", "2026-08-25T15:30:00", "MXN", "60000.00", "9600.00", "63200.00", {"retenido": "6400.00"}),
    ("05_electro_NC12_credit_note.xml", "ELE-NC-12", "E", 0, "ARG9001", "PO-2026-0007", "3.2", "2026-08-28T11:00:00", "MXN", "10000.00", "1600.00", "11600.00", {"relacionados": ("ELE-A-1021",), "tipo_relacion": "01"}),
    ("06_electro_P3_payment_complement.xml", "ELE-P-3", "P", 0, "ARG9001", "PO-2026-0007", "3.2", "2026-09-01T09:00:00", "MXN", "0", "0", "0", {"pagos": True}),
    ("07_estructuras_B90_no_po.xml", "EST-B-90", "I", 1, None, None, None, "2026-09-03T10:00:00", "MXN", "25000.00", "4000.00", "29000.00", {}),
    ("08_electro_A1021_duplicate_file.xml", "ELE-A-1021", "I", 0, "ARG9001", "PO-2026-0007", "3.2", "2026-08-14T10:22:31", "MXN", "100000.00", "16000.00", "116000.00", {"duplicate": True}),
    ("09_tampered_total.xml", "TAM-X-1", "I", 1, "ARG9001", "PO-2026-0009", "3.3", "2026-09-02T10:00:00", "MXN", "50000.00", "8000.00", "57000.00", {}),
    ("10_foreign_not_ours.xml", "FOR-Z-9", "I", 0, None, None, None, "2026-09-02T10:00:00", "MXN", "1000.00", "160.00", "1160.00", {"receptor": ("OTR990101ZZ9", "OTRA EMPRESA SA DE CV")}),
]
POS = [
    {"po_number": "PO-2026-0007", "supplier_rfc": "ELE010101AB1", "project_id": "ARG9001", "cost_code": "3.2", "currency": "MXN", "subtotal": "100000.00", "tax": "16000.00", "total": "116000.00", "status": "approved", "created_by": "juan", "approvals": [("pm", "juan"), ("director", "tomasz")], "received": "116000.00", "expected_delivery": "2026-08-12"},
    {"po_number": "PO-2026-0009", "supplier_rfc": "EST020202CD2", "project_id": "ARG9001", "cost_code": "3.3", "currency": "MXN", "subtotal": "600000.00", "tax": "96000.00", "total": "696000.00", "status": "partially_received", "created_by": "juan", "approvals": [("pm", "juan"), ("director", "tomasz"), ("board", "arturo")], "received": "556800.00", "expected_delivery": "2026-09-15"},
    {"po_number": "PO-2026-0011", "supplier_rfc": "MOD030303EF3", "project_id": "ARG9003", "cost_code": "3.1", "currency": "USD", "subtotal": "210000.00", "tax": "33600.00", "total": "243600.00", "status": "approved", "created_by": "juan", "approvals": [("pm", "juan"), ("director", "tomasz"), ("board", "arturo")], "received": "243600.00", "expected_delivery": "2026-08-01"},
    {"po_number": "PO-2026-0012", "supplier_rfc": "ING040404GH4", "project_id": "ARG9002", "cost_code": "2.1", "currency": "MXN", "subtotal": "60000.00", "tax": "9600.00", "total": "69600.00", "status": "approved", "created_by": "tomasz", "approvals": [("pm", "tomasz"), ("director", "arturo")], "received": "69600.00", "expected_delivery": "2026-08-20"},
    {"po_number": "PO-2026-0013", "supplier_rfc": "EST020202CD2", "project_id": "ARG9003", "cost_code": "3.3", "currency": "MXN", "subtotal": "1200000.00", "tax": "192000.00", "total": "1392000.00", "status": "submitted", "created_by": "juan", "approvals": [("pm", "juan")], "received": "0.00", "expected_delivery": "2026-10-10"},
    {"po_number": "PO-2026-0014", "supplier_rfc": "ELE010101AB1", "project_id": "ARG9005", "cost_code": "7.2", "currency": "MXN", "subtotal": "18000.00", "tax": "2880.00", "total": "20880.00", "status": "cancelled", "created_by": "arturo", "approvals": [], "received": "0.00", "expected_delivery": "2026-07-30"},
]
BUDGETS = {
    "ARG9001": [(1, "superseded", {"2.1": 250000, "3.1": 2100000, "3.2": 700000, "3.3": 620000, "3.4": 380000, "4.2": 520000, "4.3": 460000, "5.1": 90000, "6.1": 300000, "6.4": 190000}),
                (2, "approved", {"2.1": 250000, "3.1": 2100000, "3.2": 700000, "3.3": 720000, "3.4": 380000, "4.2": 520000, "4.3": 460000, "5.1": 90000, "6.1": 300000, "6.4": 190000})],
    "ARG9002": [(1, "approved", {"2.1": 120000, "3.1": 1500000, "3.2": 420000, "3.3": 380000, "3.4": 260000, "4.2": 300000, "4.3": 280000, "6.1": 150000, "6.4": 100000})],
    "ARG9003": [(1, "approved", {"2.1": 300000, "3.1": 4200000, "3.2": 1300000, "3.3": 1450000, "3.4": 700000, "4.1": 600000, "4.2": 900000, "4.3": 750000, "6.1": 400000, "6.4": 350000})],
    "ARG9004": [(1, "approved", {"3.5": 60000, "4.3": 30000, "6.1": 10000})],
    "ARG9005": [(1, "approved", {"3.2": 3600000, "3.4": 400000, "4.3": 500000, "5.1": 150000, "6.1": 200000, "6.4": 150000})],
    "ARG9006": [],
}
CHANGE_ORDERS = [
    {"project_id": "ARG9001", "ref": "CO-1", "status": "pending", "revenue_impact": 620000, "cost_impact": 480000, "note": "30 kW BESS expansion"},
    {"project_id": "ARG9001", "ref": "CO-2", "status": "approved", "revenue_impact": 0, "cost_impact": 100000, "note": "structure change (supplier)"},
    {"project_id": "ARG9003", "ref": "CO-1", "status": "approved", "revenue_impact": 350000, "cost_impact": 210000, "note": "extra 20 carport bays"},
    {"project_id": "ARG9003", "ref": "CO-2", "status": "rejected", "revenue_impact": 90000, "cost_impact": 95000, "note": "EV chargers (declined)"},
]
MILESTONES = {
    "ARG9001": [("M1", "Engineering release", "technical", "2026-04-15", "2026-04-15", "2026-04-14", False, 0, []),
                ("M2", "Modules on site", "commercial", "2026-05-20", "2026-05-20", "2026-05-18", True, 2172000, ["M1"]),
                ("M3", "Mechanical complete", "technical", "2026-06-30", "2026-07-21", None, False, 0, ["M2"]),
                ("M4", "Interconnection", "commercial", "2026-07-31", "2026-09-30", None, True, 4344000, ["M3"]),
                ("M5", "Commissioning", "commercial", "2026-08-15", "2026-10-15", None, True, 724000, ["M4"])],
    "ARG9002": [("M1", "Contract signed", "commercial", "2026-07-01", "2026-07-01", "2026-07-01", True, 820000, []),
                ("M2", "Design approved", "technical", "2026-08-15", "2026-09-05", None, False, 0, ["M1"]),
                ("M3", "Construction start", "technical", "2026-09-15", "2026-10-01", None, False, 0, ["M2"]),
                ("M4", "COD", "commercial", "2026-12-15", "2027-01-15", None, True, 3280000, ["M3"])],
    "ARG9003": [("M1", "Civil complete", "technical", "2026-06-30", "2026-06-30", "2026-07-03", False, 0, []),
                ("M2", "Modules delivered", "commercial", "2026-08-01", "2026-08-01", "2026-08-04", True, 4720000, ["M1"]),
                ("M3", "Structure complete", "technical", "2026-09-10", "2026-09-25", None, False, 0, ["M2"]),
                ("M4", "Energisation", "commercial", "2026-11-15", "2026-11-30", None, True, 5900000, ["M3"]),
                ("M5", "Handover", "commercial", "2026-12-15", "2026-12-20", None, True, 1180000, ["M4"])],
    "ARG9004": [("M1", "Survey", "technical", "2026-03-01", "2026-03-01", "2026-03-02", False, 0, []),
                ("M2", "Install", "commercial", "2026-05-01", "2026-11-01", None, True, 140000, ["M1"])],
    "ARG9005": [("M1", "BESS delivered", "commercial", "2026-02-10", "2026-02-10", "2026-02-09", True, 2800000, []),
                ("M2", "Commissioned", "commercial", "2026-05-30", "2026-05-30", "2026-06-02", True, 2240000, ["M1"]),
                ("M3", "Final acceptance", "commercial", "2026-08-30", "2026-08-30", "2026-08-28", True, 560000, ["M2"])],
    "ARG9006": [("M1", "Kick-off", "technical", "2026-10-01", "2026-10-01", None, False, 0, [])],
}
# customer invoices in Savio (AR): milestone billings + one PPA month + a cancelled one
SAVIO_INVOICES = [
    {"invoice_id": "inv_demo_0101", "customer": "cus_demo_002", "project_id": "ARG9001", "milestone": "M2", "issue": "2026-05-19", "due": "2026-06-18", "ccy": "MXN", "subtotal": "2172000.00", "tax": "347520.00", "total": "2519520.00", "status": "paid", "uuid": "SAV-0101"},
    {"invoice_id": "inv_demo_0102", "customer": "cus_demo_003", "project_id": "ARG9003", "milestone": "M2", "issue": "2026-08-05", "due": "2026-09-19", "ccy": "MXN", "subtotal": "4720000.00", "tax": "755200.00", "total": "5475200.00", "status": "partially_paid", "uuid": "SAV-0102"},
    {"invoice_id": "inv_demo_0103", "customer": "cus_demo_001", "project_id": "ARG9002", "milestone": "M1", "issue": "2026-07-02", "due": "2026-08-01", "ccy": "MXN", "subtotal": "820000.00", "tax": "131200.00", "total": "951200.00", "status": "open", "uuid": "SAV-0103"},
    {"invoice_id": "inv_demo_0104", "customer": "cus_demo_001", "project_id": "ARG9005", "milestone": "M3", "issue": "2026-08-29", "due": "2026-09-28", "ccy": "MXN", "subtotal": "560000.00", "tax": "89600.00", "total": "649600.00", "status": "open", "uuid": "SAV-0104"},
    {"invoice_id": "inv_demo_0105", "customer": "cus_demo_001", "project_id": None, "plant_key": "GTO1", "milestone": None, "issue": "2026-09-01", "due": "2026-10-01", "ccy": "MXN", "subtotal": "268855.20", "tax": "43016.83", "total": "311872.03", "status": "open", "uuid": "SAV-0105"},
    {"invoice_id": "inv_demo_0106", "customer": "cus_demo_004", "project_id": "ARG9004", "milestone": "M2", "issue": "2026-06-10", "due": "2026-07-10", "ccy": "USD", "subtotal": "140000.00", "tax": "22400.00", "total": "162400.00", "status": "cancelled", "uuid": "SAV-0106"},
    {"invoice_id": "inv_demo_0107", "customer": "cus_demo_002", "project_id": "ARG9001", "milestone": None, "issue": "2026-04-20", "due": "2026-05-20", "ccy": "MXN", "subtotal": "150000.00", "tax": "24000.00", "total": "174000.00", "status": "open", "uuid": "SAV-0107"},
]
SAVIO_PAYMENTS = [
    {"payment_id": "pay_demo_9001", "invoice_id": "inv_demo_0101", "date": "2026-06-15", "amount": "2519520.00", "ccy": "MXN", "bank_id": "DEMO-TX-0615A"},
    {"payment_id": "pay_demo_9002", "invoice_id": "inv_demo_0102", "date": "2026-08-28", "amount": "2737600.00", "ccy": "MXN", "bank_id": "DEMO-TX-0828A"},
]


def _iso(d):
    return d if isinstance(d, str) else d.isoformat()


def build():
    rnd = random.Random(SEED)
    files = {}
    uuids = {name: _uuid5(name) for name in sorted({c[1] for c in SUPPLIER_CFDIS} | {i["uuid"] for i in SAVIO_INVOICES})}

    # ---------------------------------------------------------- CFDI XML
    for fname, uname, tipo, sidx, project, po, cost, fecha, ccy, sub, iva, tot, extra in SUPPLIER_CFDIS:
        sup = SUPPLIERS[sidx]
        receptor = extra.get("receptor", (OUR_RFC, "ARGIA DEMO MEXICO SA DE CV"))
        kw = dict(uuid=uuids[uname], tipo=tipo, serie=uname.split("-")[1], folio=uname.split("-")[2], fecha=fecha,
                  emisor_rfc=sup["rfc"], emisor_nombre=sup["name"], receptor_rfc=receptor[0], receptor_nombre=receptor[1],
                  moneda=ccy, subtotal=sub, iva=iva, total=tot, tipo_cambio=extra.get("tipo_cambio", "1"),
                  retenido=extra.get("retenido"), relacionados=tuple(uuids[r] for r in extra.get("relacionados", ())),
                  tipo_relacion=extra.get("tipo_relacion", ""), concepto=f"{cost or 'sin PO'} · {project or 'sin proyecto'} · {po or 'sin OC'}")
        if extra.get("pagos"):
            kw["pagos"] = [{"fecha": "2026-08-30T12:00:00", "moneda": "MXN", "monto": "58000.00",
                            "docs": [{"uuid": uuids["ELE-A-1021"], "moneda": "MXN", "n": 1, "ant": "116000.00", "pag": "58000.00", "ins": "58000.00"}]}]
        files[f"cfdi/{fname}"] = cfdi_xml(**kw)
    files["cfdi/README.md"] = ("# Demo supplier CFDIs\n\nSynthetic CFDI 4.0 files (RFCs, UUIDs and names invented). What each one exercises:\n\n"
                               + "\n".join(f"- `{f}` — {d}" for f, d in [
                                   ("01_electro_A1021_inverters_mxn.xml", "ingreso MXN, matches PO-2026-0007 exactly (three-way PASS)"),
                                   ("02_estructuras_B77_structure_mxn.xml", "ingreso MXN, partial billing of PO-2026-0009 (partially received)"),
                                   ("03_modulos_M310_modules_usd.xml", "ingreso USD with TipoCambio, PO in USD"),
                                   ("04_ingenieria_C5_design_retencion.xml", "ingreso with IVA retention (professional services)"),
                                   ("05_electro_NC12_credit_note.xml", "egreso (credit note) related to 01 by TipoRelacion 01"),
                                   ("06_electro_P3_payment_complement.xml", "complemento de pago: 58,000 of 116,000 on 01"),
                                   ("07_estructuras_B90_no_po.xml", "ingreso without a PO -> MISSING_PO exception"),
                                   ("08_electro_A1021_duplicate_file.xml", "the same UUID as 01 in a second file -> must import 0 rows"),
                                   ("09_tampered_total.xml", "Total does not equal subtotal+IVA -> refused (check_totals)"),
                                   ("10_foreign_not_ours.xml", "receptor is not one of our RFCs -> FOREIGN_CFDI exception, never a payable")]) + "\n")

    # ------------------------------------------------------------- Savio
    customers = [{"customer_id": c["savio_customer_id"], "name": c["name"], "rfc": c["rfc"], "email": f"ap@{c['rfc'][:3].lower()}-demo.mx",
                  "payment_terms_days": c["terms_days"], "created_at": "2026-01-15T10:00:00Z"} for c in CUSTOMERS]
    files["savio/customers.json"] = json.dumps({"data": customers, "nextCursor": None}, indent=1, ensure_ascii=False)
    invs = []
    for i, inv in enumerate(SAVIO_INVOICES):
        cf = {"argia_project_id": inv.get("project_id") or "", "argia_plant_key": inv.get("plant_key") or "", "argia_milestone": inv.get("milestone") or ""}
        invs.append({"invoice_id": inv["invoice_id"], "customer_id": inv["customer"], "series": "A", "folio": str(1000 + i),
                     "issue_date": inv["issue"], "due_date": inv["due"], "currency": inv["ccy"],
                     "subtotal": inv["subtotal"], "tax": inv["tax"], "total": inv["total"], "status": inv["status"],
                     "paid_amount": next((p["amount"] for p in SAVIO_PAYMENTS if p["invoice_id"] == inv["invoice_id"]), "0.00"),
                     "updated_at": f"2026-09-0{1 + i % 7}T08:{10 + i:02d}:00Z",
                     "custom_fields": cf,
                     "cfdis": [{"uuid": uuids[inv["uuid"]], "type": "I", "status": "cancelled" if inv["status"] == "cancelled" else "valid",
                                "stamped_at": inv["issue"] + "T12:00:00Z"}],
                     "items": [{"description": f"{inv.get('project_id') or inv.get('plant_key')} · {inv.get('milestone') or 'PPA energy'}",
                                "quantity": 1, "unit_price": inv["subtotal"], "amount": inv["subtotal"]}]})
    invs.sort(key=lambda x: (x["updated_at"], x["invoice_id"]))
    files["savio/invoices_p1.json"] = json.dumps({"data": invs[:4], "nextCursor": "cur_demo_page2"}, indent=1, ensure_ascii=False)
    files["savio/invoices_p2.json"] = json.dumps({"data": invs[4:], "nextCursor": None}, indent=1, ensure_ascii=False)
    pays = [{"payment_id": p["payment_id"], "invoice_id": p["invoice_id"], "customer_id": next(i["customer"] for i in SAVIO_INVOICES if i["invoice_id"] == p["invoice_id"]),
             "date": p["date"], "amount": p["amount"], "currency": p["ccy"], "method": "SPEI", "reference": p["bank_id"], "status": "applied"} for p in SAVIO_PAYMENTS]
    files["savio/payments.json"] = json.dumps({"data": pays, "nextCursor": None}, indent=1, ensure_ascii=False)
    files["savio/webhooks/payment.applied.json"] = json.dumps({"id": "evt_demo_0001", "type": "payment.applied", "created_at": "2026-08-28T16:05:00Z",
                                                                "data": pays[1]}, indent=1)
    files["savio/webhooks/cfdi.canceled.json"] = json.dumps({"id": "evt_demo_0002", "type": "cfdi.canceled", "created_at": "2026-06-12T10:00:00Z",
                                                              "data": {"invoice_id": "inv_demo_0106", "uuid": uuids["SAV-0106"], "reason": "02"}}, indent=1)
    files["savio/webhooks/invoice.status.updated.json"] = json.dumps({"id": "evt_demo_0003", "type": "invoice.status.updated", "created_at": "2026-09-02T09:00:00Z",
                                                                       "data": {"invoice_id": "inv_demo_0102", "status": "partially_paid"}}, indent=1)
    files["savio/README.md"] = ("# Savio mock data\n\nShapes follow https://api.savio.mx/docs (cursor pagination: `data` + `nextCursor`; "
                                "`include=cfdis,items`; custom fields carry the ARGIA project / plant / milestone). Served by "
                                "`server/bundle/savio_mock.py` on 127.0.0.1:8530 and replayed by `argia.fin.savio.FakeTransport` in tests.\n")

    # -------------------------------------------------------------- bank
    def statement(account, clabe, ccy, y, m, opening, lines):
        first = dt.date(y, m, 1)
        last = (dt.date(y + (m // 12), m % 12 + 1, 1) - dt.timedelta(days=1))
        closing = Decimal(opening)
        rows = []
        for d, desc, debit, credit, cp, bid in lines:
            rows.append((d, desc, debit, credit, cp, bid))
            closing += Decimal(credit or "0") - Decimal(debit or "0")
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["opening", f"{Decimal(opening):.2f}"]); w.writerow(["closing", f"{closing:.2f}"]); w.writerow(["period", first.isoformat(), last.isoformat()])
        w.writerow(["date", "description", "debit", "credit", "counterpart", "bank_id"])
        for r in rows:
            w.writerow(r)
        return buf.getvalue(), f"{closing:.2f}"

    bbva_jul, c1 = statement("DEMO-BBVA-MXN", ACCOUNTS[0]["clabe"], "MXN", 2026, 7, "1250000.00", [
        ("2026-07-03", "SPEI RECIBIDO TAIGENE DEMO A1000", "", "951200.00", "TAI050505IJ5", "DEMO-TX-0703A"),   # unknown yet: inv 0103 paid? no — keep as UNMATCHED candidate
        ("2026-07-10", "PAGO NOMINA", "310000.00", "", "", "DEMO-TX-0710A"),
        ("2026-07-15", "COMISION MENSUAL", "450.00", "", "", "DEMO-TX-0715A"),
        ("2026-07-22", "TRASPASO A BANORTE USD", "184200.00", "", ACCOUNTS[1]["clabe"], "DEMO-TX-0722A"),
        ("2026-07-28", "PAGO INGENIERIA DEMO SC", "63200.00", "", "058180000077778888", "DEMO-TX-0728A"),
    ])
    bbva_aug, c2 = statement("DEMO-BBVA-MXN", ACCOUNTS[0]["clabe"], "MXN", 2026, 8, c1, [
        ("2026-08-12", "PAGO NOMINA", "310000.00", "", "", "DEMO-TX-0812A"),
        ("2026-08-15", "COMISION MENSUAL", "450.00", "", "", "DEMO-TX-0815A"),
        ("2026-08-21", "PAGO ESTRUCTURAS DEL BAJIO DEMO B77", "556800.00", "", "021180000033334444", "DEMO-TX-0821A"),
        ("2026-08-28", "SPEI RECIBIDO LOGISTICA DEMO DEL NORTE", "", "2737600.00", "LOG070707MN7", "DEMO-TX-0828A"),
        ("2026-08-30", "PAGO ELECTRO SUMINISTROS DEMO A1021 P1", "58000.00", "", "014180000011112222", "DEMO-TX-0830A"),
        ("2026-08-31", "DEPOSITO NO IDENTIFICADO", "", "12500.00", "", "DEMO-TX-0831A"),
    ])
    files["bank/DEMO-BBVA-MXN/2026-07.csv"] = bbva_jul
    files["bank/DEMO-BBVA-MXN/2026-08.csv"] = bbva_aug
    ban_jul, c3 = statement("DEMO-BANORTE-USD", ACCOUNTS[1]["clabe"], "USD", 2026, 7, "140000.00", [
        ("2026-07-22", "TRASPASO DESDE BBVA MXN", "", "10000.00", ACCOUNTS[0]["clabe"], "DEMO-TX-U0722"),
    ])
    ban_aug, c4 = statement("DEMO-BANORTE-USD", ACCOUNTS[1]["clabe"], "USD", 2026, 8, c3, [
        ("2026-08-08", "WIRE MODULOS SOLARES DEMO M310 50%", "121800.00", "", "044180000055556666", "DEMO-TX-U0808"),
        ("2026-08-15", "WIRE FEE", "35.00", "", "", "DEMO-TX-U0815"),
    ])
    files["bank/DEMO-BANORTE-USD/2026-07.csv"] = ban_jul
    files["bank/DEMO-BANORTE-USD/2026-08.csv"] = ban_aug
    cz_aug, c5 = statement("DEMO-CZ-EUR", ACCOUNTS[2]["clabe"], "EUR", 2026, 8, "18500.00", [
        ("2026-08-04", "PROLOGIS CZ PILOT DEPOSIT", "", "9500.00", "CZ5508000000001234567890", "DEMO-TX-E0804"),
        ("2026-08-20", "OFFICE RENT PRAHA", "1400.00", "", "", "DEMO-TX-E0820"),
    ])
    files["bank/DEMO-CZ-EUR/2026-08.csv"] = cz_aug
    files["bank/README.md"] = ("# Demo bank statements\n\n`argia_generic` format (opening/closing/period header lines, then date, description, debit, credit, "
                               "counterpart, bank_id). Every statement balances to the cent; one month chains into the next. Lines: supplier payments "
                               "matching the CFDIs, a customer receipt matching a Savio payment, an own transfer BBVA -> Banorte (never income/expense), "
                               "fees, payroll, and one unidentified deposit that must stay unreconciled.\n")
    files["bank/closing_balances.json"] = json.dumps({"DEMO-BBVA-MXN": {"2026-07": c1, "2026-08": c2}, "DEMO-BANORTE-USD": {"2026-07": c3, "2026-08": c4},
                                                       "DEMO-CZ-EUR": {"2026-08": c5}}, indent=1)

    # ------------------------------------------------------------- PMO snapshot
    snap = {"generated_at": "2026-09-08T06:00:00-06:00", "engine": "V8.1-demo", "master_sheet": "1DEMO_MASTER", "projects": []}
    for p in PROJECTS:
        ms = [{"ref": r, "name": n, "kind": k, "baseline": b, "planned": pl, "actual": a, "billable": bl, "amount": amt, "depends_on": dep}
              for r, n, k, b, pl, a, bl, amt, dep in MILESTONES[p["project_id"]]]
        tasks = []
        for i, m in enumerate(ms):
            for j in range(2):
                start = dt.date.fromisoformat(m["planned"]) - dt.timedelta(days=20 - 8 * j)
                tasks.append({"task_id": f"{p['project_id']}-T{i + 1}{j + 1}", "name": f"{m['name']} — {'prepare' if j == 0 else 'execute'}",
                              "phase": p["phase"], "start": start.isoformat(), "end": (start + dt.timedelta(days=7)).isoformat(),
                              "progress_pct": 100 if m["actual"] else (60 if j == 0 else 15), "resource": rnd.choice(["crew A", "crew B", "eng. office"])})
        snap["projects"].append({"project_id": p["project_id"], "name": p["name"], "customer": next((c["name"] for c in CUSTOMERS if c["rfc"] == p["customer_rfc"]), "Prologis CZ"),
                                 "site": p["site"], "type": p["project_type"], "phase": p["phase"], "pm": p["pm_user"], "sheet_id": f"1DEMO_{p['project_id']}",
                                 "progress_pct": round(sum(t["progress_pct"] for t in tasks) / len(tasks), 1), "milestones": ms, "tasks": tasks})
    files["pmo/portfolio_snapshot.json"] = json.dumps(snap, indent=1, ensure_ascii=False)
    files["pmo/README.md"] = "# PMO snapshot\n\nWhat `exportPortalSnapshot()` in the PMO V8.1 engine will write to `ARGIA PMO/05_PORTAL/portfolio_snapshot.json`. Schedule from Sheets, money from the portal.\n"

    # ------------------------------------------------------------- Drive tree
    tree = {"root_folder_id": "1DEMO_PROJECTS_ROOT", "projects": []}
    subs = ["01_offer", "02_contract", "03_design", "04_procurement", "05_construction", "06_commissioning", "07_closeout"]
    for p in PROJECTS:
        fid = "1DEMO_F_" + p["project_id"]
        docs = [{"file_id": f"{fid}_c1", "name": f"Contrato {p['project_id']} firmado.pdf", "folder": "02_contract", "mime": "application/pdf", "modified": "2026-03-10T12:00:00Z", "size": 482113, "kind": "contract"},
                {"file_id": f"{fid}_o1", "name": f"{p['offer_ref']} propuesta.pdf", "folder": "01_offer", "mime": "application/pdf", "modified": "2026-02-20T12:00:00Z", "size": 1120033, "kind": "offer"}]
        if p["project_id"] == "ARG9001":
            docs += [{"file_id": f"{fid}_p7", "name": "PO-2026-0007 Electro inversores.pdf", "folder": "04_procurement", "mime": "application/pdf", "modified": "2026-07-30T12:00:00Z", "size": 88012, "kind": "po"},
                     {"file_id": f"{fid}_i1", "name": "A-1021 ELECTRO.xml", "folder": "04_procurement", "mime": "application/xml", "modified": "2026-08-14T12:00:00Z", "size": 6120, "kind": "invoice"}]
        tree["projects"].append({"project_id": p["project_id"], "folder_id": fid, "folder_name": f"{p['project_id']} {p['name']}",
                                 "subfolders": {s: f"{fid}_{s[:2]}" for s in subs}, "files": docs})
    files["drive/project_tree.json"] = json.dumps(tree, indent=1, ensure_ascii=False)

    # ------------------------------------------------------------- master data + world
    world = {"entities": ENTITIES, "accounts": [{k: v for k, v in a.items() if k != "clabe"} | {"clabe": a["clabe"]} for a in ACCOUNTS],
             "cost_codes": [{"code": c, "parent": p, "name_en": en, "name_es": es} for c, p, en, es in COST_CODES],
             "suppliers": SUPPLIERS, "customers": CUSTOMERS, "projects": PROJECTS, "purchase_orders": POS,
             "budgets": {k: [{"version": v, "status": s, "lines": l} for v, s, l in vs] for k, vs in BUDGETS.items()},
             "change_orders": CHANGE_ORDERS, "uuids": uuids, "our_rfcs": [OUR_RFC], "fx": {"USD/MXN": "18.42", "EUR/MXN": "19.95"}}
    files["world.json"] = json.dumps(world, indent=1, ensure_ascii=False)
    files["README.md"] = ("# Finance / PM demo world (v244)\n\nGenerated by `scripts/fin_fixtures.py` (seeded, idempotent; `--check` fails when a committed file drifts). "
                          "One coherent story across every input format: `world.json` (master data, POs, budgets, change orders), `cfdi/` (supplier XMLs), "
                          "`savio/` (customer invoices, payments, webhooks — the mock endpoint serves these), `bank/` (statements), `pmo/` (the Sheets snapshot), "
                          "`drive/` (project folder tree). All names, RFCs, CLABEs and UUIDs are synthetic. The demo seed loads it into PG under the DEMO-* entities.\n")
    return files


def write(files, root=OUT):
    for rel, content in sorted(files.items()):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
    return len(files)


def check(files, root=OUT):
    bad = []
    for rel, content in sorted(files.items()):
        p = root / rel
        if not p.exists() or p.read_text(encoding="utf-8") != content:
            bad.append(rel)
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    files = build()
    if a.check:
        bad = check(files)
        print(f"fin fixtures: {len(files)} files, {len(bad)} drifted" + (": " + ", ".join(bad) if bad else ""))
        return 2 if bad else 0
    print(f"fin fixtures: wrote {write(files)} files under {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
