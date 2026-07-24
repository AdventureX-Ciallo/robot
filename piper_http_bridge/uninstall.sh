#!/usr/bin/env bash
###############################################################################
# uninstall.sh -- stop+disable the systemd service, delete the unit, and
#                 remove this bridge package from the workspace.
#
# Usage:
#   ./piper_http_bridge/uninstall.sh [--workspace DIR]
#                                    [--keep-package]  remove only the service
#                                    [--keep-service]  remove only the package
###############################################################################
set -euo pipefail

WORKSPACE="${WORKSPACE:-$HOME/piper_ros_ws}"
ROS_SETUP="${ROS_SETUP:-/opt/ros/noetic/setup.bash}"
PKG_NAME="piper_http_bridge"
SERVICE_NAME="piper-bridge.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME"
REMOVE_SERVICE=1
REMOVE_PACKAGE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)    WORKSPACE="$2"; shift 2 ;;
    --keep-package) REMOVE_PACKAGE=0; shift ;;
    --keep-service) REMOVE_SERVICE=0; shift ;;
    -h|--help)      sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

DEST="$WORKSPACE/src/$PKG_NAME"
log() { printf '\033[1;34m[uninstall]\033[0m %s\n' "$*"; }
if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

if [[ "$REMOVE_SERVICE" -eq 1 ]]; then
  if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE_NAME" || [[ -f "$UNIT_DST" ]]; then
    log "Stopping + disabling $SERVICE_NAME ..."
    $SUDO systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    $SUDO systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    [[ -f "$UNIT_DST" ]] && $SUDO rm -f "$UNIT_DST"
    $SUDO systemctl daemon-reload
    $SUDO systemctl reset-failed 2>/dev/null || true
    log "  service removed"
  else
    log "No $SERVICE_NAME installed; skipping."
  fi
fi

if [[ "$REMOVE_PACKAGE" -eq 1 ]]; then
  if [[ -d "$DEST" ]]; then
    log "Removing package from workspace: $DEST"
    rm -rf "$DEST"
    if [[ -f "$ROS_SETUP" && -d "$WORKSPACE" ]]; then
      # shellcheck disable=SC1090
      source "$ROS_SETUP" || true
      ( cd "$WORKSPACE" && catkin_make ) || true
      log "  workspace rebuilt"
    fi
  else
    log "Package not present at $DEST; skipping."
  fi
fi
log "Uninstall complete."
