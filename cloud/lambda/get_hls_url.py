import boto3, os, json
from datetime import datetime, timedelta, timezone
from botocore.exceptions import ClientError

REGION = os.environ["AWS_REGION"]
DEFAULT_STREAM = os.environ.get("STREAM_NAME", "cam-01")

CORS = {"Access-Control-Allow-Origin": "*"}
cameras_table = boto3.resource("dynamodb", region_name=REGION).Table("cameras")

def lambda_handler(event, context):
    kv = boto3.client("kinesisvideo", region_name=REGION)
    try:
        params = event.get("queryStringParameters") or {}
        stream = params.get("stream", DEFAULT_STREAM)
        # IAM's stream-ARN resource is now a wildcard (cam-*) rather than an enumerated
        # list, so this registry lookup is the only thing standing between an arbitrary
        # caller-supplied "stream" value and a live KVS API call -- not just a nicety.
        item = cameras_table.get_item(Key={"cameraId": stream}).get("Item")
        if not item:
            return {"statusCode": 400, "headers": CORS,
                     "body": json.dumps({"error": f"unknown stream '{stream}'"})}
        # The live player is this route's only caller (clips go through play-clip), so live
        # view is the default: a camera recording its main stream has its live view on the sub
        # stream, in its own KVS stream "<camera>-live", pushed while the recording producer runs
        # (adapter/live_view.py, guide §21.7). The name is derived here, never taken from the
        # caller. Defaulting matters: a page loaded before view=live existed must not be handed
        # the 2560x1440 main stream (FoundAndFixed.md #65). view=recorded asks for that.
        live = params.get("view", "live") == "live" and bool(item.get("liveRtspUrl"))
        if live:
            stream = f"{stream}-live"
        # since=<seconds>: the page pressed Start that long ago and wants only footage from
        # after it. A plain LIVE session right after a quick Stop -> Start is built from the
        # previous run's last fragment, which KVS still counts as live: the player plays 4 s
        # of old video and then waits for the new run (#65). LIVE_REPLAY from that moment
        # answers "no fragments" (-> 503, the page keeps polling) until the new run's first
        # fragment arrives, then plays on as live. Server timestamps, so no clock skew: the
        # page measures the age on its own clock and this subtracts it from AWS's.
        since = params.get("since")
        try:
            since = min(max(float(since), 1.0), 600.0) if since is not None else None
        except ValueError:
            return {"statusCode": 400, "headers": CORS,
                    "body": json.dumps({"error": "since must be a number of seconds"})}

        ep = kv.get_data_endpoint(
            StreamName=stream, APIName="GET_HLS_STREAMING_SESSION_URL"
        )["DataEndpoint"]

        kvam = boto3.client("kinesis-video-archived-media",
                            endpoint_url=ep, region_name=REGION)
        # KVS allows 300-43200s. 300 (the minimum, and the old value here) meant the
        # session URL died five minutes into every viewing session -- the client then
        # sat retrying a permanently-dead URL and the browser showed a spinner forever.
        # An hour keeps refreshes rare without leaving a usable URL lying around for
        # half a day; the client refreshes at 80% of this (see scheduleSessionRefresh).
        session_ttl = 3600
        mode = {"PlaybackMode": "LIVE"}
        if since is not None:
            mode = {"PlaybackMode": "LIVE_REPLAY", "HLSFragmentSelector": {
                "FragmentSelectorType": "SERVER_TIMESTAMP",
                "TimestampRange": {"StartTimestamp":
                                   datetime.now(timezone.utc) - timedelta(seconds=since)}}}
        url = kvam.get_hls_streaming_session_url(
            StreamName=stream,
            **mode,
            Expires=session_ttl,
            # fMP4 is KVS's default already; pinned because H.265 cameras depend on it --
            # HLS carries HEVC in fragmented MP4 (the verified path, `hvc1`), and a later
            # switch to MPEG_TS for some other reason must not silently break them
            # (measurements/codec-phase0.md §3).
            ContainerFormat="FRAGMENTED_MP4",
        )["HLSStreamingSessionURL"]

        return {
            "statusCode": 200,
            "headers": CORS,
            "body": json.dumps({"url": url, "expires_in": session_ttl, "stream": stream,
                                "view": "sub" if live else "recorded"}),
        }
    except ClientError as e:
        # Matched on the error *code*, not on a client's generated exception class.
        # This used to be `except kv.exceptions.ResourceNotFoundException`, which never
        # fired for the common case: "no fragments" is raised by the **kvam** client
        # (kinesis-video-archived-media), and boto3 generates a separate exception class
        # per client, so kvam's ResourceNotFoundException is not kv's. The friendly 503
        # below was dead code and users saw the raw AWS text via the generic handler.
        if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return {
                "statusCode": 503,
                "headers": CORS,
                "body": json.dumps({"error":
                    "stream is not live -- no footage is reaching the cloud. "
                    "Press Start; if it stays down, check the camera is online."}),
            }
        return {
            "statusCode": 500,
            "headers": CORS,
            "body": json.dumps({"error": str(e)}),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": CORS,
            "body": json.dumps({"error": str(e)}),
        }
