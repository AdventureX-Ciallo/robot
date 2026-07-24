#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture training frames from camera_stream /snapshot.

Example:
    python camera_stream/scripts/capture_snapshots.py \
        --base-url http://192.168.1.100:8090 \
        --out D:/Documents/Projects/AGILE/datasets/mahjong_raw/images \
        --count 300 --interval 0.5
"""

import argparse
import os
import time
import urllib.error
import urllib.request


def build_snapshot_url(base_url):
    return base_url.rstrip("/") + "/snapshot"


def fetch_snapshot(url, token="", timeout=5.0):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content_type = resp.headers.get("Content-Type", "")
        data = resp.read()
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("response is not a JPEG frame (Content-Type=%r)" % content_type)
    return data


def main():
    ap = argparse.ArgumentParser(
        description="Capture JPEG frames from camera_stream /snapshot for YOLO training.")
    ap.add_argument("--base-url", required=True,
                    help="camera_stream base URL, e.g. http://192.168.1.100:8090")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--count", type=int, default=300, help="number of frames to save")
    ap.add_argument("--interval", type=float, default=0.5,
                    help="seconds between frames")
    ap.add_argument("--token", default="", help="Bearer token if camera_stream uses auth")
    ap.add_argument("--prefix", default="mahjong", help="output filename prefix")
    ap.add_argument("--start-index", type=int, default=1,
                    help="first numeric index in saved filenames")
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="HTTP timeout per snapshot request")
    args = ap.parse_args()

    if args.count <= 0:
        raise SystemExit("--count must be positive")
    if args.interval < 0:
        raise SystemExit("--interval cannot be negative")

    os.makedirs(args.out, exist_ok=True)
    snapshot_url = build_snapshot_url(args.base_url)
    print("Capturing %d frames from %s" % (args.count, snapshot_url))
    print("Output directory: %s" % os.path.abspath(args.out))

    saved = 0
    for i in range(args.start_index, args.start_index + args.count):
        name = "%s_%06d.jpg" % (args.prefix, i)
        path = os.path.join(args.out, name)
        try:
            data = fetch_snapshot(snapshot_url, token=args.token, timeout=args.timeout)
            with open(path, "wb") as f:
                f.write(data)
            saved += 1
            print("[%d/%d] saved %s" % (saved, args.count, path))
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print("[%d/%d] failed: %s" % (saved + 1, args.count, e))
        if saved < args.count and args.interval:
            time.sleep(args.interval)

    print("Done. Saved %d/%d frames." % (saved, args.count))
    if saved != args.count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

