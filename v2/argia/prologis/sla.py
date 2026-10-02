"""Response-time classes of the Prologis O&M MSA (Schedule A) (v292).

Every ticket carries one class; the class fixes the response deadline.
Per the MSA, "Response Time" = detect and understand the fault, report it
in writing and dispatch where needed. The clock starts at the earliest of
the DAS alarm / Owner notice; where dispatch approval is required it
starts on receipt of that approval (not for emergencies and outages
>500 kW). Owner/tenant access delays are excluded (the ticket's WAITING
status stops the clock - see ``elapsed_hours``).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class SlaClass:
    code: str
    en: str
    es: str
    hours: Optional[float]      # None = "next site visit"
    approval_needed: bool       # does the clock wait for dispatch approval?
    priority: str               # P1..P4 colour band


CLASSES: List[SlaClass] = [
    SlaClass("EMERGENCY", "Emergency, disaster or safety issue - immediate",
             "Emergencia, desastre o seguridad - inmediata", 0.0, False, "P1"),
    SlaClass("OUT_500", "Outage > 500 kW - 24 h", "Falla > 500 kW - 24 h", 24.0, False, "P1"),
    SlaClass("OUT_100_500", "Outage 100-500 kW - 48 h", "Falla 100-500 kW - 48 h", 48.0, True, "P2"),
    SlaClass("COMM_1", "Communication loss 1 (operation not confirmed) - 48 h",
             "Pérdida de comunicación 1 (operación no confirmada) - 48 h", 48.0, True, "P2"),
    SlaClass("STRING_100", "String inverter outage <= 100 kW - 7 days",
             "Falla de inversor string <= 100 kW - 7 días", 168.0, True, "P3"),
    SlaClass("COMM_2", "Communication loss 2 (operating, billing meter fine) - 7 days",
             "Pérdida de comunicación 2 (operando, medidor de facturación bien) - 7 días", 168.0, True, "P3"),
    SlaClass("STRING_25", "String inverter outage <= 25 kW - 10 days",
             "Falla de inversor string <= 25 kW - 10 días", 240.0, True, "P3"),
    SlaClass("MICRO", "Micro-inverter outage <= 1 kW - next site visit",
             "Falla de microinversor <= 1 kW - próxima visita", None, True, "P4"),
    SlaClass("OTHER", "Other non-performance issue - next site visit",
             "Otro asunto sin impacto - próxima visita", None, True, "P4"),
]
BY_CODE: Dict[str, SlaClass] = {c.code: c for c in CLASSES}


def suggest(kw_lost: Optional[float], comm_loss: bool = False,
            operation_confirmed: bool = False, safety: bool = False) -> str:
    """The MSA class for an event (pure). kW lost decides the outage band."""
    if safety:
        return "EMERGENCY"
    if comm_loss:
        return "COMM_2" if operation_confirmed else "COMM_1"
    if kw_lost is None or kw_lost <= 0:
        return "OTHER"
    if kw_lost > 500:
        return "OUT_500"
    if kw_lost > 100:
        return "OUT_100_500"
    if kw_lost > 25:
        return "STRING_100"
    if kw_lost > 1:
        return "STRING_25"
    return "MICRO"


def clock_start(cls: SlaClass, detected: dt.datetime,
                approved: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """When the response clock starts. None = waiting for approval."""
    if not cls.approval_needed:
        return detected
    if approved is None:
        return None
    return max(detected, approved)


def elapsed_hours(start: dt.datetime, now: dt.datetime,
                  paused: Iterable[Tuple[dt.datetime, Optional[dt.datetime]]] = ()) -> float:
    """Hours on the clock from ``start`` to ``now`` minus excluded spans
    (WAITING on the Owner / tenant access). Open spans end at ``now``."""
    total = max(0.0, (now - start).total_seconds())
    for a, b in paused:
        b = now if b is None else b
        lo, hi = max(a, start), min(b, now)
        if hi > lo:
            total -= (hi - lo).total_seconds()
    return max(0.0, total) / 3600.0


def status(cls: SlaClass, hours_used: Optional[float], responded: bool) -> str:
    """'met' | 'breached' | 'running' | 'due' (no deadline) | 'not_started'."""
    if hours_used is None:
        return "not_started"
    if cls.hours is None:
        return "due"
    if responded:
        return "met" if hours_used <= max(cls.hours, 1.0) else "breached"
    return "breached" if hours_used > max(cls.hours, 1.0) else "running"


def compliance(results: Sequence[str]) -> Optional[float]:
    """Share of closed clocks that met the deadline (None when none closed)."""
    closed = [r for r in results if r in ("met", "breached")]
    if not closed:
        return None
    return sum(r == "met" for r in closed) / len(closed)
