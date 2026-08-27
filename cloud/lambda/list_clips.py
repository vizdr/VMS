import boto3, os, json

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["BUCKET"]
CAMERA = os.environ.get("STREAM_NAME", "cam-01")

CORS = {"Access-Control-Allow-Origin": "*"}

def lambda_handler(event, context):
    try:
        table = boto3.resource("dynamodb", region_name=REGION).Table("clips")
        # newest first -- ScanIndexForward=False on Query (not Scan) needs the sort key
        resp = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("cameraId").eq(CAMERA),
            ScanIndexForward=False,
            Limit=50,
        )

        # One head_object per clip to report its live storage tier -- fine at demo scale
        # (tens of clips), would need a different design (store tier in DynamoDB, updated
        # by an S3 Lifecycle -> EventBridge rule) before this fans out to thousands.
        s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com")
        clips = []
        for i in resp.get("Items", []):
            key = i["s3Key"]
            try:
                head = s3.head_object(Bucket=BUCKET, Key=key)
                tier = head.get("StorageClass", "STANDARD")
                restoring = 'ongoing-request="true"' in (head.get("Restore") or "")
            except Exception:
                tier, restoring = "UNKNOWN", False
            clips.append({
                "startTs": i["startTs"], "s3Key": key, "labels": i.get("labels", []),
                "tier": tier, "restoring": restoring,
                # DynamoDB's resource layer returns Decimal, which json.dumps can't
                # serialize directly -- cast to int. None for clips recorded before this
                # field existed (older items simply don't have the attribute).
                "durationSec": int(i["durationSec"]) if "durationSec" in i else None,
            })
        return {"statusCode": 200, "headers": CORS, "body": json.dumps({"clips": clips})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
