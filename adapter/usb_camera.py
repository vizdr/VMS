"""What USB cameras are attached to this Pi, and what each of them can actually do.

Shared by the local admin GUI (`GET /api/usb-cameras`, the "Scan USB cameras" button) and
the command line (`python3 adapter/usb_camera.py`), the same way `onvif_discovery` is
shared by the GUI and `adapter/bin/discover-onvif.py`. Read-only: it changes no device
setting, writes no file and touches no registry row.

Why a scan exists at all: every value in `/etc/adapter/cameras/<path>.env` -- the capture
format, the resolution, the frame rate, the v4l2 controls -- is a fact about one particular
camera. Swapping in a different webcam means every one of them has to be re-derived, and
the driver already knows all of it. This reports what the driver says, so the GUI can offer
real choices instead of a form full of numbers typed from a datasheet.

Two rules it follows, both learned the hard way elsewhere in this project:

  - **Ask the device, don't trust a name.** `Camera-Features.md` marks ONVIF features
    "verified" vs "advertised" because that camera's own claims are unreliable. V4L2 is
    better: a menu control enumerates exactly the entries the driver will accept, and
    values outside a reported range are refused. So capability here is fact, not claim.
  - **One implementation of the USB-parent rule.** Matching a microphone to its camera is
    done by `alsa_card_for_video` in `adapter/bin/detect-hw.sh`, and this module calls
    that function rather than reimplementing it -- two copies of one fact always drift.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time

VMS_HOME = os.environ.get("VMS_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETECT_HW = os.path.join(VMS_HOME, "adapter", "bin", "detect-hw.sh")
V4L_BYID_DIR = os.environ.get("V4L_BYID_DIR", "/dev/v4l/by-id")

# A capture node advertises at least one of these. Anything else (a UVC metadata node, for
# instance, which lists no formats at all) is not a camera for our purposes.
CAPTURE_FORMATS = ("MJPG", "YUYV", "H264")

# Controls worth offering in a GUI. Deliberately a whitelist: a camera exposes plenty that
# would only confuse (pan/tilt/zoom on a fixed mount), and the pipeline cares about none of
# them. Everything here is settable through /etc/adapter/cameras/<path>.env.
INTERESTING_CONTROLS = (
    "auto_exposure", "exposure_time_absolute", "exposure_dynamic_framerate",
    "white_balance_automatic", "white_balance_temperature",
    "focus_automatic_continuous", "focus_absolute",
    "brightness", "contrast", "saturation", "gamma", "sharpness",
    "backlight_compensation", "power_line_frequency",
)


def _run(cmd, timeout=5):
    """stdout of `cmd`, or "" -- a probe that fails must not fail the whole scan."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _v4l2(dev, *args, timeout=5):
    return _run(["v4l2-ctl", "-d", dev, *args], timeout=timeout)


def capture_devices():
    """Every capture-capable node, as its stable /dev/v4l/by-id path."""
    out = []
    for dev in sorted(glob.glob(os.path.join(V4L_BYID_DIR, "*-video-index*"))):
        formats = _v4l2(dev, "--list-formats")
        if any(f"'{f}'" in formats for f in CAPTURE_FORMATS):
            out.append(dev)
    return out


def identity(dev):
    """Human-readable name and USB location -- what the GUI shows to tell cameras apart."""
    info = _v4l2(dev, "--info")

    def field(key):
        m = re.search(rf"{key}\s*:\s*(.+)", info)
        return m.group(1).strip() if m else None

    # The by-id name embeds the USB serial; it is the only per-unit identifier available
    # without root, and it is what CAM_MATCH matches against.
    return {
        "byId": os.path.basename(dev),
        "node": os.path.realpath(dev),
        "name": field("Card type"),
        "bus": field("Bus info"),
        "driver": field("Driver name"),
    }


def formats(dev):
    """[{format, description, sizes: [{width, height, fps: [...]}]}] from the driver.

    This is what makes a resolution/frame-rate dropdown honest: only combinations the
    camera reports are offered, so the pipeline cannot be configured into a caps
    negotiation failure.
    """
    out, cur, size = [], None, None
    for line in _v4l2(dev, "--list-formats-ext").splitlines():
        m = re.match(r"\s*\[\d+\]:\s*'(\w+)'\s*\((.*?)\)", line)
        if m:
            cur = {"format": m.group(1), "description": m.group(2), "sizes": []}
            out.append(cur)
            size = None
            continue
        m = re.match(r"\s*Size: Discrete (\d+)x(\d+)", line)
        if m and cur is not None:
            size = {"width": int(m.group(1)), "height": int(m.group(2)), "fps": []}
            # The driver repeats a size once per frame-interval group; keep one entry.
            existing = next((s for s in cur["sizes"]
                             if s["width"] == size["width"] and s["height"] == size["height"]), None)
            if existing:
                size = existing
            else:
                cur["sizes"].append(size)
            continue
        m = re.match(r"\s*Interval: Discrete [\d.]+s \(([\d.]+) fps\)", line)
        if m and size is not None:
            fps = float(m.group(1))
            fps = int(fps) if fps.is_integer() else fps
            if fps not in size["fps"]:
                size["fps"].append(fps)
    return out


def controls(dev):
    """{name: {type, min, max, step, default, value, menu:[{value,label}]}} for the
    controls worth exposing. Menu entries are the driver's own list, so they are exactly
    the modes it will accept -- the gaps in the range are not options (setting
    auto_exposure to 0 or 2 on the PW310 returns Invalid argument)."""
    out, cur = {}, None
    for line in _v4l2(dev, "--list-ctrls-menus").splitlines():
        m = re.match(r"\s*([a-zA-Z0-9_]+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)", line)
        if m:
            name, ctype, rest = m.groups()
            cur = None
            if name not in INTERESTING_CONTROLS:
                continue
            fields = {k: int(v) for k, v in re.findall(r"(min|max|step|default|value)=(-?\d+)", rest)}
            entry = {"type": ctype, **fields}
            if ctype == "menu":
                entry["menu"] = []
            out[name] = entry
            cur = name if ctype == "menu" else None
            continue
        m = re.match(r"\s+(\d+): (.+)", line)
        if m and cur:
            out[cur]["menu"].append({"value": int(m.group(1)), "label": m.group(2).strip()})
    # exposure_time_absolute is in units of 100us, which no UI should have to guess.
    if "exposure_time_absolute" in out:
        out["exposure_time_absolute"]["unit"] = "100us"
    return out


def alsa_card_for(dev):
    """The ALSA card on the SAME USB device as `dev`, or None.

    Delegated to detect-hw.sh so the rule lives in one place. Matching by USB parent and
    not by card name is the whole point: ALSA calls many webcams' microphones plain
    "Webcam", and a second one becomes "Webcam_1" in plug-in order.
    """
    out = _run(["bash", "-c", f'source "{DETECT_HW}" >/dev/null 2>&1; alsa_card_for_video "$1"',
                "_", dev]).strip()
    return out or None


def alsa_capabilities(card):
    """What the microphone supports, or a reason we could not ask.

    "busy" is a normal answer, not a failure: while audio is enabled the publisher holds
    the device, and a scan must still succeed -- it is read-only and must never be the
    thing that disturbs a running camera.
    """
    if not card:
        return None
    try:
        p = subprocess.run(["arecord", "-D", card, "--dump-hw-params", "-d", "1", "/dev/null"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {"card": card, "probe": "failed"}
    text = p.stdout + p.stderr
    if "Device or resource busy" in text:
        return {"card": card, "probe": "busy (in use by the publisher)"}

    def field(key):
        m = re.search(rf"^{key}:\s*(.+)$", text, re.M)
        return m.group(1).strip() if m else None

    return {"card": card, "probe": "ok", "formats": (field("FORMAT") or "").split() or None,
            "channels": field("CHANNELS"), "rates": field("RATE")}


def selected_device():
    """The device detection would use right now, so the GUI can mark it -- and show when a
    config file pins something other than the obvious choice."""
    out = _run(["bash", DETECT_HW, "--print"], timeout=40)
    # "NOT FOUND" is what --print says when detection failed (no camera, or several and
    # therefore ambiguous). Matching \S+ against that line happily returns the word "NOT",
    # which would then be reported as the selected device.
    m = re.search(r"^video device\s*:\s*(?!NOT FOUND)(\S+)", out, re.M)
    pinned = "(present)" in (re.search(r"^config file\s*:.*$", out, re.M) or [""])[0] \
        if re.search(r"^config file\s*:.*$", out, re.M) else False
    return (m.group(1) if m else None), pinned


CONFIG_DIR = os.environ.get("ADAPTER_CAMERAS_DIR", "/etc/adapter/cameras")
PUBLISH_SH = os.path.join(VMS_HOME, "adapter", "bin", "publish-cam01.sh")
CAMERA_INIT_SH = os.path.join(VMS_HOME, "adapter", "bin", "camera-init.sh")


def effective_config(path="cam01"):
    """The settings the pipeline would run with right now: the config file's values where
    it sets them, each script's own default where it does not.

    Asked of the scripts (`--print-config`) rather than reproduced here. The defaults are
    written in the scripts that use them, and a second copy in this module would be a
    second thing to update -- exactly the drift this project keeps running into.
    """
    env = dict(os.environ, CAMERA_WAIT_SEC="0")
    out = {}
    for script in (PUBLISH_SH, CAMERA_INIT_SH):
        try:
            p = subprocess.run(["bash", script, "--print-config"], capture_output=True,
                               text=True, timeout=15, env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in p.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


def scan():
    """Everything the GUI needs for one press of "Scan USB cameras"."""
    chosen, pinned = selected_device()
    cameras = []
    for dev in capture_devices():
        card = alsa_card_for(dev)
        cameras.append({
            **identity(dev),
            "device": dev,
            "selected": dev == chosen,
            "formats": formats(dev),
            "controls": controls(dev),
            "audio": alsa_capabilities(card) if card else {"card": None, "probe": "no microphone on this camera"},
        })
    config_file = os.path.join(CONFIG_DIR, "cam01.env")
    return {"cameras": cameras, "selected": chosen, "configFilePresent": pinned,
            "ambiguous": len(cameras) > 1,
            "configFile": config_file, "config": effective_config()}


def set_control(name, value, dev=None):
    """Set one v4l2 control on the selected camera, immediately, and report what stuck.

    Live, without restarting anything: V4L2 controls can be written while the publisher
    holds the device (verified on this Pi), which is what makes dialling exposure in
    against the preview possible at all. The trade-off is that it is NOT persistent --
    `camera-init.sh` reapplies the config file's values on the next start -- so the GUI
    offers this for finding a value and the config file for keeping it.

    The read-back is not decoration. This driver CLAMPS an out-of-range value instead of
    refusing it (a requested white_balance_temperature=99999 came back as 6500), so the
    only way to know what a camera actually did is to ask it afterwards.
    """
    dev = dev or selected_device()[0]
    if not dev:
        raise ValueError("no camera selected")
    meta = controls(dev).get(name)
    if meta is None:
        raise ValueError(f"this camera has no control '{name}'")
    try:
        want = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: value must be a whole number")
    if meta["type"] == "menu":
        allowed = [m["value"] for m in meta.get("menu", [])]
        if want not in allowed:
            raise ValueError(f"{name}: {want} is not one of {allowed}")
    else:
        lo, hi = meta.get("min"), meta.get("max")
        if lo is not None and hi is not None and not (lo <= want <= hi):
            raise ValueError(f"{name}: {want} is outside {lo}-{hi}")

    p = subprocess.run(["v4l2-ctl", "-d", dev, f"--set-ctrl={name}={want}"],
                       capture_output=True, text=True, timeout=5)
    if p.returncode != 0:
        raise ValueError((p.stderr or p.stdout).strip() or f"{name}: rejected")
    got = controls(dev).get(name, {}).get("value")
    return {"control": name, "requested": want, "actual": got, "clamped": got != want}


def registry_snapshot(result=None):
    """The hardware facts worth recording in the `cameras` row, from a scan.

    Only facts about the camera itself -- never the pipeline settings, which live in
    /etc/adapter/cameras/cam01.env because they must be readable with AWS unreachable.

    `audioCapable` is the one that does real work: `camera-audio.py` gates the producer's
    audio on it, both GUIs grey out the audio checkbox without it, and `set_camera_audio`
    refuses to enable audio for a camera that lacks it. Swap in a webcam with no
    microphone and that flag has to become false, or every one of those offers audio that
    cannot work -- and enabling it would stall the *video* too, since kvssink collects
    across pads.

    The rest is descriptive, for a GUI that cannot scan the LAN itself. It carries
    `usbCapsProbedAt` for the same reason `videoCodecProbe` does: it is a snapshot taken
    at a moment, not live truth, and the camera can be unplugged five seconds later.
    """
    result = result or scan()
    cam = next((c for c in result["cameras"] if c["selected"]), None)
    if cam is None:
        return None
    serial = ""
    m = re.match(r"^usb-(.+)-video-index\d+$", cam["byId"])
    if m and "_" in m.group(1):
        serial = m.group(1).rsplit("_", 1)[1]
    mic = (cam.get("audio") or {}).get("card")
    return {
        "audioCapable": bool(mic),
        "cameraModel": cam.get("name") or "",
        "cameraSerial": serial,
        "usbCapsProbedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "usbCaps": {
            # Strings throughout: DynamoDB rejects native floats, and a frame rate can be
            # fractional (7.5 fps is a real UVC interval).
            "formats": [{"format": f["format"],
                         "sizes": [f"{s['width']}x{s['height']}" for s in f["sizes"]]}
                        for f in cam["formats"]],
            "exposureModes": [m["label"] for m in
                              (cam["controls"].get("auto_exposure", {}).get("menu") or [])],
            "controls": sorted(cam["controls"].keys()),
            "microphone": mic or "",
        },
    }


if __name__ == "__main__":
    json.dump(scan(), sys.stdout, indent=2)
    print()
