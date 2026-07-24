#!/usr/bin/env bash
###############################################################################
# camera_stream -- one-shot installer for a headless Orange Pi 3B
#
# "Clone & run". Installs the MJPEG live-stream daemon for the camera attached
# to the Orange Pi and wires it into systemd so it comes up on boot.
#
# Pure python3 + ffmpeg -- no ROS, no OpenCV. Re-running is safe: every step
# skips work that is already done.
#
# Usage:
#   ./camera_stream/install.sh [options]
#
# Options:
#   --device PATH     V4L2 device                       (default /dev/video0)
#   --port N          HTTP port                         (default 8090)
#   --view PRESET     resolution preset (qvga/vga/720p/1080p) -- overrides w/h
#   --width N         capture width                     (default 1280)
#   --height N        capture height                    (default 720)
#   --fps N           frame rate                        (default 30)
#   --quality N       JPEG quality 2 (best) .. 31 (worst) (default 5)
#   --input-format F  auto|mjpeg|yuyv|h264              (default auto)
#                     'h264' = camera emits H.264 (needed for WebRTC)
#   --webrtc          enable low-latency WebRTC: install MediaMTX and push
#                     H.264 to it (--rtsp-url defaults to rtsp://127.0.0.1:8554/cam)
#   --rtsp-url URL    MediaMTX RTSP target for WebRTC   (implies --webrtc)
#   --rtsp-transport T  RTSP transport tcp|udp          (default tcp)
#   --mediamtx        (re)install the MediaMTX WebRTC gateway too
#   --token SECRET    require this bearer token on the stream/viewer
#   --no-service      install but do not install/start systemd
#   -h | --help       show this help
###############################################################################
set -euo pipefail

# ---- defaults (override via flags or env) ------------------------------------
DEVICE="${DEVICE:-/dev/video0}"
PORT="${PORT:-8090}"
VIEW="${VIEW:-}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
FPS="${FPS:-30}"
QUALITY="${QUALITY:-5}"
INPUT_FORMAT="${INPUT_FORMAT:-auto}"
RTSP_URL="${RTSP_URL:-}"
RTSP_TRANSPORT="${RTSP_TRANSPORT:-tcp}"
WEBRTC=0
MEDIAMTX=0
TOKEN="${TOKEN:-}"
INSTALL_SERVICE=1

PKG_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # the package dir
SERVICE_NAME="camera-stream.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

if command -v sudo >/dev/null 2>&1; then SUDO=sudo; else SUDO=; fi

# ---- parse args ---------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --device)        DEVICE="$2"; shift 2 ;;
    --port)          PORT="$2"; shift 2 ;;
    --view)          VIEW="$2"; shift 2 ;;
    --width)         WIDTH="$2"; shift 2 ;;
    --height)        HEIGHT="$2"; shift 2 ;;
    --fps)           FPS="$2"; shift 2 ;;
    --quality)       QUALITY="$2"; shift 2 ;;
    --input-format)  INPUT_FORMAT="$2"; shift 2 ;;
    --webrtc)        WEBRTC=1; shift ;;
    --rtsp-url)      RTSP_URL="$2"; WEBRTC=1; shift 2 ;;
    --rtsp-transport) RTSP_TRANSPORT="$2"; shift 2 ;;
    --mediamtx)      MEDIAMTX=1; WEBRTC=1; shift ;;
    --token)         TOKEN="$2"; shift 2 ;;
    --no-service)    INSTALL_SERVICE=0; shift ;;
    -h|--help)       sed -n '2,38p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown option: $1 (use --help)" ;;
  esac
done

# WebRTC needs a push target; default to a local MediaMTX on the standard path.
if [[ "$WEBRTC" -eq 1 && -z "$RTSP_URL" ]]; then
  RTSP_URL="rtsp://127.0.0.1:8554/cam"
  MEDIAMTX=1            # no explicit target -> we must be running MediaMTX
fi

log "camera_stream installer"
log "  device : $DEVICE   ${WIDTH}x${HEIGHT} @ ${FPS}fps  q=$QUALITY  in=$INPUT_FORMAT"
log "  port   : $PORT     token=${TOKEN:+<set>}  view=${VIEW:-<w/h>}"
[[ "$WEBRTC" -eq 1 ]] && log "  webrtc : ON  -> $RTSP_URL"
command -v python3 >/dev/null 2>&1 || die "python3 not found."

# ---- 1. dependencies ----------------------------------------------------------
log "[1/3] Installing dependencies (ffmpeg, v4l-utils) ..."
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq ffmpeg v4l-utils > /dev/null
command -v ffmpeg  >/dev/null 2>&1 || die "ffmpeg not found after install."
command -v ffprobe >/dev/null 2>&1 || die "ffprobe not found after install."
ok "ffmpeg + ffprobe ready"

# ---- 2. camera sanity check ----------------------------------------------------
log "[2/3] Checking camera device ..."
chmod +x "$PKG_SRC/scripts/camera_stream_server.py" || true
if [[ -e "$DEVICE" ]]; then
  ok "found $DEVICE"
  if command -v v4l2-ctl >/dev/null 2>&1; then
    v4l2-ctl --device="$DEVICE" --list-formats-ext 2>/dev/null | \
      grep -E '^\s*\[|Size: Discrete' | head -20 || true
  fi
else
  warn "$DEVICE not present yet (camera unplugged?). The service auto-retries."
  warn "list cameras later with:  v4l2-ctl --list-devices"
fi

# ---- 3. MediaMTX WebRTC gateway (optional) --------------------------------------
if [[ "$MEDIAMTX" -eq 1 ]]; then
  log "[3/4] Installing MediaMTX WebRTC gateway ..."
  # MediaMTX path MUST match the last segment of the RTSP push URL, or the
  # WHEP endpoint the browser hits (/path/whep) won't exist -> 404.
  PATH_NAME="$(printf '%s' "$RTSP_URL" | sed -n 's#.*/\([^/]*\)$#\1#p')"
  PATH_NAME="${PATH_NAME:-cam}"
  [[ -z "$RTSP_URL" ]] && die "--mediamtx needs --rtsp-url rtsp://.../$PATH_NAME (the path MediaMTX serves)"
  log "      MediaMTX path = /$PATH_NAME (browser WHEP: http://<ip>:8889/$PATH_NAME/whep)"
  bash "$PKG_SRC/install_mediamtx.sh" --path "$PATH_NAME" \
    $([[ "$INSTALL_SERVICE" -eq 0 ]] && echo --no-service)
else
  log "[3/4] MediaMTX not requested (use --mediamtx or --webrtc to enable)."
fi

# ---- 4. systemd service --------------------------------------------------------
VIEW_ARG=""
[[ -n "$VIEW" ]] && VIEW_ARG="--view $VIEW"
RTSP_ARGS=""
[[ "$WEBRTC" -eq 1 ]] && RTSP_ARGS="--rtsp-url $RTSP_URL --rtsp-transport $RTSP_TRANSPORT"
if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  log "[4/4] Installing + starting systemd unit ($SERVICE_NAME) ..."
  UNIT="$(mktemp)"
  cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=HTTP MJPEG + WebRTC live-stream daemon for the Orange Pi camera
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# root can always open /dev/video*; switch to a video-group user if you prefer.
User=root
Group=root
ExecStart=/usr/bin/python3 $PKG_SRC/scripts/camera_stream_server.py --device $DEVICE --host 0.0.0.0 --port $PORT --width $WIDTH --height $HEIGHT --fps $FPS --quality $QUALITY --input-format $INPUT_FORMAT $VIEW_ARG $RTSP_ARGS ${TOKEN:+--token $TOKEN}
Restart=on-failure
RestartSec=3
TimeoutStartSec=30

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
    warn "$SERVICE_NAME not active yet -- inspect: journalctl -u $SERVICE_NAME -n 80 --no-pager"
  fi
else
  log "[4/4] Skipping systemd install (--no-service)."
  log "Run manually: python3 $PKG_SRC/scripts/camera_stream_server.py --device $DEVICE --port $PORT $RTSP_ARGS ${TOKEN:+--token $TOKEN}"
fi

# ---- summary ------------------------------------------------------------------
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"; IP_ADDR="${IP_ADDR:-<orangepi-ip>}"
if [[ "$WEBRTC" -eq 1 ]]; then
  RTC_PATH="$(printf '%s' "$RTSP_URL" | sed -n 's#.*/\([^/]*\)$#\1#p')"; RTC_PATH="${RTC_PATH:-cam}"
  RTC_LINE="  WebRTC : http://$IP_ADDR:8889/$RTC_PATH   (sub-second; embed in the panel)"
else
  RTC_LINE="  WebRTC : off (re-run with --webrtc to enable sub-second streaming)"
fi
cat <<EOF

$(printf '\033[1;32m')================ install complete ================$(printf '\033[0m')
  Device : $DEVICE   ${WIDTH}x${HEIGHT} @ ${FPS}fps
$RTC_LINE
  Viewer : http://$IP_ADDR:$PORT/          (MJPEG fallback, in any browser)
  Stream : http://$IP_ADDR:$PORT/stream    (MJPEG; use as an <img> src / in VLC)
  Snap   : http://$IP_ADDR:$PORT/snapshot  (single JPEG)
  State  : http://$IP_ADDR:$PORT/state
  Auth   : ${TOKEN:+bearer token ENABLED}${TOKEN:-none (WARNING: open camera!)}

Quick test:
  curl http://$IP_ADDR:$PORT/health
  curl http://$IP_ADDR:$PORT/snapshot -o frame.jpg

Manage:
  sudo systemctl status $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f
  ./camera_stream/update.sh        # re-sync + restart after editing code
  ./camera_stream/uninstall.sh     # remove service
EOF
