#!/usr/bin/env bash
# v319: encryption at rest for the ARGIA for Prologis data store.
#
# The Prologis database, documents, registry and secret key live on an
# encrypted LUKS2 volume (a container file, AES-XTS) mounted at
# /opt/argia/prologis. The volume key is NEVER stored on this server: the
# office Pi holds it and sends it over SSH (stdin) when the volume has to be
# opened - after a reboot the Pi's watchdog (every 5 min) does it. A copy of
# this server's disk therefore holds only ciphertext.
#
# The Pi reaches this script through a forced command (authorized_keys):
#   command="/opt/argia/bundle/prologis_vault.sh ssh",no-pty,no-port-forwarding,
#   no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... argia-prologis-unlock
# and may ask for: status | unlock | init | recipients | finalize.
#
# Subcommands (root on this server):
#   status       one line per fact: initialised / open / mounted / service
#   check        systemd ExecStartPre of argia-prologis: refuse to start the
#                platform on the bare mount point (no data, or an empty dir)
#   unlock       key on stdin -> open, mount, start the platform; idempotent
#   lock         stop the platform, unmount, close (admin use)
#   init         key on stdin, ONE time: create the volume, move the current
#                data into it (verified file by file), mount it, restart;
#                any failure rolls back to the plain directory
#   finalize     after the init is verified: shred the plain rollback copy
#   recipients   age public keys on stdin -> the backup recipients file
#
# The key is 64 hex characters (32 random bytes), e.g. from `openssl rand -hex 32`.
# Paths can be overridden by the environment (unit tests).
set -uo pipefail

CONTAINER="${PL_VAULT_CONTAINER:-/opt/argia/prologis.luks}"
MAPPER="${PL_VAULT_MAPPER:-argia_prologis}"
MAPPER_DIR="${PL_VAULT_MAPPER_DIR:-/dev/mapper}"
MNT="${PL_VAULT_MNT:-/opt/argia/prologis}"
SIZE="${PL_VAULT_SIZE:-4G}"
SERVICE="${PL_VAULT_SERVICE:-argia-prologis}"
HEALTH_URL="${PL_VAULT_HEALTH:-http://127.0.0.1:8520/healthz}"
PLAIN_DIR="${PL_VAULT_PLAIN_DIR:-/root}"
RECIPIENTS="${ARGIA_BACKUP_RECIPIENTS:-/opt/argia/backup_recipients.txt}"
LOG="${PL_VAULT_LOG:-/root/argia_logs/prologis_vault.log}"

say() { echo "$*"; mkdir -p "$(dirname "$LOG")" 2>/dev/null && echo "$(date -Is) $*" >> "$LOG" 2>/dev/null; return 0; }
die() { say "ERROR: $*"; exit 1; }

initialised() { [ -f "$CONTAINER" ]; }
is_open() { [ -e "$MAPPER_DIR/$MAPPER" ]; }
is_mounted() { mountpoint -q "$MNT"; }

read_key() {
  # the key arrives on stdin; trailing newlines are dropped so `cat keyfile`
  # and `printf` give the same bytes to cryptsetup every time
  local k
  k="$(head -c 512)"
  k="$(printf '%s' "$k" | tr -d '\r\n')"
  [[ "$k" =~ ^[0-9a-f]{64}$ ]] || die "the key must be 64 hex characters (openssl rand -hex 32)"
  KEY="$k"
}

cmd_status() {
  initialised && echo "initialised: yes" || echo "initialised: no"
  is_open && echo "open: yes" || echo "open: no"
  is_mounted && echo "mounted: yes" || echo "mounted: no"
  echo "service: $(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown)"
  [ -s "$RECIPIENTS" ] && echo "backup recipients: $(grep -c '^age1' "$RECIPIENTS")" || echo "backup recipients: none"
}

cmd_check() {
  if initialised; then
    is_mounted || { echo "prologis vault is locked - the office Pi unlocks it (or: prologis_vault.sh unlock < key)"; exit 1; }
  fi
  [ -f "$MNT/prologis.db" ] || [ -f "$MNT/registry.json" ] || { echo "no Prologis data under $MNT - refusing to start on an empty directory"; exit 1; }
  exit 0
}

cmd_unlock() {
  initialised || die "no encrypted volume yet (run init first)"
  if is_mounted; then say "already unlocked"; exit 0; fi
  read_key
  if ! is_open; then
    printf '%s' "$KEY" | cryptsetup open --key-file=- "$CONTAINER" "$MAPPER" || die "wrong key or damaged volume - not opened"
  fi
  mount -o noexec,nosuid,nodev "$MAPPER_DIR/$MAPPER" "$MNT" || die "opened but could not mount on $MNT"
  systemctl start "$SERVICE" || die "mounted, but $SERVICE did not start"
  say "unlocked and mounted; $SERVICE started"
}

cmd_lock() {
  systemctl stop "$SERVICE" 2>/dev/null
  is_mounted && { umount "$MNT" || die "could not unmount $MNT"; }
  is_open && { cryptsetup close "$MAPPER" || die "could not close $MAPPER"; }
  say "locked"
}

manifest() {
  # relative path + sha256 of every regular file under $1, sorted. The
  # live sqlite side files (-wal/-shm) are left out: they are folded into
  # prologis.db when the platform stops.
  (cd "$1" && find . -type f ! -name '*.db-wal' ! -name '*.db-shm' ! -name 'lost+found' -print0 \
     | sort -z | xargs -0 -r sha256sum)
}

cmd_init() {
  initialised && die "already initialised ($CONTAINER exists) - nothing done"
  command -v cryptsetup >/dev/null || die "cryptsetup is not installed (apt-get install -y cryptsetup-bin)"
  [ -d "$MNT" ] || die "$MNT does not exist"
  is_mounted && die "$MNT is already a mount point"
  read_key
  local stamp tmpmnt plain step=0
  stamp="$(date +%Y%m%d%H%M%S)"
  tmpmnt="$(mktemp -d /mnt/argia_prologis_new.XXXX)"
  plain="$PLAIN_DIR/prologis_plain_rollback_$stamp"

  rollback() {
    say "init FAILED at step $step - rolling back"
    if [ "$step" -ge 7 ]; then
      is_mounted && umount "$MNT"
      chattr -i "$MNT" 2>/dev/null
      if [ -d "$plain" ]; then rmdir "$MNT" 2>/dev/null; mv "$plain" "$MNT"; fi
    fi
    mountpoint -q "$tmpmnt" && umount "$tmpmnt"
    rmdir "$tmpmnt" 2>/dev/null
    is_open && cryptsetup close "$MAPPER"
    rm -f "$CONTAINER"
    [ "$step" -ge 6 ] && systemctl start "$SERVICE"
    exit 1
  }

  step=1; fallocate -l "$SIZE" "$CONTAINER" && chmod 600 "$CONTAINER" || rollback
  step=2; printf '%s' "$KEY" | cryptsetup luksFormat --batch-mode --type luks2 --pbkdf pbkdf2 \
            --label argia_prologis --key-file=- "$CONTAINER" || rollback
  step=3; printf '%s' "$KEY" | cryptsetup open --key-file=- "$CONTAINER" "$MAPPER" || rollback
  step=4; mkfs.ext4 -q -L argia_prologis "$MAPPER_DIR/$MAPPER" || rollback
  step=5; mount -o noexec,nosuid,nodev "$MAPPER_DIR/$MAPPER" "$tmpmnt" && chmod 700 "$tmpmnt" || rollback
  step=6; systemctl stop "$SERVICE" || rollback
  # the platform is stopped: the database is closed and consistent
  cp -a "$MNT"/. "$tmpmnt"/ || rollback
  rm -rf "$tmpmnt/lost+found"
  if [ "$(manifest "$MNT")" != "$(manifest "$tmpmnt")" ]; then say "copy differs from the original"; rollback; fi
  if [ -f "$tmpmnt/prologis.db" ]; then
    python3 - "$tmpmnt/prologis.db" <<'EOF' || rollback
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0]
sys.exit(0 if r == "ok" else 1)
EOF
  fi
  step=7; mv "$MNT" "$plain" && chmod 700 "$plain" || rollback
  mkdir -m 700 "$MNT" && chattr +i "$MNT" || rollback        # the bare mount point can never take data
  umount "$tmpmnt" && rmdir "$tmpmnt" || rollback
  mount -o noexec,nosuid,nodev "$MAPPER_DIR/$MAPPER" "$MNT" || rollback
  step=8; systemctl start "$SERVICE" || rollback
  local ok=0 i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    [ "$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$HEALTH_URL")" = 200 ] && { ok=1; break; }
    sleep 2
  done
  [ "$ok" = 1 ] || rollback
  say "init OK: $(manifest "$MNT" | wc -l) files on the encrypted volume, platform healthy."
  say "The plain copy is kept for rollback in $plain - check the platform, then run: finalize"
}

cmd_finalize() {
  local d n=0
  for d in "$PLAIN_DIR"/prologis_plain_rollback_*; do
    [ -d "$d" ] || continue
    find "$d" -type f -exec shred -u -z {} \; 2>/dev/null
    rm -rf "$d"; n=$((n + 1))
  done
  say "finalize: $n plain rollback copy(ies) shredded"
}

cmd_recipients() {
  local tmp lines
  tmp="$(mktemp)"
  head -c 4096 | tr -d '\r' | grep -v '^[[:space:]]*#' | sed '/^[[:space:]]*$/d' > "$tmp"
  lines="$(wc -l < "$tmp")"
  if [ "$lines" -lt 1 ] || [ "$lines" -gt 5 ] || grep -qvE '^age1[02-9ac-hj-np-z]{58}$' "$tmp"; then
    rm -f "$tmp"; die "expected 1 to 5 age public keys (age1..., one per line) - nothing changed"
  fi
  install -m 644 "$tmp" "$RECIPIENTS" && rm -f "$tmp"
  say "backup recipients set: $lines key(s) in $RECIPIENTS"
}

case "${1:-}" in
  ssh)
    # forced command: only the verbs below, nothing else is ever executed
    set -f                               # no glob expansion of what the client sent
    set -- ${SSH_ORIGINAL_COMMAND:-}
    set +f                               # the verbs themselves use globs (finalize)
    case "${1:-}" in
      status) cmd_status ;;
      unlock) cmd_unlock ;;
      init) cmd_init ;;
      finalize) cmd_finalize ;;
      recipients) cmd_recipients ;;
      *) die "refused: '${1:-}' (allowed: status unlock init finalize recipients)" ;;
    esac ;;
  status) cmd_status ;;
  check) cmd_check ;;
  unlock) cmd_unlock ;;
  lock) cmd_lock ;;
  init) cmd_init ;;
  finalize) cmd_finalize ;;
  recipients) cmd_recipients ;;
  *) echo "usage: prologis_vault.sh status|check|unlock|lock|init|finalize|recipients"; exit 2 ;;
esac
