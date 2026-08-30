import boto3, os, json

REGION = os.environ["AWS_REGION"]
DEFAULT_STREAM = os.environ.get("STREAM_NAME", "cam-01")
ALLOWED_STREAMS = {"cam-01", "cam-02"}  # IAM is also scoped to exactly these two ARNs

CORS = {"Access-Control-Allow-Origin": "*"}

def lambda_handler(event, context):
    # created outside the try block -- see record_clip.py for why: referencing
    # kv.exceptions.* in the except clause needs kv to exist even if validation fails
    # before the line that would normally assign it.
    kv = boto3.client("kinesisvideo", region_name=REGION)
    try:
        stream = (event.get("queryStringParameters") or {}).get("stream", DEFAULT_STREAM)
        if stream not in ALLOWED_STREAMS:
            return {"statusCode": 400, "headers": CORS,
                     "body": json.dumps({"error": f"unknown stream '{stream}'"})}

        ep = kv.get_data_endpoint(
            StreamName=stream, APIName="GET_HLS_STREAMING_SESSION_URL"
        )["DataEndpoint"]

        kvam = boto3.client("kinesis-video-archived-media",
                            endpoint_url=ep, region_name=REGION)
        url = kvam.get_hls_streaming_session_url(
            StreamName=stream,
            PlaybackMode="LIVE",
            Expires=300,
        )["HLSStreamingSessionURL"]

        return {
            "statusCode": 200,
            "headers": CORS,
            "body": json.dumps({"url": url, "expires_in": 300}),
        }
    except kv.exceptions.ResourceNotFoundException:
        return {
            "statusCode": 503,
            "headers": CORS,
            "body": json.dumps({"error": "stream is not currently live -- press Start"}),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": CORS,
            "body": json.dumps({"error": str(e)}),
        }
