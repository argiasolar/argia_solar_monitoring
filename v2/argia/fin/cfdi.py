"""CFDI 4.0 XML -> the facts the ledger needs (scenario 16, 40, 46, 47).

The XML is the primary source: UUID from the TimbreFiscalDigital,
emisor/receptor RFC, amounts, currency, type (I ingreso / E egreso /
P pago / N nómina / T traslado), related CFDIs (a credit note's
original, a payment complement's invoices). Pure; no network, no
schema validation beyond what the ledger relies on.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from argia.fin.money import D, q, same

NS = {
    "cfdi": "http://www.sat.gob.mx/cfd/4",
    "cfdi3": "http://www.sat.gob.mx/cfd/3",
    "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital",
    "pago20": "http://www.sat.gob.mx/Pagos20",
    "pago10": "http://www.sat.gob.mx/Pagos",
}

TIPOS = {"I": "ingreso", "E": "egreso", "P": "pago", "N": "nomina", "T": "traslado"}


@dataclass(frozen=True)
class PaymentDoc:
    """One DoctoRelacionado inside a complemento de pago."""
    uuid: str
    parcialidad: int
    saldo_anterior: Decimal
    importe_pagado: Decimal
    saldo_insoluto: Decimal
    moneda: str


@dataclass(frozen=True)
class Cfdi:
    uuid: str
    version: str
    tipo: str                    # I / E / P / N / T
    serie: str
    folio: str
    fecha: str                   # ISO as issued (local time of the emisor)
    emisor_rfc: str
    emisor_nombre: str
    receptor_rfc: str
    receptor_nombre: str
    moneda: str
    tipo_cambio: Decimal
    subtotal: Decimal
    descuento: Decimal
    traslados: Decimal           # IVA etc. charged
    retenciones: Decimal         # IVA/ISR withheld
    total: Decimal
    metodo_pago: str             # PUE / PPD
    forma_pago: str
    uso_cfdi: str
    relacionados: List[str] = field(default_factory=list)   # UUIDs of related CFDIs
    tipo_relacion: str = ""
    pagos: List[PaymentDoc] = field(default_factory=list)   # P only
    fecha_pago: str = ""
    monto_pago: Decimal = Decimal("0.00")

    @property
    def kind(self) -> str:
        return TIPOS.get(self.tipo, self.tipo)


class CfdiError(ValueError):
    pass


def _root(xml_text: str) -> ET.Element:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise CfdiError(f"not XML: {e}") from e
    tag = root.tag
    if not (tag.endswith("}Comprobante")):
        raise CfdiError(f"not a CFDI Comprobante: {tag}")
    return root


def _ns_of(root: ET.Element) -> str:
    return root.tag.split("}")[0].strip("{")


def parse(xml_text: str) -> Cfdi:
    """The facts of one CFDI. Raises CfdiError when the document has no
    timbre (an unstamped draft is not an invoice)."""
    root = _root(xml_text)
    ns = _ns_of(root)
    c = {"c": ns, "tfd": NS["tfd"], "p20": NS["pago20"], "p10": NS["pago10"]}
    a = root.attrib
    timbre = root.find(".//tfd:TimbreFiscalDigital", c)
    if timbre is None or not timbre.attrib.get("UUID"):
        raise CfdiError("no TimbreFiscalDigital — unstamped document")
    uuid = timbre.attrib["UUID"].strip().upper()
    emisor = root.find("c:Emisor", c)
    receptor = root.find("c:Receptor", c)
    if emisor is None or receptor is None:
        raise CfdiError("missing Emisor/Receptor")
    traslados = Decimal("0.00")
    retenciones = Decimal("0.00")
    imp = root.find("c:Impuestos", c)
    if imp is not None:
        traslados = D(imp.attrib.get("TotalImpuestosTrasladados", "0"))
        retenciones = D(imp.attrib.get("TotalImpuestosRetenidos", "0"))
    rel_uuids: List[str] = []
    tipo_rel = ""
    for r in root.findall("c:CfdiRelacionados", c):
        tipo_rel = tipo_rel or r.attrib.get("TipoRelacion", "")
        for x in r.findall("c:CfdiRelacionado", c):
            u = x.attrib.get("UUID", "").strip().upper()
            if u:
                rel_uuids.append(u)
    pagos: List[PaymentDoc] = []
    fecha_pago = ""
    monto_pago = Decimal("0.00")
    tipo = a.get("TipoDeComprobante", "").strip().upper()
    if tipo == "P":
        for pfx in ("p20", "p10"):
            for pago in root.findall(f".//{pfx}:Pago", c):
                fecha_pago = fecha_pago or pago.attrib.get("FechaPago", "")
                monto_pago += D(pago.attrib.get("Monto", "0"))
                for d in pago.findall(f"{pfx}:DoctoRelacionado", c):
                    pagos.append(PaymentDoc(
                        uuid=d.attrib.get("IdDocumento", "").strip().upper(),
                        parcialidad=int(d.attrib.get("NumParcialidad", "1") or 1),
                        saldo_anterior=D(d.attrib.get("ImpSaldoAnt", "0")),
                        importe_pagado=D(d.attrib.get("ImpPagado", "0")),
                        saldo_insoluto=D(d.attrib.get("ImpSaldoInsoluto", "0")),
                        moneda=d.attrib.get("MonedaDR", ""),
                    ))
            if pagos:
                break
    return Cfdi(
        uuid=uuid,
        version=a.get("Version", a.get("version", "")),
        tipo=tipo,
        serie=a.get("Serie", ""),
        folio=a.get("Folio", ""),
        fecha=a.get("Fecha", ""),
        emisor_rfc=emisor.attrib.get("Rfc", "").strip().upper(),
        emisor_nombre=emisor.attrib.get("Nombre", ""),
        receptor_rfc=receptor.attrib.get("Rfc", "").strip().upper(),
        receptor_nombre=receptor.attrib.get("Nombre", ""),
        moneda=a.get("Moneda", "MXN"),
        tipo_cambio=D(a.get("TipoCambio", "1") or "1"),
        subtotal=D(a.get("SubTotal", "0")),
        descuento=D(a.get("Descuento", "0")),
        traslados=traslados,
        retenciones=retenciones,
        total=D(a.get("Total", "0")),
        metodo_pago=a.get("MetodoPago", ""),
        forma_pago=a.get("FormaPago", ""),
        uso_cfdi=receptor.attrib.get("UsoCFDI", ""),
        relacionados=rel_uuids,
        tipo_relacion=tipo_rel,
        pagos=pagos,
        fecha_pago=fecha_pago,
        monto_pago=q(monto_pago),
    )


def check_totals(c: Cfdi) -> List[str]:
    """The arithmetic the SAT enforces, re-checked here so a tampered or
    truncated file never enters the ledger (scenario 40). Empty = sane."""
    out: List[str] = []
    if c.tipo in ("I", "E"):
        expect = q(c.subtotal - c.descuento + c.traslados - c.retenciones)
        if not same(expect, c.total):
            out.append(f"total {c.total} != subtotal {c.subtotal} - descuento {c.descuento}"
                       f" + traslados {c.traslados} - retenciones {c.retenciones} = {expect}")
        if c.total < 0:
            out.append("negative total")
    if c.tipo == "P":
        if c.total != 0:
            out.append("a payment complement must carry Total 0")
        paid = q(sum((p.importe_pagado for p in c.pagos), Decimal("0")))
        if c.pagos and not same(paid, c.monto_pago):
            out.append(f"Σ ImpPagado {paid} != Monto {c.monto_pago}")
        for p in c.pagos:
            if not same(p.saldo_anterior - p.importe_pagado, p.saldo_insoluto):
                out.append(f"{p.uuid}: saldo anterior {p.saldo_anterior} - pagado {p.importe_pagado}"
                           f" != insoluto {p.saldo_insoluto}")
    if not c.emisor_rfc or not c.receptor_rfc:
        out.append("missing RFC")
    return out


def direction(c: Cfdi, our_rfcs: List[str]) -> str:
    """'supplier' when one of OUR entities is the receptor, 'customer'
    when we are the emisor, 'foreign' when neither (a file that landed in
    the wrong inbox must not become a payable)."""
    ours = {r.strip().upper() for r in our_rfcs}
    if c.receptor_rfc in ours and c.emisor_rfc not in ours:
        return "supplier"
    if c.emisor_rfc in ours and c.receptor_rfc not in ours:
        return "customer"
    if c.emisor_rfc in ours and c.receptor_rfc in ours:
        return "internal"
    return "foreign"


def natural_key(c: Cfdi) -> str:
    """The one key that makes any import idempotent (scenario 62/67)."""
    return c.uuid


def summary(c: Cfdi) -> Dict[str, object]:
    return {
        "uuid": c.uuid, "tipo": c.tipo, "kind": c.kind, "fecha": c.fecha,
        "emisor_rfc": c.emisor_rfc, "receptor_rfc": c.receptor_rfc,
        "moneda": c.moneda, "tipo_cambio": str(c.tipo_cambio),
        "subtotal": str(c.subtotal), "traslados": str(c.traslados),
        "retenciones": str(c.retenciones), "total": str(c.total),
        "metodo_pago": c.metodo_pago, "relacionados": list(c.relacionados),
        "pagos": [(p.uuid, str(p.importe_pagado)) for p in c.pagos],
    }
