#!/usr/bin/env bash
#
# install.sh -- install piper_http_bridge on an Orange Pi 3B (Ubuntu + ROS Noetic)
#
# What it does:
#   1. sanity-checks the environment (ROS distro, catkin workspace, CAN utils)
#   2. copies this package into your catkin workspace src/
#   3. builds with catkin_make
#   4. (optional) installs + enables the systemd auto-start unit
#
# Usage:
#   ./install.sh                 # interactive-ish, sane defaults
#   ./install.sh --no-service    # build only, do not touch systemd
#   WORKSPACE=~/catkin_ws ./install.sh
#
set -euo pipefail

# ---- configurable -----------------------------------------------------------
WORKSPACE="${WORKSPACE:-$HOME/piper_ros}"     # catkin workspace containing src/piper
ROS_SETUP="${ROS_SETUP:-/opt/ros/noetic/setup.bash}"
PKG_NAME="piper_http_bridge"
CAN_IF="${CAN_IF:-can0}"
CAN_BITRATE="${CAN_BITRATE:-1000000}"
INSTALL_SERVICE=1
SERVICE_USER="${SUDO_USER:-$USER}"

for arg in "$@"; do
  case "$arg" in
    --no-service) INSTALL_SERVICE=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # the piper_http_bridge dir
PARENT_DIR="$(dirname "$SRC_DIR")"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

# ---- 0. must NOT be run as root (catkin + systemd user unit) ----------------
if [[ "$EUID" -eq 0 ]]; then
  warn "Running as root. Files will be owned by root; prefer a normal user."
fi

# ---- 1. sanity checks --------------------------------------------------------
log "Checking environment ..."
if [[ ! -f "$ROS_SETUP" ]]; then
  err "ROS setup not found at $ROS_SETUP"
  err "Set ROS_SETUP=/path/to/setup.bash or install ROS Noetic first."
  exit 1
fi
log "  ROS setup: $ROS_SETUP"

if [[ ! -d "$WORKSPACE/src" ]]; then
  err "catkin workspace src/ not found at $WORKSPACE/src"
  err "Set WORKSPACE=/path/to/ws (the workspace that already builds piper_ros)."
  exit 1
fi
log "  Workspace: $WORKSPACE"

# the official piper control package should already be present
if [[ ! -d "$WORKSPACE/src/piper" && ! -d "$WORKSPACE/src/piper_ros/piper" ]]; then
  warn "official 'piper' package not found under $WORKSPACE/src -- the bridge needs"
  warn "piper_ctrl_single_node.py at runtime. Make sure piper_ros is installed."
fi

if ! command -v catkin_make >/dev/null 2>&1; then
  warn "catkin_make not on PATH; sourcing ROS setup should fix it."
fi

# can-utils for CAN bring-up (informational)
if ! command -v ip >/dev/null 2>&1; then
  warn "'ip' not found; install iproute2 (sudo apt install iproute2)."
fi

# ---- 2. copy package into workspace src --------------------------------------
DEST="$WORKSPACE/src/$PKG_NAME"
log "Copying package -> $DEST"
mkdir -p "$WORKSPACE/src"
if [[ "$SRC_DIR" == "$DEST" ]]; then
  log "  already in place, skipping copy"
else
  rm -rf "$DEST"
  cp -a "$SRC_DIR" "$DEST"
fi
chmod +x "$DEST/scripts/piper_http_bridge_node.py" || true

# ---- 3. build -----------------------------------------------------------------
log "Building workspace (catkin_make) ..."
# shellcheck disable=SC1090
source "$ROS_SETUP"
( cd "$WORKSPACE" && catkin_make )
# shellcheck disable=SC1090
source "$WORKSPACE/devel/setup.bash"
log "  build OK"

# ---- 4. systemd service (optional) -------------------------------------------
if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  log "Installing systemd unit ..."
  UNIT_SRC="$DEST/scripts/piper-bridge.service"
  UNIT_DST="/etc/systemd/system/piper-bridge.service"
  TMP_UNIT="$(mktemp)"
  # Rewrite paths/user in the template unit to match this machine.
  sed \
    -e "s|^User=.*|User=${SERVICE_USER}|" \
    -e "s|^Group=.*|Group=${SERVICE_USER}|" \
    -e "s|/opt/ros/noetic/setup.bash|${ROS_SETUP}|g" \
    -e "s|/home/orangepi/piper_ros|${WORKSPACE}|g" \
    -e "s|can0 1000000|${CAN_IF} ${CAN_BITRATE}|g" \
    "$UNIT_SRC" > "$TMP_UNIT"
  if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi
  $SUDO cp "$TMP_UNIT" "$UNIT_DST"
  rm -f "$TMP_UNIT"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable piper-bridge.service
  log "  installed: $UNIT_DST (enabled at boot)"
  log "  start now with:  sudo systemctl start piper-bridge.service"
else
  log "Skipping systemd install (--no-service)."
fi

cat <<EOF

$(printf '\033[1;32m')Done.$(printf '\033[0m')

Next steps:
  1. Activate CAN:        bash can_activate.sh ${CAN_IF} ${CAN_BITRATE}
  2. Launch manually:     roslaunch ${PKG_NAME} piper_http_bridge.launch can_port:=${CAN_IF} auto_enable:=true
     ... or via systemd:  sudo systemctl start piper-bridge.service
  3. Test the port:       curl http://<orangepi-ip>:8080/state
                          curl -X POST http://<orangepi-ip>:8080/cmd -d '{"action":"enable"}'

Security: set a token at launch ( token:=YOURSECRET ) so the control port is not wide open.
EOF
