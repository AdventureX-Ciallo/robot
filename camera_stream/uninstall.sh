#!/usr/bin/env bash
###############################################################################
# uninstall.sh -- stop+disable the camera-stream systemd service and delete the
# unit. (The package itself lives in the repo; nothing was copied elsewhere.)
#
# Usage:
#   ./camera_stream/uninstall.sh
###############################################################################
set -euo pipefail

SERVICE_NAME="camera-stream.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME"
MTX_SERVICE="mediamtx.service"
MTX_UNIT="/etc/systemd/system/$MTX_SERVICE"
MTX_PREFIX="/opt/mediamtx"

log() { printf '\033[1;34m[uninstall]\033[0m %s\n' "$*"; }
if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

remove_unit() {  # $1=service name  $2=unit path
  if systemctl list-unit-files 2>/dev/null | grep -q "^$1" || [[ -f "$2" ]]; then
    log "Stopping + disabling $1 ..."
    $SUDO systemctl stop "$1" 2>/dev/null || true
    $SUDO systemctl disable "$1" 2>/dev/null || true
    [[ -f "$2" ]] && $SUDO rm -f "$2"
    log "  removed $1"
  else
    log "No $1 installed; skipping."
  fi
}

remove_unit "$SERVICE_NAME" "$UNIT_DST"
remove_unit "$MTX_SERVICE" "$MTX_UNIT"
$SUDO systemctl daemon-reload
$SUDO systemctl reset-failed 2>/dev/null || true
[[ -d "$MTX_PREFIX" ]] && { log "Removing $MTX_PREFIX"; $SUDO rm -rf "$MTX_PREFIX"; }
log "Uninstall complete."
