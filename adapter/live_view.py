"""Live view on an ONVIF camera's sub stream, whatever stream is recorded (guide §21.7).

Recording takes the stream chosen in the admin GUI (guide §21.6: `rtspUrl`, MediaMTX path
`cam03`, KVS stream `cam-03`). Live view -- the admin GUI's local preview and the cloud page's
player -- always shows the sub stream when the camera has one: it is far less data to watch, and
a browser decodes 800x448 H.265 more readily than 2560x1440.

When the recorded stream *is* the sub stream, or the camera has only one, live view simply uses
the recording path and stream, as before. Only a camera recording its main stream gets a
separate live view, three parts with derived names (camera_control.live_*):
  - registry `liveRtspUrl`: the sub stream's URI, with the registered URI's credentials. Its
    presence is what every reader goes by (sync_mediamtx_paths.py, the admin GUI's preview,
    pir-get-hls-url);
  - MediaMTX path `cam03_live`, pulling it continuously like any camera path (a producer's
    stream-codec.py needs the path's tracks before anyone reads it, so not on demand);
  - KVS stream `cam-03-live` and its producer `kvs-cam@cam03_live` (provision-live.sh), which
    runs exactly while the recording producer runs: Start starts both, Stop stops both.

Setting it up writes the registry first, then MediaMTX, then systemd, the order a MediaMTX
restart rebuilds things in; removing goes the other way. Rows change field by field, and
`liveRtspUrl` is a new field the successor doesn't read (PIR-MQTT-VMS-PI4.md §4.3).
"""
import os
import subprocess
from pathlib import Path

import camera_control
import mediamtx_api
import onvif_media2

PROVISION = str(Path(__file__).resolve().parent / "bin" / "provision-live.sh")
# KVS serves HLS only from a stream that keeps data (retention 0 is ingest-only). One hour is
# the minimum and plenty: nothing but the live player reads this stream.
LIVE_RETENTION_HOURS = 1


def live_uri(streams: list, rtsp_url: str) -> str | None:
    """The URI live view should pull, or None when it should use the recorded stream: only a
    camera recording its main stream, and having a sub stream, gets one. `streams` is
    onvif_media2.streams(), biggest picture first."""
    current = next((s for s in streams if onvif_media2.same_stream(s["streamUri"], rtsp_url)), None)
    sub = next((s for s in streams if s["name"] == "sub"), None)
    if current is None or current["name"] != "main" or sub is None:
        return None
    return onvif_media2.with_credentials(sub["streamUri"], rtsp_url)


def channel_exists(camera_id: str) -> bool:
    return os.path.exists(os.path.join(camera_control.CHANNELS_DIR,
                                       f"{camera_control.live_path_name(camera_id)}.env"))


def _provision(action: str, camera_id: str):
    r = subprocess.run(["sudo", PROVISION, action, camera_id,
                        camera_control.mediamtx_path_name(camera_id),
                        camera_control.unit_name(camera_id)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"provision-live.sh {action}: {(r.stderr or r.stdout).strip()}")


def _ensure_kvs_stream(kv, camera_id: str) -> str:
    name = camera_control.live_stream_name(camera_id)
    try:
        return kv.describe_stream(StreamName=name)["StreamInfo"]["StreamARN"]
    except kv.exceptions.ResourceNotFoundException:
        # video/h264 as at registration: stream-codec.py corrects MediaType at the first start.
        return kv.create_stream(StreamName=name, DataRetentionInHours=LIVE_RETENTION_HOURS,
                                MediaType="video/h264")["StreamARN"]


def apply(camera_id: str, item: dict, uri: str | None, table, kv) -> dict:
    """Make the camera's live view match `uri` (live_uri()), and say what changed.

    `item` is the camera's registry row as it is now; `table` the cameras table and `kv` a
    kinesisvideo client, both through the device identity. Idempotent: an unchanged camera costs
    one MediaMTX read and no write anywhere."""
    path = camera_control.live_path_name(camera_id)
    if uri:
        stream_arn = _ensure_kvs_stream(kv, camera_id)
        if item.get("liveRtspUrl") != uri:
            table.update_item(Key={"cameraId": camera_id}, UpdateExpression="SET liveRtspUrl = :u",
                              ExpressionAttributeValues={":u": uri})
        want = {"source": uri, "rtspTransport": "tcp"}
        conf = mediamtx_api.get_path_conf(path)
        if conf is None:
            mediamtx_api.add_path(path, want)
        elif conf.get("source") != uri:
            mediamtx_api.patch_path(path, want)       # reconnects only the live view's readers
        if not channel_exists(camera_id):
            _provision("add", camera_id)
        return {"live": "sub", "path": path, "kvsStream": camera_control.live_stream_name(camera_id),
                "kvsStreamArn": stream_arn}

    # The KVS stream stays: it costs nothing idle, and a later switch back to main reuses it.
    if channel_exists(camera_id):
        _provision("remove", camera_id)
    mediamtx_api.delete_path(path)
    if item.get("liveRtspUrl"):
        table.update_item(Key={"cameraId": camera_id}, UpdateExpression="REMOVE liveRtspUrl")
    return {"live": "recorded stream", "path": camera_control.mediamtx_path_name(camera_id)}
