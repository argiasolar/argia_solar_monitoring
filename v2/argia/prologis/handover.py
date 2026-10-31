"""Handover of each site from Prologis to ARGIA (v338).

Tomasz, 31 Oct 2026: "we are going to take over plants from Prologis under
our software, so we should be able to see where we are in the handover
process in terms of documentation, physical walkthrough, protocols signed,
measurements etc".

* One fixed checklist per site (ITEMS), grouped in five steps (CATEGORIES).
  Each item says who delivers it: Prologis, ARGIA or both.
* Only what people record is stored, one row per site and item
  (handover_items): status, due date, note, who changed it and when. An item
  without a row is "to do". So the checklist can grow without migrations.
* Status: todo, progress, done, blocked, na (not applicable). Progress =
  done / (items - not applicable). Stage of a site: not started (nothing
  recorded beyond "to do"), in progress, handed over (every applicable item
  done).
* Sample rows (sample=1) exist only to show the module before the real
  handover starts; a real edit of an item clears its sample flag, and
  ``clear_sample`` removes the rest. Seeding never overwrites a real row.

Pure functions plus small table functions on the server-only database; the
caller writes the audit row (store.audit).
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Iterable, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS handover_items (
  site_code TEXT NOT NULL, item TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'todo',
  due TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', sample INTEGER NOT NULL DEFAULT 0,
  updated_by TEXT NOT NULL DEFAULT '', updated_utc TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (site_code, item));
"""

# step -> (English, Spanish)
CATEGORIES = [
    ("docs", "Documentation", "Documentación"),
    ("walk", "Physical walkthrough", "Recorrido físico"),
    ("meas", "Measurements and tests", "Mediciones y pruebas"),
    ("data", "Monitoring and data", "Monitoreo y datos"),
    ("sign", "Protocols and sign-off", "Protocolos y firmas"),
]
# who delivers it
WHO = {"prologis": ("Prologis", "Prologis"), "argia": ("ARGIA", "ARGIA"), "both": ("Both", "Ambos")}
# (key, step, who, English, Spanish)
ITEMS = [
    ("docs_asbuilt", "docs", "prologis", "As-built drawings and single-line diagram", "Planos as-built y diagrama unifilar"),
    ("docs_datasheets", "docs", "prologis", "Datasheets: modules, inverters, structure, protection", "Fichas técnicas: módulos, inversores, estructura, protecciones"),
    ("docs_warranties", "docs", "prologis", "Warranty certificates and serial number lists", "Certificados de garantía y listas de números de serie"),
    ("docs_commissioning", "docs", "prologis", "Commissioning and acceptance reports", "Reportes de puesta en marcha y aceptación"),
    ("docs_grid", "docs", "prologis", "Grid connection agreement and permits", "Contrato de interconexión y permisos"),
    ("docs_history", "docs", "prologis", "O&M history: past tickets, reports, open defects", "Historial de O&M: tickets, reportes, defectos abiertos"),
    ("walk_visit", "walk", "both", "Joint site walkthrough done", "Recorrido conjunto del sitio realizado"),
    ("walk_roof", "walk", "argia", "Roof, structure and access condition recorded", "Estado de techo, estructura y accesos registrado"),
    ("walk_equipment", "walk", "argia", "Inverters, combiner boxes and cabling inspected", "Inversores, cajas combinadoras y cableado inspeccionados"),
    ("walk_safety", "walk", "argia", "Safety: access, lifelines, extinguishers, signage", "Seguridad: accesos, líneas de vida, extintores, señalización"),
    ("walk_photos", "walk", "argia", "Photo record of every array and electrical room", "Registro fotográfico de cada arreglo y cuarto eléctrico"),
    ("meas_iv", "meas", "argia", "IV curves of every string", "Curvas IV de cada cadena"),
    ("meas_insulation", "meas", "argia", "Insulation resistance and earthing (IEC 62446-1)", "Resistencia de aislamiento y tierras (IEC 62446-1)"),
    ("meas_thermo", "meas", "argia", "Thermography (IEC 62446-3)", "Termografía (IEC 62446-3)"),
    ("meas_meter", "meas", "both", "Meter reading and baseline energy recorded", "Lectura del medidor y energía base registradas"),
    ("data_access", "data", "prologis", "Read-only access to the inverter and logger portals", "Acceso de solo lectura a los portales de inversores y dataloggers"),
    ("data_live", "data", "argia", "Data flowing to the platform and checked", "Datos llegando a la plataforma y revisados"),
    ("data_register", "data", "argia", "Equipment register loaded with serials", "Registro de equipos cargado con números de serie"),
    ("sign_punch", "sign", "both", "Punch list of defects agreed", "Lista de pendientes y defectos acordada"),
    ("sign_spares", "sign", "prologis", "Spare parts, keys and access cards handed over", "Refacciones, llaves y tarjetas de acceso entregadas"),
    ("sign_protocol", "sign", "both", "Handover protocol signed by both sides", "Acta de entrega firmada por ambas partes"),
]
ITEM_KEYS = [k for k, *_ in ITEMS]
TODO, PROGRESS, DONE, BLOCKED, NA = "todo", "progress", "done", "blocked", "na"
# status -> (English, Spanish, pill class)
STATUSES = {TODO: ("To do", "Pendiente", "s-off"), PROGRESS: ("In progress", "En curso", "s-info"),
            DONE: ("Done", "Hecho", "s-ok"), BLOCKED: ("Blocked", "Bloqueado", "s-bad"), NA: ("Not applicable", "No aplica", "s-off")}
NOT_STARTED, IN_PROGRESS, HANDED_OVER = "not_started", "in_progress", "handed_over"
STAGES = {NOT_STARTED: ("Not started", "Sin iniciar", "s-off"), IN_PROGRESS: ("In progress", "En curso", "s-info"),
          HANDED_OVER: ("Handed over", "Entregado", "s-ok")}
NOTE_MAX = 300
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HandoverError(ValueError):
    pass


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    c.commit()


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def items(c: sqlite3.Connection, site_code: str) -> List[dict]:
    """The full checklist of one site, in template order, merged with what was recorded."""
    rows = {r["item"]: r for r in c.execute("SELECT * FROM handover_items WHERE site_code=?", (site_code,))}
    out = []
    for key, cat, who, en, es in ITEMS:
        r = rows.get(key)
        out.append({"key": key, "cat": cat, "who": who, "en": en, "es": es,
                    "status": r["status"] if r else TODO, "due": r["due"] if r else "", "note": r["note"] if r else "",
                    "sample": bool(r["sample"]) if r else False,
                    "updated_by": r["updated_by"] if r else "", "updated_utc": r["updated_utc"] if r else ""})
    return out


def summary(its: Sequence[dict], today: Optional[dt.date] = None) -> dict:
    """Progress of one site's checklist. Pure."""
    applicable = [i for i in its if i["status"] != NA]
    done = sum(1 for i in applicable if i["status"] == DONE)
    by_cat = {}
    for cat, *_ in CATEGORIES:
        a = [i for i in applicable if i["cat"] == cat]
        by_cat[cat] = (sum(1 for i in a if i["status"] == DONE), len(a))
    overdue = 0
    if today is not None:
        overdue = sum(1 for i in applicable if i["status"] != DONE and i["due"] and i["due"] < today.isoformat())
    if applicable and done == len(applicable):
        stage = HANDED_OVER
    elif all(i["status"] == TODO for i in its if i["status"] != NA):
        stage = NOT_STARTED
    else:
        stage = IN_PROGRESS
    return {"done": done, "total": len(applicable), "pct": (done / len(applicable)) if applicable else 0.0,
            "blocked": sum(1 for i in applicable if i["status"] == BLOCKED), "overdue": overdue,
            "by_cat": by_cat, "stage": stage, "sample": any(i["sample"] for i in its),
            "last": max((i["updated_utc"] for i in its if i["updated_utc"]), default="")}


def set_item(c: sqlite3.Connection, site_code: str, item: str, status: str, due: str, note: str, by: str,
             site_codes: Iterable[str]) -> None:
    """Record one item. A real edit always clears the sample flag."""
    if site_code not in set(site_codes):
        raise HandoverError("unknown site")
    if item not in ITEM_KEYS:
        raise HandoverError("unknown checklist item")
    if status not in STATUSES:
        raise HandoverError("unknown status")
    due = (due or "").strip()
    if due:
        if not _DATE.match(due):
            raise HandoverError("due date must be YYYY-MM-DD")
        dt.date.fromisoformat(due)
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        raise HandoverError(f"note longer than {NOTE_MAX} characters")
    c.execute("INSERT INTO handover_items (site_code, item, status, due, note, sample, updated_by, updated_utc) VALUES (?,?,?,?,?,0,?,?) "
              "ON CONFLICT(site_code, item) DO UPDATE SET status=excluded.status, due=excluded.due, note=excluded.note, sample=0, "
              "updated_by=excluded.updated_by, updated_utc=excluded.updated_utc",
              (site_code, item, status, due, note, by, now_utc()))
    c.commit()


def clear_sample(c: sqlite3.Connection) -> int:
    n = c.execute("DELETE FROM handover_items WHERE sample=1").rowcount
    c.commit()
    return n


def sample_plan(site_codes: Sequence[str], today: dt.date) -> List[tuple]:
    """Deterministic sample rows (site, item, status, due, note) that show every state:
    the first site handed over, the next ones at different points, one blocked item,
    one overdue item, the last third not started. Pure."""
    n = len(site_codes)
    rows = []
    notes = {"docs_grid": ("Waiting for the signed interconnection annex from Prologis legal", BLOCKED),
             "meas_iv": ("2 strings below 90% of the datasheet curve, on the punch list", DONE),
             "walk_roof": ("Ponding near drain 3; photo in the walkthrough report", DONE)}
    for idx, code in enumerate(site_codes):
        if idx >= max(1, (2 * n) // 3):
            break                                              # the last third: not started
        share = max(0.1, 1.0 - 0.07 * idx)                    # 100%, 93%, 86% ... down to 10%
        cut = round(share * len(ITEMS))
        for j, (key, *_rest) in enumerate(ITEMS):
            if j < cut:
                status, note = DONE, ""
                if key in notes and idx in (1, 2):
                    note, status = notes[key]
            elif j < cut + 2:
                status, note = PROGRESS, ""
            else:
                continue
            due = ""
            if status == BLOCKED:
                due = (today + dt.timedelta(days=10)).isoformat()
            elif status != DONE:
                due = (today + dt.timedelta(days=7 * (j - cut + 1) + idx)).isoformat()
                if idx == 2 and j == cut:
                    due = (today - dt.timedelta(days=5)).isoformat()   # one overdue item
            rows.append((code, key, status, due, note))
    return rows


def seed_sample(c: sqlite3.Connection, site_codes: Sequence[str], today: dt.date, by: str = "argia-sample") -> int:
    """Write the sample plan where nothing is recorded yet. Returns rows written."""
    n = 0
    for code, key, status, due, note in sample_plan(site_codes, today):
        cur = c.execute("INSERT OR IGNORE INTO handover_items (site_code, item, status, due, note, sample, updated_by, updated_utc) "
                        "VALUES (?,?,?,?,?,1,?,?)", (code, key, status, due, note, by, now_utc()))
        n += cur.rowcount
    c.commit()
    return n
