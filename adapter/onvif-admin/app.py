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
import asyncio
import base64
import json
import re
import shutil
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import camera_control
import onvif_discovery
import usb_camera
from aws_device_creds import get_session
from outage_buffer import PIR_DIR, read_registry_cache
from pir_watcher import HEARTBEAT_FILE as PIR_HEARTBEAT

app = Flask(__name__, static_folder="static")

CAMERA_ID_RE = re.compile(r"^cam-\d{2}$")
MEDIAMTX_API = "http://127.0.0.1:9997"
REGION = config.AWS_REGION


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


@app.get("/api/usb-cameras")
def usb_cameras():
    """What USB cameras are attached and what each can do -- the "Scan USB cameras" button.

    The local counterpart of /api/scan: WS-Discovery finds networked ONVIF cameras, this
    finds the ones on this Pi's own USB bus. It is easier and more trustworthy than the
    ONVIF side -- no credentials, no multicast timeout, and the capabilities come from the
    driver rather than from what a vendor advertises.

    GET, not POST, and deliberately: it takes no input, changes nothing, and is safe to
    repeat. It also runs happily while the camera is streaming (every call is a read), so
    pressing the button never disturbs a live feed.
    """
    try:
        return jsonify(usb_camera.scan())
    except Exception as e:  # noqa: BLE001 -- a probe failure must read as a failed scan,
        return jsonify({"error": f"scan failed: {e}"}), 500   # not a 500 from Flask itself


CONFIGURE_SH = str(Path(__file__).resolve().parent.parent / "bin" / "configure-camera.sh")
USB_UNITS = ("kvs-camera-init.service", "kvs-camera-publish.service")
APPLY_VERIFY_SEC = 30


def _sudo_configure(*args):
    """Run the privileged config helper. Its stderr is the user-facing reason on failure."""
    p = subprocess.run(["sudo", CONFIGURE_SH, *args], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or "configure-camera.sh failed")
    return p.stdout


def _restart_usb_units():
    """Restart the camera units. These are systemd *user* units, so this runs unprivileged
    here rather than inside the sudo helper -- root cannot manage them at all."""
    for unit in USB_UNITS:
        subprocess.run(["systemctl", "--user", "restart", unit], capture_output=True,
                       text=True, timeout=60)


def _path_bytes(path):
    """MediaMTX's received-byte counter for a path, or None if it is not publishing."""
    try:
        r = requests.get(f"{MEDIAMTX_API}/v3/paths/get/{path}", timeout=3)
        if r.ok:
            st = r.json()
            if st.get("ready") and st.get("tracks"):
                return st.get("bytesReceived", 0)
    except requests.RequestException:
        pass
    return None


def _camera_is_live(path="cam01", budget=APPLY_VERIFY_SEC):
    """True once MediaMTX is actually *receiving bytes* on that path again.

    `systemctl is-active` proves nothing, and -- learned the hard way while building this
    -- neither does MediaMTX's `ready` flag: after a failed configuration the path sat at
    ready:true advertising an H264 track while no media arrived at all, and ffprobe could
    not decode a single frame. `ready` means a publisher connected and declared its
    tracks, which a pipeline can do and then stall. So require the byte counter to
    actually advance: that is the difference between a camera that says it is working and
    one that is.
    """
    deadline = time.monotonic() + budget
    first = None
    while time.monotonic() < deadline:
        n = _path_bytes(path)
        if n is not None:
            if first is None:
                first = n
            elif n > first:
                return True
        time.sleep(2)
    return False


def _sync_usb_registry():
    """Record the attached camera's hardware facts on the `cameras` row. Best effort.

    Never allowed to affect the camera: the registry is a *consumer* of this information,
    and the pipeline runs from /etc/adapter/cameras/cam01.env with no AWS involved at all.
    So a slow or unreachable account degrades to "not recorded", exactly as
    stream-codec.py treats the same problem -- returning the reason rather than raising.
    """
    try:
        snap = usb_camera.registry_snapshot()
        if not snap:
            return {"synced": False, "reason": "no camera selected"}
        item = cameras_table().get_item(Key={"cameraId": "cam-01"}).get("Item") or {}
        cameras_table().update_item(
            Key={"cameraId": "cam-01"},
            UpdateExpression=("SET audioCapable = :ac, cameraModel = :cm, cameraSerial = :cs, "
                              "usbCaps = :uc, usbCapsProbedAt = :ts"),
            ExpressionAttributeValues={
                ":ac": snap["audioCapable"], ":cm": snap["cameraModel"],
                ":cs": snap["cameraSerial"], ":uc": snap["usbCaps"], ":ts": snap["usbCapsProbedAt"],
            },
        )
        out = {"synced": True, "audioCapable": snap["audioCapable"],
               "cameraModel": snap["cameraModel"]}
        # A pin that no longer resolves is reported, never silently deleted: it is the
        # operator's setting, and publish-cam01.sh already ignores it safely. Saying so
        # here is what turns "audio quietly went missing" into something visible.
        pin = item.get("audioDevice")
        if pin and pin != snap["usbCaps"]["microphone"]:
            out["stalePin"] = (f"registry pins audioDevice={pin}, but this camera's "
                               f"microphone is {snap['usbCaps']['microphone'] or 'absent'}")
        return out
    except Exception as e:  # noqa: BLE001
        return {"synced": False, "reason": f"{type(e).__name__}: {e}"}


@app.post("/api/usb-cameras/control")
def usb_set_control():
    """Set one camera control live, without restarting anything.

    Separate from applying a configuration on purpose. This is for *finding* a value --
    nudge the exposure and watch the preview -- and it does not survive a restart, because
    camera-init.sh reapplies the config file. Saving is what makes it stick, and that path
    has the backup and the rollback precisely because it is the one that can break video.
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(usb_camera.set_control(body.get("name"), body.get("value")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.post("/api/usb-cameras/sync-registry")
def usb_sync_registry():
    """Record the attached camera's capabilities in the registry, on demand."""
    return jsonify(_sync_usb_registry())


@app.get("/api/usb-cameras/backups")
def usb_backups():
    """Saved configurations, newest first, each with when it was taken and which camera
    it describes -- so reverting is a choice between recognisable things."""
    try:
        rows = []
        for line in _sudo_configure("list", "cam01").splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                rows.append({"name": parts[0], "savedAt": parts[1], "model": parts[2]})
        return jsonify({"backups": rows})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


def _apply_and_verify(write, what):
    """Write a configuration, restart, and prove video came back -- or put it back.

    Automatic rollback is not a nicety here. cam-01's video and audio share one pipeline
    and every setting in this file feeds it, so a resolution the camera does not offer, or
    a microphone name that no longer exists, does not degrade the stream -- it removes the
    camera. Without this, a GUI mistake would cost the camera until someone with a
    terminal noticed, and the person who made the mistake is exactly the person who cannot
    fix it from here.
    """
    # Before the write: `apply` creates the backup, so a list taken afterwards already
    # contains it and the "what did we just displace?" diff comes back empty -- which is
    # exactly how the first version of this failed, leaving a broken camera in place with
    # "no backup to roll back to".
    before = _sudo_configure("list", "cam01").splitlines()
    write()
    _restart_usb_units()
    if _camera_is_live():
        return jsonify({"ok": True, "applied": what, "verified": "video is flowing again",
                        "registry": _sync_usb_registry()})

    # It did not come back. The newest backup is the configuration we just displaced.
    after = _sudo_configure("list", "cam01").splitlines()
    fresh = [l.split("\t")[0] for l in after if l not in before]
    if not fresh:
        return jsonify({"ok": False, "applied": what, "rolledBack": False,
                        "error": f"no video within {APPLY_VERIFY_SEC}s and no backup to "
                                 "roll back to -- the camera is left with the new settings"}), 500
    _sudo_configure("revert", "cam01", fresh[0])
    _restart_usb_units()
    recovered = _camera_is_live()
    return jsonify({"ok": False, "applied": what, "rolledBack": True, "restored": fresh[0],
                    "recovered": recovered,
                    "error": f"no video within {APPLY_VERIFY_SEC}s -- the previous "
                             f"configuration was restored and video "
                             f"{'is back' if recovered else 'did NOT come back either'}"}), 409


@app.post("/api/usb-cameras/apply")
def usb_apply():
    """Write the configuration the panel previewed, then prove the camera still works.

    The body is the file itself, not a set of fields: the panel already renders exactly
    what should be written, and re-deriving it here would be a second generator free to
    disagree with the preview. It is safe because configure-camera.sh validates every line
    against a whitelist and refuses anything else -- the text is checked, not trusted.
    """
    body = request.get_json(force=True, silent=True) or {}
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return jsonify({"error": "content (the configuration file) is required"}), 400

    tmp = Path(tempfile.gettempdir()) / f"cam01-apply-{os.getpid()}.env"
    try:
        tmp.write_text(content)
        return _apply_and_verify(lambda: _sudo_configure("apply", "cam01", str(tmp)),
                                 "new configuration")
    except RuntimeError as e:
        # Validation refused it. Nothing was written, so there is nothing to undo.
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/usb-cameras/revert")
def usb_revert():
    """Restore a saved configuration, with the same verification as applying one."""
    name = (request.get_json(force=True, silent=True) or {}).get("backup")
    if not isinstance(name, str) or not name:
        return jsonify({"error": "backup name is required"}), 400
    try:
        return _apply_and_verify(lambda: _sudo_configure("revert", "cam01", name),
                                 f"backup {name}")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


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

        # Only patch when the source actually changed. MediaMTX decides whether a config
        # update can be applied in place or needs the path closed and recreated
        # (core.pathConfCanBeUpdated): `source` is NOT in its in-place whitelist, so
        # patching it disconnects every reader -- including a running KVS producer, and
        # any outage-buffer recording mid-segment. A re-registration that changes nothing
        # (the common case: same camera, same IP, user just re-scanned) should cost
        # nothing. When the URI really has moved, the restart is correct and unavoidable.
        #
        # `rtspTransport`, not `sourceProtocol`: the latter is a deprecated alias that
        # MediaMTX still accepts but may drop. Both were verified live to apply in place
        # when the value is unchanged.
        current = requests.get(
            f"{MEDIAMTX_API}/v3/config/paths/get/{mediamtx_path}", timeout=5
        )
        unchanged = current.ok and current.json().get("source") == stream_uri

        if not unchanged:
            r = requests.patch(
                f"{MEDIAMTX_API}/v3/config/paths/patch/{mediamtx_path}",
                json={"source": stream_uri, "rtspTransport": "tcp"},
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
        json={"source": stream_uri, "rtspTransport": "tcp"},
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
        # DELETE, not POST: MediaMTX answers POST on this route with "404 page not
        # found", and the response was never checked -- so a failed registration
        # always left an orphan path behind (FoundAndFixed.md #36).
        requests.delete(f"{MEDIAMTX_API}/v3/config/paths/delete/{mediamtx_path}", timeout=5)
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
    # Polled every few seconds per camera, so the ID is checked against the supervisor's local
    # registry cache, not DynamoDB: this LAN page must keep answering with the internet down.
    # A lookup per poll hung ~36 s each while DNS was failing (an unresolvable name isn't bound
    # by urlopen's timeout), and six of them filled the browser's connection pool and froze the
    # whole page (FoundAndFixed.md #55). DynamoDB only on a Pi without the cache.
    known = read_registry_cache()
    if known:
        if camera_id not in known:
            return jsonify({"error": "unknown camera"}), 404
    elif "Item" not in cameras_table().get_item(Key={"cameraId": camera_id}):
        return jsonify({"error": "unknown camera"}), 404
    # Queries systemctl directly rather than tracking "what the last button click did" --
    # this is the actual ground truth (also catches the unit crashing or being stopped
    # by something other than this GUI, not just this GUI's own actions).
    return jsonify({"cameraId": camera_id, "state": camera_control.get_stream_status(camera_id)})


@app.get("/api/cameras/<camera_id>/analytics")
def get_analytics(camera_id):
    """Read the camera's motion-analytics tuning (Phase 4).

    Read-only, and not by choice: `SetVideoAnalyticsConfiguration` is a **silent no-op**
    on this camera. It returns success and changes nothing -- verified by sending
    Sensitivity=55 and reading back 80 immediately after, twice. The advertised ONVIF 2.0
    analytics service (`ver20/analytics/wsdl`) exposes no callable operations either, and
    the vendor ISAPI path 404s. So these values can only be changed in the camera's own
    web UI; surfacing them here at least explains *why* detection behaves as it does.
    """
    item = cameras_table().get_item(Key={"cameraId": camera_id}).get("Item")
    if not item or not item.get("onvifHost"):
        return jsonify({"error": "unknown or non-ONVIF camera"}), 404

    async def _read():
        from onvif import ONVIFCamera
        cam = ONVIFCamera(item["onvifHost"], int(item.get("onvifPort", 80)),
                          item["onvifUser"], item["onvifPassword"],
                          wsdl_dir=camera_control.WSDL_DIR)
        await cam.update_xaddrs()
        media = await cam.create_media_service()
        vac = (await media.GetVideoAnalyticsConfigurations())[0]
        mod = vac.AnalyticsEngineConfiguration.AnalyticsModule[0]
        rule = vac.RuleEngineConfiguration.Rule[0]
        out = {
            "configName": vac.Name,
            "module": {"name": mod.Name, "type": mod.Type,
                       "params": {s.Name: s.Value for s in mod.Parameters.SimpleItem}},
            "rule": {"name": rule.Name, "type": rule.Type,
                     "params": {s.Name: s.Value for s in rule.Parameters.SimpleItem}},
            "writable": False,
            "readOnlyReason": "SetVideoAnalyticsConfiguration is a no-op on this camera "
                              "(accepted, ignored) — change these in the camera's web UI",
        }
        await cam.close()
        return out

    try:
        data = asyncio.run(_read())
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}"}), 502

    # ActiveCells is a base64 bitmap of enabled detection cells. The camera returns an
    # empty Layout element, so it never tells us the grid's Columns/Rows -- report the
    # bit count and let the reader draw their own conclusion rather than inventing a
    # geometry that might be wrong.
    cells = data["rule"]["params"].get("ActiveCells")
    if cells:
        try:
            raw = base64.b64decode(cells)
            data["activeCells"] = {
                "base64": cells,
                "hex": raw.hex(),
                "bits": "".join(f"{b:08b}" for b in raw),
                "cellCount": len(raw) * 8,
                "gridGeometry": "unknown — camera returns an empty Layout element",
            }
        except Exception:
            pass
    return jsonify(data)


@app.post("/api/cameras/<camera_id>/mode")
def set_recording_mode(camera_id):
    """Phase 1 (Camera-Features.md §9) -- writes recordingMode to the registry only.

    Mirrors the cloud `set-camera-mode` Lambda deliberately: same validation, same
    allowed values, same ONVIF-host requirement. Both write the one registry row, so
    whichever GUI you use, the other sees it on next refresh.
    """
    VALID = {"manual", "motion", "cellMotion", "human"}
    mode = (request.get_json(force=True, silent=True) or {}).get("recordingMode")
    if mode not in VALID:
        return jsonify({"error": f"recordingMode must be one of {sorted(VALID)}"}), 400

    table = cameras_table()
    item = table.get_item(Key={"cameraId": camera_id}).get("Item")
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if mode != "manual" and not item.get("onvifHost"):
        return jsonify({"error": "no ONVIF host — this camera supports only 'manual'"}), 400

    table.update_item(
        Key={"cameraId": camera_id},
        UpdateExpression="SET recordingMode = :m",
        ExpressionAttributeValues={":m": mode},
    )
    return jsonify({"cameraId": camera_id, "recordingMode": mode})


@app.post("/api/cameras/<camera_id>/audio")
def set_audio(camera_id):
    """Writes audioEnabled to the registry. Mirrors the cloud `set-camera-audio` Lambda.

    Takes effect on the camera's next Start, not immediately: the producer reads the flag
    once at launch (adapter/bin/camera-audio.py) because KVS refuses a stream whose
    fragments change from video-only to audio+video partway through.
    """
    enabled = (request.get_json(force=True, silent=True) or {}).get("audioEnabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "audioEnabled must be true or false"}), 400

    table = cameras_table()
    item = table.get_item(Key={"cameraId": camera_id}).get("Item")
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if enabled and not item.get("audioCapable"):
        return jsonify({"error": "camera has no usable audio source"}), 400

    table.update_item(
        Key={"cameraId": camera_id},
        UpdateExpression="SET audioEnabled = :a",
        ExpressionAttributeValues={":a": enabled},
    )
    return jsonify({"cameraId": camera_id, "audioEnabled": enabled, "appliesOn": "next start"})


@app.post("/api/cameras/<camera_id>/outage-buffer")
def set_outage_buffer(camera_id):
    """Writes outageBufferSec. Mirrors the cloud `set-camera-outage-buffer` Lambda.

    Unlike the audio flag this needs no restart: kvs-outage-buffer.service re-reads the
    registry every 60s and arms or disarms MediaMTX recording in place. It only has any
    effect while the camera's KVS producer is running, though -- with no producer there is
    no cloud stream to protect, and that gate is what keeps the rolling pre-roll off the
    USB stick 24/7.
    """
    VALID = {0, 30, 120, 300, 600, 1800, 3600, 18000, 43200, 86400}
    secs = (request.get_json(force=True, silent=True) or {}).get("outageBufferSec")
    if not isinstance(secs, int) or isinstance(secs, bool) or secs not in VALID:
        return jsonify({"error": f"outageBufferSec must be one of {sorted(VALID)}"}), 400

    table = cameras_table()
    if "Item" not in table.get_item(Key={"cameraId": camera_id}):
        return jsonify({"error": "unknown camera"}), 404

    table.update_item(
        Key={"cameraId": camera_id},
        UpdateExpression="SET outageBufferSec = :s",
        ExpressionAttributeValues={":s": secs},
    )
    return jsonify({"cameraId": camera_id, "outageBufferSec": secs, "appliesOn": "within 60s"})


@app.post("/api/cameras/<camera_id>/pir")
def set_pir_recording(camera_id):
    """Writes pirRecording, the PIR trigger's on/off switch (PIR-MQTT-VMS-PI4.md §3.8).

    A separate flag, not a fifth recordingMode value, deliberately: the cameras table is
    shared with the successor project, whose code knows exactly four modes. A new field is
    invisible to it; a new enum value would reach it (§4.2, data contract rule 2). So this
    never touches recordingMode -- for cam-01 that stays "manual".

    Switching on needs pirTopic, the capability saying a PIR sensor publishes for this
    camera; switching off is always allowed. The outage supervisor arms the ring (in RAM since
    Phase 12) and the PIR watcher starts acting on motion, both within about a minute.
    """
    enabled = (request.get_json(force=True, silent=True) or {}).get("pirRecording")
    if not isinstance(enabled, bool):
        return jsonify({"error": "pirRecording must be true or false"}), 400

    table = cameras_table()
    item = table.get_item(Key={"cameraId": camera_id}).get("Item")
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if enabled and not item.get("pirTopic"):
        return jsonify({"error": "no PIR sensor configured for this camera (pirTopic)"}), 400
    if bool(item.get("pirRecording")) == enabled:
        # Nothing to change -- and no write, so "off" on a camera without a PIR sensor
        # doesn't plant a pirRecording field in a row the successor also reads.
        return jsonify({"cameraId": camera_id, "pirRecording": enabled})

    table.update_item(
        Key={"cameraId": camera_id},
        UpdateExpression="SET pirRecording = :p",
        ExpressionAttributeValues={":p": enabled},
    )
    return jsonify({"cameraId": camera_id, "pirRecording": enabled})


# --- PIR panel and clips (PIR-MQTT-VMS-PI4.md Phases 8, 10) ---------------------------------
#
# Read-mostly views over what kvs-pir-watcher writes, plus the two markers this app owns
# (keep, upload-requested -- one writer per file, §3.6). Clips are served from the stick for
# the page's <video>; nothing here talks to AWS except the registry fallback for a topic.
# Cloud upload requests arrive as the uploader's local record of them (cloud-request.json,
# §3.11), so this page shows them without AWS, and offline.

SESSION_ID_RE = re.compile(r"^cam-\d{2}-\d{8}T\d{6}Z(-\d+)?$")   # no path can escape PIR_DIR
PIR_STALE_SEC = 30          # three missed 10 s heartbeats: the watcher's view is *unknown*


@app.get("/api/pir")
def pir_status():
    """The watcher's heartbeat, with its age. Stale means unknown, never "fine" (aws_state.py)."""
    try:
        h = json.loads(PIR_HEARTBEAT.read_text())
    except FileNotFoundError:
        return jsonify({"available": False, "reason": "no heartbeat file -- is kvs-pir-watcher running?"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"available": False, "reason": f"unreadable heartbeat ({type(e).__name__})"})
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(h["ts"])).total_seconds()
    return jsonify({**h, "available": True, "ageSec": round(age, 1), "stale": age > PIR_STALE_SEC})


def _pir_base(camera_id):
    """'home/pico2w-01' for this camera: from the watcher's heartbeat (works offline), else
    from the registry."""
    try:
        topic = json.loads(PIR_HEARTBEAT.read_text())["cameras"][camera_id]["pirTopic"]
    except Exception:  # noqa: BLE001
        item = cameras_table().get_item(Key={"cameraId": camera_id}).get("Item") or {}
        topic = item.get("pirTopic")
    return topic[:-len("/pir/event")] if topic and topic.endswith("/pir/event") else None


@app.post("/api/cameras/<camera_id>/pir/retrigger")
def pir_retrigger(camera_id):
    """Send the PIR module's trigger mode to the Pico, as MQTT user vms (the ACL lets vms --
    and only vms -- write pir/cmd/#). The Pico doesn't report its mode, so the panel shows the
    last command the watcher saw, and a Pico reboot returns it to retriggerable."""
    on = (request.get_json(force=True, silent=True) or {}).get("retriggerable")
    if not isinstance(on, bool):
        return jsonify({"error": "retriggerable must be true or false"}), 400
    base = _pir_base(camera_id)
    if not base:
        return jsonify({"error": "no PIR sensor configured for this camera (pirTopic)"}), 400
    import paho.mqtt.publish as publish
    try:
        publish.single(f"{base}/pir/cmd/retrigger", "1" if on else "0", qos=1,
                       hostname=config.get("MQTT_HOST"), port=int(config.get("MQTT_PORT")),
                       auth={"username": config.get("MQTT_USER"),
                             "password": Path(config.get("MQTT_PASSWORD_FILE")).read_text().strip()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"broker: {type(e).__name__}: {e}"}), 502
    return jsonify({"cameraId": camera_id, "retriggerable": on, "topic": f"{base}/pir/cmd/retrigger"})


def _pir_session_dir(sid):
    if not SESSION_ID_RE.match(sid) or not (PIR_DIR / sid / "state.json").exists():
        abort(404)
    return PIR_DIR / sid


def _pir_session(sdir):
    j = json.loads((sdir / "state.json").read_text())
    def _read(name):
        try:
            return json.loads((sdir / name).read_text())
        except Exception:  # noqa: BLE001
            return None
    uploaded, cloud_request = _read("uploaded.json"), _read("cloud-request.json")
    keys = ("sessionId", "cameraId", "status", "from", "to", "t0", "durationSec", "window",
            "reason", "sizeBytes", "videoCodec", "error")
    return {**{k: j.get(k) for k in keys},
            "hasClip": (sdir / "clip.mp4").exists(), "hasThumb": (sdir / "thumb-1.jpg").exists(),
            "kept": (sdir / "keep").exists(),
            "uploadRequested": (sdir / "upload-requested").exists(), "uploaded": uploaded,
            "cloudRequest": cloud_request}


PIR_PAGE_MAX = 50


@app.get("/api/pir-clips")
def pir_clips():
    """One page of the stick's PIR sessions, newest first -- the same contract as the cloud's
    list-clips and GET /pir/clips: `limit` per page, `page` 1-based or "last", and `page`,
    `pages`, `total` in the reply. A page past the end (retention deleted clips meanwhile) is
    the last page."""
    try:
        limit = int(request.args.get("limit", 10))
        page = request.args.get("page", "1")
        page = page if page == "last" else int(page)
    except ValueError:
        return jsonify({"error": "limit and page must be integers (page may be 'last')"}), 400
    if not 1 <= limit <= PIR_PAGE_MAX or (page != "last" and page < 1):
        return jsonify({"error": f"limit must be 1-{PIR_PAGE_MAX}, page at least 1"}), 400
    found = []
    for path in PIR_DIR.glob("*/state.json") if PIR_DIR.is_dir() else []:
        if SESSION_ID_RE.match(path.parent.name):
            try:
                found.append((float(json.loads(path.read_text()).get("from") or 0), path.parent))
            except Exception:  # noqa: BLE001 -- a half-written journal shows up next refresh
                pass
    found.sort(key=lambda f: f[0], reverse=True)
    pages = max(1, -(-len(found) // limit))
    page = pages if page == "last" else min(page, pages)
    clips = []
    for _, sdir in found[(page - 1) * limit: page * limit]:
        try:
            clips.append(_pir_session(sdir))
        except Exception:  # noqa: BLE001 -- deleted or half-written between listing and reading
            pass
    return jsonify({"clips": clips, "page": page, "pages": pages, "total": len(found)})


@app.get("/api/pir-clips/<sid>/clip.mp4")
def pir_clip_file(sid):
    f = _pir_session_dir(sid) / "clip.mp4"
    if not f.exists():
        abort(404)
    # conditional=True answers Range requests with 206 -- a <video> needs that to seek.
    return send_file(f, mimetype="video/mp4", conditional=True)


@app.get("/api/pir-clips/<sid>/thumb-1.jpg")
def pir_thumb_file(sid):
    f = _pir_session_dir(sid) / "thumb-1.jpg"
    if not f.exists():
        abort(404)
    return send_file(f, mimetype="image/jpeg", conditional=True)


@app.post("/api/pir-clips/<sid>/keep")
def pir_keep(sid):
    """The `keep` marker: local retention (14 days, then the disk floor) never deletes it."""
    keep = (request.get_json(force=True, silent=True) or {}).get("keep")
    if not isinstance(keep, bool):
        return jsonify({"error": "keep must be true or false"}), 400
    marker = _pir_session_dir(sid) / "keep"
    marker.touch() if keep else marker.unlink(missing_ok=True)
    return jsonify({"sessionId": sid, "kept": keep})


@app.post("/api/pir-clips/<sid>/upload")
def pir_upload(sid):
    """Request an upload (or {"cancel": true} withdraw one not yet done). kvs-outage-uploader
    picks the marker up within 30 s; the local clip stays (D10)."""
    sdir = _pir_session_dir(sid)
    s = _pir_session(sdir)
    marker = sdir / "upload-requested"
    if (request.get_json(force=True, silent=True) or {}).get("cancel"):
        if s["uploaded"]:
            return jsonify({"error": "already in AWS -- delete it in the cloud clip list"}), 409
        if s["cloudRequest"]:
            return jsonify({"error": "requested from the cloud page -- the uploader is already on it"}), 409
        marker.unlink(missing_ok=True)
        return jsonify({"sessionId": sid, "state": "local only"})
    if s["uploaded"]:
        return jsonify({"sessionId": sid, "state": "in AWS", "s3Key": s["uploaded"]["s3Key"]})
    if s["status"] != "merged" or not s["hasClip"]:
        return jsonify({"error": f"the clip isn't ready (status {s['status']})"}), 409
    marker.touch()
    return jsonify({"sessionId": sid, "state": "requested", "appliesOn": "within 30 s"})


@app.delete("/api/pir-clips/<sid>")
def pir_delete(sid):
    """Delete the local session. Refused while it is still being recorded or assembled, and
    while an upload is pending (the uploader may be reading the file). The copy in AWS, if
    any, stays -- an upload is a copy (D10)."""
    sdir = _pir_session_dir(sid)
    s = _pir_session(sdir)
    if s["status"] not in ("merged", "failed"):
        return jsonify({"error": f"still {s['status']} -- wait until the clip is assembled"}), 409
    if s["uploadRequested"] and not s["uploaded"]:
        return jsonify({"error": "an upload is pending -- cancel it first"}), 409
    if s["cloudRequest"] and not s["uploaded"]:
        return jsonify({"error": "an upload requested from the cloud page is pending"}), 409
    shutil.rmtree(sdir)
    return jsonify({"deleted": sid, "inAws": (s["uploaded"] or {}).get("s3Key")})


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
