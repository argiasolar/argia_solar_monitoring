"""Software bill of materials of pio06 as CycloneDX 1.5 JSON (v324).

Prologis VSQ / ARGIA policy: know every software component that runs the
platform and check it for known vulnerabilities. This lists

* every installed Debian package (dpkg-query) - the system Python that runs
  ARGIA for Prologis (Flask, Werkzeug, Jinja2 come from Debian), nginx,
  OpenSSL, SQLite, cryptsetup, age, ClamAV ... - as pkg:deb purls, and
* every Python distribution of the job environment this script runs in
  (the v2 venv) as pkg:pypi purls,

with the deployed git commit as the SBOM's subject. Debian packages get
their security fixes from unattended-upgrades; the repo's Python
requirements are audited with pip-audit in CI (.github/workflows/v2-security.yml).

    run_job.sh sbom server_sbom.py --out /root/argia_logs/sbom_server.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys
import uuid
from typing import Dict, List, Optional, Sequence
from urllib.parse import quote

DPKG_FORMAT = "${Package}\\t${Version}\\t${Architecture}\\t${db:Status-Abbrev}\\n"


def parse_dpkg(text: str) -> List[Dict[str, str]]:
    """dpkg-query lines -> installed packages (status 'ii' only). Pure."""
    out = []
    for ln in (text or "").splitlines():
        parts = ln.split("\t")
        if len(parts) < 4 or not parts[3].startswith("ii"):
            continue
        out.append({"name": parts[0], "version": parts[1], "arch": parts[2]})
    return out


def components(debs: Sequence[Dict[str, str]], pys: Sequence[Dict[str, str]]) -> List[Dict]:
    comps = []
    for d in sorted(debs, key=lambda x: x["name"]):
        purl = f"pkg:deb/debian/{quote(d['name'])}@{quote(d['version'], safe='')}?arch={d['arch']}"
        comps.append({"type": "library" if d["name"].startswith(("lib", "python3-")) else "application",
                      "name": d["name"], "version": d["version"], "purl": purl, "bom-ref": purl})
    for p in sorted(pys, key=lambda x: x["name"].lower()):
        purl = f"pkg:pypi/{quote(p['name'].lower())}@{quote(p['version'], safe='')}"
        comps.append({"type": "library", "name": p["name"], "version": p["version"], "purl": purl, "bom-ref": purl})
    return comps


def bom(debs: Sequence[Dict[str, str]], pys: Sequence[Dict[str, str]], commit: str, host: str,
        now: Optional[dt.datetime] = None) -> Dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{uuid.uuid4()}", "version": 1,
            "metadata": {"timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "tools": [{"vendor": "ARGIA", "name": "server_sbom.py", "version": "v324"}],
                         "component": {"type": "application", "name": "argia-monitoring-server", "version": commit or "unknown",
                                       "description": f"ARGIA server {host}: portal, monitoring jobs, ARGIA for Prologis"}},
            "components": components(debs, pys)}


def python_dists() -> List[Dict[str, str]]:
    from importlib import metadata
    seen, out = set(), []
    for d in metadata.distributions():
        name = (d.metadata["Name"] or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name, "version": d.version})
    return out


def git_commit(repo: str) -> str:
    try:
        return subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="CycloneDX SBOM of this server")
    ap.add_argument("--out", default="/root/argia_logs/sbom_server.json")
    a = ap.parse_args(argv)
    try:
        dpkg = subprocess.run(["dpkg-query", "-W", "-f", DPKG_FORMAT], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        dpkg = ""
    debs = parse_dpkg(dpkg)
    pys = python_dists()
    repo = os.environ.get("ARGIA_REPO", os.path.join(os.path.expanduser("~"), "argia_v2"))
    doc = bom(debs, pys, git_commit(repo), platform.node())
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, a.out)
    print(f"SBOM: {len(doc['components'])} components ({len(debs)} Debian packages, {len(pys)} Python distributions) -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
