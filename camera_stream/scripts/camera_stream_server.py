#!/usr/bin/env python3
# -*-coding:utf8-*-
"""
camera_stream_server -- HTTP live-stream daemon for a camera on the Orange Pi.

RECOMMENDED for a headless Orange Pi 3B: pure Python 3 standard library, no ROS,
no OpenCV. Frames are captured with the system ffmpeg/ffprobe CLI (subprocess)
and rebroadcast to any number of browser clients as MJPEG (multipart/x-mixed-
replace), which every browser renders natively in a plain <img> tag.

Endpoints (default port 8090):
    GET /            embedded HTML viewer (open it in any browser)
    GET /stream      the MJPEG live feed -- point an <img src> or VLC at it
    GET /snapshot    a single JPEG frame
    GET /health      {"ok":true,"alive":true}
    GET /state       capture stats + the last ffprobe stream info (JSON)

Run:
    python3 camera_stream_server.py --device /dev/video0 --port 8090 \
        --width 1280 --height 720 --fps 30 --token SECRET

Requires ffmpeg + ffprobe on PATH (sudo apt install ffmpeg). A background
watchdog restarts the capture pipe if it dies, so the daemon recovers from a
camera hiccup or an unplug/replug without a restart.
"""

import argparse
import json
import logging
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("camera_stream_server")

# Views are case/underscore-insensitive so callers can write "vga" or "VGA".
RESOLUTION_PRESETS = {
    "320x240":   (320, 240),
    "qvga":      (320, 240),
    "640x480":   (640, 480),
    "vga":       (640, 480),
    "800x600":   (800, 600),
    "svga":      (800, 600),
    "1280x720":  (1280, 720),
    "720p":      (1280, 720),
    "hd":        (1280, 720),
    "1920x1080": (1920, 1080),
    "1080p":     (1920, 1080),
    "fhd":       (1920, 1080),
}

BOUNDARY = "frame"  # MJPEG part boundary; body uses --frame\r\n ... \r\n--frame--

VIEWER_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>camera_stream</title>
<style>
  html,body{margin:0;height:100%;background:#0b0e13;color:#e6edf3;
    font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  header{display:flex;gap:1rem;align-items:baseline;padding:.6rem 1rem;
    background:#11161d;border-bottom:1px solid #232a34}
  header h1{font-size:1rem;margin:0;font-weight:600}
  header span{font-size:.8rem;color:#8b98a9}
  main{height:calc(100% - 49px);display:flex;align-items:center;justify-content:center}
  img{max-width:100%;max-height:100%;object-fit:contain;background:#000}
  #msg{position:fixed;left:1rem;bottom:.8rem;font-size:.8rem;color:#8b98a9}
</style>
</head>
<body>
<header><h1>camera_stream</h1><span id="info"></span></header>
<main><img id="v" src="__STREAM__" alt="live stream"></main>
<div id="msg">connecting...</div>
<script>
var img = document.getElementById('v'), msg = document.getElementById('msg');
function ok(on){ msg.textContent = on ? 'live' : 'reconnecting...'; }
img.onload  = function(){ ok(true); };
img.onerror = function(){ ok(false); setTimeout(function(){
  img.src = '__STREAM__?t=' + Date.now();   // bust cache and re-subscribe
}, 1000); };
fetch('__STATE__').then(function(r){return r.json();}).then(function(s){
  if (s && s.stream) document.getElementById('info').textContent =
    s.device + '  ' + (s.requested_width||'?') + 'x' + (s.requested_height||'?') +
    ' @ ' + (s.requested_fps||'?') + 'fps';
}).catch(function(){});
</script>
</body>
</html>
"""


def resolve_resolution(view=None, width=None, height=None):
    """Resolve (width, height) from a named preset or explicit width/height.

    `view` wins when given (e.g. "vga", "720p"); otherwise explicit
    width/height; otherwise the caller's defaults are simply passed in as
    width/height. Raises ValueError on an unknown preset or bad dimensions.
    """
    if view:
        key = str(view).strip().lower()
        if key not in RESOLUTION_PRESETS:
            raise ValueError("unknown view %r -- choose from: %s" % (
                view, ", ".join(sorted(RESOLUTION_PRESETS))))
        return RESOLUTION_PRESETS[key]
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        raise ValueError("width/height must be integers (got %r x %r)"
                         % (width, height))
    if not (16 <= w <= 7680 and 16 <= h <= 4320):
        raise ValueError("resolution %dx%d out of range" % (w, h))
    return (w, h)


def probe_device(device, timeout=8.0):
    """Return ffprobe's parsed JSON for `device` (dict) or None on failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", device],
            capture_output=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return None


def first_video_stream(probe):
    """Pull the first video stream dict out of an ffprobe result (or {})."""
    if not isinstance(probe, dict):
        return {}
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return {}


class CameraBackend(object):
    """Owns the ffmpeg capture pipe and fans the latest JPEG out to clients.

    Capture runs in a background thread that shells out to ffmpeg, which emits
    a continuous MJPEG on stdout (`-f mpjpeg`). Each time ffmpeg prints the
    boundary line we know the previous frame is complete, so we publish the
    buffered bytes as one JPEG. A Condition wakes every subscribed stream
    handler, so N browsers all read the same frames without N cameras.
    """

    def __init__(self, device="/dev/video0", width=1280, height=720, fps=30,
                 quality=5, input_format="auto", extra_input=None):
        self.device = device
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        # mjpeg -q:v: 2 (best) .. 31 (worst); clamp to a sane band.
        self.quality = max(2, min(31, int(quality)))
        self.input_format = (input_format or "auto").lower()  # auto|mjpeg|yuyv
        self.extra_input = list(extra_input or [])

        self._cond = threading.Condition()
        self._frame = None            # newest complete JPEG (bytes)
        self._seq = 0                 # increments once per published frame
        self._lock = threading.Lock()
        self._proc = None
        self._stop = False
        self.start_ts = time.time()
        self.last_frame_ts = None     # set on the first published frame
        self.frame_count = 0
        self.restarts = 0
        self.last_error = None
        self.probe = None             # ffprobe info, filled on first frame
        self._thread = None

    # ------------------------------------------------------------------ cmd
    def _build_cmd(self):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "v4l2"]
        if self.input_format in ("mjpeg", "yuyv", "yuyv422"):
            cmd += ["-input_format", "mjpeg" if self.input_format == "mjpeg"
                    else "yuyv422"]
        cmd += ["-video_size", "%dx%d" % (self.width, self.height),
                "-framerate", str(self.fps)]
        cmd += self.extra_input
        cmd += ["-i", self.device,
                "-an",                       # no audio
                "-c:v", "mjpeg",
                "-q:v", str(self.quality),
                "-f", "mpjpeg", "-"]         # MJPEG to stdout
        return cmd

    # ------------------------------------------------------------- lifecycle
    def start(self):
        """Start the capture thread (idempotent)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        """Stop capturing and kill any running ffmpeg (idempotent)."""
        self._stop = True
        self._kill_proc()
        with self._cond:
            self._cond.notify_all()      # wake stream handlers so they can exit

    def _kill_proc(self):
        with self._lock:
            p = self._proc
            self._proc = None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    # -------------------------------------------------------------- capture
    def _run(self):
        backoff = 1.0
        while not self._stop:
            cmd = self._build_cmd()
            try:
                log.info("starting capture: %s", " ".join(cmd))
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                with self._lock:
                    self._proc = proc
                self._read_frames(proc)
                # clean exit or EOF -> capture ended
                if self._stop:
                    break
                self._drain_stderr(proc)
            except FileNotFoundError:
                self.last_error = "ffmpeg not found on PATH (sudo apt install ffmpeg)"
                log.error(self.last_error)
                break                              # no point retrying
            except Exception as e:
                self.last_error = str(e)
                log.error("capture error: %s", e)

            if self._stop:
                break
            self.restarts += 1
            log.warning("capture lost; restarting in %.1fs", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)     # 1s -> 2 -> 4 ... cap 30s

    def _read_frames(self, proc):
        """Read ffmpeg's mpjpeg stdout and publish each complete JPEG."""
        buf = bytearray()
        out = proc.stdout
        while not self._stop:
            chunk = out.read(65536)
            if not chunk:
                break                              # ffmpeg exited / pipe closed
            buf += chunk
            # ffmpeg (mpjpeg) writes:  --frame\r\n headers \r\n\r\n <jpeg> \r\n
            # Splitting on the boundary marker gives us whole frames.
            while True:
                idx = buf.find(b"\r\n--" + BOUNDARY.encode())
                if idx < 0:
                    # keep the tail (could be a partial boundary) and read more
                    if len(buf) > 4 * 1024 * 1024:   # runaway guard
                        del buf[:-16]
                    break
                frame_blob = bytes(buf[:idx])
                del buf[:idx]
                jpeg = self._extract_jpeg(frame_blob)
                if jpeg:
                    self._publish(jpeg)

    @staticmethod
    def _extract_jpeg(blob):
        """Return the JPEG inside one mpjpeg part (strip headers), or None."""
        if not blob:
            return None
        s = blob.find(b"\xff\xd8")      # SOI
        e = blob.rfind(b"\xff\xd9")     # EOI
        if s < 0 or e < 0 or e <= s:
            return None
        return blob[s:e + 2]

    def _publish(self, jpeg):
        with self._cond:
            self._frame = jpeg
            self._seq += 1
            self.frame_count += 1
            if self.last_frame_ts is None:
                self.last_frame_ts = time.time()
                # First frame is a good moment to record what the camera is.
                self.probe = probe_device(self.device)
            self._cond.notify_all()

    def _drain_stderr(self, proc):
        try:
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            if err:
                self.last_error = err.splitlines()[-1]
                log.warning("ffmpeg: %s", self.last_error)
        except Exception:
            pass

    # -------------------------------------------------------------- clients
    def frames(self, last_seq):
        """Generator yielding (seq, jpeg) for each frame after `last_seq`.

        A streaming handler calls this in a loop; it blocks on the Condition
        until a newer frame is published, then yields it. Cheap for many
        clients because they all share the one captured frame.
        """
        while True:
            with self._cond:
                if self._seq == last_seq and not self._stop:
                    self._cond.wait(timeout=5.0)
                if self._stop:
                    return
                if self._seq == last_seq:
                    continue           # spurious wakeup / timeout, no new frame
                last_seq = self._seq
                yield last_seq, self._frame

    def snapshot(self):
        with self._cond:
            return self._frame

    def state(self):
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
        stream = first_video_stream(self.probe)
        return {
            "device": self.device,
            "requested_width": self.width,
            "requested_height": self.height,
            "requested_fps": self.fps,
            "quality": self.quality,
            "running": running,
            "frames": self.frame_count,
            "restarts": self.restarts,
            "uptime_s": round(time.time() - self.start_ts, 1),
            "last_error": self.last_error,
            "stream": {
                "codec": stream.get("codec_name"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "pix_fmt": stream.get("pix_fmt"),
                "avg_frame_rate": stream.get("avg_frame_rate"),
            } if stream else None,
            "stamp": time.time(),
        }


def make_http_handler(backend, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CameraStream/1.0"
        protocol_version = "HTTP/1.1"

        # -- helpers -----------------------------------------------------
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

        def _send_bytes(self, code, data, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            pass  # keep quiet

        # -- GET ---------------------------------------------------------
        def do_GET(self):
            if not self._authorized():
                return self._send_json(401, {"ok": False, "error": "unauthorized"})
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/api/health"):
                return self._send_json(200, {"ok": True, "alive": True})
            if path in ("/state", "/api/state"):
                return self._send_json(200, backend.state())
            if path in ("/snapshot", "/snapshot.jpg", "/api/snapshot"):
                return self._snapshot()
            if path in ("/stream", "/mjpeg", "/api/stream", "/stream.mjpeg"):
                return self._stream()
            if path in ("/", "/index.html", "/viewer"):
                return self._viewer()
            return self._send_json(404, {"ok": False, "error": "not found"})

        # -- handlers ----------------------------------------------------
        def _viewer(self):
            stream = self._auth_query("/stream")
            state = self._auth_query("/state")
            html = (VIEWER_HTML
                    .replace("__STREAM__", stream)
                    .replace("__STATE__", state))
            self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")

        def _auth_query(self, base):
            """Append ?token= so the viewer's <img>/fetch carry auth if used."""
            return base + ("?token=%s" % token if token else "")

        def _snapshot(self):
            frame = backend.snapshot()
            if frame is None:
                return self._send_json(503, {"ok": False,
                                             "error": "no frame yet"})
            self._send_bytes(200, frame, "image/jpeg")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=%s" % BOUNDARY)
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            close = getattr(self, "close_connection", True)
            try:
                for _seq, jpeg in backend.frames(-1):
                    hdr = (b"--" + BOUNDARY.encode() + b"\r\n"
                           b"Content-Type: image/jpeg\r\n"
                           b"Content-Length: " + str(len(jpeg)).encode() +
                           b"\r\n\r\n")
                    self.wfile.write(hdr)
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass                            # client closed the tab
            except Exception:
                pass
            finally:
                self.close_connection = close

    return Handler


def serve(backend, host="0.0.0.0", port=8090, token="", log=log):
    """Start the streaming HTTP server. Returns a shutdown() callable."""
    httpd = ThreadingHTTPServer((host, port), make_http_handler(backend, token))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("MJPEG stream on http://%s:%d/stream  (viewer: http://%s:%d/)",
             host, port, host, port)
    if not token:
        log.info("no auth token -- anyone on the network can view the camera")

    def shutdown():
        httpd.shutdown()
        httpd.server_close()

    return shutdown


def main():
    ap = argparse.ArgumentParser(
        description="HTTP MJPEG live-stream daemon for a camera on the Orange Pi")
    ap.add_argument("--device", default="/dev/video0",
                    help="V4L2 device (default /dev/video0)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090, help="HTTP port (default 8090)")
    ap.add_argument("--view", default=None,
                    help="resolution preset (qvga/vga/720p/1080p) -- overrides w/h")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--quality", type=int, default=5,
                    help="JPEG quality 2 (best) .. 31 (worst); default 5")
    ap.add_argument("--input-format", default="auto",
                    help="v4l2 input format: auto|mjpeg|yuyv (default auto)")
    ap.add_argument("--token", default="", help="bearer token (recommended)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="[%(levelname)s] %(message)s")

    try:
        w, h = resolve_resolution(args.view, args.width, args.height)
    except ValueError as e:
        log.error("%s", e)
        raise SystemExit(2)

    backend = CameraBackend(device=args.device, width=w, height=h, fps=args.fps,
                            quality=args.quality, input_format=args.input_format)
    backend.start()

    # Give the capture a moment; warn (don't die) if nothing arrives yet --
    # the watchdog keeps retrying in the background, so stay up regardless.
    deadline = time.time() + 5.0
    while time.time() < deadline and backend.snapshot() is None \
            and backend.last_error is None:
        time.sleep(0.1)
    if backend.snapshot() is None:
        log.warning("no frame yet from %s -- the stream will come up when the "
                    "camera does (auto-retrying). Check: v4l2-ctl --list-devices",
                    args.device)
    else:
        log.info("capturing %s -> %dx%d @ %dfps", args.device, w, h, args.fps)

    shutdown = serve(backend, host=args.host, port=args.port, token=args.token)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        shutdown()
        backend.stop()


if __name__ == "__main__":
    main()
