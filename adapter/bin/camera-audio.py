#!/usr/bin/env python3
"""Print a camera's audio settings as shell-sourceable lines.

The producer scripts (stream-cam02.sh, stream-channel.sh) are bash, and the registry is
the single source of truth for whether a camera records audio -- so they need one tiny
lookup at startup rather than a hardcoded pipeline. Usage:

    eval "$(camera-audio.py cam-02)"     # sets AUDIO, AUDIO_CODEC, AUDIO_DEVICE

The pipeline is fixed for the life of a gst-launch process, so this is deliberately read
ONCE at start and never polled: changing the flag takes effect on the next Start. That is
not just an implementation shortcut -- KVS rejects a stream whose fragments change from
video-only to audio+video mid-stream ("Track changes aren't supported", GetClip and
GetHLSStreamingSessionURL both), so flipping this under a running producer would corrupt
the very clips the setting exists to improve.

Failure is deliberately soft: if the registry is unreachable this prints AUDIO=off and
exits 0, so a network blip degrades to today's video-only behaviour instead of leaving
the camera with no producer at all.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config

REGION = config.AWS_REGION


def main() -> int:
    if len(sys.argv) != 2:
        print("AUDIO=off")
        print("usage: camera-audio.py <cameraId>", file=sys.stderr)
        return 0

    camera_id = sys.argv[1]
    try:
        from aws_device_creds import get_session
        table = get_session(REGION).resource("dynamodb").Table("cameras")
        item = table.get_item(Key={"cameraId": camera_id}).get("Item") or {}
    except Exception as e:  # noqa: BLE001 -- see module docstring on soft failure
        print("AUDIO=off")
        print(f"camera-audio.py: registry lookup failed ({type(e).__name__}: {e}); "
              f"defaulting {camera_id} to video-only", file=sys.stderr)
        return 0

    # Both flags must be true. audioCapable is a property of the hardware, written at
    # registration; audioEnabled is the user's choice. Gating on both means a stale
    # audioEnabled on a camera that turns out to have no microphone cannot produce a
    # pipeline that waits forever for an audio pad that never delivers.
    enabled = bool(item.get("audioEnabled", False)) and bool(item.get("audioCapable", False))
    print(f"AUDIO={'on' if enabled else 'off'}")
    print(f"AUDIO_CODEC={item.get('audioCodec', '')}")
    print(f"AUDIO_DEVICE={item.get('audioDevice', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
