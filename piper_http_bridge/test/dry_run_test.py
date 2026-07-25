#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
Dry-run test for piper_http_bridge_node -- runs WITHOUT ROS, CAN, or the arm.

It injects stub modules for rospy and the ROS message/service types, then loads
the real node module and exercises:
  * joint limit validation (deg)
  * unit conversion (deg<->rad, mm<->m)
  * the HTTP server (GET /state, POST /cmd)
  * the TCP server (newline-delimited JSON)
  * auth-token enforcement

Run:  python test/dry_run_test.py
Exits 0 on success, 1 on failure.
"""

import importlib.util
import json
import math
import os
import socket
import sys
import threading
import time
import types
import urllib.request
import urllib.error

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
NODE_PATH = os.path.join(THIS_DIR, "..", "scripts", "piper_http_bridge_node.py")

PASSED = []
FAILED = []


def check(name, cond):
    if cond:
        PASSED.append(name)
        print("  PASS  %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s" % name)


# ---------------------------------------------------------------------------
# Build stub ROS modules so the node imports cleanly without a ROS install.
# ---------------------------------------------------------------------------
def build_stubs():
    # rospy ---------------------------------------------------------------
    rospy = types.ModuleType("rospy")

    class _Time:
        @staticmethod
        def now():
            t = types.SimpleNamespace()
            t.to_sec = lambda: time.time()
            return t

    rospy.Time = _Time
    rospy.loginfo = lambda *a, **k: None
    rospy.logwarn = lambda *a, **k: None
    rospy.logwarn_throttle = lambda *a, **k: None
    rospy.logerr = lambda *a, **k: None
    rospy.logdebug = lambda *a, **k: None
    rospy.init_node = lambda *a, **k: None
    rospy.get_param = lambda name, default=None: default
    rospy.spin = lambda: None
    rospy.is_shutdown = lambda: False

    class Publisher:
        def __init__(self, *a, **k):
            self.last = None

        def publish(self, msg):
            self.last = msg
            Publisher.published.append(msg)

    Publisher.published = []
    rospy.Publisher = Publisher

    class Subscriber:
        def __init__(self, topic, msgtype, cb, **k):
            Subscriber.registry[topic] = cb

    Subscriber.registry = {}
    rospy.Subscriber = Subscriber

    class ServiceProxy:
        def __init__(self, name, srvtype):
            self.name = name

        def wait_for_service(self, timeout=None):
            return True

        def __call__(self, *a, **k):
            r = types.SimpleNamespace()
            r.success = True
            r.message = "stub:%s" % self.name
            return r

    rospy.ServiceProxy = ServiceProxy
    sys.modules["rospy"] = rospy

    # message packages -----------------------------------------------------
    def _msg_class(attrs):
        def __init__(self):
            for a in attrs:
                setattr(self, a, 0.0 if a != "header" else types.SimpleNamespace(stamp=0))
            # JointState needs list fields
            if "position" in attrs:
                self.position = []
                self.velocity = []
                self.effort = []
                self.name = []
        return type("Msg", (), {"__init__": __init__})

    sensor_msgs = types.ModuleType("sensor_msgs")
    sensor_msgs_msg = types.ModuleType("sensor_msgs.msg")
    sensor_msgs_msg.JointState = _msg_class(["header", "name", "position",
                                             "velocity", "effort"])
    sensor_msgs.msg = sensor_msgs_msg
    sys.modules["sensor_msgs"] = sensor_msgs
    sys.modules["sensor_msgs.msg"] = sensor_msgs_msg

    std_msgs = types.ModuleType("std_msgs")
    std_msgs_msg = types.ModuleType("std_msgs.msg")
    std_msgs_msg.Bool = _msg_class(["data"])
    std_msgs.msg = std_msgs_msg
    sys.modules["std_msgs"] = std_msgs
    sys.modules["std_msgs.msg"] = std_msgs_msg

    std_srvs = types.ModuleType("std_srvs")
    std_srvs_srv = types.ModuleType("std_srvs.srv")
    std_srvs_srv.Trigger = object
    std_srvs_srv.SetBool = object
    std_srvs.srv = std_srvs_srv
    sys.modules["std_srvs"] = std_srvs
    sys.modules["std_srvs.srv"] = std_srvs_srv

    piper_msgs = types.ModuleType("piper_msgs")
    piper_msgs_msg = types.ModuleType("piper_msgs.msg")
    piper_msgs_msg.PiperStatusMsg = _msg_class(
        ["ctrl_mode", "arm_status", "mode_feedback", "teach_status",
         "motion_status", "trajectory_num", "err_code"])
    piper_msgs_msg.PosCmd = _msg_class(["x", "y", "z", "roll", "pitch",
                                        "yaw", "gripper", "mode1", "mode2"])
    piper_msgs_msg.PiperEulerPose = _msg_class(["header", "x", "y", "z",
                                                "roll", "pitch", "yaw"])
    piper_msgs.msg = piper_msgs_msg
    sys.modules["piper_msgs"] = piper_msgs
    sys.modules["piper_msgs.msg"] = piper_msgs_msg

    piper_msgs_srv = types.ModuleType("piper_msgs.srv")
    piper_msgs_srv.Enable = object
    piper_msgs_srv.Gripper = object
    piper_msgs_srv.GoZero = object
    piper_msgs.srv = piper_msgs_srv
    sys.modules["piper_msgs.srv"] = piper_msgs_srv

    return rospy


def load_node():
    build_stubs()
    spec = importlib.util.spec_from_file_location("piper_http_bridge_node",
                                                  NODE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_validation_and_units(mod):
    print("[validation & unit conversion]")
    B = mod.PiperBridge.__new__(mod.PiperBridge)  # bypass __init__/ROS pubs
    # good joints
    ok = mod.PiperBridge._validate_joints([0, 30, -30, 0, 20, 0])
    check("accepts in-range joints", ok == [0, 30, -30, 0, 20, 0])
    # wrong length
    try:
        mod.PiperBridge._validate_joints([0, 30])
        check("rejects wrong joint count", False)
    except ValueError:
        check("rejects wrong joint count", True)
    # out of range (J2 max 195)
    try:
        mod.PiperBridge._validate_joints([0, 200, 0, 0, 0, 0])
        check("rejects out-of-range joint", False)
    except ValueError:
        check("rejects out-of-range joint", True)
    # non-numeric
    try:
        mod.PiperBridge._validate_joints([0, "x", 0, 0, 0, 0])
        check("rejects non-numeric joint", False)
    except ValueError:
        check("rejects non-numeric joint", True)
    # speed clamp
    check("clamps speed high", mod.PiperBridge._clamp_speed(500) == 100)
    check("clamps speed low", mod.PiperBridge._clamp_speed(-5) == 1)
    check("clamps speed default", mod.PiperBridge._clamp_speed("bad") == 50)
    # constants
    check("deg2rad", abs(mod.DEG2RAD - math.pi / 180.0) < 1e-12)
    check("mm2m", abs(mod.MM2M - 0.001) < 1e-12)


def _start_servers(mod, token=""):
    import rospy  # the stub
    bridge = mod.PiperBridge()
    http_handler = mod.make_http_handler(bridge, token)
    from http.server import ThreadingHTTPServer
    from socketserver import ThreadingTCPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), http_handler)
    http_port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    tcp_handler = mod.make_tcp_handler(bridge, token)
    tcpd = ThreadingTCPServer(("127.0.0.1", 0), tcp_handler)
    tcp_port = tcpd.server_address[1]
    threading.Thread(target=tcpd.serve_forever, daemon=True).start()
    return bridge, httpd, http_port, tcpd, tcp_port


def _http(port, path, payload=None, token=""):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=("POST" if payload is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode(), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _tcp(port, payload, token=""):
    if token:
        payload = dict(payload, token=token)
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    f = s.makefile("rw")
    f.write(json.dumps(payload) + "\n")
    f.flush()
    line = f.readline()
    s.close()
    return json.loads(line)


def test_http(mod):
    print("[HTTP server]")
    bridge, httpd, port, tcpd, _ = _start_servers(mod)
    try:
        code, body = _http(port, "/health")
        check("GET /health 200", code == 200 and body.get("ok"))

        code, body = _http(port, "/state")
        check("GET /state 200", code == 200 and body.get("ok") and "state" in body)

        code, body = _http(port, "/cmd", {"action": "enable"})
        check("POST enable", code == 200 and body.get("ok"))

        code, body = _http(port, "/cmd",
                           {"action": "joint_ctrl",
                            "joints": [0, 30, -30, 0, 20, 0], "speed": 30})
        check("POST joint_ctrl ok", code == 200 and body.get("ok"))

        # verify radian conversion on the published JointState
        import rospy
        pub = rospy.Publisher.published[-1]
        check("joint_ctrl publishes 7-dof", len(pub.position) == 7)
        check("joint1 deg->rad", abs(pub.position[1] - 30 * math.pi / 180) < 1e-6)
        check("joint speed in velocity[6]", abs(pub.velocity[6] - 30) < 1e-6)

        code, body = _http(port, "/cmd",
                           {"action": "joint_ctrl", "joints": [0, 999, 0, 0, 0, 0]})
        check("POST joint_ctrl out-of-range -> 400", code == 400 and not body.get("ok"))

        code, body = _http(port, "/cmd",
                           {"action": "pose_ctrl", "x": 200, "y": 0, "z": 200,
                            "roll": 0, "pitch": 90, "yaw": 0})
        check("POST pose_ctrl ok", code == 200 and body.get("ok"))
        pub = rospy.Publisher.published[-1]
        check("pose x mm->m", abs(pub.x - 0.2) < 1e-9)
        check("pose pitch deg->rad", abs(pub.pitch - 90 * math.pi / 180) < 1e-6)

        code, body = _http(port, "/cmd", {"action": "gripper",
                                          "position_mm": 40, "effort": 1000})
        check("POST gripper ok", code == 200 and body.get("ok"))

        code, body = _http(port, "/cmd", {"action": "stop"})
        check("POST stop ok", code == 200 and body.get("ok"))

        code, body = _http(port, "/cmd", {"action": "bogus"})
        check("unknown action -> 400", code == 400)

        code, body = _http(port, "/nope")
        check("bad path -> 404", code == 404)
    finally:
        httpd.shutdown()
        tcpd.shutdown()


def test_tcp(mod):
    print("[TCP server]")
    bridge, httpd, _, tcpd, port = _start_servers(mod)
    try:
        body = _tcp(port, {"action": "enable"})
        check("tcp enable ok", body.get("ok"))
        body = _tcp(port, {"action": "state"})
        check("tcp state ok", body.get("ok") and "state" in body)
        body = _tcp(port, {"action": "joint_ctrl",
                           "joints": [0, 30, -30, 0, 20, 0]})
        check("tcp joint_ctrl ok", body.get("ok"))
        body = _tcp(port, {"action": "joint_ctrl", "joints": [0, 999, 0, 0, 0, 0]})
        check("tcp joint_ctrl out-of-range", not body.get("ok"))
    finally:
        httpd.shutdown()
        tcpd.shutdown()


def test_auth(mod):
    print("[auth token]")
    bridge, httpd, http_port, tcpd, tcp_port = _start_servers(mod, token="secret")
    try:
        code, body = _http(http_port, "/state")  # no token
        check("http no-token -> 401", code == 401)
        code, body = _http(http_port, "/state", token="secret")
        check("http with-token 200", code == 200 and body.get("ok"))
        body = _tcp(tcp_port, {"action": "state"})  # no token
        check("tcp no-token -> 401", body.get("ok") is False)
        body = _tcp(tcp_port, {"action": "state"}, token="secret")
        check("tcp with-token ok", body.get("ok"))
    finally:
        httpd.shutdown()
        tcpd.shutdown()


def main():
    mod = load_node()
    test_validation_and_units(mod)
    test_http(mod)
    test_tcp(mod)
    test_auth(mod)
    print("\n=================================")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  FAILED: %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
