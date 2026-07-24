#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
piper_controller -- upper-machine (host) controller with a zero-dependency
web control panel for the Piper 6-axis arm.

Treats the gripper as an oriented point (tool tip) and jogs it in Cartesian
space via the arm's HTTP endpoint (piper_sdk_server / piper_http_bridge).

Run:
    python piper_controller.py --endpoint http://192.168.1.100:8080 --token SECRET
then open:
    http://<this-host>:8000/

Keyboard (focus on the page):
    W / S   : +X / -X        (forward / back)
    A / D   : +Y / -Y        (left / right)
    Q / E   : +Z / -Z        (up / down)
    X / Z   : roll + / roll -
    C / V   : pitch + / pitch -
    R / F   : yaw + / yaw -
    G / H   : gripper close / open
Hold a key or an on-screen button to jog continuously; release to stop.
Space / Enter are intentionally NOT bound (they clash with Tab focus);
enable / disable / estop / go-zero are on-page buttons only.
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

# Cartesian workspace clamp (metres) -- defensive, keep the tool tip sane.
# Loosen via CLI if your setup needs a bigger envelope.
DEFAULT_LIMITS = {"x": (0.05, 0.60), "y": (-0.45, 0.45), "z": (0.0, 0.60)}

# Cartesian jog step (metres / degrees) per key press.
STEP_TRANS = {"S": 0.005, "M": 0.02, "L": 0.05}     # small/medium/large
STEP_ROT = {"S": 2.0, "M": 5.0, "L": 15.0}          # degrees
STEP_JOINT = {"S": 1.0, "M": 3.0, "L": 10.0}        # degrees
STEP_GRIP = {"S": 2.0, "M": 10.0, "L": 40.0}        # mm


class Controller(object):
    """Holds endpoint client + cached state; applies jogs safely."""

    def __init__(self, endpoint, token="", speed=30, limits=None):
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
        self.limits = limits or DEFAULT_LIMITS
        self._lock = threading.Lock()

    # ---- state ---------------------------------------------------------
    def state(self):
        try:
            return {"ok": True, "state": self.client.state()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- helpers -------------------------------------------------------
    def _cur_pose(self):
        st = self.client.state()
        ep = st.get("end_pose")
        if not ep:
            raise pc.PiperError("no end_pose feedback yet (arm enabled & CAN up?)")
        # state gives mm / deg -> convert to m / deg for the endpoint's pose_ctrl
        return (ep["x"] / 1000.0, ep["y"] / 1000.0, ep["z"] / 1000.0,
                ep["roll"], ep["pitch"], ep["yaw"])

    def _clamp(self, axis, v):
        lo, hi = self.limits[axis]
        return max(lo, min(hi, v))

    # ---- Cartesian jog -------------------------------------------------
    def jog(self, dx=0.0, dy=0.0, dz=0.0, droll=0.0, dpitch=0.0, dyaw=0.0):
        with self._lock:
            x, y, z, r, p, yw = self._cur_pose()
            x = self._clamp("x", x + dx)
            y = self._clamp("y", y + dy)
            z = self._clamp("z", z + dz)
            r += droll
            p += dpitch
            yw += dyaw
            res = self.client.pose_ctrl(x * 1000.0, y * 1000.0, z * 1000.0,
                                        r, p, yw)
        return {"ok": True, "pose": {"x": round(x, 4), "y": round(y, 4),
                                     "z": round(z, 4), "roll": round(r, 2),
                                     "pitch": round(p, 2), "yaw": round(yw, 2)}}

    # ---- joint jog -----------------------------------------------------
    def joint_jog(self, index, delta_deg):
        index = int(index)
        if not 0 <= index <= 5:
            raise pc.PiperError("joint index must be 0..5")
        with self._lock:
            st = self.client.state()
            joints = st.get("joints_deg")
            if not joints:
                raise pc.PiperError("no joint feedback yet")
            joints = list(joints)
            joints[index] = joints[index] + float(delta_deg)
            res = self.client.joint_ctrl(joints, speed=self.speed)
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


def _ws_consume_one_frame(rfile):
    """Block until one full WebSocket frame arrives (to catch the client
    closing / erroring). Returns False when the connection is dead."""
    try:
        hdr = rfile.read(2)
        if len(hdr) < 2:
            return False
        opcode = hdr[0] & 0x0F
        length = hdr[1] & 0x7F
        if length == 126:
            ext = rfile.read(2)
            if len(ext) < 2:
                return False
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = rfile.read(8)
            if len(ext) < 8:
                return False
            length = struct.unpack("!Q", ext)[0]
        if hdr[1] & 0x80:                       # masked -> consume mask key
            if len(rfile.read(4)) < 4:
                return False
        if length and len(rfile.read(length)) < length:
            return False
        return opcode != 0x8                    # 0x8 == close frame
    except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
        return False


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
            # Park this thread until the client disconnects so the HTTP server
            # does not tear down the (now upgraded) socket.
            while client.alive:
                if not _ws_consume_one_frame(self.rfile):
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
            if a == "jog":
                return ctrl.jog(p.get("dx", 0), p.get("dy", 0), p.get("dz", 0),
                                p.get("droll", 0), p.get("dpitch", 0), p.get("dyaw", 0))
            if a == "joint_jog":
                return ctrl.joint_jog(p.get("index", 0), p.get("delta", 0))
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
    ap.add_argument("--ws-rate", type=float, default=10.0,
                    help="WebSocket state push rate in Hz (default 10)")
    args = ap.parse_args()

    panel_path = os.path.join(HERE, "panel.html")
    with open(panel_path, "r", encoding="utf-8") as f:
        panel_html = f.read()
    # inject the camera stream URL (empty string hides the camera card)
    panel_html = panel_html.replace("__CAMERA_URL__", args.camera)

    ctrl = Controller(args.endpoint, token=args.token, speed=args.speed)
    hub = WSHub(ctrl, hz=args.ws_rate)
    httpd = ThreadingHTTPServer((args.host, args.port),
                                make_handler(ctrl, panel_html, hub))
    httpd.daemon_threads = True
    print("Piper controller panel")
    print("  endpoint : %s" % args.endpoint)
    print("  panel    : http://%s:%d/  (state pushed over WebSocket @ %.0f Hz)"
          % (args.host, args.port, args.ws_rate))
    if args.camera:
        print("  camera   : %s" % args.camera)
    print("  open that URL in a browser, then use WASD/QE/XZ/G/H to drive the arm.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
