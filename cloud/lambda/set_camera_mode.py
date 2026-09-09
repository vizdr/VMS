import boto3, os, json

REGION = os.environ["AWS_REGION"]
CORS = {"Access-Control-Allow-Origin": "*"}

# Phase 1 of detection-driven recording (Camera-Features.md §9). This only writes the
# registry -- nothing consumes the value yet, by design: shipping the control surface
# before the event logic keeps the change independently testable and carries no
# regression risk to the working control plane.
VALID_MODES = {"manual", "motion", "cellMotion", "human"}

cameras_table = boto3.resource("dynamodb", region_name=REGION).Table("cameras")


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        camera_id = body.get("cameraId")
        mode = body.get("recordingMode")

        if mode not in VALID_MODES:
            return {"statusCode": 400, "headers": CORS, "body": json.dumps(
                {"error": f"recordingMode must be one of {sorted(VALID_MODES)}"})}

        item = cameras_table.get_item(Key={"cameraId": camera_id}).get("Item") if camera_id else None
        if not item:
            return {"statusCode": 400, "headers": CORS,
                    "body": json.dumps({"error": f"unknown camera '{camera_id}'"})}

        # A detection mode requires an ONVIF event subscription, which requires an ONVIF
        # host. Rejecting here rather than only hiding the option in the UI: the API is
        # reachable independently of the page, and a camera silently set to a mode it
        # cannot honour would look like a broken watcher later.
        if mode != "manual" and not item.get("onvifHost"):
            return {"statusCode": 400, "headers": CORS, "body": json.dumps(
                {"error": f"'{camera_id}' has no ONVIF host, so it supports only 'manual'"})}

        cameras_table.update_item(
            Key={"cameraId": camera_id},
            UpdateExpression="SET recordingMode = :m",
            ExpressionAttributeValues={":m": mode},
        )
        return {"statusCode": 200, "headers": CORS,
                "body": json.dumps({"cameraId": camera_id, "recordingMode": mode})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
