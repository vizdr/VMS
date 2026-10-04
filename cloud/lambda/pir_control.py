"""pir-control -- this project's cloud side of the PIR trigger (PIR-MQTT-VMS-PI4.md §3.9-§3.11).

Behind pir-api, which only this repository deploys (the successor never sees it):

  GET  /pir?camera=cam-01                  the switch (cameras.pirRecording, pirTopic) and the
                                           Pi's status item from pir-local
  GET  /pir/clips?camera=cam-01[&page=N|last][&limit=10][&deleted=1]
                                           the Pi's local clip index, newest first, paged
                                           like list-clips (page, pages, total); clips
                                           deleted on the Pi only with deleted=1
  POST /pir/mode    {"camera", "enabled"}  writes pirRecording -- and nothing else
  POST /pir/upload  {"camera", "startTs"}  asks the Pi to upload one local clip

The cameras table is shared with the successor, so the only write there is a field-level
update of pirRecording (data contract §4.3, rules 1-2; the role's policy enforces it too).
pir-local is this project's own table: the Pi writes the status and the clip index (Phase 10),
this function writes only uploadRequestedAt/requestedBy. Every write logs the Cognito user:
restricting PIR control to a group (extra 3) is postponed, and the log prepares it.
"""
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

REGION = os.environ["AWS_REGION"]
LOCAL_TABLE = os.environ.get("PIR_TABLE", "pir-local")
DEFAULT_LIMIT = MAX_LIMIT = 50

CORS = {"Access-Control-Allow-Origin": "*"}
ddb = boto3.resource("dynamodb", region_name=REGION)
cameras = ddb.Table("cameras")
local = ddb.Table(LOCAL_TABLE)


def _plain(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(type(o).__name__)


def resp(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body, default=_plain)}


def who(event):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    return claims.get("cognito:username") or claims.get("email") or "unknown"


def lambda_handler(event, context):
    # Every path returns through resp(): API Gateway's own error response has no CORS
    # headers (see get_hls_url.py), so an exception must never escape.
    try:
        route = (event.get("httpMethod"), event.get("resource"))
        q = event.get("queryStringParameters") or {}
        body = json.loads(event.get("body") or "{}")
        if route == ("GET", "/pir"):
            return get_pir(q.get("camera", "cam-01"))
        if route == ("GET", "/pir/clips"):
            return get_clips(q.get("camera", "cam-01"), q)
        if route == ("POST", "/pir/mode"):
            return set_mode(body, who(event))
        if route == ("POST", "/pir/upload"):
            return request_upload(body, who(event))
        return resp(404, {"error": f"no route {route[0]} {route[1]}"})
    except Exception as e:  # noqa: BLE001
        return resp(500, {"error": str(e)})


def get_pir(camera):
    item = cameras.get_item(Key={"cameraId": camera}).get("Item")
    if not item:
        return resp(404, {"error": f"unknown camera '{camera}'"})
    status = local.get_item(Key={"cameraId": camera, "sk": "status"}).get("Item")
    return resp(200, {
        "camera": camera,
        "pirTopic": item.get("pirTopic"),
        "pirRecording": bool(item.get("pirRecording")),
        "status": status,                 # None until the Pi reports (Phase 10)
    })


def clip_keys(camera, now):
    """Every live clip row's sort key, newest first, with whether it is deleted on the Pi. Keys
    only and all of them, as list-clips does: that gives a page count, a total and a jump to
    any page. The rows carry thumbnails (~3.5 KB), so this reads ~1 MB per ~300 clips -- a few
    requests for the 14 days the Pi keeps.

    A row past its `ttl` is left out: DynamoDB deletes expired items only eventually ("within a
    few days"), and until then a query still returns them. So a deleted clip leaves the list one
    day after its deletion, as the Pi intended (pir_cloud.DELETED_TTL_SEC), not days later."""
    keys = []
    query = {"KeyConditionExpression": Key("cameraId").eq(camera) & Key("sk").begins_with("clip#"),
             "ScanIndexForward": False, "ProjectionExpression": "sk, deletedAt, #ttl",
             "ExpressionAttributeNames": {"#ttl": "ttl"}}   # TTL is a reserved word
    while True:
        r = local.query(**query)
        for item in r["Items"]:
            if "ttl" in item and int(item["ttl"]) <= now:
                continue
            keys.append((item["sk"], "deletedAt" in item))
        if "LastEvaluatedKey" not in r:
            return keys
        query["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def get_clips(camera, q):
    """One page, newest first. `page` is 1-based or "last" (default 1); a page past the end --
    the Pi's retention deleted clips since the page last looked -- is the last page.

    Clips deleted on the Pi are left out unless `deleted=1`: a burst of deletions would otherwise
    fill page after page with rows nobody can act on. Their number comes back as `deleted`
    either way, so the page can offer to show them; `onPi` counts the rest, and `total` is what
    the pages cover."""
    try:
        limit = int(q.get("limit", DEFAULT_LIMIT))
    except ValueError:
        return resp(400, {"error": "limit must be an integer"})
    if not 1 <= limit <= MAX_LIMIT:
        return resp(400, {"error": f"limit must be between 1 and {MAX_LIMIT}"})
    page_param = q.get("page", "1")
    if page_param != "last":
        try:
            page_param = int(page_param)
        except ValueError:
            return resp(400, {"error": "page must be a positive integer or 'last'"})
        if page_param < 1:
            return resp(400, {"error": "page must be a positive integer or 'last'"})
    show_deleted = q.get("deleted") == "1"
    keys = clip_keys(camera, int(time.time()))
    deleted = sum(1 for _, d in keys if d)
    listed = [sk for sk, d in keys if show_deleted or not d]
    pages = max(1, -(-len(listed) // limit))
    page = pages if page_param == "last" else min(page_param, pages)
    wanted = listed[(page - 1) * limit: page * limit]
    items = []
    if wanted:
        # The page's rows are no longer adjacent in the table once deleted ones are hidden, so
        # read the key range they span and keep just them. Built from keys read above, so it
        # can't reach another camera's rows.
        query = {"KeyConditionExpression": Key("cameraId").eq(camera) & Key("sk").between(wanted[-1], wanted[0]),
                 "ScanIndexForward": False}
        keep = set(wanted)
        while True:
            r = local.query(**query)
            items += [i for i in r["Items"] if i["sk"] in keep]
            if "LastEvaluatedKey" not in r:
                break
            query["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return resp(200, {"camera": camera, "clips": items, "page": page, "pages": pages,
                      "total": len(listed), "onPi": len(keys) - deleted, "deleted": deleted,
                      "showingDeleted": show_deleted})


def set_mode(body, user):
    camera, enabled = body.get("camera"), body.get("enabled")
    if not camera or not isinstance(enabled, bool):
        return resp(400, {"error": "camera and enabled (true/false) are required"})
    item = cameras.get_item(Key={"cameraId": camera}).get("Item")
    if not item:
        return resp(404, {"error": f"unknown camera '{camera}'"})
    if enabled and not item.get("pirTopic"):
        return resp(400, {"error": "no PIR sensor configured for this camera (pirTopic)"})
    if bool(item.get("pirRecording")) != enabled:   # nothing changes -> nothing written
        cameras.update_item(
            Key={"cameraId": camera},
            UpdateExpression="SET pirRecording = :p",
            ConditionExpression="attribute_exists(cameraId)",
            ExpressionAttributeValues={":p": enabled},
        )
    print(json.dumps({"action": "pir-mode", "camera": camera, "enabled": enabled, "user": user}))
    return resp(200, {"camera": camera, "pirRecording": enabled, "appliesOn": "within 60 s"})


def request_upload(body, user):
    camera, start = body.get("camera"), body.get("startTs")
    if not camera or not start:
        return resp(400, {"error": "camera and startTs are required"})
    try:
        local.update_item(
            Key={"cameraId": camera, "sk": f"clip#{start}"},
            UpdateExpression="SET uploadRequestedAt = :t, requestedBy = :u",
            ConditionExpression="attribute_exists(sk) AND attribute_not_exists(deletedAt)",
            ExpressionAttributeValues={":t": datetime.now(timezone.utc).isoformat(), ":u": user},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return resp(404, {"error": "no such clip on the Pi (deleted, or not indexed yet)"})
        raise
    print(json.dumps({"action": "pir-upload", "camera": camera, "startTs": start, "user": user}))
    return resp(200, {"camera": camera, "startTs": start, "state": "requested",
                      "appliesOn": "the Pi checks every 30 s"})
