#!/usr/bin/env python3
"""Print the video codec a camera's MediaMTX path is actually receiving, shell-sourceable:

    eval "$(stream-codec.py cam-02)"      # sets VIDEO_CODEC=h264|h265

The producer builds its depayloader from this, not from the registry. A camera's codec is
set on the camera itself (admin GUI -> onvif_media2.py), so what MediaMTX receives is the
truth -- and asking it needs no AWS, which keeps every producer startable while AWS is
unreachable (the rule cam-01 already lives by).

Waits up to --wait seconds for the path to come up with H.264 or H.265 video. If it does
not, prints nothing and exits 1, so the unit fails and systemd retries (Restart=on-failure)
-- what a dead source has always meant for a producer.

When the codec differs from the last one recorded, it also records the change, soft-failing
and on a hard time budget so AWS can never delay a start:
  - registry: videoCodecActive, plus videoCodecActiveSince when it replaces another codec.
    The GUIs show it, and clip windows get clamped to it: KVS refuses any clip or HLS
    session spanning a codec change (measurements/codec-phase0.md §4).
  - KVS: the stream's MediaType, which consumers such as the console player read.
    UpdateStream, not a new stream -- the archive and ARN stay (codec-phase0.md §3).
"""
import argparse
import datetime as dt
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import camera_control  # noqa: E402
import mediamtx_api    # noqa: E402

RECORD_BUDGET_SEC = 8


def wait_for_codec(path: str, wait: int) -> str | None:
    deadline = time.monotonic() + wait
    while True:
        codec = mediamtx_api.path_video_codec(path)
        if codec or time.monotonic() >= deadline:
            return codec
        time.sleep(1)


def record(camera_id: str, codec: str) -> None:
    try:
        from botocore.config import Config
        from aws_device_creds import get_session   # loads config; inside the try on purpose
        quick = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
        session = get_session()

        table = session.resource("dynamodb", config=quick).Table("cameras")
        prev = (table.get_item(Key={"cameraId": camera_id}).get("Item") or {}).get("videoCodecActive")
        if prev != codec:
            # "Since" only marks a real switch. On the very first record there is no
            # earlier codec in the archive, so there is no boundary to clamp clips to.
            update, values = "SET videoCodecActive = :c", {":c": codec}
            if prev:
                update += ", videoCodecActiveSince = :t"
                values[":t"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            table.update_item(Key={"cameraId": camera_id}, UpdateExpression=update,
                              ExpressionAttributeValues=values)
            print(f"stream-codec.py: {camera_id} now streams {codec} (was {prev or 'unrecorded'})",
                  file=sys.stderr)

        kv = session.client("kinesisvideo", config=quick)
        info = kv.describe_stream(StreamName=camera_id)["StreamInfo"]
        parts = [p for p in (info.get("MediaType") or "").split(",") if p]
        want = f"video/{codec}"
        if want not in parts:
            # Replace only the video part; keep anything else (e.g. an audio type) as set.
            media_type = ",".join([want] + [p for p in parts if not p.startswith("video/")])
            kv.update_stream(StreamName=camera_id, CurrentVersion=info["Version"],
                             MediaType=media_type)
            print(f"stream-codec.py: {camera_id} KVS MediaType -> {media_type}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 -- recording is best effort, streaming is not
        print(f"stream-codec.py: could not record {codec} for {camera_id} "
              f"({type(e).__name__}: {e}); streaming anyway", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("camera_id")
    ap.add_argument("--wait", type=int, default=30, help="seconds to wait for the source")
    args = ap.parse_args()

    path = camera_control.mediamtx_path_name(args.camera_id)
    codec = wait_for_codec(path, args.wait)
    if not codec:
        print(f"stream-codec.py: no H.264/H.265 video on MediaMTX path '{path}' after "
              f"{args.wait}s", file=sys.stderr)
        return 1

    t = threading.Thread(target=record, args=(args.camera_id, codec), daemon=True)
    t.start()
    t.join(RECORD_BUDGET_SEC)
    if t.is_alive():
        print(f"stream-codec.py: AWS slow -- not waiting to record {codec}; next start retries",
              file=sys.stderr)
    print(f"VIDEO_CODEC={codec}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
