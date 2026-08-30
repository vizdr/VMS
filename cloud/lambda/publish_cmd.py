import boto3, os, json

REGION = os.environ["AWS_REGION"]
THING  = os.environ.get("THING_NAME", "adapter-01")
CMD_T  = f"adapter/{THING}/cmd"
ALLOWED_IR_MODES = {"AUTO", "ON", "OFF"}

CORS = {"Access-Control-Allow-Origin": "*"}
cameras_table = boto3.resource("dynamodb", region_name=REGION).Table("cameras")

def lambda_handler(event, context):
    # See get_hls_url.py for why every path returns through here rather than letting
    # an exception propagate -- API Gateway's own error response has no CORS headers.
    try:
        body = json.loads(event.get("body") or "{}")
        action = body.get("action")
        camera = body.get("camera", "cam-01")
        if action not in ("start", "stop", "ir"):
            return {
                "statusCode": 400,
                "headers": CORS,
                "body": json.dumps({"error": "action must be 'start', 'stop', or 'ir'"}),
            }
        cam_item = cameras_table.get_item(Key={"cameraId": camera}).get("Item")
        if not cam_item:
            return {
                "statusCode": 400,
                "headers": CORS,
                "body": json.dumps({"error": f"unknown camera '{camera}'"}),
            }

        payload = {"action": action, "camera": camera}
        if action == "ir":
            if not cam_item.get("hasIrControl"):
                return {
                    "statusCode": 400,
                    "headers": CORS,
                    "body": json.dumps({"error": f"camera '{camera}' has no IR control"}),
                }
            mode = body.get("mode")
            if mode not in ALLOWED_IR_MODES:
                return {
                    "statusCode": 400,
                    "headers": CORS,
                    "body": json.dumps({"error": "mode must be 'AUTO', 'ON', or 'OFF'"}),
                }
            payload["mode"] = mode

        # One shared command topic for the whole adapter (not one topic per camera) --
        # the camera field inside the payload is what agent.py uses to route to the
        # right systemd unit (kvs-cam01.service vs kvs-cam02.service) or ONVIF client.
        # The IoT policy's existing adapter/adapter-01/* wildcard already covers this
        # without changes.
        iot_data = boto3.client("iot-data", region_name=REGION)
        iot_data.publish(topic=CMD_T, qos=1, payload=json.dumps(payload))

        return {
            "statusCode": 200,
            "headers": CORS,
            "body": json.dumps({"published": action, "camera": camera, **({"mode": payload["mode"]} if action == "ir" else {})}),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": CORS,
            "body": json.dumps({"error": str(e)}),
        }
