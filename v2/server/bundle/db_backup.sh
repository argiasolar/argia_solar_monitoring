#!/usr/bin/env bash
# Nightly DB backup on pio06 (argia-dbdump.timer, 03:30 server time).
#
# Writes to /root/argia_backups/:
#   argia_mont_YYYYMMDD.dump   pg_dump -Fc of the monitoring DB
#   users_YYYYMMDD.db          sqlite online-backup of the auth DB
#   *_latest.*                 stable names the Pi pulls by
# Keeps the newest 3 dated copies locally — the real retention lives
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

# local retention: newest 3 dated copies of each
ls -1t "$OUT"/argia_mont_2*.dump 2>/dev/null | tail -n +4 | xargs -r rm -f
ls -1t "$OUT"/users_2*.db 2>/dev/null | tail -n +4 | xargs -r rm -f

echo "$(date -Is) backup OK: argia_mont_$stamp.dump ($(stat -c%s "$OUT/argia_mont_$stamp.dump") bytes)"
