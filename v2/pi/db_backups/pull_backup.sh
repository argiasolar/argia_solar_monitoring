#!/usr/bin/env bash
# Nightly backup PULL on the Pi (cron 22:00 Mexico City = after the
# server's 03:30-CET dump). The Pi initiates with a read-only SFTP key
# restricted to /root/argia_backups on pio06 - the server holds no Pi
# credentials, so compromising the server cannot touch these copies.
#
#   ~/db_backups/daily/    argia_mont_YYYYMMDD.dump[.age] + users_YYYYMMDD.db[.age], 14 kept
#   ~/db_backups/weekly/   Sunday's dump, 8 kept
#   ~/db_backups/prologis/ ARGIA for Prologis archive, 14 kept
#   ~/db_backups/reports/  mirror of the emailed financial PDFs
#
# v319: the server seals its backups with age (public keys only; the two
# private keys are kept offline - the Pi cannot decrypt either). The server
# checks the plain files before sealing (PGDMP, pg_restore -l, sqlite
# integrity) and writes backup_manifest_latest.json; here every pulled file
# must match the manifest's sha256 and carry the age header. Plain files
# (before the switch) are still accepted and checked as before; once
# ~/.argia_keys/backup_recipients.txt exists, plain copies kept here are
# sealed in place, so nothing plain stays at rest on the Pi either.
# Failures alert via ntfy (the topic already on Tomasz's phone).
set -uo pipefail
NTFY_TOPIC="argia-reportwatch-x9k24fq7"
SRV="${SRV:-root@37.235.105.173}"
KEY="${KEY:-$HOME/.ssh/argia_backup_pull}"
BASE="${BASE:-$HOME/db_backups}"
RECIPIENTS="${RECIPIENTS:-$HOME/.argia_keys/backup_recipients.txt}"
SFTP="${SFTP:-sftp}"
stamp="${STAMP:-$(date +%Y%m%d)}"
MIN_DUMP_BYTES=100000

# v305.2: phone pushes are switched in report_watch/push.conf (ARGIA_PUSH overrides);
# the server's health file reports a stale off-site backup either way (pi_status.py)
PUSH=off
[ -f "$(dirname "$0")/../report_watch/push.conf" ] && . "$(dirname "$0")/../report_watch/push.conf"
PUSH="${ARGIA_PUSH:-$PUSH}"
alert() {
  if [ "$PUSH" = on ]; then
    curl -s -m 10 -H "Title: ARGIA DB backup" -H "Priority: high" \
      -H "Tags: floppy_disk,warning" -d "$1" \
      "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 || true
  fi
  echo "$(date -Is) ALERT: $1"
}

get() {  # get REMOTE LOCAL - one sftp fetch, quiet
  "$SFTP" -q -i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=25 "$SRV" >/dev/null 2>&1 <<EOF
get $1 $2
EOF
}

is_sealed() { head -c 21 "$1" 2>/dev/null | grep -q "age-encryption.org/v1"; }

# matches FILE KEY: the file's sha256 equals the manifest entry KEY
matches() {
  python3 - "$BASE/.manifest.json" "$2" "$1" <<'EOF'
import hashlib, json, sys
try:
    m = json.load(open(sys.argv[1]))["files"][sys.argv[2]]
except Exception:
    sys.exit(1)
h = hashlib.sha256(open(sys.argv[3], "rb").read()).hexdigest()
sys.exit(0 if h == m["sha256"] else 1)
EOF
}

mkdir -p "$BASE/daily" "$BASE/weekly" "$BASE/reports" "$BASE/prologis"
# v309: one pull at a time - the 22:00 cron run and report_watch's catch-up
# (catchup.sh) must never write the same temp files together
exec 9>"$BASE/.pull.lock"
if ! flock -n 9; then
  echo "$(date -Is) another pull is running - skipped"
  exit 0
fi

# ---- the monitoring DB dump: sealed (v319) or plain ----
rm -f "$BASE/.manifest.json"
get backup_manifest_latest.json "$BASE/.manifest.json" || true
tmp_dump="$BASE/daily/.dump_$stamp.tmp"
tmp_users="$BASE/daily/.users_$stamp.tmp"
rm -f "$tmp_dump" "$tmp_users"
if get argia_mont_latest.dump.age "$tmp_dump"; then
  sealed=1
  if ! is_sealed "$tmp_dump"; then
    alert "backup INVALID $stamp - .age file without the age header"; rm -f "$tmp_dump"; exit 1
  fi
  if ! matches "$tmp_dump" dump; then
    alert "backup INVALID $stamp - sealed dump does not match the server manifest (sha256)"; rm -f "$tmp_dump"; exit 1
  fi
  if ! get users_latest.db.age "$tmp_users" || ! matches "$tmp_users" users; then
    alert "backup INVALID $stamp - sealed users db missing or not matching the manifest"; rm -f "$tmp_dump" "$tmp_users"; exit 1
  fi
  dump_name="argia_mont_$stamp.dump.age"; users_name="users_$stamp.db.age"
elif get argia_mont_latest.dump "$tmp_dump"; then
  sealed=0
  if ! head -c 5 "$tmp_dump" | grep -q "PGDMP"; then
    alert "backup INVALID $stamp - no PGDMP magic, not a pg_dump file"; rm -f "$tmp_dump"; exit 1
  fi
  if ! get users_latest.db "$tmp_users"; then
    alert "backup pull FAILED $stamp - users db missing"; rm -f "$tmp_dump"; exit 1
  fi
  dump_name="argia_mont_$stamp.dump"; users_name="users_$stamp.db"
else
  alert "backup pull FAILED (sftp) $stamp - server unreachable or key rejected"
  exit 1
fi
sz="$(stat -c%s "$tmp_dump")"
if [ "$sz" -lt "$MIN_DUMP_BYTES" ]; then
  alert "backup SUSPICIOUS $stamp - only $sz bytes"
  rm -f "$tmp_dump" "$tmp_users"; exit 1
fi
mv "$tmp_dump" "$BASE/daily/$dump_name"
mv "$tmp_users" "$BASE/daily/$users_name"

# portfolio snapshot for ppa_watch.py (v214; best-effort)
mkdir -p "$HOME/report_watch"
get portfolio_latest.json "$HOME/report_watch/portfolio.json" || true

# v292: ARGIA for Prologis archive (db + documents; best-effort, own retention)
ptmp="$BASE/prologis/.prologis_$stamp.tmp"
rm -f "$ptmp"
if get prologis_latest.tar.gz.age "$ptmp"; then
  if is_sealed "$ptmp" && matches "$ptmp" prologis; then
    mv "$ptmp" "$BASE/prologis/prologis_$stamp.tar.gz.age"
  else
    alert "Prologis backup INVALID $stamp - sealed archive not matching the manifest"; rm -f "$ptmp"
  fi
elif get prologis_latest.tar.gz "$ptmp"; then
  if tar -tzf "$ptmp" ./prologis.db >/dev/null 2>&1 || tar -tzf "$ptmp" prologis.db >/dev/null 2>&1; then
    mv "$ptmp" "$BASE/prologis/prologis_$stamp.tar.gz"
  else
    alert "Prologis backup INVALID $stamp - archive without prologis.db"; rm -f "$ptmp"
  fi
fi

# financial report PDFs (small; best-effort)
"$SFTP" -q -i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o ConnectTimeout=25 "$SRV" >/dev/null 2>&1 <<EOF || true
get reports/* $BASE/reports/
EOF

# Sunday -> weekly copy
if [ "$(date +%u)" = "7" ]; then
  cp -f "$BASE/daily/$dump_name" "$BASE/weekly/"
fi

# v319: plain copies kept from before the switch are sealed in place
if [ -s "$RECIPIENTS" ] && command -v age >/dev/null; then
  n=0
  for f in "$BASE"/daily/argia_mont_*.dump "$BASE"/daily/users_*.db "$BASE"/weekly/argia_mont_*.dump \
           "$BASE"/prologis/prologis_*.tar.gz "$BASE"/reports/*.pdf; do
    [ -f "$f" ] || continue
    if age -R "$RECIPIENTS" -o "$f.age.tmp" "$f" && is_sealed "$f.age.tmp"; then
      mv "$f.age.tmp" "$f.age"; touch -r "$f" "$f.age"; rm -f "$f"; n=$((n + 1))
    else
      rm -f "$f.age.tmp"; alert "could not seal the old plain copy $(basename "$f")"
    fi
  done
  [ "$n" -gt 0 ] && echo "$(date -Is) sealed $n plain copies kept from before encryption"
fi

# retention: 14 daily, 8 weekly, 14 Prologis (plain or sealed)
ls -1t "$BASE"/daily/argia_mont_*.dump* 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t "$BASE"/daily/users_*.db* 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t "$BASE"/weekly/argia_mont_*.dump* 2>/dev/null | tail -n +9 | xargs -r rm -f
ls -1t "$BASE"/prologis/prologis_*.tar.gz* 2>/dev/null | tail -n +15 | xargs -r rm -f

# monthly deeper test on plain dumps (sealed dumps are tested by the server
# with pg_restore -l before sealing; the manifest records it)
if [ "$sealed" = 0 ] && [ "$(date +%d)" = "01" ] && command -v pg_restore >/dev/null 2>&1; then
  if pg_restore -l "$BASE/daily/$dump_name" >/dev/null 2>&1; then
    echo "$(date -Is) monthly restore-list test OK"
  else
    alert "MONTHLY RESTORE TEST FAILED - $stamp dump does not list cleanly"
    exit 1
  fi
fi

echo "$(date -Is) pull OK $dump_name ($sz bytes$([ "$sealed" = 1 ] && echo ", sealed, manifest OK"))"
