"""Display names for every outgoing communication (v217).

Tomasz, 2026-09-06: "I do not want to see code names like NL2 in any
communication — we have names for that; the code name can be a
detailed description only." The engine, the ledger and the alert keys
keep their plant codes and serial numbers (they are the stable ids);
this module is the rendering layer every mail and push message goes
through on its way out:

* plant: ``Budenheim`` first, ``Budenheim (NL2)`` where the code is a
  useful detail;
* inverter: ``Inverter 3`` (the label the vendor portal shows and an
  engineer can find on site), ``Inverter 3 · SN JJM4D4P017`` in detail;
* message text: the composers write ``NL2 JJM4D4P017: ...`` — the
  leading code prefix is dropped (the header already names the unit)
  and any other code mention is replaced by its name;
* metric / key phrases: ``string_fault`` -> "new string diagnostic
  flag" — one table for the alert mails and the daily mail.

Everything here is pure except ``load_names()`` (PostgreSQL, {} on
error so a database hiccup degrades to codes, never to no mail).
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, Optional, Tuple

METRIC_PHRASE: Dict[str, str] = {
    "inverter_temp_high": "inverter running hot",
    "inverter_fault": "inverter reports a fault code",
    "inverter_relative": "inverter below its peers",
    "inverter_silent": "inverter silent while the plant produces",
    "string_fault": "new string diagnostic flag",
    "energy_daily_pct": "production far below expected",
    "plant_offline": "plant produced nothing",
    "plant_twin_yield": "below its twin plant",
    "data_stale": "telemetry gap",
    "daily_digest": "daily digest",
    "plant-dark": "no telemetry today",
    "plant-stale": "telemetry stale",
    "inverter-silent": "inverter silent",
    "recon-fail": "reconciliation FAIL",
    "satellite-drift": "irradiance sensor drift suspected",
    "unit-failed": "scheduled job failed",
    "disk-full": "server disk nearly full",
    "postgres-down": "PostgreSQL unreachable",
    "cfe-heartbeat": "CFE tariff fetcher heartbeat stale",
    "cfe-probe": "CFE tariff probe warning",
    "cfe-reject": "CFE tariff CSV rejected",
    "cfe-coverage": "CFE tariff coverage gap",
}


def phrase(metric_or_prefix: str) -> str:
    """'string_fault' -> 'new string diagnostic flag'; unknown -> itself
    with underscores/dashes as spaces (never a KeyError in a mail)."""
    m = (metric_or_prefix or "").strip()
    return METRIC_PHRASE.get(m) or m.replace("_", " ").replace("-", " ")


def short_customer(name: str) -> str:
    """'PLASTIC OMNIUM PPA land (Monterrey, NL)' -> 'Plastic Omnium';
    'SAG PPA roof (CDMX, MEX)' -> 'SAG'; 'HIRSCHMANN-MEXICO (San Miguel,
    GTO)' -> 'Hirschmann-Mexico'. Pure."""
    head = re.split(r",|\(|\s+(?:PPA|CAPEX|LaaS|roof|land)\b", name or "", 1)[0].strip(" ,·")
    # acronym customers (SAG, SMS) stay upper; long all-caps names get title case
    return head.title() if head.isupper() and len(head) > 4 else head


class Names:
    """Plant and inverter display names. ``plants``: {plant_key: name};
    ``inverters``: {(plant_key, serial): label}. Unknown keys render as
    themselves, so a partial table never hides information."""

    def __init__(self, plants: Optional[Dict[str, str]] = None,
                 inverters: Optional[Dict[Tuple[str, str], str]] = None) -> None:
        self.plants = {k.upper(): v for k, v in (plants or {}).items() if k and v}
        self.plants.setdefault("PORTFOLIO", "Portfolio")     # the engine's fleet digest row
        self.inverters = {(k.upper(), sn.strip()): v for (k, sn), v in (inverters or {}).items()
                          if k and sn and v and v.strip() != sn.strip()}
        # longest keys first so 'GTO10' would never be eaten by 'GTO1'
        keys = sorted(self.plants, key=len, reverse=True)
        self._plant_re = re.compile(r"\b(" + "|".join(map(re.escape, keys)) + r")\b") if keys else None

    # ---- plants
    def plant(self, key: Optional[str]) -> str:
        k = (key or "").upper()
        return self.plants.get(k, key or "")

    def plant_full(self, key: Optional[str]) -> str:
        """'Budenheim (NL2)' — name first, code as the detail."""
        k = (key or "").upper()
        name = self.plants.get(k)
        if k == "PORTFOLIO":
            return name or "Portfolio"
        return f"{name} ({key})" if name else (key or "")

    # ---- inverters
    # v230 (Tomasz): "Inverter 3" alone is not good enough — every place
    # a person reads shows the label AND the serial, one format:
    #     Inverter 3 (JGMAE65009)        text (mails, tickets, Ask)
    #     Inverter 3 <span class="sn">JGMAE65009</span>   pages
    def inverter_short(self, plant_key: Optional[str], sn: Optional[str]) -> str:
        """The label alone ('Inverter 3'), or 'inverter <serial>' when the
        unit has no label. For sort keys and tight chart legends."""
        lab = self.inverters.get(((plant_key or "").upper(), (sn or "").strip()))
        return lab if lab else f"inverter {sn}" if sn else ""

    def inverter(self, plant_key: Optional[str], sn: Optional[str]) -> str:
        """'Inverter 3 (JGMAE65009)' — label and serial, always."""
        return inverter_text(self.inverters.get(((plant_key or "").upper(), (sn or "").strip())), sn)

    inverter_full = inverter          # one format since v230

    def inverter_html(self, plant_key: Optional[str], sn: Optional[str]) -> str:
        """'Inverter 3 <span class="sn">JGMAE65009</span>' (escaped)."""
        return inverter_html(self.inverters.get(((plant_key or "").upper(), (sn or "").strip())), sn)

    # ---- free text
    def text(self, message: str, plant_key: Optional[str] = None,
             sn: Optional[str] = None, strip_prefix: bool = True) -> str:
        """Message text for people: drop the composer's ``PK SN: `` /
        ``PK: `` prefix (the header already says who), then replace any
        remaining plant code by its name and the serial by its label."""
        s = message or ""
        if strip_prefix and plant_key:
            for pre in ((f"{plant_key} {sn}: " if sn else None), f"{plant_key}: "):
                if pre and s.startswith(pre):
                    s = s[len(pre):]
                    break
        if self._plant_re is not None:
            s = self._plant_re.sub(lambda m: self.plants[m.group(1).upper()], s)
        if sn and plant_key:
            lab = self.inverters.get((plant_key.upper(), sn.strip()))
            if lab:
                s = re.sub(r"\b" + re.escape(sn) + r"\b", inverter_text(lab, sn), s)
        return s


def inverter_text(label: Optional[str], sn: Optional[str]) -> str:
    """Label + serial for text: 'Inverter 3 (JGMAE65009)'; a unit without
    a label is 'inverter JGMAE65009'; no serial -> ''."""
    sn = (sn or "").strip()
    if not sn:
        return ""
    return f"{label} ({sn})" if label else f"inverter {sn}"


def inverter_html(label: Optional[str], sn: Optional[str]) -> str:
    """Label + serial for pages, the serial in a muted mono span
    (portal_chrome styles ``.sn``). Escaped."""
    import html as _html
    sn = (sn or "").strip()
    if not sn:
        return ""
    tail = f'<span class="sn">{_html.escape(sn)}</span>'
    return f"{_html.escape(label)} {tail}" if label else f"inverter {tail}"


def names_from_rows(plant_rows: Iterable, inverter_rows: Iterable = ()) -> Names:
    """plant_rows: (plant_key, customer); inverter_rows: (plant_key,
    serial, label). Labels equal to the serial are not labels."""
    plants = {}
    for r in plant_rows:
        if len(r) >= 2 and r[0]:
            plants[r[0]] = short_customer(r[1] or "") or r[0]
    invs = {}
    for r in inverter_rows:
        if len(r) >= 3 and r[0] and r[1] and r[2]:
            invs[(r[0], r[1].strip())] = r[2].strip()
    return Names(plants, invs)


def load_names() -> Names:
    """Names from the live plant / inverter tables; codes-only on any
    error (a mail with codes beats no mail)."""
    try:
        from argia.store.pgq import psql_rows
        plants = psql_rows("SELECT plant_key, coalesce(customer,'') FROM plant;")
        # the configured label first; the label the vendor sent with the
        # newest telemetry fills the gaps (that is what the portal shows)
        invs = psql_rows("SELECT plant_key, inverter_sn, coalesce(inverter_label,'')"
                         " FROM inverter WHERE inverter_label IS NOT NULL AND inverter_label <> '';")
        seen = {(r[0], r[1].strip()) for r in invs if len(r) >= 2}
        tele = psql_rows("SELECT DISTINCT ON (plant_key, inverter_sn) plant_key, inverter_sn,"
                         " inverter_label FROM telemetry WHERE ts_utc > now() - interval '7 days'"
                         " AND inverter_label IS NOT NULL AND inverter_label <> ''"
                         " ORDER BY plant_key, inverter_sn, ts_utc DESC;")
        invs = list(invs) + [r for r in tele if len(r) >= 3 and (r[0], r[1].strip()) not in seen]
        return names_from_rows(plants, invs)
    except Exception:  # noqa: BLE001
        return Names()
