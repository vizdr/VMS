import boto3, os, json

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["BUCKET"]

CORS = {"Access-Control-Allow-Origin": "*"}

# Deep Archive retrieval tiers: "Standard" ~12h, "Bulk" ~48h. "Expedited" does not exist
# for Deep Archive (only for regular Glacier) -- there is no fast path here, by design;
# that's the whole reason it's ~1/6th the storage cost of Standard.
RESTORE_ETA_HOURS = 12

def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        key = body.get("s3Key")
        if not key or not key.startswith("clips/"):
            return {"statusCode": 400, "headers": CORS,
                     "body": json.dumps({"error": "s3Key required"})}

        # explicit regional endpoint -- without it, generate_presigned_url can build a
        # global (s3.amazonaws.com) hostname while signing for eu-central-1, and S3
        # rejects the mismatch between the endpoint hit and the signing region with a
        # bare 400 (no useful error body). Only matters outside us-east-1.
        s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com")
        head = s3.head_object(Bucket=BUCKET, Key=key)
        # S3 omits StorageClass entirely for the Standard tier -- absence means Standard,
        # not an error.
        storage_class = head.get("StorageClass", "STANDARD")
        restore = head.get("Restore")  # e.g. 'ongoing-request="true"'

        if storage_class in ("STANDARD", "STANDARD_IA"):
            url = s3.generate_presigned_url(
                "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=300)
            return {"statusCode": 200, "headers": CORS,
                     "body": json.dumps({"status": "ready", "url": url})}

        # DEEP_ARCHIVE or GLACIER from here on -- not directly readable.
        if restore is None:
            s3.restore_object(Bucket=BUCKET, Key=key, RestoreRequest={
                "Days": 1, "GlacierJobParameters": {"Tier": "Standard"}})
            return {"statusCode": 202, "headers": CORS, "body": json.dumps({
                "status": "restore_requested", "eta_hours": RESTORE_ETA_HOURS})}

        if 'ongoing-request="true"' in restore:
            return {"statusCode": 202, "headers": CORS,
                     "body": json.dumps({"status": "restoring", "eta_hours": RESTORE_ETA_HOURS})}

        # ongoing-request="false" -- restore finished, object is temporarily readable
        # again (still reported as DEEP_ARCHIVE/GLACIER storage class, but a GET works
        # until the restored copy's expiry-date).
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=300)
        return {"statusCode": 200, "headers": CORS,
                 "body": json.dumps({"status": "ready", "url": url})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
