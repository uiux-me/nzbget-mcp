#!/usr/bin/env bash
# Install NZBGet + the MCP server on a low-memory Raspberry Pi (Debian, arm64).
#
# Run from your workstation:
#   ssh piracy 'sudo DOWNLOAD_ROOT=/mnt/downloads bash -s' < deploy/setup-pi.sh
#
# Idempotent: safe to re-run after changing DOWNLOAD_ROOT or the tuning below.

set -euo pipefail

NZBGET_VERSION="${NZBGET_VERSION:-26.2}"
DOWNLOAD_ROOT="${DOWNLOAD_ROOT:-/mnt/downloads}"
MCP_PREFIX="${MCP_PREFIX:-/opt/nzbget-mcp}"
CONF=/var/lib/nzbget/nzbget.conf

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning: %s\033[0m\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { echo "run this with sudo" >&2; exit 1; }

arch=$(dpkg --print-architecture)
[[ $arch == arm64 ]] || warn "expected arm64, found $arch — adjust NZBGET_VERSION asset if this fails"

# --- NZBGet ------------------------------------------------------------------
if [[ "$(dpkg-query -W -f='${Version}' nzbget 2>/dev/null || true)" == "$NZBGET_VERSION" ]]; then
    log "NZBGet $NZBGET_VERSION already installed"
else
    log "Installing NZBGet $NZBGET_VERSION"
    deb="/tmp/nzbget-${NZBGET_VERSION}-${arch}.deb"
    curl -fsSL -o "$deb" \
        "https://github.com/nzbgetcom/nzbget/releases/download/v${NZBGET_VERSION}/nzbget-${NZBGET_VERSION}-${arch}.deb"
    apt-get install -y "$deb"
    rm -f "$deb"
fi

log "Installing unpackers"
apt-get install -y 7zip >/dev/null 2>&1 || warn "could not install 7zip"
# unrar lives in non-free; RAR5 archives need it and 7zip cannot always stand in.
apt-get install -y unrar >/dev/null 2>&1 || \
    warn "unrar unavailable (it is in non-free). RAR unpacking may fail; enable non-free and re-run:
      sudo sed -i 's/ non-free-firmware/ non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources
      sudo apt-get update && sudo apt-get install -y unrar"

# --- Config ------------------------------------------------------------------
set_opt() {
    local key="$1" val="$2"
    if grep -qE "^${key}=" "$CONF"; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$CONF"
    else
        printf '%s=%s\n' "$key" "$val" >> "$CONF"
    fi
}

log "Tuning $CONF for 1 GB RAM / 4 slow cores / USB 2.0 storage"

# All working directories live on the external drive: DestDir, InterDir, NzbDir
# and QueueDir all default to ${MainDir}/..., so this one setting moves the lot
# off the SD card. TempDir defaults to /var/lib/nzbget/tmp, so move it too.
set_opt MainDir "$DOWNLOAD_ROOT"
set_opt TempDir '${MainDir}/tmp'

# Memory: the box has ~900 MB total. Defaults are ArticleCache=100/ParBuffer=100,
# which peak around 200 MB during repair. Halved to leave room for the OS.
set_opt ArticleCache 50
set_opt ParBuffer 50

# CPU: four 1.4 GHz A53 cores. Never post-process two items at once, and stop
# downloading while par-repairing or unpacking so the cores aren't split.
set_opt PostStrategy sequential
set_opt ParPauseQueue yes
set_opt UnpackPauseQueue yes

# Disk: pause well before the drive fills, since unpacking needs room for a
# second copy of the release.
set_opt DiskSpace 5000

# Sparse-file writes cut disk operations roughly in half; ext4 supports them.
set_opt DirectWrite yes

chown nzbget:nzbget "$CONF"

# --- Guard against a missing drive -------------------------------------------
# Without this, an unmounted drive means NZBGet happily recreates MainDir on the
# SD card and downloads there — the exact wear we are avoiding.
log "Binding nzbget.service to $DOWNLOAD_ROOT"
install -d /etc/systemd/system/nzbget.service.d
cat > /etc/systemd/system/nzbget.service.d/require-mount.conf <<EOF
[Unit]
RequiresMountsFor=$DOWNLOAD_ROOT
EOF

if mountpoint -q "$DOWNLOAD_ROOT"; then
    install -d -o nzbget -g nzbget "$DOWNLOAD_ROOT"
    chown nzbget:nzbget "$DOWNLOAD_ROOT"
    MOUNTED=yes
else
    warn "$DOWNLOAD_ROOT is not a mount point — nzbget.service will refuse to start until the drive is mounted (by design)"
    MOUNTED=no
fi

# --- MCP server --------------------------------------------------------------
log "Installing the MCP server into $MCP_PREFIX"
apt-get install -y python3-venv >/dev/null 2>&1 || true
install -d "$MCP_PREFIX"
[[ -d "$MCP_PREFIX/venv" ]] || python3 -m venv "$MCP_PREFIX/venv"
"$MCP_PREFIX/venv/bin/pip" -q install --upgrade pip
"$MCP_PREFIX/venv/bin/pip" -q install "$MCP_PREFIX/src"

password=$(grep -E '^ControlPassword=' "$CONF" | cut -d= -f2-)
cat > /etc/nzbget-mcp.env <<EOF
NZBGET_URL=http://127.0.0.1:6789
NZBGET_USERNAME=$(grep -E '^ControlUsername=' "$CONF" | cut -d= -f2-)
NZBGET_PASSWORD=$password
MCP_TRANSPORT=stdio
EOF
chmod 640 /etc/nzbget-mcp.env

systemctl daemon-reload
if [[ $MOUNTED == yes ]]; then
    systemctl enable --now nzbget.service
else
    systemctl enable nzbget.service
fi

log "Done"
echo "NZBGet:      $(systemctl is-active nzbget.service) — web UI on http://$(hostname -I | awk '{print $1}'):6789"
echo "Downloads:   $DOWNLOAD_ROOT (mounted: $MOUNTED)"
echo "MCP server:  $MCP_PREFIX/venv/bin/nzbget-mcp"
if [[ "$password" == "tegbzn6789" ]]; then
    warn "ControlPassword is still the documented default — change it in the web UI, then re-run this script to sync /etc/nzbget-mcp.env"
fi
