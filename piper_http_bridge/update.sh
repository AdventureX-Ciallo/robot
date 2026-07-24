#!/usr/bin/env bash
###############################################################################
# update.sh -- re-sync this package, rebuild if needed, and restart the service.
#
#   sdk backend : nothing to compile; just restarts the service.
#   ros backend : copies the package into the catkin workspace, catkin_make,
#                 then restarts.
#
# Usage:
#   ./piper_http_bridge/update.sh [--backend sdk|ros] [--workspace DIR] [--no-restart]
###############################################################################
set -euo pipefail

BACKEND="${BACKEND:-sdk}"
WORKSPACE="${WORKSPACE:-$HOME/piper_ros_ws}"
ROS_SETUP="${ROS_SETUP:-/opt/ros/noetic/setup.bash}"
PKG_NAME="piper_http_bridge"
SERVICE_NAME="piper-bridge.service"
RESTART=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)    BACKEND="$2"; shift 2 ;;
    --workspace)  WORKSPACE="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    -h|--help)    sed -n '2,11p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

PKG_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log() { printf '\033[1;34m[update]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

if [[ "$BACKEND" == "ros" ]]; then
  DEST="$WORKSPACE/src/$PKG_NAME"
  [[ -f "$ROS_SETUP" ]]     || die "ROS setup not found: $ROS_SETUP"
  [[ -d "$WORKSPACE/src" ]] || die "workspace src not found: $WORKSPACE/src (run install.sh first)"
  if [[ "$PKG_SRC" != "$DEST" ]]; then
    log "Syncing package -> $DEST"
    rm -rf "$DEST"; cp -a "$PKG_SRC" "$DEST"
    chmod +x "$DEST/scripts/piper_http_bridge_node.py" || true
  fi
  log "Rebuilding workspace ..."
  # shellcheck disable=SC1090
  source "$ROS_SETUP"
  ( cd "$WORKSPACE" && catkin_make )
  log "  build OK"
else
  log "sdk backend: nothing to compile."
fi

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
