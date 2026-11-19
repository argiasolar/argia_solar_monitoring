"""v383: "open a ticket" from the plant page's string and inverter views.

Pure. The troubleshooting card (EPI views) and the string layout card offer a
prefilled /maintenance/new/ link on what is clearly underperforming, and the
number of the open ticket instead when one already covers that input or that
inverter (so the team does not open the same ticket twice).

Matching an open ticket: same plant and inverter serial, and
* a string: the title names the same input ("input 4" or "MPPT 2", whole word);
* an inverter: the title names no input at all (an inverter-level ticket).
The titles this module writes always carry that token, so tickets opened from
the cards are found again; a ticket typed by hand is found when it uses the
same words.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence, Tuple
from urllib.parse import urlencode

OPEN_STATUSES = ("NEW", "IN_PROGRESS", "WAITING", "VERIFICATION")
OPEN_SQL = ("SELECT number, plant_key, coalesce(inverter_sn, ''), title FROM ticket"
            " WHERE status IN ('NEW','IN_PROGRESS','WAITING','VERIFICATION');")
_ANY_INPUT = re.compile(r"\b(input|MPPT)\s+\d+\b", re.IGNORECASE)
NEW_URL = "/maintenance/new/"

OpenTicket = Tuple[str, str, str, str]          # number, plant_key, inverter_sn, title


def input_token(kind: str, index: int) -> str:
    """'input 4' for a string input (Growatt 'str', Huawei 'pv', layout 's4'), 'MPPT 2' for an MPPT."""
    return f"MPPT {int(index)}" if kind in ("mppt", "m") else f"input {int(index)}"


def token_of_channel(ch: str) -> str:
    """Layout channel 's4' -> 'input 4', 'm2' -> 'MPPT 2'."""
    return input_token(ch[0], int(ch[1:]))


def match_open(tickets: Iterable[OpenTicket], plant_key: str, sn: str, token: Optional[str] = None) -> Optional[str]:
    """The number of the first open ticket on this inverter that covers ``token`` (None = inverter level)."""
    pk = (plant_key or "").upper()
    for number, tpk, tsn, title in tickets:
        if (tpk or "").upper() != pk or (tsn or "").strip() != (sn or "").strip():
            continue
        if token is None:
            if not _ANY_INPUT.search(title or ""):
                return number
        elif re.search(r"\b" + re.escape(token) + r"\b", title or "", re.IGNORECASE):
            return number
    return None


def new_url(plant_key: str, sn: str, title: str, description: str, category: str, priority: str = "P3") -> str:
    """The prefilled form (the ticket app validates every field again on submit)."""
    return NEW_URL + "?" + urlencode({"plant": plant_key.upper(), "sn": sn, "category": category, "priority": priority,
                                      "title": title[:140], "description": description[:3500]})


def epi_tickets(payload: dict, plant_key: str, plant_name: str, tickets: Sequence[OpenTicket],
                channel_token: dict) -> dict:
    """For the troubleshooting card: {'s': {uid: {...}}, 'i': {inv: {...}}} where each entry is
    {'open': number} or {'url': prefilled link}. Strings: those below the band and those with no
    output; inverters: those with a dip against their peers. ``channel_token``: uid -> 'input n'."""
    day = payload["day"]
    out = {"s": {}, "i": {}}
    inv_by = {i["inv"]: i for i in payload["inverters"]}
    for inv, sv in payload["strings"].items():
        meta = inv_by.get(inv, {})
        sn, name = meta.get("sn") or "", meta.get("name") or inv
        wo = dict(zip(sv["uids"], sv.get("wo") or [""] * len(sv["uids"])))
        epi = sv.get("epi600_mtd") or {}
        items = [(o["uid"], "low", o) for o in sv["out"] if o["pct"] < 0] + [(u, "zero", None) for u in sv["zero"]]
        for uid, kind, o in items:
            tok = channel_token.get(uid)
            if not tok or not sn:
                continue
            hit = match_open(tickets, plant_key, sn, tok)
            if hit:
                out["s"][uid] = {"open": hit}
                continue
            if kind == "low":
                what = f"{o['pct']}% vs the inverter's other strings"
                detail = (f"Median deviation {o['pct']}% for {o['minutes']} min with G_poa of 600 W/m2 or more; "
                          f"worst {o['worst']}% at {o['at']}.")
                prio = "P3"
            else:
                what = "no output"
                detail = "Below 5% of the inverter's median string with G_poa of 400 W/m2 or more, all day."
                prio = "P2"
            e = epi.get(uid)
            desc = "\n".join([
                f"Plant: {plant_name}. Day: {day}.",
                f"Where: {wo.get(uid) or name + ' ' + sn + ', ' + tok}.",
                detail,
                f"String EPI-600 month to date: {e * 100:.1f}%." if e is not None else "String EPI-600 month to date: n/a.",
                "Opened from the plant page, troubleshooting card (strings view).",
            ])
            out["s"][uid] = {"url": new_url(plant_key, sn, f"{name} {tok}: {what} ({day})", desc, "array/string", prio)}
    for i in payload["inverters"]:
        if not i.get("gaps") or not i.get("sn"):
            continue
        hit = match_open(tickets, plant_key, i["sn"])
        if hit:
            out["i"][i["inv"]] = {"open": hit}
            continue
        worst = min(i["gaps"], key=lambda g: g["pct_peers"])
        spans = ", ".join(f"{g['from']}-{g['to']} ({g['pct_peers']}%)" for g in i["gaps"])
        desc = "\n".join([
            f"Plant: {plant_name}. Day: {day}. Inverter: {i['name']} {i['sn']}.",
            f"Below the plant's other inverters (actual/design ratio under 85% of their median): {spans}.",
            f"EPI-600 day {i['epi600_day'] * 100:.1f}%." if i.get("epi600_day") is not None else "EPI-600 day n/a.",
            "Opened from the plant page, troubleshooting card (inverters view).",
        ])
        out["i"][i["inv"]] = {"url": new_url(plant_key, i["sn"], f"{i['name']}: {worst['pct_peers']}% vs the other inverters ({day})",
                                             desc, "inverter/derating", "P3")}
    return out


def layout_open(strings: Sequence[dict], plant_key: str, tickets: Sequence[OpenTicket]) -> dict:
    """For the string layout card: {index of the string: open ticket number} (the card builds the new link)."""
    out = {}
    for k, s in enumerate(strings):
        try:
            tok = token_of_channel(str(s.get("ch") or ""))
        except (ValueError, IndexError):
            continue
        hit = match_open(tickets, plant_key, s.get("sn") or "", tok)
        if hit:
            out[str(k)] = hit
    return out
