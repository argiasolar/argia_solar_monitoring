#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_DEFAULT = "https://server.growatt.com"


# ---------------- SAFE FILENAMES ----------------

def safe_name(s: str) -> str:
    s = s.replace("https://server.growatt.com", "")
    s = re.sub(r'[\\/:*?"<>|\r\n]+', "_", s)
    return s.strip("_")[:180]


# ---------------- BASIC UTILS ----------------

def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> None:
    raise SystemExit(f"❌ {msg}")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def request_any(session: requests.Session, method: str, url: str, **kwargs) -> Tuple[int, Dict[str, str], Any, str]:
    resp = session.request(method, url, **kwargs)
    text = resp.text or ""
    parsed = None
    try:
        parsed = resp.json()
    except Exception:
        parsed = None
    return resp.status_code, dict(resp.headers), parsed, text


# ---------------- GROWATT CLIENT ----------------

@dataclass
class GrowattWeb:
    base: str
    username: str
    password: str

    def __post_init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0"})

    def login(self) -> None:
        request_any(self.s, "GET", f"{self.base}/login", timeout=30)

        payload = {"account": self.username, "password": self.password}
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/login",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        st, _, _, body = request_any(self.s, "POST", f"{self.base}/login", data=payload, headers=headers, timeout=30)

        if "assToken" not in self.s.cookies.get_dict():
            die("Login failed — assToken missing")

        log("✅ Login OK")

    def seed_plant(self, plant_id: str) -> None:
        self.s.cookies.set("selectedPlantId", str(plant_id))

    def get_pv_page(self, plant_id: str) -> str:
        self.seed_plant(plant_id)
        st, _, _, html = request_any(self.s, "GET", f"{self.base}/device/photovoltaic", params={"plantId": plant_id}, timeout=30)
        log(f"GET PV {plant_id} -> {st}")
        return html or ""

    def post_form(self, path: str, plant_id: str, data: Dict[str, str]) -> Tuple[int, Any, str]:
        self.seed_plant(plant_id)
        headers = {"X-Requested-With": "XMLHttpRequest"}
        st, _, parsed, raw = request_any(self.s, "POST", f"{self.base}{path}", headers=headers, data=data, timeout=45)
        return st, parsed, raw


# ---------------- MAIN ----------------

def main() -> None:
    base = env("GROWATT_BASE", BASE_DEFAULT)
    user = env("GROWATT_USERNAME")
    pwd = env("GROWATT_PASSWORD")
    plant_ids = env("GROWATT_PLANT_IDS")

    if not user or not pwd or not plant_ids:
        die("Missing env vars")

    plant_ids = [p.strip() for p in plant_ids.split(",") if p.strip()]
    out_dir = env("GROWATT_OUT_DIR", "out")
    ensure_dir(out_dir)

    cli = GrowattWeb(base=base, username=user, password=pwd)
    cli.login()

    summary = {"plants": []}

    for plant_id in plant_ids:
        log(f"Scanning plant {plant_id}")

        html = cli.get_pv_page(plant_id)
        write_text(os.path.join(out_dir, f"{plant_id}__pv.html"), html)

        endpoints = [
            "/newInvAPI.do?op=getInvList",
            "/device/getInvList",
            "/panel/inverter/getInverterList"
        ]

        tries = []

        for ep in endpoints:
            st, parsed, raw = cli.post_form(ep, plant_id, {"plantId": plant_id})

            fname = f"{plant_id}__try__{safe_name(ep)}.json"
            write_json(os.path.join(out_dir, fname), {"status": st, "parsed": parsed, "raw": raw[:2000]})

            tries.append({"endpoint": ep, "status": st, "ok_json": isinstance(parsed, (dict, list))})

            time.sleep(0.3)

        summary["plants"].append({"plant_id": plant_id, "tries": tries})

    write_json(os.path.join(out_dir, "inverter_scan_summary.json"), summary)
    log("✅ Scan done")


if __name__ == "__main__":
    main()
