#!/usr/bin/env bash
#
# uninstall.sh -- remove piper_http_bridge: stop+disable the systemd service,
#                 delete the unit, and remove the package from the workspace.
#
# Usage:
#   ./uninstall.sh                 # remove service + package
#   ./uninstall.sh --keep-package  # remove only the systemd service
#   ./uninstall.sh --keep-service  # remove only the package from the workspace
#   WORKSPACE=~/catkin_ws ./uninstall.sh
#
set -euo pipefail

WORKSPACE="${WORKSPACE:-$HOME/piper_ros}"
PKG_NAME="piper_http_bridge"
SERVICE_NAME="piper-bridge.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME"
REMOVE_SERVICE=1
REMOVE_PACKAGE=1

for arg in "$@"; do
  case "$arg" in
    --keep-package) REMOVE_PACKAGE=0 ;;
    --keep-service) REMOVE_SERVICE=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

DEST="$WORKSPACE/src/$PKG_NAME"
log() { printf '\033[1;34m[uninstall]\033[0m %s\n' "$*"; }
if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

# 1. stop + disable + remove the systemd service
if [[ "$REMOVE_SERVICE" -eq 1 ]]; then
  if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE_NAME" || [[ -f "$UNIT_DST" ]]; then
    log "Stopping and disabling $SERVICE_NAME ..."
    $SUDO systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    $SUDO systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    if [[ -f "$UNIT_DST" ]]; then
      $SUDO rm -f "$UNIT_DST"
    fi
    $SUDO systemctl daemon-reload
    $SUDO systemctl reset-failed 2>/dev/null || true
    log "  service removed"
  else
    log "No $SERVICE_NAME installed; skipping."
  fi
fi

# 2. remove the package from the workspace
if [[ "$REMOVE_PACKAGE" -eq 1 ]]; then
  if [[ -d "$DEST" ]]; then
    log "Removing package from workspace: $DEST"
    rm -rf "$DEST"
    # rebuild so catkin drops the stale references
    if [[ -f "${ROS_SETUP:-/opt/ros/noetic/setup.bash}" ]]; then
      # shellcheck disable=SC1090
      source "${ROS_SETUP:-/opt/ros/noetic/setup.bash}" || true
      ( cd "$WORKSPACE" && catkin_make ) || true
      log "  workspace rebuilt"
    fi
  else
    log "Package not present at $DEST; skipping."
  fi
fi

log "Uninstall complete."
