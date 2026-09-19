#!/usr/bin/env bash
# Compatibility entry point. The supported server platform is Alibaba Cloud Linux 3.
set -Eeuo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec bash "$SCRIPT_DIR/install-alinux3.sh" "$@"
