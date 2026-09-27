import boto3, os, json

REGION = os.environ["AWS_REGION"]
CORS = {"Access-Control-Allow-Origin": "*"}

def video_codec(item):
    """The codec a camera is set to deliver: the admin's choice (videoCodec, written by the
    local admin GUI when it switches the camera), else the hardware-first default the
    adapter recorded (videoCodecDefault, adapter/codec_caps.py). Every camera predating
    codec selection streams H.264, hence the last resort."""
    return item.get("videoCodec") or item.get("videoCodecDefault") or "h264"


def lambda_handler(event, context):
    try:
        table = boto3.resource("dynamodb", region_name=REGION).Table("cameras")
        # Scan, not Query -- this table has no sort key and is expected to stay small
        # (a handful of cameras on one adapter), so a full scan is the right tool here.
        items = table.scan()["Items"]
        cameras = sorted(
            (
                {
                    "id": i["cameraId"],
                    "mode": i.get("mode"),
                    "hasIrControl": bool(i.get("hasIrControl", False)),
                    # How clips get created for this camera. "manual" (the default, and
                    # what every camera had before Phase 1) means only the Record button
                    # makes clips; the detection modes are consumed by the event watcher.
                    "recordingMode": i.get("recordingMode", "manual"),
                    # Detection needs an ONVIF event subscription, so it is only possible
                    # for cameras we have an ONVIF host for. cam-01 is a USB webcam with
                    # no ONVIF at all and can therefore only ever be "manual" -- the GUIs
                    # gate the selector on this, the same way they gate IR control.
                    "supportsDetection": bool(i.get("onvifHost")),
                    # Whether this camera records audio alongside video. Two separate
                    # facts, both needed by the GUIs: audioCapable is a property of the
                    # hardware (written at registration), audioEnabled is the user's
                    # choice. Default is video-only -- audio is opt-in per camera, which
                    # is both the safe default for a recording system and the one the
                    # law tends to assume (see Camera-Features.md on audio).
                    "audioCapable": bool(i.get("audioCapable", False)),
                    "audioEnabled": bool(i.get("audioEnabled", False)),
                    # Seconds of local recording kept after the connection to AWS drops;
                    # 0 = off, the default. Separate from the fixed ~2 min pre-roll that
                    # is always included (OUTAGE.md 2.2). Every camera can do this -- it
                    # is MediaMTX-side, not camera-side -- so unlike audio there is no
                    # capability flag to gate on, only the USB buffer being present,
                    # which only the adapter can see.
                    "outageBufferSec": int(i.get("outageBufferSec", 0) or 0),
                    # Video codec, for display only: it is chosen in the local admin GUI,
                    # which switches the camera's own encoder -- there is deliberately no
                    # cloud route to change it. videoEncode says where the encoding happens
                    # ("camera" or "pi-hw", the adapter's hardware encoder); the client
                    # also warns a viewer whose browser can't decode H.265.
                    "videoCodec": video_codec(i),
                    "videoEncode": (i.get("videoCodecCaps") or {}).get(video_codec(i)),
                    # What the cloud stream last carried (set by the producer at start);
                    # differs from videoCodec only until the producer next starts.
                    "videoCodecActive": i.get("videoCodecActive"),
                }
                for i in items
            ),
            key=lambda c: c["id"],
        )
        return {"statusCode": 200, "headers": CORS, "body": json.dumps({"cameras": cameras})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
