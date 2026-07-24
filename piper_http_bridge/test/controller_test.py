#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
Dry-run test for the host web controller (piper_controller.py). No arm, no
endpoint server process -- the endpoint is faked by monkeypatching the
controller's piper_client so we can verify jog composition, Cartesian
clamping, joint jog, gripper and the panel HTTP API.

Run:  python test/controller_test.py   ->  exit 0 on success
"""

import importlib.util
import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import types
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
HC = os.path.join(HERE, "..", "host_controller")
sys.path.insert(0, HC)

PASSED, FAILED = [], []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name)


# Fake piper_client module with a controllable PiperClient.
class FakeClient:
    def __init__(self, host, http_port=8080, token="", **kw):
        self.sent = []          # (method, args)
        self._state = {
            "joints_deg": [10.0, 20.0, -30.0, 0.0, 5.0, -10.0],
            "gripper_mm": 40.0,
            "end_pose": {"x": 200.0, "y": 0.0, "z": 150.0,
                         "roll": 0.0, "pitch": 90.0, "yaw": 0.0},
            "arm_status": {"ctrl_mode": 1, "err_code": 0},
            "enabled": True,
        }

    def _rec(self, n, *a):
        self.sent.append((n, a))

    def state(self):            self._rec("state"); return self._state
    def enable(self):           self._rec("enable"); return {"ok": True}
    def disable(self):          self._rec("disable"); return {"ok": True}
    def stop(self):             self._rec("stop"); return {"ok": True}
    def go_zero(self):          self._rec("go_zero"); return {"ok": True}
    def gripper(self, mm, effort=1000): self._rec("gripper", mm); return {"ok": True}

    def pose_ctrl(self, x, y, z, roll, pitch, yaw, gripper_mm=None):
        self._rec("pose_ctrl", x, y, z, roll, pitch, yaw)
        # echo back as new state (mm/deg) so subsequent jogs accumulate
        self._state["end_pose"] = {"x": x, "y": y, "z": z,
                                   "roll": roll, "pitch": pitch, "yaw": yaw}
        return {"ok": True}

    def joint_ctrl(self, joints, speed=50, gripper_mm=None):
        self._rec("joint_ctrl", list(joints), speed)
        self._state["joints_deg"] = list(joints)
        return {"ok": True}


def load_controller():
    fake = types.ModuleType("piper_client")
    fake.PiperClient = FakeClient
    fake.PiperError = Exception
    sys.modules["piper_client"] = fake
    spec = importlib.util.spec_from_file_location(
        "piper_controller", os.path.join(HC, "piper_controller.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def last(client, name):
    for m, a in reversed(client.sent):
        if m == name:
            return a
    return None


def test_jog_and_clamp(mod):
    print("[cartesian jog + clamp]")
    c = mod.Controller("http://127.0.0.1:8080")
    cl = c.client
    # pose starts at x=200mm y=0 z=150mm roll0 pitch90 yaw0
    c.jog(dx=0.05, dz=-0.05, droll=10.0)   # +50mm x, -50mm z, +10deg roll
    args = last(cl, "pose_ctrl")
    check("jog x 200+50=250mm", abs(args[0] - 250.0) < 1e-6)
    check("jog z 150-50=100mm", abs(args[2] - 100.0) < 1e-6)
    check("jog roll 0+10=10", abs(args[3] - 10.0) < 1e-6)
    check("jog pitch unchanged 90", abs(args[4] - 90.0) < 1e-6)

    # clamp: push x far beyond the 0.60 m limit
    for _ in range(50):
        c.jog(dx=0.1)
    args = last(cl, "pose_ctrl")
    check("x clamped at 600mm", abs(args[0] - 600.0) < 1e-6)
    # clamp: push z below the 0.0 limit
    for _ in range(50):
        c.jog(dz=-0.1)
    args = last(cl, "pose_ctrl")
    check("z clamped at 0mm", abs(args[2] - 0.0) < 1e-6)


def test_joint_jog(mod):
    print("[joint jog]")
    c = mod.Controller("http://127.0.0.1:8080", speed=25)
    cl = c.client
    c.joint_jog(1, 5.0)   # J2 +5 deg -> 20+5=25
    args = last(cl, "joint_ctrl")
    check("joint2 jogged to 25", abs(args[0][1] - 25.0) < 1e-6)
    check("speed passed 25", args[1] == 25)
    try:
        c.joint_jog(6, 5.0)
        check("joint index 6 rejected", False)
    except Exception:
        check("joint index 6 rejected", True)


def test_gripper_and_modes(mod):
    print("[gripper + modes]")
    c = mod.Controller("http://127.0.0.1:8080")
    cl = c.client
    c.gripper(20)
    check("gripper 20mm", last(cl, "gripper") == (20,))
    c.enable();  check("enable", last(cl, "enable") is not None)
    c.disable(); check("disable", last(cl, "disable") is not None)
    c.stop();    check("stop", last(cl, "stop") is not None)
    c.go_zero(); check("go_zero", last(cl, "go_zero") is not None)


def _post(port, payload):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/cmd" % port,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode(), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_http_panel(mod):
    print("[panel HTTP API]")
    c = mod.Controller("http://127.0.0.1:8080")
    handler = mod.make_handler(c, "<html>panel</html>")
    httpd = mod.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # panel html
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5) as r:
            body = r.read().decode()
        check("GET / serves panel", "panel" in body)

        # state api
        with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=5) as r:
            st = json.loads(r.read().decode())
        check("GET /api/state ok", st["ok"] and st["state"]["gripper_mm"] == 40.0)

        # jog via HTTP
        code, r = _post(port, {"action": "jog", "dx": 0.02})
        check("POST jog ok", code == 200 and r["ok"])
        code, r = _post(port, {"action": "joint_jog", "index": 0, "delta": 3})
        check("POST joint_jog ok", code == 200 and r["ok"])
        code, r = _post(port, {"action": "gripper", "position_mm": 10})
        check("POST gripper ok", code == 200 and r["ok"])
        code, r = _post(port, {"action": "bogus"})
        check("unknown action -> error", r["ok"] is False)
    finally:
        httpd.shutdown(); httpd.server_close()


def _ws_read_text_frame(sock_file):
    """Read one server->client (unmasked) text frame; returns the payload str."""
    hdr = sock_file.read(2)
    assert len(hdr) == 2, "short frame header"
    fin_opcode, b1 = hdr[0], hdr[1]
    assert fin_opcode & 0x0F == 0x1, "expected a text frame"
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock_file.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock_file.read(8))[0]
    return sock_file.read(length).decode("utf-8")


def test_websocket(mod):
    print("[websocket state push]")
    c = mod.Controller("http://127.0.0.1:8080")
    hub = mod.WSHub(c, hz=50.0)      # fast for the test
    handler = mod.make_handler(c, "<html>panel</html>", hub)
    httpd = mod.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        f = s.makefile("rb")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key)
        s.sendall(req.encode("ascii"))
        # read the 101 response headers (terminated by a blank line)
        status = f.readline().decode("latin-1")
        check("ws 101 switching protocols", "101" in status)
        accept = ""
        while True:
            line = f.readline().decode("latin-1")
            if line in ("\r\n", "\n", ""):
                break
            if line.lower().startswith("sec-websocket-accept:"):
                accept = line.split(":", 1)[1].strip()
        check("ws accept header matches", accept == mod._ws_accept(key))
        # the hub should push state frames containing the fake gripper value
        payload = None
        for _ in range(50):
            payload = _ws_read_text_frame(f)
            if "gripper_mm" in payload:
                break
        check("ws pushes state with gripper", payload and "gripper_mm" in payload)
        s.close()
    finally:
        httpd.shutdown(); httpd.server_close()


def main():
    mod = load_controller()
    test_jog_and_clamp(mod)
    test_joint_jog(mod)
    test_gripper_and_modes(mod)
    test_http_panel(mod)
    test_websocket(mod)
    print("\n=================================")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    for f in FAILED:
        print("  FAILED: " + f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
