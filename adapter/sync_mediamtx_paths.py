#!/usr/bin/env python3
"""Recreate MediaMTX paths for network (passthrough) cameras from the camera registry.

Why this exists: MediaMTX does not persist API config changes to mediamtx.yml (see
mediamtx_api.py). The admin GUI adds a registered camera's path through that API, so every
MediaMTX restart silently dropped every GUI-registered camera until someone clicked
Re-register -- and cam-02 was kept alive by hardcoding it, RTSP credentials included, in the
tracked mediamtx.yml. Now the registry (DynamoDB `cameras`, field `rtspUrl`) is the only
place a network camera's source lives, and this runs as kvs-mediamtx.service's
ExecStartPost= (LAUNCH.md A8): after every MediaMTX start, Restart=on-failure ones included,
which a separate unit ordered After= MediaMTX would miss.

Offline starts: the last successful registry read is cached locally, mode 0600 because the
URLs carry camera credentials, and used when AWS is unreachable -- cameras must come up
without the cloud; recording through an outage is what the outage buffer exists for.

It never removes a path. A path the registry doesn't list (cam01, which its own publisher
feeds; anything added by hand) is left alone. It adds missing paths and fixes a source that
changed. An unchanged path is not touched at all, because patching `source` makes MediaMTX
recreate the path and disconnect its readers, a running KVS producer included.

Usage:  sync_mediamtx_paths.py [--dry-run]
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

import camera_control
import mediamtx_api

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "vms-adapter"
CACHE = STATE_DIR / "cameras-cache.json"
API_WAIT_SEC = 20        # ExecStartPost runs as soon as MediaMTX forks, before its API listens


def log(msg):
    print(f"sync-paths: {msg}", flush=True)


def redact(url):
    """Hide RTSP credentials in anything that reaches the journal."""
    url = re.sub(r"(?i)(password=)[^&]*", r"\1***", url)
    return re.sub(r"(rtsps?://[^:/@]+:)[^@]*@", r"\1***@", url)


def fetch_registry():
    """The registry's cameras, reduced to what path setup needs. Raises when unreachable."""
    from botocore.config import Config
    from aws_device_creds import get_session   # also loads /etc/adapter/adapter.env
    cfg = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
    items = get_session().resource("dynamodb", config=cfg).Table("cameras").scan()["Items"]
    keep = ("mode", "rtspUrl", "onvifHost", "onvifPort", "onvifEndpointRef")  # the last three: rematch_cameras.py
    return [{"cameraId": i["cameraId"], **{k: (int(i[k]) if k == "onvifPort" else i[k]) for k in keep if k in i}}
            for i in items]


def save_cache(cameras):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "cameras": cameras}, f, indent=1)
    os.replace(tmp, CACHE)


def load_cameras(dry_run=False):
    try:
        cameras = fetch_registry()
    except Exception as e:  # noqa: BLE001 -- any failure here means "fall back to the cache"
        log(f"registry unreachable ({type(e).__name__}: {e})")
        try:
            data = json.loads(CACHE.read_text())
        except FileNotFoundError:
            log(f"no cache at {CACHE} either -- no network camera paths this start")
            return None
        log(f"using cached registry from {data['fetchedAt']}")
        return data["cameras"]
    if not dry_run:
        save_cache(cameras)
    log(f"registry: {len(cameras)} camera(s){'' if dry_run else ', cache updated'}")
    return cameras


def wait_for_api():
    deadline = time.monotonic() + API_WAIT_SEC
    while True:
        try:
            requests.get(f"{mediamtx_api.API}/v3/config/global/get", timeout=2).raise_for_status()
            return True
        except requests.RequestException:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.5)


def sync(cameras, dry_run=False):
    for cam in sorted(cameras, key=lambda c: c["cameraId"]):
        camera_id = cam["cameraId"]
        if cam.get("mode") != "passthrough":
            continue                      # transcode cameras publish into their path themselves
        if not cam.get("rtspUrl"):
            log(f"{camera_id}: passthrough but no rtspUrl in the registry -- "
                f"Re-register it in the admin GUI (LAUNCH.md E3)")
            continue
        path = camera_control.mediamtx_path_name(camera_id)
        want = {"source": cam["rtspUrl"], "rtspTransport": "tcp"}
        conf = mediamtx_api.get_path_conf(path)
        if conf is None:
            action = "add"
        elif conf.get("source") == want["source"] and conf.get("rtspTransport") == "tcp":
            log(f"{camera_id}: {path} up to date")
            continue
        else:
            action = "patch"              # reconnects this path's readers -- the source really moved
        log(f"{camera_id}: {action} {path} <- {redact(want['source'])}{' (dry run)' if dry_run else ''}")
        if not dry_run:
            (mediamtx_api.add_path if action == "add" else mediamtx_api.patch_path)(path, want)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    args = ap.parse_args()

    cameras = load_cameras(args.dry_run)
    if cameras is None:
        return 0
    if not wait_for_api():
        log(f"MediaMTX API at {mediamtx_api.API} not answering after {API_WAIT_SEC}s")
        return 1
    sync(cameras, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
