"""Shared camera-control logic used by both agent.py (cloud MQTT control plane) and the
local ONVIF admin GUI (adapter/onvif-admin/app.py) -- one implementation of "how do we
talk to a camera" instead of two copies that drift apart. ONVIF credentials are read
from the caller-supplied camera registry item (the "cameras" DynamoDB table row) rather
than a hardcoded dict, so a camera registered through the admin GUI gets working IR
control over the cloud/MQTT path too, with no manual edit anywhere.
"""
import asyncio
import subprocess

from onvif import ONVIFCamera

WSDL_DIR = "/home/vladimir/MyProjects/VMS/venv-adapter/lib/python3.13/site-packages/onvif/wsdl"
ALLOWED_IR_MODES = {"AUTO", "ON", "OFF"}


def mediamtx_path_name(camera_id: str) -> str:
    # "cam-03" -> "cam03" -- the hyphen-free form used for both the MediaMTX path name
    # and the kvs-cam01.service/kvs-cam02.service systemd unit suffix, everywhere the
    # hyphenated "cam-03" identifier (KVS stream name, DynamoDB cameraId, S3 key prefix)
    # can't be used directly. One helper here means the naming mismatch that once made
    # agent.py silently no-op on cam-02 commands (f"kvs-{camera_id}.service" -> the
    # nonexistent "kvs-cam-02.service") can't reappear in a second caller.
    return camera_id.replace("-", "")


def unit_name(camera_id: str) -> str:
    return f"kvs-{mediamtx_path_name(camera_id)}.service"


def set_stream(camera_id: str, on: bool):
    action = "start" if on else "stop"
    subprocess.run(["sudo", "systemctl", action, unit_name(camera_id)], check=False)


def get_stream_status(camera_id: str) -> str:
    # `systemctl is-active` on a *system* unit is an unprivileged read (unlike
    # start/stop) -- no sudo needed here. Always prints the actual state to stdout
    # ("active", "inactive", "failed", "activating", ...) regardless of exit code, so
    # capture stdout unconditionally rather than gating on returncode == 0.
    result = subprocess.run(
        ["systemctl", "is-active", unit_name(camera_id)],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


async def _set_ir_mode_async(camera_item: dict, mode: str):
    cam = ONVIFCamera(
        camera_item["onvifHost"], int(camera_item.get("onvifPort", 80)),
        camera_item["onvifUser"], camera_item["onvifPassword"],
        wsdl_dir=WSDL_DIR,
    )
    await cam.update_xaddrs()
    imaging = await cam.create_imaging_service()
    media = await cam.create_media_service()
    profiles = await media.GetProfiles()
    video_source_token = profiles[0].VideoSourceConfiguration.SourceToken
    await imaging.SetImagingSettings({
        "VideoSourceToken": video_source_token,
        "ImagingSettings": {"IrCutFilter": mode},
    })
    await cam.close()


def set_ir_mode(camera_item: dict, mode: str):
    if mode not in ALLOWED_IR_MODES:
        raise ValueError(f"invalid IR mode '{mode}'")
    asyncio.run(_set_ir_mode_async(camera_item, mode))
