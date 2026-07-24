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
    1..6    : select joint; [ / ] jog that joint - / +
    Space   : stop (estop)   ;  Enter : enable
All of the above is also clickable on the panel.

Security: the panel forwards your bearer token to the endpoint; it is never
stored. If you expose this beyond localhost, put it behind the same token.
"""

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
# HTTP server: serves the panel HTML + a /api/* JSON proxy to the Controller.
# ---------------------------------------------------------------------------
def make_handler(ctrl, panel_html):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PiperController/1.0"

        def _json(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, code, text):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._html(200, panel_html)
            if self.path == "/api/state":
                return self._json(200, ctrl.state())
            return self._json(404, {"ok": False, "error": "not found"})

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
    args = ap.parse_args()

    panel_path = os.path.join(HERE, "panel.html")
    with open(panel_path, "r", encoding="utf-8") as f:
        panel_html = f.read()

    ctrl = Controller(args.endpoint, token=args.token, speed=args.speed)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(ctrl, panel_html))
    httpd.daemon_threads = True
    print("Piper controller panel")
    print("  endpoint : %s" % args.endpoint)
    print("  panel    : http://%s:%d/" % (args.host, args.port))
    print("  open that URL in a browser, then use WASD/QE/XZ/G/H to drive the arm.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
