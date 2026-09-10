"""v248 — cost centres: where the money goes when it is not a project.

The accountants already give every póliza line a *segment*: the 4-digit
business case for project money, and a handful of internal codes for the
rest (701 OPERATION COSTS, 702/703/749 offices, 706 PAYROLL, a code per
person paid on fees, 1195 the León warehouse, 888 training, 1100/1387
warranties…). The catalogue here turns that list into something a reader
can group: every code gets a *kind* and, for the people, one group —
SALARIES & FEES — so the cost page shows one line for payroll instead of
twenty names (Tomasz, 2026-09-10).

Pure: ``classify`` decides from the accountants' project list, ``canon``
normalises names to ONE case (the tracker, the overview and the books
each spell the same thing differently), ``label`` is the one way a
project or cost centre is written on every page: code first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

PROJECT, OVERHEAD, PAYROLL, WARRANTY, OTHER = 'project', 'overhead', 'payroll', 'warranty', 'other'
KINDS = (PROJECT, OVERHEAD, PAYROLL, WARRANTY, OTHER)
SALARIES = 'SALARIES'                         # the group every payroll code rolls into

KIND_LABEL = {PROJECT: ('Project', 'Proyecto'), OVERHEAD: ('Overhead', 'Gastos generales'), PAYROLL: ('Salaries & fees', 'Sueldos y honorarios'),
              WARRANTY: ('Warranties', 'Garantías'), OTHER: ('Other', 'Otros')}

_OVERHEAD_STRONG = re.compile(r'ARGIA SOLAR|ARGIA HOLDING|OPERATION COST|WAREH|ALMACEN|TRAINING|SEMINAR|CAPACITACION|PROLOGIS GENERAL', re.I)
_OVERHEAD = re.compile(r'\bOFFICE\b|OFICINA|\bGENERAL\b|HOLDING', re.I)
_PROJECT = re.compile(r'\b(SOLAR|PPA|CAPEX|LAAS|LIGHTING|ROOF|LAND|CARPORT|PROLOGIS|EPC|BESS)\b|KWP|KVA', re.I)
_WARRANTY = re.compile(r'GARANTIA|WARRANT', re.I)
_PAYROLL = re.compile(r'PAYROLL|NOMINA|SALAR|SUELDO', re.I)
_BUSINESS = re.compile(r'\d|\b(SOLAR|PPA|CAPEX|LAAS|LIGHTING|ROOF|LAND|PROLOGIS|OFFICE|COSTS|PANEL|PANELES|LED|EPC|SA|CV|SRO|SRL|GROUP|MEXICO|EXPERTS|SMART|METERING|INC|LLC|LTD|GARANTIAS|GUADALAJARA|MONTERREY|QUERETARO|TIJUANA|JUAREZ|REYNOSA|SALTILLO|LEON|SILAO|APODACA|TOLUCA|SLP|CDMX|MTY|GDL|BJX|EDOMEX|JALISCO|NAYARIT|OAX|GTO|CPA|HOLDING)\b|WAREH|KWP', re.I)
_UNITS = {'KWP': 'kWp', 'KW': 'kW', 'MWP': 'MWp', 'MW': 'MW', 'KWH': 'kWh', 'MWH': 'MWh', 'KVA': 'kVA', 'MVA': 'MVA', 'KV': 'kV', 'HP': 'HP'}
_FIXES = {'LEON WAREHPUSE': 'LEON WAREHOUSE', 'PROLOGIES': 'PROLOGIS', 'QUIMCA': 'QUIMICA', 'MTERING': 'METERING', 'DAVID CASTO': 'DAVID CASTRO'}


def canon(name: Optional[str]) -> str:
    """ONE case for every name on the portal: upper-case, single spaces,
    units kept readable (605.5 kWp, 500 kVA), the known typos fixed."""
    s = re.sub(r'\s+', ' ', str(name or '')).strip().upper()
    for bad, good in _FIXES.items():
        s = s.replace(bad, good)
    return re.sub(r'\b(\d[\d.,]*)\s?(KWP|KWH|MWP|MWH|KVA|MVA|KW|MW|KV)\b', lambda m: f'{m.group(1)} {_UNITS[m.group(2)]}', s)


def is_person(name: str) -> bool:
    """Two to four capitalised words, no digits, no business vocabulary."""
    s = canon(name)
    words = [w for w in re.split(r'[ ]+', s) if w]
    if not 2 <= len(words) <= 5 or _BUSINESS.search(s):
        return False
    return all(re.fullmatch(r"[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ'\-]+", w) or w in ('DE', 'DEL', 'LA', 'LOS', 'Y') for w in words)


def classify(code: int, name: str, project_type: str = '') -> str:
    pt = (project_type or '').upper()
    n = canon(name)
    if pt in ('GM', 'LAAS'):
        return PROJECT
    if not n or n.startswith('NOT USED'):
        return OTHER
    if pt == 'PL_080' or _PAYROLL.search(n):
        return PAYROLL
    if _WARRANTY.search(n):
        return WARRANTY
    if _OVERHEAD_STRONG.search(n):
        return OVERHEAD
    if is_person(n):
        return PAYROLL
    if _PROJECT.search(n):
        return PROJECT
    if _OVERHEAD.search(n):
        return OVERHEAD
    return PROJECT if code >= 1000 else OTHER


@dataclass
class CostCenter:
    code: int
    name: str            # canonical (upper case)
    kind: str
    grp: str             # SALARIES for payroll, else '' (the code stands alone)
    manual: bool = False

    @property
    def is_project(self) -> bool:
        return self.kind == PROJECT


def build(projects: Iterable, existing: Optional[Dict[int, CostCenter]] = None, project_codes: Iterable[int] = ()) -> List[CostCenter]:
    """From the accountants' project list (``acctbook.ProjectCode`` or dicts
    with code/name/project_type). A code that is in the projects overview
    (``project_codes``) is a project whatever its name looks like; a row
    marked manual in ``existing`` keeps its kind and group; names always
    follow the accountants."""
    known = {int(c) for c in project_codes}
    out: List[CostCenter] = []
    for p in projects:
        code = int(p['code'] if isinstance(p, dict) else p.code)
        name = p['name'] if isinstance(p, dict) else p.name
        pt = p.get('project_type', '') if isinstance(p, dict) else getattr(p, 'project_type', '')
        n = canon(name)
        if not n or n.startswith('NOT USED'):
            continue
        old = (existing or {}).get(code)
        if old and old.manual:
            out.append(CostCenter(code, n, old.kind, old.grp, True))
            continue
        kind = PROJECT if code in known else classify(code, n, pt)
        out.append(CostCenter(code, n, kind, SALARIES if kind == PAYROLL else ''))
    return out


def label(code, name: Optional[str] = None, kind: str = PROJECT) -> str:
    """The one way a project or cost centre is written: 'ARG1350 · SOLAR PPA
    ROOF 605.5 kWp TAIGENE GUANAJUATO' for a project, '701 · OPERATION
    COSTS' for a cost centre. ``code`` may be None (tracker rows without one)."""
    n = canon(name)
    if code in (None, ''):
        return n or '—'
    c = int(code)
    head = f'ARG{c:04d}' if kind == PROJECT and c >= 1000 else str(c)   # the ARGnnnn folders start at 1000; older codes stay bare
    return f'{head} · {n}' if n else head


def href(code, kind: str = PROJECT) -> str:
    c = int(code)
    return f'/projects/ARG{c:04d}/' if kind == PROJECT else f'/finance/costs/{c}/'
