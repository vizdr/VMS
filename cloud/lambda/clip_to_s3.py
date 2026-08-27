import boto3, os, json
from datetime import datetime, timedelta, timezone

REGION = os.environ["AWS_REGION"]
S3B = os.environ["BUCKET"]

def lambda_handler(event, context):
    # Triggered by an IoT Rule (fire-and-forget, no caller waiting on a response) --
    # no CORS concern here unlike the API Gateway Lambdas in §8, but still log clearly
    # on failure since a silently-dropped event otherwise only shows up as a gap in the
    # evidence trail days later.
    try:
        ts = datetime.fromisoformat(event["timestamp"])
        start, end = ts - timedelta(seconds=12), ts + timedelta(seconds=33)
        stream = event["stream"]

        kv = boto3.client("kinesisvideo", region_name=REGION)
        ep = kv.get_data_endpoint(StreamName=stream, APIName="GET_CLIP")["DataEndpoint"]
        kvam = boto3.client("kinesis-video-archived-media", endpoint_url=ep, region_name=REGION)

        clip = kvam.get_clip(
            StreamName=stream,
            ClipFragmentSelector={
                "FragmentSelectorType": "PRODUCER_TIMESTAMP",
                "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end},
            },
        )["Payload"].read()

        key = f"clips/{stream}/{ts:%Y/%m/%d}/{ts:%H%M%S}.mp4"
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=S3B, Key=key, Body=clip, ContentType="video/mp4")

        boto3.resource("dynamodb", region_name=REGION).Table("clips").put_item(Item={
            "cameraId": stream, "startTs": start.isoformat(),
            "s3Key": key, "labels": event.get("labels", []),
            # int, not float -- DynamoDB's resource layer rejects native Python floats
            # (needs Decimal), and sub-second precision isn't useful for a UI duration display
            "durationSec": round((end - start).total_seconds()),
        })
        return {"key": key}
    except Exception as e:
        print(f"clip-to-s3 failed for event {json.dumps(event)}: {e}")
        raise   # let the IoT Rule's own error action / CloudWatch metric see the failure
