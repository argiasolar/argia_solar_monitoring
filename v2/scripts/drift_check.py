"""Status-quo harness (v218): is what RUNS on pio06 what is IN GIT — and
does the live system still answer the way the go-live checklist says?

    drift_check.py                 # write /root/argia_logs/drift_latest.json, print a summary
    drift_check.py --print         # only print
    drift_check.py --out FILE

Three sections, all pure functions over injected readers (unit-tested
without a server):

* **files** — every deployed copy (``/opt/argia/bundle``, the systemd
  units, the nginx vhosts and the auth snippet) byte-compared with its
  source in the repo checkout; deployed files with no source ("extras")
  are listed too, because that is how forgotten CSVs and old bundles
  accumulate;
* **git** — the server checkout must be at ``origin/main`` and clean
  (a hand edit on the server is exactly the failure mode the bundle
  directory was created to end);
* **smoke** — the public answers the portal must give (401 on /login,
  302 on /, 301 from the retired domains, 200 for the favicon), the
  age of the last backup and of the portfolio snapshot, every argia
  timer active.

The nightly unit never fails on a finding: it writes the JSON and the
alert mailer turns findings into admin-only WARNINGs (v217 routing) on
the daily digest. Nothing here changes the server.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Sequence, Tuple

REPO = os.environ.get("ARGIA_REPO", "/root/argia_v2")
BUNDLE_DIR = "/opt/argia/bundle"
UNIT_DIR = "/etc/systemd/system"
OUT_DEFAULT = "/root/argia_logs/drift_latest.json"

# repo-relative source -> deployed path (the explicit ones; the bundle's
# *.py/*.sh/*.sql and argia-*.service/.timer are mapped by name below)
EXPLICIT: List[Tuple[str, str]] = [
    ("v2/server/monitoring_gen.py", f"{BUNDLE_DIR}/monitoring_gen.py"),
    ("v2/server/bundle/nginx-argia_session.conf", "/etc/nginx/snippets/argia_auth.conf"),
    ("v2/server/bundle/nginx-monitoring.argia.com.mx.conf", "/etc/nginx/sites-enabled/monitoring.argia.com.mx.conf"),
    ("v2/server/bundle/portal.argia.com.mx.conf", "/etc/nginx/sites-enabled/portal.argia.com.mx.conf"),
    ("v2/server/bundle/report.argia.com.mx.conf", "/etc/nginx/sites-enabled/report.argia.com.mx.conf"),
    ("v2/server/bundle/portfolio.argia.com.mx.conf", "/etc/nginx/sites-enabled/portfolio.argia.com.mx.conf"),
]
# bundle files that are deliberately NOT deployed anywhere
NOT_DEPLOYED = {
    "README.md",
    "nginx-argia_auth.conf",            # the pre-session (basic auth) snippet, kept as documented rollback
    "portal.argia.com.mx.http.conf",    # bootstrap vhost used once before the certificate existed
}
BUNDLE_EXTS = (".py", ".sh", ".sql")

# public answers the portal must give: (name, url, accepted status codes)
SMOKE_HTTP: List[Tuple[str, str, Tuple[int, ...]]] = [
    ("portal-login", "https://portal.argia.com.mx/login", (401,)),
    ("portal-root", "https://portal.argia.com.mx/", (302,)),
    ("portal-favicon", "https://portal.argia.com.mx/favicon.png", (200,)),
    ("portal-setup-wall", "https://portal.argia.com.mx/setup/", (302,)),
    ("portal-monitoring-wall", "https://portal.argia.com.mx/monitoring/", (302,)),
    ("old-report", "https://report.argia.com.mx/", (301,)),
    ("old-monitoring", "https://monitoring.argia.com.mx/", (301,)),
    ("old-portfolio", "https://portfolio.argia.com.mx/", (301,)),
]
BACKUP_MAX_H = 26.0
FRESH_FILES = [("backup-dump", "/root/argia_backups/argia_mont_latest.dump"),
               ("backup-users", "/root/argia_backups/users_latest.db"),
               ("portfolio-snapshot", "/root/argia_backups/portfolio_latest.json")]


# ------------------------------------------------------------------ pure
def pairs(repo: str, bundle_files: Sequence[str]) -> List[Tuple[str, str]]:
    """(source, deployed) for every file the repo says is deployed.
    ``bundle_files`` = names in v2/server/bundle (injected for tests)."""
    out = [(os.path.join(repo, s), d) for s, d in EXPLICIT]
    for name in sorted(bundle_files):
        src = os.path.join(repo, "v2/server/bundle", name)
        if name in NOT_DEPLOYED or name in {os.path.basename(s) for s, _ in EXPLICIT}:
            continue
        if name.startswith("argia-") and (name.endswith(".service") or name.endswith(".timer")):
            out.append((src, os.path.join(UNIT_DIR, name)))
        elif name.endswith(BUNDLE_EXTS):
            out.append((src, os.path.join(BUNDLE_DIR, name)))
    return out


def unmapped(bundle_files: Sequence[str]) -> List[str]:
    """Bundle files that are neither deployed by a rule nor declared
    NOT_DEPLOYED — a new file type someone forgot to wire."""
    mapped = {os.path.basename(s) for s, _ in pairs("", bundle_files)} | NOT_DEPLOYED | {"__pycache__"}
    return sorted(n for n in bundle_files if n not in mapped)


def compare(pairs_: Sequence[Tuple[str, str]],
            read: Callable[[str], Optional[bytes]]) -> List[Dict[str, str]]:
    """[{src, dst, status}] with status same | diff | missing (deployed
    copy absent) | no-source (repo file absent — a deploy would delete)."""
    out = []
    for src, dst in pairs_:
        a, b = read(src), read(dst)
        if a is None:
            status = "no-source"
        elif b is None:
            status = "missing"
        else:
            status = "same" if a == b else "diff"
        out.append({"src": src, "dst": dst, "status": status})
    return out


def extras(deployed_names: Sequence[str], expected_names: Sequence[str],
           ignore: Sequence[str] = ("__pycache__",)) -> List[str]:
    """Deployed files with no source in the repo."""
    exp = set(expected_names)
    return sorted(n for n in deployed_names if n not in exp and n not in ignore)


def judge_http(name: str, code: Optional[int]) -> Dict[str, object]:
    want = next((c for n, _, c in SMOKE_HTTP if n == name), ())
    ok = code is not None and code in want
    return {"check": name, "code": code, "want": list(want), "ok": ok}


def judge_age(name: str, age_h: Optional[float], max_h: float = BACKUP_MAX_H) -> Dict[str, object]:
    ok = age_h is not None and age_h <= max_h
    return {"check": name, "age_h": None if age_h is None else round(age_h, 1), "max_h": max_h, "ok": ok}


def findings(report: dict) -> List[str]:
    """One line per problem — what the alert mailer shows. Pure."""
    out: List[str] = []
    g = report.get("git") or {}
    if g.get("head") and g.get("origin") and g["head"] != g["origin"]:
        out.append(f"checkout {g['head'][:7]} is not origin/main {g['origin'][:7]}")
    for f in g.get("dirty", []):
        out.append(f"hand-edited in checkout: {f}")
    for r in report.get("files", []):
        if r["status"] != "same":
            out.append(f"{r['status']}: {r['dst']}")
    for x in report.get("extras", []):
        out.append(f"extra (not in git): {x}")
    for u in report.get("unmapped", []):
        out.append(f"unmapped bundle file (no deploy rule): {u}")
    for s in report.get("smoke", []):
        if not s.get("ok"):
            if "code" in s:
                out.append(f"{s['check']}: HTTP {s['code']} (expected {'/'.join(map(str, s['want']))})")
            elif "age_h" in s:
                out.append(f"{s['check']}: {s['age_h']} h old (max {s['max_h']})")
            else:
                out.append(f"{s['check']}: {s.get('detail', 'failed')}")
    return out


# ------------------------------------------------------------------ I/O
def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _run(cmd: Sequence[str], cwd: Optional[str] = None, timeout: int = 60) -> str:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def git_state(repo: str) -> Dict[str, object]:
    _run(["git", "fetch", "-q", "origin"], cwd=repo, timeout=90)
    head = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    origin = _run(["git", "rev-parse", "origin/main"], cwd=repo).strip()
    dirty = [l[3:] for l in _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo).splitlines() if l]
    return {"head": head, "origin": origin, "dirty": dirty}


def http_code(url: str, timeout: int = 20) -> Optional[int]:
    out = _run(["curl", "-sS", "-o", "/dev/null", "-m", str(timeout), "-w", "%{http_code}", url], timeout=timeout + 5)
    try:
        return int(out.strip()) or None
    except ValueError:
        return None


def file_age_h(path: str, now: Optional[float] = None) -> Optional[float]:
    try:
        return ((now or dt.datetime.now().timestamp()) - os.path.getmtime(path)) / 3600.0
    except OSError:
        return None


def timers_inactive() -> List[str]:
    out = []
    for line in _run(["systemctl", "list-unit-files", "argia-*.timer", "--no-legend"]).splitlines():
        name = line.split()[0] if line.split() else ""
        if name and _run(["systemctl", "is-active", name]).strip() != "active":
            out.append(name)
    return out


def build_report(repo: str = REPO) -> dict:
    bundle_files = sorted(os.listdir(os.path.join(repo, "v2/server/bundle")))
    prs = pairs(repo, bundle_files)
    files = compare(prs, _read)
    deployed = sorted(n for n in os.listdir(BUNDLE_DIR)) if os.path.isdir(BUNDLE_DIR) else []
    expected = [os.path.basename(d) for _, d in prs if d.startswith(BUNDLE_DIR + "/")]
    smoke: List[dict] = [judge_http(n, http_code(u)) for n, u, _ in SMOKE_HTTP]
    smoke += [judge_age(n, file_age_h(p)) for n, p in FRESH_FILES]
    inactive = timers_inactive()
    smoke.append({"check": "timers-active", "ok": not inactive,
                  "detail": "inactive: " + ", ".join(inactive) if inactive else "all active"})
    rep = {"generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "git": git_state(repo), "files": files,
           "extras": extras(deployed, expected), "unmapped": unmapped(bundle_files),
           "smoke": smoke}
    rep["findings"] = findings(rep)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="repo vs deployed drift + live smoke checks")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--print", action="store_true", help="print only, write nothing")
    ap.add_argument("--repo", default=REPO)
    a = ap.parse_args(argv)
    rep = build_report(a.repo)
    n_same = sum(1 for r in rep["files"] if r["status"] == "same")
    print(f"drift_check {rep['generated_utc']}: {n_same}/{len(rep['files'])} deployed files match git, "
          f"{len(rep['extras'])} extra, {sum(1 for s in rep['smoke'] if s['ok'])}/{len(rep['smoke'])} smoke OK")
    for line in rep["findings"]:
        print("  - " + line)
    if not rep["findings"]:
        print("  status quo intact")
    if not a.print:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        tmp = a.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
        os.replace(tmp, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
