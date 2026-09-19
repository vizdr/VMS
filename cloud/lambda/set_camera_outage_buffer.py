import boto3, os, json

REGION = os.environ["AWS_REGION"]
CORS = {"Access-Control-Allow-Origin": "*"}

cameras_table = boto3.resource("dynamodb", region_name=REGION).Table("cameras")

# How long to keep recording locally AFTER the connection drops. 0 = off (the default).
# The ~2 min pre-roll is separate and always included, so the shortest setting still
# yields ~2.5 min of footage -- see OUTAGE.md 2.2, and note the UI must say so or the
# numbers read as nonsense.
#
# This list is duplicated in adapter/outage_buffer.py (VALID_LIMITS) and in both GUIs.
# Deliberately: the Lambda cannot import adapter code, and a value this API accepts but
# the supervisor rejects would be silently ignored on the device.
VALID_LIMITS = {0, 30, 120, 300, 600, 1800, 3600, 18000, 43200, 86400}


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        camera_id = body.get("cameraId")
        secs = body.get("outageBufferSec")

        if not isinstance(secs, int) or isinstance(secs, bool) or secs not in VALID_LIMITS:
            return {"statusCode": 400, "headers": CORS, "body": json.dumps(
                {"error": f"outageBufferSec must be one of {sorted(VALID_LIMITS)}"})}

        item = cameras_table.get_item(Key={"cameraId": camera_id}).get("Item") if camera_id else None
        if not item:
            return {"statusCode": 400, "headers": CORS,
                    "body": json.dumps({"error": f"unknown camera '{camera_id}'"})}

        cameras_table.update_item(
            Key={"cameraId": camera_id},
            UpdateExpression="SET outageBufferSec = :s",
            ExpressionAttributeValues={":s": secs},
        )
        # Unlike the audio flag, this one applies without restarting anything: the
        # supervisor re-reads the registry every 60s and arms or disarms MediaMTX
        # recording in place. Buffering only actually runs while the camera's producer is
        # running, though -- with no producer there is no cloud stream to protect.
        return {"statusCode": 200, "headers": CORS, "body": json.dumps(
            {"cameraId": camera_id, "outageBufferSec": secs, "appliesOn": "within 60s"})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
