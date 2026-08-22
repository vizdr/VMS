import boto3, os, json

REGION = os.environ["AWS_REGION"]
STREAM = os.environ.get("STREAM_NAME", "cam-01")

CORS = {"Access-Control-Allow-Origin": "*"}

def lambda_handler(event, context):
    # An uncaught exception here returns API Gateway's own generic error response,
    # which has no CORS headers at all -- the browser then can't even read the error
    # body and reports a raw "NetworkError" instead of a useful message. Always return
    # through this function, never let boto3 exceptions propagate.
    try:
        kv = boto3.client("kinesisvideo", region_name=REGION)
        ep = kv.get_data_endpoint(
            StreamName=STREAM, APIName="GET_HLS_STREAMING_SESSION_URL"
        )["DataEndpoint"]

        kvam = boto3.client("kinesis-video-archived-media",
                            endpoint_url=ep, region_name=REGION)
        url = kvam.get_hls_streaming_session_url(
            StreamName=STREAM,
            PlaybackMode="LIVE",
            Expires=300,
        )["HLSStreamingSessionURL"]

        return {
            "statusCode": 200,
            "headers": CORS,
            "body": json.dumps({"url": url, "expires_in": 300}),
        }
    except kv.exceptions.ResourceNotFoundException:
        # Most common cause: kvs-cam01.service isn't currently producing, so
        # PlaybackMode=LIVE has nothing to return a session against.
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
