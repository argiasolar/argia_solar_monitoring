"""Scenario 16 (supplier invoice import), 40 (taxes), 46 (cancelled
CFDI), 47 (payment complements), 62/67 (UUID once) — CFDI 4.0 parsing."""
from __future__ import annotations

from decimal import Decimal

import pytest

from argia.fin import cfdi

INGRESO = """<?xml version="1.0" encoding="utf-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
 Version="4.0" Serie="A" Folio="1021" Fecha="2026-08-14T10:22:31" SubTotal="100000.00" Descuento="0.00"
 Moneda="MXN" Total="116000.00" TipoDeComprobante="I" MetodoPago="PPD" FormaPago="99" LugarExpedicion="37138">
  <cfdi:Emisor Rfc="ELE010101AB1" Nombre="ELECTRO SUMINISTROS SA DE CV" RegimenFiscal="601"/>
  <cfdi:Receptor Rfc="ARG150101XY2" Nombre="ARGIA MEXICO SA DE CV" UsoCFDI="G03" DomicilioFiscalReceptor="37138" RegimenFiscalReceptor="601"/>
  <cfdi:Conceptos><cfdi:Concepto ClaveProdServ="26111700" Cantidad="10" ClaveUnidad="H87" Descripcion="Inversor 75 kW" ValorUnitario="10000.00" Importe="100000.00" ObjetoImp="02"/></cfdi:Conceptos>
  <cfdi:Impuestos TotalImpuestosTrasladados="16000.00">
    <cfdi:Traslados><cfdi:Traslado Base="100000.00" Impuesto="002" TipoFactor="Tasa" TasaOCuota="0.160000" Importe="16000.00"/></cfdi:Traslados>
  </cfdi:Impuestos>
  <cfdi:Complemento>
    <tfd:TimbreFiscalDigital Version="1.1" UUID="6f1a2b3c-4d5e-4f60-8a71-9b8c7d6e5f40" FechaTimbrado="2026-08-14T10:23:02" SelloCFD="x" NoCertificadoSAT="1" SelloSAT="y"/>
  </cfdi:Complemento>
</cfdi:Comprobante>"""

RETENCION = INGRESO.replace('Total="116000.00"', 'Total="105333.33"').replace(
    '<cfdi:Impuestos TotalImpuestosTrasladados="16000.00">',
    '<cfdi:Impuestos TotalImpuestosTrasladados="16000.00" TotalImpuestosRetenidos="10666.67">').replace(
    "6f1a2b3c-4d5e-4f60-8a71-9b8c7d6e5f40", "aaaaaaaa-0000-4000-8000-000000000001")

EGRESO = INGRESO.replace('TipoDeComprobante="I"', 'TipoDeComprobante="E"').replace(
    "<cfdi:Emisor", '<cfdi:CfdiRelacionados TipoRelacion="01"><cfdi:CfdiRelacionado UUID="6f1a2b3c-4d5e-4f60-8a71-9b8c7d6e5f40"/></cfdi:CfdiRelacionados><cfdi:Emisor', 1).replace(
    "6f1a2b3c-4d5e-4f60-8a71-9b8c7d6e5f40\" FechaTimbrado", "bbbbbbbb-0000-4000-8000-000000000002\" FechaTimbrado")

PAGO = """<?xml version="1.0" encoding="utf-8"?>
<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" xmlns:pago20="http://www.sat.gob.mx/Pagos20"
 Version="4.0" Fecha="2026-09-01T09:00:00" SubTotal="0" Moneda="XXX" Total="0" TipoDeComprobante="P" LugarExpedicion="37138">
  <cfdi:Emisor Rfc="ELE010101AB1" Nombre="ELECTRO" RegimenFiscal="601"/>
  <cfdi:Receptor Rfc="ARG150101XY2" Nombre="ARGIA" UsoCFDI="CP01" DomicilioFiscalReceptor="37138" RegimenFiscalReceptor="601"/>
  <cfdi:Conceptos><cfdi:Concepto ClaveProdServ="84111506" Cantidad="1" ClaveUnidad="ACT" Descripcion="Pago" ValorUnitario="0" Importe="0" ObjetoImp="01"/></cfdi:Conceptos>
  <cfdi:Complemento>
    <pago20:Pagos Version="2.0">
      <pago20:Pago FechaPago="2026-08-30T12:00:00" FormaDePagoP="03" MonedaP="MXN" Monto="58000.00">
        <pago20:DoctoRelacionado IdDocumento="6f1a2b3c-4d5e-4f60-8a71-9b8c7d6e5f40" MonedaDR="MXN" NumParcialidad="1" ImpSaldoAnt="116000.00" ImpPagado="58000.00" ImpSaldoInsoluto="58000.00" ObjetoImpDR="02"/>
      </pago20:Pago>
    </pago20:Pagos>
    <tfd:TimbreFiscalDigital Version="1.1" UUID="cccccccc-0000-4000-8000-000000000003" FechaTimbrado="2026-09-01T09:01:00" SelloCFD="x" NoCertificadoSAT="1" SelloSAT="y"/>
  </cfdi:Complemento>
</cfdi:Comprobante>"""


class TestParse:
    def test_ingreso_facts(self):
        c = cfdi.parse(INGRESO)
        assert c.uuid == "6F1A2B3C-4D5E-4F60-8A71-9B8C7D6E5F40"
        assert c.tipo == "I" and c.kind == "ingreso"
        assert c.emisor_rfc == "ELE010101AB1" and c.receptor_rfc == "ARG150101XY2"
        assert c.subtotal == Decimal("100000.00") and c.traslados == Decimal("16000.00")
        assert c.retenciones == Decimal("0.00") and c.total == Decimal("116000.00")
        assert c.moneda == "MXN" and c.tipo_cambio == Decimal("1.00") and c.metodo_pago == "PPD"
        assert cfdi.check_totals(c) == []
        assert cfdi.natural_key(c) == c.uuid

    def test_retention_arithmetic(self):
        c = cfdi.parse(RETENCION)
        assert c.retenciones == Decimal("10666.67")
        assert cfdi.check_totals(c) == []

    def test_tampered_total_is_caught(self):
        c = cfdi.parse(INGRESO.replace('Total="116000.00"', 'Total="115000.00"'))
        bad = cfdi.check_totals(c)
        assert len(bad) == 1 and "total 115000.00 !=" in bad[0]

    def test_unstamped_is_not_an_invoice(self):
        with pytest.raises(cfdi.CfdiError, match="unstamped"):
            cfdi.parse(INGRESO.replace('UUID="6f1a2b3c-4d5e-4f60-8a71-9b8c7d6e5f40"', 'UUID=""'))

    def test_not_xml(self):
        with pytest.raises(cfdi.CfdiError):
            cfdi.parse("hello")

    def test_credit_note_links_its_original(self):
        c = cfdi.parse(EGRESO)
        assert c.tipo == "E" and c.relacionados == ["6F1A2B3C-4D5E-4F60-8A71-9B8C7D6E5F40"]
        assert c.tipo_relacion == "01"

    def test_payment_complement(self):
        c = cfdi.parse(PAGO)
        assert c.tipo == "P" and c.total == 0 and c.monto_pago == Decimal("58000.00")
        assert len(c.pagos) == 1
        p = c.pagos[0]
        assert p.uuid == "6F1A2B3C-4D5E-4F60-8A71-9B8C7D6E5F40" and p.parcialidad == 1
        assert p.saldo_anterior == Decimal("116000.00") and p.importe_pagado == Decimal("58000.00") and p.saldo_insoluto == Decimal("58000.00")
        assert cfdi.check_totals(c) == []

    def test_payment_complement_arithmetic_checked(self):
        c = cfdi.parse(PAGO.replace('ImpSaldoInsoluto="58000.00"', 'ImpSaldoInsoluto="50000.00"'))
        assert any("insoluto" in b for b in cfdi.check_totals(c))

    def test_direction(self):
        c = cfdi.parse(INGRESO)
        assert cfdi.direction(c, ["ARG150101XY2"]) == "supplier"
        assert cfdi.direction(c, ["ELE010101AB1"]) == "customer"
        assert cfdi.direction(c, ["OTHER"]) == "foreign"
        assert cfdi.direction(c, ["ARG150101XY2", "ELE010101AB1"]) == "internal"

    def test_summary_is_json_safe(self):
        import json
        s = cfdi.summary(cfdi.parse(PAGO))
        json.dumps(s)
        assert s["pagos"] == [("6F1A2B3C-4D5E-4F60-8A71-9B8C7D6E5F40", "58000.00")]

    def test_same_file_twice_is_one_key(self):
        a, b = cfdi.parse(INGRESO), cfdi.parse(INGRESO.replace("\n", "\r\n"))
        assert cfdi.natural_key(a) == cfdi.natural_key(b)
