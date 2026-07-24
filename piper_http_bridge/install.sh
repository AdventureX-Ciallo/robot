#!/usr/bin/env bash
###############################################################################
# piper_http_bridge -- one-shot installer for a headless Orange Pi 3B
#
# "Clone & run": after `git clone <this-repo>`, run this script once and it
# will, idempotently:
#   1. install system + python dependencies (can-utils, piper_sdk, catkin...)
#   2. clone & build the official agilex piper_ros (noetic) into a workspace
#   3. copy this bridge package into that workspace and build it
#   4. install & start a systemd unit that brings up CAN (can0) + the nodes
#
# Re-running is safe: every step skips work that is already done.
#
# Usage:
#   ./piper_http_bridge/install.sh [options]
#
# Options:
#   --workspace DIR     catkin workspace to create/use   (default ~/piper_ros_ws)
#   --can IFACE         CAN interface name               (default can0)
#   --bitrate BPS       CAN bitrate                      (default 1000000)
#   --token SECRET      require this bearer token on the control port
#   --http-port N       HTTP control port                (default 8080)
#   --tcp-port N        TCP control port                 (default 9090)
#   --no-service        build everything but do not install/start systemd
#   --no-can            skip CAN bring-up (you manage the interface yourself)
#   -h | --help         show this help
###############################################################################
set -euo pipefail

# ---- defaults (override via flags or env) ------------------------------------
WORKSPACE="${WORKSPACE:-$HOME/piper_ros_ws}"
ROS_SETUP="${ROS_SETUP:-/opt/ros/noetic/setup.bash}"
PIPER_ROS_REPO="${PIPER_ROS_REPO:-https://github.com/agilexrobotics/piper_ros.git}"
PIPER_ROS_BRANCH="${PIPER_ROS_BRANCH:-noetic}"
CAN_IF="${CAN_IF:-can0}"
CAN_BITRATE="${CAN_BITRATE:-1000000}"
TOKEN="${TOKEN:-}"
HTTP_PORT="${HTTP_PORT:-8080}"
TCP_PORT="${TCP_PORT:-9090}"
INSTALL_SERVICE=1
DO_CAN=1

# the directory that contains this script == the piper_http_bridge package
PKG_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_NAME="piper_http_bridge"
SERVICE_NAME="piper-bridge.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME"
RUN_USER="${SUDO_USER:-$USER}"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

# ---- parse args ---------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)  WORKSPACE="$2"; shift 2 ;;
    --can)        CAN_IF="$2"; shift 2 ;;
    --bitrate)    CAN_BITRATE="$2"; shift 2 ;;
    --token)      TOKEN="$2"; shift 2 ;;
    --http-port)  HTTP_PORT="$2"; shift 2 ;;
    --tcp-port)   TCP_PORT="$2"; shift 2 ;;
    --no-service) INSTALL_SERVICE=0; shift ;;
    --no-can)     DO_CAN=0; shift ;;
    -h|--help)    sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown option: $1 (use --help)" ;;
  esac
done

# ---- 0. preconditions ---------------------------------------------------------
log "piper_http_bridge installer"
log "  workspace : $WORKSPACE"
log "  CAN iface : $CAN_IF @ $CAN_BITRATE"
log "  ports     : http=$HTTP_PORT tcp=$TCP_PORT  token=${TOKEN:+<set>}"
[[ -f "$ROS_SETUP" ]] || die "ROS setup not found at $ROS_SETUP.
       Install ROS Noetic first (http://wiki.ros.org/noetic/Installation/Ubuntu),
       or point ROS_SETUP at your distro's setup.bash."

command -v git >/dev/null 2>&1 || die "git not found (sudo apt install git)."

# ---- 1. dependencies ----------------------------------------------------------
log "[1/5] Installing system + python dependencies ..."
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq \
    can-utils ethtool iproute2 git \
    python3-pip python3-wstool python3-catkin-tools python3-rosdep \
    > /dev/null
# piper_sdk needs python-can >= 3.3.4
python3 -m pip install --quiet --upgrade python-can piper_sdk
ok "dependencies ready (can-utils, piper_sdk, catkin tools)"

# ---- 2. clone + build official piper_ros --------------------------------------
log "[2/5] Fetching official piper_ros ($PIPER_ROS_BRANCH) into workspace ..."
mkdir -p "$WORKSPACE/src"
if [[ -d "$WORKSPACE/src/piper" && -d "$WORKSPACE/src/piper_msgs" ]]; then
  ok "piper_ros packages already present, skipping clone"
elif [[ -d "$WORKSPACE/src/piper_ros/.git" ]]; then
  log "  piper_ros already cloned, pulling latest ..."
  git -C "$WORKSPACE/src/piper_ros" fetch --quiet origin "$PIPER_ROS_BRANCH" || true
  git -C "$WORKSPACE/src/piper_ros" checkout --quiet "$PIPER_ROS_BRANCH" || true
else
  # clone into a temp dir, then move the catkin packages out of its src/ into ours
  TMP_CLONE="$(mktemp -d)"
  log "  cloning $PIPER_ROS_REPO (branch $PIPER_ROS_BRANCH) ..."
  git clone --depth 1 --branch "$PIPER_ROS_BRANCH" "$PIPER_ROS_REPO" "$TMP_CLONE/piper_ros"
  # the repo nests its catkin packages under src/
  cp -a "$TMP_CLONE/piper_ros/src/." "$WORKSPACE/src/"
  # keep the helper scripts (can_activate.sh etc.) alongside for reference
  cp -a "$TMP_CLONE/piper_ros/."/*.sh "$WORKSPACE/" 2>/dev/null || true
  rm -rf "$TMP_CLONE"
  ok "piper_ros sources placed in $WORKSPACE/src"
fi

# If MoveIt from source causes trouble on the Pi, ignore it (use apt moveit).
if [[ -d "$WORKSPACE/src/piper_moveit/moveit-1.1.11" ]]; then
  if [[ ! -f "$WORKSPACE/src/piper_moveit/moveit-1.1.11/CATKIN_IGNORE" ]]; then
    log "  ignoring vendored moveit-1.1.11 (install ros-noetic-moveit via apt instead)"
    touch "$WORKSPACE/src/piper_moveit/moveit-1.1.11/CATKIN_IGNORE"
  fi
fi

# ---- 3. copy this bridge package + build --------------------------------------
log "[3/5] Installing bridge package and building workspace ..."
DEST="$WORKSPACE/src/$PKG_NAME"
if [[ "$PKG_SRC" != "$DEST" ]]; then
  rm -rf "$DEST"
  cp -a "$PKG_SRC" "$DEST"
fi
chmod +x "$DEST/scripts/piper_http_bridge_node.py" || true

# shellcheck disable=SC1090
source "$ROS_SETUP"
( cd "$WORKSPACE" && catkin_make )
# shellcheck disable=SC1090
source "$WORKSPACE/devel/setup.bash"
ok "workspace built"

# ---- 4. CAN bring-up (now, so the arm is reachable immediately) ---------------
if [[ "$DO_CAN" -eq 1 ]]; then
  log "[4/5] Bringing up CAN interface '$CAN_IF' @ $CAN_BITRATE ..."
  CAN_ACTIVATE="$WORKSPACE/can_activate.sh"
  if ip link show "$CAN_IF" 2>/dev/null | grep -q "UP"; then
    ok "$CAN_IF already UP"
  elif [[ -f "$CAN_ACTIVATE" ]]; then
    $SUDO bash "$CAN_ACTIVATE" "$CAN_IF" "$CAN_BITRATE" || \
      warn "can_activate.sh returned non-zero (CAN may need a replug or a USB-address arg)"
  else
    # fallback: direct iproute2 bring-up
    warn "can_activate.sh not found; using iproute2 directly"
    $SUDO ip link set "$CAN_IF" down 2>/dev/null || true
    $SUDO ip link set "$CAN_IF" up type can bitrate "$CAN_BITRATE" || \
      warn "failed to bring up $CAN_IF"
  fi
else
  log "[4/5] Skipping CAN bring-up (--no-can)."
fi

# ---- 5. systemd service --------------------------------------------------------
if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  log "[5/5] Installing + starting systemd unit ($SERVICE_NAME) ..."
  TOKEN_ARG=""
  [[ -n "$TOKEN" ]] && TOKEN_ARG="token:=$TOKEN"
  UNIT="$(mktemp)"
  cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Piper 6-axis arm ROS control node + HTTP/TCP bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
# bring CAN up before ROS (ignore failure if already up)
ExecStartPre=/bin/bash -c 'ip link show $CAN_IF 2>/dev/null | grep -q UP || bash $WORKSPACE/can_activate.sh $CAN_IF $CAN_BITRATE || true'
ExecStart=/bin/bash -c 'source $ROS_SETUP && source $WORKSPACE/devel/setup.bash && roslaunch $PKG_NAME piper_http_bridge.launch can_port:=$CAN_IF auto_enable:=true http_port:=$HTTP_PORT tcp_port:=$TCP_PORT $TOKEN_ARG'
Restart=on-failure
RestartSec=3
TimeoutStartSec=90

[Install]
WantedBy=multi-user.target
UNIT_EOF
  $SUDO cp "$UNIT" "$UNIT_DST"
  rm -f "$UNIT"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  $SUDO systemctl restart "$SERVICE_NAME"
  sleep 3
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "$SERVICE_NAME is active"
  else
    warn "$SERVICE_NAME not active yet -- inspect with:"
    warn "  journalctl -u $SERVICE_NAME -n 80 --no-pager"
  fi
else
  log "[5/5] Skipping systemd install (--no-service)."
fi

# ---- summary ------------------------------------------------------------------
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP_ADDR="${IP_ADDR:-<orangepi-ip>}"
cat <<EOF

$(printf '\033[1;32m')================ install complete ================$(printf '\033[0m')
  Workspace : $WORKSPACE
  CAN iface : $CAN_IF @ $CAN_BITRATE
  HTTP      : http://$IP_ADDR:$HTTP_PORT   (GET /state, POST /cmd)
  TCP       : tcp://$IP_ADDR:$TCP_PORT      (newline-delimited JSON)
  Auth      : ${TOKEN:+bearer token ENABLED}${TOKEN:-none (WARNING: open control port!)}

Quick test:
  curl http://$IP_ADDR:$HTTP_PORT/state
  curl -X POST http://$IP_ADDR:$HTTP_PORT/cmd -H 'Content-Type: application/json' \\
       -d '{"action":"enable"}'
  curl -X POST http://$IP_ADDR:$HTTP_PORT/cmd -d '{"action":"joint_ctrl","joints":[0,30,-30,0,20,0],"speed":10}'

Manage:
  sudo systemctl status $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f
  ./piper_http_bridge/update.sh        # rebuild + restart after editing code
  ./piper_http_bridge/uninstall.sh     # remove service + package
EOF
