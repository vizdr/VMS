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
import copy
import json
import re
import shutil
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import camera_control
import live_view
import mediamtx_api
import onvif_discovery
import onvif_media2
import usb_camera
from aws_device_creds import get_session
from outage_buffer import PIR_DIR, read_registry_cache
from pir_watcher import HEARTBEAT_FILE as PIR_HEARTBEAT

app = Flask(__name__, static_folder="static")

CAMERA_ID_RE = re.compile(r"^cam-\d{2}$")
# A camera's display name ("Kitchen", "Hall-1", "Küche"): what people read in both GUIs. The
# cameraId stays the key everywhere else (KVS, S3, DynamoDB, MediaMTX, units), so a name is
# only ever displayed (guide §16.2.2). Letters and digits in any script, space, - _ . -- nothing
# that HTML or a shell would read as syntax: both pages render it, and this page has no login.
DISPLAY_NAME_RE = re.compile(r"^[\w .-]{1,32}$")
DISPLAY_NAME_MAX = 32
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


@app.get("/api/usb-cameras/mic-gain")
def usb_get_mic_gain():
    """The webcam microphone's gain: range, step and current value (usb_camera.mic_gain)."""
    try:
        out = usb_camera.mic_gain()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    return jsonify({**out, "savedDb": _config_value("MIC_GAIN_DB")})


@app.post("/api/usb-cameras/mic-gain")
def usb_set_mic_gain():
    """Set the microphone gain {"gainDb": -6} live and save it in cam01.env as MIC_GAIN_DB.

    Both at once, unlike the video controls' Set now / Apply: a gain is a mixer setting that
    needs no restart and can't break video, so there is nothing to try before keeping. The file
    goes through configure-camera.sh like every other edit (validated, previous one backed up),
    and camera-init.sh reapplies it at every start. Recordings and clips carry the new level from
    now on; the browser's own player volume only changes what one listener hears.
    """
    db = (request.get_json(force=True, silent=True) or {}).get("gainDb")
    if not isinstance(db, (int, float)) or isinstance(db, bool):
        return jsonify({"error": "gainDb must be a number"}), 400
    try:
        out = usb_camera.set_mic_gain(db)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        _save_config_value("MIC_GAIN_DB", f"{out['gainDb']:g}")
        out["saved"] = True
    except Exception as e:  # noqa: BLE001 -- set live already; say why it won't survive a restart
        out["saved"], out["saveError"] = False, str(e)
    return jsonify(out)


def _config_value(key, path="cam01"):
    try:
        for line in open(os.path.join(usb_camera.CONFIG_DIR, f"{path}.env")):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return None


def _save_config_value(key, value, path="cam01"):
    """Set one KEY=VALUE in the camera's config file, every other line kept, through the
    privileged helper (validation, backup, atomic write)."""
    try:
        lines = open(os.path.join(usb_camera.CONFIG_DIR, f"{path}.env")).read().splitlines()
    except FileNotFoundError:
        lines = [f"# {path}: written by the admin GUI"]
    lines = [l for l in lines if not l.startswith(key + "=")] + [f"{key}={value}"]
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("\n".join(lines) + "\n")
    try:
        _sudo_configure("apply", path, f.name)
    finally:
        os.unlink(f.name)


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
        # The local preview is live view, so it plays the sub stream's path when the camera
        # records its main stream (live_view.py, guide §21.7). The URI itself carries
        # credentials and the page doesn't need it.
        i["previewPath"] = (camera_control.live_path_name(i["cameraId"])
                            if i.pop("liveRtspUrl", None) else i["mediamtxPath"])
    # Unregistered cameras too, with their unregisteredAt: the page leaves them out of the
    # table and needs them only to offer Re-register on a matching discovered device.
    return jsonify({"cameras": sorted(items, key=lambda c: c["cameraId"])})


@app.post("/api/cameras")
def register_camera():
    body = request.get_json(force=True, silent=True) or {}
    camera_id = body.get("cameraId", "")
    host, port = body.get("host"), int(body.get("port", 80))
    # A camera may have no password at all (a factory "admin" with a blank one): "" is stored as
    # given, and every ONVIF call still sends the username with an empty password's digest.
    user, password = body.get("user") or "", body.get("password") or ""
    stream_uri = body.get("streamUri")
    has_ir_control = bool(body.get("hasIrControl"))
    # Found at discovery (onvif_discovery: a G.711 microphone on the profile). Absent means
    # "not known" -- the scan couldn't log in -- and a re-registration then leaves the stored
    # value alone. audioEnabled is never set here: recording audio stays the user's choice.
    audio_capable = body.get("audioCapable")
    fps = body.get("fps")             # the form preselects onvif_media2.DEFAULT_FPS
    # Optional display name (guide §16.2.2). Absent: a re-registration keeps the stored one;
    # "" clears it. Checked before anything is changed anywhere.
    name_given = "displayName" in body

    if not CAMERA_ID_RE.match(camera_id):
        return jsonify({"error": "cameraId must look like 'cam-03'"}), 400
    if not all([host, user, stream_uri]):
        return jsonify({"error": "host, user, and streamUri are required (the password may be empty)"}), 400
    if not isinstance(user, str) or not isinstance(password, str):
        return jsonify({"error": "user and password must be strings"}), 400
    if audio_capable is not None and not isinstance(audio_capable, bool):
        return jsonify({"error": "audioCapable must be true or false"}), 400
    if fps is not None and fps not in onvif_media2.FPS_CHOICES:
        return jsonify({"error": f"fps must be one of {list(onvif_media2.FPS_CHOICES)}"}), 400

    table = cameras_table()
    existing = table.get_item(Key={"cameraId": camera_id}).get("Item")
    mediamtx_path = camera_control.mediamtx_path_name(camera_id)
    display_name = None
    if name_given:
        display_name, err = _display_name(body.get("displayName"), camera_id, _all_names(table))
        if err:
            return jsonify({"error": err}), 400

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
        # Re-registering an unregistered camera also brings it back: its path is added again
        # (Unregister deleted it), the mark is removed, and a kvs-cam@ instance is enabled
        # again, as provisioning left it -- enabled, not started (see unregister_camera).
        reactivate = camera_control.is_unregistered(existing)
        # The form offers the stream URIs the camera reports, which usually lack the credentials
        # a registered URI may carry (cam-02's ?username=..&password=..): keep those.
        stream_uri = onvif_media2.with_credentials(stream_uri, existing.get("rtspUrl"))
        try:
            stream_uri = onvif_discovery.rtsp_login(stream_uri, user, password)
        except onvif_discovery.RtspUnauthorized as e:
            return jsonify({"error": str(e)}), 400
        fps_result = _registration_fps(host, port, user, password, stream_uri, fps)

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
        # when the value is unchanged. A path with no configuration (an unregistered camera's,
        # or one a MediaMTX restart dropped) is added rather than patched: a patch 404s.
        want = {"source": stream_uri, "rtspTransport": "tcp"}
        try:
            conf = mediamtx_api.get_path_conf(mediamtx_path)
            if conf is None:
                mediamtx_api.add_path(mediamtx_path, want)
            elif conf.get("source") != stream_uri:
                mediamtx_api.patch_path(mediamtx_path, want)
        except (mediamtx_api.MediaMTXError, requests.RequestException) as e:
            return jsonify({"error": f"MediaMTX path update failed: {e}"}), 502

        # REMOVE is a no-op on a row without the mark, so every re-registration can carry it.
        sets = ("hasIrControl = :ir, onvifHost = :h, onvifPort = :p, onvifUser = :u, "
                "onvifPassword = :pw, rtspUrl = :uri, updatedAt = :t")
        values = {":ir": has_ir_control, ":h": host, ":p": port, ":u": user, ":pw": password,
                  ":uri": stream_uri, ":t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if audio_capable is not None:
            sets += ", audioCapable = :ac"
            values[":ac"] = audio_capable
        removes = "unregisteredAt"
        if name_given and display_name:
            sets += ", displayName = :dn"
            values[":dn"] = display_name
        elif name_given:
            removes += ", displayName"
        table.update_item(Key={"cameraId": camera_id},
                          UpdateExpression=f"SET {sets} REMOVE {removes}",
                          ExpressionAttributeValues=values)
        if reactivate and camera_control.unit_name(camera_id).startswith("kvs-cam@"):
            camera_control.set_boot_start(camera_id, True)
        detection = _registration_detection(camera_id, host, port, user, password)
        # The stream may have changed, and an unregistered camera's live path went with
        # Unregister: set the live view up again from what the camera says now.
        live = _apply_live_view(camera_id, {**existing, "onvifHost": host, "onvifPort": port,
                                            "onvifUser": user, "onvifPassword": password,
                                            "rtspUrl": stream_uri})
        _refresh_path_cache()
        return jsonify({"cameraId": camera_id, "kvsStreamArn": existing["kvsStreamArn"],
                        "updated": True, "reactivated": reactivate, "fps": fps_result,
                        "detectionModes": detection, "liveView": live}), 200

    # The login the camera's RTSP demands, in the URI MediaMTX pulls (#66). Refused: nothing is
    # created, rather than a camera whose every pull is a 401.
    try:
        stream_uri = onvif_discovery.rtsp_login(stream_uri, user, password)
    except onvif_discovery.RtspUnauthorized as e:
        return jsonify({"error": str(e)}), 400

    # 0. The frame rate, on the camera, before MediaMTX ever connects: a change can restart the
    #    camera's RTSP session, which costs nothing yet. Not fatal -- a camera without Media2
    #    keeps its own rate, and the response says so.
    fps_result = _registration_fps(host, port, user, password, stream_uri, fps)

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
    row = {
        "cameraId": camera_id,
        "mode": "passthrough",
        "hasIrControl": has_ir_control,
        "kvsStreamArn": stream_arn,
        "onvifHost": host,
        "onvifPort": port,
        "onvifUser": user,
        "onvifPassword": password,
        "rtspUrl": stream_uri,
        "audioCapable": bool(audio_capable),
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if display_name:
        row["displayName"] = display_name
    table.put_item(Item=row)

    detection = _registration_detection(camera_id, host, port, user, password)
    live = _apply_live_view(camera_id, row)    # registered on the main stream: live view on sub
    _refresh_path_cache()
    return jsonify({"cameraId": camera_id, "kvsStreamArn": stream_arn, "updated": False,
                    "fps": fps_result, "detectionModes": detection, "liveView": live}), 201


def _registration_detection(camera_id, host, port, user, password):
    """Record the camera's recording triggers at registration, so both GUIs restrict the
    selector from the first load. Never fails the registration; None when it couldn't ask."""
    try:
        dmap, _, web_url = _read_detection(host, port, user, password)
    except Exception:  # noqa: BLE001
        return None
    return _record_detection(camera_id, dmap, web_url=web_url)


def _set_camera_fps(client, stream_uri, fps):
    """Set the frame rate of the encoder behind `stream_uri`, keeping the keyframe interval in
    seconds (onvif_media2.fps_config). Returns the camera's read-back, which is only its claim;
    callers that need proof measure the wire (_verify_fps)."""
    prof = onvif_media2.stream_profile(client, stream_uri)
    cfg = client.encoder_config(prof["encoderToken"])
    now = onvif_media2.summarize(cfg)
    opts = client.encoder_options(prof["encoderToken"]).get(now["encoding"]) or {}
    if fps not in onvif_media2.fps_choices(opts, (now["width"], now["height"])):
        raise onvif_media2.OnvifError(f"this stream's encoder doesn't offer {fps} fps")
    if now["frameRate"] != fps:
        client.set_encoder_config(onvif_media2.fps_config(cfg, opts, fps))
    return onvif_media2.summarize(client.encoder_config(prof["encoderToken"]))


def _registration_fps(host, port, user, password, stream_uri, fps):
    """The registration form's frame rate applied to the camera, or None when none was asked.
    Never fails the registration: the camera's read-back or the reason goes in the response."""
    if fps is None:
        return None
    try:
        got = _set_camera_fps(onvif_media2.Media2(host, port, user, password), stream_uri, fps)
        return {"asked": fps, "configured": got["frameRate"], "govLength": got["govLength"]}
    except Exception as e:  # noqa: BLE001
        return {"asked": fps, "error": f"{type(e).__name__}: {e}"}


def _apply_live_view(camera_id, item, found=None):
    """Point the camera's live view at its sub stream, or back at the recorded stream
    (live_view.py, guide §21.7), from the camera's own stream list. Never fails the action that
    called it -- a registration or a stream switch has already worked; the outcome, or why the
    live view wasn't set up, goes in the response."""
    try:
        if found is None:
            found = onvif_media2.streams(onvif_media2.for_camera(item))
        uri = live_view.live_uri(found, item.get("rtspUrl"))
        return live_view.apply(camera_id, item, uri, cameras_table(),
                               get_session(REGION).client("kinesisvideo"))
    except Exception as e:  # noqa: BLE001
        return {"error": f"live view not set up ({type(e).__name__}: {e}) -- "
                         f"re-register the camera to retry"}


def _refresh_path_cache():
    """Rewrite sync_mediamtx_paths.py's offline copy of the registry now, through its own
    code, so a MediaMTX start without AWS sets up the paths as of this change rather than as
    of the last start. Best effort: the next online MediaMTX start rewrites it anyway."""
    try:
        import sync_mediamtx_paths
        sync_mediamtx_paths.load_cameras()
    except Exception as e:  # noqa: BLE001
        print(f"path cache not refreshed: {type(e).__name__}: {e}", flush=True)


def _all_names(table):
    """{cameraId: displayName} of every row, unregistered ones included: they come back with
    Re-register, and must not come back sharing a name."""
    names, kw = {}, {"ProjectionExpression": "cameraId, displayName"}
    while True:
        page = table.scan(**kw)
        names.update({i["cameraId"]: i.get("displayName") for i in page["Items"]})
        if "LastEvaluatedKey" not in page:
            return names
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _display_name(raw, camera_id, names):
    """(name or None, error or None). Empty means "no name": the GUIs then show the ID."""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "displayName must be text"
    name = " ".join(raw.split())                  # trimmed, inner whitespace collapsed
    if not name:
        return None, None
    if len(name) > DISPLAY_NAME_MAX or not DISPLAY_NAME_RE.match(name):
        return None, (f"a name has 1-{DISPLAY_NAME_MAX} characters: letters, digits, "
                      f"space, '-', '_' and '.'")
    if not re.search(r"[^\W_]", name):
        return None, "a name needs at least one letter or digit"
    if re.fullmatch(r"(?i)cam-?\d+", name):
        return None, "a name can't look like a camera ID"
    taken = next((cid for cid, n in names.items()
                  if cid != camera_id and n and n.casefold() == name.casefold()), None)
    if taken:
        return None, f"'{name}' is already the name of {taken}"
    return name, None


@app.post("/api/cameras/<camera_id>/name")
def set_display_name(camera_id):
    """Set or clear a camera's display name: {"displayName": "Kitchen"}, or "" to clear. Any
    camera, cam-01 included, unregistered ones too. Writes only that field (and updatedAt),
    and nothing when nothing changes. Edited here only; the cloud page shows it (GET /pir)."""
    if not CAMERA_ID_RE.match(camera_id):
        return jsonify({"error": "bad camera id"}), 400
    raw = (request.get_json(force=True, silent=True) or {}).get("displayName", "")
    table = cameras_table()
    names = _all_names(table)
    if camera_id not in names:
        return jsonify({"error": "unknown camera"}), 404
    name, err = _display_name(raw, camera_id, names)
    if err:
        return jsonify({"error": err}), 400
    if name == names[camera_id]:
        return jsonify({"cameraId": camera_id, "displayName": name, "unchanged": True})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if name:
        table.update_item(Key={"cameraId": camera_id},
                          UpdateExpression="SET displayName = :n, updatedAt = :t",
                          ExpressionAttributeValues={":n": name, ":t": now})
    else:
        table.update_item(Key={"cameraId": camera_id},
                          UpdateExpression="SET updatedAt = :t REMOVE displayName",
                          ExpressionAttributeValues={":t": now})
    return jsonify({"cameraId": camera_id, "displayName": name})


@app.post("/api/cameras/<camera_id>/unregister")
def unregister_camera(camera_id):
    """Hide a camera from both GUIs and stop everything that acts on it, keeping what it has.

    The row stays: the registry is shared with the successor, so rows are updated field by
    field (PIR-MQTT-VMS-PI4.md §4.3, rule 1). So do the KVS stream, the clips and every
    setting, which is what lets Re-register (Discover, then the button on the matching device)
    bring the camera back as it was. Three things change:
      1. unregisteredAt is set, the mark every reader skips (camera_control.is_unregistered);
      2. the producer is stopped and disabled: it costs nothing and doesn't start at boot;
      3. the MediaMTX path is deleted, so the Pi stops pulling the camera's stream.
    The registry goes first: with AWS unreachable nothing has changed yet, and the cloud page
    couldn't learn of it anyway.

    ONVIF cameras only, because Re-register finds a camera by discovery, which can't see the
    USB camera.
    """
    table = cameras_table()
    item = table.get_item(Key={"cameraId": camera_id}).get("Item")
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if item.get("mode") != "passthrough" or not item.get("onvifHost"):
        return jsonify({"error": f"only ONVIF cameras can be unregistered: Re-register finds "
                                 f"them again by discovery, which can't see '{camera_id}'"}), 409
    if camera_control.is_unregistered(item):
        return jsonify({"cameraId": camera_id, "unregisteredAt": item["unregisteredAt"]})

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    table.update_item(
        Key={"cameraId": camera_id},
        UpdateExpression="SET unregisteredAt = :t, updatedAt = :t",
        ExpressionAttributeValues={":t": now},
    )
    camera_control.set_stream(camera_id, False)
    camera_control.set_boot_start(camera_id, False)
    result = {"cameraId": camera_id, "unregisteredAt": now}
    try:
        # The live view's path too (live_view.py); its producer stopped with the recording one
        # (PartOf=). Its registry field and unit files stay, like everything else, until
        # Re-register sets the live view up again.
        mediamtx_api.delete_path(camera_control.mediamtx_path_name(camera_id))
        mediamtx_api.delete_path(camera_control.live_path_name(camera_id))
    except (mediamtx_api.MediaMTXError, requests.RequestException) as e:
        result["warning"] = f"MediaMTX path not removed ({e}); it goes at the next MediaMTX restart"
    _refresh_path_cache()
    return jsonify(result)


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
    # detectionModes: what the camera's own event list supports, recorded by GET .../detection.
    # A mode outside it would subscribe to a topic the camera never sends (FoundAndFixed.md #64).
    supported = item.get("detectionModes")
    if mode != "manual" and supported is not None and mode not in supported:
        return jsonify({"error": f"this camera doesn't publish the events for '{mode}' "
                                 f"(it supports: {', '.join(supported) or 'no detection mode'})"}), 409

    table.update_item(
        Key={"cameraId": camera_id},
        UpdateExpression="SET recordingMode = :m",
        ExpressionAttributeValues={":m": mode},
    )
    return jsonify({"cameraId": camera_id, "recordingMode": mode})


def _read_detection(host, port, user, password):
    """(detection map, topics, web URL) read from the camera: its event list (onvif_events) and
    its own settings page, if it has one (onvif_discovery.web_url)."""
    async def read():
        from onvif import ONVIFCamera
        import onvif_events
        cam = ONVIFCamera(host, int(port or 80), user, password, wsdl_dir=camera_control.WSDL_DIR)
        try:
            await cam.update_xaddrs()
            dmap, topics = await onvif_events.camera_detection(cam)
            try:
                ports = await onvif_discovery.http_ports(cam)
            except Exception:  # noqa: BLE001
                ports = []
            return dmap, topics, ports
        finally:
            await cam.close()
    dmap, topics, ports = asyncio.run(asyncio.wait_for(read(), 15))
    return dmap, topics, onvif_discovery.web_url(host, ports)


def _record_detection(camera_id, dmap, item=None, web_url=None):
    """Write detectionModes (the modes), detectionTopics (mode -> topic and on/off field) and
    webUrl (the camera's own settings page, where it has one) to the registry, field-level and
    only when they changed. New fields, invisible to the successor; recordingMode keeps its four
    shared values. Best effort: the next table load retries."""
    import onvif_events
    modes = onvif_events.supported_modes(dmap)
    topics = {m: {"topic": v["topic"], "field": v["field"]} for m, v in dmap.items()}
    if item is not None and item.get("detectionModes") == modes and \
            item.get("detectionTopics") == topics and (web_url is None or item.get("webUrl") == web_url):
        return modes
    sets, values = "detectionModes = :m, detectionTopics = :t", {":m": modes, ":t": topics}
    if web_url:
        sets += ", webUrl = :w"
        values[":w"] = web_url
    try:
        cameras_table().update_item(Key={"cameraId": camera_id}, UpdateExpression=f"SET {sets}",
                                    ExpressionAttributeValues=values)
    except Exception:  # noqa: BLE001
        pass
    return modes


@app.get("/api/cameras/<camera_id>/detection")
def get_detection_modes(camera_id):
    """The detection modes this camera can serve and the topic behind each, read from its own
    event list (onvif_events.detection_map), once per table load like the codec, and recorded in
    the registry so the cloud page (via GET /pir) and the mode routes offer the same choice."""
    try:
        item = _camera_item_quick(camera_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"registry unreachable ({type(e).__name__})"}), 503
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if not item.get("onvifHost"):
        return jsonify({"modes": [], "reason": "no ONVIF host: manual recording only"})
    try:
        dmap, topics, web_url = _read_detection(item["onvifHost"], item.get("onvifPort", 80),
                                                item["onvifUser"], item["onvifPassword"])
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"camera unreachable ({type(e).__name__})",
                        "modes": item.get("detectionModes"), "webUrl": item.get("webUrl"),
                        "tests": item.get("detectionTests")}), 502
    modes = _record_detection(camera_id, dmap, item, web_url)
    return jsonify({"modes": modes, "detection": dmap, "topics": sorted(topics),
                    "webUrl": web_url or item.get("webUrl"), "tests": item.get("detectionTests")})


# --- trigger test ------------------------------------------------------------------------------
#
# "Offered" is not "works": a camera's event list says which detections it has, not that they are
# switched on (cam-03's cell motion needed motion detection ticked in its own web interface) or
# that they reach ONVIF at all (its human alarm never does). The test listens to the camera for up
# to a minute while someone walks past, for every trigger it offers at once, and records what
# arrived. In memory while it runs, polled by the page without any AWS call (FoundAndFixed.md #55);
# the outcome goes to the registry as detectionTests when it ends, for both GUIs to show.

TRIGGER_TEST_SEC = 60
START_STATE_SEC = 2.0
_trigger_tests = {}                 # cameraId -> state, written by the test's thread
_trigger_lock = threading.Lock()


@app.post("/api/cameras/<camera_id>/detection/test")
def start_trigger_test(camera_id):
    secs = (request.get_json(force=True, silent=True) or {}).get("seconds", TRIGGER_TEST_SEC)
    if not isinstance(secs, int) or not 15 <= secs <= 180:
        return jsonify({"error": "seconds must be a whole number from 15 to 180"}), 400
    try:
        item = _camera_item_quick(camera_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"registry unreachable ({type(e).__name__})"}), 503
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if not item.get("onvifHost"):
        return jsonify({"error": "no ONVIF host: this camera has no events to test"}), 400
    with _trigger_lock:
        running = _trigger_tests.get(camera_id)
        if running and running["state"] in ("starting", "listening"):
            return jsonify({"error": "a test is already running for this camera"}), 409
        state = {"state": "starting", "cameraId": camera_id, "seconds": secs,
                 "startedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "modes": {}, "otherTopics": {}, "error": None, "_stop": threading.Event()}
        _trigger_tests[camera_id] = state
    threading.Thread(target=lambda: asyncio.run(_trigger_test(item, state)), daemon=True).start()
    return jsonify(_public_test(state)), 202


@app.get("/api/cameras/<camera_id>/detection/test")
def trigger_test_status(camera_id):
    with _trigger_lock:
        state = _trigger_tests.get(camera_id)
        return jsonify(_public_test(state) if state else {"state": "none", "cameraId": camera_id})


@app.delete("/api/cameras/<camera_id>/detection/test")
def stop_trigger_test(camera_id):
    with _trigger_lock:
        state = _trigger_tests.get(camera_id)
        if state:
            state["_stop"].set()
        return jsonify(_public_test(state) if state else {"state": "none", "cameraId": camera_id})


def _public_test(state):
    out = {k: copy.deepcopy(v) for k, v in state.items() if not k.startswith("_")}
    if state["state"] == "listening":
        out["secondsLeft"] = max(0, int(state["_deadline"] - time.monotonic()))
    return out


async def _trigger_test(item, state):
    """Listen for every trigger the camera offers until each has fired, the time is up, or Stop.

    What a fresh subscription delivers first -- the Initialized state and any backlog of earlier
    Changed messages, which cam-03 sends together 0.3 s after subscribing -- is the state *at the
    start*, not a detection during the walk: anything in the first START_STATE_SEC, or marked
    Initialized, is recorded apart and never counts as seen."""
    from onvif import ONVIFCamera
    import onvif_events
    cam = None
    try:
        cam = ONVIFCamera(item["onvifHost"], int(item.get("onvifPort", 80)), item["onvifUser"],
                          item["onvifPassword"], wsdl_dir=camera_control.WSDL_DIR)
        await cam.update_xaddrs()
        dmap, _ = await onvif_events.camera_detection(cam)
        by_topic = {v["topic"]: m for m, v in dmap.items()}
        with _trigger_lock:
            state["modes"] = {m: {"topic": v["topic"], "field": v["field"], "seen": 0,
                                  "firstAfterSec": None, "atStart": None} for m, v in dmap.items()}
        if not dmap:
            return
        async with onvif_events.PullSubscription(cam) as sub:
            t0 = time.monotonic()
            with _trigger_lock:
                state["state"], state["_deadline"] = "listening", t0 + state["seconds"]
            while time.monotonic() < state["_deadline"] and not state["_stop"].is_set():
                notes = await sub.pull()
                # The camera's opening report (Initialized state, backlog) arrives at once: cam-03
                # sent it 0.3 s after subscribing. A person can't have walked in by then.
                first = time.monotonic() - t0 < START_STATE_SEC
                with _trigger_lock:
                    for note in notes:
                        topic, op, data = onvif_events.parse_notification(note)
                        mode = by_topic.get(topic)
                        if mode is None:
                            state["otherTopics"][topic] = state["otherTopics"].get(topic, 0) + 1
                            continue
                        value = onvif_events.state_value(data, dmap[mode]["field"])
                        if value is None:
                            continue
                        on, entry = onvif_events.is_on(value), state["modes"][mode]
                        if first or op == "Initialized":
                            entry["atStart"] = on
                        elif on:
                            entry["seen"] += 1
                            if entry["firstAfterSec"] is None:
                                entry["firstAfterSec"] = round(time.monotonic() - t0, 1)
                    if all(e["seen"] for e in state["modes"].values()):
                        break
    except Exception as e:  # noqa: BLE001
        with _trigger_lock:
            state["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        if cam is not None:
            try:
                await cam.close()
            except Exception:  # noqa: BLE001
                pass
        with _trigger_lock:
            state["endedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            state["stopped"] = state["_stop"].is_set()
            state["state"] = "error" if state["error"] else "done"
        if not state["error"] and state["modes"]:
            _record_trigger_test(item["cameraId"], state)


def _record_trigger_test(camera_id, state):
    """Merge this test's outcome into the registry's detectionTests (mode -> result, when, how
    many), best effort. A stopped test records only what it saw: "not seen" needs the full time."""
    results = {}
    for mode, e in state["modes"].items():
        if e["seen"]:
            results[mode] = {"result": "seen", "at": state["endedAt"], "events": e["seen"]}
        elif not state["stopped"]:
            results[mode] = {"result": "not-seen", "at": state["endedAt"], "events": 0}
    if not results:
        return
    try:
        table = cameras_table()
        current = (table.get_item(Key={"cameraId": camera_id}).get("Item") or {}).get("detectionTests") or {}
        table.update_item(Key={"cameraId": camera_id}, UpdateExpression="SET detectionTests = :d",
                          ExpressionAttributeValues={":d": {**current, **results}})
    except Exception:  # noqa: BLE001
        pass


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


# --- Video codec (guide §21) ------------------------------------------------------------------
#
# The codec is a property of the camera, so switching it means switching the camera's own
# encoder over ONVIF Media2 (adapter/onvif_media2.py); nothing on the Pi transcodes. Everything
# downstream follows by itself: the camera drops RTSP, MediaMTX reconnects with the new track,
# the producer's watchdog restarts it on the matching chain, and stream-codec.py records
# videoCodecActive and the KVS MediaType. Measured on cam-03: ~6 s without video either way.
#
# cam-01 is encoded by the Pi, which has no H.265 encoder (guide §21.1), so it is shown, not
# offered.

CODEC_VERIFY_SEC = 45     # the camera's reconnect, plus one failed attempt on a half-ready SDP


def _camera_item_quick(camera_id):
    """The registry row, on short timeouts. The codec routes need the ONVIF password, which the
    local cache deliberately doesn't hold; they run once per page load, never from a poll
    (FoundAndFixed.md #55)."""
    from botocore.config import Config
    quick = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
    table = get_session(REGION).resource("dynamodb", config=quick).Table("cameras")
    return table.get_item(Key={"cameraId": camera_id}).get("Item")


def _codec_options(opts):
    return {c: ({"available": True, "encodedBy": "camera"} if e in opts else
                {"available": False, "reason": "the camera's encoder doesn't offer it"})
            for c, e in onvif_media2.ENCODING.items()}


@app.get("/api/cameras/<camera_id>/codec")
def get_codec(camera_id):
    """What the camera can send, what it is set to, and what MediaMTX actually receives.

    Read-only. `wire` is the truth and `configured` only the camera's claim: cam-03 claimed
    H264 2880x1620 while it sent H.265 2560x1440, so the page shows both when they differ.
    """
    try:
        item = _camera_item_quick(camera_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"registry unreachable ({type(e).__name__})"}), 503
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    out = {"cameraId": camera_id, "chosen": item.get("videoCodec"),
           "wire": mediamtx_api.path_video_track(camera_control.mediamtx_path_name(camera_id))}
    if not item.get("onvifHost"):
        return jsonify({**out, "switchable": False, "configured": "h264", "options": {
            "h264": {"available": True, "encodedBy": "pi-hw"},
            "h265": {"available": False, "reason": "encoded on the Pi, which has no H.265 "
                                                   "encoder (guide §21.1)"}}})
    try:
        cam = onvif_media2.for_camera(item)
        prof = onvif_media2.stream_profile(cam, item.get("rtspUrl"))
        settings = onvif_media2.summarize(cam.encoder_config(prof["encoderToken"]))
        opts = cam.encoder_options(prof["encoderToken"])
        options = _codec_options(opts)
    except onvif_media2.OnvifError as e:
        return jsonify({**out, "error": str(e)}), 502
    # The registry learns the codec only from a switch, so a camera registered already sending
    # H.265, or switched in its own web page, had no videoCodec or a stale one: the cloud page
    # then labelled it H.264 (list_cameras' default; FoundAndFixed.md #68). The wire is the
    # authority: record it when the registry disagrees. Once, since the next read agrees.
    if out["wire"] and item.get("videoCodec") != out["wire"]["codec"]:
        out["registry"] = _record_codec(camera_id, out["wire"]["codec"], opts, prof["encoderToken"])
        if out["registry"]["recorded"]:
            out["chosen"] = out["wire"]["codec"]
    return jsonify({**out, "options": options,
                    "switchable": sum(o["available"] for o in options.values()) > 1,
                    "configured": onvif_media2.CODEC.get(settings["encoding"], settings["encoding"]),
                    "settings": settings, "encoderToken": prof["encoderToken"]})


def _wait_for_wire(path, codec, budget=CODEC_VERIFY_SEC):
    """The video track once MediaMTX receives `codec` on `path`, or None after `budget`."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        track = mediamtx_api.path_video_track(path)
        if track and track["codec"] == codec:
            return track
        time.sleep(0.5)
    return None


def _record_codec(camera_id, codec, opts, encoder_token):
    """Write the choice to the registry, field by field. Best effort: the camera has already
    switched, and the producer records what it streams on its own (videoCodecActive).

    Same fields and values the successor writes (PIR-MQTT-VMS-PI4.md §4.3 rule 2, read by
    list_cameras.py): videoCodec is the admin's choice -- the cloud page labels the camera and
    warns a browser without HEVC from it -- and videoCodecCaps says who encodes each codec.
    videoCodecDefault is the successor's and is not touched.
    """
    caps = {c: ("camera" if e in opts else "none") for c, e in onvif_media2.ENCODING.items()}
    try:
        from botocore.config import Config
        quick = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
        get_session(REGION).resource("dynamodb", config=quick).Table("cameras").update_item(
            Key={"cameraId": camera_id},
            UpdateExpression=("SET videoCodec = :c, videoCodecCaps = :caps, "
                              "videoEncoderToken = :t, videoCodecProbe = :p"),
            ExpressionAttributeValues={":c": codec, ":caps": caps, ":t": encoder_token,
                                       ":p": "media2"})
        return {"recorded": True}
    except Exception as e:  # noqa: BLE001
        return {"recorded": False, "reason": f"{type(e).__name__}: {e}"}


@app.post("/api/cameras/<camera_id>/codec")
def set_codec(camera_id):
    """Switch the camera's encoder to {"videoCodec": "h264"|"h265"}, and prove it.

    Done means MediaMTX receives the new codec, not that the camera said yes: a camera's
    read-back can disagree with its stream (get_codec). Every other encoder setting is kept
    where the new codec allows it, and the picture size is taken from the wire rather than
    from the camera's possibly stale configuration (onvif_media2.plan_config). If the wire
    already carries the codec, the camera is not touched at all -- no reason to cost ~6 s of
    video.

    Not rolled back on failure, unlike the USB apply: either codec is a working stream, so a
    camera that is slow to come back is reported, not reverted into a second interruption.
    """
    target = (request.get_json(force=True, silent=True) or {}).get("videoCodec")
    if target not in onvif_media2.ENCODING:
        return jsonify({"error": f"videoCodec must be one of {sorted(onvif_media2.ENCODING)}"}), 400
    try:
        item = _camera_item_quick(camera_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"registry unreachable ({type(e).__name__})"}), 503
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if not item.get("onvifHost"):
        return jsonify({"error": "this camera is encoded on the Pi, which has no H.265 "
                                 "encoder -- it stays H.264 (guide §21.1)"}), 400

    path = camera_control.mediamtx_path_name(camera_id)
    t0 = time.monotonic()
    try:
        cam = onvif_media2.for_camera(item)
        prof = onvif_media2.stream_profile(cam, item.get("rtspUrl"))
        cfg = cam.encoder_config(prof["encoderToken"])
        opts = cam.encoder_options(prof["encoderToken"])
        encoding = onvif_media2.ENCODING[target]
        if encoding not in opts:
            return jsonify({"error": f"the camera's encoder doesn't offer {encoding}"}), 409
        wire = mediamtx_api.path_video_track(path)
        unchanged = bool(wire and wire["codec"] == target)
        if not unchanged:
            size = (wire["width"], wire["height"]) if wire else None
            cam.set_encoder_config(onvif_media2.plan_config(cfg, opts, encoding, size))
        settings = onvif_media2.summarize(cam.encoder_config(prof["encoderToken"]))
    except onvif_media2.OnvifError as e:
        return jsonify({"error": str(e)}), 502

    if not unchanged:
        wire = _wait_for_wire(path, target)
        if not wire:
            now = mediamtx_api.path_video_track(path)
            return jsonify({
                "error": f"the camera accepted {encoding} (it reads back "
                         f"{settings['encoding']}), but after {CODEC_VERIFY_SEC} s MediaMTX "
                         f"receives {now['codec'].upper() if now else 'no video'}",
                "settings": settings}), 504
    return jsonify({"cameraId": camera_id, "videoCodec": target, "unchanged": unchanged,
                    "wire": wire, "settings": settings,
                    "tookSec": round(time.monotonic() - t0, 1),
                    "registry": _record_codec(camera_id, target, opts, prof["encoderToken"])})


# --- stream and frame rate (guide §21.6) -------------------------------------------------------
#
# Which of the camera's streams this system pulls (main or sub) and at what frame rate. Like the
# codec, both are properties of the camera and stay LAN-side. Done means the wire: MediaMTX
# receives the other stream, or the frames arrive at the new rate -- never the camera's read-back.

FPS_TOLERANCE = 0.15       # cam-03 sends 19.8 fps at a 20 fps limit; a camera capping lower is reported
FPS_MEASURE_SEC = 4
FPS_VERIFY_SEC = 30


def _measure_fps(path, seconds=FPS_MEASURE_SEC):
    """Frames per second MediaMTX re-serves on `path`, from the timestamps of `seconds` of video
    packets. One reader for a few seconds; None when nothing usable arrives."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time", "-of", "csv=p=0",
             "-read_intervals", f"%+{seconds}", f"rtsp://127.0.0.1:8554/{path}"],
            capture_output=True, text=True, timeout=seconds + 10)
    except subprocess.TimeoutExpired:
        return None
    ts = []
    for line in r.stdout.split():
        try:
            ts.append(float(line.strip(",")))
        except ValueError:
            pass                          # "N/A": the first packets before RTCP sync
    ts.sort()
    if len(ts) < 3 or ts[-1] <= ts[0]:
        return None
    return round((len(ts) - 1) / (ts[-1] - ts[0]), 1)


def _fps_ok(measured, fps):
    return measured is not None and abs(measured - fps) <= FPS_TOLERANCE * fps


def _verify_fps(path, fps, budget=FPS_VERIFY_SEC):
    """Measure until the wire shows `fps` or the budget runs out; the last measurement either
    way. A camera may apply the rate in place or restart its RTSP session first."""
    deadline = time.monotonic() + budget
    measured = None
    while time.monotonic() < deadline:
        if (mediamtx_api.get_path_state(path) or {}).get("ready"):
            measured = _measure_fps(path)
            if _fps_ok(measured, fps):
                return measured
        time.sleep(2)
    return measured


def _wait_for_new_session(path, ready_before, budget=CODEC_VERIFY_SEC):
    """The video track once MediaMTX has reconnected to the path's (new) source -- readyTime
    differs from `ready_before` -- or None after `budget`."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        state = mediamtx_api.get_path_state(path) or {}
        if state.get("ready") and state.get("readyTime") != ready_before:
            track = mediamtx_api.path_video_track(path)
            if track:
                return track
        time.sleep(0.5)
    return None


def _stream_view(x):
    return {k: x[k] for k in ("name", "encoding", "width", "height", "fps", "govLength", "fpsChoices")}


@app.get("/api/cameras/<camera_id>/stream")
def get_stream_choice(camera_id):
    """The camera's streams, which one this system pulls, and the frame rates each accepts.
    Read-only, once per table load (FoundAndFixed.md #55); fps here is the camera's setting."""
    try:
        item = _camera_item_quick(camera_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"registry unreachable ({type(e).__name__})"}), 503
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if not item.get("onvifHost"):
        return jsonify({"switchable": False,
                        "reason": "the USB camera's frame rate is set in section 4 (cam01.env)"})
    try:
        found = onvif_media2.streams(onvif_media2.for_camera(item))
    except onvif_media2.OnvifError as e:
        return jsonify({"error": str(e)}), 502
    current = next((x["name"] for x in found
                    if onvif_media2.same_stream(x["streamUri"], item.get("rtspUrl"))), None)
    live_path = (camera_control.live_path_name(camera_id) if item.get("liveRtspUrl")
                 else camera_control.mediamtx_path_name(camera_id))
    return jsonify({"cameraId": camera_id, "switchable": True, "current": current,
                    "streams": [_stream_view(x) for x in found],
                    "wire": mediamtx_api.path_video_track(camera_control.mediamtx_path_name(camera_id)),
                    # What live view (preview and cloud player) shows: guide §21.7.
                    "live": {"stream": "sub" if item.get("liveRtspUrl") else current,
                             "path": live_path, "wire": mediamtx_api.path_video_track(live_path)}})


@app.post("/api/cameras/<camera_id>/stream")
def set_stream_choice(camera_id):
    """Switch to {"stream": "main"|"sub"} and/or set {"fps": 10|15|20|25}, and prove it.

    A stream switch points the MediaMTX path at the other profile's URI (credentials carried
    over, onvif_media2.with_credentials) and writes rtspUrl first, because a MediaMTX restart
    rebuilds the path from the registry. MediaMTX reconnecting with video is the proof; the
    producer follows on its own, like after a codec switch. The frame rate is then set on the
    encoder of the stream now pulled, keyframe interval kept in seconds, and proved by counting
    frames on the wire. A camera that sends fewer than asked is reported, not rolled back.
    """
    body = request.get_json(force=True, silent=True) or {}
    want_stream, want_fps = body.get("stream"), body.get("fps")
    if want_stream is None and want_fps is None:
        return jsonify({"error": "give stream and/or fps"}), 400
    if want_fps is not None and want_fps not in onvif_media2.FPS_CHOICES:
        return jsonify({"error": f"fps must be one of {list(onvif_media2.FPS_CHOICES)}"}), 400
    try:
        item = _camera_item_quick(camera_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"registry unreachable ({type(e).__name__})"}), 503
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if not item.get("onvifHost"):
        return jsonify({"error": "the USB camera's frame rate is set in section 4 (cam01.env)"}), 400
    if camera_control.is_unregistered(item):
        return jsonify({"error": f"'{camera_id}' is unregistered: Re-register it first"}), 409

    path = camera_control.mediamtx_path_name(camera_id)
    t0 = time.monotonic()
    out = {"cameraId": camera_id}
    try:
        cam = onvif_media2.for_camera(item)
        found = onvif_media2.streams(cam)
        target = next((x for x in found
                       if onvif_media2.same_stream(x["streamUri"], item.get("rtspUrl"))), None)
        if target is None:
            return jsonify({"error": "none of the camera's streams is the registered one -- "
                                     "re-register the camera"}), 502

        if want_stream and want_stream != target["name"]:
            new = next((x for x in found if x["name"] == want_stream), None)
            if new is None:
                return jsonify({"error": f"the camera has no '{want_stream}' stream"}), 400
            new_uri = onvif_media2.with_credentials(new["streamUri"], item["rtspUrl"])
            ready_before = (mediamtx_api.get_path_state(path) or {}).get("readyTime")
            table = cameras_table()
            table.update_item(Key={"cameraId": camera_id},
                              UpdateExpression="SET rtspUrl = :u, updatedAt = :t",
                              ExpressionAttributeValues={":u": new_uri, ":t": datetime.now(
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            try:
                mediamtx_api.patch_path(path, {"source": new_uri, "rtspTransport": "tcp"})
            except (mediamtx_api.MediaMTXError, requests.RequestException) as e:
                table.update_item(Key={"cameraId": camera_id}, UpdateExpression="SET rtspUrl = :u",
                                  ExpressionAttributeValues={":u": item["rtspUrl"]})
                return jsonify({"error": f"MediaMTX path update failed: {e}"}), 502
            _refresh_path_cache()
            wire = _wait_for_new_session(path, ready_before)
            if not wire:
                return jsonify({"error": f"switched to the {want_stream} stream, but after "
                                         f"{CODEC_VERIFY_SEC} s MediaMTX receives no video from it"}), 504
            # videoCodec etc. with the successor's meanings: the codec now delivered is the new
            # stream's, which can differ (cam-03: main H.265, sub H.264).
            out["registry"] = _record_codec(camera_id, wire["codec"],
                                            cam.encoder_options(new["encoderToken"]), new["encoderToken"])
            out["stream"], out["wire"] = new["name"], wire
            target, item = new, {**item, "rtspUrl": new_uri}
            # Recording moved to main: live view goes on to sub; moved to sub: live view is the
            # recorded stream again.
            out["liveView"] = _apply_live_view(camera_id, item, found)
            _refresh_path_cache()

        if want_fps is not None:
            settings = _set_camera_fps(cam, item["rtspUrl"], want_fps)
            measured = _verify_fps(path, want_fps)
            out["fps"] = {"asked": want_fps, "measured": measured,
                          "configured": settings["frameRate"], "govLength": settings["govLength"]}
            if not _fps_ok(measured, want_fps):
                out["warning"] = (f"the camera accepted {want_fps} fps, but MediaMTX receives "
                                  + (f"{measured} fps" if measured else "no countable video"))
    except onvif_media2.OnvifError as e:
        return jsonify({"error": str(e), **out}), 502

    fps_now = (out.get("fps") or {}).get("measured") or target["fps"] or 0
    if item.get("audioCapable") and item.get("audioEnabled") and fps_now >= 24:
        out["note"] = ("audio at 25 fps is unmeasured (guide §18.3, measured at 15 and 20): "
                       "count 0x30000005 rejects after the next Start")
    out["tookSec"] = round(time.monotonic() - t0, 1)
    return jsonify(out)


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
    item = cameras_table().get_item(Key={"cameraId": camera_id}).get("Item")
    if not item:
        return jsonify({"error": "unknown camera"}), 404
    if action == "start" and camera_control.is_unregistered(item):
        return jsonify({"error": f"'{camera_id}' is unregistered: Re-register it first"}), 409
    camera_control.set_stream(camera_id, action == "start")
    return jsonify({"cameraId": camera_id, "action": action})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
