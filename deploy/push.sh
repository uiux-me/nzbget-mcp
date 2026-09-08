#!/usr/bin/env bash
# Sync this repo to the Pi and run the installer. Usage: deploy/push.sh [host]
set -euo pipefail

HOST="${1:-piracy}"
DOWNLOAD_ROOT="${DOWNLOAD_ROOT:-/mnt/downloads}"
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

echo "==> Syncing source to $HOST"
rsync -az --delete \
    --exclude '__pycache__' --exclude '*.egg-info' \
    "$root/src" "$root/pyproject.toml" "$root/README.md" \
    "$HOST:/tmp/nzbget-mcp-src/"

echo "==> Installing on $HOST (DOWNLOAD_ROOT=$DOWNLOAD_ROOT)"
ssh "$HOST" "sudo install -d /opt/nzbget-mcp/src && sudo rsync -a --delete /tmp/nzbget-mcp-src/ /opt/nzbget-mcp/src/"
ssh "$HOST" "sudo DOWNLOAD_ROOT='$DOWNLOAD_ROOT' bash -s" < "$root/deploy/setup-pi.sh"
