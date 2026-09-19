#!/usr/bin/env bash
set -Eeuo pipefail

AGENT_HOME=${AGENT_HOME:-/srv/agent}
BACKUP_DIR=${BACKUP_DIR:-$AGENT_HOME/backups}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE="$BACKUP_DIR/assistant-$STAMP.tar.gz"

[[ $EUID -eq 0 ]] || { echo "请使用 sudo 运行" >&2; exit 1; }
WAS_ACTIVE=0
if systemctl is-active --quiet assistant.service; then
  WAS_ACTIVE=1
  systemctl stop assistant.service
fi
restore_service() {
  if [[ $WAS_ACTIVE -eq 1 ]]; then
    systemctl start assistant.service
  fi
}
trap restore_service EXIT

install -d -o root -g root -m 0700 "$BACKUP_DIR"
items=(data assistant.env home)
for item in "${items[@]}"; do
  [[ -e "$AGENT_HOME/$item" ]] || { echo "缺少 $AGENT_HOME/$item" >&2; exit 1; }
done
tar --numeric-owner -C "$AGENT_HOME" -czf "$ARCHIVE" "${items[@]}"
chmod 0600 "$ARCHIVE"
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
chmod 0600 "$ARCHIVE.sha256"
echo "$ARCHIVE"
