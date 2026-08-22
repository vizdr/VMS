import boto3, os, json

REGION = os.environ["AWS_REGION"]
THING  = os.environ.get("THING_NAME", "adapter-01")
CMD_T  = f"adapter/{THING}/cmd"

CORS = {"Access-Control-Allow-Origin": "*"}

def lambda_handler(event, context):
    # See get_hls_url.py for why every path returns through here rather than letting
    # an exception propagate -- API Gateway's own error response has no CORS headers.
    try:
        body = json.loads(event.get("body") or "{}")
        action = body.get("action")
        if action not in ("start", "stop"):
            return {
                "statusCode": 400,
                "headers": CORS,
                "body": json.dumps({"error": "action must be 'start' or 'stop'"}),
            }

        iot_data = boto3.client("iot-data", region_name=REGION)
        iot_data.publish(topic=CMD_T, qos=1, payload=json.dumps({"action": action}))

        return {
            "statusCode": 200,
            "headers": CORS,
            "body": json.dumps({"published": action}),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": CORS,
            "body": json.dumps({"error": str(e)}),
        }
