"""v324 - security hygiene of ARGIA for Prologis: executables refused,
uploads blocked until the malware scan passes (clamscan run once per
batch; a fake clamscan stands in for ClamAV here), quarantine and the
desk mail, the privacy notice link, the honest security page, the
12-month web log rotation, and the CycloneDX SBOM of the server."""
from __future__ import annotations

import datetime as dt
import importlib
import io
import json
import os
import pathlib
import re
import stat
import subprocess
import sys

import pytest

from argia.prologis import alarms as AL
from argia.prologis import scan as SC
from argia.prologis import store as S
from argia.prologis import totp as TOTP

V2 = pathlib.Path(__file__).resolve().parents[2]
FIX = V2 / "tests" / "fixtures" / "prologis"
# A harmless marker stands in for malware: the real EICAR test string gets this
# test file quarantined by Windows Defender on the laptop (found 10 Oct).
MARKER = b"ARGIA-FAKE-MALWARE-MARKER-FOR-TESTS"
sys.path.insert(0, str(V2 / "scripts"))


@pytest.fixture
def c(tmp_path):
    con = S.connect(str(tmp_path / "pl.db"))
    yield con
    con.close()


# ------------------------------------------------------------------ scan rules
def test_executables_are_recognised():
    assert SC.executable_kind(b"MZ\x90\x00rest") == "Windows executable"
    assert SC.executable_kind(b"\x7fELF\x02") == "Linux executable"
    assert SC.executable_kind(b"#!/bin/sh\nrm -rf") == "script"
    assert SC.executable_kind(b"\xcf\xfa\xed\xfe....") == "macOS executable"
    for ok in (b"%PDF-1.7", b"\x89PNG\r\n", b"PK\x03\x04", b"site,kwh\n", b""):
        assert SC.executable_kind(ok) is None


def test_parse_clamscan_output():
    found, errors = SC.parse_output("/x/ab12: Test.Marker-1 FOUND\n/x/cd34: Can't open file or directory ERROR\nnoise\n")
    assert found == {"/x/ab12": "Test.Marker-1"} and errors == {"/x/cd34": "Can't open file or directory"}


def _doc(c, files, name, data, site="TST001"):
    return S.add_document(c, str(files), site, "datasheets", name, data, "application/pdf", "op")


class FakeRun:
    """Stands in for subprocess.run(clamscan ...): files holding the test
    marker are FOUND; the exit code follows clamscan (0 clean, 1 found, 2 error)."""

    def __init__(self, rc=None, unreadable=()):
        self.calls = []
        self.rc = rc
        self.unreadable = set(unreadable)

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        lines, found = [], False
        for p in cmd[4:]:
            if os.path.basename(p) in self.unreadable:
                lines.append(f"{p}: Can't open file or directory ERROR")
                continue
            if MARKER in open(p, "rb").read():
                lines.append(f"{p}: Test.Marker-1 FOUND")
                found = True
        rc = self.rc if self.rc is not None else (2 if self.unreadable else (1 if found else 0))
        return subprocess.CompletedProcess(cmd, rc, "\n".join(lines) + "\n", "")


def test_scan_pending_clean_infected_quarantine(c, tmp_path):
    files, quar = tmp_path / "files", tmp_path / "quarantine"
    good = _doc(c, files, "datasheet.pdf", b"%PDF-1.7 ok")
    bad = _doc(c, files, "invoice.pdf", MARKER)
    dup = _doc(c, files, "copy_of_invoice.pdf", MARKER, site="TST002")       # same content, second record
    assert {r["scan_status"] for r in c.execute("SELECT scan_status FROM documents")} == {"pending"}
    run = FakeRun()
    out = SC.scan_pending(c, str(files), str(quar), run=run, which=lambda x: "/usr/bin/clamscan")
    assert out == {"pending": 3, "clean": 1, "infected": 2, "errors": 0, "missing": 0}
    assert len(run.calls) == 1 and run.calls[0][:4] == SC.SCAN_CMD                 # one batch, one database load
    st = {r["id"]: (r["scan_status"], r["scan_detail"]) for r in c.execute("SELECT * FROM documents")}
    assert st[good] == ("clean", "") and st[bad] == ("infected", "Test.Marker-1") and st[dup][0] == "infected"
    sha = c.execute("SELECT sha256 FROM documents WHERE id=?", (bad,)).fetchone()[0]
    assert (quar / sha).exists() and not (files / sha[:2] / sha).exists()
    if os.name != "nt":                                                         # no POSIX modes on Windows
        assert stat.S_IMODE(os.stat(quar / sha).st_mode) == 0o600
    assert len(c.execute("SELECT * FROM audit WHERE action='upload_infected'").fetchall()) == 2
    assert SC.scan_pending(c, str(files), str(quar), run=run, which=lambda x: "x")["pending"] == 0
    subj, body = SC.infected_mail(c.execute("SELECT * FROM documents WHERE scan_status='infected'").fetchall())
    assert "2 upload(s)" in subj and "invoice.pdf" in body and "Test.Marker-1" in body


def test_scan_fails_closed(c, tmp_path):
    files, quar = tmp_path / "files", tmp_path / "q"
    a = _doc(c, files, "a.pdf", b"%PDF a")
    b = _doc(c, files, "b.pdf", b"%PDF b")
    out = SC.scan_pending(c, str(files), str(quar), run=FakeRun(), which=lambda x: None)
    assert "not installed" in out["skipped"]
    assert {r[0] for r in c.execute("SELECT scan_status FROM documents")} == {"pending"}          # nothing passes unscanned
    sha_b = c.execute("SELECT sha256 FROM documents WHERE id=?", (b,)).fetchone()[0]
    out = SC.scan_pending(c, str(files), str(quar), run=FakeRun(unreadable={sha_b}), which=lambda x: "x")
    st = dict(c.execute("SELECT id, scan_status FROM documents").fetchall())
    assert st[b] == "error" and st[a] == "pending" and out["errors"] == 1          # exit 2: unnamed files are retried
    out = SC.scan_pending(c, str(files), str(quar), run=FakeRun(rc=40), which=lambda x: "x")
    assert out["skipped"] == "scanner exit 40" and dict(c.execute("SELECT id, scan_status FROM documents").fetchall())[a] == "pending"
    os.remove(files / c.execute("SELECT sha256 FROM documents WHERE id=?", (a,)).fetchone()[0][:2] /
              c.execute("SELECT sha256 FROM documents WHERE id=?", (a,)).fetchone()[0])
    assert SC.scan_pending(c, str(files), str(quar), run=FakeRun(), which=lambda x: "x")["missing"] == 1


def test_privacy_setting_and_digest_line(c, tmp_path):
    for bad in ("http://argia.com.mx/aviso", "argia.com.mx", "https://x", "javascript:alert(1)", 'https://a.com/"><script>'):
        with pytest.raises(ValueError):
            AL.set_setting(c, "privacy_url", bad, "ad")
    AL.set_setting(c, "privacy_url", "https://argia.com.mx/aviso-de-privacidad", "ad")
    assert AL.setting(c, "privacy_url") == "https://argia.com.mx/aviso-de-privacidad"
    AL.set_setting(c, "privacy_url", "", "ad")                                      # can be cleared
    _doc(c, tmp_path / "f", "a.pdf", b"%PDF")
    c.execute("UPDATE documents SET uploaded_utc='2026-10-10 10:00:00'")
    c.commit()
    from argia.prologis import registry as R
    rg = R.load(str(FIX / "registry.json"))
    body = AL.digest(c, rg, dt.datetime(2026, 10, 10, 7, 30), "x")[1]                 # 13:30 UTC: 3.5 h pending
    assert "Uploads waiting for the malware scan for over 1 h: 1" in body


# ------------------------------------------------------------------ SBOM
DPKG = ("nginx\t1.26.3-3\tamd64\tii \npython3-flask\t3.1.1-1+deb13u1\tall\tii \nold-pkg\t1.0\tamd64\trc \n"
        "libssl3t64\t3.5.1-1\tamd64\tii \nbash\t5.2.37-2+b5\tamd64\tii \nepoch-pkg\t1:2.3~rc1-1\tamd64\tii \nbroken line\n")


def test_sbom_from_dpkg_and_python():
    import server_sbom as SB
    debs = SB.parse_dpkg(DPKG)
    assert [d["name"] for d in debs] == ["nginx", "python3-flask", "libssl3t64", "bash", "epoch-pkg"]     # 'rc' = removed: skipped
    doc = SB.bom(debs, [{"name": "Requests", "version": "2.34.2"}, {"name": "urllib3", "version": "2.8.0"}], "b88d0ac", "pio06",
                 now=dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc))
    assert doc["bomFormat"] == "CycloneDX" and doc["specVersion"] == "1.5" and doc["metadata"]["component"]["version"] == "b88d0ac"
    purls = {x["name"]: x["purl"] for x in doc["components"]}
    assert purls["python3-flask"] == "pkg:deb/debian/python3-flask@3.1.1-1%2Bdeb13u1?arch=all"
    assert purls["epoch-pkg"] == "pkg:deb/debian/epoch-pkg@1%3A2.3~rc1-1?arch=amd64"
    assert purls["Requests"] == "pkg:pypi/requests@2.34.2"
    types = {x["name"]: x["type"] for x in doc["components"]}
    assert types["libssl3t64"] == "library" and types["nginx"] == "application"
    assert len({x["bom-ref"] for x in doc["components"]}) == len(doc["components"])
    try:
        from cyclonedx.schema import SchemaVersion
        from cyclonedx.validation.json import JsonStrictValidator
    except ImportError:
        return
    assert JsonStrictValidator(SchemaVersion.V1_5).validate_str(json.dumps(doc)) is None


def test_sbom_main_writes_the_file(tmp_path, monkeypatch, capsys):
    import server_sbom as SB
    monkeypatch.setattr(SB.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, DPKG if cmd[0] == "dpkg-query" else "abc1234\n", ""))
    assert SB.main(["--out", str(tmp_path / "x" / "sbom.json")]) == 0
    doc = json.load(open(tmp_path / "x" / "sbom.json"))
    assert doc["metadata"]["component"]["version"] == "abc1234" and len(doc["components"]) > 5
    assert re.search(r"SBOM: \d+ components \(5 Debian packages, \d+ Python distributions\)", capsys.readouterr().out)


# ------------------------------------------------------------------ deployment files
def test_web_logs_kept_twelve_months():
    lr = (V2 / "server" / "bundle" / "argia-prologis.logrotate").read_text()
    assert "/var/log/argia-prologis/*.log" in lr and re.search(r"^\s*rotate 400$", lr, re.M) and "daily" in lr
    vh = (V2 / "server" / "bundle" / "prologis.argia.com.mx.conf").read_text()
    assert "access_log /var/log/argia-prologis/access.log;" in vh and "/var/log/nginx/prologis" not in vh
    sys.path.insert(0, str(V2 / "scripts"))
    import drift_check as D
    assert ("v2/server/bundle/argia-prologis.logrotate", "/etc/logrotate.d/argia-prologis") in D.EXPLICIT
    assert D.unmapped(os.listdir(V2 / "server" / "bundle")) == []


def test_security_workflow_audits_the_pinned_requirements():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((V2.parent / ".github" / "workflows" / "v2-security.yml").read_text())
    runs = " ".join(st.get("run", "") for st in wf["jobs"]["audit"]["steps"])
    assert "pip-audit -r v2/requirements.lock" in runs and "cyclonedx-py requirements v2/requirements.lock" in runs
    assert "set -o pipefail" in runs                                       # a finding fails the run
    lock = (V2 / "requirements.lock").read_text()
    assert "urllib3==2.8.0" in lock


# ------------------------------------------------------------------ the web flow
NEW_PW = "Sunny-rooftops-2026"


@pytest.fixture
def env(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    sys.path.insert(0, str(V2 / "server" / "bundle"))
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX / "registry.json"))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    # a fake clamscan on PATH (the marker -> FOUND), so the CLI runs end to end
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "clamscan").write_text("#!/bin/sh\nrc=0\nfor f in \"$@\"; do case \"$f\" in --*) continue;; esac\n"
                                   "if grep -q ARGIA-FAKE-MALWARE-MARKER \"$f\"; then echo \"$f: Test.Marker-1 FOUND\"; rc=1; fi; done\nexit $rc\n")
    (bin_ / "clamscan").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    import prologis_app
    app_mod = importlib.reload(prologis_app)
    app_mod.app.config["TESTING"] = True
    con = S.connect(str(tmp_path / "pl.db"))
    pws = {r: S.create_user(con, r, r.title() + " User", f"{r}@x.test", "Prologis", r, "test") for r in S.ROLES}
    con.close()
    return app_mod, pws, tmp_path


def _csrf(h):
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', h)
    return m.group(1) if m else ""


def sign_in(app_mod, user, pw):
    cl = app_mod.app.test_client()
    cl.post("/login", data={"username": user, "password": pw, "next": "/"})
    page = cl.get("/mfa").get_data(as_text=True)
    secret = re.search(r'name="pending" value="([A-Z2-7]+)"', page).group(1)
    cl.post("/mfa", data={"code": TOTP.code_at(secret, int(__import__("time").time() // 30)), "pending": secret, "csrf": _csrf(page), "next": "/"})
    page = cl.get("/password").get_data(as_text=True)
    cl.post("/password", data={"pw1": NEW_PW, "pw2": NEW_PW, "csrf": _csrf(page)})
    assert cl.get("/").status_code == 200
    return cl


@pytest.mark.skipif(os.name == "nt", reason="the fake clamscan is a POSIX shell script")
def test_upload_scan_download_flow(env, capsys):
    app_mod, pws, tmp = env
    mgr = sign_in(app_mod, "manager", pws["manager"])
    tok = _csrf(mgr.get("/docs/").get_data(as_text=True))

    def up(name, data):
        return mgr.post("/docs/upload", data={"site": "TST001", "folder": "datasheets", "file": (io.BytesIO(data), name), "csrf": tok},
                        content_type="multipart/form-data")
    assert up("manual.pdf", b"MZ\x90\x00 really an exe").status_code == 415
    assert up("run.txt", b"#!/bin/bash\necho hi").status_code == 415
    assert up("manual.pdf", b"%PDF-1.7 fine").status_code == 302
    assert up("report.pdf", MARKER).status_code == 302
    h = mgr.get("/docs/").get_data(as_text=True)
    assert h.count("being scanned") == 2 and "executables are refused" in h
    con = S.connect(str(tmp / "pl.db"))
    ids = dict(con.execute("SELECT name, id FROM documents").fetchall())
    r = mgr.get(f"/docs/{ids['manual.pdf']}/download")
    assert r.status_code == 409 and "waiting for the malware scan" in r.get_data(as_text=True)
    assert app_mod.main(["x", "--scan-uploads"]) == 0
    out = capsys.readouterr().out
    assert "upload scan: pending=2, clean=1, infected=1" in out
    r = mgr.get(f"/docs/{ids['manual.pdf']}/download")
    assert r.status_code == 200 and r.data == b"%PDF-1.7 fine"
    r = mgr.get(f"/docs/{ids['report.pdf']}/download")
    assert r.status_code == 409 and "quarantined" in r.get_data(as_text=True) and "Test.Marker-1" in r.get_data(as_text=True)
    assert "quarantined" in mgr.get("/docs/").get_data(as_text=True)
    m = con.execute("SELECT * FROM outbox WHERE kind='malware'").fetchone()
    assert m and "report.pdf" in m["body"] and m["status"] == "no_recipient"          # no desk address set in the test
    acts = [x["action"] for x in con.execute("SELECT action FROM audit")]
    assert acts.count("upload_refused") == 2 and "doc_download_blocked" in acts and "upload_infected" in acts


def test_scan_cli_skips_a_locked_store(tmp_path, monkeypatch, capsys):
    pytest.importorskip("flask")
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "locked" / "prologis.db"))
    sys.path.insert(0, str(V2 / "server" / "bundle"))
    import prologis_app
    app_mod = importlib.reload(prologis_app)
    assert app_mod.main(["x", "--scan-uploads"]) == 0
    assert "upload scan skipped" in capsys.readouterr().out and not (tmp_path / "locked").exists()


def test_privacy_link_and_honest_security_page(env, monkeypatch):
    app_mod, pws, tmp = env
    ad = sign_in(app_mod, "admin", pws["admin"])
    h = ad.get("/security/").get_data(as_text=True)
    assert "Privacy notice</a>" not in h and "Encryption at rest" not in h             # no link yet; not claimed on a plain folder
    assert "Malware-checked uploads" in h and "Access logs kept 12 months" in h
    tok = _csrf(h)
    assert ad.post("/security/privacy", data={"url": "http://argia.com.mx/a", "csrf": tok}).status_code == 400
    ad.post("/security/privacy", data={"url": "https://argia.com.mx/aviso-de-privacidad", "csrf": tok})
    for path in ("/", "/security/"):
        assert 'href="https://argia.com.mx/aviso-de-privacidad"' in ad.get(path).get_data(as_text=True), path
    anon = app_mod.app.test_client()
    assert "aviso-de-privacidad" in anon.get("/login").get_data(as_text=True)
    monkeypatch.setattr(app_mod.os.path, "ismount", lambda p: p == app_mod.DATA_DIR)
    assert "Encryption at rest" in ad.get("/security/").get_data(as_text=True)
    mgr = sign_in(app_mod, "manager", pws["manager"])
    assert mgr.post("/security/privacy", data={"url": "https://evil.example/x", "csrf": _csrf(mgr.get("/alarms/").get_data(as_text=True))}).status_code == 403
    ad.get("/lang/es")
    h = ad.get("/security/").get_data(as_text=True)
    assert "Aviso de privacidad" in h and "Cargas revisadas contra malware" in h and chr(0x2014) not in h
