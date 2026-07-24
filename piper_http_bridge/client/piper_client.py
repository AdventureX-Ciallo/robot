#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
Piper HTTP/TCP client -- talks to the control port exposed by this repo
(piper_sdk_server.py or the ROS bridge). Pure standard library, no deps.

Usage as a library:
    from piper_client import PiperClient
    arm = PiperClient("192.168.1.100", token="secret")
    arm.enable()
    arm.joint_ctrl([0, 30, -30, 0, 20, 0], speed=10)
    print(arm.state())
    arm.gripper(40)            # open to 40 mm
    arm.go_zero()
    arm.disable()

Or run the built-in demo from the command line:
    python piper_client.py --host 192.168.1.100 --token secret --demo
    python piper_client.py --host 192.168.1.100 --token secret --state
    python piper_client.py --host 192.168.1.100 --token secret \
        --joints 0,30,-30,0,20,0 --speed 10

Units: joints=deg, x/y/z=mm, roll/pitch/yaw=deg, gripper=mm, speed=1..100%.
"""

import argparse
import json
import socket
import sys
import urllib.request
import urllib.error


class PiperError(Exception):
    pass


class PiperClient(object):
    """Client for the Piper HTTP (and TCP) control port."""

    def __init__(self, host, http_port=8080, tcp_port=9090, token="",
                 timeout=5.0, use_tcp=False):
        self.host = host
        self.http_port = http_port
        self.tcp_port = tcp_port
        self.token = token
        self.timeout = timeout
        self.use_tcp = use_tcp  # default transport = HTTP

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _http(self, path, payload=None):
        url = "http://%s:%d%s" % (self.host, self.http_port, path)
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers=headers,
            method=("POST" if payload is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except Exception:
                raise PiperError("HTTP %d" % e.code)
        except urllib.error.URLError as e:
            raise PiperError("connection failed: %s" % e.reason)
        return body

    def _tcp(self, payload):
        if self.token:
            payload = dict(payload, token=self.token)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect((self.host, self.tcp_port))
            f = s.makefile("rw")
            f.write(json.dumps(payload) + "\n")
            f.flush()
            return json.loads(f.readline())
        finally:
            s.close()

    def cmd(self, action, **fields):
        """Send a command; returns the result dict or raises PiperError."""
        payload = dict(fields, action=action)
        body = self._tcp(payload) if self.use_tcp else self._http("/cmd", payload)
        if not body.get("ok"):
            raise PiperError("%s failed: %s" % (action, body.get("error", body)))
        return body

    # ------------------------------------------------------------------
    # high-level API (units: deg / mm)
    # ------------------------------------------------------------------
    def state(self):
        body = self._http("/state") if not self.use_tcp else self._tcp({"action": "state"})
        if not body.get("ok"):
            raise PiperError("state failed: %s" % body.get("error"))
        return body["state"]

    def health(self):
        return self._http("/health")

    def enable(self):
        return self.cmd("enable")

    def disable(self):
        return self.cmd("disable")

    def joint_ctrl(self, joints, speed=50, gripper_mm=None):
        """joints: list of 6 angles in degrees."""
        return self.cmd("joint_ctrl", joints=list(joints), speed=speed,
                        gripper_mm=gripper_mm)

    def pose_ctrl(self, x, y, z, roll=0.0, pitch=0.0, yaw=0.0, gripper_mm=None):
        """x,y,z in mm; roll,pitch,yaw in deg."""
        return self.cmd("pose_ctrl", x=x, y=y, z=z,
                        roll=roll, pitch=pitch, yaw=yaw, gripper_mm=gripper_mm)

    def gripper(self, position_mm, effort=1000):
        """position_mm: 0..80 mm; effort 0..5000 (0.001 N/m)."""
        return self.cmd("gripper", position_mm=position_mm, effort=effort)

    def go_zero(self, is_mit_mode=False):
        return self.cmd("go_zero", is_mit_mode=is_mit_mode)

    def stop(self):
        """Emergency stop (constant-damping fall)."""
        return self.cmd("stop")

    def reset(self):
        """Cut power immediately (arm falls)."""
        return self.cmd("reset")

    def block_arm(self, block=True):
        return self.cmd("block_arm", block=block)

    def set_mode(self, mode):
        """mode: 'joint' (MOVE J) or 'pose' (MOVE P)."""
        return self.cmd("set_mode", mode=mode)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _pretty(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _demo(arm):
    print("-> health:", _pretty(arm.health()))
    print("-> enable")
    arm.enable()
    print("-> state:", _pretty(arm.state()))
    print("-> joint_ctrl [0,30,-30,0,20,0] speed=10")
    arm.joint_ctrl([0, 30, -30, 0, 20, 0], speed=10)
    print("-> gripper 40mm")
    arm.gripper(40)
    print("-> go_zero")
    arm.go_zero()
    print("-> disable")
    arm.disable()
    print("demo done.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Piper HTTP/TCP client CLI")
    ap.add_argument("--host", required=True, help="Orange Pi IP / hostname")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--tcp-port", type=int, default=9090)
    ap.add_argument("--token", default="")
    ap.add_argument("--tcp", action="store_true", help="use TCP instead of HTTP")
    ap.add_argument("--timeout", type=float, default=5.0)
    # actions
    ap.add_argument("--demo", action="store_true", help="run the full demo sequence")
    ap.add_argument("--state", action="store_true", help="print current state")
    ap.add_argument("--enable", action="store_true")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--joints", help="comma-separated 6 joint angles (deg)")
    ap.add_argument("--speed", type=int, default=50)
    ap.add_argument("--pose", help="comma-separated x,y,z,roll,pitch,yaw (mm,deg)")
    ap.add_argument("--gripper", type=float, help="gripper position in mm")
    ap.add_argument("--go-zero", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)

    arm = PiperClient(args.host, http_port=args.http_port,
                      tcp_port=args.tcp_port, token=args.token,
                      timeout=args.timeout, use_tcp=args.tcp)
    try:
        if args.demo:
            _demo(arm)
            return 0
        if args.state:
            print(_pretty(arm.state()))
        if args.enable:
            print(_pretty(arm.enable()))
        if args.disable:
            print(_pretty(arm.disable()))
        if args.joints:
            joints = [float(v) for v in args.joints.split(",")]
            print(_pretty(arm.joint_ctrl(joints, speed=args.speed)))
        if args.pose:
            p = [float(v) for v in args.pose.split(",")]
            p += [0.0] * (6 - len(p))
            print(_pretty(arm.pose_ctrl(*p)))
        if args.gripper is not None:
            print(_pretty(arm.gripper(args.gripper)))
        if args.go_zero:
            print(_pretty(arm.go_zero()))
        if args.stop:
            print(_pretty(arm.stop()))
        if args.reset:
            print(_pretty(arm.reset()))
        if not any([args.demo, args.state, args.enable, args.disable, args.joints,
                    args.pose, args.gripper is not None, args.go_zero,
                    args.stop, args.reset]):
            ap.print_help()
        return 0
    except PiperError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
