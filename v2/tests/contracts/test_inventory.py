"""v263 - the inventory of what the system does, pinned.

A move, a rename or a refactor must not make a feature disappear without
someone deciding it. Three inventories are pinned as plain text files next
to this test; if one changes on purpose, update the file in the same commit
(ARGIA_UPDATE_CONTRACTS=1 pytest tests/contracts rewrites them - then read
the git diff, which IS the review of what was added or removed):

* routes.txt - every URL the five portal apps answer (method + path);
* jobs.txt   - every systemd job on pio06: unit, schedule, the script it runs;
* pi_jobs.txt - every cron job on the office Pi.

Plus two rules that need no snapshot:
* every script a job runs exists in the repo;
* every job on the server is either run for real by
  tests/portal/test_jobs_smoke.py or listed there as not runnable, with a
  reason - a new job cannot slip in untested and unnoticed.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
HERE = pathlib.Path(__file__).parent
APPS = ("ask_app", "auth_app", "fin_app", "maint_app", "setup_app")
LONG_RUNNING = {"ask_app.py", "auth_app.py", "fin_app.py", "maint_app.py", "setup_app.py", "savio_mock.py"}
# jobs exercised end to end by a dedicated test module rather than the smoke list
RUN_ELSEWHERE = {"portal_gen.py": "tests/portal/test_portal_site.py"}
UPDATE = os.environ.get("ARGIA_UPDATE_CONTRACTS") == "1"


def _check(name, lines):
    path = HERE / name
    text = "\n".join(lines) + "\n"
    if UPDATE:
        path.write_text(text, encoding="utf-8")
    want = path.read_text(encoding="utf-8").splitlines()
    got = text.splitlines()
    lost, new = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    assert not lost, f"{name}: these are GONE (feature lost?):\n  " + "\n  ".join(lost) + (
        f"\n{name}: new, not yet in the file:\n  " + "\n  ".join(new) if new else "")
    assert not new, f"{name}: new entries - if intended, add them (ARGIA_UPDATE_CONTRACTS=1):\n  " + "\n  ".join(new)


# ------------------------------------------------------------ routes
def routes():
    out = []
    for app in APPS:
        tree = ast.parse((BUNDLE / f"{app}.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not isinstance(n, ast.FunctionDef):
                continue
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("route", "get", "post"):
                    path = d.args[0].value if d.args and isinstance(d.args[0], ast.Constant) else "?"
                    if d.func.attr == "route":
                        meths = [e.value for k in d.keywords if k.arg == "methods" for e in k.value.elts] or ["GET"]
                    else:
                        meths = [d.func.attr.upper()]
                    for m in meths:
                        out.append(f"{app}  {m.upper():6s} {path}")
    return sorted(set(out))


def test_every_url_the_apps_answer_is_still_there():
    r = routes()
    assert len(r) > 80
    _check("routes.txt", r)


# ------------------------------------------------------------ server jobs
def jobs():
    rows = []
    for u in sorted(BUNDLE.glob("*.service")):
        s = u.read_text(encoding="utf-8")
        ex = " ".join(re.findall(r"^ExecStart=(.*)$", s, re.M))
        scripts = re.findall(r"run_job\.sh \S+ (\S+\.py)", ex) or re.findall(r"([\w./-]+\.(?:py|sh))", ex)
        timer = u.with_suffix(".timer")
        cal = ";".join(re.findall(r"^OnCalendar=(.*)$", timer.read_text(encoding="utf-8"), re.M)) if timer.exists() else "(service)"
        rows.append((u.stem, cal, [pathlib.Path(x).name for x in scripts if "run_job" not in x]))
    return rows


def test_every_server_job_is_still_scheduled_the_same_way():
    _check("jobs.txt", [f"{unit}  |  {cal}  |  {' '.join(sc)}" for unit, cal, sc in jobs()])


def test_every_script_a_job_runs_exists():
    missing = []
    for unit, _, scripts in jobs():
        for sc in scripts:
            if not any((V2 / d / sc).exists() for d in ("scripts", "server/bundle", "server", "pi")):
                missing.append(f"{unit}: {sc}")
    assert not missing, missing


def test_every_server_job_is_run_by_a_test_or_says_why_not():
    sys.path.insert(0, str(V2 / "tests" / "portal"))
    import importlib
    smoke = importlib.import_module("tests.portal.test_jobs_smoke")
    covered = {s + ".py" for s, _, _ in smoke.JOBS} | {k if "." in k else k + ".py" for k in smoke.NOT_RUNNABLE_HERE}
    orphans = []
    for unit, _, scripts in jobs():
        for sc in scripts:
            if sc not in covered and sc not in LONG_RUNNING and sc not in RUN_ELSEWHERE:
                orphans.append(f"{unit}: {sc}")
    assert not orphans, ("jobs neither run by tests/portal/test_jobs_smoke.py nor listed in its "
                         "NOT_RUNNABLE_HERE:\n  " + "\n  ".join(orphans))


# ------------------------------------------------------------ Pi jobs
def test_every_pi_job_is_still_scheduled_and_its_script_exists():
    lines = [ln.strip() for ln in (V2 / "pi" / "crontab.example").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    rows = []
    for ln in lines:
        m = re.search(r"/argia_v2/v2/(\S+\.(?:sh|py))", ln)
        if m:
            assert (V2 / m.group(1)).exists(), m.group(1)
            rows.append(f"{' '.join(ln.split()[:5])}  |  {m.group(1)}")
        else:
            rows.append(f"{' '.join(ln.split()[:5])}  |  {' '.join(ln.split()[5:7])}")
    _check("pi_jobs.txt", rows)


@pytest.mark.skipif(not UPDATE, reason="only when rewriting the contract files")
def test_contract_files_were_rewritten():
    assert all((HERE / f).exists() for f in ("routes.txt", "jobs.txt", "pi_jobs.txt"))


# ------------------------------------------------------------ the fleet list
def test_the_fleet_is_the_same_in_every_hardcoded_list():
    """v263: a new plant must be added in THREE code lists (report_gen's
    PPA/CAPEX, setup_app's PLANTS, portal_chrome's SLUGS) - miss one and the
    plant silently lacks a report page, a setup entry or a customer URL.
    Until that is refactored into one list, this test makes the three agree."""
    def literal(path, name):
        tree = ast.parse((V2 / path).read_text(encoding="utf-8"))
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == name for t in n.targets):
                return ast.literal_eval(n.value)
        raise AssertionError(f"{name} not found in {path}")
    report = set(literal("server/bundle/report_gen.py", "PPA")) | set(literal("server/bundle/report_gen.py", "CAPEX"))
    setup = {p.upper() for p in literal("server/bundle/setup_app.py", "PLANTS")}
    slugs = set(literal("server/bundle/portal_chrome.py", "SLUGS"))
    assert report == setup, f"report_gen vs setup_app: {sorted(report ^ setup)}"
    assert report <= slugs, f"no customer URL slug for: {sorted(report - slugs)}"
