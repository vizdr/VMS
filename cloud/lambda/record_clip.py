import boto3, os, json
from datetime import datetime, timedelta

REGION = os.environ["AWS_REGION"]
S3B = os.environ["BUCKET"]
STREAM = os.environ.get("STREAM_NAME", "cam-01")

CORS = {"Access-Control-Allow-Origin": "*"}

# Small safety padding on both ends -- the browser's Start/Stop button presses are wall
# clock on the *viewer's* machine, not the producer's; a couple of seconds of margin
# absorbs minor clock skew and network round-trip between click and this Lambda running,
# same rationale as the fixed +/-window in clip_to_s3.py, just applied to a caller-given
# range instead of one fixed to a single event timestamp.
PAD = timedelta(seconds=2)

def lambda_handler(event, context):
    # created outside the try block -- referencing kv.exceptions.* in the except clause
    # below needs kv to exist even if a request-parsing error happens before the line
    # that normally assigns it, or the except clause itself raises a masking NameError
    kv = boto3.client("kinesisvideo", region_name=REGION)
    try:
        body = json.loads(event.get("body") or "{}")
        start = datetime.fromisoformat(body["startTs"]) - PAD
        end = datetime.fromisoformat(body["endTs"]) + PAD
        if end <= start:
            return {"statusCode": 400, "headers": CORS,
                     "body": json.dumps({"error": "endTs must be after startTs"})}

        ep = kv.get_data_endpoint(StreamName=STREAM, APIName="GET_CLIP")["DataEndpoint"]
        kvam = boto3.client("kinesis-video-archived-media", endpoint_url=ep, region_name=REGION)

        clip = kvam.get_clip(
            StreamName=STREAM,
            ClipFragmentSelector={
                "FragmentSelectorType": "PRODUCER_TIMESTAMP",
                "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end},
            },
        )["Payload"].read()

        key = f"clips/{STREAM}/{start:%Y/%m/%d}/{start:%H%M%S}-manual.mp4"
        boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com") \
            .put_object(Bucket=S3B, Key=key, Body=clip, ContentType="video/mp4")

        boto3.resource("dynamodb", region_name=REGION).Table("clips").put_item(Item={
            "cameraId": STREAM, "startTs": start.isoformat(),
            "s3Key": key, "labels": ["manual-recording"],
            "durationSec": round((end - start).total_seconds()),
        })
        return {"statusCode": 200, "headers": CORS, "body": json.dumps({"key": key})}
    except kv.exceptions.ResourceNotFoundException:
        return {"statusCode": 503, "headers": CORS, "body": json.dumps(
            {"error": "no footage found for that time range -- was the stream live throughout?"})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
