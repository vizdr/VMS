import boto3, os, json

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["BUCKET"]

CORS = {"Access-Control-Allow-Origin": "*"}
VALID_TIERS = {"STANDARD", "STANDARD_IA", "DEEP_ARCHIVE"}

def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        key = body.get("s3Key")
        target = body.get("tier")
        if not key or not key.startswith("clips/") or target not in VALID_TIERS:
            return {"statusCode": 400, "headers": CORS,
                     "body": json.dumps({"error": "s3Key and a valid tier are required"})}

        s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com")
        head = s3.head_object(Bucket=BUCKET, Key=key)
        current = head.get("StorageClass", "STANDARD")
        restore = head.get("Restore")

        if current == target:
            return {"statusCode": 200, "headers": CORS, "body": json.dumps({"status": "unchanged"})}

        # Moving a clip OUT of Deep Archive/Glacier needs it already restored (temporarily
        # readable) first -- CopyObject can't read the bytes of a still-archived object.
        # Moving further IN (Standard -> IA -> Deep Archive) has no such restriction; the
        # object's bytes are always readable by S3 itself for that direction.
        if current in ("DEEP_ARCHIVE", "GLACIER"):
            if restore is None or 'ongoing-request="true"' in restore:
                return {"statusCode": 409, "headers": CORS, "body": json.dumps({
                    "error": "clip is archived and not yet restored -- press Play first, "
                             "wait for the restore to finish, then try changing the tier again"})}

        s3.copy_object(
            Bucket=BUCKET, Key=key,
            CopySource={"Bucket": BUCKET, "Key": key},
            StorageClass=target,
            MetadataDirective="COPY",
        )
        return {"statusCode": 200, "headers": CORS, "body": json.dumps({"status": "changed", "tier": target})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
