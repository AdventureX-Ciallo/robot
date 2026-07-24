#!/usr/bin/env bash
#
# update.sh -- re-copy the package into the catkin workspace, rebuild, and
#              restart the systemd service if it is installed.
#
# Usage:
#   ./update.sh                 # update code + rebuild + restart service
#   ./update.sh --no-restart    # update code + rebuild, leave service alone
#   WORKSPACE=~/catkin_ws ./update.sh
#
set -euo pipefail

WORKSPACE="${WORKSPACE:-$HOME/piper_ros}"
ROS_SETUP="${ROS_SETUP:-/opt/ros/noetic/setup.bash}"
PKG_NAME="piper_http_bridge"
RESTART=1

for arg in "$@"; do
  case "$arg" in
    --no-restart) RESTART=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$WORKSPACE/src/$PKG_NAME"

log()  { printf '\033[1;34m[update]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

[[ -f "$ROS_SETUP" ]] || { err "ROS setup not found: $ROS_SETUP"; exit 1; }
[[ -d "$WORKSPACE/src" ]] || { err "workspace src not found: $WORKSPACE/src"; exit 1; }

# 1. refresh the package source in the workspace
if [[ "$SRC_DIR" != "$DEST" ]]; then
  log "Syncing package -> $DEST"
  mkdir -p "$WORKSPACE/src"
  rm -rf "$DEST"
  cp -a "$SRC_DIR" "$DEST"
  chmod +x "$DEST/scripts/piper_http_bridge_node.py" || true
else
  log "Package already at $DEST"
fi

# 2. rebuild
log "Rebuilding ..."
# shellcheck disable=SC1090
source "$ROS_SETUP"
( cd "$WORKSPACE" && catkin_make )
# shellcheck disable=SC1090
source "$WORKSPACE/devel/setup.bash"
log "  build OK"

# 3. restart the service if present
if [[ "$RESTART" -eq 1 ]]; then
  if systemctl list-unit-files 2>/dev/null | grep -q '^piper-bridge.service'; then
    if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi
    log "Restarting piper-bridge.service ..."
    $SUDO systemctl daemon-reload
    $SUDO systemctl restart piper-bridge.service
    sleep 2
    systemctl is-active --quiet piper-bridge.service \
      && log "  service is active" \
      || err "  service failed to stay active -- check: journalctl -u piper-bridge -n 50"
  else
    log "piper-bridge.service not installed; skipping restart."
    log "Launch manually with: roslaunch $PKG_NAME piper_http_bridge.launch"
  fi
fi

log "Update complete."
