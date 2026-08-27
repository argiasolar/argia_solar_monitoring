#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
    s = (s or "").strip()
    s = s.replace("https://server.growatt.com", "")
    s = re.sub(r'[\\/:*?"<>|\r\n]+', "_", s)
    s = re.sub(r"\s+", "_", s)
    return s.strip("_")[:180]


# ---------------- BASIC UTILS ----------------

def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    raise SystemExit(f"❌ {msg}")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8", errors="ignore") as f:
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


def extract_ajax_paths(html: str) -> List[str]:
    """
    Extract likely AJAX endpoints from scripts:
      url: "/device/xxx"
      $.post("/device/yyy")
      fetch("/device/zzz")
    """
    html = html or ""
    hits: List[str] = []

    # url: "/path"
    for m in re.finditer(r"url\s*:\s*['\"](\/[^'\"]+)['\"]", html):
        hits.append(m.group(1))

    # $.get/post("/path"
    for m in re.finditer(r"\$\.(?:get|post)\(\s*['\"](\/[^'\"]+)['\"]", html):
        hits.append(m.group(1))

    # fetch("/path"
    for m in re.finditer(r"fetch\(\s*['\"](\/[^'\"]+)['\"]", html):
        hits.append(m.group(1))

    # de-dup keep order
    seen = set()
    out = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


# ---------------- GROWATT CLIENT ----------------

@dataclass
class GrowattWeb:
    base: str
    username: str
    password: str

    def __post_init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
                "Connection": "keep-alive",
            }
        )

    def login(self) -> None:
        request_any(self.s, "GET", f"{self.base}/login", timeout=30)

        payload = {"account": self.username, "password": self.password}
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/login",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        st, _, _, body = request_any(self.s, "POST", f"{self.base}/login", data=payload, headers=headers, timeout=30)

        if "assToken" not in self.s.cookies.get_dict():
            snippet = (body or "").strip().replace("\n", " ")[:240]
            die(f"Login failed (assToken missing). HTTP={st} snippet='{snippet}'")

        log("✅ Login OK (assToken present).")

        # seed UI
        request_any(self.s, "GET", f"{self.base}/index", timeout=30)

    def seed_plant(self, plant_id: str) -> None:
        # cookies Growatt UI expects
        try:
            self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")
            self.s.cookies.set("selPage", "%2Fdevice", domain="server.growatt.com", path="/")
        except Exception:
            # fallback (no domain/path)
            self.s.cookies.set("selectedPlantId", str(plant_id))
            self.s.cookies.set("selPage", "%2Fdevice")

    def get_page(self, path: str, plant_id: str, params: Optional[dict] = None, referer: Optional[str] = None) -> Tuple[int, str]:
        self.seed_plant(plant_id)
        headers = {}
        if referer:
            headers["Referer"] = referer
        st, _, _, html = request_any(self.s, "GET", f"{self.base}{path}", params=params or {}, headers=headers, timeout=45)
        return st, (html or "")


# ---------------- MAIN ----------------

def main() -> None:
    base = env("GROWATT_BASE", BASE_DEFAULT) or BASE_DEFAULT
    user = env("GROWATT_USERNAME")
    pwd = env("GROWATT_PASSWORD")
    plant_ids_raw = env("GROWATT_PLANT_IDS")
    out_dir = env("GROWATT_OUT_DIR", "out") or "out"

    if not user or not pwd:
        die("Missing GROWATT_USERNAME / GROWATT_PASSWORD")
    if not plant_ids_raw:
        die("Missing GROWATT_PLANT_IDS (comma separated), e.g. 9275498,9309589,9309575,10078094")

    plant_ids = [p.strip() for p in plant_ids_raw.split(",") if p.strip()]
    if not plant_ids:
        die("No plantIds provided")

    ensure_dir(out_dir)

    cli = GrowattWeb(base=base, username=user, password=pwd)
    cli.login()

    # artifact marker
    write_text(os.path.join(out_dir, f"RUN_MARKER_{int(time.time())}.txt"), "scan started\n")

    summary: Dict[str, Any] = {
        "base": base,
        "plants": [],
    }

    for plant_id in plant_ids:
        log(f"\n🏭 Plant {plant_id}")

        plant_block: Dict[str, Any] = {"plant_id": plant_id, "pages": {}, "ajax_paths": {}}

        # 1) PV page
        st, pv_html = cli.get_page("/device/photovoltaic", plant_id, params={"plantId": plant_id}, referer=f"{base}/panel")
        pv_fn = os.path.join(out_dir, f"{plant_id}__pv.html")
        write_text(pv_fn, pv_html)
        plant_block["pages"]["pv"] = {"http": st, "file": pv_fn, "len": len(pv_html)}
        plant_block["ajax_paths"]["pv"] = extract_ajax_paths(pv_html)
        write_json(os.path.join(out_dir, f"{plant_id}__pv_ajax_paths.json"), {"paths": plant_block["ajax_paths"]["pv"]})
        log(f"  PV page: HTTP {st} len={len(pv_html)} paths={len(plant_block['ajax_paths']['pv'])}")

        # 2) MAX page (this is the one we need for telemetry endpoints)
        st, max_html = cli.get_page("/device/getMAXPage", plant_id, params={"ttt": str(int(time.time()*1000))}, referer=f"{base}/device/photovoltaic?plantId={plant_id}")
        max_fn = os.path.join(out_dir, f"{plant_id}__getMAXPage.html")
        write_text(max_fn, max_html)
        plant_block["pages"]["getMAXPage"] = {"http": st, "file": max_fn, "len": len(max_html)}
        plant_block["ajax_paths"]["getMAXPage"] = extract_ajax_paths(max_html)
        write_json(os.path.join(out_dir, f"{plant_id}__getMAXPage_ajax_paths.json"), {"paths": plant_block["ajax_paths"]["getMAXPage"]})
        log(f"  getMAXPage: HTTP {st} len={len(max_html)} paths={len(plant_block['ajax_paths']['getMAXPage'])}")

        # 3) Inverter page (sometimes contains realtime endpoints)
        st, inv_html = cli.get_page("/device/getInverterPage", plant_id, params={"plantId": plant_id}, referer=f"{base}/device/photovoltaic?plantId={plant_id}")
        inv_fn = os.path.join(out_dir, f"{plant_id}__getInverterPage.html")
        write_text(inv_fn, inv_html)
        plant_block["pages"]["getInverterPage"] = {"http": st, "file": inv_fn, "len": len(inv_html)}
        plant_block["ajax_paths"]["getInverterPage"] = extract_ajax_paths(inv_html)
        write_json(os.path.join(out_dir, f"{plant_id}__getInverterPage_ajax_paths.json"), {"paths": plant_block["ajax_paths"]["getInverterPage"]})
        log(f"  getInverterPage: HTTP {st} len={len(inv_html)} paths={len(plant_block['ajax_paths']['getInverterPage'])}")

        # Save combined paths for convenience
        combined = []
        seen = set()
        for bucket in ("pv", "getMAXPage", "getInverterPage"):
            for p in plant_block["ajax_paths"].get(bucket, []):
                if p not in seen:
                    seen.add(p)
                    combined.append(p)
        plant_block["ajax_paths"]["combined"] = combined
        write_json(os.path.join(out_dir, f"{plant_id}__ajax_paths_combined.json"), {"paths": combined})

        summary["plants"].append(plant_block)

    write_json(os.path.join(out_dir, "inverter_scan_summary.json"), summary)
    log(f"\n✅ Done. Output saved to: {out_dir}/ (artifact should upload OK)")

if __name__ == "__main__":
    main()
