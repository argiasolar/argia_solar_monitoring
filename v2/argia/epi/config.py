"""v382: the server-side EPI configuration of the PPA plants (never in the repository).

Folder (ARGIA_EPI_DIR, default /opt/argia/epi):
  Annex_A_string_register.csv, Annex_B_plant_model_parameters.csv,
  Annex_C_design_soiling_profile.csv  - Arturo's annexes (spec v0.7)
  bias_v1.json    - B_m computed by scripts/epi600_design_check.py
  limits_v1.json  - inverter AC limits, kW (audit section 2)
  plants_v1.json  - {annex plant name: {key, vendor, channel_overrides}}
plus the layout files (inverter label -> serial, string -> MPPT and input) in
ARGIA_LAYOUT_DIR. A plant missing from any of them is simply not configured.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List

from argia.epi import engine as E
from argia.epi import model as M

EPI_DIR = os.environ.get('ARGIA_EPI_DIR', '/opt/argia/epi')
LAYOUT_DIR = os.environ.get('ARGIA_LAYOUT_DIR', '/opt/argia/layouts')
FILES = ('Annex_A_string_register.csv', 'Annex_B_plant_model_parameters.csv', 'Annex_C_design_soiling_profile.csv',
         'bias_v1.json', 'limits_v1.json', 'plants_v1.json')


@dataclass
class PlantConfig:
    name: str
    key: str
    vendor: str
    pm: M.PlantModel
    strings: List[M.StringDef]
    channel_of: Dict[str, E.Channel]
    sn_of: Dict[str, str]
    workorder: Dict[str, str] = field(default_factory=dict)   # uid -> "MPPT m, input n (layout name)"


def channel_for(string_id: str, vendor: str) -> E.Channel:
    n = int(re.sub(r'\D', '', string_id))
    if string_id.upper().startswith('MPPT'):
        return E.Channel('mppt', n)
    return E.Channel('pv' if vendor == 'HUAWEI' else 'str', n)


def load(epi_dir: str = EPI_DIR, layout_dir: str = LAYOUT_DIR) -> Dict[str, PlantConfig]:
    """plant_key -> PlantConfig; {} when the folder is incomplete (the page then has no card)."""
    if not all(os.path.exists(os.path.join(epi_dir, f)) for f in FILES):
        return {}
    j = lambda f: json.load(open(os.path.join(epi_dir, f), encoding='utf-8'))
    plants, bias, limits = j('plants_v1.json'), j('bias_v1.json')['bias'], j('limits_v1.json')
    with open(os.path.join(epi_dir, 'Annex_C_design_soiling_profile.csv'), encoding='utf-8') as f:
        soiling = tuple(float(r['design_soiling_pct']) for r in csv.DictReader(f))
    with open(os.path.join(epi_dir, 'Annex_B_plant_model_parameters.csv'), encoding='utf-8') as f:
        annex_b = {r['plant']: r for r in csv.DictReader(f)}
    rows: Dict[str, list] = {}
    with open(os.path.join(epi_dir, 'Annex_A_string_register.csv'), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.setdefault(r['plant'], []).append(r)
    out = {}
    for name, meta in plants.items():
        key, vendor = meta['key'], meta['vendor']
        lay_path = os.path.join(layout_dir, f'{key}.json')
        if name not in annex_b or name not in bias or name not in limits or name not in rows or not os.path.exists(lay_path):
            continue
        b = annex_b[name]
        pm = M.PlantModel(name, float(b['latitude']), float(b['longitude']),
                          tuple(float(b[k]) for k in ('b0', 'b1', 'b2', 'b3')), soiling, tuple(bias[name]), limits[name])
        strings, chan = [], {}
        for r in rows[name]:
            s = M.StringDef(r['uid'], r['inverter'], float(r['kwp']), float(r['tilt']), float(r['azimuth']),
                            r['module'], dt.date.fromisoformat(r['cod_date']))
            strings.append(s)
            chan[s.uid] = channel_for(r['string_id'], vendor)
        for uid, idx in (meta.get('channel_overrides') or {}).items():
            if uid in chan:
                chan[uid] = E.Channel(chan[uid].kind, int(idx))
        lay = json.load(open(lay_path, encoding='utf-8'))
        sn_of = {i['inv']: i['sn'] for i in lay.get('inverters', [])}
        by_ch = {(s.get('sn'), s.get('ch')): s for s in lay.get('strings', [])}
        wo = {}
        for s in strings:
            c = chan[s.uid]
            ls = by_ch.get((sn_of.get(s.inverter), ('m' if c.kind == 'mppt' else 's') + str(c.index)))
            mppt = (ls or {}).get('mppt') or (c.index if c.kind == 'mppt' else (c.index + 1) // 2 if c.kind == 'str' else None)
            wo[s.uid] = (f"{s.inverter} {sn_of.get(s.inverter, '')}, MPPT {mppt}, input {c.index}"
                         + (f" ({ls['name']})" if ls and ls.get('name') else ''))
        out[key] = PlantConfig(name, key, vendor, pm, strings, chan, sn_of, wo)
    return out
