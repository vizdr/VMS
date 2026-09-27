import boto3, os, json
from botocore.exceptions import ClientError
from datetime import datetime, timedelta, timezone

REGION = os.environ["AWS_REGION"]
S3B = os.environ["BUCKET"]


def utc(ts):
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def codec_windows(start, end, camera):
    """[start, end] split at the camera's last video-codec switch -- one window per codec.

    KVS refuses a clip whose fragments change codec ("The codec private data is not
    consistent between all fragments", measurements/codec-phase0.md §4), so a detection
    whose window spans an H.264/H.265 switch would otherwise lose its clip entirely. The
    producer stamps videoCodecActiveSince when it restarts on the new codec
    (adapter/bin/stream-codec.py); nothing of the old codec is newer than that. Same
    function in record_clip.py.
    """
    since = (camera or {}).get("videoCodecActiveSince")
    if since:
        since = utc(datetime.fromisoformat(since))
        if start < since < end:
            return [(start, since), (since, end)]
    return [(start, end)]


def mp4_video_codec(data):
    """'h264' / 'h265' from the clip's own MP4 sample description, or None.

    Read from the payload, not the registry: the two halves of a clip split at a codec
    switch have different codecs. Only `moov` is searched -- the sample-entry fourccs
    (avc1/avc3, hvc1/hev1) could occur by chance in `mdat`, and `ftyp`'s brand list may
    itself say "avc1". Same function in record_clip.py.
    """
    i, n = 0, len(data)
    while i + 8 <= n:
        size, box = int.from_bytes(data[i:i + 4], "big"), data[i + 4:i + 8]
        hdr = 8
        if size == 1:                        # 64-bit largesize
            size, hdr = int.from_bytes(data[i + 8:i + 16], "big"), 16
        elif size == 0:                      # box runs to end of file
            size = n - i
        if size < hdr:
            return None                      # malformed; don't guess
        if box == b"moov":
            moov = data[i + hdr:i + size]
            if b"hvc1" in moov or b"hev1" in moov:
                return "h265"
            if b"avc1" in moov or b"avc3" in moov:
                return "h264"
            return None
        i += size
    return None


def no_fragments(e):
    # By error code: GetClip errors come from the archived-media client, whose exception
    # classes are not the kinesisvideo client's (FoundAndFixed.md #45).
    return isinstance(e, ClientError) and e.response.get("Error", {}).get("Code") == "ResourceNotFoundException"


def lambda_handler(event, context):
    # Triggered by an IoT Rule (fire-and-forget, no caller waiting on a response) --
    # no CORS concern here unlike the API Gateway Lambdas in §8, but still log clearly
    # on failure since a silently-dropped event otherwise only shows up as a gap in the
    # evidence trail days later.
    try:
        ts = utc(datetime.fromisoformat(event["timestamp"]))
        start, end = ts - timedelta(seconds=12), ts + timedelta(seconds=33)
        stream = event["stream"]

        camera = boto3.resource("dynamodb", region_name=REGION).Table("cameras") \
            .get_item(Key={"cameraId": stream}).get("Item")
        windows = codec_windows(start, end, camera)
        split = len(windows) > 1

        kv = boto3.client("kinesisvideo", region_name=REGION)
        ep = kv.get_data_endpoint(StreamName=stream, APIName="GET_CLIP")["DataEndpoint"]
        kvam = boto3.client("kinesis-video-archived-media", endpoint_url=ep, region_name=REGION)
        s3 = boto3.client("s3", region_name=REGION)
        clips = boto3.resource("dynamodb", region_name=REGION).Table("clips")

        keys, missing = [], None
        for n, (s, e) in enumerate(windows, 1):
            try:
                clip = kvam.get_clip(
                    StreamName=stream,
                    ClipFragmentSelector={
                        "FragmentSelectorType": "PRODUCER_TIMESTAMP",
                        "TimestampRange": {"StartTimestamp": s, "EndTimestamp": e},
                    },
                )["Payload"].read()
            except ClientError as err:
                # One side of a split may be empty (the producer's restart on the new
                # codec takes ~10 s); only "no footage on either side" is a failure.
                if split and no_fragments(err):
                    missing = err
                    continue
                raise

            # The unsplit key is unchanged; the parts of a split get a suffix so the two
            # clips of one detection cannot overwrite each other.
            suffix = f"-{n}" if split else ""
            key = f"clips/{stream}/{ts:%Y/%m/%d}/{ts:%H%M%S}{suffix}.mp4"
            s3.put_object(Bucket=S3B, Key=key, Body=clip, ContentType="video/mp4")

            clips.put_item(Item={
                "cameraId": stream, "startTs": s.isoformat(),
                "s3Key": key,
                "labels": event.get("labels", []) + (["codec-switch"] if split else []),
                # int, not float -- DynamoDB's resource layer rejects native Python floats
                # (needs Decimal), and sub-second precision isn't useful for a UI duration display
                "durationSec": round((e - s).total_seconds()),
                # shown after the duration in the clip list; absent only if unreadable
                **({"videoCodec": c} if (c := mp4_video_codec(clip)) else {}),
            })
            keys.append(key)
        if not keys:
            raise missing
        return {"keys": keys}
    except Exception as e:
        print(f"clip-to-s3 failed for event {json.dumps(event)}: {e}")
        raise   # let the IoT Rule's own error action / CloudWatch metric see the failure
