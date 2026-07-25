#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
piper_controller -- upper-machine (host) controller with a zero-dependency
web control panel for the Piper 6-axis arm.

Jogs each of the six joints directly (joint space) via the arm's HTTP
endpoint (piper_sdk_server / piper_http_bridge). No Cartesian jog.

Run:
    python piper_controller.py --endpoint http://192.168.1.100:8080 --token SECRET
then open:
    http://<this-host>:8000/

Keyboard (focus on the page):
    1 / 2   : joint 1  - / +
    3 / 4   : joint 2  - / +
    5 / 6   : joint 3  - / +
    7 / 8   : joint 4  - / +
    9 / 0   : joint 5  - / +
    - / =   : joint 6  - / +
    G / H   : gripper close / open
Hold a key or an on-screen button to jog continuously; release to stop.
Space / Enter are intentionally NOT bound (they clash with Tab focus);
enable / disable / estop / go-zero are on-page buttons only.
The joint step is a plain number of degrees set on the panel.
All of the above is also clickable on the panel.

State is pushed to the browser over a WebSocket (~10 Hz) for a smooth,
low-latency panel; if the socket cannot be established the panel falls
back to plain HTTP polling. Pure standard library, no external deps.

Security: the panel forwards your bearer token to the endpoint; it is never
stored. If you expose this beyond localhost, put it behind the same token.
"""

import argparse
import base64
import hashlib
import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Windows consoles default to a latin-1/cp1252 codec; force UTF-8 so any
# non-ASCII output (or a browser hitting us with a non-ASCII path) cannot
# crash the server with "latin-1 codec can't encode ...".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "client"))
import piper_client as pc  # noqa: E402


class Controller(object):
    """Velocity-driven jog controller.

    The panel sends ONE 'move' signal on key/button press and ONE 'hold'
    signal on release -- not a 40 Hz stream of position increments. While a
    move is active a watchdog here keeps handing the arm fresh position
    targets (base += vel*dt each tick), so motion is smooth and the arm is
    always chasing a near-future point. On 'hold' -- or if keepalives stop
    arriving (browser closed / link dead) -- the watchdog immediately
    re-issues the CURRENT target (decelerate in place) and re-syncs the base
    to real feedback, so the arm stops right away instead of draining a
    backlog of queued targets (the overshoot).
    """

    TICK = 0.04            # watchdog period (s) -> 25 Hz target refresh
    KA_TIMEOUT = 0.45      # no keepalive for this long -> auto-hold
    VEL_CAP = 45.0         # deg/s safety cap on jog velocity

    def __init__(self, endpoint, token="", speed=30, capture_dir=""):
        self.ep = endpoint.rstrip("/")
        # parse host/port for piper_client
        hostport = self.ep.split("://", 1)[-1].split("/")[0]
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
            port = int(port)
        else:
            host, port = hostport, 8080
        self.client = pc.PiperClient(host, http_port=port, token=token)
        self.speed = speed
        self.capture_dir = capture_dir
        self._capture_count = 0
        self._last_joints = None     # commanded joints (jog base; synced to feedback)
        self._lock = threading.Lock()
        self._vel = [0.0] * 6        # deg/s per joint while a move is active
        self._ka = 0.0               # last keepalive (or move) timestamp
        self._stop = threading.Event()
        self._wd = threading.Thread(target=self._watchdog, daemon=True)
        self._wd.start()

    # ---- state ---------------------------------------------------------
    def state(self):
        try:
            st = self.client.state()
            # Correct the jog base from real feedback so drift doesn't accumulate.
            if isinstance(st, dict) and st.get("joints_deg"):
                with self._lock:
                    self._last_joints = list(st["joints_deg"])
            return {"ok": True, "state": st}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- move / hold signaling ----------------------------------------
    def move(self, index, direction, step_deg, boost=1):
        """Press signal: start jogging `index` in `direction` at step*boost deg/s."""
        index = int(index)
        if not 0 <= index <= 5:
            raise pc.PiperError("joint index must be 0..5")
        vel = max(-self.VEL_CAP, min(self.VEL_CAP,
                  float(direction) * abs(float(step_deg)) * float(boost or 1)))
        with self._lock:
            self._vel = [0.0] * 6
            self._vel[index] = vel
            self._ka = time.time()       # a move also counts as a keepalive
        return {"ok": True, "index": index, "vel": vel}

    def keepalive(self):
        with self._lock:
            self._ka = time.time()
        return {"ok": True}

    def hold(self):
        """Release signal: stop jogging now and re-sync to real feedback."""
        with self._lock:
            self._vel = [0.0] * 6
            self._ka = time.time()
        self._sync_base()
        return {"ok": True}

    def _sync_base(self):
        try:
            st = self.client.state()
            if isinstance(st, dict) and st.get("joints_deg"):
                with self._lock:
                    self._last_joints = list(st["joints_deg"])
        except Exception:
            pass

    # ---- watchdog: stream targets while moving, stop on keepalive loss --
    def _watchdog(self):
        while not self._stop.is_set():
            time.sleep(self.TICK)
            with self._lock:
                vel = list(self._vel)
                base = list(self._last_joints) if self._last_joints else None
                ka = self._ka
            moving = any(v != 0.0 for v in vel)
            if not moving:
                continue
            # keepalive lost (browser closed / link dead) -> stop the arm
            if time.time() - ka > self.KA_TIMEOUT:
                self.hold()
                continue
            if base is None:
                self._sync_base()
                continue
            target = [base[i] + vel[i] * self.TICK for i in range(6)]
            try:
                self.client.joint_ctrl(target, speed=self.speed)
                with self._lock:
                    self._last_joints = target
            except Exception:
                pass

    # ---- joint jog -----------------------------------------------------
    def joint_jog(self, index, delta_deg):
        index = int(index)
        if not 0 <= index <= 5:
            raise pc.PiperError("joint index must be 0..5")
        delta = float(delta_deg)
        with self._lock:
            base = self._last_joints
            if base is None:
                st = self.client.state()
                base = st.get("joints_deg")
                if not base:
                    raise pc.PiperError("no joint feedback yet")
            joints = list(base)
            joints[index] = joints[index] + delta
            res = self.client.joint_ctrl(joints, speed=self.speed)
            self._last_joints = joints
        return {"ok": True, "joints_deg": joints}

    # ---- gripper / mode ------------------------------------------------
    def gripper(self, position_mm):
        return self.client.gripper(float(position_mm))

    def enable(self):
        return self.client.enable()

    def disable(self):
        return self.client.disable()

    def stop(self):
        return self.client.stop()

    def go_zero(self):
        return self.client.go_zero()

    def save_capture(self, data_url, prefix="mahjong"):
        if not self.capture_dir:
            raise pc.PiperError("capture directory is not configured")
        if not isinstance(data_url, str) or "," not in data_url:
            raise pc.PiperError("capture data must be a data URL")
        meta, b64 = data_url.split(",", 1)
        if "image/jpeg" not in meta and "image/png" not in meta:
            raise pc.PiperError("capture data must be image/jpeg or image/png")
        os.makedirs(self.capture_dir, exist_ok=True)
        with self._lock:
            self._capture_count += 1
            idx = self._capture_count
        ext = ".jpg" if "image/jpeg" in meta else ".png"
        safe_prefix = "".join(ch for ch in str(prefix) if ch.isalnum() or ch in "-_")
        if not safe_prefix:
            safe_prefix = "mahjong"
        path = os.path.join(self.capture_dir, "%s_%06d%s" %
                            (safe_prefix, idx, ext))
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return {"ok": True, "path": path, "index": idx}


# ---------------------------------------------------------------------------
# Minimal RFC6455 WebSocket layer (stdlib only) for smooth state streaming.
# ---------------------------------------------------------------------------
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key):
    h = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(h).decode("ascii")


def _ws_frame_text(text):
    """Encode one unmasked, FIN-set text frame."""
    payload = text.encode("utf-8")
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 0x10000:
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    return bytes(header) + payload


class WSClient(object):
    """One connected WebSocket peer that only ever receives state pushes."""

    def __init__(self, rfile, wfile):
        self.rfile = rfile
        self.wfile = wfile
        self.lock = threading.Lock()
        self.alive = True

    def send(self, text):
        if not self.alive:
            return
        try:
            with self.lock:
                self.wfile.write(_ws_frame_text(text))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            self.alive = False


class WSHub(object):
    """Tracks connected clients, polls the arm, broadcasts state ~10 Hz."""

    def __init__(self, ctrl, hz=10.0):
        self.ctrl = ctrl
        self.interval = 1.0 / max(1.0, float(hz))
        self.clients = []
        self._lock = threading.Lock()

    def add(self, client):
        with self._lock:
            self.clients.append(client)
        self._ensure_running()

    def _remove_dead(self):
        with self._lock:
            self.clients = [c for c in self.clients if c.alive]

    def _ensure_running(self):
        with self._lock:
            if getattr(self, "_thread", None) and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            with self._lock:
                if not self.clients:
                    return  # no viewers; a later add() restarts the loop
            try:
                msg = json.dumps(self.ctrl.state())
            except Exception as e:
                msg = json.dumps({"ok": False, "error": str(e)})
            with self._lock:
                for c in self.clients:
                    c.send(msg)
            self._remove_dead()
            time.sleep(self.interval)


def _ws_read_text_frame(rfile):
    """Read one WebSocket frame and return its decoded text payload.

    Control (ping/pong/close) frames are handled inline and skipped. Returns
    None when the connection is dead or the peer sent a close frame.
    """
    while True:
        try:
            hdr = rfile.read(2)
            if len(hdr) < 2:
                return None
            fin = hdr[0] & 0x80
            opcode = hdr[0] & 0x0F
            length = hdr[1] & 0x7F
            if length == 126:
                ext = rfile.read(2)
                if len(ext) < 2:
                    return None
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = rfile.read(8)
                if len(ext) < 8:
                    return None
                length = struct.unpack("!Q", ext)[0]
            mask = hdr[1] & 0x80
            key = b""
            if mask:                            # client frames are masked
                key = rfile.read(4)
                if len(key) < 4:
                    return None
            payload = b""
            if length:
                payload = rfile.read(length)
                if len(payload) < length:
                    return None
            if mask:
                payload = bytes(payload[i] ^ key[i % 4] for i in range(length))
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            return None

        if opcode == 0x8:                       # close
            return None
        if opcode in (0x9, 0xA):                # ping / pong -> ignore
            continue
        if opcode in (0x1, 0x2, 0x0):           # text / binary / continuation
            if not fin:
                # Fragmented messages aren't used by the panel; treat as closed.
                return None
            try:
                return payload.decode("utf-8")
            except Exception:
                return None
        # unknown opcode -> ignore and keep reading


# ---------------------------------------------------------------------------
# HTTP server: serves the panel HTML + a /api/* JSON proxy to the Controller.
# ---------------------------------------------------------------------------
def make_handler(ctrl, panel_html, hub=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PiperController/1.0"

        def _json(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self._send(code, "application/json", body)

        def _html(self, code, text):
            self._send(code, "text/html; charset=utf-8", text.encode("utf-8"))

        def _send(self, code, ctype, body):
            # Guard against latin-1 header-buffer crashes when a client sends
            # a non-ASCII request line (e.g. a path or header containing CJK).
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (UnicodeEncodeError, BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

        def send_error(self, code, message=None, explain=None):
            # The default BaseHTTPRequestHandler error page interpolates the
            # request path into headers/body via latin-1, which crashes on
            # non-ASCII paths. Serve a minimal ASCII-only error instead.
            reason = self.responses.get(code, ("Error",))[0]
            body = ("%d %s" % (code, reason)).encode("ascii", "replace")
            self._send(code, "text/plain; charset=us-ascii", body)

        def do_GET(self):
            if self.path == "/ws":
                return self._ws_handshake()
            if self.path in ("/", "/index.html"):
                return self._html(200, panel_html)
            if self.path == "/api/state":
                return self._json(200, ctrl.state())
            return self._json(404, {"ok": False, "error": "not found"})

        def _ws_handshake(self):
            if hub is None:
                return self._send(404, "text/plain", b"websocket disabled")
            key = self.headers.get("Sec-WebSocket-Key")
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if not key or upgrade != "websocket":
                return self._send(400, "text/plain", b"bad websocket request")
            try:
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", _ws_accept(key))
                self.end_headers()
                self.wfile.flush()
            except (UnicodeEncodeError, BrokenPipeError, ConnectionResetError):
                return
            client = WSClient(self.rfile, self.wfile)
            hub.add(client)
            # Read this client's incoming frames: control commands run over the
            # same socket (replies carry the request's "_id"); state keeps being
            # pushed by the hub. Park here until the client disconnects so the
            # HTTP server does not tear down the (now upgraded) socket.
            while client.alive:
                payload = _ws_read_text_frame(self.rfile)
                if payload is None:
                    client.alive = False
                    break
                try:
                    p = json.loads(payload)
                    out = self._dispatch(p)
                    if isinstance(p, dict) and "_id" in p:
                        out = dict(out) if isinstance(out, dict) \
                            else {"ok": True, "r": out}
                        out["_id"] = p["_id"]
                        client.send(json.dumps(out))
                except Exception as e:
                    try:
                        err = {"ok": False, "error": str(e)}
                        if isinstance(p, dict) and "_id" in p:
                            err["_id"] = p["_id"]
                        client.send(json.dumps(err))
                    except Exception:
                        client.alive = False
            self.close_connection = True

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                p = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return self._json(400, {"ok": False, "error": "bad json"})
            try:
                out = self._dispatch(p)
                return self._json(200, out)
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        def _dispatch(self, p):
            a = p.get("action")
            if a == "joint_jog":
                return ctrl.joint_jog(p.get("index", 0), p.get("delta", 0))
            if a == "move":
                return ctrl.move(p.get("index", 0), p.get("dir", 0),
                                 p.get("step", 3), p.get("boost", 1))
            if a == "hold":
                return ctrl.hold()
            if a == "keepalive":
                return ctrl.keepalive()
            if a == "gripper":
                return {"ok": True, "r": ctrl.gripper(p.get("position_mm", 0))}
            if a == "enable":
                return {"ok": True, "r": ctrl.enable()}
            if a == "disable":
                return {"ok": True, "r": ctrl.disable()}
            if a == "stop":
                return {"ok": True, "r": ctrl.stop()}
            if a == "go_zero":
                return {"ok": True, "r": ctrl.go_zero()}
            if a == "capture_frame":
                return ctrl.save_capture(p.get("data_url", ""),
                                         p.get("prefix", "mahjong"))
            return {"ok": False, "error": "unknown action %r" % a}

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Piper web controller panel")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8080",
                    help="arm control endpoint base URL")
    ap.add_argument("--token", default="", help="endpoint bearer token")
    ap.add_argument("--port", type=int, default=8000, help="panel listen port")
    ap.add_argument("--host", default="0.0.0.0", help="panel listen host")
    ap.add_argument("--speed", type=int, default=30, help="joint jog speed %%")
    ap.add_argument("--camera", default="",
                    help="MJPEG camera stream URL shown in the panel "
                         "(e.g. http://<orangepi>:8090/stream.mjpeg)")
    ap.add_argument("--camera-webrtc", default="",
                    help="MediaMTX WebRTC path URL for sub-second live view "
                         "(e.g. http://<orangepi>:8889/cam). When set, the "
                         "panel streams video over WebRTC instead of MJPEG.")
    ap.add_argument("--ws-rate", type=float, default=10.0,
                    help="WebSocket state push rate in Hz (default 10)")
    ap.add_argument("--capture-dir",
                    default="D:/Documents/Projects/AGILE/datasets/mahjong_raw/images",
                    help="directory where browser camera captures are saved")
    args = ap.parse_args()

    panel_path = os.path.join(HERE, "panel.html")
    with open(panel_path, "r", encoding="utf-8") as f:
        panel_html = f.read()
    # inject the camera URLs (both empty hides the camera card)
    panel_html = panel_html.replace("__CAMERA_URL__", args.camera)
    panel_html = panel_html.replace("__CAMERA_WEBRTC_URL__", args.camera_webrtc)

    ctrl = Controller(args.endpoint, token=args.token, speed=args.speed,
                      capture_dir=args.capture_dir)
    hub = WSHub(ctrl, hz=args.ws_rate)
    httpd = ThreadingHTTPServer((args.host, args.port),
                                make_handler(ctrl, panel_html, hub))
    httpd.daemon_threads = True
    print("Piper controller panel")
    print("  endpoint : %s" % args.endpoint)
    print("  panel    : http://%s:%d/  (state pushed over WebSocket @ %.0f Hz)"
          % (args.host, args.port, args.ws_rate))
    if args.camera or args.camera_webrtc:
        print("  camera   : %s" % (args.camera_webrtc
                                   and ("%s (WebRTC)" % args.camera_webrtc)
                                   or args.camera))
    print("  captures : %s" % args.capture_dir)
    print("  open that URL in a browser, then use WASD/QE/XZ/G/H to drive the arm.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
