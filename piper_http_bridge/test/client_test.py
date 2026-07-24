#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
Round-trip test: run a fake-SDK control server locally, then drive it with
PiperClient over BOTH HTTP and TCP. No ROS, no CAN, no real arm.

Run:  python test/client_test.py   ->  exit 0 on success
"""

import importlib.util
import json
import os
import sys
import threading
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
CLIENT = os.path.join(HERE, "..", "client")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, CLIENT)

PASSED, FAILED = [], []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakePiper:
    def __init__(self, can_name="can0", **kw):
        self.calls = []

    def _rec(self, n, *a): self.calls.append((n, a))
    def ConnectPort(self, *a, **k): self._rec("ConnectPort")
    def DisconnectPort(self, *a, **k): self._rec("DisconnectPort")
    def MotionCtrl_2(self, *a): self._rec("MotionCtrl_2", *a)
    def ModeCtrl(self, *a): self._rec("ModeCtrl", *a)
    def EnableArm(self, *a): self._rec("EnableArm", *a)
    def DisableArm(self, *a): self._rec("DisableArm", *a)
    def JointCtrl(self, *a): self._rec("JointCtrl", *a)
    def EndPoseCtrl(self, *a): self._rec("EndPoseCtrl", *a)
    def GripperCtrl(self, *a): self._rec("GripperCtrl", *a)
    def EmergencyStop(self, *a): self._rec("EmergencyStop", *a)
    def ResetPiper(self, *a): self._rec("ResetPiper", *a)

    def GetArmJointMsgs(self):
        return _Obj(joint_state=_Obj(joint_1=10000, joint_2=20000, joint_3=-30000,
                                     joint_4=0, joint_5=5000, joint_6=-10000))

    def GetArmGripperMsgs(self):
        return _Obj(gripper_state=_Obj(grippers_angle=40000))

    def GetArmEndPoseMsgs(self):
        return _Obj(end_pose=_Obj(X_axis=200000, Y_axis=0, Z_axis=150000,
                                  RX_axis=0, RY_axis=90000, RZ_axis=0))

    def GetArmStatus(self):
        return _Obj(arm_status=_Obj(ctrl_mode=1, arm_status=0, mode_feed=1,
                                    teach_status=0, motion_status=0, err_code=0))


def load_server_module():
    fake = types.ModuleType("piper_sdk")
    fake.C_PiperInterface_V2 = FakePiper
    fake.C_PiperInterface = FakePiper
    sys.modules["piper_sdk"] = fake
    spec = importlib.util.spec_from_file_location(
        "piper_sdk_server", os.path.join(SCRIPTS, "piper_sdk_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def start_server(mod, token="tok"):
    import server_common as sc
    backend = mod.PiperSDKBackend(can_port="can0")
    httpd = sc.ThreadingHTTPServer(("127.0.0.1", 0), sc.make_http_handler(backend, token))
    hp = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tcpd = sc.ThreadingTCPServer(("127.0.0.1", 0), sc.make_tcp_handler(backend, token))
    tp = tcpd.server_address[1]
    threading.Thread(target=tcpd.serve_forever, daemon=True).start()
    return backend, (httpd, tcpd), hp, tp


def last_call(fake, name):
    for m, a in reversed(fake.calls):
        if m == name:
            return a
    return None


def exercise(client, fake, label):
    print("[%s]" % label)
    st = client.state()
    check("%s state joints deg" % label, st["joints_deg"][1] == 20.0)
    check("%s state gripper mm" % label, st["gripper_mm"] == 40.0)

    client.enable();  check("%s enable -> EnableArm" % label, last_call(fake, "EnableArm") is not None)
    client.joint_ctrl([0, 30, -30, 0, 20, 0], speed=10)
    args = last_call(fake, "JointCtrl")
    check("%s joint deg->0.001deg" % label, args == (0, 30000, -30000, 0, 20000, 0))
    check("%s speed 10 applied" % label, last_call(fake, "MotionCtrl_2")[2] == 10)

    client.pose_ctrl(200, 0, 150, 0, 90, 0)
    check("%s pose mm/deg->0.001" % label, last_call(fake, "EndPoseCtrl") == (200000, 0, 150000, 0, 90000, 0))

    client.gripper(40)
    check("%s gripper 40mm->40000" % label, last_call(fake, "GripperCtrl")[0] == 40000)

    client.go_zero(); check("%s go_zero all-zero" % label, last_call(fake, "JointCtrl") == (0, 0, 0, 0, 0, 0))
    client.stop();    check("%s stop estop" % label, last_call(fake, "EmergencyStop") == (0x01,))
    client.disable(); check("%s disable" % label, last_call(fake, "DisableArm") is not None)

    # limit rejection surfaces as PiperError
    try:
        client.joint_ctrl([0, 999, 0, 0, 0, 0])
        check("%s out-of-range raises" % label, False)
    except Exception:
        check("%s out-of-range raises" % label, True)


def main():
    import piper_client as pc
    mod = load_server_module()
    backend, servers, hp, tp = start_server(mod, token="tok")
    fake = backend.piper
    try:
        # wrong token should fail
        bad = pc.PiperClient("127.0.0.1", http_port=hp, token="wrong")
        try:
            bad.state()
            check("wrong token rejected", False)
        except pc.PiperError:
            check("wrong token rejected", True)

        http_client = pc.PiperClient("127.0.0.1", http_port=hp, token="tok")
        exercise(http_client, fake, "HTTP")

        tcp_client = pc.PiperClient("127.0.0.1", tcp_port=tp, token="tok", use_tcp=True)
        exercise(tcp_client, fake, "TCP")
    finally:
        for s in servers:
            s.shutdown(); s.server_close()

    print("\n=================================")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    for f in FAILED:
        print("  FAILED: " + f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
