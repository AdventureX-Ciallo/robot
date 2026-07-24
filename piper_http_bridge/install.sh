#!/usr/bin/env bash
###############################################################################
# piper_http_bridge -- one-shot installer for a headless Orange Pi 3B
#
# "Clone & run". Two backends:
#   * sdk  (DEFAULT) -- no ROS. Pure piper_sdk (python-can + SocketCAN) ->
#                       HTTP/TCP. Lightest option for the RK3566. Only needs
#                       python3 + pip. Full command set (stop/reset/go_zero/...).
#   * ros            -- bridge the official piper_ros (noetic) control node.
#                       Use if you also want MoveIt/RViz/ROS tooling.
#
# Re-running is safe: every step skips work that is already done.
#
# Usage:
#   ./piper_http_bridge/install.sh [options]
#
# Options:
#   --backend sdk|ros   control backend                  (default sdk)
#   --can IFACE         CAN interface name               (default can0)
#   --bitrate BPS       CAN bitrate                      (default 1000000)
#   --token SECRET      require this bearer token on the control port
#   --http-port N       HTTP control port                (default 8080)
#   --tcp-port N        TCP control port                 (default 9090)
#   --speed N           default arm speed %% 1-100        (default 50)
#   --workspace DIR     (ros backend) catkin workspace   (default ~/piper_ros_ws)
#   --no-auto-enable    do NOT auto-enable the arm on startup (default: it does)
#   --with-controller   also install the web control panel (piper-controller svc)
#   --controller-port N control panel port               (default 8000)
#   --controller-endpoint URL  endpoint the panel drives (default http://127.0.0.1:<http-port>)
#   --camera URL        MJPEG camera stream shown in the panel (optional)
#   --no-service        install but do not install/start systemd
#   --no-can            skip CAN bring-up (the server also brings can0 up itself)
#   -h | --help         show this help
###############################################################################
set -euo pipefail

# ---- defaults (override via flags or env) ------------------------------------
BACKEND="${BACKEND:-sdk}"
WORKSPACE="${WORKSPACE:-$HOME/piper_ros_ws}"
ROS_SETUP="${ROS_SETUP:-/opt/ros/noetic/setup.bash}"
PIPER_ROS_REPO="${PIPER_ROS_REPO:-https://github.com/agilexrobotics/piper_ros.git}"
PIPER_ROS_BRANCH="${PIPER_ROS_BRANCH:-noetic}"
CAN_IF="${CAN_IF:-can0}"
CAN_BITRATE="${CAN_BITRATE:-1000000}"
TOKEN="${TOKEN:-}"
HTTP_PORT="${HTTP_PORT:-8080}"
TCP_PORT="${TCP_PORT:-9090}"
SPEED="${SPEED:-50}"
AUTO_ENABLE=1
INSTALL_SERVICE=1
DO_CAN=1
WITH_CONTROLLER=0
CONTROLLER_PORT="${CONTROLLER_PORT:-8000}"
CONTROLLER_ENDPOINT="${CONTROLLER_ENDPOINT:-}"
CAMERA="${CAMERA:-}"

PKG_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # the package dir
PKG_NAME="piper_http_bridge"
SERVICE_NAME="piper-bridge.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME"
RUN_USER="${SUDO_USER:-${USER:-$(id -un 2>/dev/null || echo root)}}"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

# ---- parse args ---------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)     BACKEND="$2"; shift 2 ;;
    --workspace)   WORKSPACE="$2"; shift 2 ;;
    --can)         CAN_IF="$2"; shift 2 ;;
    --bitrate)     CAN_BITRATE="$2"; shift 2 ;;
    --token)       TOKEN="$2"; shift 2 ;;
    --http-port)   HTTP_PORT="$2"; shift 2 ;;
    --tcp-port)    TCP_PORT="$2"; shift 2 ;;
    --speed)       SPEED="$2"; shift 2 ;;
    --no-auto-enable) AUTO_ENABLE=0; shift ;;
    --no-service)  INSTALL_SERVICE=0; shift ;;
    --no-can)      DO_CAN=0; shift ;;
    --with-controller) WITH_CONTROLLER=1; shift ;;
    --controller-port) CONTROLLER_PORT="$2"; shift 2 ;;
    --controller-endpoint) CONTROLLER_ENDPOINT="$2"; shift 2 ;;
    --camera)        CAMERA="$2"; shift 2 ;;
    -h|--help)     sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown option: $1 (use --help)" ;;
  esac
done
[[ "$BACKEND" == "sdk" || "$BACKEND" == "ros" ]] || die "--backend must be 'sdk' or 'ros'"

log "piper_http_bridge installer  (backend=$BACKEND)"
log "  CAN iface : $CAN_IF @ $CAN_BITRATE"
log "  ports     : http=$HTTP_PORT tcp=$TCP_PORT  token=${TOKEN:+<set>}  speed=$SPEED"
command -v python3 >/dev/null 2>&1 || die "python3 not found."

# ---- 1. dependencies ----------------------------------------------------------
log "[1/4] Installing dependencies ..."
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
if [[ "$BACKEND" == "sdk" ]]; then
  $SUDO apt-get install -y -qq can-utils ethtool iproute2 python3-pip > /dev/null
  # The service runs as root and imports the SYSTEM python3, so install the
  # SDK system-wide (root), not just for the calling user.
  $SUDO python3 -m pip install --quiet --upgrade python-can piper_sdk
  ok "deps ready (can-utils, piper_sdk) -- no ROS needed"
else
  command -v git >/dev/null 2>&1 || die "git not found (sudo apt install git)."
  $SUDO apt-get install -y -qq \
      can-utils ethtool iproute2 git \
      python3-pip python3-wstool python3-catkin-tools python3-rosdep > /dev/null
  python3 -m pip install --quiet --upgrade python-can piper_sdk
  [[ -f "$ROS_SETUP" ]] || die "ROS setup not found at $ROS_SETUP (needed for --backend ros)."
  ok "deps ready (can-utils, piper_sdk, catkin tools)"
fi

# ---- 2. backend-specific build ------------------------------------------------
if [[ "$BACKEND" == "ros" ]]; then
  log "[2/4] Fetching + building official piper_ros ($PIPER_ROS_BRANCH) ..."
  mkdir -p "$WORKSPACE/src"
  if [[ -d "$WORKSPACE/src/piper" && -d "$WORKSPACE/src/piper_msgs" ]]; then
    ok "piper_ros packages already present"
  else
    TMP_CLONE="$(mktemp -d)"
    git clone --depth 1 --branch "$PIPER_ROS_BRANCH" "$PIPER_ROS_REPO" "$TMP_CLONE/piper_ros"
    cp -a "$TMP_CLONE/piper_ros/src/." "$WORKSPACE/src/"
    cp -a "$TMP_CLONE/piper_ros/."/*.sh "$WORKSPACE/" 2>/dev/null || true
    rm -rf "$TMP_CLONE"
    ok "piper_ros sources placed in $WORKSPACE/src"
  fi
  if [[ -d "$WORKSPACE/src/piper_moveit/moveit-1.1.11" ]]; then
    touch "$WORKSPACE/src/piper_moveit/moveit-1.1.11/CATKIN_IGNORE"
  fi
  DEST="$WORKSPACE/src/$PKG_NAME"
  rm -rf "$DEST"; cp -a "$PKG_SRC" "$DEST"
  chmod +x "$DEST/scripts/piper_http_bridge_node.py" || true
  # shellcheck disable=SC1090
  source "$ROS_SETUP"
  ( cd "$WORKSPACE" && catkin_make )
  # shellcheck disable=SC1090
  source "$WORKSPACE/devel/setup.bash"
  ok "ROS workspace built"
else
  log "[2/4] SDK backend: nothing to compile (pure python)."
  chmod +x "$PKG_SRC/scripts/piper_sdk_server.py" || true
  ok "server script ready at $PKG_SRC/scripts/piper_sdk_server.py"
fi

# ---- 3. CAN bring-up -----------------------------------------------------------
if [[ "$DO_CAN" -eq 1 ]]; then
  log "[3/4] Bringing up CAN interface '$CAN_IF' @ $CAN_BITRATE ..."
  CAN_ACTIVATE="$WORKSPACE/can_activate.sh"
  if ip link show "$CAN_IF" 2>/dev/null | grep -q "UP"; then
    ok "$CAN_IF already UP"
  elif [[ "$BACKEND" == "ros" && -f "$CAN_ACTIVATE" ]]; then
    $SUDO bash "$CAN_ACTIVATE" "$CAN_IF" "$CAN_BITRATE" || \
      warn "can_activate.sh failed (CAN may need a replug or a USB-address arg)"
  else
    # direct iproute2 bring-up (SDK path, or helper script absent)
    if ip link show "$CAN_IF" >/dev/null 2>&1; then
      $SUDO ip link set "$CAN_IF" down 2>/dev/null || true
      $SUDO ip link set "$CAN_IF" up type can bitrate "$CAN_BITRATE" \
        && ok "$CAN_IF up @ $CAN_BITRATE" || warn "failed to bring up $CAN_IF"
    else
      warn "$CAN_IF not present yet (USB-CAN unplugged?). Bring it up later with:"
      warn "  sudo ip link set $CAN_IF up type can bitrate $CAN_BITRATE"
    fi
  fi
else
  log "[3/4] Skipping CAN bring-up (--no-can)."
fi

# ---- 4. systemd service --------------------------------------------------------
AUTO_ARG=""
[[ "$AUTO_ENABLE" -eq 0 ]] && AUTO_ARG="--no-auto-enable"
if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  log "[4/4] Installing + starting systemd unit ($SERVICE_NAME) ..."
  UNIT="$(mktemp)"
  if [[ "$BACKEND" == "sdk" ]]; then
    cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Piper 6-axis arm HTTP/TCP control server (piper_sdk, no ROS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# run as root so the server can bring the CAN interface up itself (needs
# CAP_NET_ADMIN). The server auto-raises can0 and auto-enables the arm.
User=root
Group=root
ExecStart=/usr/bin/python3 $PKG_SRC/scripts/piper_sdk_server.py --can $CAN_IF --bitrate $CAN_BITRATE --host 0.0.0.0 --http-port $HTTP_PORT --tcp-port $TCP_PORT --speed $SPEED ${TOKEN:+--token $TOKEN} $AUTO_ARG
Restart=on-failure
RestartSec=3
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
UNIT_EOF
  else
    TOKEN_ARG=""
    [[ -n "$TOKEN" ]] && TOKEN_ARG="token:=$TOKEN"
    cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Piper 6-axis arm ROS control node + HTTP/TCP bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
ExecStartPre=/bin/bash -c 'ip link show $CAN_IF 2>/dev/null | grep -q UP || bash $WORKSPACE/can_activate.sh $CAN_IF $CAN_BITRATE || true'
ExecStart=/bin/bash -c 'source $ROS_SETUP && source $WORKSPACE/devel/setup.bash && roslaunch $PKG_NAME piper_http_bridge.launch can_port:=$CAN_IF auto_enable:=true http_port:=$HTTP_PORT tcp_port:=$TCP_PORT $TOKEN_ARG'
Restart=on-failure
RestartSec=3
TimeoutStartSec=90

[Install]
WantedBy=multi-user.target
UNIT_EOF
  fi
  $SUDO cp "$UNIT" "$UNIT_DST"
  rm -f "$UNIT"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  $SUDO systemctl restart "$SERVICE_NAME"
  sleep 3
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "$SERVICE_NAME is active"
  else
    warn "$SERVICE_NAME not active yet -- inspect: journalctl -u $SERVICE_NAME -n 80 --no-pager"
  fi
else
  log "[4/4] Skipping systemd install (--no-service)."
  if [[ "$BACKEND" == "sdk" ]]; then
    log "Run manually: python3 $PKG_SRC/scripts/piper_sdk_server.py --can $CAN_IF ${TOKEN:+--token $TOKEN}"
  fi
fi

# ---- optional: host web control panel ------------------------------------------
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"; IP_ADDR="${IP_ADDR:-<orangepi-ip>}"
CONTROLLER_SUMMARY=""
if [[ "$WITH_CONTROLLER" -eq 1 ]]; then
  log "[+] Installing host web control panel (port $CONTROLLER_PORT) ..."
  CTRL_INSTALL="$PKG_SRC/host_controller/install.sh"
  if [[ -f "$CTRL_INSTALL" ]]; then
    # default: panel runs on the same host as the arm service -> localhost
    CEP="${CONTROLLER_ENDPOINT:-http://127.0.0.1:$HTTP_PORT}"
    CTRL_ARGS=(--endpoint "$CEP" --port "$CONTROLLER_PORT" --speed "$SPEED")
    [[ -n "$TOKEN" ]] && CTRL_ARGS+=(--token "$TOKEN")
    [[ -n "$CAMERA" ]] && CTRL_ARGS+=(--camera "$CAMERA")
    [[ "$INSTALL_SERVICE" -eq 0 ]] && CTRL_ARGS+=(--no-service)
    if bash "$CTRL_INSTALL" "${CTRL_ARGS[@]}"; then
      ok "control panel installed"
      CONTROLLER_SUMMARY="  Panel     : http://$IP_ADDR:$CONTROLLER_PORT/   (open in a browser)"
    else
      warn "control panel install failed (arm service is unaffected)"
    fi
  else
    warn "host_controller/install.sh not found at $CTRL_INSTALL; skipping panel"
  fi
fi

# ---- summary ------------------------------------------------------------------
cat <<EOF

$(printf '\033[1;32m')================ install complete ================$(printf '\033[0m')
  Backend   : $BACKEND
  CAN iface : $CAN_IF @ $CAN_BITRATE
  HTTP      : http://$IP_ADDR:$HTTP_PORT   (GET /state, POST /cmd)
  TCP       : tcp://$IP_ADDR:$TCP_PORT      (newline-delimited JSON)
  Auth      : ${TOKEN:+bearer token ENABLED}${TOKEN:-none (WARNING: open control port!)}
$CONTROLLER_SUMMARY

Quick test:
  curl http://$IP_ADDR:$HTTP_PORT/state
  curl -X POST http://$IP_ADDR:$HTTP_PORT/cmd -d '{"action":"enable"}'
  curl -X POST http://$IP_ADDR:$HTTP_PORT/cmd -d '{"action":"joint_ctrl","joints":[0,30,-30,0,20,0],"speed":10}'

Manage:
  sudo systemctl status $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f
  ./piper_http_bridge/update.sh        # re-sync + restart after editing code
  ./piper_http_bridge/uninstall.sh     # remove service + package
EOF
