#!/usr/bin/env python3
"""
argia_growatt_scan_inverters.py

Non-dev friendly:
- Logs into Growatt web (server.growatt.com)
- For each plantId, opens /device/photovoltaic?plantId=...
- Extracts candidate AJAX endpoints from the HTML
- Tries a small set of known inverter-list endpoints
- Prints what works and saves outputs into out/

You only need to run it once and paste the output back here.
"""

from __future__ import annotations

import os
import re
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_DEFAULT = "https://server.growatt.com"


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


def safe_json_loads(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    start_candidates = [text.find("{"), text.find("[")]
    start_candidates = [i for i in start_candidates if i >= 0]
    if not start_candidates:
        return None
    start = min(start_candidates)
    return json.loads(text[start:])


def request_any(session: requests.Session, method: str, url: str, **kwargs) -> Tuple[int, Dict[str, str], Any, str]:
    resp = session.request(method, url, **kwargs)
    text = resp.text or ""
    parsed = None
    try:
        parsed = resp.json()
    except Exception:
        try:
            parsed = safe_json_loads(text)
        except Exception:
            parsed = None
    return resp.status_code, dict(resp.headers), parsed, text


@dataclass
class GrowattWeb:
    base: str
    username: str
    password: str

    def __post_init__(self) -> None:
        self.s = requests.Session()
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        )
        self.s.headers.update(
            {
                "User-Agent": ua,
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8,pl;q=0.7,cs;q=0.6",
                "Connection": "keep-alive",
            }
        )

    def login(self) -> None:
        st, _, _, _ = request_any(self.s, "GET", f"{self.base}/login", timeout=30)
        if st != 200:
            die(f"GET /login failed: HTTP {st}")

        payload = {"account": self.username, "password": self.password}
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/login",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        st, _, _, body = request_any(self.s, "POST", f"{self.base}/login", data=payload, headers=headers, timeout=30)
        cookies = self.s.cookies.get_dict()
        if "assToken" not in cookies:
            snippet = (body or "").strip().replace("\n", " ")[:240]
            die(f"Login failed: assToken cookie missing. HTTP={st} body_snippet='{snippet}'")
        log("✅ Login OK (assToken present).")

    def seed_plant(self, plant_id: str) -> None:
        # Growatt web relies on selectedPlantId cookie
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device/photovoltaic")

    def get_pv_page(self, plant_id: str) -> str:
        self.seed_plant(plant_id)
        url = f"{self.base}/device/photovoltaic"
        st, _, _, html = request_any(self.s, "GET", url, params={"plantId": plant_id}, timeout=30)
        log(f"GET /device/photovoltaic?plantId={plant_id} -> {st} (len={len(html or '')})")
        return html or ""

    def post_form(self, path: str, plant_id: str, data: Dict[str, str]) -> Tuple[int, Any, str]:
        self.seed_plant(plant_id)
        url = f"{self.base}{path}"
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/device/photovoltaic?plantId={plant_id}",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        st, _, parsed, raw = request_any(self.s, "POST", url, headers=headers, data=data, timeout=45)
        return st, parsed, raw


def extract_paths_from_html(html: str) -> List[str]:
    # pick anything that looks like /something/something or /something.do
    found = set(re.findall(r"(/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+(?:\.do)?)", html or ""))
    # keep only likely API-ish paths
    out = []
    for p in sorted(found):
        if p.startswith("/javaScript"):
            continue
        if p.startswith("/v3/"):
            continue
        if p.startswith("/css") or p.endswith(".css"):
            continue
        if "images" in p:
            continue
        out.append(p)
    return out[:200]


def try_known_inverter_list_endpoints(cli: GrowattWeb, plant_id: str, out_dir: str) -> List[Dict[str, Any]]:
    """
    We try a few commonly used endpoints seen in Growatt UI variants.
    We don't assume which one you have; we just record which returns JSON.
    """
    candidates = [
        # most common patterns (different UI versions)
        ("/newInvAPI.do?op=getInvList", None),  # GET-like in old UI, but we'll try POST too? -> we'll skip, handled separately
        ("/newInvAPI.do?op=getInvList", "GET"),
        ("/newInvAPI.do?op=getInvList", "POST"),
        ("/newPlantAPI.do?op=getPlantList", "GET"),
        ("/device/getInvList", "POST"),
        ("/device/getPlantDeviceList", "POST"),
        ("/device/getDeviceList", "POST"),
        ("/panel/inverter/getInverterList", "POST"),
        ("/indexbC/inv/getInvList", "POST"),
    ]

    results: List[Dict[str, Any]] = []

    # 1) Try GET endpoints
    for path, method in candidates:
        if method != "GET":
            continue
        url = f"{cli.base}{path}"
        cli.seed_plant(plant_id)
        st, _, parsed, raw = request_any(cli.s, "GET", url, params={"plantId": plant_id}, timeout=30)
        ok = isinstance(parsed, (dict, list))
        results.append(
            {
                "path": path,
                "method": "GET",
                "http": st,
                "parsed_type": type(parsed).__name__,
                "ok_json": ok,
                "raw_snippet": (raw or "").strip().replace("\n", " ")[:200],
            }
        )
        write_json(os.path.join(out_dir, f"{plant_id}__try__{path.replace('/','_').replace('?','_')}.json"), {"parsed": parsed, "raw": raw})
        time.sleep(0.2)

    # 2) Try POST endpoints with common payloads
    post_payloads = [
        {"plantId": str(plant_id), "currPage": "1"},
        {"plantId": str(plant_id), "currPage": "1", "pageSize": "50"},
        {"plantId": str(plant_id), "currPage": "1", "alias": ""},
    ]

    for path, method in candidates:
        if method != "POST":
            continue
        for i, data in enumerate(post_payloads, start=1):
            st, parsed, raw = cli.post_form(path, plant_id, data=data)
            ok = isinstance(parsed, (dict, list))
            results.append(
                {
                    "path": path,
                    "method": "POST",
                    "payload": data,
                    "http": st,
                    "parsed_type": type(parsed).__name__,
                    "ok_json": ok,
                    "raw_snippet": (raw or "").strip().replace("\n", " ")[:200],
                }
            )
            write_json(
                os.path.join(out_dir, f"{plant_id}__try__{path.replace('/','_')}__p{i}.json"),
                {"payload": data, "parsed": parsed, "raw": raw},
            )
            time.sleep(0.2)

    return results


def main() -> None:
    base = env("GROWATT_BASE", BASE_DEFAULT) or BASE_DEFAULT
    user = env("GROWATT_USERNAME")
    pwd = env("GROWATT_PASSWORD")
    plant_ids_raw = env("GROWATT_PLANT_IDS")  # comma separated

    if not user or not pwd:
        die("Missing GROWATT_USERNAME / GROWATT_PASSWORD")
    if not plant_ids_raw:
        die("Missing GROWATT_PLANT_IDS (comma separated), e.g. 9275498,9309589")

    plant_ids = [p.strip() for p in plant_ids_raw.split(",") if p.strip()]
    if not plant_ids:
        die("No plantIds provided.")

    out_dir = env("GROWATT_OUT_DIR", "out") or "out"
    ensure_dir(out_dir)

    log("=== Growatt Inverter Scanner ===")
    log(f"Base: {base}")
    log(f"Plants: {', '.join(plant_ids)}")
    log(f"Out dir: {out_dir}")

    cli = GrowattWeb(base=base, username=user, password=pwd)
    cli.login()

    summary: Dict[str, Any] = {"base": base, "plants": []}

    for plant_id in plant_ids:
        log("")
        log(f"🏭 PlantId={plant_id}")
        pv_html = cli.get_pv_page(plant_id)
        pv_path = os.path.join(out_dir, f"{plant_id}__pv.html")
        write_text(pv_path, pv_html)

        paths = extract_paths_from_html(pv_html)
        write_json(os.path.join(out_dir, f"{plant_id}__pv_paths.json"), {"paths": paths})

        log(f"Found {len(paths)} candidate paths inside PV page HTML (saved).")

        tries = try_known_inverter_list_endpoints(cli, plant_id, out_dir)
        ok = [t for t in tries if t.get("ok_json")]

        log(f"Tried {len(tries)} endpoints. JSON responses: {len(ok)}")
        if ok:
            log("✅ JSON endpoints (top 3):")
            for t in ok[:3]:
                log(f"  - {t['method']} {t['path']} (HTTP {t['http']}) parsed={t['parsed_type']}")
        else:
            log("⚠️ No JSON inverter-list endpoint found in this scan (we will adjust scan list).")

        summary["plants"].append(
            {
                "plant_id": plant_id,
                "pv_html_file": pv_path,
                "paths_file": os.path.join(out_dir, f"{plant_id}__pv_paths.json"),
                "tries": tries,
                "json_ok": ok,
            }
        )

    summary_path = os.path.join(out_dir, "inverter_scan_summary.json")
    write_json(summary_path, summary)
    log("")
    log(f"🧾 Summary saved: {summary_path}")
    log("✅ Done.")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        die(f"Network error: {e}")
