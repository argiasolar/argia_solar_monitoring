#!/usr/bin/env bash
# v319: ONE-TIME key setup on the office Pi, run by Tomasz (never by Claude:
# keys are credentials). Idempotent - existing keys are never overwritten.
#
# Creates, all under ~/.argia_keys (mode 700) or ~/.ssh:
#   prologis_luks.key            the volume key of the encrypted Prologis store
#                                (64 hex chars). The server never keeps it.
#                                Make ONE offline copy (USB in the safe).
#   ~/.ssh/argia_prologis_unlock SSH key the Pi uses to send that key; the
#                                server accepts it only for prologis_vault.sh
#   backup_identity_<name>.txt   one age private key per holder (tomasz, vit):
#                                move each to its holder, then shred it here
#   backup_recipients.txt        the two PUBLIC keys - backups are sealed for them
#
# Prints the authorized_keys line to add on the server, and the next steps.
set -euo pipefail
K="$HOME/.argia_keys"
mkdir -p "$K" && chmod 700 "$K"
command -v age-keygen >/dev/null || { echo "install age first: sudo apt-get install -y age"; exit 1; }
command -v openssl >/dev/null || { echo "install openssl first: sudo apt-get install -y openssl"; exit 1; }

if [ ! -f "$K/prologis_luks.key" ]; then
  (umask 077; openssl rand -hex 32 > "$K/prologis_luks.key")
  chmod 400 "$K/prologis_luks.key"
  echo "created  $K/prologis_luks.key"
else
  echo "kept     $K/prologis_luks.key"
fi

if [ ! -f "$HOME/.ssh/argia_prologis_unlock" ]; then
  ssh-keygen -q -t ed25519 -N '' -C argia-prologis-unlock -f "$HOME/.ssh/argia_prologis_unlock"
  echo "created  $HOME/.ssh/argia_prologis_unlock"
else
  echo "kept     $HOME/.ssh/argia_prologis_unlock"
fi

for who in tomasz vit; do
  if [ ! -f "$K/backup_identity_$who.txt" ] && ! grep -q "# $who" "$K/backup_recipients.txt" 2>/dev/null; then
    (umask 077; age-keygen -o "$K/backup_identity_$who.txt" 2>/dev/null)
    pub=$(age-keygen -y "$K/backup_identity_$who.txt")
    echo "# $who" >> "$K/backup_recipients.txt"
    echo "$pub" >> "$K/backup_recipients.txt"
    echo "created  backup key for $who (public: $pub)"
  fi
done
chmod 644 "$K/backup_recipients.txt"

cat <<EOF

=== 1. Add this ONE line to /root/.ssh/authorized_keys on the server ===
command="/opt/argia/bundle/prologis_vault.sh ssh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding $(cat "$HOME/.ssh/argia_prologis_unlock.pub")

=== 2. Then, from this Pi ===
ssh -i ~/.ssh/argia_prologis_unlock root@37.235.105.173 status
ssh -i ~/.ssh/argia_prologis_unlock root@37.235.105.173 recipients < $K/backup_recipients.txt
ssh -i ~/.ssh/argia_prologis_unlock root@37.235.105.173 init < $K/prologis_luks.key

=== 3. Offline copies (do not skip) ===
- $K/prologis_luks.key            -> one USB stick in the office safe
- $K/backup_identity_tomasz.txt   -> Tomasz's password manager / USB, then: shred -u that file here
- $K/backup_identity_vit.txt      -> Vit, then: shred -u that file here
Without a backup identity no backup can ever be decrypted. Without the
volume key (Pi + USB) the Prologis store cannot be opened after a reboot.
EOF
