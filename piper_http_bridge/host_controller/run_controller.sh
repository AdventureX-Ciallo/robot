#!/usr/bin/env bash
###############################################################################
# run_controller.sh -- launch the Piper web control panel on the upper machine.
#
# Usage:
#   ./run_controller.sh --endpoint http://192.168.1.100:8080 --token SECRET
#   ENDPOINT=http://192.168.1.100:8080 TOKEN=SECRET ./run_controller.sh
#
# Then open http://localhost:8000/ in a browser and drive with WASD/QE/XZ/G/H.
###############################################################################
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENDPOINT="${ENDPOINT:-http://127.0.0.1:8080}"
TOKEN="${TOKEN:-}"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
SPEED="${SPEED:-30}"

ARGS=(--endpoint "$ENDPOINT" --port "$PORT" --host "$HOST" --speed "$SPEED")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoint) ARGS=(--endpoint "$2" "${ARGS[@]:2}"); shift 2 ;;
    --token)    TOKEN="$2"; shift 2 ;;
    --port)     ARGS=(--port "$2" "${ARGS[@]:2}"); shift 2 ;;
    --host)     ARGS=(--host "$2" "${ARGS[@]:2}"); shift 2 ;;
    --speed)    ARGS=(--speed "$2" "${ARGS[@]:2}"); shift 2 ;;
    -h|--help)  sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TOKEN" ]] && ARGS+=(--token "$TOKEN")

echo "Starting Piper controller panel -> $ENDPOINT"
exec python3 "$HERE/piper_controller.py" "${ARGS[@]}"
