import boto3, os, json
from boto3.dynamodb.conditions import Key

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["BUCKET"]
DEFAULT_CAMERA = os.environ.get("STREAM_NAME", "cam-01")
# Paging ported from the successor's list-clips (deployed 2026-09-28) so both projects' pages
# page the same way. A caller that doesn't ask for a page size gets what this route always
# returned: the newest 50.
DEFAULT_LIMIT = MAX_LIMIT = 50

CORS = {"Access-Control-Allow-Origin": "*"}
cameras_table = boto3.resource("dynamodb", region_name=REGION).Table("cameras")

def bad_request(msg):
    return {"statusCode": 400, "headers": CORS, "body": json.dumps({"error": msg})}

def clip_keys(table, camera):
    """Every clip's startTs for the camera, newest first. Keys only, and all of them --
    that is what gives a page count and a jump to any page. One request per ~1 MB of items,
    i.e. one request for thousands of clips."""
    keys, query = [], dict(KeyConditionExpression=Key("cameraId").eq(camera),
                           ScanIndexForward=False, ProjectionExpression="startTs")
    while True:
        resp = table.query(**query)
        keys += [i["startTs"] for i in resp["Items"]]
        if "LastEvaluatedKey" not in resp:
            return keys
        query["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

def lambda_handler(event, context):
    try:
        params = event.get("queryStringParameters") or {}
        camera = params.get("camera", DEFAULT_CAMERA)
        if "Item" not in cameras_table.get_item(Key={"cameraId": camera}):
            return bad_request(f"unknown camera '{camera}'")

        # Paging: `limit` clips per page, newest first; `page` is 1-based, or "last".
        # Without `page`, just the newest `limit` clips.
        try:
            limit = int(params.get("limit", DEFAULT_LIMIT))
        except ValueError:
            return bad_request("limit must be an integer")
        if not 1 <= limit <= MAX_LIMIT:
            return bad_request(f"limit must be between 1 and {MAX_LIMIT}")
        page_param = params.get("page")
        if page_param is not None and page_param != "last":
            try:
                page_param = int(page_param)
            except ValueError:
                return bad_request("page must be a positive integer or 'last'")
            if page_param < 1:
                return bad_request("page must be a positive integer or 'last'")

        table = boto3.resource("dynamodb", region_name=REGION).Table("clips")
        # newest first -- ScanIndexForward=False on Query (not Scan) needs the sort key
        query = dict(KeyConditionExpression=Key("cameraId").eq(camera),
                     ScanIndexForward=False, Limit=limit)
        paging = {}
        if page_param is not None:
            keys = clip_keys(table, camera)
            pages = max(1, -(-len(keys) // limit))
            # A page past the end -- deletes shrank the list since the client last looked --
            # is the last page, not an error: the client shows whatever page it gets back.
            page = pages if page_param == "last" else min(page_param, pages)
            offset = (page - 1) * limit
            if offset:
                # Built here from the camera and a key just read, so it can't reach
                # another camera's clips.
                query["ExclusiveStartKey"] = {"cameraId": camera, "startTs": keys[offset - 1]}
            paging = {"page": page, "pages": pages, "total": len(keys)}
        items = table.query(**query).get("Items", [])

        # One head_object per clip to report its live storage tier -- fine at demo scale
        # (one page of clips), would need a different design (store tier in DynamoDB,
        # updated by an S3 Lifecycle -> EventBridge rule) before pages get large.
        s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com")
        clips = []
        for i in items:
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
                # "h264" / "h265", read from the clip itself when it was made
                # (record_clip.py, clip_to_s3.py, adapter/outage_uploader.py). Shown after
                # the duration; None only for a clip whose codec could not be read.
                "videoCodec": i.get("videoCodec"),
            })
        return {"statusCode": 200, "headers": CORS,
                "body": json.dumps({"clips": clips, **paging})}
    except Exception as e:
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
