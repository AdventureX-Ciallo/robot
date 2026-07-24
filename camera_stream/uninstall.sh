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

log() { printf '\033[1;34m[uninstall]\033[0m %s\n' "$*"; }
if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE_NAME" || [[ -f "$UNIT_DST" ]]; then
  log "Stopping + disabling $SERVICE_NAME ..."
  $SUDO systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  $SUDO systemctl disable "$SERVICE_NAME" 2>/dev/null || true
  [[ -f "$UNIT_DST" ]] && $SUDO rm -f "$UNIT_DST"
  $SUDO systemctl daemon-reload
  $SUDO systemctl reset-failed 2>/dev/null || true
  log "  service removed"
else
  log "No $SERVICE_NAME installed; nothing to do."
fi
log "Uninstall complete."
