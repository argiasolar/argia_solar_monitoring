"""v319: encryption at rest for ARGIA for Prologis (questionnaire: encrypted
backups by 31 Oct, an encrypted volume for the Prologis data store).

Tomasz, 9 Oct: "prologis.argia.com.mx should have encrypted data on server".
Decision: the volume key lives on the office Pi, never on the server; the Pi
unlocks after a reboot. Backups are sealed with age for two offline keys.

The shell scripts run for real. Root-only tools (cryptsetup, mount, chattr,
systemctl) are replaced by small shims that keep their state in files, so the
init / rollback / unlock logic is exercised end to end without a kernel.
``age`` is the real binary where installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
VAULT = V2 / "server" / "bundle" / "prologis_vault.sh"
BACKUP = V2 / "server" / "bundle" / "db_backup.sh"
PULL = V2 / "pi" / "db_backups" / "pull_backup.sh"
UNLOCK = V2 / "pi" / "prologis_vault" / "unlock.sh"
SETUP = V2 / "pi" / "prologis_vault" / "setup_keys.sh"
KEY = "ab" * 32

linux = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="runs the real shell scripts (Linux CI)")
needs_age = pytest.mark.skipif(not shutil.which("age") or not shutil.which("age-keygen"), reason="age not installed")


# ------------------------------------------------------------------ shims for the vault
SHIMS = {
    # cryptsetup luksFormat|open|close: the container file holds sha256(key); open checks it
    "cryptsetup": r'''#!/bin/bash
S="$SHIM_STATE"; echo "cryptsetup $*" >> "$S/calls"
case "$1" in
  luksFormat) c="${@: -1}"; k=$(cat); [ -n "$SHIM_FAIL_FORMAT" ] && exit 1
              printf '%s' "$k" | sha256sum | cut -d' ' -f1 > "$c" ;;
  open) c="${@: -2:1}"; m="${@: -1}"; k=$(cat)
        [ "$(printf '%s' "$k" | sha256sum | cut -d' ' -f1)" = "$(cat "$c")" ] || exit 2
        mkdir -p "$S/vol"; touch "$PL_VAULT_MAPPER_DIR/$m" ;;
  close) rm -f "$PL_VAULT_MAPPER_DIR/$2" ;;
esac''',
    "mkfs.ext4": '#!/bin/bash\necho "mkfs $*" >> "$SHIM_STATE/calls"\nrm -rf "$SHIM_STATE/vol"; mkdir -p "$SHIM_STATE/vol/lost+found"',
    # mount DEV TARGET: the volume's files appear in TARGET; umount puts them back
    "mount": r'''#!/bin/bash
S="$SHIM_STATE"; t="${@: -1}"; echo "mount $*" >> "$S/calls"
[ -e "$PL_VAULT_MAPPER_DIR/argia_prologis" ] || exit 32
cp -a "$S/vol/." "$t/" && echo "$t" >> "$S/mounted"''',
    "umount": r'''#!/bin/bash
S="$SHIM_STATE"; t="$1"; echo "umount $*" >> "$S/calls"
rm -rf "$S/vol"; mkdir -p "$S/vol"; cp -a "$t/." "$S/vol/"; find "$t" -mindepth 1 -delete
grep -vxF "$t" "$S/mounted" > "$S/m2"; mv "$S/m2" "$S/mounted"''',
    "mountpoint": '#!/bin/bash\nt="${@: -1}"; grep -qxF "$t" "$SHIM_STATE/mounted" 2>/dev/null',
    "chattr": '#!/bin/bash\necho "chattr $*" >> "$SHIM_STATE/calls"',
    "fallocate": '#!/bin/bash\n: > "${@: -1}"',
    "systemctl": r'''#!/bin/bash
echo "systemctl $*" >> "$SHIM_STATE/calls"
case "$1" in start) echo active > "$SHIM_STATE/svc" ;; stop) echo inactive > "$SHIM_STATE/svc" ;;
  is-active) cat "$SHIM_STATE/svc" 2>/dev/null || echo inactive ;; esac''',
    "curl": '#!/bin/bash\nif [ -n "$SHIM_HEALTH_FAIL" ]; then echo 502; else echo 200; fi',
}


def vault_env(tmp_path):
    shims = tmp_path / "shims"
    shims.mkdir()
    for name, body in SHIMS.items():
        p = shims / name
        p.write_text(body + "\n")
        p.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "mounted").write_text("")
    (state / "svc").write_text("active\n")
    mapper = tmp_path / "mapper"
    mapper.mkdir()
    mnt = tmp_path / "prologis"
    mnt.mkdir()
    db = sqlite3.connect(mnt / "prologis.db")
    db.execute("CREATE TABLE users (u TEXT)")
    db.execute("INSERT INTO users VALUES ('tomasz')")
    db.commit()
    db.close()
    (mnt / "registry.json").write_text('{"sites": []}')
    (mnt / "files").mkdir()
    (mnt / "files" / "asbuilt.pdf").write_bytes(b"%PDF-1.4 as-built")
    (mnt / "secret.key").write_bytes(os.urandom(32))
    env = dict(os.environ, PATH=f"{shims}:{os.environ['PATH']}", SHIM_STATE=str(state),
               PL_VAULT_CONTAINER=str(tmp_path / "prologis.luks"), PL_VAULT_MAPPER_DIR=str(mapper),
               PL_VAULT_MNT=str(mnt), PL_VAULT_PLAIN_DIR=str(tmp_path), PL_VAULT_LOG=str(tmp_path / "vault.log"),
               ARGIA_BACKUP_RECIPIENTS=str(tmp_path / "recipients.txt"))
    env.pop("SSH_ORIGINAL_COMMAND", None)
    return env, state, mnt


def vault(env, *args, stdin="", ssh=None):
    e = dict(env)
    if ssh is not None:
        e["SSH_ORIGINAL_COMMAND"] = ssh
    return subprocess.run(["bash", str(VAULT), *args], env=e, input=stdin, capture_output=True, text=True, timeout=60)


def tree(d: pathlib.Path):
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file() and not p.name.endswith(("-wal", "-shm"))}


@linux
class TestVault:
    def test_init_moves_the_data_onto_the_encrypted_volume(self, tmp_path):
        env, state, mnt = vault_env(tmp_path)
        before = tree(mnt)
        r = vault(env, "init", stdin=KEY + "\n")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "init OK: 4 files" in r.stdout
        assert tree(mnt) == before                                   # same files, same bytes, now on the volume
        assert str(mnt) in (state / "mounted").read_text().split()
        calls = (state / "calls").read_text()
        assert "luksFormat --batch-mode --type luks2" in calls and f"chattr +i {mnt}" in calls
        assert calls.index("systemctl stop") < calls.index("systemctl start")
        rollback = list(tmp_path.glob("prologis_plain_rollback_*"))
        assert len(rollback) == 1 and tree(rollback[0]) == before    # the plain copy waits for finalize
        assert (tmp_path / "prologis.luks").read_text().strip() == hashlib.sha256(KEY.encode()).hexdigest()

    def test_init_twice_does_nothing(self, tmp_path):
        env, state, mnt = vault_env(tmp_path)
        assert vault(env, "init", stdin=KEY).returncode == 0
        r = vault(env, "init", stdin=KEY)
        assert r.returncode == 1 and "already initialised" in r.stdout

    @pytest.mark.parametrize("bad", ["", "short", "AB" * 32, "ab" * 31 + "zz", "ab" * 33])
    def test_a_malformed_key_is_refused_before_anything_happens(self, tmp_path, bad):
        env, state, mnt = vault_env(tmp_path)
        r = vault(env, "init", stdin=bad)
        assert r.returncode == 1 and "64 hex characters" in r.stdout
        assert not (tmp_path / "prologis.luks").exists() and (mnt / "prologis.db").exists()

    def test_a_failed_health_check_rolls_everything_back(self, tmp_path):
        env, state, mnt = vault_env(tmp_path)
        before = tree(mnt)
        r = vault(dict(env, SHIM_HEALTH_FAIL="1"), "init", stdin=KEY)
        assert r.returncode == 1 and "rolling back" in r.stdout
        assert tree(mnt) == before                                   # the plain directory is back in place
        assert not (tmp_path / "prologis.luks").exists()
        assert (state / "svc").read_text().strip() == "active"       # the platform runs again
        assert not list(tmp_path.glob("prologis_plain_rollback_*"))

    def test_a_failed_format_leaves_the_platform_untouched(self, tmp_path):
        env, state, mnt = vault_env(tmp_path)
        r = vault(dict(env, SHIM_FAIL_FORMAT="1"), "init", stdin=KEY)
        assert r.returncode == 1 and not (tmp_path / "prologis.luks").exists()
        assert "systemctl stop" not in (state / "calls").read_text()

    def test_unlock_after_a_reboot_and_idempotent(self, tmp_path):
        env, state, mnt = vault_env(tmp_path)
        before = tree(mnt)
        assert vault(env, "init", stdin=KEY).returncode == 0
        assert vault(env, "lock").returncode == 0                    # what a reboot leaves behind
        assert not any(mnt.iterdir()) and vault(env, "check").returncode == 1
        r = vault(env, "unlock", stdin=KEY)
        assert r.returncode == 0 and "unlocked and mounted" in r.stdout
        assert tree(mnt) == before and vault(env, "check").returncode == 0
        again = vault(env, "unlock", stdin=KEY)
        assert again.returncode == 0 and "already unlocked" in again.stdout

    def test_a_wrong_key_does_not_open(self, tmp_path):
        env, state, mnt = vault_env(tmp_path)
        assert vault(env, "init", stdin=KEY).returncode == 0
        vault(env, "lock")
        r = vault(env, "unlock", stdin="cd" * 32)
        assert r.returncode == 1 and "wrong key" in r.stdout and not any(mnt.iterdir())

    def test_check_refuses_an_empty_directory_and_allows_the_old_layout(self, tmp_path):
        env, state, mnt = vault_env(tmp_path)
        assert vault(env, "check").returncode == 0                   # before init: the plain store as today
        for p in sorted(mnt.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        r = vault(env, "check")
        assert r.returncode == 1 and "refusing to start on an empty directory" in r.stdout

    @pytest.mark.parametrize("cmd", ["rm -rf /", "bash", "lock", "check", "status; id", "unlock && id", ""])
    def test_the_forced_command_runs_only_its_verbs(self, tmp_path, cmd):
        env, state, mnt = vault_env(tmp_path)
        r = vault(env, "ssh", ssh=cmd)
        if cmd.split(";")[0].strip() == "status" or cmd.startswith("unlock"):
            return                                                   # first word is a verb; the rest is ignored
        assert r.returncode == 1 and "refused" in r.stdout

    def test_status_over_ssh(self, tmp_path):
        env, state, mnt = vault_env(tmp_path)
        r = vault(env, "ssh", ssh="status")
        assert r.returncode == 0 and "initialised: no" in r.stdout

    def test_finalize_shreds_the_plain_copy(self, tmp_path):
        env, state, mnt = vault_env(tmp_path)
        assert vault(env, "init", stdin=KEY).returncode == 0
        r = vault(env, "ssh", ssh="finalize")
        assert r.returncode == 0 and "1 plain rollback copy(ies) shredded" in r.stdout
        assert not list(tmp_path.glob("prologis_plain_rollback_*"))


@linux
class TestRecipients:
    GOOD = "age1" + "q" * 58

    def test_valid_keys_with_comments_are_stored(self, tmp_path):
        env, _, _ = vault_env(tmp_path)
        r = vault(env, "ssh", ssh="recipients", stdin=f"# tomasz\n{self.GOOD}\n# vit\nage1{'p' * 58}\n")
        assert r.returncode == 0 and "2 key(s)" in r.stdout
        assert (tmp_path / "recipients.txt").read_text().split() == [self.GOOD, "age1" + "p" * 58]

    @pytest.mark.parametrize("bad", ["", "age1short", "ssh-ed25519 AAAA", "age1" + "B" * 58, "age1" + "b" * 58,
                                     "\n".join(["age1" + "q" * 58] * 6)])
    def test_anything_else_changes_nothing(self, tmp_path, bad):
        env, _, _ = vault_env(tmp_path)
        (tmp_path / "recipients.txt").write_text("keep\n")
        r = vault(env, "recipients", stdin=bad)
        assert r.returncode == 1 and (tmp_path / "recipients.txt").read_text() == "keep\n"


# ------------------------------------------------------------------ backups
def identities(tmp_path, n=2):
    ids, pubs = [], []
    for i in range(n):
        p = tmp_path / f"id{i}.txt"
        subprocess.run(["age-keygen", "-o", str(p)], capture_output=True, check=True)
        ids.append(p)
        pubs.append(subprocess.run(["age-keygen", "-y", str(p)], capture_output=True, text=True, check=True).stdout.strip())
    return ids, pubs


def backup_env(tmp_path, recipients=None, prologis=True, locked=False):
    out = tmp_path / "bk"
    auth = tmp_path / "users.db"
    sqlite3.connect(auth).execute("CREATE TABLE u (x)").connection.commit()
    pl = tmp_path / "pl"
    pl.mkdir()
    if prologis and not locked:
        c = sqlite3.connect(pl / "prologis.db")
        c.execute("CREATE TABLE t (x)")
        c.commit()
        c.close()
        (pl / "registry.json").write_text("{}")
    fake_dump = tmp_path / "fake.dump"
    fake_dump.write_bytes(b"PGDMP" + os.urandom(150_000))
    shims = tmp_path / "bshims"
    shims.mkdir()
    (shims / "pg_restore").write_text("#!/bin/bash\nhead -c 5 \"${@: -1}\" | grep -q PGDMP\n")
    (shims / "mountpoint").write_text(f"#!/bin/bash\n[ \"{'' if locked else 'x'}\" = x ]\n")
    for p in shims.iterdir():
        p.chmod(0o755)
    rec = tmp_path / "recipients.txt"
    if recipients:
        rec.write_text("# holders\n" + "\n".join(recipients) + "\n")
    container = tmp_path / "prologis.luks"
    if locked:
        container.write_text("x")
    env = dict(os.environ, PATH=f"{shims}:{os.environ['PATH']}", ARGIA_BACKUP_DIR=str(out),
               ARGIA_BACKUP_RECIPIENTS=str(rec), ARGIA_PL_DIR=str(pl), PL_VAULT_CONTAINER=str(container),
               ARGIA_AUTH_DB=str(auth), ARGIA_PG_DUMP_CMD=f"cat {fake_dump}", ARGIA_PORTFOLIO_CMD="true")
    return env, out, fake_dump


def run_backup(env):
    return subprocess.run(["bash", str(BACKUP)], env=env, capture_output=True, text=True, timeout=60)


@linux
@needs_age
class TestServerBackup:
    def test_sealed_backups_decrypt_with_either_offline_key(self, tmp_path):
        ids, pubs = identities(tmp_path)
        env, out, fake = backup_env(tmp_path, pubs)
        r = run_backup(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "encrypted)" in r.stdout
        names = sorted(p.name for p in out.iterdir() if p.is_file())
        assert not [n for n in names if n.endswith((".dump", ".db", ".tar.gz"))]          # nothing plain at rest
        for ident in ids:
            plain = subprocess.run(["age", "-d", "-i", str(ident), str(out / "argia_mont_latest.dump.age")],
                                   capture_output=True, check=True).stdout
            assert plain == fake.read_bytes()
        m = json.loads((out / "backup_manifest_latest.json").read_text())
        assert m["encrypted"] is True and set(m["files"]) == {"dump", "users", "prologis"}
        assert m["files"]["dump"]["checks"] == ["pgdmp", "size", "pg_restore-list"]
        for k, f in m["files"].items():
            assert hashlib.sha256((out / f["name"]).read_bytes()).hexdigest() == f["sha256"]

    def test_without_recipients_the_backup_is_written_as_before(self, tmp_path):
        env, out, fake = backup_env(tmp_path)
        r = run_backup(env)
        assert r.returncode == 0 and "NOT ENCRYPTED" in r.stdout
        assert (out / "argia_mont_latest.dump").read_bytes() == fake.read_bytes()
        assert json.loads((out / "backup_manifest_latest.json").read_text())["encrypted"] is False

    def test_switching_on_seals_the_plain_copies_left_from_before(self, tmp_path):
        ids, pubs = identities(tmp_path, 1)
        env, out, _ = backup_env(tmp_path)
        assert run_backup(env).returncode == 0
        (out / "argia_mont_20261001.dump").write_bytes(b"PGDMP old")
        (tmp_path / "recipients.txt").write_text(pubs[0] + "\n")
        assert run_backup(env).returncode == 0
        names = sorted(p.name for p in out.iterdir() if p.is_file())
        assert "argia_mont_20261001.dump.age" in names and "argia_mont_latest.dump" not in names
        assert not [n for n in names if n.endswith((".dump", ".db", ".tar.gz"))]

    def test_a_locked_vault_skips_only_the_prologis_archive(self, tmp_path):
        ids, pubs = identities(tmp_path, 1)
        env, out, _ = backup_env(tmp_path, pubs, locked=True)
        r = run_backup(env)
        assert r.returncode == 0 and "SKIPPED - the encrypted volume is locked" in r.stdout
        assert (out / "argia_mont_latest.dump.age").exists() and not list(out.glob("prologis_*"))

    def test_a_stub_dump_fails_the_backup(self, tmp_path):
        env, out, fake = backup_env(tmp_path)
        fake.write_bytes(b"PGDMP tiny")
        assert run_backup(env).returncode != 0


# ------------------------------------------------------------------ Pi pull
def pull_env(tmp_path, served: pathlib.Path, recipients=None):
    sftp = tmp_path / "fake_sftp"
    sftp.write_text(f'''#!/bin/bash
while read -r verb src dst; do
  [ "$verb" = get ] || continue
  case "$src" in *\\**) for f in {served}/$src; do [ -f "$f" ] && cp "$f" "$dst"; done ;;
    *) [ -f "{served}/$src" ] || exit 1; cp "{served}/$src" "$dst" ;; esac
done
''')
    sftp.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    rec = home / "recipients.txt"
    if recipients:
        rec.write_text("\n".join(recipients) + "\n")
    env = dict(os.environ, HOME=str(home), SFTP=str(sftp), BASE=str(home / "db_backups"), KEY=str(tmp_path / "k"),
               RECIPIENTS=str(rec), STAMP="20261010", ARGIA_PUSH="off")
    return env, home / "db_backups"


def run_pull(env):
    return subprocess.run(["bash", str(PULL)], env=env, capture_output=True, text=True, timeout=60)


@linux
@needs_age
class TestPiPull:
    def test_sealed_files_matching_the_manifest_are_kept(self, tmp_path):
        ids, pubs = identities(tmp_path, 1)
        env, out, _ = backup_env(tmp_path, pubs)
        assert run_backup(env).returncode == 0
        penv, base = pull_env(tmp_path, out)
        r = run_pull(penv)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "sealed, manifest OK" in r.stdout
        assert (base / "daily" / "argia_mont_20261010.dump.age").exists()
        assert (base / "daily" / "users_20261010.db.age").exists()
        assert (base / "prologis" / "prologis_20261010.tar.gz.age").exists()

    def test_a_file_that_does_not_match_the_manifest_is_rejected(self, tmp_path):
        ids, pubs = identities(tmp_path, 1)
        env, out, _ = backup_env(tmp_path, pubs)
        assert run_backup(env).returncode == 0
        m = json.loads((out / "backup_manifest_latest.json").read_text())
        m["files"]["dump"]["sha256"] = "0" * 64
        (out / "backup_manifest_latest.json").write_text(json.dumps(m))
        penv, base = pull_env(tmp_path, out)
        r = run_pull(penv)
        assert r.returncode == 1 and "does not match the server manifest" in r.stdout
        assert not list((base / "daily").glob("argia_mont_*"))

    def test_plain_backups_still_pull_and_old_copies_are_sealed_when_keys_exist(self, tmp_path):
        ids, pubs = identities(tmp_path, 1)
        env, out, fake = backup_env(tmp_path)
        assert run_backup(env).returncode == 0                       # server not switched yet
        penv, base = pull_env(tmp_path, out, recipients=pubs)
        r = run_pull(penv)
        assert r.returncode == 0, r.stdout + r.stderr
        daily = sorted(p.name for p in (base / "daily").iterdir())
        assert daily == ["argia_mont_20261010.dump.age", "users_20261010.db.age"]   # sealed on the Pi at once
        plain = subprocess.run(["age", "-d", "-i", str(ids[0]), str(base / "daily" / daily[0])],
                               capture_output=True, check=True).stdout
        assert plain == fake.read_bytes()


# ------------------------------------------------------------------ Pi unlock watchdog
def unlock_env(tmp_path, code="502"):
    calls = tmp_path / "ssh_calls"
    ssh = tmp_path / "fake_ssh"
    ssh.write_text(f'#!/bin/bash\necho "$* | $(cat)" >> "{calls}"\necho "unlocked and mounted; argia-prologis started"\n')
    curl = tmp_path / "fake_curl"
    curl.write_text(f"#!/bin/bash\necho {code}\n")
    for p in (ssh, curl):
        p.chmod(0o755)
    (tmp_path / "sshkey").write_text("k")
    (tmp_path / "vaultkey").write_text(KEY + "\n")
    env = dict(os.environ, SSH=str(ssh), CURL=str(curl), SSH_KEY=str(tmp_path / "sshkey"),
               VAULT_KEY=str(tmp_path / "vaultkey"), STAMP=str(tmp_path / "st"), NOW="1000000")
    return env, calls


@linux
class TestPiUnlock:
    def test_healthy_platform_nothing_happens(self, tmp_path):
        env, calls = unlock_env(tmp_path, "200")
        r = subprocess.run(["bash", str(UNLOCK)], env=env, capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout == "" and not calls.exists()

    def test_down_platform_gets_the_key_on_stdin(self, tmp_path):
        env, calls = unlock_env(tmp_path, "502")
        r = subprocess.run(["bash", str(UNLOCK)], env=env, capture_output=True, text=True)
        assert "HTTP 502 - vault unlock: unlocked and mounted" in r.stdout
        line = calls.read_text().strip()
        assert line.endswith(f"unlock | {KEY}") and "IdentitiesOnly=yes" in line

    def test_one_attempt_per_ten_minutes(self, tmp_path):
        env, calls = unlock_env(tmp_path, "502")
        subprocess.run(["bash", str(UNLOCK)], env=env, capture_output=True, text=True)
        r = subprocess.run(["bash", str(UNLOCK)], env=dict(env, NOW="1000300"), capture_output=True, text=True)
        assert r.stdout == "" and len(calls.read_text().splitlines()) == 1

    def test_not_set_up_silent(self, tmp_path):
        env, calls = unlock_env(tmp_path, "502")
        (tmp_path / "vaultkey").unlink()
        r = subprocess.run(["bash", str(UNLOCK)], env=env, capture_output=True, text=True)
        assert r.stdout == "" and not calls.exists()


@linux
@needs_age
def test_setup_keys_is_idempotent_and_prints_the_forced_command(tmp_path):
    home = tmp_path / "h"
    (home / ".ssh").mkdir(parents=True)
    shim = tmp_path / "sk"
    shim.mkdir()
    (shim / "ssh-keygen").write_text('#!/bin/bash\nf="${@: -1}"; echo PRIV > "$f"; echo "ssh-ed25519 AAAAtest argia-prologis-unlock" > "$f.pub"\n')
    (shim / "ssh-keygen").chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=f"{shim}:{os.environ['PATH']}")
    r1 = subprocess.run(["bash", str(SETUP)], env=env, capture_output=True, text=True)
    assert r1.returncode == 0, r1.stderr
    key = (home / ".argia_keys" / "prologis_luks.key").read_text().strip()
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    assert oct((home / ".argia_keys").stat().st_mode)[-3:] == "700"
    assert 'command="/opt/argia/bundle/prologis_vault.sh ssh",no-pty' in r1.stdout
    rec = (home / ".argia_keys" / "backup_recipients.txt").read_text()
    assert rec.count("age1") == 2 and "# tomasz" in rec and "# vit" in rec
    r2 = subprocess.run(["bash", str(SETUP)], env=env, capture_output=True, text=True)
    assert "kept" in r2.stdout and (home / ".argia_keys" / "prologis_luks.key").read_text().strip() == key
    assert (home / ".argia_keys" / "backup_recipients.txt").read_text() == rec


# ------------------------------------------------------------------ drift check
sys.path.insert(0, str(V2 / "scripts"))
import drift_check as DC  # noqa: E402


def test_drift_backup_encryption_states():
    plain = ["argia_mont_20261009.dump", "users_latest.db.age"]
    assert DC.encryption_checks([], False, False, False)[0]["ok"] is False
    enc = DC.encryption_checks(plain, True, True, True)[0]
    assert enc["ok"] is False and "argia_mont_20261009.dump" in enc["detail"]
    ok = DC.encryption_checks(["argia_mont_latest.dump.age", "users_latest.db.age", "prologis_latest.tar.gz.age",
                               "backup_manifest_latest.json", "portfolio_latest.json"], True, True, True)
    assert [c["ok"] for c in ok] == [True, True]


def test_drift_vault_states():
    assert "not on the encrypted volume" in DC.encryption_checks([], True, False, False)[1]["detail"]
    assert "locked" in DC.encryption_checks([], True, True, False)[1]["detail"]


def test_freshness_takes_the_newest_of_sealed_or_plain(tmp_path):
    a, b = tmp_path / "x.dump.age", tmp_path / "x.dump"
    a.write_text("1")
    b.write_text("2")
    os.utime(a, (1000, 1000))
    os.utime(b, (5000, 5000))
    assert DC.file_age_h((str(a), str(b)), now=5000 + 3600) == pytest.approx(1.0)
    assert DC.file_age_h((str(tmp_path / "missing"),), now=0) is None
    assert DC.file_age_h(str(b), now=5000 + 7200) == pytest.approx(2.0)


def test_findings_put_the_vault_and_encryption_in_the_smoke_group():
    from argia.alerts import monitor as MON
    rep = {"generated_utc": "2026-10-10T00:00:00Z", "findings": ["prologis-vault: encrypted volume locked",
                                                                 "backup-encrypted: no backup recipients"]}
    import datetime as dt
    al = MON.drift_alerts(rep, now=dt.datetime(2026, 10, 10, 1, tzinfo=dt.timezone.utc))
    assert [a.key for a in al] == ["smoke-fail"]


def test_the_platform_unit_checks_the_vault_before_starting():
    unit = (V2 / "server" / "bundle" / "argia-prologis.service").read_text()
    assert "ExecStartPre=/bin/bash /opt/argia/bundle/prologis_vault.sh check" in unit
    assert unit.index("ExecStartPre=") < unit.index("ExecStart=/usr/bin/python3")


def test_the_pi_watchdog_calls_the_unlock():
    rw = (V2 / "pi" / "report_watch" / "report_watch.sh").read_text()
    assert "prologis_vault/unlock.sh" in rw


def test_shell_scripts_are_executable_in_git():
    for p in (VAULT, UNLOCK, SETUP, PULL, BACKUP):
        out = subprocess.run(["git", "-C", str(V2), "ls-files", "-s", str(p)], capture_output=True, text=True).stdout
        assert out == "" or out.startswith("100755"), (p, out)
