#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
Backend-agnostic HTTP / TCP front-end for the Piper arm.

A "backend" is any object exposing these methods, each returning a result dict:
    get_state()                                  -> dict
    cmd_enable(enable: bool)                     -> dict
    cmd_joint_ctrl(joints_deg, speed, gripper_mm)-> dict
    cmd_pose_ctrl(x,y,z,roll,pitch,yaw,gripper_mm)-> dict
    cmd_gripper(position_mm, effort)             -> dict
    cmd_stop() / cmd_reset() / cmd_go_zero(is_mit_mode) / cmd_block_arm(block)

The dispatcher + servers are shared by both the ROS bridge and the
direct-SDK node so the client-facing JSON contract stays identical.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingTCPServer, StreamRequestHandler

# Joint limits (degrees) -- mirrors the official piper_sdk JointCtrl table.
JOINT_LIMITS_DEG = [
    (-150.0, 150.0),   # joint 1
    (0.0,    180.0),   # joint 2
    (-170.0, 0.0),     # joint 3
    (-100.0, 100.0),   # joint 4
    (-70.0,  70.0),    # joint 5
    (-120.0, 120.0),   # joint 6
]

import math
DEG2RAD = math.pi / 180.0
RAD2DEG = 180.0 / math.pi
MM2M = 0.001
M2MM = 1000.0


def validate_joints(joints):
    if not isinstance(joints, (list, tuple)) or len(joints) != 6:
        raise ValueError("joints must be a list of 6 numbers (degrees)")
    out = []
    for i, v in enumerate(joints):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError("joint %d is not a number: %r" % (i + 1, v))
        lo, hi = JOINT_LIMITS_DEG[i]
        if fv < lo or fv > hi:
            raise ValueError(
                "joint %d = %.3f deg out of range [%.1f, %.1f]" % (i + 1, fv, lo, hi))
        out.append(fv)
    return out


def clamp_speed(speed):
    try:
        s = int(speed)
    except (TypeError, ValueError):
        return 50
    return max(1, min(100, s))


def dispatch(backend, payload):
    """payload: dict with an 'action' key. Returns a result dict."""
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    action = payload.get("action")
    if not action:
        raise ValueError("missing 'action' field")

    if action == "state":
        return {"ok": True, "state": backend.get_state()}
    if action == "enable":
        return backend.cmd_enable(payload.get("enable", True))
    if action == "disable":
        return backend.cmd_enable(False)
    if action == "joint_ctrl":
        return backend.cmd_joint_ctrl(
            payload.get("joints"),
            payload.get("speed", 50),
            payload.get("gripper_mm"))
    if action == "pose_ctrl":
        return backend.cmd_pose_ctrl(
            payload.get("x", 0.0), payload.get("y", 0.0), payload.get("z", 0.0),
            payload.get("roll", 0.0), payload.get("pitch", 0.0),
            payload.get("yaw", 0.0), payload.get("gripper_mm"))
    if action == "gripper":
        return backend.cmd_gripper(payload.get("position_mm", 0.0),
                                   payload.get("effort", 1000))
    if action == "stop":
        return backend.cmd_stop()
    if action == "reset":
        return backend.cmd_reset()
    if action == "go_zero":
        return backend.cmd_go_zero(payload.get("is_mit_mode", False))
    if action == "block_arm":
        return backend.cmd_block_arm(payload.get("block", True))
    raise ValueError("unknown action: %r" % action)


def make_http_handler(backend, token, log=print):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PiperHttpBridge/1.0"

        def _authorized(self):
            if not token:
                return True
            return self.headers.get("Authorization", "") == ("Bearer %s" % token)

        def _send_json(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass  # keep quiet

        def do_GET(self):
            if not self._authorized():
                return self._send_json(401, {"ok": False, "error": "unauthorized"})
            if self.path in ("/state", "/api/state"):
                return self._send_json(200, {"ok": True, "state": backend.get_state()})
            if self.path in ("/health", "/api/health"):
                return self._send_json(200, {"ok": True, "alive": True})
            return self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._send_json(401, {"ok": False, "error": "unauthorized"})
            if self.path not in ("/cmd", "/api/cmd", "/control"):
                return self._send_json(404, {"ok": False, "error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
                return self._send_json(200, dispatch(backend, payload))
            except ValueError as e:
                return self._send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})

    return Handler


def make_tcp_handler(backend, token, log=print):
    class TcpHandler(StreamRequestHandler):
        def handle(self):
            for line in self.rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line.decode("utf-8"))
                    if token and payload.get("token") != token:
                        self._reply({"ok": False, "error": "unauthorized"})
                        continue
                    self._reply(dispatch(backend, payload))
                except ValueError as e:
                    self._reply({"ok": False, "error": str(e)})
                except Exception as e:
                    self._reply({"ok": False, "error": str(e)})

        def _reply(self, obj):
            self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))

    return TcpHandler


def serve(backend, host="0.0.0.0", http_port=8080, tcp_port=9090,
          token="", enable_tcp=True, log=print):
    """Start HTTP (+optional TCP) servers. Returns a shutdown() callable."""
    httpd = ThreadingHTTPServer((host, http_port), make_http_handler(backend, token, log))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("HTTP control on http://%s:%d  (GET /state, POST /cmd)" % (host, http_port))

    tcpd = None
    if enable_tcp:
        tcpd = ThreadingTCPServer((host, tcp_port), make_tcp_handler(backend, token, log))
        tcpd.daemon_threads = True
        threading.Thread(target=tcpd.serve_forever, daemon=True).start()
        log("TCP control on tcp://%s:%d  (newline-delimited JSON)" % (host, tcp_port))

    if not token:
        log("WARNING: no auth token -- anyone on the network can control the arm!")

    def shutdown():
        httpd.shutdown()
        httpd.server_close()
        if tcpd:
            tcpd.shutdown()
            tcpd.server_close()

    return shutdown
