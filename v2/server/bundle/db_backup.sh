#!/usr/bin/env bash
# Nightly DB backup on pio06 (argia-dbdump.timer, 03:30 server time).
#
# Writes to /root/argia_backups/:
#   argia_mont_YYYYMMDD.dump   pg_dump -Fc of the monitoring DB
#   users_YYYYMMDD.db          sqlite online-backup of the auth DB
#   prologis_YYYYMMDD.tar.gz   v292: ARGIA for Prologis db + files (when present)
#   *_latest.*                 stable names the Pi pulls by
# Keeps the newest 3 dated copies locally - the real retention lives
# on the Pi (14 daily + 8 weekly), pulled via a read-only SFTP key, so
# a compromised or wiped server cannot reach the Pi's copies.
set -euo pipefail
OUT="${ARGIA_BACKUP_DIR:-/root/argia_backups}"
mkdir -p "$OUT" "$OUT/reports"
stamp="$(date +%Y%m%d)"

tmp="$OUT/.argia_mont_$stamp.tmp"
runuser -u postgres -- pg_dump -Fc argia_mont > "$tmp"
head -c 5 "$tmp" | grep -q "PGDMP"           # custom-format magic
[ "$(stat -c%s "$tmp")" -ge 100000 ]         # a real dump, not a stub
mv "$tmp" "$OUT/argia_mont_$stamp.dump"
cp -f "$OUT/argia_mont_$stamp.dump" "$OUT/argia_mont_latest.dump"

# auth DB: sqlite online backup (safe while the app is running)
python3 - "$OUT/users_$stamp.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect("file:/opt/argia/auth/users.db?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
dst.close(); src.close()
EOF
cp -f "$OUT/users_$stamp.db" "$OUT/users_latest.db"

# v292: ARGIA for Prologis - its own database (sqlite online backup) plus
# registry, uploaded files and brand, as one archive. Best-effort only when
# the platform exists; a failure here must not lose the main backup.
if [ -f /opt/argia/prologis/prologis.db ]; then
  ptmp="$(mktemp -d)"
  if python3 - "$ptmp/prologis.db" <<'EOF2'
import sqlite3, sys
src = sqlite3.connect("file:/opt/argia/prologis/prologis.db?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
dst.close(); src.close()
EOF2
  then
    tar -czf "$OUT/.prologis_$stamp.tmp" -C "$ptmp" prologis.db -C /opt/argia/prologis \
        --exclude=prologis.db --exclude='prologis.db-*' --exclude=secret.key . \
      && mv "$OUT/.prologis_$stamp.tmp" "$OUT/prologis_$stamp.tar.gz" \
      && cp -f "$OUT/prologis_$stamp.tar.gz" "$OUT/prologis_latest.tar.gz" \
      || echo "$(date -Is) prologis archive FAILED (main backup unaffected)"
  else
    echo "$(date -Is) prologis db backup FAILED (main backup unaffected)"
  fi
  rm -rf "$ptmp"
  ls -1t "$OUT"/prologis_2*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
fi

# portfolio snapshot for the Pi's outage watch (v214) - best-effort
/root/argia_v2/v2/pi/run_job.sh portfolio-export portfolio_export.py --out "$OUT/portfolio_latest.json" || echo "$(date -Is) portfolio export FAILED (backup unaffected)"

# local retention: newest 3 dated copies of each
ls -1t "$OUT"/argia_mont_2*.dump 2>/dev/null | tail -n +4 | xargs -r rm -f
ls -1t "$OUT"/users_2*.db 2>/dev/null | tail -n +4 | xargs -r rm -f

echo "$(date -Is) backup OK: argia_mont_$stamp.dump ($(stat -c%s "$OUT/argia_mont_$stamp.dump") bytes)"
