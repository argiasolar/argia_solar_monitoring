"""v263 - a real, throwaway PostgreSQL for end-to-end tests.

The portal generators (portal_gen, report_gen, monitoring_gen) and the
jobs read PostgreSQL through ``runuser -u postgres -- psql -d argia_mont``.
Until v263 no test ran them at all - they were only checked by reading
their source text. This fixture makes them run for real:

1. initdb a private cluster in a temp dir (unix socket only, no TCP port,
   nothing else on the machine is touched);
2. load tests/fixtures/pg/schema.sql - the production DDL, no rows;
3. load tests/fixtures/pg/seed.sql - a synthetic fleet, dates relative to
   today in Mexico;
4. put a test-double ``runuser`` first on PATH that drops "-u <user> --"
   and runs psql against the private cluster (PGHOST/PGPORT/PGUSER).

No production code is changed for this: the code under test runs exactly
the command it runs on pio06.

Skips when PostgreSQL is not installed (the Windows laptop). CI sets
ARGIA_REQUIRE_PG=1, which turns that skip into a failure, so the portal
tests can never silently stop running there.
"""
from __future__ import annotations

import glob
import os
import pathlib
import shutil
import subprocess

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
PG_FIX = V2 / "tests" / "fixtures" / "pg"
PORT = "54329"

FAKE_RUNUSER = """#!/bin/sh
# test double: runuser -u <user> -- cmd args...  ->  cmd args... (against the private cluster)
while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
[ "$1" = "--" ] && shift
exec "$@"
"""


def _pg_bin():
    env = os.environ.get("PG_BIN")
    if env and pathlib.Path(env, "initdb").exists():
        return env
    cands = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"), reverse=True)
    if cands:
        return str(pathlib.Path(cands[0]).parent)
    w = shutil.which("initdb")
    return str(pathlib.Path(w).parent) if w else None


def _as_pg(cmd):
    """initdb/pg_ctl refuse to run as root; the sandbox runs tests as root."""
    if os.name != "nt" and os.geteuid() == 0:
        return ["/usr/sbin/runuser", "-u", "postgres", "--"] + cmd
    return cmd


@pytest.fixture(scope="session")
def pg_env(tmp_path_factory):
    """Environment (PATH + PG*) pointing every psql call at a seeded private cluster."""
    b = _pg_bin()
    if not b or shutil.which("psql") is None:
        if os.environ.get("ARGIA_REQUIRE_PG") == "1":
            pytest.fail("ARGIA_REQUIRE_PG=1 but no PostgreSQL binaries were found (set PG_BIN)")
        pytest.skip("PostgreSQL not installed - portal end-to-end tests skipped")
    # not under pytest's tmp dir: that one is 0700 and, when tests run as root,
    # the postgres user could not reach the data directory inside it
    import tempfile
    root = pathlib.Path(tempfile.mkdtemp(prefix="argia_pg_"))
    root.chmod(0o755)
    data, sock, bindir = root / "data", root / "sock", root / "bin"
    for d in (data, sock, bindir):
        d.mkdir()
    if os.name != "nt" and os.geteuid() == 0:
        shutil.chown(root, "postgres")
        for d in (data, sock):
            shutil.chown(d, "postgres")
    (bindir / "runuser").write_text(FAKE_RUNUSER)
    (bindir / "runuser").chmod(0o755)

    for cmd in ([f"{b}/initdb", "-D", str(data), "-A", "trust", "-U", "argia", "-E", "UTF8", "--locale=C.UTF-8"],
                [f"{b}/pg_ctl", "-D", str(data), "-l", str(root / "pg.log"), "-w",
                 "-o", f"-k {sock} -c listen_addresses='' -p {PORT} -c fsync=off", "start"]):
        r = subprocess.run(_as_pg(cmd), capture_output=True, text=True)
        assert r.returncode == 0, f"{cmd[0]} failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}"
    env = dict(os.environ)
    env.update(PGHOST=str(sock), PGPORT=PORT, PGUSER="argia", PGTZ="UTC",
               PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}")
    try:
        subprocess.run(["createdb", "argia_mont"], env=env, check=True, capture_output=True)
        for f in ("schema.sql", "seed.sql"):
            r = subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", "argia_mont", "-f", str(PG_FIX / f)],
                               env=env, capture_output=True, text=True)
            assert r.returncode == 0, f"{f} failed to load:\n{r.stderr[-2000:]}"
        # keep the seeded state as a template: fresh_db() restores it in ~0.1 s
        subprocess.run(["createdb", "-T", "argia_mont", "argia_seed"], env=env, check=True, capture_output=True)
        yield env
    finally:
        subprocess.run(_as_pg([f"{b}/pg_ctl", "-D", str(data), "-m", "immediate", "stop"]), capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


def reset_db(env):
    """Throw away whatever a test wrote and start again from the seed."""
    for cmd in (["dropdb", "--if-exists", "--force", "argia_mont"], ["createdb", "-T", "argia_seed", "argia_mont"]):
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


@pytest.fixture
def fresh_db(pg_env):
    """The seeded database, untouched by earlier tests."""
    reset_db(pg_env)
    return pg_env


@pytest.fixture(scope="session")
def psql(pg_env):
    """psql(sql) -> rows (list of lists of str), against the seeded cluster."""
    def run(sql):
        r = subprocess.run(["psql", "-d", "argia_mont", "-X", "-t", "-A", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
                           env=pg_env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return [ln.split("\t") for ln in r.stdout.splitlines() if ln.strip()]
    return run


@pytest.fixture(scope="session")
def portal_site(pg_env, tmp_path_factory):
    """Run server/bundle/portal_gen.py for real, IN this process (so coverage
    counts portal_gen, report_gen and monitoring_gen), into a temp web root.
    Returns (out_dir, stdout)."""
    import contextlib
    import io
    import runpy
    import sys

    reset_db(pg_env)
    out = tmp_path_factory.mktemp("www")
    saved_env, saved_argv, saved_path = dict(os.environ), list(sys.argv), list(sys.path)
    buf = io.StringIO()
    try:
        os.environ.update({k: pg_env[k] for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH")})
        sys.argv = [str(V2 / "server/bundle/portal_gen.py"), str(out)]
        for m in ("portal_gen", "report_gen", "monitoring_gen"):
            sys.modules.pop(m, None)
        with contextlib.redirect_stdout(buf):
            runpy.run_path(str(V2 / "server/bundle/portal_gen.py"), run_name="__main__")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        sys.argv, sys.path[:] = saved_argv, saved_path
    return out, buf.getvalue()
