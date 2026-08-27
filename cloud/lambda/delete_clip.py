import boto3, os, json

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["BUCKET"]

CORS = {"Access-Control-Allow-Origin": "*"}

def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        key = body.get("s3Key")
        start_ts = body.get("startTs")
        if not key or not key.startswith("clips/") or not start_ts:
            return {"statusCode": 400, "headers": CORS,
                     "body": json.dumps({"error": "s3Key and startTs are required"})}

        # cameraId isn't in the request -- it's recoverable from the key itself, since
        # every writer (clip_to_s3.py, record_clip.py) always builds keys as
        # clips/<stream>/... . Avoids requiring the client to separately track/send it.
        parts = key.split("/")
        if len(parts) < 2:
            return {"statusCode": 400, "headers": CORS,
                     "body": json.dumps({"error": "malformed s3Key"})}
        camera_id = parts[1]

        s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com")
        # Deleting an archived (Deep Archive/Glacier) object needs no prior restore --
        # unlike reading or reclassifying it, DeleteObject always works directly.
        s3.delete_object(Bucket=BUCKET, Key=key)

        boto3.resource("dynamodb", region_name=REGION).Table("clips").delete_item(
            Key={"cameraId": camera_id, "startTs": start_ts})

        return {"statusCode": 200, "headers": CORS, "body": json.dumps({"status": "deleted"})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
