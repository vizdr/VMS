"""Local ONVIF admin GUI -- discover, register, and control ONVIF cameras on this LAN.

Deliberately separate from client/index.html: WS-Discovery is UDP multicast, which only
works from a process on the same LAN segment as the cameras -- the cloud-hosted client
(served from S3, reached over the internet through CloudFront/API Gateway) has no route
to do that, and Lambda has no route to the LAN at all. So the *discovery and
registration* half of camera onboarding has to run here, on the Pi, not in the cloud.
Once a camera is registered (a row in the "cameras" DynamoDB table plus local systemd/
MediaMTX wiring), the existing cloud client and MQTT control plane pick it up with no
further change -- this app's job ends at registration and basic local control.

Access: LAN-only, no login. Consistent with this project's existing trust boundary --
MediaMTX's local RTSP endpoints and the camera's own ONVIF/RTSP services are equally
unauthenticated on this LAN. Not intended to be port-forwarded or exposed beyond it.
"""
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import camera_control
import onvif_discovery
from aws_device_creds import get_session

app = Flask(__name__, static_folder="static")

CAMERA_ID_RE = re.compile(r"^cam-\d{2}$")
MEDIAMTX_API = "http://127.0.0.1:9997"
REGION = "eu-central-1"


def cameras_table():
    return get_session(REGION).resource("dynamodb").Table("cameras")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/scan")
def scan():
    body = request.get_json(force=True, silent=True) or {}
    user, password = body.get("user"), body.get("password")

    services = onvif_discovery.scan(timeout=int(body.get("timeout", 4)))
    results = []
    for svc in services:
        xaddrs = svc.getXAddrs()
        scopes = [s.getValue() if hasattr(s, "getValue") else str(s) for s in svc.getScopes()]
        entry = {"xaddr": xaddrs[0] if xaddrs else None, "scopes": scopes}
        if user and xaddrs:
            try:
                entry["details"] = onvif_discovery.enrich_sync(xaddrs[0], user, password)
            except Exception as e:
                entry["enrichError"] = str(e)
        results.append(entry)
    return jsonify({"devices": results})


@app.get("/api/cameras")
def list_cameras():
    items = cameras_table().scan()["Items"]
    for i in items:
        i.pop("onvifPassword", None)  # never echo the credential back to the page
        # Every camera has a MediaMTX path regardless of mode (cam-01's transcode
        # pipeline publishes into MediaMTX exactly like cam-02's passthrough pull does),
        # so this is always derivable -- send it rather than have the page re-implement
        # the naming convention itself.
        i["mediamtxPath"] = camera_control.mediamtx_path_name(i["cameraId"])
    return jsonify({"cameras": sorted(items, key=lambda c: c["cameraId"])})


@app.post("/api/cameras")
def register_camera():
    body = request.get_json(force=True, silent=True) or {}
    camera_id = body.get("cameraId", "")
    host, port = body.get("host"), int(body.get("port", 80))
    user, password = body.get("user"), body.get("password")
    stream_uri = body.get("streamUri")
    has_ir_control = bool(body.get("hasIrControl"))

    if not CAMERA_ID_RE.match(camera_id):
        return jsonify({"error": "cameraId must look like 'cam-03'"}), 400
    if not all([host, user, password, stream_uri]):
        return jsonify({"error": "host, user, password, and streamUri are required"}), 400

    table = cameras_table()
    existing = table.get_item(Key={"cameraId": camera_id}).get("Item")
    mediamtx_path = camera_control.mediamtx_path_name(camera_id)

    if existing:
        # Re-register: an already-discovered device commonly IS an already-registered
        # camera (its ONVIF host doesn't change just because you scanned again), so this
        # updates the stored details -- credentials, RTSP URI if a DHCP lease moved the
        # IP -- rather than re-running the full provisioning flow. Deliberately does NOT
        # touch systemd: cam-01/cam-02 predate the kvs-cam@.service template and run as
        # their own individually-named units, so re-running provision-camera.sh against
        # them would create a second, conflicting producer for the same KVS stream
        # rather than updating the first. The MediaMTX path source is still safe to
        # refresh -- editing a path's config doesn't care which unit is feeding it.
        if existing.get("mode") != "passthrough" or not existing.get("onvifHost"):
            return jsonify({"error": f"'{camera_id}' exists but isn't a re-registerable ONVIF camera"}), 409

        r = requests.patch(
            f"{MEDIAMTX_API}/v3/config/paths/patch/{mediamtx_path}",
            json={"source": stream_uri, "sourceProtocol": "tcp"},
            timeout=5,
        )
        if not r.ok:
            return jsonify({"error": f"MediaMTX path update failed: {r.status_code} {r.text}"}), 502

        table.update_item(
            Key={"cameraId": camera_id},
            UpdateExpression=(
                "SET hasIrControl = :ir, onvifHost = :h, onvifPort = :p, "
                "onvifUser = :u, onvifPassword = :pw, rtspUrl = :uri, updatedAt = :t"
            ),
            ExpressionAttributeValues={
                ":ir": has_ir_control, ":h": host, ":p": port, ":u": user, ":pw": password,
                ":uri": stream_uri, ":t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        return jsonify({"cameraId": camera_id, "kvsStreamArn": existing["kvsStreamArn"], "updated": True}), 200

    # 1. MediaMTX path, live via its local API -- no YAML edit, no restart, so the
    #    other already-running cameras are undisturbed.
    r = requests.post(
        f"{MEDIAMTX_API}/v3/config/paths/add/{mediamtx_path}",
        json={"source": stream_uri, "sourceProtocol": "tcp"},
        timeout=5,
    )
    if not r.ok:
        return jsonify({"error": f"MediaMTX path add failed: {r.status_code} {r.text}"}), 502

    # 2. Local systemd wiring: env file for the kvs-cam@.service template, then
    #    daemon-reload + enable --now. See provision-camera.sh's own docstring for why
    #    this is a separate validated script rather than this process writing under
    #    /etc directly.
    provision = subprocess.run(
        ["sudo", str(Path(__file__).resolve().parent.parent / "bin" / "provision-camera.sh"),
         camera_id, mediamtx_path],
        capture_output=True, text=True,
    )
    if provision.returncode != 0:
        requests.post(f"{MEDIAMTX_API}/v3/config/paths/delete/{mediamtx_path}", timeout=5)
        return jsonify({"error": f"provisioning failed: {provision.stderr.strip()}"}), 500

    # 3. The KVS stream itself.
    kv = get_session(REGION).client("kinesisvideo")
    try:
        stream_arn = kv.create_stream(
            StreamName=camera_id, DataRetentionInHours=24, MediaType="video/h264",
        )["StreamARN"]
    except kv.exceptions.ResourceInUseException:
        stream_arn = kv.describe_stream(StreamName=camera_id)["StreamInfo"]["StreamARN"]

    # 4. The registry row -- Phase 1 of the credential-storage plan: plain attributes.
    #    Phase 2 (SSM Parameter Store SecureString + a credentialRef here instead of
    #    onvifPassword) is a deliberately deferred follow-up, not done in this pass.
    table.put_item(Item={
        "cameraId": camera_id,
        "mode": "passthrough",
        "hasIrControl": has_ir_control,
        "kvsStreamArn": stream_arn,
        "onvifHost": host,
        "onvifPort": port,
        "onvifUser": user,
        "onvifPassword": password,
        "rtspUrl": stream_uri,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    return jsonify({"cameraId": camera_id, "kvsStreamArn": stream_arn, "updated": False}), 201


@app.get("/api/cameras/<camera_id>/status")
def stream_status(camera_id):
    if "Item" not in cameras_table().get_item(Key={"cameraId": camera_id}):
        return jsonify({"error": "unknown camera"}), 404
    # Queries systemctl directly rather than tracking "what the last button click did" --
    # this is the actual ground truth (also catches the unit crashing or being stopped
    # by something other than this GUI, not just this GUI's own actions).
    return jsonify({"cameraId": camera_id, "state": camera_control.get_stream_status(camera_id)})


@app.post("/api/cameras/<camera_id>/ir")
def set_ir(camera_id):
    mode = (request.get_json(force=True, silent=True) or {}).get("mode")
    item = cameras_table().get_item(Key={"cameraId": camera_id}).get("Item")
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if not item.get("hasIrControl"):
        return jsonify({"error": "camera has no IR control"}), 400
    try:
        camera_control.set_ir_mode(item, mode)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"cameraId": camera_id, "irMode": mode})


@app.post("/api/cameras/<camera_id>/<action>")
def stream_action(camera_id, action):
    if action not in ("start", "stop"):
        return jsonify({"error": "action must be 'start' or 'stop'"}), 400
    if "Item" not in cameras_table().get_item(Key={"cameraId": camera_id}):
        return jsonify({"error": "unknown camera"}), 404
    camera_control.set_stream(camera_id, action == "start")
    return jsonify({"cameraId": camera_id, "action": action})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
