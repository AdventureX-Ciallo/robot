#!/usr/bin/env bash
###############################################################################
# update.sh -- re-sync is a no-op for this pure-python package (nothing to
# compile); it just restarts the systemd service so code edits take effect.
#
# Usage:
#   ./camera_stream/update.sh [--no-restart]
###############################################################################
set -euo pipefail

SERVICE_NAME="camera-stream.service"
RESTART=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-restart) RESTART=0; shift ;;
    -h|--help)    sed -n '2,8p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m[update]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

log "pure-python package: nothing to compile."

if [[ "$RESTART" -eq 1 ]]; then
  if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE_NAME"; then
    log "Restarting $SERVICE_NAME ..."
    $SUDO systemctl daemon-reload
    $SUDO systemctl restart "$SERVICE_NAME"
    sleep 2
    systemctl is-active --quiet "$SERVICE_NAME" \
      && log "  service active" \
      || die "  service failed to stay up -- check: journalctl -u $SERVICE_NAME -n 80"
  else
    log "$SERVICE_NAME not installed; skipping restart."
  fi
fi
log "Update complete."
