import boto3, os, json

REGION = os.environ["AWS_REGION"]
CORS = {"Access-Control-Allow-Origin": "*"}

def lambda_handler(event, context):
    try:
        table = boto3.resource("dynamodb", region_name=REGION).Table("cameras")
        # Scan, not Query -- this table has no sort key and is expected to stay small
        # (a handful of cameras on one adapter), so a full scan is the right tool here.
        items = table.scan()["Items"]
        cameras = sorted(
            (
                {
                    "id": i["cameraId"],
                    "mode": i.get("mode"),
                    "hasIrControl": bool(i.get("hasIrControl", False)),
                }
                for i in items
            ),
            key=lambda c: c["id"],
        )
        return {"statusCode": 200, "headers": CORS, "body": json.dumps({"cameras": cameras})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
