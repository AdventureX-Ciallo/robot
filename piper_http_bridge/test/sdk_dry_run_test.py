#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
Dry-run test for the DIRECT-SDK path (piper_sdk_server.py) -- no ROS, no CAN,
no real arm. A fake piper_sdk module is injected so the backend can be
constructed and every command exercised for correct unit conversion and
limit validation. HTTP + TCP front-ends are tested too.

Run:  python test/sdk_dry_run_test.py     ->  exit 0 on success
"""

import importlib.util
import json
import math
import os
import socket
import sys
import threading
import types
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

PASSED, FAILED = [], []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name)


# ---------------------------------------------------------------------------
# Fake piper_sdk that records calls and returns canned feedback.
# ---------------------------------------------------------------------------
class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakePiper:
    instances = []

    def __init__(self, can_name="can0", **kw):
        self.can_name = can_name
        self.calls = []          # list of (method, args)
        FakePiper.instances.append(self)

    def _rec(self, name, *args):
        self.calls.append((name, args))

    # connection
    def ConnectPort(self, *a, **k): self._rec("ConnectPort")
    def DisconnectPort(self, *a, **k): self._rec("DisconnectPort")
    # mode / enable
    def MotionCtrl_2(self, *a): self._rec("MotionCtrl_2", *a)
    def ModeCtrl(self, *a): self._rec("ModeCtrl", *a)
    def EnableArm(self, *a): self._rec("EnableArm", *a)
    def DisableArm(self, *a): self._rec("DisableArm", *a)
    # control
    def JointCtrl(self, *a): self._rec("JointCtrl", *a)
    def EndPoseCtrl(self, *a): self._rec("EndPoseCtrl", *a)
    def GripperCtrl(self, *a): self._rec("GripperCtrl", *a)
    def EmergencyStop(self, *a): self._rec("EmergencyStop", *a)
    def ResetPiper(self, *a): self._rec("ResetPiper", *a)
    # feedback (raw SDK units: 0.001 deg / 0.001 mm)
    def GetArmJointMsgs(self):
        js = _Obj(joint_1=10000, joint_2=20000, joint_3=-30000,
                  joint_4=0, joint_5=5000, joint_6=-10000)
        return _Obj(joint_state=js)

    def GetArmGripperMsgs(self):
        return _Obj(gripper_state=_Obj(grippers_angle=40000))  # 40 mm

    def GetArmEndPoseMsgs(self):
        ep = _Obj(X_axis=200000, Y_axis=0, Z_axis=150000,
                  RX_axis=0, RY_axis=90000, RZ_axis=0)
        return _Obj(end_pose=ep)

    def GetArmStatus(self):
        st = _Obj(ctrl_mode=1, arm_status=0, mode_feed=1, teach_status=0,
                  motion_status=0, err_code=0)
        return _Obj(arm_status=st)


def load_server():
    fake = types.ModuleType("piper_sdk")
    fake.C_PiperInterface_V2 = FakePiper
    fake.C_PiperInterface = FakePiper
    sys.modules["piper_sdk"] = fake

    spec = importlib.util.spec_from_file_location(
        "piper_sdk_server", os.path.join(SCRIPTS, "piper_sdk_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def last_call(fake, name):
    for m, args in reversed(fake.calls):
        if m == name:
            return args
    return None


def test_backend_units(mod):
    print("[SDK backend unit conversion]")
    b = mod.PiperSDKBackend(can_port="can0", default_speed=50)
    fake = b.piper

    # joint_ctrl: deg -> 0.001 deg
    b.cmd_joint_ctrl([0, 30, -30, 0, 20, 0], speed=30)
    args = last_call(fake, "JointCtrl")
    check("JointCtrl 6 args", args is not None and len(args) == 6)
    check("joint2 30deg -> 30000", args[1] == 30000)
    check("joint3 -30deg -> -30000", args[2] == -30000)
    check("joint5 20deg -> 20000", args[4] == 20000)
    check("speed applied via MotionCtrl_2", last_call(fake, "MotionCtrl_2")[2] == 30)

    # pose_ctrl: mm -> 0.001 mm, deg -> 0.001 deg
    b.cmd_pose_ctrl(200, 0, 150, 0, 90, 0)
    args = last_call(fake, "EndPoseCtrl")
    check("pose X 200mm -> 200000", args[0] == 200000)
    check("pose Z 150mm -> 150000", args[2] == 150000)
    check("pose pitch 90deg -> 90000", args[4] == 90000)

    # gripper: mm -> 0.001 mm
    b.cmd_gripper(40, effort=1000)
    args = last_call(fake, "GripperCtrl")
    check("gripper 40mm -> 40000", args[0] == 40000)
    check("gripper enable code 0x01", args[2] == 0x01)

    # enable / disable
    b.cmd_enable(True);  check("EnableArm called", last_call(fake, "EnableArm") is not None)
    b.cmd_enable(False); check("DisableArm called", last_call(fake, "DisableArm") is not None)

    # stop / reset / go_zero
    b.cmd_stop();  check("stop -> EmergencyStop(0x01)", last_call(fake, "EmergencyStop") == (0x01,))
    b.cmd_reset(); check("reset -> ResetPiper", last_call(fake, "ResetPiper") is not None)
    b.cmd_go_zero()
    check("go_zero -> JointCtrl all zero", last_call(fake, "JointCtrl") == (0, 0, 0, 0, 0, 0))

    # move-mode switching: pose needs MOVE P (0x00), joint needs MOVE J (0x01)
    b.cmd_pose_ctrl(200, 0, 150, 0, 90, 0)
    mc = last_call(fake, "MotionCtrl_2")
    check("pose_ctrl sets MOVE P", mc is not None and mc[1] == 0x00)
    b.cmd_joint_ctrl([0, 30, -30, 0, 20, 0], speed=30)
    mc = last_call(fake, "MotionCtrl_2")
    check("joint_ctrl sets MOVE J", mc is not None and mc[1] == 0x01)
    check("joint_ctrl speed 30", mc[2] == 30)
    r = b.cmd_set_mode("pose")
    check("set_mode pose", r.get("mode") == "move_p" and last_call(fake, "MotionCtrl_2")[1] == 0x00)
    r = b.cmd_set_mode("joint")
    check("set_mode joint", r.get("mode") == "move_j" and last_call(fake, "MotionCtrl_2")[1] == 0x01)

    # state feedback conversion (raw 0.001 -> deg / mm)
    st = b.get_state()
    check("state joints deg", st["joints_deg"][1] == 20.0 and st["joints_deg"][4] == 5.0)
    check("state gripper mm", st["gripper_mm"] == 40.0)
    check("state end pose mm", st["end_pose"]["x"] == 200.0 and st["end_pose"]["z"] == 150.0)
    check("state end pose deg", st["end_pose"]["pitch"] == 90.0)

    # limit rejection
    try:
        b.cmd_joint_ctrl([0, 999, 0, 0, 0, 0])
        check("out-of-range rejected", False)
    except ValueError:
        check("out-of-range rejected", True)


def _http(port, path, payload=None, token=""):
    url = "http://127.0.0.1:%d%s" % (port, path)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=("POST" if payload is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode(), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _tcp_roundtrip(port, payload):
    # Use an explicit AF_INET socket (create_connection can hang on Windows
    # when it resolves localhost to an IPv6 address the server isn't bound to).
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(("127.0.0.1", port))
    f = s.makefile("rw")
    f.write(json.dumps(payload) + "\n")
    f.flush()
    line = f.readline()
    s.close()
    return json.loads(line)


def test_frontends(mod):
    print("[SDK path HTTP/TCP front-end]")
    import server_common as sc
    b = mod.PiperSDKBackend(can_port="can0")
    httpd = sc.ThreadingHTTPServer(("127.0.0.1", 0), sc.make_http_handler(b, "tok"))
    hp = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tcpd = sc.ThreadingTCPServer(("127.0.0.1", 0), sc.make_tcp_handler(b, "tok"))
    tp = tcpd.server_address[1]
    threading.Thread(target=tcpd.serve_forever, daemon=True).start()
    try:
        code, body = _http(hp, "/state")            # no token
        check("http no-token 401", code == 401)
        code, body = _http(hp, "/state", token="tok")
        check("http state 200", code == 200 and body["ok"] and body["state"]["gripper_mm"] == 40.0)
        code, body = _http(hp, "/cmd", {"action": "joint_ctrl",
                                        "joints": [0, 30, -30, 0, 20, 0]}, token="tok")
        check("http joint_ctrl ok", code == 200 and body["ok"])
        code, body = _http(hp, "/cmd", {"action": "joint_ctrl",
                                        "joints": [0, 999, 0, 0, 0, 0]}, token="tok")
        check("http out-of-range 400", code == 400)

        body = _tcp_roundtrip(tp, {"action": "enable", "token": "tok"})
        check("tcp enable ok", body["ok"])
    finally:
        httpd.shutdown(); httpd.server_close()
        tcpd.shutdown(); tcpd.server_close()


def test_jog(mod):
    print("[relative jog -> heading-aware, EndPoseCtrl]")
    import math
    b = mod.PiperSDKBackend(can_port="can0")
    fake = b.piper
    fake.calls = []                       # ignore startup noise

    # --- forward jog: feedback pose (200,0,150 mm; yaw 0) advances +X ---
    r = b.cmd_jog(fwd=0.01)
    check("jog ok", r.get("ok") is True)
    args = last_call(fake, "EndPoseCtrl")
    check("jog issues EndPoseCtrl", args is not None and len(args) == 6)
    check("fwd +10mm -> X 210000", args[0] == 210000)
    check("y unchanged 0", args[1] == 0)
    check("z unchanged 150000", args[2] == 150000)
    mc = last_call(fake, "MotionCtrl_2")
    check("jog uses MOVE P", mc is not None and mc[1] == 0x00)

    # --- heading-relative: yaw 90 deg turns 'forward' into +Y ---
    b._current_pose = lambda: ((0.2, 0.0, 0.15), (0.0, 90.0, 90.0))
    r = b.cmd_jog(fwd=0.01)
    args = last_call(fake, "EndPoseCtrl")
    check("yaw90 fwd -> +Y", args[1] == 10000 and args[0] == 200000)

    # --- up is world +Z regardless of yaw ---
    b._current_pose = lambda: ((0.2, 0.0, 0.15), (0.0, 90.0, 0.0))
    b.cmd_jog(up=0.005)
    args = last_call(fake, "EndPoseCtrl")
    check("up +5mm -> Z 155000", args[2] == 155000)

    # --- roll about the tool optical axis changes orientation ---
    b._current_pose = lambda: ((0.2, 0.0, 0.15), (0.0, 90.0, 0.0))
    b.cmd_jog(roll=2.0)
    args = last_call(fake, "EndPoseCtrl")
    check("roll jog changes RX", args[3] != 0)

    # --- rotation round-trip + gimbal-lock continuity ---
    R = b._rotxyz(0.0, 90.0, 0.0)
    rr, pp, yy = b._mat_to_euler_near(R, 0.0, 0.0)
    check("pitch90 euler stays continuous", abs(pp - 90.0) < 1e-6
          and abs(yy) < 1e-6 and abs(rr) < 1e-6)

    # --- repeated jogs do NOT re-send the mode switch (smooth streaming) ---
    b._current_pose = lambda: ((0.2, 0.0, 0.15), (0.0, 90.0, 0.0))
    b._move_mode = b.MOVE_P
    fake.calls = []
    b.cmd_jog(fwd=0.004)
    b.cmd_jog(fwd=0.004)
    mc_calls = [c for c in fake.calls if c[0] in ("MotionCtrl_2", "ModeCtrl")]
    check("no redundant mode switch while streaming", len(mc_calls) == 0)
    check("each jog still commands", sum(1 for c in fake.calls
                                         if c[0] == "EndPoseCtrl") == 2)


def test_watchdog(mod):
    print("[watchdog / CAN recovery]")
    # --- _can_bus_off parses `ip -details link show` output ---
    class FakeCompleted:
        def __init__(self, out):
            self.stdout = out

    orig_run = mod.subprocess.run
    try:
        mod.subprocess.run = lambda *a, **k: FakeCompleted("... can state BUS-OFF ...")
        check("detects BUS-OFF", mod._can_bus_off("can0") is True)
        mod.subprocess.run = lambda *a, **k: FakeCompleted("... can state ERROR-ACTIVE ...")
        check("ERROR-ACTIVE not BUS-OFF", mod._can_bus_off("can0") is False)
        mod.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("no ip"))
        check("ip failure -> not BUS-OFF", mod._can_bus_off("can0") is False)
    finally:
        mod.subprocess.run = orig_run

    # --- recover_can issues down/bitrate+restart/up and confirms recovery ---
    calls = []
    def fake_run_ok(cmd, **k):
        calls.append(cmd)
        return FakeCompleted("")
    orig_run = mod.subprocess.run
    orig_up, orig_off = mod._can_is_up, mod._can_bus_off
    try:
        mod.subprocess.run = fake_run_ok
        mod._can_is_up = lambda iface: True
        mod._can_bus_off = lambda iface: False   # recovered
        ok = mod.recover_can("can0", 1000000, log=mod.log)
        joined = " ".join(" ".join(c) for c in calls)
        check("recover_can succeeds", ok is True)
        check("recover issues down", "down" in joined)
        check("recover sets restart-ms", "restart-ms 100" in joined)
        check("recover sets bitrate", "bitrate 1000000" in joined)
        check("recover brings up", " up" in joined or joined.endswith("up"))
    finally:
        mod.subprocess.run = orig_run
        mod._can_is_up, mod._can_bus_off = orig_up, orig_off

    # --- reconnect() rebuilds the SDK link and re-enables ---
    b = mod.PiperSDKBackend(can_port="can0")
    old_piper = b.piper
    b.reconnect()
    check("reconnect makes new piper instance", b.piper is not old_piper)
    check("reconnect ConnectPort on new instance",
          any(c[0] == "ConnectPort" for c in b.piper.calls))
    check("reconnect re-enables", any(c[0] == "EnableArm" for c in b.piper.calls))


def main():
    mod = load_server()
    test_backend_units(mod)
    test_frontends(mod)
    test_jog(mod)
    test_watchdog(mod)
    print("\n=================================")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    for f in FAILED:
        print("  FAILED: " + f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
