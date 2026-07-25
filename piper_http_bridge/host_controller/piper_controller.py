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

# Joint limits (deg) -- mirrors server_common.validate_joints. The jog loop
# clamps the per-joint target to these so one axis reaching its limit never
# makes joint_ctrl reject the whole frame (which silently froze all jogging).
JOINT_LIMITS = [(-150.0, 150.0), (0.0, 180.0), (-170.0, 0.0),
                (-100.0, 100.0), (-70.0, 70.0), (-120.0, 120.0)]


def _clamp_joint(j, v):
    lo, hi = JOINT_LIMITS[j]
    return max(lo, min(hi, v))


class Controller(object):
    """Motion-vector jog controller.

    The browser owns a 6-vector V: holding a joint's "+" sets that component
    to +step, "-" sets it to -step, releasing zeroes it. The panel streams V
    back at 100 Hz and every 7th packet is a keepalive. The server stores V
    and a control loop polls it at TICK: any non-zero component makes the loop
    integrate the target along V (speed proportional to that axis's step) and
    command the arm; all-zero -> the arm holds its current pose. If keepalives
    stop (browser closed / link dead) V goes stale after STALE_S and the loop
    holds position. go_zero/stop park the loop so they aren't fought;
    enable/disable are independent CAN power signals.
    """

    TICK = 0.05            # control-loop poll period (s) -> 20 Hz
    MAX_DPS = 45.0         # deg/s slew cap per joint (safety)
    SPEED_MULT = 4.0       # |vector component| (deg) -> jog speed: |v|*4 deg/s
    STALE_S = 0.4          # no keepalive/vector for this long -> released
    POS_TOL = 0.05         # deg; below this error we stop commanding (deadband)
    RESYNC_DEG = 30.0      # target this far from feedback -> re-sync & wait

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
        self._lock = threading.Lock()
        self._vec = [0.0] * 6        # stored motion vector (signed step per axis)
        self._ka = 0.0               # last keepalive/vector timestamp
        self._target = None          # commanded joints the loop is steering to
        self._suspend = False        # True -> loop idle (after go_zero/stop)
        self._stop = threading.Event()
        # diagnostics counters
        self._c_move = 0             # motion-vector packets received
        self._c_ka = 0               # keepalive packets received
        self._c_hold = 0             # hold packets received
        self._c_badgroup = 0         # frame-groups that went stale (no keepalive)
        self._c_resync = 0           # target re-syncs to feedback
        self._c_skip = 0             # ticks skipped on a bad read/send
        self._stale_now = False      # currently in a stale (link-dead) stretch
        self._wd = threading.Thread(target=self._loop, daemon=True)
        self._wd.start()

    # ---- state ---------------------------------------------------------
    def state(self):
        try:
            st = self.client.state()
            if isinstance(st, dict):
                st["server"] = self.diag()
            return {"ok": True, "state": st}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def diag(self):
        """Internal controller status for the panel's server-state readout."""
        with self._lock:
            tgt = list(self._target) if self._target else None
            return {
                "suspended": self._suspend,
                "stale": self._stale_now,
                "vec": [round(v, 2) for v in self._vec],
                "target": ([round(v, 2) for v in tgt] if tgt is not None else None),
                "latch": ([round(v, 2) for v in tgt] if tgt is not None else None),
                "ka_age": round(max(0.0, time.time() - self._ka), 2),
                "c_move": self._c_move,
                "c_keepalive": self._c_ka,
                "c_hold": self._c_hold,
                "c_badgroup": self._c_badgroup,
                "c_resync": self._c_resync,
                "c_skip": self._c_skip,
            }

    def _read_joints(self):
        st = self.client.state()
        j = st.get("joints_deg") if isinstance(st, dict) else None
        if not j:
            raise pc.PiperError("no joint feedback yet")
        return list(j)

    # ---- motion-vector intake ------------------------------------------
    def move(self, index=None, direction=None, step_deg=None, boost=1,
             dirs=None, steps=None, vec=None):
        """Store the browser's motion vector (signed step per axis). Accepts a
        raw `vec[6]`, or folds the dirs[6]/steps[6] (and legacy single-axis)
        forms into a vector. Every move packet refreshes the keepalive clock."""
        if vec is None:
            vec = [0.0] * 6
            if dirs is not None:
                d = list(dirs)[:6]
                s = list(steps or [0] * 6)[:6]
                for j in range(min(6, len(d))):
                    dv = float(d[j])
                    sv = abs(float(s[j])) if j < len(s) else 0.0
                    vec[j] = (1.0 if dv > 0 else (-1.0 if dv < 0 else 0.0)) * sv
            elif index is not None:
                vec[int(index)] = (1.0 if float(direction) >= 0 else -1.0) \
                    * abs(float(step_deg))
        v = [float(x) for x in list(vec)[:6]]
        v += [0.0] * (6 - len(v))
        with self._lock:
            self._suspend = False      # fresh input re-engages the loop
            self._vec = v
            self._ka = time.time()
            self._c_move += 1
        return {"ok": True}

    def set_vec(self, vec):
        """Compact signal path: store the 6-vector and refresh the keepalive
        clock. Any packet on the signal stream counts as a keepalive."""
        v = [float(x) for x in list(vec)[:6]]
        v += [0.0] * (6 - len(v))
        with self._lock:
            self._suspend = False
            self._vec = v
            self._ka = time.time()
            self._c_move += 1
        return {"ok": True}

    def keepalive(self):
        with self._lock:
            self._ka = time.time()
            self._c_ka += 1
        return {"ok": True}

    def hold(self):
        with self._lock:
            self._vec = [0.0] * 6
            self._ka = time.time()
            self._c_hold += 1
        return {"ok": True}

    # ---- control loop: poll the vector, steer along it -----------------
    def _loop(self):
        while not self._stop.is_set():
            time.sleep(self.TICK)
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self):
        now = time.time()
        with self._lock:
            vec = list(self._vec)
            ka = self._ka
            suspended = self._suspend
        if now - ka > self.STALE_S:
            vec = [0.0] * 6                            # stale -> released
            with self._lock:
                if not self._stale_now:                # a frame-group went dead
                    self._c_badgroup += 1
                self._stale_now = True
        else:
            with self._lock:
                self._stale_now = False

        # A bad read (CAN/network blip) must not wedge the loop: skip this tick
        # and KEEP the current intent, so motion resumes on the next clean tick.
        try:
            joints = self._read_joints()
        except Exception:
            with self._lock:
                self._c_skip += 1
            return
        with self._lock:
            if self._target is None:
                self._target = list(joints)
            target = list(self._target)

        if any(v != 0.0 for v in vec):
            # non-zero components -> integrate the target along the vector,
            # clamped to each joint's limit so a maxed-out axis can't wedge the rest
            for j in range(6):
                v = vec[j]
                if v == 0.0:
                    continue
                dps = max(1.0, min(self.MAX_DPS, abs(v) * self.SPEED_MULT))
                target[j] = _clamp_joint(j, target[j] +
                                         (1.0 if v > 0 else -1.0) * dps * self.TICK)
        elif suspended:
            # go_zero / stop owns the arm right now -> stay out of the way
            with self._lock:
                self._target = list(joints)
            return
        # else: released -> target stays put; arm converges & stops

        err = max(abs(target[i] - joints[i]) for i in range(6))
        with self._lock:
            self._target = target
        if err > self.RESYNC_DEG:
            # target ran away from reality (bad/stale signaling, restart) ->
            # re-sync to where the arm actually is and wait for a clean vector
            with self._lock:
                self._target = list(joints)
                self._c_resync += 1
            return
        if err <= self.POS_TOL:
            return                                         # settled; don't spam
        try:
            self.client.joint_ctrl(target, speed=self.speed)
        except Exception:
            # send failed this tick -> wait for the next clean tick; don't lock up
            with self._lock:
                self._c_skip += 1
            return

    # ---- joint jog -----------------------------------------------------
    def joint_jog(self, index, delta_deg):
        index = int(index)
        if not 0 <= index <= 5:
            raise pc.PiperError("joint index must be 0..5")
        delta = float(delta_deg)
        joints = self._read_joints()
        joints[index] = joints[index] + delta
        self.client.joint_ctrl(joints, speed=self.speed)
        with self._lock:
            self._suspend = False
            self._target = list(joints)
        return {"ok": True, "joints_deg": joints}

    # ---- gripper / mode ------------------------------------------------
    def gripper(self, position_mm):
        return self.client.gripper(float(position_mm))

    def _park_loop(self):
        """Hand the arm to an absolute command (go_zero/stop): stop the control
        loop from steering back to a stale jog target."""
        with self._lock:
            self._suspend = True
            self._vec = [0.0] * 6

    def enable(self):
        return self.client.enable()

    def disable(self):
        # enable/disable are independent CAN power signals (motor torque on/off),
        # not pose jumps -- the control loop can stay engaged; it just has no effect
        # while the drivers are unpowered.
        return self.client.disable()

    def stop(self):
        self._park_loop()
        return self.client.stop()

    def go_zero(self):
        self._park_loop()
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
            if self.path in ("/ws/state", "/ws/teleop", "/ws/gripper"):
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
            print("[ws] %s connected" % self.path)
            # /ws/state only ever pushes state; the two command channels read.
            if self.path == "/ws/state":
                hub.add(client)
            while client.alive:
                payload = _ws_read_text_frame(self.rfile)
                if payload is None:
                    client.alive = False
                    break
                try:
                    p = json.loads(payload)
                    # Compact signal packets (arrays) take the fast path with no
                    # reply; dicts go through dispatch and may get an _id reply.
                    if isinstance(p, list):
                        if p and p[0] == "v":
                            ctrl.set_vec(p[1:])
                        elif p and p[0] == "k":
                            ctrl.keepalive()
                        elif p and p[0] == "h":
                            ctrl.hold()
                        continue
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
            print("[ws] %s closed" % self.path)
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
                return ctrl.move(p.get("index"), p.get("dir"), p.get("step"),
                                 p.get("boost", 1),
                                 dirs=p.get("dirs"), steps=p.get("steps"),
                                 vec=p.get("vec"))
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
