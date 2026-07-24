#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture YOLO training frames from a local webcam.

Examples:
    python camera_stream/scripts/capture_local_camera.py --list

    python camera_stream/scripts/capture_local_camera.py \
        --camera 0 \
        --out D:/Documents/Projects/AGILE/datasets/mahjong_raw/images \
        --count 300 --fps 1
"""

import argparse
import os
import time

import cv2


BACKENDS = {
    "any": cv2.CAP_ANY,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
}


def list_cameras(max_index=8, backend_name="dshow", warmup=10):
    found = []
    backend = BACKENDS.get(backend_name, cv2.CAP_DSHOW)
    for idx in range(max_index):
        cap = cv2.VideoCapture(idx, backend)
        ok, frame = False, None
        for _ in range(max(1, warmup)):
            ok, frame = cap.read()
            if ok and frame is not None:
                pass
        if ok and frame is not None:
            h, w = frame.shape[:2]
            found.append((idx, w, h))
        cap.release()
    return found


def open_camera(index, width=None, height=None, fps=None, backend_name="dshow"):
    backend = BACKENDS.get(backend_name, cv2.CAP_DSHOW)
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        raise RuntimeError("could not open camera index %s with backend %s" %
                           (index, backend_name))
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
    if fps:
        cap.set(cv2.CAP_PROP_FPS, float(fps))
    return cap


def main():
    ap = argparse.ArgumentParser(
        description="Capture local webcam frames for mahjong YOLO training.")
    ap.add_argument("--list", action="store_true", help="list available camera indexes")
    ap.add_argument("--camera", type=int, default=0, help="OpenCV camera index")
    ap.add_argument("--backend", choices=sorted(BACKENDS), default="dshow",
                    help="OpenCV backend (default: dshow)")
    ap.add_argument("--out", default="D:/Documents/Projects/AGILE/datasets/mahjong_raw/images",
                    help="output image directory")
    ap.add_argument("--count", type=int, default=300, help="number of frames to save")
    ap.add_argument("--fps", type=float, default=1.0, help="capture frames per second")
    ap.add_argument("--width", type=int, default=0, help="requested camera width")
    ap.add_argument("--height", type=int, default=0, help="requested camera height")
    ap.add_argument("--prefix", default="mahjong", help="output filename prefix")
    ap.add_argument("--start-index", type=int, default=1,
                    help="first numeric index in saved filenames")
    ap.add_argument("--preview", action="store_true",
                    help="show a preview window while capturing; press q to stop")
    ap.add_argument("--warmup", type=int, default=30,
                    help="frames to discard after opening the camera")
    args = ap.parse_args()

    if args.list:
        found = list_cameras(backend_name=args.backend, warmup=args.warmup)
        if not found:
            print("No cameras found.")
        for idx, w, h in found:
            print("camera %d: %dx%d" % (idx, w, h))
        return

    if args.count <= 0:
        raise SystemExit("--count must be positive")
    if args.fps <= 0:
        raise SystemExit("--fps must be positive")

    os.makedirs(args.out, exist_ok=True)
    cap = open_camera(args.camera, args.width or None, args.height or None, args.fps,
                      backend_name=args.backend)

    for _ in range(max(0, args.warmup)):
        cap.read()

    interval = 1.0 / args.fps
    saved = 0
    next_capture = time.time()
    print("Capturing %d frames from local camera %d at %.2f fps (backend=%s)" %
          (args.count, args.camera, args.fps, args.backend))
    print("Output directory: %s" % os.path.abspath(args.out))

    try:
        while saved < args.count:
            ok, frame = cap.read()
            if not ok or frame is None:
                print("camera read failed; retrying...")
                time.sleep(0.1)
                continue

            now = time.time()
            if now >= next_capture:
                idx = args.start_index + saved
                path = os.path.join(args.out, "%s_%06d.jpg" % (args.prefix, idx))
                cv2.imwrite(path, frame)
                saved += 1
                next_capture = now + interval
                print("[%d/%d] saved %s" % (saved, args.count, path))

            if args.preview:
                cv2.imshow("capture_local_camera", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cap.release()
        if args.preview:
            cv2.destroyAllWindows()

    print("Done. Saved %d/%d frames." % (saved, args.count))


if __name__ == "__main__":
    main()
