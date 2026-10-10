#!/usr/bin/env bash
# v319: the office Pi keeps the key of the encrypted ARGIA for Prologis volume
# on pio06 (the server never stores it). Called by report_watch.sh every
# 5 minutes: when prologis.argia.com.mx does not answer its health check,
# the key is sent over SSH (stdin) to the server's forced command
# `prologis_vault.sh ssh` with the verb "unlock" - which opens and mounts
# the volume and starts the platform, or answers "already unlocked".
#
# At most one attempt per RETRY_SEC. Prints one line when it acts, silent
# otherwise; exit 0 always (the caller is a watchdog). Paths can be
# overridden by the environment (unit tests).
set -u
SRV="${SRV:-root@37.235.105.173}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/argia_prologis_unlock}"
VAULT_KEY="${VAULT_KEY:-$HOME/.argia_keys/prologis_luks.key}"
HEALTH="${HEALTH:-https://prologis.argia.com.mx/healthz}"
STAMP="${STAMP:-$HOME/report_watch/prologis_unlock_at}"
NOW="${NOW:-$(date +%s)}"
RETRY_SEC="${RETRY_SEC:-600}"
SSH="${SSH:-ssh}"
CURL="${CURL:-curl}"

[ -f "$SSH_KEY" ] && [ -f "$VAULT_KEY" ] || exit 0      # not set up on this machine

code=$("$CURL" -s -o /dev/null -w '%{http_code}' -m 15 "$HEALTH" 2>/dev/null)
[ "$code" = 200 ] && exit 0
last=$(cat "$STAMP" 2>/dev/null || echo 0)
[ $(( NOW - last )) -ge "$RETRY_SEC" ] || exit 0
mkdir -p "$(dirname "$STAMP")"
echo "$NOW" > "$STAMP"

out=$("$SSH" -i "$SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=25 "$SRV" unlock < "$VAULT_KEY" 2>&1 | tail -1)
echo "$(date '+%Y-%m-%d %H:%M:%S') prologis health HTTP ${code:-none} - vault unlock: ${out:-no answer}"
exit 0
