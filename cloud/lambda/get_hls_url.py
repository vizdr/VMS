import boto3, os, json
from botocore.exceptions import ClientError

REGION = os.environ["AWS_REGION"]
DEFAULT_STREAM = os.environ.get("STREAM_NAME", "cam-01")

CORS = {"Access-Control-Allow-Origin": "*"}
cameras_table = boto3.resource("dynamodb", region_name=REGION).Table("cameras")

def lambda_handler(event, context):
    kv = boto3.client("kinesisvideo", region_name=REGION)
    try:
        stream = (event.get("queryStringParameters") or {}).get("stream", DEFAULT_STREAM)
        # IAM's stream-ARN resource is now a wildcard (cam-*) rather than an enumerated
        # list, so this registry lookup is the only thing standing between an arbitrary
        # caller-supplied "stream" value and a live KVS API call -- not just a nicety.
        if "Item" not in cameras_table.get_item(Key={"cameraId": stream}):
            return {"statusCode": 400, "headers": CORS,
                     "body": json.dumps({"error": f"unknown stream '{stream}'"})}

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
        url = kvam.get_hls_streaming_session_url(
            StreamName=stream,
            PlaybackMode="LIVE",
            Expires=session_ttl,
        )["HLSStreamingSessionURL"]

        return {
            "statusCode": 200,
            "headers": CORS,
            "body": json.dumps({"url": url, "expires_in": session_ttl}),
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
