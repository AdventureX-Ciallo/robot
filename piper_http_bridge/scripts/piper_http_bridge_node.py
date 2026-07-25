#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
piper_http_bridge_node

Bridges the official AgileX ``piper_ctrl_single_node`` ROS interface to a plain
HTTP/JSON (and TCP newline-delimited-JSON) control port so that any client on
the network can drive the Piper 6-axis arm without installing ROS.

Units exposed to HTTP/TCP clients (converted to/from ROS here):
  * joints            -> degrees (ROS topic uses radians)
  * end pose (x,y,z)  -> millimetres (ROS topic uses metres)
  * end pose (r,p,y)  -> degrees   (ROS topic uses radians)
  * gripper           -> millimetres (ROS service uses metres)

This node does NOT talk to CAN directly; it publishes/subscribes/calls the
topics & services already provided by ``piper_ctrl_single_node.py``.
"""

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingTCPServer, StreamRequestHandler

import rospy
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool
from std_srvs.srv import Trigger, SetBool
from piper_msgs.msg import PiperStatusMsg, PosCmd, PiperEulerPose
from piper_msgs.srv import Enable, Gripper, GoZero

# ---------------------------------------------------------------------------
# Joint limits (degrees) -- measured motion range of this arm.
# Used for software-side validation before forwarding to the arm.
# ---------------------------------------------------------------------------
JOINT_LIMITS_DEG = [
    (-154.0, 154.0),   # joint 1
    (0.0,    195.0),   # joint 2
    (-175.0, 0.0),     # joint 3
    (-102.0, 102.0),   # joint 4
    (-75.0,  75.0),    # joint 5
    (-170.0, 170.0),   # joint 6
]

# Per-joint max velocity (deg/s) -- measured activity speed of this arm.
JOINT_MAX_DPS = [180.0, 195.0, 180.0, 225.0, 225.0, 225.0]

# Gripper open/close travel (mm) -- measured 0..100 with +/-0.5 mm tolerance.
GRIPPER_MIN_MM = 0.0
GRIPPER_MAX_MM = 100.0
GRIPPER_TOL_MM = 0.5


def clamp_gripper(position_mm):
    try:
        v = float(position_mm)
    except (TypeError, ValueError):
        return GRIPPER_MIN_MM
    return max(GRIPPER_MIN_MM, min(GRIPPER_MAX_MM, v))

DEG2RAD = math.pi / 180.0
RAD2DEG = 180.0 / math.pi
MM2M = 0.001
M2MM = 1000.0


class PiperBridge(object):
    """Owns all ROS publishers/subscribers/service-proxies and shared state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest = {
            "joints_deg": None,      # list[6]
            "gripper_mm": None,
            "end_pose": None,        # dict x,y,z (mm), roll,pitch,yaw (deg)
            "arm_status": None,      # dict
            "enabled": None,         # bool
            "stamp": None,
        }

        # Publishers to the control node.
        self._pub_joint = rospy.Publisher("/joint_ctrl_single", JointState,
                                          queue_size=1, tcp_nodelay=True)
        self._pub_pos = rospy.Publisher("/pos_cmd", PosCmd,
                                        queue_size=1, tcp_nodelay=True)
        self._pub_enable_flag = rospy.Publisher("/enable_flag", Bool,
                                                queue_size=1, tcp_nodelay=True)

        # Subscribers for feedback.
        rospy.Subscriber("/joint_states_single", JointState,
                         self._cb_joint, queue_size=1, tcp_nodelay=True)
        rospy.Subscriber("/end_pose_euler", PiperEulerPose,
                         self._cb_end_pose, queue_size=1, tcp_nodelay=True)
        rospy.Subscriber("/arm_status", PiperStatusMsg,
                         self._cb_arm_status, queue_size=1, tcp_nodelay=True)

        # Service proxies (lazy-connected on first use).
        self._srv_enable = rospy.ServiceProxy("/enable_srv", Enable)
        self._srv_gripper = rospy.ServiceProxy("/gripper_srv", Gripper)
        self._srv_go_zero = rospy.ServiceProxy("/go_zero_srv", GoZero)
        self._srv_stop = rospy.ServiceProxy("/stop_srv", Trigger)
        self._srv_reset = rospy.ServiceProxy("/reset_srv", Trigger)
        self._srv_block = rospy.ServiceProxy("/block_arm", SetBool)

    # ------------------------------------------------------------------
    # Feedback callbacks
    # ------------------------------------------------------------------
    def _cb_joint(self, msg):
        # position[0..5] rad -> deg ; position[6] gripper m -> mm
        with self._lock:
            try:
                self._latest["joints_deg"] = [round(p * RAD2DEG, 3)
                                              for p in msg.position[0:6]]
                if len(msg.position) > 6:
                    self._latest["gripper_mm"] = round(msg.position[6] * M2MM, 3)
                self._latest["stamp"] = rospy.Time.now().to_sec()
            except Exception as e:
                rospy.logwarn_throttle(5.0, "joint cb error: %s", e)

    def _cb_end_pose(self, msg):
        with self._lock:
            self._latest["end_pose"] = {
                "x": round(msg.x * M2MM, 3),
                "y": round(msg.y * M2MM, 3),
                "z": round(msg.z * M2MM, 3),
                "roll": round(msg.roll * RAD2DEG, 3),
                "pitch": round(msg.pitch * RAD2DEG, 3),
                "yaw": round(msg.yaw * RAD2DEG, 3),
            }

    def _cb_arm_status(self, msg):
        with self._lock:
            self._latest["arm_status"] = {
                "ctrl_mode": msg.ctrl_mode,
                "arm_status": msg.arm_status,
                "mode_feedback": msg.mode_feedback,
                "teach_status": msg.teach_status,
                "motion_status": msg.motion_status,
                "err_code": msg.err_code,
            }
            # motion_status 0x00 == reached set point (roughly "enabled & alive")
            self._latest["enabled"] = (msg.arm_status == 0)

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------
    def get_state(self):
        with self._lock:
            return json.loads(json.dumps(self._latest))

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_joints(joints):
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
                    "joint %d = %.3f deg out of range [%.1f, %.1f]"
                    % (i + 1, fv, lo, hi))
            out.append(fv)
        return out

    @staticmethod
    def _clamp_speed(speed):
        try:
            s = int(speed)
        except (TypeError, ValueError):
            return 50
        return max(1, min(100, s))

    # ------------------------------------------------------------------
    # Command implementations (each returns a result dict)
    # ------------------------------------------------------------------
    def cmd_enable(self, enable):
        enable = bool(enable)
        try:
            self._srv_enable.wait_for_service(timeout=2.0)
            resp = self._srv_enable(enable)
        except Exception as e:
            # Fallback: publish the enable_flag topic (node also subscribes it).
            self._pub_enable_flag.publish(Bool(data=enable))
            return {"ok": True, "via": "topic", "enabled": enable}
        with self._lock:
            self._latest["enabled"] = enable
        return {"ok": True, "via": "service", "enabled": enable,
                "message": getattr(resp, "message", "")}

    def cmd_joint_ctrl(self, joints, speed=50, gripper_mm=None):
        joints = self._validate_joints(joints)
        speed = self._clamp_speed(speed)

        msg = JointState()
        msg.header.stamp = rospy.Time.now()
        msg.name = ["joint1", "joint2", "joint3", "joint4", "joint5",
                    "joint6", "gripper"]
        rad = [j * DEG2RAD for j in joints]
        # gripper: if not requested, hold current position (or 0).
        if gripper_mm is None:
            with self._lock:
                cur = self._latest.get("gripper_mm")
            gripper_m = (cur if cur is not None else 0.0) * MM2M
        else:
            gripper_m = float(gripper_mm) * MM2M
        msg.position = rad + [gripper_m]
        msg.velocity = [0.0] * 6 + [float(speed)]
        msg.effort = [0.0] * 7
        self._pub_joint.publish(msg)
        return {"ok": True, "joints_deg": joints, "speed": speed,
                "gripper_mm": gripper_m * M2MM}

    def cmd_pose_ctrl(self, x, y, z, roll, pitch, yaw, gripper_mm=None):
        msg = PosCmd()
        msg.x = float(x) * MM2M
        msg.y = float(y) * MM2M
        msg.z = float(z) * MM2M
        msg.roll = float(roll) * DEG2RAD
        msg.pitch = float(pitch) * DEG2RAD
        msg.yaw = float(yaw) * DEG2RAD
        if gripper_mm is not None:
            msg.gripper = float(gripper_mm) * MM2M
        self._pub_pos.publish(msg)
        return {"ok": True, "pose_mm_deg": [x, y, z, roll, pitch, yaw]}

    def cmd_gripper(self, position_mm, effort=1000):
        # position_mm: 0..100 mm ; effort: 0..5000 (0.001 N/m)
        position_mm = clamp_gripper(position_mm)
        position_m = position_mm * MM2M
        try:
            self._srv_gripper.wait_for_service(timeout=2.0)
            resp = self._srv_gripper(position_m, float(effort))
            return {"ok": True, "gripper_mm": position_mm,
                    "message": getattr(resp, "message", "")}
        except Exception as e:
            raise RuntimeError("gripper service failed: %s" % e)

    def _call_trigger(self, proxy, name):
        try:
            proxy.wait_for_service(timeout=2.0)
            resp = proxy()
            return {"ok": bool(getattr(resp, "success", True)),
                    "action": name,
                    "message": getattr(resp, "message", "")}
        except Exception as e:
            raise RuntimeError("%s service failed: %s" % (name, e))

    def cmd_stop(self):
        return self._call_trigger(self._srv_stop, "stop")

    def cmd_reset(self):
        return self._call_trigger(self._srv_reset, "reset")

    def cmd_go_zero(self, is_mit_mode=False):
        try:
            self._srv_go_zero.wait_for_service(timeout=2.0)
            resp = self._srv_go_zero(bool(is_mit_mode))
            return {"ok": True, "action": "go_zero",
                    "is_mit_mode": bool(is_mit_mode),
                    "message": getattr(resp, "message", "")}
        except Exception as e:
            raise RuntimeError("go_zero service failed: %s" % e)

    def cmd_block_arm(self, block):
        try:
            self._srv_block.wait_for_service(timeout=2.0)
            resp = self._srv_block(bool(block))
            return {"ok": bool(getattr(resp, "success", True)),
                    "action": "block_arm", "block": bool(block),
                    "message": getattr(resp, "message", "")}
        except Exception as e:
            raise RuntimeError("block_arm service failed: %s" % e)


# ---------------------------------------------------------------------------
# Command dispatcher shared by HTTP and TCP servers.
# ---------------------------------------------------------------------------
def dispatch(bridge, payload):
    """payload: dict with at least an 'action' key. Returns a result dict."""
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    action = payload.get("action")
    if not action:
        raise ValueError("missing 'action' field")

    if action == "state":
        return {"ok": True, "state": bridge.get_state()}
    if action == "enable":
        return bridge.cmd_enable(payload.get("enable", True))
    if action == "disable":
        return bridge.cmd_enable(False)
    if action == "joint_ctrl":
        return bridge.cmd_joint_ctrl(
            payload.get("joints"),
            payload.get("speed", 50),
            payload.get("gripper_mm"))
    if action == "pose_ctrl":
        return bridge.cmd_pose_ctrl(
            payload.get("x", 0.0), payload.get("y", 0.0), payload.get("z", 0.0),
            payload.get("roll", 0.0), payload.get("pitch", 0.0),
            payload.get("yaw", 0.0), payload.get("gripper_mm"))
    if action == "gripper":
        return bridge.cmd_gripper(payload.get("position_mm", 0.0),
                                  payload.get("effort", 1000))
    if action == "stop":
        return bridge.cmd_stop()
    if action == "reset":
        return bridge.cmd_reset()
    if action == "go_zero":
        return bridge.cmd_go_zero(payload.get("is_mit_mode", False))
    if action == "block_arm":
        return bridge.cmd_block_arm(payload.get("block", True))
    raise ValueError("unknown action: %r" % action)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
def make_http_handler(bridge, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PiperHttpBridge/0.1"

        def _authorized(self):
            if not token:
                return True
            auth = self.headers.get("Authorization", "")
            return auth == ("Bearer %s" % token)

        def _send_json(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # quieter logs
            rospy.logdebug("http %s - %s" % (self.address_string(),
                                             fmt % args))

        def do_GET(self):
            if not self._authorized():
                return self._send_json(401, {"ok": False, "error": "unauthorized"})
            if self.path in ("/state", "/api/state"):
                return self._send_json(200, {"ok": True,
                                             "state": bridge.get_state()})
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
                result = dispatch(bridge, payload)
                return self._send_json(200, result)
            except ValueError as e:
                return self._send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                rospy.logerr("cmd error: %s", e)
                return self._send_json(500, {"ok": False, "error": str(e)})

    return Handler


# ---------------------------------------------------------------------------
# TCP server (newline-delimited JSON)
# ---------------------------------------------------------------------------
def make_tcp_handler(bridge, token):
    class TcpHandler(StreamRequestHandler):
        def handle(self):
            for line in self.rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line.decode("utf-8"))
                    if token and payload.get("token") != token:
                        self._reply(401, {"ok": False, "error": "unauthorized"})
                        continue
                    result = dispatch(bridge, payload)
                    self._reply(200, result)
                except ValueError as e:
                    self._reply(400, {"ok": False, "error": str(e)})
                except Exception as e:
                    rospy.logerr("tcp cmd error: %s", e)
                    self._reply(500, {"ok": False, "error": str(e)})

        def _reply(self, code, obj):
            self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))

    return TcpHandler


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    rospy.init_node("piper_http_bridge", anonymous=False)

    http_port = rospy.get_param("~http_port", 8080)
    tcp_port = rospy.get_param("~tcp_port", 9090)
    host = rospy.get_param("~host", "0.0.0.0")
    token = rospy.get_param("~token", "")
    enable_tcp = rospy.get_param("~enable_tcp", True)

    bridge = PiperBridge()
    rospy.loginfo("piper_http_bridge: ROS bridge ready")

    # HTTP server in its own thread.
    http_handler = make_http_handler(bridge, token)
    httpd = ThreadingHTTPServer((host, http_port), http_handler)
    httpd.daemon_threads = True
    http_th = threading.Thread(target=httpd.serve_forever)
    http_th.daemon = True
    http_th.start()
    rospy.loginfo("HTTP control listening on http://%s:%d  (GET /state, POST /cmd)",
                  host, http_port)

    # Optional TCP server in its own thread.
    tcpd = None
    if enable_tcp:
        tcp_handler = make_tcp_handler(bridge, token)
        tcpd = ThreadingTCPServer((host, tcp_port), tcp_handler)
        tcpd.daemon_threads = True
        tcp_th = threading.Thread(target=tcpd.serve_forever)
        tcp_th.daemon = True
        tcp_th.start()
        rospy.loginfo("TCP control listening on tcp://%s:%d (newline-delimited JSON)",
                      host, tcp_port)

    if token:
        rospy.loginfo("auth token ENABLED")
    else:
        rospy.logwarn("auth token DISABLED -- anyone on the network can control the arm!")

    try:
        rospy.spin()
    finally:
        httpd.shutdown()
        if tcpd:
            tcpd.shutdown()


if __name__ == "__main__":
    main()
