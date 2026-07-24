#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
piper_sdk_server -- direct SDK HTTP/TCP control server for the Piper 6-axis arm.

RECOMMENDED for a headless Orange Pi 3B: no ROS required. It talks to the arm
straight through piper_sdk (python-can + SocketCAN) and exposes the exact same
HTTP/JSON and TCP/JSON contract as the ROS bridge (see server_common.py).

Run:
    python3 piper_sdk_server.py --can can0 --http-port 8080 --tcp-port 9090 \
        --token SECRET --speed 30

Client units: joints=deg, x/y/z=mm, roll/pitch/yaw=deg, gripper=mm.
"""

import argparse
import json
import logging
import threading
import time

import server_common as sc

try:
    from piper_sdk import C_PiperInterface_V2 as _Piper
except Exception:  # very old SDKs only expose C_PiperInterface
    try:
        from piper_sdk import C_PiperInterface as _Piper
    except Exception as e:
        _Piper = None
        _IMPORT_ERR = e

log = logging.getLogger("piper_sdk_server")


class PiperSDKBackend(object):
    """Backend that drives the arm via piper_sdk directly."""

    def __init__(self, can_port="can0", default_speed=50, auto_enable=False):
        if _Piper is None:
            raise RuntimeError("piper_sdk import failed: %s" % _IMPORT_ERR)
        self._lock = threading.Lock()
        self._default_speed = sc.clamp_speed(default_speed)
        self._enabled = False
        self.piper = _Piper(can_name=can_port)
        self.piper.ConnectPort()
        # put the arm in CAN position/velocity control mode
        try:
            self.piper.MotionCtrl_2(0x01, 0x01, self._default_speed, 0x00)
        except AttributeError:
            self.piper.ModeCtrl(0x01, 0x01, self._default_speed, 0x00)
        if auto_enable:
            self.cmd_enable(True)

    # ---- helpers -------------------------------------------------------
    def _set_speed(self, speed):
        speed = sc.clamp_speed(speed)
        try:
            self.piper.MotionCtrl_2(0x01, 0x01, speed, 0x00)
        except AttributeError:
            self.piper.ModeCtrl(0x01, 0x01, speed, 0x00)

    # ---- state ---------------------------------------------------------
    def get_state(self):
        p = self.piper
        joints_deg, gripper_mm, end_pose, arm_status = None, None, None, None
        try:
            js = p.GetArmJointMsgs().joint_state
            joints_deg = [round(v / 1000.0, 3) for v in (
                js.joint_1, js.joint_2, js.joint_3,
                js.joint_4, js.joint_5, js.joint_6)]
        except Exception:
            pass
        try:
            gripper_mm = round(p.GetArmGripperMsgs().gripper_state.grippers_angle
                               / 1000.0, 3)
        except Exception:
            pass
        try:
            ep = p.GetArmEndPoseMsgs().end_pose
            end_pose = {
                "x": round(ep.X_axis / 1000.0, 3),
                "y": round(ep.Y_axis / 1000.0, 3),
                "z": round(ep.Z_axis / 1000.0, 3),
                "roll": round(ep.RX_axis / 1000.0, 3),
                "pitch": round(ep.RY_axis / 1000.0, 3),
                "yaw": round(ep.RZ_axis / 1000.0, 3),
            }
        except Exception:
            pass
        try:
            st = p.GetArmStatus().arm_status
            arm_status = {
                "ctrl_mode": st.ctrl_mode,
                "arm_status": st.arm_status,
                "mode_feedback": st.mode_feed,
                "teach_status": st.teach_status,
                "motion_status": st.motion_status,
                "err_code": st.err_code,
            }
        except Exception:
            pass
        return {
            "joints_deg": joints_deg,
            "gripper_mm": gripper_mm,
            "end_pose": end_pose,
            "arm_status": arm_status,
            "enabled": self._enabled,
            "stamp": time.time(),
        }

    # ---- commands ------------------------------------------------------
    def cmd_enable(self, enable):
        enable = bool(enable)
        with self._lock:
            if enable:
                self.piper.EnableArm(7)
                try:
                    self.piper.GripperCtrl(0, 1000, 0x01, 0)
                except Exception:
                    pass
            else:
                self.piper.DisableArm(7)
            self._enabled = enable
        return {"ok": True, "via": "sdk", "enabled": enable}

    def cmd_joint_ctrl(self, joints, speed=None, gripper_mm=None):
        joints = sc.validate_joints(joints)
        self._set_speed(speed if speed is not None else self._default_speed)
        args = [int(round(j * 1000.0)) for j in joints]  # deg -> 0.001 deg
        with self._lock:
            self.piper.JointCtrl(*args)
            if gripper_mm is not None:
                self.cmd_gripper(gripper_mm)
        return {"ok": True, "joints_deg": joints,
                "speed": sc.clamp_speed(speed if speed is not None
                                        else self._default_speed)}

    def cmd_pose_ctrl(self, x, y, z, roll, pitch, yaw, gripper_mm=None):
        # mm -> 0.001 mm ; deg -> 0.001 deg
        X, Y, Z = (int(round(float(v) * 1000.0)) for v in (x, y, z))
        RX, RY, RZ = (int(round(float(v) * 1000.0)) for v in (roll, pitch, yaw))
        with self._lock:
            self.piper.EndPoseCtrl(X, Y, Z, RX, RY, RZ)
            if gripper_mm is not None:
                self.cmd_gripper(gripper_mm)
        return {"ok": True, "pose_mm_deg": [x, y, z, roll, pitch, yaw]}

    def cmd_gripper(self, position_mm, effort=1000):
        # mm -> 0.001 mm ; effort 0..5000 (0.001 N/m)
        angle = int(round(float(position_mm) * 1000.0))
        effort = int(max(0, min(5000, int(effort))))
        with self._lock:
            self.piper.GripperCtrl(angle, effort, 0x01, 0)
        return {"ok": True, "gripper_mm": position_mm, "effort": effort}

    def cmd_stop(self):
        with self._lock:
            self.piper.EmergencyStop(0x01)
        return {"ok": True, "action": "stop"}

    def cmd_reset(self):
        with self._lock:
            try:
                self.piper.ResetPiper()
            except AttributeError:
                self.piper.EmergencyStop(0x00)
        return {"ok": True, "action": "reset"}

    def cmd_go_zero(self, is_mit_mode=False):
        # drive all joints to zero through JointCtrl
        return self.cmd_joint_ctrl([0.0] * 6, self._default_speed)

    def cmd_block_arm(self, block):
        # SDK has no block-arm equivalent; approximate with e-stop hold.
        with self._lock:
            self.piper.EmergencyStop(0x01 if block else 0x02)
        return {"ok": True, "action": "block_arm", "block": bool(block)}


def main():
    ap = argparse.ArgumentParser(description="Piper SDK HTTP/TCP control server")
    ap.add_argument("--can", default="can0", help="CAN interface (default can0)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--tcp-port", type=int, default=9090)
    ap.add_argument("--no-tcp", action="store_true")
    ap.add_argument("--token", default="", help="bearer token (recommended)")
    ap.add_argument("--speed", type=int, default=50, help="default speed %% 1-100")
    ap.add_argument("--auto-enable", action="store_true",
                    help="enable the arm on startup")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="[%(levelname)s] %(message)s")

    backend = PiperSDKBackend(can_port=args.can,
                              default_speed=args.speed,
                              auto_enable=args.auto_enable)
    log.info("connected to arm on %s", args.can)

    shutdown = sc.serve(backend, host=args.host,
                        http_port=args.http_port,
                        tcp_port=args.tcp_port,
                        token=args.token,
                        enable_tcp=not args.no_tcp,
                        log=lambda m: log.info(m))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        shutdown()
        try:
            backend.piper.DisconnectPort()
        except Exception:
            pass


if __name__ == "__main__":
    main()
