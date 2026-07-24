#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
Dry-run test for camera_stream_server.py -- no camera, no v4l2, no real ffmpeg.

A fake subprocess.Popen pipes canned mpjpeg bytes into the capture thread, and
ffprobe is stubbed, so we can exercise frame splitting, fan-out broadcast,
snapshot, the embedded viewer and every HTTP endpoint on any machine
(even Windows). A fake ffmpeg binary on PATH lets us also drive main().

Run:  python test/dry_run_test.py     ->  exit 0 on success
"""

import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")

PASSED, FAILED = [], []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name)


def load_server():
    spec = importlib.util.spec_from_file_location(
        "camera_stream_server", os.path.join(SCRIPTS, "camera_stream_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fake ffmpeg subprocess: feeds canned mpjpeg bytes, then stays alive.
# ---------------------------------------------------------------------------
def make_jpeg(tag):
    # A minimal blob bracketed by real JPEG SOI/EOI markers.
    return b"\xff\xd8" + tag + b"\xff\xd9"


def mpjpeg_stream(tags):
    """Build ffmpeg's mpjpeg stdout for a sequence of frames."""
    out = b""
    for t in tags:
        out += (b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(t)).encode() + b"\r\n\r\n"
                + t + b"\r\n")
    return out


class FakePopen(object):
    instances = []

    def __init__(self, cmd, stdout=None, stderr=None, **kw):
        self.cmd = cmd
        self.killed = False
        FakePopen.instances.append(self)
        # First spawn yields 3 frames then stays open (blocks on read).
        payload = mpjpeg_stream([make_jpeg(b"f0"), make_jpeg(b"f1"),
                                 make_jpeg(b"f2")])
        self._buf = io.BytesIO(payload)
        self.stdout = _BlockingReader(self._buf, self)
        self.stderr = io.BytesIO(b"")

    def poll(self):
        return None if not self.killed else 0      # None => still running

    def kill(self):
        self.killed = True


class _BlockingReader(object):
    """Returns canned bytes, then blocks until the proc is killed (so the
    capture thread parks in read() like it would on a real, idle camera)."""

    def __init__(self, buf, proc):
        self._buf = buf
        self._proc = proc

    def read(self, n=-1):
        data = self._buf.read(n)
        if data:
            return data
        while not self._proc.killed:
            time.sleep(0.01)
        return b""


FFPROBE_JSON = json.dumps({
    "streams": [{
        "codec_type": "video", "codec_name": "mjpeg",
        "width": 1280, "height": 720, "pix_fmt": "yuvj422p",
        "avg_frame_rate": "30/1",
    }],
    "format": {"format_name": "video4linux2"},
}).encode()


class FakeCompleted(object):
    def __init__(self, out=b"", rc=0):
        self.stdout = out
        self.returncode = rc


def install_fakes(mod):
    """Patch subprocess.Popen / subprocess.run / probe_device with fakes."""
    orig_popen, orig_run, orig_probe = \
        mod.subprocess.Popen, mod.subprocess.run, mod.probe_device
    mod.subprocess.Popen = FakePopen
    mod.subprocess.run = lambda *a, **k: FakeCompleted(FFPROBE_JSON)
    mod.probe_device = lambda device, timeout=8.0: json.loads(
        FFPROBE_JSON.decode())

    def restore():
        mod.subprocess.Popen, mod.subprocess.run, mod.probe_device = \
            orig_popen, orig_run, orig_probe
    return restore


def wait_frames(backend, n, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and backend.frame_count < n:
        time.sleep(0.01)
    return backend.frame_count >= n


# ---------------------------------------------------------------------------
def test_resolution(mod):
    print("[resolution presets]")
    check("vga -> 640x480", mod.resolve_resolution("vga") == (640, 480))
    check("720p -> 1280x720", mod.resolve_resolution("720p") == (1280, 720))
    check("case-insensitive", mod.resolve_resolution("VGA") == (640, 480))
    check("explicit w/h", mod.resolve_resolution(None, 800, 600) == (800, 600))
    try:
        mod.resolve_resolution("nope")
        check("unknown preset rejected", False)
    except ValueError:
        check("unknown preset rejected", True)
    try:
        mod.resolve_resolution(None, 1, 1)
        check("tiny resolution rejected", False)
    except ValueError:
        check("tiny resolution rejected", True)


def test_jpeg_extract(mod):
    print("[jpeg splitting from a raw stream]")
    restore = install_fakes(mod)
    try:
        b = mod.CameraBackend(device="/dev/video0")
        # Feed the scanner 3 framed jpegs back to back through a finite stream.
        payload = mpjpeg_stream([make_jpeg(b"a"), make_jpeg(b"b"),
                                 make_jpeg(b"c")])
        published = []
        b._publish = lambda j: published.append(j)      # observe publishes
        proc = FakePopen(["ffmpeg"], stdout=True, stderr=True)
        proc.stdout = io.BytesIO(payload)               # finite, no blocking
        b._read_frames(proc)
        check("split 3 frames", len(published) == 3)
        check("frame bytes intact", published[0] == make_jpeg(b"a")
              and published[2] == make_jpeg(b"c"))
    finally:
        restore()


def test_capture_and_broadcast(mod):
    print("[capture + frame fan-out]")
    restore = install_fakes(mod)
    try:
        b = mod.CameraBackend(device="/dev/video0", width=1280, height=720,
                              fps=30)
        b.start()
        try:
            check("frames captured", wait_frames(b, 3))
            snap = b.snapshot()
            check("snapshot is last frame", snap == make_jpeg(b"f2"))
            st = b.state()
            check("state running", st["running"] is True)
            check("state frame count", st["frames"] >= 3)
            check("state probed width", st["stream"]["width"] == 1280)

            # fan-out: a consumer joining now gets the latest frame immediately
            got = []
            gen = b.frames(-1)
            got.append(next(gen))
            check("broadcast yields latest frame", len(got) == 1
                  and got[0][1] == make_jpeg(b"f2") and got[0][0] >= 3)
        finally:
            b.stop()
        check("stop is clean", True)
    finally:
        restore()


def test_h264_commands(mod):
    print("[h264 low-latency command building]")
    restore = install_fakes(mod)
    try:
        # No RTSP target -> no H.264 pipe at all.
        b = mod.CameraBackend(device="/dev/video0")
        check("no rtsp -> no h264 cmd", b._build_h264_cmd() is None)

        # Native camera H.264 -> pure passthrough (copy), no re-encode.
        b = mod.CameraBackend(device="/dev/video0", input_format="h264",
                              rtsp_url="rtsp://127.0.0.1:8554/cam")
        cmd = b._build_h264_cmd()
        check("native h264 passthrough", cmd is not None
              and "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy")
        check("native h264 input_format", "-input_format" in cmd
              and cmd[cmd.index("-input_format") + 1] == "h264")
        check("annexb bitstream filter", "h264_mp4toannexb" in cmd)
        check("rtsp target present", cmd[-1] == "rtsp://127.0.0.1:8554/cam")
        st = b.state()
        check("webrtc state configured", st["webrtc"]["configured"] is True)
        check("webrtc page_url derived",
              st["webrtc"]["page_url"] == "http://127.0.0.1:8889/cam")

        # MJPEG camera, no HW encoder -> software x264 for WebRTC.
        mod._HW_H264 = False
        b = mod.CameraBackend(device="/dev/video0", input_format="mjpeg",
                              fps=30, rtsp_url="rtsp://127.0.0.1:8554/cam")
        cmd = b._build_h264_cmd()
        check("mjpeg transcodes to x264", "libx264" in cmd
              and "zerolatency" in cmd and cmd[cmd.index("-c:v") + 1] == "libx264")

        # MJPEG camera with a VPU available -> prefer the hardware encoder.
        mod._HW_H264 = True
        b = mod.CameraBackend(device="/dev/video0", input_format="mjpeg",
                              fps=30, rtsp_url="rtsp://127.0.0.1:8554/cam")
        cmd = b._build_h264_cmd()
        check("mjpeg prefers hw v4l2m2m", "h264_v4l2m2m" in cmd
              and "libx264" not in cmd)
        mod._HW_H264 = None     # reset cache

        # A user-supplied hardware encoder overrides the software default.
        b = mod.CameraBackend(device="/dev/video0", input_format="mjpeg",
                              h264_args=["-c:v", "h264_v4l2m2m", "-b:v", "4M"],
                              rtsp_url="rtsp://127.0.0.1:8554/cam")
        cmd = b._build_h264_cmd()
        check("hw encoder override", "h264_v4l2m2m" in cmd
              and "libx264" not in cmd)

        # h264 thread starts and stops cleanly alongside the MJPEG pipe.
        b = mod.CameraBackend(device="/dev/video0", input_format="h264",
                              rtsp_url="rtsp://127.0.0.1:8554/cam")
        b.start()
        try:
            check("h264 thread running", b._h264_thread is not None
                  and b._h264_thread.is_alive())
        finally:
            b.stop()
        check("h264 stop is clean", True)
    finally:
        restore()


def _http(port, path, token=""):
    url = "http://127.0.0.1:%d%s" % (port, path)
    headers = {}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode(), r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_http(mod):
    print("[HTTP front-end]")
    restore = install_fakes(mod)
    try:
        b = mod.CameraBackend(device="/dev/video0")
        b.start()
        wait_frames(b, 1)
        httpd = mod.ThreadingHTTPServer(("127.0.0.1", 0),
                                        mod.make_http_handler(b, "tok"))
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            code, _, _ = _http(port, "/health")            # no token
            check("no-token 401", code == 401)

            code, _, body = _http(port, "/health", token="tok")
            check("health 200", code == 200 and json.loads(body)["alive"])

            code, hdrs, body = _http(port, "/snapshot", token="tok")
            check("snapshot jpeg", code == 200
                  and hdrs.get("Content-Type") == "image/jpeg"
                  and body.startswith(b"\xff\xd8"))

            code, _, body = _http(port, "/state", token="tok")
            check("state json", code == 200
                  and json.loads(body)["requested_fps"] == 30)

            code, hdrs, body = _http(port, "/", token="tok")
            check("viewer html", code == 200
                  and b"text/html" in hdrs.get("Content-Type", "").encode()
                  and b"/stream?token=tok" in body)

            code, _, _ = _http(port, "/nope", token="tok")
            check("unknown path 404", code == 404)

            # MJPEG stream: read a bounded chunk via urllib (has a timeout)
            # then close -- proves content-type + boundary + jpeg flow.
            code, ct, body = _read_stream_prefix(port, token="tok")
            check("stream content-type", code == 200
                  and ct.startswith("multipart/x-mixed-replace"))
            check("stream has boundary+jpeg", b"--frame" in body
                  and b"\xff\xd8" in body)
        finally:
            httpd.shutdown()
            httpd.server_close()
            b.stop()
    finally:
        restore()


def _read_stream_prefix(port, token=""):
    """Open /stream and read just enough MJPEG to see one part (boundary +
    headers + jpeg), then close. The server writes a part per frame, so a few
    bounded reads are plenty; we never sit waiting for more frames."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(("127.0.0.1", port))
    req = ("GET /stream HTTP/1.1\r\nHost: x\r\n"
           + ("Authorization: Bearer %s\r\n" % token if token else "")
           + "Connection: close\r\n\r\n")
    s.sendall(req.encode())
    data = b""
    try:
        for _ in range(3):
            data += s.recv(65536)
            if b"--frame" in data and b"\xff\xd8" in data:
                break
    finally:
        s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    code, ct = 0, ""
    lines = head.split(b"\r\n")
    if lines and len(lines[0].split()) >= 2:
        try:
            code = int(lines[0].split()[1])
        except ValueError:
            code = 0
    for ln in lines[1:]:
        if ln.lower().startswith(b"content-type:"):
            ct = ln.split(b":", 1)[1].strip().decode()
    return code, ct, body


def main():
    mod = load_server()
    test_resolution(mod)
    test_jpeg_extract(mod)
    test_capture_and_broadcast(mod)
    test_h264_commands(mod)
    test_http(mod)
    print("\n=================================")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    for f in FAILED:
        print("  FAILED: " + f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
