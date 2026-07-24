#!/usr/bin/env bash
###############################################################################
# update.sh -- re-copy this bridge package into the catkin workspace, rebuild,
#              and restart the systemd service if it is installed.
#
# Usage:
#   ./piper_http_bridge/update.sh [--workspace DIR] [--no-restart]
###############################################################################
set -euo pipefail

WORKSPACE="${WORKSPACE:-$HOME/piper_ros_ws}"
ROS_SETUP="${ROS_SETUP:-/opt/ros/noetic/setup.bash}"
PKG_NAME="piper_http_bridge"
SERVICE_NAME="piper-bridge.service"
RESTART=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)  WORKSPACE="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    -h|--help)    sed -n '2,8p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

PKG_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$WORKSPACE/src/$PKG_NAME"
log() { printf '\033[1;34m[update]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

[[ -f "$ROS_SETUP" ]]      || die "ROS setup not found: $ROS_SETUP"
[[ -d "$WORKSPACE/src" ]]  || die "workspace src not found: $WORKSPACE/src (run install.sh first)"

# 1. refresh package source
if [[ "$PKG_SRC" != "$DEST" ]]; then
  log "Syncing package -> $DEST"
  mkdir -p "$WORKSPACE/src"
  rm -rf "$DEST"
  cp -a "$PKG_SRC" "$DEST"
  chmod +x "$DEST/scripts/piper_http_bridge_node.py" || true
else
  log "Package already in place"
fi

# 2. rebuild
log "Rebuilding workspace ..."
# shellcheck disable=SC1090
source "$ROS_SETUP"
( cd "$WORKSPACE" && catkin_make )
# shellcheck disable=SC1090
source "$WORKSPACE/devel/setup.bash"
log "  build OK"

# 3. restart service if present
if [[ "$RESTART" -eq 1 ]]; then
  if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE_NAME"; then
    log "Restarting $SERVICE_NAME ..."
    $SUDO systemctl daemon-reload
    $SUDO systemctl restart "$SERVICE_NAME"
    sleep 2
    if systemctl is-active --quiet "$SERVICE_NAME"; then
      log "  service active"
    else
      die "  service failed to stay up -- check: journalctl -u $SERVICE_NAME -n 80"
    fi
  else
    log "$SERVICE_NAME not installed; skipping restart."
    log "Launch manually: roslaunch $PKG_NAME piper_http_bridge.launch"
  fi
fi
log "Update complete."
