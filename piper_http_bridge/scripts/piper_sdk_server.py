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
import math
import subprocess
import sys
import threading
import time

import server_common as sc


def _can_is_up(iface):
    """True if the SocketCAN interface exists and is UP."""
    try:
        with open("/sys/class/net/%s/operstate" % iface) as f:
            return f.read().strip() == "up"
    except OSError:
        return False


def _can_exists(iface):
    try:
        with open("/sys/class/net/%s/operstate" % iface):
            return True
    except OSError:
        return False


def bring_can_up(iface, bitrate=1000000, log=logging.getLogger("piper_sdk_server")):
    """Bring the SocketCAN interface up at `bitrate` if it isn't already.

    Returns True if the interface is UP afterwards. Tries without sudo first,
    then with sudo -n (non-interactive). Safe to call repeatedly.
    """
    if not _can_exists(iface):
        log.warning("CAN interface '%s' not found in /sys/class/net -- is the "
                    "USB-CAN adapter plugged in?", iface)
        return False
    if _can_is_up(iface):
        log.info("CAN interface %s already UP", iface)
        return True
    cmds = [
        ["ip", "link", "set", iface, "down"],
        ["ip", "link", "set", iface, "up", "type", "can", "bitrate", str(bitrate)],
    ]
    for use_sudo in (False, True):
        try:
            for c in cmds:
                cmd = (["sudo", "-n"] + c) if use_sudo else c
                subprocess.run(cmd, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        if _can_is_up(iface):
            log.info("brought CAN interface %s UP @ %d bps%s", iface, bitrate,
                     " (via sudo)" if use_sudo else "")
            return True
    log.error("could not bring CAN interface %s UP (need root/CAP_NET_ADMIN). "
              "Run: sudo ip link set %s up type can bitrate %d", iface, iface, bitrate)
    return False


def _can_bus_off(iface):
    """True if the CAN controller is in BUS-OFF state (symptom of the gs_usb
    stuck-TX bug: RX keeps counting but no frame can be sent)."""
    try:
        out = subprocess.run(["ip", "-details", "link", "show", iface],
                             capture_output=True, text=True, timeout=5).stdout
        return "BUS-OFF" in out
    except Exception:
        return False


def recover_can(iface, bitrate=1000000, log=logging.getLogger("piper_sdk_server")):
    """Reset a stuck/BUS-OFF CAN interface (down -> set bitrate+restart -> up).

    This is the software equivalent of unplugging/replugging the USB-CAN
    adapter to clear the gs_usb stuck-transmit condition.
    """
    log.warning("recovering CAN interface %s (BUS-OFF / stuck TX) ...", iface)
    cmds = [
        ["ip", "link", "set", iface, "down"],
        ["ip", "link", "set", iface, "type", "can", "bitrate", str(bitrate),
         "restart-ms", "100"],
        ["ip", "link", "set", iface, "up"],
    ]
    for use_sudo in (False, True):
        try:
            for c in cmds:
                cmd = (["sudo", "-n"] + c) if use_sudo else c
                subprocess.run(cmd, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        if _can_is_up(iface) and not _can_bus_off(iface):
            log.info("CAN interface %s recovered (ERROR-ACTIVE)", iface)
            return True
    log.error("CAN interface %s recovery failed", iface)
    return False


def can_watchdog(backend_holder, iface, bitrate=1000000, interval=2.0,
                 log=logging.getLogger("piper_sdk_server")):
    """Background thread: watch for BUS-OFF and auto-recover + reconnect.

    `backend_holder` is a dict-like with an optional 'backend' key holding the
    PiperSDKBackend; after a recovery we reconnect the SDK so the arm keeps
    listening on the freshly-reset bus.
    """
    while True:
        time.sleep(interval)
        try:
            if _can_exists(iface) and _can_is_up(iface) and _can_bus_off(iface):
                log.warning("watchdog: %s BUS-OFF detected", iface)
                if recover_can(iface, bitrate, log):
                    b = backend_holder.get("backend")
                    if b is not None:
                        try:
                            b.reconnect()
                            log.warning("watchdog: SDK reconnected on %s", iface)
                        except Exception as e:
                            log.error("watchdog: SDK reconnect failed: %s", e)
        except Exception as e:
            log.error("watchdog error: %s", e)


log = logging.getLogger("piper_sdk_server")

try:
    from piper_sdk import C_PiperInterface_V2 as _Piper
except Exception as _e1:  # very old SDKs only expose C_PiperInterface
    try:
        from piper_sdk import C_PiperInterface as _Piper
    except Exception as e:
        _Piper = None
        _IMPORT_ERR = e
        print("=" * 60, file=sys.stderr)
        print("FATAL: could not import piper_sdk.", file=sys.stderr)
        print("  import error: %s" % e, file=sys.stderr)
        print("Install it for the SAME python3 that runs this service:", file=sys.stderr)
        print("  sudo python3 -m pip install --upgrade python-can piper_sdk", file=sys.stderr)
        print("=" * 60, file=sys.stderr)


class PiperSDKBackend(object):
    """Backend that drives the arm via piper_sdk directly."""

    def __init__(self, can_port="can0", default_speed=50, auto_enable=False):
        if _Piper is None:
            raise RuntimeError("piper_sdk import failed: %s" % _IMPORT_ERR)
        self._lock = threading.Lock()
        self.can_port = can_port
        self._default_speed = sc.clamp_speed(default_speed)
        self._enabled = False
        self._move_mode = None      # last move mode we commanded (avoid re-sends)
        self._connect()
        if auto_enable:
            self.enable_and_wait()

    def _connect(self):
        self.piper = _Piper(can_name=self.can_port)
        self.piper.ConnectPort()
        # put the arm in CAN position/velocity control mode
        try:
            self.piper.MotionCtrl_2(0x01, 0x01, self._default_speed, 0x00)
        except AttributeError:
            self.piper.ModeCtrl(0x01, 0x01, self._default_speed, 0x00)

    def reconnect(self):
        """Tear down and re-establish the SDK connection (after a CAN reset)."""
        with self._lock:
            try:
                self.piper.DisconnectPort()
            except Exception:
                pass
            self._connect()
            self._enabled = False
        # re-enable the arm on the fresh bus
        self.enable_and_wait()

    # ---- helpers -------------------------------------------------------
    # move-mode feedback codes (from the arm): 0x00 MOVE P, 0x01 MOVE J
    MOVE_P, MOVE_J = 0x00, 0x01

    def _set_speed(self, speed):
        speed = sc.clamp_speed(speed)
        try:
            self.piper.MotionCtrl_2(0x01, 0x01, speed, 0x00)
        except AttributeError:
            self.piper.ModeCtrl(0x01, 0x01, speed, 0x00)

    def _set_move_mode(self, move_mode, speed=None):
        """Switch ctrl to CAN mode + given move_mode (MOVE_J / MOVE_P).

        Skips the MotionCtrl_2 frame when the mode is unchanged and no new
        speed is requested -- re-sending it on every jog command causes a
        visible stutter while streaming.
        """
        spd = None if speed is None else sc.clamp_speed(speed)
        if spd is None and self._move_mode == move_mode:
            return
        eff = spd if spd is not None else self._default_speed
        try:
            self.piper.MotionCtrl_2(0x01, move_mode, eff, 0x00)
        except AttributeError:
            self.piper.ModeCtrl(0x01, move_mode, eff, 0x00)
        self._move_mode = move_mode

    def get_move_mode(self):
        try:
            return self.piper.GetArmStatus().arm_status.mode_feed
        except Exception:
            return None

    def cmd_set_mode(self, mode):
        """Explicitly set move mode: 'j'/'joint'/'move_j' or 'p'/'pose'/'move_p'."""
        m = str(mode).strip().lower()
        if m in ("j", "joint", "move_j", "0x01", "1"):
            self._set_move_mode(self.MOVE_J)
            return {"ok": True, "mode": "move_j"}
        if m in ("p", "pose", "move_p", "0x00", "0"):
            self._set_move_mode(self.MOVE_P)
            return {"ok": True, "mode": "move_p"}
        raise ValueError("mode must be 'joint' (move_j) or 'pose' (move_p)")

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
            "move_mode": (arm_status or {}).get("mode_feedback"),
            "enabled": self._enabled,
            "stamp": time.time(),
        }

    # ---- enable --------------------------------------------------------
    def _drivers_enabled(self):
        try:
            info = self.piper.GetArmLowSpdInfoMsgs()
            motors = (info.motor_1, info.motor_2, info.motor_3,
                      info.motor_4, info.motor_5, info.motor_6)
            return all(getattr(m.foc_status, "driver_enable_status", False)
                       for m in motors)
        except Exception:
            return False

    def enable_and_wait(self, timeout=8.0, poll=0.2):
        """Send EnableArm until the drivers report enabled (or timeout)."""
        self.piper.EnableArm(7)
        try:
            self.piper.GripperCtrl(0, 1000, 0x01, 0)
        except Exception:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._drivers_enabled():
                self._enabled = True
                log.info("arm ENABLED (drivers report enabled)")
                return True
            time.sleep(poll)
            try:
                self.piper.EnableArm(7)
            except Exception:
                pass
        self._enabled = True  # proceed anyway; commands may still work
        log.warning("enable not confirmed within %.1fs; continuing anyway", timeout)
        return False

    # ---- commands ------------------------------------------------------
    def cmd_enable(self, enable):
        enable = bool(enable)
        if enable:
            # interactive call: short wait so the HTTP/TCP request stays snappy
            self.enable_and_wait(timeout=1.5)
        else:
            with self._lock:
                self.piper.DisableArm(7)
            self._enabled = False
        return {"ok": True, "via": "sdk", "enabled": enable}

    def cmd_joint_ctrl(self, joints, speed=None, gripper_mm=None):
        joints = sc.validate_joints(joints)
        spd = sc.clamp_speed(speed if speed is not None else self._default_speed)
        args = [int(round(j * 1000.0)) for j in joints]  # deg -> 0.001 deg
        with self._lock:
            self._set_move_mode(self.MOVE_J, spd)   # joint control needs MOVE J
            self.piper.JointCtrl(*args)
            if gripper_mm is not None:
                self.cmd_gripper(gripper_mm)
        return {"ok": True, "joints_deg": joints, "speed": spd}

    def cmd_pose_ctrl(self, x, y, z, roll, pitch, yaw, gripper_mm=None):
        # mm -> 0.001 mm ; deg -> 0.001 deg
        X, Y, Z = (int(round(float(v) * 1000.0)) for v in (x, y, z))
        RX, RY, RZ = (int(round(float(v) * 1000.0)) for v in (roll, pitch, yaw))
        with self._lock:
            self._set_move_mode(self.MOVE_P)        # end-pose control needs MOVE P
            self.piper.EndPoseCtrl(X, Y, Z, RX, RY, RZ)
            if gripper_mm is not None:
                self.cmd_gripper(gripper_mm)
        return {"ok": True, "pose_mm_deg": [x, y, z, roll, pitch, yaw]}

    def _read_joints_deg(self):
        js = self.piper.GetArmJointMsgs().joint_state
        return [v / 1000.0 for v in (js.joint_1, js.joint_2, js.joint_3,
                                     js.joint_4, js.joint_5, js.joint_6)]

    def cmd_gripper(self, position_mm, effort=1000):
        # mm -> 0.001 mm ; effort 0..5000 (0.001 N/m); travel clamped to range
        position_mm = sc.clamp_gripper(position_mm)
        angle = int(round(position_mm * 1000.0))
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
    ap.add_argument("--bitrate", type=int, default=1000000, help="CAN bitrate")
    ap.add_argument("--no-can-init", action="store_true",
                    help="do not auto-bring-up the CAN interface")
    ap.add_argument("--no-watchdog", action="store_true",
                    help="disable the BUS-OFF auto-recovery watchdog")
    ap.add_argument("--watchdog-interval", type=float, default=2.0,
                    help="watchdog poll interval in seconds (default 2.0)")
    ap.add_argument("--auto-enable", dest="auto_enable", action="store_true",
                    default=True, help="enable the arm on startup (default)")
    ap.add_argument("--no-auto-enable", dest="auto_enable", action="store_false",
                    help="do not auto-enable the arm on startup")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="[%(levelname)s] %(message)s")

    # 1. bring the CAN interface up ourselves (no manual step needed).
    if not args.no_can_init:
        bring_can_up(args.can, args.bitrate, log)

    # 2. connect to the arm.
    try:
        backend = PiperSDKBackend(can_port=args.can,
                                  default_speed=args.speed,
                                  auto_enable=False)
    except Exception as e:
        log.error("failed to connect to the arm on '%s': %s", args.can, e)
        log.error("checklist:")
        log.error("  1) USB-CAN adapter plugged in?            ->  ip link show type can")
        log.error("  2) CAN interface up at 1 Mbps?            ->  sudo ip link set %s up type can bitrate 1000000", args.can)
        log.error("  3) arm powered on, green CAN plug seated?")
        log.error("  4) firmware >= V1.5-2 (needed by C_PiperInterface_V2)?")
        sys.exit(2)
    log.info("connected to arm on %s", args.can)

    # 3. enable the arm straight away (default), so it's ready to move.
    if args.auto_enable:
        backend.enable_and_wait()

    # 4. start the BUS-OFF watchdog (auto-recover from the gs_usb stuck-TX bug).
    if not args.no_watchdog:
        holder = {"backend": backend}
        wd = threading.Thread(target=can_watchdog,
                              args=(holder, args.can, args.bitrate,
                                    args.watchdog_interval, log),
                              daemon=True)
        wd.start()
        log.info("CAN watchdog running (interval %.1fs)", args.watchdog_interval)

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
