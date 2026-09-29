#!/usr/bin/env bash
set -Eeuo pipefail

AGENT_HOME=${AGENT_HOME:-/srv/agent}
REPO_DIR=${REPO_DIR:-$AGENT_HOME/assistant}
BRANCH=${BRANCH:-master}
LOCK_FILE=${LOCK_FILE:-/run/lock/assistant-auto-deploy.lock}
LOG_FILE=${LOG_FILE:-$AGENT_HOME/data/auto-deploy.log}

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
[[ -d "$REPO_DIR/.git" ]] || { echo "missing repository: $REPO_DIR" >&2; exit 1; }
install -d -o agent -g agent -m 0750 "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1
printf '\n[%s] auto-deploy start branch=%s\n' "$(date -Is)" "$BRANCH"

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "another deployment is running"; exit 1; }

if [[ -n "$(runuser -u agent -- git -C "$REPO_DIR" status --porcelain --untracked-files=all)" ]]; then
  echo "working tree is not clean; refusing automatic deployment"
  exit 1
fi

runuser -u agent -- git -C "$REPO_DIR" fetch --all --prune
runuser -u agent -- git -C "$REPO_DIR" pull --ff-only origin "$BRANCH"
bash "$REPO_DIR/deploy/verify-alinux3.sh" --tests
systemctl restart assistant.service
bash "$REPO_DIR/deploy/verify-alinux3.sh" --live
printf '[%s] auto-deploy complete commit=%s\n' "$(date -Is)" "$(runuser -u agent -- git -C "$REPO_DIR" rev-parse --short HEAD)"
