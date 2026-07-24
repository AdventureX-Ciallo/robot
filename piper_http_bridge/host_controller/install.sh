#!/usr/bin/env bash
###############################################################################
# host_controller install -- install the Piper web control panel as a systemd
# service on the upper machine. Pure python3 stdlib, no pip, no ROS.
#
# Usage:
#   ./install.sh --endpoint http://192.168.1.100:8080 --token SECRET [options]
#
# Options:
#   --endpoint URL   arm control endpoint        (default http://127.0.0.1:8080)
#   --token SECRET   endpoint bearer token
#   --port N         panel listen port           (default 8000)
#   --host IP        panel listen host           (default 0.0.0.0)
#   --speed N        joint jog speed %%          (default 30)
#   --prefix DIR     install location            (default /opt/piper_controller)
#   --no-service     install files but do not install/start systemd
#   -h | --help      show this help
###############################################################################
set -euo pipefail

ENDPOINT="${ENDPOINT:-http://127.0.0.1:8080}"
TOKEN="${TOKEN:-}"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
SPEED="${SPEED:-30}"
PREFIX="${PREFIX:-/opt/piper_controller}"
INSTALL_SERVICE=1
SERVICE_NAME="piper-controller.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME"
RUN_USER="${SUDO_USER:-${USER:-root}}"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoint)  ENDPOINT="$2"; shift 2 ;;
    --token)     TOKEN="$2"; shift 2 ;;
    --port)      PORT="$2"; shift 2 ;;
    --host)      HOST="$2"; shift 2 ;;
    --speed)     SPEED="$2"; shift 2 ;;
    --prefix)    PREFIX="$2"; shift 2 ;;
    --no-service) INSTALL_SERVICE=0; shift ;;
    -h|--help)   sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown option: $1 (use --help)" ;;
  esac
done

log "host_controller install"
log "  endpoint : $ENDPOINT"
log "  panel    : http://$HOST:$PORT/   prefix=$PREFIX"
command -v python3 >/dev/null 2>&1 || die "python3 not found."

# 1. copy files to prefix (controller + panel + the piper_client it imports)
log "[1/3] Installing files -> $PREFIX"
$SUDO mkdir -p "$PREFIX/host_controller" "$PREFIX/client"
$SUDO cp -a "$SRC/piper_controller.py" "$SRC/panel.html" "$PREFIX/host_controller/"
# piper_client lives in ../client relative to the controller
CLIENT_SRC="$SRC/../client/piper_client.py"
[[ -f "$CLIENT_SRC" ]] || die "piper_client.py not found at $CLIENT_SRC"
$SUDO cp -a "$CLIENT_SRC" "$PREFIX/client/"
$SUDO chown -R "$RUN_USER:$RUN_USER" "$PREFIX" 2>/dev/null || true
ok "files installed"

# 2. systemd service
if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  log "[2/3] Installing + starting systemd unit ($SERVICE_NAME)"
  TOKEN_ARG=""
  [[ -n "$TOKEN" ]] && TOKEN_ARG="--token $TOKEN"
  UNIT="$(mktemp)"
  cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Piper web control panel (host controller)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
ExecStart=/usr/bin/python3 $PREFIX/host_controller/piper_controller.py --endpoint $ENDPOINT --host $HOST --port $PORT --speed $SPEED $TOKEN_ARG
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT_EOF
  $SUDO cp "$UNIT" "$UNIT_DST"
  rm -f "$UNIT"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  $SUDO systemctl restart "$SERVICE_NAME"
  sleep 2
  systemctl is-active --quiet "$SERVICE_NAME" \
    && ok "$SERVICE_NAME is active" \
    || warn "not active yet -- check: journalctl -u $SERVICE_NAME -n 50"
else
  log "[2/3] Skipping systemd (--no-service)"
fi

# 3. summary
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"; IP_ADDR="${IP_ADDR:-127.0.0.1}"
cat <<EOF

$(printf '\033[1;32m')============ controller install complete ============$(printf '\033[0m')
  Panel    : http://$IP_ADDR:$PORT/    (open in a browser)
  Endpoint : $ENDPOINT
  Service  : $SERVICE_NAME
  Manage   : sudo systemctl status $SERVICE_NAME
             journalctl -u $SERVICE_NAME -f
  Uninstall: sudo systemctl disable --now $SERVICE_NAME && sudo rm -rf $PREFIX $UNIT_DST

Drive the arm with WASD/QE/XZ and G/H in the web page.
EOF
