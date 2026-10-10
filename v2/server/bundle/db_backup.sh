#!/usr/bin/env bash
# Nightly DB backup on pio06 (argia-dbdump.timer, 03:30 server time).
#
# Writes to /root/argia_backups/:
#   argia_mont_YYYYMMDD.dump[.age]   pg_dump -Fc of the monitoring DB
#   users_YYYYMMDD.db[.age]          sqlite online-backup of the auth DB
#   prologis_YYYYMMDD.tar.gz[.age]   v292: ARGIA for Prologis db + files (when present)
#   *_latest.*                       stable names the Pi pulls by
#   backup_manifest_latest.json      v319: sha256 + checks of tonight's files
# Keeps the newest 3 dated copies locally - the real retention lives
# on the Pi (14 daily + 8 weekly), pulled via a read-only SFTP key, so
# a compromised or wiped server cannot reach the Pi's copies.
#
# v319: encryption at rest. When the recipients file holds age public keys
# (prologis_vault.sh recipients), every file is checked in plain form first
# (PGDMP magic, size, pg_restore -l, sqlite integrity) and then sealed with
# age for those keys; the plain file is removed. The private keys are kept
# offline by two people - neither this server nor the Pi can decrypt. With
# no recipients file the backup is written as before (and the morning drift
# check reports it as not encrypted).
set -euo pipefail
OUT="${ARGIA_BACKUP_DIR:-/root/argia_backups}"
RECIPIENTS="${ARGIA_BACKUP_RECIPIENTS:-/opt/argia/backup_recipients.txt}"
PL_DIR="${ARGIA_PL_DIR:-/opt/argia/prologis}"
PL_CONTAINER="${PL_VAULT_CONTAINER:-/opt/argia/prologis.luks}"
AUTH_DB="${ARGIA_AUTH_DB:-/opt/argia/auth/users.db}"
PG_DUMP_CMD="${ARGIA_PG_DUMP_CMD:-runuser -u postgres -- pg_dump -Fc argia_mont}"
mkdir -p "$OUT" "$OUT/reports"
stamp="$(date +%Y%m%d)"
ENCRYPT=0
if [ -s "$RECIPIENTS" ]; then
  if command -v age >/dev/null; then ENCRYPT=1
  else echo "$(date -Is) WARNING: backup recipients set but 'age' is not installed - writing PLAIN backups"; fi
fi
declare -A CHECKS=()

# seal FILE: encrypt to FILE.age for the recipients and remove the plain
# file; without encryption the file stays as it is. Prints the final path.
seal() {
  if [ "$ENCRYPT" = 1 ]; then
    age -R "$RECIPIENTS" -o "$1.age.tmp" "$1"
    head -c 21 "$1.age.tmp" | grep -q "age-encryption.org/v1"
    mv "$1.age.tmp" "$1.age"
    rm -f "$1"
    echo "$1.age"
  else
    echo "$1"
  fi
}

# latest NAME DATED: the stable copy the Pi pulls; the other form (plain or
# sealed) of the same name is removed so only one "latest" exists
latest() {
  local base="$1" dated="$2" ext=""
  [[ "$dated" == *.age ]] && ext=".age"
  cp -f "$dated" "$OUT/$base$ext"
  if [ -n "$ext" ]; then rm -f "$OUT/$base"; else rm -f "$OUT/$base.age"; fi
  echo "$OUT/$base$ext"
}

tmp="$OUT/.argia_mont_$stamp.tmp"
$PG_DUMP_CMD > "$tmp"
head -c 5 "$tmp" | grep -q "PGDMP"           # custom-format magic
[ "$(stat -c%s "$tmp")" -ge 100000 ]         # a real dump, not a stub
if command -v pg_restore >/dev/null; then
  pg_restore -l "$tmp" >/dev/null             # v319: the archive lists cleanly (was the Pi's monthly test)
  CHECKS[dump]="pgdmp,size,pg_restore-list"
else
  CHECKS[dump]="pgdmp,size"
fi
plain_size=$(stat -c%s "$tmp")
mv "$tmp" "$OUT/argia_mont_$stamp.dump"
dump_file="$(seal "$OUT/argia_mont_$stamp.dump")"
dump_latest="$(latest argia_mont_latest.dump "$dump_file")"

# auth DB: sqlite online backup (safe while the app is running)
python3 - "$AUTH_DB" "$OUT/users_$stamp.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
dst.close(); src.close()
EOF
CHECKS[users]="sqlite-integrity"
users_file="$(seal "$OUT/users_$stamp.db")"
users_latest="$(latest users_latest.db "$users_file")"

# v292: ARGIA for Prologis - its own database (sqlite online backup) plus
# registry, uploaded files and brand, as one archive. Best-effort only when
# the platform exists; a failure here must not lose the main backup.
# v319: on the encrypted volume, only while it is unlocked and mounted.
pl_latest=""
if [ -f "$PL_CONTAINER" ] && ! mountpoint -q "$PL_DIR"; then
  echo "$(date -Is) prologis archive SKIPPED - the encrypted volume is locked (main backup unaffected)"
elif [ -f "$PL_DIR/prologis.db" ]; then
  ptmp="$(mktemp -d)"
  if python3 - "$PL_DIR/prologis.db" "$ptmp/prologis.db" <<'EOF2'
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
dst.close(); src.close()
EOF2
  then
    if tar -czf "$OUT/.prologis_$stamp.tmp" -C "$ptmp" prologis.db -C "$PL_DIR" \
          --exclude=prologis.db --exclude='prologis.db-*' --exclude=secret.key --exclude=lost+found . \
       && mv "$OUT/.prologis_$stamp.tmp" "$OUT/prologis_$stamp.tar.gz"; then
      CHECKS[prologis]="sqlite-integrity,tar"
      pl_file="$(seal "$OUT/prologis_$stamp.tar.gz")"
      pl_latest="$(latest prologis_latest.tar.gz "$pl_file")"
    else
      echo "$(date -Is) prologis archive FAILED (main backup unaffected)"
    fi
  else
    echo "$(date -Is) prologis db backup FAILED (main backup unaffected)"
  fi
  rm -rf "$ptmp"
fi

# v319: tonight's manifest - the Pi checks every pulled file against it
python3 - "$OUT/backup_manifest_latest.json" "$stamp" "$ENCRYPT" "$plain_size" \
  "dump=$dump_latest=${CHECKS[dump]}" "users=$users_latest=${CHECKS[users]}" \
  ${pl_latest:+"prologis=$pl_latest=${CHECKS[prologis]}"} <<'EOF3'
import hashlib, json, os, sys
out, stamp, enc, plain_size = sys.argv[1], sys.argv[2], sys.argv[3] == "1", int(sys.argv[4])
files = {}
for arg in sys.argv[5:]:
    key, path, checks = arg.split("=", 2)
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    files[key] = {"name": os.path.basename(path), "sha256": h.hexdigest(), "bytes": os.path.getsize(path),
                  "checks": checks.split(",")}
files["dump"]["plain_bytes"] = plain_size
doc = {"stamp": stamp, "encrypted": enc, "files": files}
with open(out + ".tmp", "w") as fh:
    json.dump(doc, fh, indent=1)
os.replace(out + ".tmp", out)
EOF3

# v319: plain copies left from before encryption are sealed (or removed) once
# the recipients exist - nothing plain stays at rest
if [ "$ENCRYPT" = 1 ]; then
  for f in "$OUT"/argia_mont_2*.dump "$OUT"/users_2*.db "$OUT"/prologis_2*.tar.gz; do
    if [ -f "$f" ]; then seal "$f" >/dev/null; fi
  done
  rm -f "$OUT/argia_mont_latest.dump" "$OUT/users_latest.db" "$OUT/prologis_latest.tar.gz"
fi

# portfolio snapshot for the Pi's outage watch (v214) - best-effort
if [ -n "${ARGIA_PORTFOLIO_CMD:-}" ]; then
  $ARGIA_PORTFOLIO_CMD || echo "$(date -Is) portfolio export FAILED (backup unaffected)"
else
  /root/argia_v2/v2/pi/run_job.sh portfolio-export portfolio_export.py --out "$OUT/portfolio_latest.json" || echo "$(date -Is) portfolio export FAILED (backup unaffected)"
fi

# local retention: newest 3 dated copies of each (plain or sealed)
for pat in 'argia_mont_2*.dump*' 'users_2*.db*' 'prologis_2*.tar.gz*'; do
  # shellcheck disable=SC2086
  ls -1t "$OUT"/$pat 2>/dev/null | tail -n +4 | xargs -r rm -f || true
done

echo "$(date -Is) backup OK: $(basename "$dump_file") ($(stat -c%s "$dump_file") bytes, $([ "$ENCRYPT" = 1 ] && echo encrypted || echo NOT ENCRYPTED))"
