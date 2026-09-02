#!/usr/bin/env bash
# Nightly backup PULL on the Pi (cron 22:00 Mexico City = after the
# server's 03:30-CET dump). The Pi initiates with a read-only SFTP key
# restricted to /root/argia_backups on pio06 — the server holds no Pi
# credentials, so compromising the server cannot touch these copies.
#
#   ~/db_backups/daily/    argia_mont_YYYYMMDD.dump + users_YYYYMMDD.db, 14 kept
#   ~/db_backups/weekly/   Sunday's dump, 8 kept
#   ~/db_backups/reports/  mirror of the emailed financial PDFs
#
# Integrity every night (PGDMP magic + size); a deeper pg_restore -l
# listing test on the 1st of each month when pg_restore is available.
# Failures alert via ntfy (the topic already on Tomasz's phone).
set -uo pipefail
NTFY_TOPIC="argia-reportwatch-x9k24fq7"
SRV="root@37.235.105.173"
KEY="$HOME/.ssh/argia_backup_pull"
BASE="$HOME/db_backups"
stamp="$(date +%Y%m%d)"

alert() {
  curl -s -m 10 -H "Title: ARGIA DB backup" -H "Priority: high" \
    -H "Tags: floppy_disk,warning" -d "$1" \
    "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 || true
  echo "$(date -Is) ALERT: $1"
}

mkdir -p "$BASE/daily" "$BASE/weekly" "$BASE/reports"
tmp_dump="$BASE/daily/.dump_$stamp.tmp"
tmp_users="$BASE/daily/.users_$stamp.tmp"

if ! sftp -q -i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
       -o ConnectTimeout=25 "$SRV" >/dev/null 2>&1 <<EOF
get argia_mont_latest.dump $tmp_dump
get users_latest.db $tmp_users
EOF
then
  alert "backup pull FAILED (sftp) $stamp — server unreachable or key rejected"
  exit 1
fi

if ! head -c 5 "$tmp_dump" | grep -q "PGDMP"; then
  alert "backup INVALID $stamp — no PGDMP magic, not a pg_dump file"
  rm -f "$tmp_dump" "$tmp_users"; exit 1
fi
sz="$(stat -c%s "$tmp_dump")"
if [ "$sz" -lt 100000 ]; then
  alert "backup SUSPICIOUS $stamp — only $sz bytes"
  rm -f "$tmp_dump" "$tmp_users"; exit 1
fi
mv "$tmp_dump" "$BASE/daily/argia_mont_$stamp.dump"
mv "$tmp_users" "$BASE/daily/users_$stamp.db"

# financial report PDFs (small; best-effort)
sftp -q -i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o ConnectTimeout=25 "$SRV" >/dev/null 2>&1 <<EOF || true
get reports/* $BASE/reports/
EOF

# Sunday -> weekly copy
if [ "$(date +%u)" = "7" ]; then
  cp -f "$BASE/daily/argia_mont_$stamp.dump" "$BASE/weekly/"
fi

# retention: 14 daily, 8 weekly
ls -1t "$BASE"/daily/argia_mont_*.dump 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t "$BASE"/daily/users_*.db 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t "$BASE"/weekly/argia_mont_*.dump 2>/dev/null | tail -n +9 | xargs -r rm -f

# monthly deeper test: can pg_restore list the archive's contents?
if [ "$(date +%d)" = "01" ] && command -v pg_restore >/dev/null 2>&1; then
  if pg_restore -l "$BASE/daily/argia_mont_$stamp.dump" >/dev/null 2>&1; then
    echo "$(date -Is) monthly restore-list test OK"
  else
    alert "MONTHLY RESTORE TEST FAILED — $stamp dump does not list cleanly"
    exit 1
  fi
fi

echo "$(date -Is) pull OK argia_mont_$stamp.dump ($sz bytes)"
